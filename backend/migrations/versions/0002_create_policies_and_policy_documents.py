"""Create policies and policy documents (Phase 2.1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12

Design notes:
- Both tables are tenant-owned: each row carries company_id (NOT NULL, foreign key to companies).
- A policy belongs to one company. Its policy_number is unique within that company only, (company_id,
  policy_number); different companies may use the same number.
- A policy document belongs to one policy of the SAME company. The database enforces this with a composite foreign
  key, (company_id, policy_id) → policies (company_id, id), so a document can never point at another company's
  policy, whatever the application does. (The plain company_id foreign key is kept as well, per the tenant-key rule.)
- Versions: document_key names one logical document of a policy (for example "policy-wording"); each upload of it is
  a new row with the next version. (company_id, policy_id, document_key, version) is unique, so versions are never
  overwritten and a policy can hold several documents, each with many versions. Lifecycle (ACTIVE / SUPERSEDED,
  effective dates, freshness) is Phase 11 and is not modelled here.
- Document status: UPLOADED (recorded; nothing has processed or validated it) or INACTIVE (withdrawn, kept for
  history). Later phases add their own states with their own migrations.
- File content is not stored in PostgreSQL. storage_reference is an opaque reference to where the file lives; it is
  unique across all companies, so two documents (and so two tenants) can never share one stored file.
- checksum_sha256 is integrity metadata, not a uniqueness rule: legitimate identical copies are allowed.
- Deleting a company with policies, or a policy with documents, is refused (ON DELETE RESTRICT); nothing is ever
  removed silently. updated_at is maintained by the set_updated_at() trigger from migration 0001.
- No seed data.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE policies (
            id             uuid        NOT NULL DEFAULT gen_random_uuid(),
            company_id     uuid        NOT NULL,
            policy_number  text        NOT NULL,
            name           text        NOT NULL,
            insurance_type text        NOT NULL,
            status         text        NOT NULL DEFAULT 'ACTIVE',
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT policies_pkey PRIMARY KEY (id),
            CONSTRAINT policies_company_id_fkey FOREIGN KEY (company_id) REFERENCES companies (id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            CONSTRAINT policies_company_id_policy_number_key UNIQUE (company_id, policy_number),
            -- The target of policy_documents' composite foreign key: a policy together with its company.
            CONSTRAINT policies_company_id_id_key UNIQUE (company_id, id),
            CONSTRAINT policies_policy_number_check CHECK (
                policy_number = btrim(policy_number) AND policy_number <> '' AND char_length(policy_number) <= 100
                AND policy_number !~ '[[:cntrl:]]'
            ),
            CONSTRAINT policies_name_check CHECK (btrim(name) <> '' AND char_length(name) <= 300),
            CONSTRAINT policies_insurance_type_check CHECK (insurance_type IN ('HEALTH', 'MOTOR', 'TRAVEL', 'HOME')),
            CONSTRAINT policies_status_check CHECK (status IN ('ACTIVE', 'INACTIVE'))
        )
        """
    )
    op.execute(
        "CREATE TRIGGER policies_set_updated_at BEFORE UPDATE ON policies "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.execute(
        r"""
        CREATE TABLE policy_documents (
            id                uuid        NOT NULL DEFAULT gen_random_uuid(),
            company_id        uuid        NOT NULL,
            policy_id         uuid        NOT NULL,
            document_key      text        NOT NULL,
            version           integer     NOT NULL,
            status            text        NOT NULL DEFAULT 'UPLOADED',
            original_filename text        NOT NULL,
            storage_reference text        NOT NULL,
            content_type      text        NOT NULL,
            size_bytes        bigint      NOT NULL,
            checksum_sha256   text        NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT policy_documents_pkey PRIMARY KEY (id),
            CONSTRAINT policy_documents_company_id_fkey FOREIGN KEY (company_id) REFERENCES companies (id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            -- Same-company rule: the document's policy must belong to the document's company.
            CONSTRAINT policy_documents_policy_fkey FOREIGN KEY (company_id, policy_id)
                REFERENCES policies (company_id, id) ON UPDATE RESTRICT ON DELETE RESTRICT,
            CONSTRAINT policy_documents_company_id_policy_id_document_key_version_key
                UNIQUE (company_id, policy_id, document_key, version),
            CONSTRAINT policy_documents_storage_reference_key UNIQUE (storage_reference),
            CONSTRAINT policy_documents_document_key_check CHECK (
                document_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(document_key) <= 100
            ),
            CONSTRAINT policy_documents_version_check CHECK (version >= 1),
            CONSTRAINT policy_documents_status_check CHECK (status IN ('UPLOADED', 'INACTIVE')),
            CONSTRAINT policy_documents_original_filename_check CHECK (
                btrim(original_filename) <> '' AND char_length(original_filename) <= 255
                AND original_filename !~ '[[:cntrl:]/\\]'
            ),
            CONSTRAINT policy_documents_storage_reference_check CHECK (
                storage_reference = btrim(storage_reference) AND storage_reference <> ''
                AND char_length(storage_reference) <= 1024 AND storage_reference !~ '[[:cntrl:]]'
            ),
            CONSTRAINT policy_documents_content_type_check CHECK (content_type IN ('application/pdf')),
            CONSTRAINT policy_documents_size_bytes_check CHECK (size_bytes > 0),
            CONSTRAINT policy_documents_checksum_sha256_check CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
        )
        """
    )
    # The unique constraints' indexes lead with company_id (and then policy_id), so they also serve the foreign-key
    # checks and the tenant-scoped lookups; no further index is needed yet.
    op.execute(
        "CREATE TRIGGER policy_documents_set_updated_at BEFORE UPDATE ON policy_documents "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE policy_documents")
    op.execute("DROP TABLE policies")
