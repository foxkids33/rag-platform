# Architecture baseline

## Workspace as the central scope

Every user operation happens inside a workspace.

A workspace stores an explicit source mode:

- `USER_DOCUMENTS`: workspace-owned documents only;
- `KNOWLEDGE_BASE`: the active immutable version of the selected knowledge base only;
- `HYBRID`: both layers enter the same dense/lexical/RRF/reranker pipeline.

`base_knowledge_base_id` selects the predefined knowledge base. Only its
`active_version_id` is eligible for retrieval; older and staging versions remain
isolated. Every returned source carries provenance identifying either the workspace
document or the knowledge-base name and version.

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
