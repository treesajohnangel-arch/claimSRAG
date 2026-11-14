"""Migration tests: each revision builds its schema from an empty database, and can be reversed and replayed.

The Phase 1.1 tests target revision 0001 explicitly (they were written when 0001 was head); the Phase 2.1 tests
target revision 0002 explicitly (head when they were written); the Phase 5 tests cover 0003, which is head.
"""

import re
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import psycopg
import pytest

from app.config import Settings
from app.models import DocumentStatus, InsuranceType, PolicyStatus
from tests.database.conftest import PASSWORD_HASH, run_alembic

pytestmark = pytest.mark.database

Connect = Callable[[Settings], psycopg.Connection[Any]]

EXPECTED_CONSTRAINTS = {
    ("companies", "companies_pkey", "p"),
    ("companies", "companies_slug_key", "u"),
    ("companies", "companies_name_check", "c"),
    ("companies", "companies_slug_check", "c"),
    ("companies", "companies_status_check", "c"),
    ("users", "users_pkey", "p"),
    ("users", "users_company_id_fkey", "f"),
    ("users", "users_company_id_email_key", "u"),
    ("users", "users_email_normalized_check", "c"),
    ("users", "users_email_format_check", "c"),
    ("users", "users_password_hash_check", "c"),
    ("users", "users_full_name_check", "c"),
    ("users", "users_role_check", "c"),
    ("users", "users_status_check", "c"),
}


def _public_tables(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    return {row["table_name"] for row in rows}


def test_upgrade_from_an_empty_database_builds_exactly_the_phase_1_1_schema(
    empty_database: Settings, connect: Connect
) -> None:
    with connect(empty_database) as conn:
        assert _public_tables(conn) == set()

    run_alembic(empty_database, "upgrade", "0001")

    with connect(empty_database) as conn:
        tables = _public_tables(conn)
        constraints = conn.execute(
            "SELECT conrelid::regclass::text AS table_name, conname, contype FROM pg_constraint "
            "WHERE conrelid IN ('companies'::regclass, 'users'::regclass) AND contype IN ('p', 'u', 'f', 'c')"
        ).fetchall()
        foreign_key = conn.execute(
            "SELECT confdeltype, confupdtype FROM pg_constraint WHERE conname = 'users_company_id_fkey'"
        ).fetchone()
        nullable = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('companies', 'users') AND is_nullable = 'YES'"
        ).fetchall()
        columns = conn.execute(
            "SELECT table_name, column_name, data_type, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('companies', 'users')"
        ).fetchall()
        triggers = conn.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()

    by_column = {(c["table_name"], c["column_name"]): c for c in columns}
    assert tables == {"alembic_version", "companies", "users"}
    assert {(c["table_name"], c["conname"], c["contype"]) for c in constraints} == EXPECTED_CONSTRAINTS
    assert foreign_key == {"confdeltype": "r", "confupdtype": "r"}  # RESTRICT on delete and on update
    assert nullable == []  # every column is NOT NULL
    for table in ("companies", "users"):
        assert by_column[(table, "id")]["data_type"] == "uuid"
        assert by_column[(table, "id")]["column_default"] == "gen_random_uuid()"
        assert by_column[(table, "created_at")]["data_type"] == "timestamp with time zone"
        assert by_column[(table, "updated_at")]["data_type"] == "timestamp with time zone"
    assert {t["tgname"] for t in triggers} == {"companies_set_updated_at", "users_set_updated_at"}
    assert version == {"version_num": "0001"}


def test_migration_downgrades_cleanly_and_can_be_replayed(empty_database: Settings, connect: Connect) -> None:
    run_alembic(empty_database, "upgrade", "0001")
    run_alembic(empty_database, "downgrade", "base")

    with connect(empty_database) as conn:
        after_downgrade = _public_tables(conn)
        functions = conn.execute("SELECT count(*) AS n FROM pg_proc WHERE proname = 'set_updated_at'").fetchone()

    run_alembic(empty_database, "upgrade", "0001")

    with connect(empty_database) as conn:
        after_replay = _public_tables(conn)

    assert after_downgrade == {"alembic_version"}  # Alembic's own bookkeeping table remains, empty
    assert functions == {"n": 0}
    assert after_replay == {"alembic_version", "companies", "users"}


