import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.operations import initialize_embedding_schema as schema


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", [0, -1, 2001, 4096])
async def test_invalid_hnsw_dimension_is_rejected_before_connecting(dimension):
    with pytest.raises(ValueError, match="between 1 and 2000"):
        await schema.initialize_dimension(dimension)


@pytest.mark.asyncio
async def test_api_dimension_mismatch_is_rejected_before_connecting(monkeypatch):
    monkeypatch.setattr(settings, "embedding_dim", 384)
    with pytest.raises(ValueError, match="EMBEDDING_DIM"):
        await schema.initialize_dimension(1024)


@pytest_asyncio.fixture
async def isolated_schema(monkeypatch):
    if os.getenv("RUN_DATABASE_TESTS") != "1":
        pytest.skip("requires a disposable PostgreSQL with pgvector installed")
    name = f"test_embedding_{uuid.uuid4().hex}"
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
        await connection.execute(text(f'SET search_path TO "{name}", public'))
        await connection.execute(
            text("CREATE TABLE document_chunks (id integer PRIMARY KEY, embedding vector(384))")
        )
        await connection.execute(
            text(
                "CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks "
                "USING hnsw (embedding vector_cosine_ops)"
            )
        )
        await connection.commit()
        monkeypatch.setattr(schema, "SessionLocal", async_sessionmaker(connection))
        monkeypatch.setattr(settings, "embedding_dim", 1024)
        monkeypatch.setattr(settings, "embedding_schema_initialization_allowed", True)
        try:
            yield connection
        finally:
            await connection.rollback()
            await connection.execute(text("SET search_path TO public"))
            await connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
            await connection.commit()
    await engine.dispose()


async def column_dimension(connection):
    result = await connection.scalar(
        text(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid='document_chunks'::regclass "
            "AND attname='embedding'"
        )
    )
    await connection.commit()
    return result


@pytest.mark.asyncio
async def test_empty_schema_changes_atomically_and_restart_is_a_noop(isolated_schema):
    report = await schema.initialize_dimension(1024)
    assert report["write_performed"] is True
    assert await column_dimension(isolated_schema) == 1024
    # A subsequent startup must work after ingestion, including NULL embeddings.
    await isolated_schema.execute(text("INSERT INTO document_chunks VALUES (1, NULL)"))
    await isolated_schema.commit()
    report = await schema.initialize_dimension(1024)
    assert report["write_performed"] is False
    assert report["change_required"] is False


@pytest.mark.asyncio
async def test_nonempty_schema_cannot_bypass_guard(isolated_schema):
    await isolated_schema.execute(text("INSERT INTO document_chunks VALUES (1, NULL)"))
    await isolated_schema.commit()
    with pytest.raises(RuntimeError, match="not empty"):
        await schema.initialize_dimension(1024, require_empty=False)
    assert await column_dimension(isolated_schema) == 384
    assert await isolated_schema.scalar(text("SELECT count(*) FROM document_chunks")) == 1


@pytest.mark.asyncio
async def test_dry_run_leaves_type_and_index_unchanged(isolated_schema):
    report = await schema.initialize_dimension(1024, dry_run=True)
    assert report["dry_run"] and report["change_required"]
    assert not report["write_performed"]
    assert await column_dimension(isolated_schema) == 384
    assert await isolated_schema.scalar(text("SELECT to_regclass('ix_chunks_embedding_hnsw')"))


@pytest.mark.asyncio
async def test_regular_profile_cannot_change_schema(isolated_schema, monkeypatch):
    monkeypatch.setattr(settings, "embedding_schema_initialization_allowed", False)
    with pytest.raises(RuntimeError, match="disabled"):
        await schema.initialize_dimension(1024)
    assert await column_dimension(isolated_schema) == 384


@pytest.mark.asyncio
async def test_index_creation_failure_rolls_back_column_and_original_index(
    isolated_schema, monkeypatch
):
    factory = schema.SessionLocal

    def broken_session():
        session = factory()
        execute = session.execute

        async def fail_index(statement, *args, **kwargs):
            if str(statement).startswith("CREATE INDEX"):
                raise RuntimeError("simulated index creation failure")
            return await execute(statement, *args, **kwargs)

        session.execute = fail_index
        return session

    monkeypatch.setattr(schema, "SessionLocal", broken_session)
    with pytest.raises(RuntimeError, match="simulated"):
        await schema.initialize_dimension(1024)
    assert await column_dimension(isolated_schema) == 384
    assert await isolated_schema.scalar(text("SELECT to_regclass('ix_chunks_embedding_hnsw')"))
