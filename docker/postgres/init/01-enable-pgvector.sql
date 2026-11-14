-- Runs once, when the PostgreSQL data volume is first initialised (/docker-entrypoint-initdb.d).
-- Enables pgvector in the application database. It creates no tables: the ClaimsRAG schema belongs to later phases.
CREATE EXTENSION IF NOT EXISTS vector;