# --- Phase 2.1: migration 0002 (policies, policy_documents) ---------------------------------------------------

PHASE_1_TABLES = {"alembic_version", "companies", "users"}
PHASE_2_1_TABLES = PHASE_1_TABLES | {"policies", "policy_documents"}
EXPECTED_0002_CONSTRAINTS = {
    ("policies", "policies_pkey", "p"),
    ("policies", "policies_company_id_fkey", "f"),
    ("policies", "policies_company_id_policy_number_key", "u"),
    ("policies", "policies_company_id_id_key", "u"),
    ("policies", "policies_policy_number_check", "c"),
    ("policies", "policies_name_check", "c"),
    ("policies", "policies_insurance_type_check", "c"),
    ("policies", "policies_status_check", "c"),
    ("policy_documents", "policy_documents_pkey", "p"),
    ("policy_documents", "policy_documents_company_id_fkey", "f"),
    ("policy_documents", "policy_documents_policy_fkey", "f"),
    ("policy_documents", "policy_documents_company_id_policy_id_document_key_version_key", "u"),
    ("policy_documents", "policy_documents_storage_reference_key", "u"),
    ("policy_documents", "policy_documents_document_key_check", "c"),
    ("policy_documents", "policy_documents_version_check", "c"),
    ("policy_documents", "policy_documents_status_check", "c"),
    ("policy_documents", "policy_documents_original_filename_check", "c"),
    ("policy_documents", "policy_documents_storage_reference_check", "c"),
    ("policy_documents", "policy_documents_content_type_check", "c"),
    ("policy_documents", "policy_documents_size_bytes_check", "c"),
    ("policy_documents", "policy_documents_checksum_sha256_check", "c"),
}
VOCABULARIES: dict[str, type[StrEnum]] = {
    "policies_insurance_type_check": InsuranceType,
    "policies_status_check": PolicyStatus,
    "policy_documents_status_check": DocumentStatus,
}


def _constraints(conn: psycopg.Connection[Any], tables: tuple[str, ...]) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT conrelid::regclass::text AS table_name, conname, contype, pg_get_constraintdef(oid) AS definition "
        "FROM pg_constraint WHERE conrelid::regclass::text = ANY(%s) AND contype IN ('p', 'u', 'f', 'c')",
        (list(tables),),
    ).fetchall()
    return {row["conname"]: row for row in rows}


def test_upgrade_to_head_adds_exactly_the_phase_2_1_schema(empty_database: Settings, connect: Connect) -> None:
    run_alembic(empty_database, "upgrade", "0002")  # pinned: 0002 was head when written

    with connect(empty_database) as conn:
        tables = _public_tables(conn)
        new = _constraints(conn, ("policies", "policy_documents"))
        old = _constraints(conn, ("companies", "users"))
        nullable = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name IN ('policies', 'policy_documents') AND is_nullable = 'YES'"
        ).fetchall()
        columns = conn.execute(
            "SELECT table_name, column_name, data_type, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('policies', 'policy_documents')"
        ).fetchall()
        triggers = conn.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()

    by_column = {(c["table_name"], c["column_name"]): c for c in columns}
    assert tables == PHASE_2_1_TABLES
    assert {(c["table_name"], name, c["contype"]) for name, c in new.items()} == EXPECTED_0002_CONSTRAINTS
    assert {(c["table_name"], name, c["contype"]) for name, c in old.items()} == EXPECTED_CONSTRAINTS  # 0001 intact
    # Same-company rule, in the database: a document's (company, policy) pair must be an existing policy's.
    assert new["policy_documents_policy_fkey"]["definition"] == (
        "FOREIGN KEY (company_id, policy_id) REFERENCES policies(company_id, id) ON UPDATE RESTRICT ON DELETE RESTRICT"
    )
    for name in ("policies_company_id_fkey", "policy_documents_company_id_fkey"):
        assert new[name]["definition"] == (
            "FOREIGN KEY (company_id) REFERENCES companies(id) ON UPDATE RESTRICT ON DELETE RESTRICT"
        )
    assert new["policies_company_id_policy_number_key"]["definition"] == "UNIQUE (company_id, policy_number)"
    assert new["policy_documents_storage_reference_key"]["definition"] == "UNIQUE (storage_reference)"
    assert nullable == []  # every column is NOT NULL
    for table in ("policies", "policy_documents"):
        assert by_column[(table, "id")]["data_type"] == "uuid"
        assert by_column[(table, "id")]["column_default"] == "gen_random_uuid()"
        assert by_column[(table, "created_at")]["data_type"] == "timestamp with time zone"
        assert by_column[(table, "updated_at")]["data_type"] == "timestamp with time zone"
    assert by_column[("policy_documents", "size_bytes")]["data_type"] == "bigint"
    assert by_column[("policy_documents", "version")]["data_type"] == "integer"
    assert not any(c["data_type"] == "bytea" for c in columns)  # no file content in the database
    assert {t["tgname"] for t in triggers} == {"companies_set_updated_at", "users_set_updated_at",
                                               "policies_set_updated_at", "policy_documents_set_updated_at"}
    assert version == {"version_num": "0002"}


