"""Tenant-aware database access: a psycopg connection bound to one trusted tenant (D-020).

    TenantDatabase(conn, tenant)   # tenant: the TenantIdentity from resolve_tenant() / current_tenant
    tenant_db.list_users()         # only the tenant's rows (likewise list_policies(), list_documents(policy_id))
    tenant_db.create_policy(...)   # a new row always gets the tenant's company_id

Every query on tenant-owned data goes through a ``TenantDatabase`` method. The tenant is fixed once, when the object
is built, and every query filters on ``TenantIdentity.company_id`` as a bound SQL parameter. No method takes a
company id, so a caller cannot pick another tenant, and the object exposes no raw ``execute``.

It fails closed. Anything but a valid ``TenantIdentity`` is refused before any query runs. There is no default,
first, nil, NULL or caller-supplied tenant, and no role (COMPANY_ADMIN included) bypasses the filter. As a second
line of defence, rows that do not belong to the tenant are never returned: they raise instead.

Scope: this module knows only the connection and the tenant. It does not read HTTP requests, decode tokens or check
permissions (RBAC). It does not decide resource ownership beyond the tenant, and there is no PostgreSQL row-level
security. ``list_users`` and ``get_user`` establish and test the boundary on ``users``; they are not a
user-management API. ``list_policies``, ``get_policy``, ``list_documents`` and ``get_document`` are the minimal
tenant-scoped reads of ``policies`` and ``policy_documents`` (Phase 2.1).

Writes (Phase 2.2: ``create_policy``, ``update_policy``, ``set_policy_status``, ``delete_policy``) follow the same
rule. An INSERT takes ``company_id`` only from the TenantIdentity; an UPDATE or DELETE filters on it in its own WHERE
clause, so another tenant's row is never touched and is reported exactly like a missing one (None / False). No
statement ever sets ``company_id``. The database's constraints stay authoritative: their errors (psycopg
``IntegrityError`` subclasses, e.g. a duplicate policy number or a policy that still has documents) propagate
unchanged. Methods never commit; the caller owns the transaction.

Chunk embeddings (Phase 5: ``insert_chunk_embedding``, ``list_chunk_embeddings``, ``get_chunk_embedding``,
``delete_chunk_embedding``) are tenant-owned rows like the others. An insert stores the tenant's company with the
caller's policy beside it, and the composite foreign key to ``policy_documents`` refuses any document, policy or
version that is not the tenant's. An existing embedding of the same chunk by the same model is left as it is
(``ON CONFLICT DO NOTHING``; the insert returns None).

Chunk search (Phase 6: ``search_chunks_by_vector``, ``search_chunks_by_keywords``) reads ``chunk_embeddings`` joined to
the document and policy rows of the same company. Every table is filtered on the tenant in SQL; optional policy,
document and insurance-type filters are bound values (``COALESCE``: None means no filter), so a filter naming
another tenant's id simply matches nothing. Only UPLOADED documents of ACTIVE policies are searched (the interim
rule for invariant 6, D-029). Vectors are never selected.

Document writes (Phase 2.3: ``create_document``, ``update_document``, ``set_document_status``, ``delete_document``)
add one rule. A new document row stores the tenant's ``company_id`` with the caller's ``policy_id`` beside it, so the
composite foreign key (company_id, policy_id) → policies (company_id, id) makes PostgreSQL refuse another tenant's
policy exactly like a missing one (``ForeignKeyViolation``, D-024). A version's identity (policy, key, version) and
its file metadata never change in place (D-025): only the display file name and the status do. Only metadata is
stored; no file is read, hashed or written here.
"""

import logging
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict

from app.models import (ChunkEmbedding, ChunkMatch, DocumentStatus, InsuranceType, Policy, PolicyDocument,
                        PolicyStatus, UserRole, UserStatus)
from app.tenancy import TenantIdentity

logger = logging.getLogger(__name__)

TENANT_SCOPE_FAILURE_MESSAGE = "Tenant-scoped database access refused"

