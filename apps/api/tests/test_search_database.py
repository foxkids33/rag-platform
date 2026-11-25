import os
import uuid

import pytest
from sqlalchemy import delete, text

from app.api.routes.search import SearchRequest, search
from app.core.security import Principal
from app.db.models import Document, DocumentChunk, Workspace
from app.db.session import SessionLocal, engine
from app.services.query_signals import normalize_exact_text

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DATABASE_TESTS") != "1",
    reason="set RUN_DATABASE_TESTS=1 against a migrated disposable database",
)


@pytest.fixture(autouse=True)
async def dispose_database_pool_after_test():
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_exact_identifier_channel_executes_against_postgres(monkeypatch) -> None:
    workspace_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    longer_identifier_chunk_id = uuid.uuid4()
    principal = Principal(
        subject="search-db-test",
        tenant_id="search-db-test",
        roles=frozenset(),
        claims={},
    )

    async with SessionLocal() as db:
        workspace = Workspace(
            id=workspace_id,
            tenant_id=principal.tenant_id,
            name="Exact search integration test",
            user_id=principal.subject,
            source_mode="USER_DOCUMENTS",
        )
        document = Document(
            id=document_id,
            workspace_id=workspace_id,
            knowledge_base_version_id=None,
            filename="identifier.txt",
            object_key=f"tests/{document_id}/identifier.txt",
            mime_type="text/plain",
            sha256="f" * 64,
            status="READY",
            search_enabled=True,
            source_type="UPLOAD",
        )
        chunk = DocumentChunk(
            id=chunk_id,
            document_id=document_id,
            parent_chunk_id=None,
            chunk_index=0,
            text="Код blue_kb.942 обозначает точную запись в каталоге.",
            normalized_text=normalize_exact_text(
                "Код blue_kb.942 обозначает точную запись в каталоге."
            ),
            parent_text=None,
            heading=None,
            heading_breadcrumb=None,
            page_start=None,
            page_end=None,
            embedding=None,
            search_vector=None,
            metadata_={},
        )
        longer_identifier_chunk = DocumentChunk(
            id=longer_identifier_chunk_id,
            document_id=document_id,
            parent_chunk_id=None,
            chunk_index=1,
            text="Код BLUE-KB-9421 относится к другой записи.",
            normalized_text=normalize_exact_text(
                "Код BLUE-KB-9421 относится к другой записи."
            ),
            parent_text=None,
            heading=None,
            heading_breadcrumb=None,
            page_start=None,
            page_end=None,
            embedding=None,
            search_vector=None,
            metadata_={},
        )
        db.add(workspace)
        await db.flush()
        db.add(document)
        await db.flush()
        db.add_all([chunk, longer_identifier_chunk])
        await db.commit()

        try:
            response = await search(
                workspace_id,
                SearchRequest(
                    query="Что означает BLUE-KB-942?",
                    limit=5,
                    mode="lexical",
                    rerank=False,
                ),
                db,
                principal,
            )
            assert response.exact_identifiers == ["bluekb942"]
            assert response.results[0].chunk_id == chunk_id
            assert response.results[0].exact_score == 1.0
            assert response.results[0].exact_rank == 1
            assert all(
                item.chunk_id != longer_identifier_chunk_id for item in response.results
            )
            from app.api.routes import search as search_route

            async def embed_query(_query):
                return [1.0] + [0.0] * 383

            monkeypatch.setattr(search_route.embeddings, "embed_query", embed_query)
            dense = await search(
                workspace_id,
                SearchRequest(query="Что означает BLUE-KB-942?", mode="dense", rerank=False),
                db, principal,
            )
            # These chunks have exact text but no vectors: they must not leak
            # into a pure dense model comparison.
            assert dense.exact_identifiers == []
            assert dense.results == []
        finally:
            await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await db.commit()


@pytest.mark.asyncio
async def test_soft_lexical_query_breaks_common_exact_identifier_ties() -> None:
    workspace_id = uuid.uuid4()
    document_id = uuid.uuid4()
    relevant_chunk_id = uuid.uuid4()
    noise_chunk_id = uuid.uuid4()
    principal = Principal(
        subject="search-db-test",
        tenant_id="search-db-test",
        roles=frozenset(),
        claims={},
    )

    async with SessionLocal() as db:
        workspace = Workspace(
            id=workspace_id,
            tenant_id=principal.tenant_id,
            name="Lexical tie-break integration test",
            user_id=principal.subject,
            source_mode="USER_DOCUMENTS",
        )
        document = Document(
            id=document_id,
            workspace_id=workspace_id,
            knowledge_base_version_id=None,
            filename="aliases.txt",
            object_key=f"tests/{document_id}/aliases.txt",
            mime_type="text/plain",
            sha256="e" * 64,
            status="READY",
            search_enabled=True,
            source_type="UPLOAD",
        )
        relevant_text = "VOL_ID Normal Cold Ice Glacier"
        noise_text = "VOL_ID unrelated"
        chunks = [
            DocumentChunk(
                id=relevant_chunk_id,
                document_id=document_id,
                parent_chunk_id=None,
                chunk_index=0,
                text=relevant_text,
                normalized_text=normalize_exact_text(relevant_text),
                parent_text=None,
                heading=None,
                heading_breadcrumb=None,
                page_start=None,
                page_end=None,
                embedding=None,
                search_vector=None,
                metadata_={},
            ),
            DocumentChunk(
                id=noise_chunk_id,
                document_id=document_id,
                parent_chunk_id=None,
                chunk_index=1,
                text=noise_text,
                normalized_text=normalize_exact_text(noise_text),
                parent_text=None,
                heading=None,
                heading_breadcrumb=None,
                page_start=None,
                page_end=None,
                embedding=None,
                search_vector=None,
                metadata_={},
            ),
        ]
        db.add(workspace)
        await db.flush()
        db.add(document)
        await db.flush()
        db.add_all(chunks)
        await db.flush()
        await db.execute(
            text(
                """
                UPDATE document_chunks
                SET search_vector = to_tsvector('simple', text)
                WHERE document_id = :document_id
                """
            ),
            {"document_id": document_id},
        )
        await db.commit()

        try:
            response = await search(
                workspace_id,
                SearchRequest(
                    query=(
                        "Какие алиасы Normal, Cold, Ice и Glacier "
                        "сопоставлены с VOL_ID?"
                    ),
                    limit=5,
                    mode="lexical",
                    rerank=False,
                ),
                db,
                principal,
            )
            assert response.exact_identifiers == ["volid"]
            assert response.results[0].chunk_id == relevant_chunk_id
            assert response.results[0].lexical_rank == 1
            assert response.results[0].exact_rank == 1
            assert response.results[1].chunk_id == noise_chunk_id

            lexical_only = await search(
                workspace_id,
                SearchRequest(
                    query="Normal Cold Ice Glacier",
                    limit=5,
                    mode="lexical",
                    rerank=False,
                ),
                db,
                principal,
            )
            assert lexical_only.exact_identifiers == []
            assert lexical_only.results[0].chunk_id == relevant_chunk_id
            assert lexical_only.results[0].lexical_rank == 1
            assert lexical_only.results[0].rrf_score == pytest.approx(1.0 / 61.0)
        finally:
            await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await db.commit()
