"""Create companies (tenants) and users.

Revision ID: 0001
Revises:
Create Date: 2026-11-11

Design notes:
- A company is the tenant. Every user belongs to exactly one company (users.company_id, NOT NULL, foreign key).
- Allowed values (status, role) are CHECK constraints on text columns. They are simpler to change than enum types.
- Emails are stored normalised (trimmed, lower-case), and the database rejects anything else. Uniqueness is per
  company, (company_id, email), so it is case-insensitive. Different companies may have the same email.
- Deleting a company that still has users is refused (ON DELETE RESTRICT). Users are never orphaned or silently
  deleted.
- IDs come from gen_random_uuid(), built into PostgreSQL. updated_at is set by a trigger on every UPDATE.
- No seed data, and no credentials.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # clock_timestamp(), not now(): an update made later in the same transaction as the insert still advances it.
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END;
        $$
        """
    )

    op.execute(
        r"""
        CREATE TABLE companies (
            id          uuid        NOT NULL DEFAULT gen_random_uuid(),
            name        text        NOT NULL,
            slug        text        NOT NULL,
            status      text        NOT NULL DEFAULT 'ACTIVE',
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT companies_pkey PRIMARY KEY (id),
            CONSTRAINT companies_slug_key UNIQUE (slug),
            CONSTRAINT companies_name_check CHECK (btrim(name) <> '' AND char_length(name) <= 200),
            CONSTRAINT companies_slug_check CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(slug) <= 63),
            CONSTRAINT companies_status_check CHECK (status IN ('ACTIVE', 'INACTIVE'))
        )
        """
    )
    op.execute(
        "CREATE TRIGGER companies_set_updated_at BEFORE UPDATE ON companies "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.execute(
        r"""
        CREATE TABLE users (
            id            uuid        NOT NULL DEFAULT gen_random_uuid(),
            company_id    uuid        NOT NULL,
            email         text        NOT NULL,
            password_hash text        NOT NULL,
            full_name     text        NOT NULL,
            role          text        NOT NULL,
            status        text        NOT NULL DEFAULT 'ACTIVE',
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT users_pkey PRIMARY KEY (id),
            CONSTRAINT users_company_id_fkey FOREIGN KEY (company_id) REFERENCES companies (id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            CONSTRAINT users_company_id_email_key UNIQUE (company_id, email),
            CONSTRAINT users_email_normalized_check CHECK (email = lower(btrim(email))),
            CONSTRAINT users_email_format_check CHECK (email ~ '^[^@\s]+@[^@\s]+$' AND char_length(email) <= 254),
            CONSTRAINT users_password_hash_check CHECK (password_hash <> ''),
            CONSTRAINT users_full_name_check CHECK (btrim(full_name) <> '' AND char_length(full_name) <= 200),
            CONSTRAINT users_role_check CHECK (role IN ('COMPANY_ADMIN', 'STAFF')),
            CONSTRAINT users_status_check CHECK (status IN ('ACTIVE', 'INACTIVE'))
        )
        """
    )
    # The unique constraint's index leads with company_id, so it also serves foreign-key checks and per-company lookups.
    op.execute(
        "CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE users")
    op.execute("DROP TABLE companies")
    op.execute("DROP FUNCTION set_updated_at()")
