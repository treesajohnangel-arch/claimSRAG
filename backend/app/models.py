"""Company (tenant), user, policy and policy-document records, matching the tables of migrations 0001 and 0002.

The database enforces every constraint; these read-only models only carry rows in Python. In ClaimsRAG V1 a
company is the tenant: each user, policy and policy document belongs to exactly one company, through ``company_id``,
and a document always belongs to a policy of the same company.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr


class CompanyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class UserRole(StrEnum):
    COMPANY_ADMIN = "COMPANY_ADMIN"
    STAFF = "STAFF"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class Company(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    slug: str
    status: CompanyStatus
    created_at: datetime
    updated_at: datetime


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    company_id: UUID
    email: str
    # SecretStr: masked in repr/str/JSON, so the hash cannot leak through logs or responses by accident.
    password_hash: SecretStr
    full_name: str
    role: UserRole
    status: UserStatus
    created_at: datetime
    updated_at: datetime


class InsuranceType(StrEnum):
    HEALTH = "HEALTH"
    MOTOR = "MOTOR"
    TRAVEL = "TRAVEL"
    HOME = "HOME"


class PolicyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class DocumentStatus(StrEnum):
    """UPLOADED: recorded, but nothing has processed or validated it. INACTIVE: withdrawn, kept for history.

    Processing and version-lifecycle states (e.g. ACTIVE/SUPERSEDED) belong to later phases.
    """

    UPLOADED = "UPLOADED"
    INACTIVE = "INACTIVE"


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    company_id: UUID
    policy_number: str
    name: str
    insurance_type: InsuranceType
    status: PolicyStatus
    created_at: datetime
    updated_at: datetime


class PolicyDocument(BaseModel):
    """Metadata for one version of one document of a policy. The file itself is not in the database:
    ``storage_reference`` says where it lives."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    company_id: UUID
    policy_id: UUID
    document_key: str
    version: int
    status: DocumentStatus
    original_filename: str
    storage_reference: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    created_at: datetime
    updated_at: datetime


class ChunkEmbedding(BaseModel):
    """One Phase 4 chunk's embedding by one model (migration 0003), with the chunk's full provenance."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    company_id: UUID
    policy_id: UUID
    document_id: UUID
    document_version: int
    chunk_id: UUID
    chunk_index: int
    page_start: int
    page_end: int
    section_heading: str | None
    chunk_text: str
    embedding_model: str
    embedding_dimension: int
    embedding: tuple[float, ...]
    created_at: datetime


class ChunkMatch(BaseModel):
    """A stored chunk found by a tenant-scoped search (Phase 6), with its provenance and the search's score. Read
    through ``TenantDatabase._own``: the company is checked, then dropped; the vector is never selected."""

    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    document_id: UUID
    policy_id: UUID
    document_version: int
    chunk_index: int
    page_start: int
    page_end: int
    section_heading: str | None
    chunk_text: str
    score: float
