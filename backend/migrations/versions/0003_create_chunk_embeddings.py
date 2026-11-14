"""Create chunk_embeddings (Phase 5).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13

Design notes:
- One row is the embedding of one Phase 4 chunk by one embedding model. It is tenant-owned (company_id NOT NULL,
  foreign key to companies) and immutable: there is no updated_at, and no code updates it.
- Provenance is enforced by the database. The composite foreign key (company_id, policy_id, document_id,
  document_version) → policy_documents (company_id, policy_id, id, version) means the company, policy, document and
  version of an embedding can never disagree with the document record, or cross tenants. policy_documents gets
  the unique constraint the key needs. (id alone is unique; the wider key exists only as the foreign key's target.)
- policy_id and document_version repeat what the document record holds. They are kept because the composite key
  above needs them, and Phase 6 can filter on them without a join.
- chunk_text is the exact Phase 4 chunk text, so retrieval and citations need no re-extraction.
- The vector column has a fixed dimension, vector(384): the model chosen in Phase 5 (BAAI/bge-small-en-v1.5).
  embedding_dimension records it per row and must equal vector_dims(embedding). A model with another dimension
  needs its own migration.
- Idempotency:
  - UNIQUE (company_id, chunk_id, embedding_model): a chunk has at most one embedding per model, and re-embedding
    the same chunk is recognised (chunk_id is content-derived, D-027).
  - UNIQUE (company_id, document_id, embedding_model, chunk_index): two different chunkings of one document cannot
    coexist for the same model.
- pgvector itself rejects NaN and infinite values. No vector index yet: similarity search is Phase 6.
- Deletes are RESTRICT, as everywhere: a document (or company) with embeddings cannot be deleted. Nothing cascades.
- The vector extension is created if missing (the development database already has it); downgrade keeps it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "ALTER TABLE policy_documents ADD CONSTRAINT policy_documents_provenance_key "
        "UNIQUE (company_id, policy_id, id, version)"
    )
    op.execute(
        """
        CREATE TABLE chunk_embeddings (
            id                  uuid        NOT NULL DEFAULT gen_random_uuid(),
            company_id          uuid        NOT NULL,
            policy_id           uuid        NOT NULL,
            document_id         uuid        NOT NULL,
            document_version    integer     NOT NULL,
            chunk_id            uuid        NOT NULL,
            chunk_index         integer     NOT NULL,
            page_start          integer     NOT NULL,
            page_end            integer     NOT NULL,
            section_heading     text,
            chunk_text          text        NOT NULL,
            embedding_model     text        NOT NULL,
            embedding_dimension integer     NOT NULL,
            embedding           vector(384) NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT chunk_embeddings_pkey PRIMARY KEY (id),
            CONSTRAINT chunk_embeddings_company_id_fkey FOREIGN KEY (company_id) REFERENCES companies (id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            -- Provenance: the embedding's company, policy, document and version are those of one document record.
            CONSTRAINT chunk_embeddings_document_fkey FOREIGN KEY (company_id, policy_id, document_id, document_version)
                REFERENCES policy_documents (company_id, policy_id, id, version) ON UPDATE RESTRICT ON DELETE RESTRICT,
            CONSTRAINT chunk_embeddings_company_id_chunk_id_embedding_model_key
                UNIQUE (company_id, chunk_id, embedding_model),
            CONSTRAINT chunk_embeddings_document_chunk_index_key
                UNIQUE (company_id, document_id, embedding_model, chunk_index),
            CONSTRAINT chunk_embeddings_chunk_index_check CHECK (chunk_index >= 0),
            CONSTRAINT chunk_embeddings_pages_check CHECK (page_start >= 1 AND page_end >= page_start),
            CONSTRAINT chunk_embeddings_section_heading_check CHECK (
                section_heading IS NULL OR (section_heading = btrim(section_heading) AND section_heading <> '')
            ),
            CONSTRAINT chunk_embeddings_chunk_text_check CHECK (
                chunk_text = btrim(chunk_text) AND chunk_text <> '' AND char_length(chunk_text) <= 8000
            ),
            CONSTRAINT chunk_embeddings_embedding_model_check CHECK (
                embedding_model = btrim(embedding_model) AND embedding_model <> '' AND char_length(embedding_model) <= 200
            ),
            CONSTRAINT chunk_embeddings_embedding_dimension_check CHECK (embedding_dimension = vector_dims(embedding))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE chunk_embeddings")
    op.execute("ALTER TABLE policy_documents DROP CONSTRAINT policy_documents_provenance_key")
