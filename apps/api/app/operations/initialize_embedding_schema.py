from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from app.db.session import SessionLocal


async def initialize_dimension(dimension: int, *, require_empty: bool) -> None:
    if not 1 <= dimension <= 4096:
        raise ValueError("embedding dimension must be between 1 and 4096")
    async with SessionLocal() as db:
        chunks = int(await db.scalar(text("SELECT count(*) FROM document_chunks")) or 0)
        current = await db.scalar(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'document_chunks'::regclass "
                "AND attname = 'embedding' AND NOT attisdropped"
            )
        )
        if current is None:
            raise RuntimeError("document_chunks.embedding does not exist")
        current_dimension = int(current)
        if current_dimension == dimension:
            print(f"embedding schema already uses vector({dimension}); chunks={chunks}")
            return
        if require_empty and chunks:
            raise RuntimeError(
                f"refusing to change vector({current_dimension}) to vector({dimension}): "
                f"document_chunks contains {chunks} rows"
            )
        await db.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
        await db.execute(
            text(
                f"ALTER TABLE document_chunks ALTER COLUMN embedding "
                f"TYPE vector({dimension})"
            )
        )
        await db.execute(
            text(
                "CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks "
                "USING hnsw (embedding vector_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)"
            )
        )
        await db.commit()
        print(f"initialized empty embedding schema: vector({dimension})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize an isolated embedding schema")
    parser.add_argument("--dimension", type=int, required=True)
    parser.add_argument("--require-empty", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(initialize_dimension(args.dimension, require_empty=args.require_empty))
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