# Literal SQL with bound parameters only: no value is ever formatted into these strings.
_LIST_USERS_SQL = """
    SELECT id, company_id, email, full_name, role, status
    FROM users
    WHERE company_id = %(company_id)s
    ORDER BY email, id
"""
_GET_USER_SQL = """
    SELECT id, company_id, email, full_name, role, status
    FROM users
    WHERE company_id = %(company_id)s AND id = %(user_id)s
"""
_LIST_POLICIES_SQL = """
    SELECT id, company_id, policy_number, name, insurance_type, status, created_at, updated_at
    FROM policies
    WHERE company_id = %(company_id)s
    ORDER BY policy_number, id
"""
_GET_POLICY_SQL = """
    SELECT id, company_id, policy_number, name, insurance_type, status, created_at, updated_at
    FROM policies
    WHERE company_id = %(company_id)s AND id = %(policy_id)s
"""
# Policy writes. The new row's company_id is the bound tenant; updates and deletes filter on it in WHERE. A NULL
# (None) in an update means "leave unchanged": every column is NOT NULL, so NULL is never a value to store.
_CREATE_POLICY_SQL = """
    INSERT INTO policies (company_id, policy_number, name, insurance_type, status)
    VALUES (%(company_id)s, %(policy_number)s, %(name)s, %(insurance_type)s, %(status)s)
    RETURNING id, company_id, policy_number, name, insurance_type, status, created_at, updated_at
"""
_UPDATE_POLICY_SQL = """
    UPDATE policies
    SET policy_number = COALESCE(%(policy_number)s, policy_number),
        name = COALESCE(%(name)s, name),
        insurance_type = COALESCE(%(insurance_type)s, insurance_type)
    WHERE company_id = %(company_id)s AND id = %(policy_id)s
    RETURNING id, company_id, policy_number, name, insurance_type, status, created_at, updated_at
"""
_SET_POLICY_STATUS_SQL = """
    UPDATE policies
    SET status = %(status)s
    WHERE company_id = %(company_id)s AND id = %(policy_id)s
    RETURNING id, company_id, policy_number, name, insurance_type, status, created_at, updated_at
"""
_DELETE_POLICY_SQL = """
    DELETE FROM policies
    WHERE company_id = %(company_id)s AND id = %(policy_id)s
    RETURNING id, company_id, policy_number, name, insurance_type, status, created_at, updated_at
"""
_LIST_DOCUMENTS_SQL = """
    SELECT id, company_id, policy_id, document_key, version, status, original_filename, storage_reference,
           content_type, size_bytes, checksum_sha256, created_at, updated_at
    FROM policy_documents
    WHERE company_id = %(company_id)s AND policy_id = %(policy_id)s
    ORDER BY document_key, version, id
"""
_GET_DOCUMENT_SQL = """
    SELECT id, company_id, policy_id, document_key, version, status, original_filename, storage_reference,
           content_type, size_bytes, checksum_sha256, created_at, updated_at
    FROM policy_documents
    WHERE company_id = %(company_id)s AND id = %(document_id)s
"""
# Document writes. The new row's company_id is the bound tenant, with policy_id beside it: the composite foreign key
# checks the pair, so a policy of another tenant (or none) is refused by the database. Updates and deletes filter on
# the tenant and the document id in WHERE. Only original_filename and status are ever SET (D-025).
_CREATE_DOCUMENT_SQL = """
    INSERT INTO policy_documents (company_id, policy_id, document_key, version, status, original_filename,
                                  storage_reference, content_type, size_bytes, checksum_sha256)
    VALUES (%(company_id)s, %(policy_id)s, %(document_key)s, %(version)s, %(status)s, %(original_filename)s,
            %(storage_reference)s, %(content_type)s, %(size_bytes)s, %(checksum_sha256)s)
    RETURNING id, company_id, policy_id, document_key, version, status, original_filename, storage_reference,
              content_type, size_bytes, checksum_sha256, created_at, updated_at
"""
_UPDATE_DOCUMENT_SQL = """
    UPDATE policy_documents
    SET original_filename = %(original_filename)s
    WHERE company_id = %(company_id)s AND id = %(document_id)s
    RETURNING id, company_id, policy_id, document_key, version, status, original_filename, storage_reference,
              content_type, size_bytes, checksum_sha256, created_at, updated_at
"""
_SET_DOCUMENT_STATUS_SQL = """
    UPDATE policy_documents
    SET status = %(status)s
    WHERE company_id = %(company_id)s AND id = %(document_id)s
    RETURNING id, company_id, policy_id, document_key, version, status, original_filename, storage_reference,
              content_type, size_bytes, checksum_sha256, created_at, updated_at
"""
_DELETE_DOCUMENT_SQL = """
    DELETE FROM policy_documents
    WHERE company_id = %(company_id)s AND id = %(document_id)s
    RETURNING id, company_id, policy_id, document_key, version, status, original_filename, storage_reference,
              content_type, size_bytes, checksum_sha256, created_at, updated_at
"""