def test_vocabularies_in_the_database_match_the_python_enums(migrated_database: Settings, connect: Connect) -> None:
    with connect(migrated_database) as conn:
        constraints = _constraints(conn, ("policies", "policy_documents"))

    for name, vocabulary in VOCABULARIES.items():
        allowed = set(re.findall(r"'([^']*)'", constraints[name]["definition"]))
        assert allowed == {member.value for member in vocabulary}, name


def test_0002_downgrades_to_0001_keeping_phase_1_data_and_can_be_replayed(
    empty_database: Settings, connect: Connect
) -> None:
    run_alembic(empty_database, "upgrade", "0002")  # pinned: 0002 was head when written
    with connect(empty_database) as conn:
        company = conn.execute(
            "INSERT INTO companies (name, slug) VALUES ('Kept Insurance', 'kept') RETURNING id"
        ).fetchone()
        conn.execute(
            "INSERT INTO users (company_id, email, password_hash, full_name, role) VALUES (%s, %s, %s, %s, %s)",
            (company["id"], "kept@kept.example", PASSWORD_HASH, "Kept User", "STAFF"),
        )

    run_alembic(empty_database, "downgrade", "0001")

    with connect(empty_database) as conn:
        after_downgrade = _public_tables(conn)
        kept = conn.execute("SELECT count(*) AS n FROM users WHERE company_id = %s", (company["id"],)).fetchone()
        triggers = conn.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
        functions = conn.execute("SELECT count(*) AS n FROM pg_proc WHERE proname = 'set_updated_at'").fetchone()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()

    run_alembic(empty_database, "downgrade", "base")
    run_alembic(empty_database, "upgrade", "0002")  # pinned: 0002 was head when written

    with connect(empty_database) as conn:
        after_replay = _public_tables(conn)

    assert after_downgrade == PHASE_1_TABLES
    assert kept == {"n": 1}  # Phase 1 data survives removing 0002
    assert {t["tgname"] for t in triggers} == {"companies_set_updated_at", "users_set_updated_at"}
    assert functions == {"n": 1}  # set_updated_at() belongs to 0001 and stays
    assert version == {"version_num": "0001"}
    assert after_replay == PHASE_2_1_TABLES


# --- Phase 5: migration 0003 (chunk_embeddings) ----------------------------------------------------------------

