"""Safely rebuild one workspace when the service and database dimensions match."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

import asyncpg
import httpx

_VECTOR_TYPE_PATTERN = re.compile(r"^vector\((?P<dimension>[1-9][0-9]*)\)$")


def _asyncpg_url(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.9g}" for value in values) + "]"


def parse_vector_dimension(data_type: str) -> int:
    match = _VECTOR_TYPE_PATTERN.fullmatch(data_type.strip())
    if match is None:
        raise RuntimeError(
            "document_chunks.embedding must have a fixed pgvector dimension; "
            f"database reported {data_type!r}"
        )
    return int(match.group("dimension"))


def validate_embedding_space(
    health: dict[str, Any],
    database_dimension: int,
    expected_model: str | None = None,
    expected_dimension: int | None = None,
) -> tuple[str, int]:
    model = health.get("model")
    output_dimension = health.get("output_dimension")
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError("Embedding service readiness response has no model name")
    if isinstance(output_dimension, bool) or not isinstance(output_dimension, int):
        raise TypeError("Embedding service readiness response has no integer output_dimension")
    if expected_model is not None and model != expected_model:
        raise RuntimeError(
            f"Embedding model mismatch: expected {expected_model!r}, service reported {model!r}"
        )
    if expected_dimension is not None and output_dimension != expected_dimension:
        raise RuntimeError(
            "Embedding service dimension mismatch: "
            f"expected {expected_dimension}, service reported {output_dimension}"
        )
    if output_dimension != database_dimension:
        raise RuntimeError(
            "Refusing to mix embedding spaces: service outputs "
            f"{output_dimension} dimensions but document_chunks.embedding is "
            f"vector({database_dimension}). Use an isolated database/index and perform a full reindex."
        )
    return model, output_dimension


async def get_embedding_column_dimension(connection: asyncpg.Connection) -> int:
    data_type = await connection.fetchval(
        """
        SELECT format_type(attribute.atttypid, attribute.atttypmod)
        FROM pg_attribute AS attribute
        JOIN pg_class AS relation ON relation.oid = attribute.attrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = current_schema()
          AND relation.relname = 'document_chunks'
          AND attribute.attname = 'embedding'
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        """
    )
    if not isinstance(data_type, str):
        raise TypeError("document_chunks.embedding was not found in the current database schema")
    return parse_vector_dimension(data_type)


async def rebuild_embeddings(
    database_url: str,
    embedding_base_url: str,
    workspace_id: UUID,
    batch_size: int,
    *,
    expected_model: str | None = None,
    expected_dimension: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    started = perf_counter()
    connection = await asyncpg.connect(_asyncpg_url(database_url))
    try:
        database_dimension = await get_embedding_column_dimension(connection)
        rows = await connection.fetch(
            """
            SELECT
                c.id,
                concat_ws(
                    E'\n\n',
                    nullif(c.heading_breadcrumb, ''),
                    nullif(c.heading, ''),
                    c.text
                ) AS search_text
            FROM document_chunks AS c
            JOIN documents AS d ON d.id = c.document_id
            WHERE d.workspace_id = $1
              AND d.status = 'READY'
              AND d.search_enabled IS TRUE
            ORDER BY c.id
            """,
            workspace_id,
        )
        if not rows:
            raise RuntimeError("Workspace has no searchable chunks")

        updates: list[tuple[str, UUID]] = []
        async with httpx.AsyncClient(
            base_url=embedding_base_url.rstrip("/"), timeout=180.0
        ) as client:
            health_response = await client.get("/health/ready")
            health_response.raise_for_status()
            health = health_response.json()
            if not isinstance(health, dict):
                raise TypeError("Embedding service returned an invalid readiness response")
            model, output_dimension = validate_embedding_space(
                health,
                database_dimension,
                expected_model=expected_model,
                expected_dimension=expected_dimension,
            )

            for offset in range(0, len(rows), batch_size):
                batch = rows[offset : offset + batch_size]
                response = await client.post(
                    "/embed",
                    json={
                        "inputs": [str(row["search_text"]) for row in batch],
                        "normalize": True,
                        "truncate": True,
                        "input_type": "passage",
                    },
                )
                response.raise_for_status()
                vectors = response.json()
                if not isinstance(vectors, list) or len(vectors) != len(batch):
                    raise RuntimeError("Embedding service returned an invalid batch")
                if any(
                    not isinstance(vector, list) or len(vector) != output_dimension
                    for vector in vectors
                ):
                    raise RuntimeError(
                        "Embedding service returned a vector with an unexpected dimension"
                    )
                updates.extend(
                    (_vector_literal(vector), row["id"])
                    for row, vector in zip(batch, vectors, strict=True)
                )

        if not dry_run:
            async with connection.transaction():
                await connection.executemany(
                    "UPDATE document_chunks SET embedding = $1::vector WHERE id = $2",
                    updates,
                )

        return {
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "workspace_id": str(workspace_id),
            "chunk_count": len(updates),
            "embedding_model": model,
            "embedding_dimension": output_dimension,
            "database_dimension": database_dimension,
            "embedding_service": health,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "dry_run": dry_run,
            "write_performed": not dry_run,
            "atomic_update": not dry_run,
            "search_text_policy": "heading_breadcrumb + heading + chunk_text",
        }
    finally:
        await connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--embedding-base-url", required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--expected-model")
    parser.add_argument("--expected-dimension", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 1 <= args.batch_size <= 64:
        raise SystemExit("--batch-size must be between 1 and 64")
    try:
        report = asyncio.run(
            rebuild_embeddings(
                args.database_url,
                args.embedding_base_url,
                args.workspace_id,
                args.batch_size,
                expected_model=args.expected_model,
                expected_dimension=args.expected_dimension,
                dry_run=args.dry_run,
            )
        )
    except (asyncpg.PostgresError, httpx.HTTPError, RuntimeError, TypeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
