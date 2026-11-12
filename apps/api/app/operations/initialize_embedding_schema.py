from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.session import SessionLocal
from app.evaluation.embedding_reindex import parse_vector_dimension


async def initialize_dimension(
    dimension: int, *, require_empty: bool = True, dry_run: bool = False
) -> dict:
    if not 1 <= dimension <= 2000:
        raise ValueError("HNSW vector embedding dimension must be between 1 and 2000")
    if dimension != settings.embedding_dim:
        raise ValueError("Requested dimension must match this API's EMBEDDING_DIM")
    async with SessionLocal() as db:
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        current_dimension = await _current_dimension(db)
        if current_dimension == dimension:
            return _report(current_dimension, dimension, dry_run, changed=False)
        if not dry_run and not settings.embedding_schema_initialization_allowed:
            raise RuntimeError(
                "Schema initialization is disabled; use the isolated BGE Compose profile"
            )
        if not dry_run:
            await db.execute(text("LOCK TABLE document_chunks IN ACCESS EXCLUSIVE MODE"))
            current_dimension = await _current_dimension(db)
            if current_dimension == dimension:
                return _report(current_dimension, dimension, dry_run, changed=False)
        if await db.scalar(text("SELECT EXISTS (SELECT 1 FROM document_chunks)")):
            raise RuntimeError(
                f"refusing to change vector({current_dimension}) to vector({dimension}): "
                "document_chunks is not empty; use a separate database and reindex"
            )
        if dry_run:
            return _report(current_dimension, dimension, dry_run, changed=False)
        await db.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
        await db.execute(
            text(f"ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector({dimension})")
        )
        await db.execute(
            text(
                "CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks "
                "USING hnsw (embedding vector_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)"
            )
        )
        await db.commit()
        return _report(current_dimension, dimension, dry_run, changed=True)


async def _current_dimension(db) -> int:
    column_type = await db.scalar(
        text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = to_regclass('document_chunks') "
            "AND attname = 'embedding' AND NOT attisdropped"
        )
    )
    if column_type is None:
        raise RuntimeError("document_chunks.embedding does not exist; run migrations first")
    return parse_vector_dimension(column_type)


def _report(current: int, requested: int, dry_run: bool, *, changed: bool) -> dict:
    return {
        "previous_dimension": current,
        "requested_dimension": requested,
        "database_dimension": requested if changed else current,
        "change_required": current != requested,
        "dry_run": dry_run,
        "write_performed": changed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize an isolated embedding schema")
    parser.add_argument("--dimension", type=int, required=True)
    parser.add_argument(
        "--require-empty", action="store_true", help="Compatibility flag; always enforced"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Inspect without changing the schema"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(initialize_dimension(args.dimension, dry_run=args.dry_run))
        print(json.dumps(report, sort_keys=True))
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        # Connection exceptions can contain credentials; keep CLI errors generic.
        print(
            "ERROR: database operation failed or lock timed out; transaction rolled back",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