# Chunk embeddings (Phase 5). The vector is bound as a float array and cast to vector; it is read back as real[].
_INSERT_CHUNK_EMBEDDING_SQL = """
    INSERT INTO chunk_embeddings (company_id, policy_id, document_id, document_version, chunk_id, chunk_index,
                                  page_start, page_end, section_heading, chunk_text, embedding_model,
                                  embedding_dimension, embedding)
    VALUES (%(company_id)s, %(policy_id)s, %(document_id)s, %(document_version)s, %(chunk_id)s, %(chunk_index)s,
            %(page_start)s, %(page_end)s, %(section_heading)s, %(chunk_text)s, %(embedding_model)s,
            %(embedding_dimension)s, %(embedding)s::vector)
    ON CONFLICT (company_id, chunk_id, embedding_model) DO NOTHING
    RETURNING id, company_id, policy_id, document_id, document_version, chunk_id, chunk_index, page_start, page_end,
              section_heading, chunk_text, embedding_model, embedding_dimension, embedding::real[] AS embedding,
              created_at
"""
_LIST_CHUNK_EMBEDDINGS_SQL = """
    SELECT id, company_id, policy_id, document_id, document_version, chunk_id, chunk_index, page_start, page_end,
           section_heading, chunk_text, embedding_model, embedding_dimension, embedding::real[] AS embedding,
           created_at
    FROM chunk_embeddings
    WHERE company_id = %(company_id)s AND document_id = %(document_id)s AND embedding_model = %(embedding_model)s
    ORDER BY chunk_index
"""
_GET_CHUNK_EMBEDDING_SQL = """
    SELECT id, company_id, policy_id, document_id, document_version, chunk_id, chunk_index, page_start, page_end,
           section_heading, chunk_text, embedding_model, embedding_dimension, embedding::real[] AS embedding,
           created_at
    FROM chunk_embeddings
    WHERE company_id = %(company_id)s AND id = %(embedding_id)s
"""
_DELETE_CHUNK_EMBEDDING_SQL = """
    DELETE FROM chunk_embeddings
    WHERE company_id = %(company_id)s AND id = %(embedding_id)s
    RETURNING id, company_id, policy_id, document_id, document_version, chunk_id, chunk_index, page_start, page_end,
              section_heading, chunk_text, embedding_model, embedding_dimension, embedding::real[] AS embedding,
              created_at
"""
# Chunk search (Phase 6). Exact cosine search (pgvector <=>, no approximate index) and PostgreSQL full-text search
# over the tenant's rows; ties broken by chunk_id so the order is deterministic. The query text and every filter are
# bound parameters; 'english' is the fixed text-search configuration.
_SEARCH_CHUNKS_BY_VECTOR_SQL = """
    SELECT ce.company_id, ce.chunk_id, ce.document_id, ce.policy_id, ce.document_version, ce.chunk_index,
           ce.page_start, ce.page_end, ce.section_heading, ce.chunk_text,
           1 - (ce.embedding <=> %(query_embedding)s::vector) AS score
    FROM chunk_embeddings ce
    JOIN policy_documents d ON d.id = ce.document_id
    JOIN policies p ON p.id = ce.policy_id
    WHERE ce.company_id = %(company_id)s AND d.company_id = %(company_id)s AND p.company_id = %(company_id)s
      AND ce.embedding_model = %(embedding_model)s AND d.status = 'UPLOADED' AND p.status = 'ACTIVE'
      AND ce.policy_id = COALESCE(%(policy_id)s::uuid, ce.policy_id)
      AND ce.document_id = COALESCE(%(document_id)s::uuid, ce.document_id)
      AND p.insurance_type = COALESCE(%(insurance_type)s::text, p.insurance_type)
    ORDER BY ce.embedding <=> %(query_embedding)s::vector, ce.chunk_id
    LIMIT %(limit)s
"""
_SEARCH_CHUNKS_BY_KEYWORDS_SQL = """
    SELECT ce.company_id, ce.chunk_id, ce.document_id, ce.policy_id, ce.document_version, ce.chunk_index,
           ce.page_start, ce.page_end, ce.section_heading, ce.chunk_text,
           ts_rank_cd(to_tsvector('english', ce.chunk_text), websearch_to_tsquery('english', %(query)s)) AS score
    FROM chunk_embeddings ce
    JOIN policy_documents d ON d.id = ce.document_id
    JOIN policies p ON p.id = ce.policy_id
    WHERE ce.company_id = %(company_id)s AND d.company_id = %(company_id)s AND p.company_id = %(company_id)s
      AND ce.embedding_model = %(embedding_model)s AND d.status = 'UPLOADED' AND p.status = 'ACTIVE'
      AND ce.policy_id = COALESCE(%(policy_id)s::uuid, ce.policy_id)
      AND ce.document_id = COALESCE(%(document_id)s::uuid, ce.document_id)
      AND p.insurance_type = COALESCE(%(insurance_type)s::text, p.insurance_type)
      AND to_tsvector('english', ce.chunk_text) @@ websearch_to_tsquery('english', %(query)s)
    ORDER BY score DESC, ce.chunk_id
    LIMIT %(limit)s
"""


