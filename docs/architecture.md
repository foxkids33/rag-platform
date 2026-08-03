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

## Knowledge-base publication lifecycle

```text
DRAFT
  -> INDEXING
  -> READY
  -> ACTIVE
  -> ARCHIVED
       -> ACTIVE  (explicit rollback)
```

Documents are uploaded and indexed inside an isolated version. `ACTIVE` and
`ARCHIVED` versions are immutable: content changes require a new draft. Publishing
updates `knowledge_bases.active_version_id` in one database transaction, so
workspaces never observe a partially indexed version.

## Two graph features in the product plan

The project intentionally keeps two different graph concepts separate:

1. **Conversation explorer** — a visual canvas of question/answer cards. A user can
   branch from any previous answer, drag cards, follow arrows and inspect a minimap.
   This is a UI and conversation-navigation feature; it does not require Neo4j.
2. **Semantic knowledge graph** — entities and relations extracted from a specific
   immutable knowledge-base version and stored in Neo4j. It becomes an additional
   retrieval signal for multi-hop and relationship questions.

The conversation explorer can be delivered first. Semantic graph extraction is
version-bound and is enabled only after the knowledge-base publication lifecycle is
stable.

## Remaining roadmap

```text
KB draft/index/publish lifecycle
  -> conversation graph explorer
  -> Neo4j entity/relation extraction per KB version
  -> GraphRAG + deep query planning
  -> answer verification and evaluation datasets
  -> Langfuse traces, metrics and feedback
  -> authentication, roles and production hardening
```
