# Architecture baseline

## Workspace as the central scope

Every user operation happens inside a workspace.

A workspace may have:

- `base_knowledge_base_id = null`: user documents only;
- `base_knowledge_base_id = <uuid>`: predefined knowledge base;
- any number of workspace-owned documents layered over the base knowledge base.

The effective retrieval scope is therefore:

```text
active version of selected knowledge base
OR
workspace-owned documents
```

## Planned online RAG path

```text
question
  -> optional conversation-aware rewrite
  -> dense retrieval (pgvector)
  -> lexical retrieval (PostgreSQL FTS)
  -> RRF fusion
  -> deterministic exact/domain-term boosts
  -> cross-encoder reranker
  -> parent/neighbor expansion
  -> context packing
  -> vLLM generation
  -> citations + SSE
```

The default fast path has no mandatory planner LLM call before retrieval.

## Planned ingestion path

```text
upload/import
  -> object storage
  -> ingestion job
  -> parser (Docling where appropriate)
  -> hierarchical parent/child chunking
  -> SHA-256 deduplication
  -> embeddings
  -> PostgreSQL + pgvector
  -> optional graph extraction
```

## Knowledge base versioning

A knowledge base points to one active immutable version. New versions are indexed independently and activated only after successful completion, providing an alias-switching equivalent for PostgreSQL.