class TenantScopeError(Exception):
    """Tenant-scoped access was refused. Always the same generic message, with no detail."""

    def __init__(self) -> None:
        super().__init__(TENANT_SCOPE_FAILURE_MESSAGE)


class TenantUser(BaseModel):
    """A user row read through a tenant scope. No password hash: tenant reads never select it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    company_id: UUID
    email: str
    full_name: str
    role: UserRole
    status: UserStatus


class TenantDatabase:
    """Database access scoped to one tenant. Immutable: the tenant cannot be changed after construction."""

    __slots__ = ("_conn", "_tenant")

    def __init__(self, conn: psycopg.Connection[Any], tenant: TenantIdentity) -> None:
        company_id = getattr(tenant, "company_id", None) if isinstance(tenant, TenantIdentity) else None
        if not isinstance(company_id, UUID) or company_id.int == 0:
            logger.warning("tenant-scoped database access refused: no valid tenant")
            raise TenantScopeError
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_tenant", tenant)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("TenantDatabase is immutable")

    @property
    def tenant(self) -> TenantIdentity:
        return self._tenant

    def list_users(self) -> list[TenantUser]:
        """The tenant's users, ordered by email."""
        return [self._own(row, TenantUser) for row in self._fetch(_LIST_USERS_SQL, {})]

    def get_user(self, user_id: UUID) -> TenantUser | None:
        """The tenant's user with this id, or None, also when the id belongs to another tenant."""
        if not isinstance(user_id, UUID):
            raise TypeError("user_id must be a UUID")
        rows = self._fetch(_GET_USER_SQL, {"user_id": user_id})
        return self._own(rows[0], TenantUser) if rows else None

    def list_policies(self) -> list[Policy]:
        """The tenant's policies, ordered by policy number."""
        return [self._own(row, Policy) for row in self._fetch(_LIST_POLICIES_SQL, {})]

    def get_policy(self, policy_id: UUID) -> Policy | None:
        """The tenant's policy with this id, or None, also when the id belongs to another tenant."""
        if not isinstance(policy_id, UUID):
            raise TypeError("policy_id must be a UUID")
        rows = self._fetch(_GET_POLICY_SQL, {"policy_id": policy_id})
        return self._own(rows[0], Policy) if rows else None

    def create_policy(
        self, *, policy_number: str, name: str, insurance_type: InsuranceType,
        status: PolicyStatus = PolicyStatus.ACTIVE,
    ) -> Policy:
        """Create a policy in the tenant's company and return it. The database generates the id and enforces the
        value rules and the per-company unique policy number (errors propagate)."""
        if not isinstance(policy_number, str) or not isinstance(name, str):
            raise TypeError("policy_number and name must be strings")
        if not isinstance(insurance_type, InsuranceType) or not isinstance(status, PolicyStatus):
            raise TypeError("insurance_type must be an InsuranceType and status a PolicyStatus")
        params = {"policy_number": policy_number, "name": name, "insurance_type": insurance_type.value,
                  "status": status.value}
        return self._own(self._fetch(_CREATE_POLICY_SQL, params)[0], Policy)

    def update_policy(
        self, policy_id: UUID, *, policy_number: str | None = None, name: str | None = None,
        insurance_type: InsuranceType | None = None,
    ) -> Policy | None:
        """Change the given fields of the tenant's policy; the others are left as they are. Returns the updated
        policy, or None when the tenant has no policy with this id (also when another tenant does). The id, company
        and status are not changed here (status: ``set_policy_status``)."""
        if not isinstance(policy_id, UUID):
            raise TypeError("policy_id must be a UUID")
        if policy_number is None and name is None and insurance_type is None:
            raise ValueError("update_policy needs at least one field to change")
        if not all(isinstance(value, str) for value in (policy_number, name) if value is not None):
            raise TypeError("policy_number and name must be strings")
        if insurance_type is not None and not isinstance(insurance_type, InsuranceType):
            raise TypeError("insurance_type must be an InsuranceType")
        params = {"policy_id": policy_id, "policy_number": policy_number, "name": name,
                  "insurance_type": insurance_type.value if insurance_type is not None else None}
        rows = self._fetch(_UPDATE_POLICY_SQL, params)
        return self._own(rows[0], Policy) if rows else None

    def set_policy_status(self, policy_id: UUID, status: PolicyStatus) -> Policy | None:
        """Set the status of the tenant's policy. Returns the updated policy, or None when the tenant has no policy
        with this id (also when another tenant does)."""
        if not isinstance(policy_id, UUID):
            raise TypeError("policy_id must be a UUID")
        if not isinstance(status, PolicyStatus):
            raise TypeError("status must be a PolicyStatus")
        rows = self._fetch(_SET_POLICY_STATUS_SQL, {"policy_id": policy_id, "status": status.value})
        return self._own(rows[0], Policy) if rows else None

    def delete_policy(self, policy_id: UUID) -> bool:
        """Delete the tenant's policy. False when the tenant has no policy with this id (also when another tenant
        does). A policy that still has documents is never deleted: the database refuses (RestrictViolation)."""
        if not isinstance(policy_id, UUID):
            raise TypeError("policy_id must be a UUID")
        return bool([self._own(row, Policy) for row in self._fetch(_DELETE_POLICY_SQL, {"policy_id": policy_id})])

    def list_documents(self, policy_id: UUID) -> list[PolicyDocument]:
        """The documents of the tenant's policy with this id, ordered by document key and version. Empty for another
        tenant's policy."""
        if not isinstance(policy_id, UUID):
            raise TypeError("policy_id must be a UUID")
        return [self._own(row, PolicyDocument) for row in self._fetch(_LIST_DOCUMENTS_SQL, {"policy_id": policy_id})]

    def get_document(self, document_id: UUID) -> PolicyDocument | None:
        """The tenant's policy document with this id, or None, also when the id belongs to another tenant."""
        if not isinstance(document_id, UUID):
            raise TypeError("document_id must be a UUID")
        rows = self._fetch(_GET_DOCUMENT_SQL, {"document_id": document_id})
        return self._own(rows[0], PolicyDocument) if rows else None

    def create_document(
        self, policy_id: UUID, *, document_key: str, version: int, original_filename: str, storage_reference: str,
        content_type: str, size_bytes: int, checksum_sha256: str, status: DocumentStatus = DocumentStatus.UPLOADED,
    ) -> PolicyDocument:
        """Record the metadata of one document version under the tenant's policy and return it. The database
        generates the id and enforces the metadata rules and the unique (policy, document_key, version). A policy
        that is not the tenant's, or does not exist, is refused by the database (``ForeignKeyViolation`` on
        ``policy_documents_policy_fkey``, the same for both). Versions are given, never assigned here."""
        if not isinstance(policy_id, UUID):
            raise TypeError("policy_id must be a UUID")
        if not all(isinstance(value, str)
                   for value in (document_key, original_filename, storage_reference, content_type, checksum_sha256)):
            raise TypeError("document_key, original_filename, storage_reference, content_type and checksum_sha256 "
                            "must be strings")
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (version, size_bytes)):
            raise TypeError("version and size_bytes must be integers")
        if not isinstance(status, DocumentStatus):
            raise TypeError("status must be a DocumentStatus")
        params = {"policy_id": policy_id, "document_key": document_key, "version": version,
                  "original_filename": original_filename, "storage_reference": storage_reference,
                  "content_type": content_type, "size_bytes": size_bytes, "checksum_sha256": checksum_sha256,
                  "status": status.value}
        return self._own(self._fetch(_CREATE_DOCUMENT_SQL, params)[0], PolicyDocument)

    def update_document(self, document_id: UUID, *, original_filename: str) -> PolicyDocument | None:
        """Change the display file name of the tenant's document. Returns the updated document, or None when the
        tenant has no document with this id (also when another tenant does). Nothing else changes in place (D-025):
        a new file, key or version is a new document row; the status changes through ``set_document_status``."""
        if not isinstance(document_id, UUID):
            raise TypeError("document_id must be a UUID")
        if not isinstance(original_filename, str):
            raise TypeError("original_filename must be a string")
        params = {"document_id": document_id, "original_filename": original_filename}
        rows = self._fetch(_UPDATE_DOCUMENT_SQL, params)
        return self._own(rows[0], PolicyDocument) if rows else None

    def set_document_status(self, document_id: UUID, status: DocumentStatus) -> PolicyDocument | None:
        """Set the status (UPLOADED or INACTIVE) of the tenant's document. Returns the updated document, or None when
        the tenant has no document with this id (also when another tenant does). Other versions are not touched."""
        if not isinstance(document_id, UUID):
            raise TypeError("document_id must be a UUID")
        if not isinstance(status, DocumentStatus):
            raise TypeError("status must be a DocumentStatus")
        rows = self._fetch(_SET_DOCUMENT_STATUS_SQL, {"document_id": document_id, "status": status.value})
        return self._own(rows[0], PolicyDocument) if rows else None

    def delete_document(self, document_id: UUID) -> bool:
        """Delete the tenant's document record (this one version only; its policy and other versions stay). False
        when the tenant has no document with this id (also when another tenant does)."""
        if not isinstance(document_id, UUID):
            raise TypeError("document_id must be a UUID")
        rows = self._fetch(_DELETE_DOCUMENT_SQL, {"document_id": document_id})
        return bool([self._own(row, PolicyDocument) for row in rows])

    def insert_chunk_embedding(
        self, *, policy_id: UUID, document_id: UUID, document_version: int, chunk_id: UUID, chunk_index: int,
        page_start: int, page_end: int, section_heading: str | None, chunk_text: str, embedding_model: str,
        embedding: list[float],
    ) -> ChunkEmbedding | None:
        """Store one chunk's embedding in the tenant's company. Returns the new row, or None when this chunk already
        has an embedding by this model (it is left unchanged). A document, policy or version that is not the
        tenant's is refused by the database (``ForeignKeyViolation`` on ``chunk_embeddings_document_fkey``)."""
        if not all(isinstance(value, UUID) for value in (policy_id, document_id, chunk_id)):
            raise TypeError("policy_id, document_id and chunk_id must be UUIDs")
        if not all(isinstance(value, int) and not isinstance(value, bool)
                   for value in (document_version, chunk_index, page_start, page_end)):
            raise TypeError("document_version, chunk_index, page_start and page_end must be integers")
        if not isinstance(chunk_text, str) or not isinstance(embedding_model, str) or not (
                section_heading is None or isinstance(section_heading, str)):
            raise TypeError("chunk_text, embedding_model and section_heading must be strings")
        if not isinstance(embedding, list) or not all(type(value) is float for value in embedding):
            raise TypeError("embedding must be a list of floats")
        params = {"policy_id": policy_id, "document_id": document_id, "document_version": document_version,
                  "chunk_id": chunk_id, "chunk_index": chunk_index, "page_start": page_start, "page_end": page_end,
                  "section_heading": section_heading, "chunk_text": chunk_text, "embedding_model": embedding_model,
                  "embedding_dimension": len(embedding), "embedding": embedding}
        rows = self._fetch(_INSERT_CHUNK_EMBEDDING_SQL, params)
        return self._own(rows[0], ChunkEmbedding) if rows else None

    def list_chunk_embeddings(self, document_id: UUID, embedding_model: str) -> list[ChunkEmbedding]:
        """The tenant's embeddings of one document by one model, in chunk order. Empty for another tenant's
        document."""
        if not isinstance(document_id, UUID) or not isinstance(embedding_model, str):
            raise TypeError("document_id must be a UUID and embedding_model a string")
        rows = self._fetch(_LIST_CHUNK_EMBEDDINGS_SQL, {"document_id": document_id, "embedding_model": embedding_model})
        return [self._own(row, ChunkEmbedding) for row in rows]

    def get_chunk_embedding(self, embedding_id: UUID) -> ChunkEmbedding | None:
        """The tenant's embedding with this id, or None, also when the id belongs to another tenant."""
        if not isinstance(embedding_id, UUID):
            raise TypeError("embedding_id must be a UUID")
        rows = self._fetch(_GET_CHUNK_EMBEDDING_SQL, {"embedding_id": embedding_id})
        return self._own(rows[0], ChunkEmbedding) if rows else None

    def delete_chunk_embedding(self, embedding_id: UUID) -> bool:
        """Delete the tenant's embedding. False when the tenant has none with this id (also when another tenant
        does)."""
        if not isinstance(embedding_id, UUID):
            raise TypeError("embedding_id must be a UUID")
        rows = self._fetch(_DELETE_CHUNK_EMBEDDING_SQL, {"embedding_id": embedding_id})
        return bool([self._own(row, ChunkEmbedding) for row in rows])

    def search_chunks_by_vector(
        self, query_embedding: list[float], *, embedding_model: str, limit: int, policy_id: UUID | None = None,
        document_id: UUID | None = None, insurance_type: InsuranceType | None = None,
    ) -> list[ChunkMatch]:
        """The tenant's chunks closest to ``query_embedding`` (cosine similarity, highest first), among embeddings by
        ``embedding_model``, optionally filtered. ``score`` is the cosine similarity."""
        if not isinstance(query_embedding, list) or not all(type(value) is float for value in query_embedding):
            raise TypeError("query_embedding must be a list of floats")
        params = {"query_embedding": query_embedding,
                  **self._search_filters(embedding_model, limit, policy_id, document_id, insurance_type)}
        return [self._own(row, ChunkMatch) for row in self._fetch(_SEARCH_CHUNKS_BY_VECTOR_SQL, params)]

    def search_chunks_by_keywords(
        self, query: str, *, embedding_model: str, limit: int, policy_id: UUID | None = None,
        document_id: UUID | None = None, insurance_type: InsuranceType | None = None,
    ) -> list[ChunkMatch]:
        """The tenant's chunks matching ``query`` by PostgreSQL full-text search (english configuration,
        ``websearch_to_tsquery``), best ``ts_rank_cd`` first, optionally filtered. ``score`` is that rank."""
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        params = {"query": query,
                  **self._search_filters(embedding_model, limit, policy_id, document_id, insurance_type)}
        return [self._own(row, ChunkMatch) for row in self._fetch(_SEARCH_CHUNKS_BY_KEYWORDS_SQL, params)]

    @staticmethod
    def _search_filters(embedding_model: str, limit: int, policy_id: UUID | None, document_id: UUID | None,
                        insurance_type: InsuranceType | None) -> dict[str, Any]:
        if not isinstance(embedding_model, str):
            raise TypeError("embedding_model must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        if not all(value is None or isinstance(value, UUID) for value in (policy_id, document_id)):
            raise TypeError("policy_id and document_id must be UUIDs or None")
        if insurance_type is not None and not isinstance(insurance_type, InsuranceType):
            raise TypeError("insurance_type must be an InsuranceType or None")
        return {"embedding_model": embedding_model, "limit": limit, "policy_id": policy_id,
                "document_id": document_id,
                "insurance_type": insurance_type.value if insurance_type is not None else None}

    def _fetch(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        # The tenant is always bound last, from the trusted TenantIdentity, so no params value can replace it.
        with self._conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, {**params, "company_id": self._tenant.company_id}).fetchall()

    def _own[M: BaseModel](self, row: dict[str, Any], model: type[M]) -> M:
        if row.get("company_id") != self._tenant.company_id:
            # Only a broken query can get here. Refuse rather than return another tenant's row.
            logger.error("tenant-scoped query returned a row outside the tenant; refused")
            raise TenantScopeError
        return model.model_validate(row)