PHASE_5_TABLES = PHASE_2_1_TABLES | {"chunk_embeddings"}
EXPECTED_0003_CONSTRAINTS = {
    ("chunk_embeddings", "chunk_embeddings_pkey", "p"),
    ("chunk_embeddings", "chunk_embeddings_company_id_fkey", "f"),
    ("chunk_embeddings", "chunk_embeddings_document_fkey", "f"),
    ("chunk_embeddings", "chunk_embeddings_company_id_chunk_id_embedding_model_key", "u"),
    ("chunk_embeddings", "chunk_embeddings_document_chunk_index_key", "u"),
    ("chunk_embeddings", "chunk_embeddings_chunk_index_check", "c"),
    ("chunk_embeddings", "chunk_embeddings_pages_check", "c"),
    ("chunk_embeddings", "chunk_embeddings_section_heading_check", "c"),
    ("chunk_embeddings", "chunk_embeddings_chunk_text_check", "c"),
    ("chunk_embeddings", "chunk_embeddings_embedding_model_check", "c"),
    ("chunk_embeddings", "chunk_embeddings_embedding_dimension_check", "c"),
}


def test_0003_upgrades_from_0002_and_adds_exactly_the_embedding_schema(
    empty_database: Settings, connect: Connect
) -> None:
    run_alembic(empty_database, "upgrade", "0002")
    with connect(empty_database) as conn:  # Phase 2 data present before the upgrade
        company = conn.execute("INSERT INTO companies (name, slug) VALUES ('Kept', 'kept') RETURNING id").fetchone()
        conn.execute("INSERT INTO policies (company_id, policy_number, name, insurance_type) "
                     "VALUES (%s, 'POL-1', 'Cover', 'HEALTH')", (company["id"],))

    run_alembic(empty_database, "upgrade", "head")

    with connect(empty_database) as conn:
        tables = _public_tables(conn)
        new = _constraints(conn, ("chunk_embeddings",))
        documents = _constraints(conn, ("policy_documents",))
        vector_type = conn.execute(
            "SELECT format_type(atttypid, atttypmod) AS type FROM pg_attribute "
            "WHERE attrelid = 'chunk_embeddings'::regclass AND attname = 'embedding'").fetchone()
        nullable = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'chunk_embeddings' AND is_nullable = 'YES'").fetchall()
        kept = conn.execute("SELECT count(*) AS n FROM policies").fetchone()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()

    assert tables == PHASE_5_TABLES
    assert {(c["table_name"], name, c["contype"]) for name, c in new.items()} == EXPECTED_0003_CONSTRAINTS
    assert new["chunk_embeddings_document_fkey"]["definition"] == (
        "FOREIGN KEY (company_id, policy_id, document_id, document_version) REFERENCES "
        "policy_documents(company_id, policy_id, id, version) ON UPDATE RESTRICT ON DELETE RESTRICT")
    assert new["chunk_embeddings_company_id_fkey"]["definition"] == (
        "FOREIGN KEY (company_id) REFERENCES companies(id) ON UPDATE RESTRICT ON DELETE RESTRICT")
    assert new["chunk_embeddings_company_id_chunk_id_embedding_model_key"]["definition"] == (
        "UNIQUE (company_id, chunk_id, embedding_model)")
    assert documents["policy_documents_provenance_key"]["definition"] == "UNIQUE (company_id, policy_id, id, version)"
    assert vector_type == {"type": "vector(384)"}
    assert nullable == [{"column_name": "section_heading"}]
    assert kept == {"n": 1} and version == {"version_num": "0003"}


def test_0003_downgrades_to_0002_and_can_be_replayed(empty_database: Settings, connect: Connect) -> None:
    run_alembic(empty_database, "upgrade", "head")
    run_alembic(empty_database, "downgrade", "0002")

    with connect(empty_database) as conn:
        after_downgrade = _public_tables(conn)
        provenance_key = _constraints(conn, ("policy_documents",)).get("policy_documents_provenance_key")
        extension = conn.execute("SELECT count(*) AS n FROM pg_extension WHERE extname = 'vector'").fetchone()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()

    run_alembic(empty_database, "upgrade", "head")

    with connect(empty_database) as conn:
        after_replay = _public_tables(conn)

    assert after_downgrade == PHASE_2_1_TABLES and provenance_key is None
    assert extension == {"n": 1}  # the shared extension is kept
    assert version == {"version_num": "0002"}
    assert after_replay == PHASE_5_TABLES
