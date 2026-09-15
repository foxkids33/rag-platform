# KBR: snapshot and near-real-time RAG

## Scope agreed with the owner

Include every accessible current page in space **KBR**, including `copy:` titles,
the engineering laboratory, product development and pages bearing `archived`
labels. These labels do not exclude a page. This is a corpus-selection policy,
not a claim that commands have been independently reviewed or are safe on every
product version. Actual deleted/unreadable pages are a different case.

The initial snapshot on 2026-09-09 contains **298 pages**, 6,503,926 bytes of JSON
and metadata for **653 attachments**. Attachment binaries, OCR, comments,
embedded diagrams and external linked pages are not part of the indexed corpus.
Snapshot manifests provide the authoritative counts for subsequent runs.

## Architecture implemented in this iteration

```text
Confluence Data Center REST API
  -> one-shot / polling adapter (scripts/confluence/sync.mjs)
  -> immutable local raw snapshots + checksums + per-run manifests
  -> authenticated workspace import API
  -> MinIO page envelope + PostgreSQL ingestion_jobs (durable)
  -> existing worker: Confluence parser -> atomic code-aware chunks -> embeddings
  -> PostgreSQL/pgvector + lexical/exact search
  -> governed workspace retrieval -> context -> vLLM -> source URL/page/version
```

Redis is a wakeup mechanism, not the authoritative job store. Existing worker
leases, recovery and retry handling remain in use. No Kafka, Confluent, Flink,
additional vector database, source-database CDC or model fine-tuning is added.
For this corpus those systems are not prerequisites for change propagation.

Polling is **near-real-time**, not instant: freshness includes the polling
interval, source/API time, queue wait and embedding time. The initial run is a
static snapshot. Continuous polling is opt-in. No Confluence webhook has been
registered. The adapter never executes commands found in articles.

## Runbook

Run from the repository root with Node >=22. No npm installation is needed for
the operational scripts. Node is already used by the web application.
Credentials are read as data from `.env` (never sourced as shell commands).
For this Data Center instance the existing REST-token plugin uses Basic auth.
Do not put credentials in command-line flags, source files or bug reports.

Snapshot only (no workspace/API changes):

```bash
node scripts/confluence/sync.mjs --snapshot-only --space KBR
```

`--output` defaults to `.storage/confluence/KBR`, excluded from Git. New
directories use mode 0700 and files 0600. The adapter does not copy secrets into
manifests. Page contents can themselves contain secrets/PII; protect the entire
snapshot directory and back it up only within the approved security boundary.
Restrictive creation modes do not repair permissions on pre-existing folders.

Before importing, test and deploy the API **and** worker changes together.
The API exposes `parser_format=confluence_storage_v1`; this guard is not a probe
of the worker's deployed version. Do not import new envelopes into an old worker.
Confirm the actual embedding model and dimensions before recreating containers.
The pilot uses multilingual MiniLM (384); the local `.env` has been reconciled
with that model. Previously it incorrectly named BGE-M3 with dimension 384.
Changing to BGE-M3 requires a separate dimension/index migration and evaluation.

Create a separate workspace and import without touching the previous TXT pilot:

```bash
node scripts/confluence/sync.mjs --space KBR --create-workspace 'KBR pilot'
```

The workspace ID is retained in `state.json`. OIDC deployments require a
tenant-admin `RAG_API_TOKEN` in the process environment; the adapter uses the
normal API authorization checks. It never manufactures an authenticated identity.
For an existing workspace supply `--workspace-id <uuid>` and a separate output
directory if its identity differs from the saved state.

Refresh on demand:

```bash
node scripts/confluence/sync.mjs --space KBR
```

The UI also provides **Загрузить / обновить**, with `KBR` preselected. This
bounded space-import path updates pages but does **not** reconcile removals or
produce filesystem snapshots. The adapter is the full synchronization path.
Space import refuses a reported overflow instead of silently accepting a partial
corpus. The adapter uses batches of 10 and is not limited to a single import call.

Optional continuous refresh (do not start for a frozen benchmark):

```bash
node scripts/confluence/sync.mjs --space KBR --watch-seconds 60 --wait-seconds 0
```

The adapter compares page identity/version/title/ancestors/labels. Existing raw
snapshots are reused only after checksum verification. Code/body changes go
through normal durable ingestion. A version mismatch between snapshot and import
is reported as a failure, not hidden. A run is complete only if the listed pages
were obtained; `index_complete` separately requires READY/search-enabled pages
at the expected versions, with no missing or duplicate source identities.
With `--wait-seconds 0`, queued ingestion is expected and is observed next cycle.

Only connector-tracked documents are candidates for disabling. Absence from a
complete non-empty inventory must be confirmed by a page-level 403/404, move out
of the space or non-current status. Timeouts, 401, 429 and 5xx do not mean deletion.
Documents are disabled, not physically deleted. Automatic restoration only
reenables pages disabled by this adapter, not manually excluded documents.

`sync.lock` prevents concurrent adapters using the same output. After SIGKILL or
power loss, inspect its PID and ensure no process is active before removing that
specific lock. Do not delete snapshots or state as a generic recovery action.
Use separate output directories for different source/workspace identities.

## Parsing and fidelity

The `.confluence.json` envelope stores source URL, page ID/version, title,
ancestors, labels and original storage XML. The worker parses CDATA as code,
retains warnings and handy-tip/expand bodies, excludes styling parameters, and
marks unsupported macros/images explicitly. No lossy fallback is used on invalid
storage XML. Parent groups preserve section order; code blocks are atomic even
when larger than the configured target chunk size. This does not guarantee that
an embedding model can semantically encode an arbitrarily long block without
truncation. Oversized blocks are flagged in chunk metadata.

Context cleaning preserves fenced whitespace and URLs. If the context budget
cannot hold a code block, it is omitted rather than sliced, and the instruction
is marked partial. A partial section is not a verified complete runbook. Source
URL/page/version are returned with citations and displayed in the UI.
The LLM is instructed to quote requested commands completely and exactly, using a
separate fence for each command. This is **not a deterministic command-substitution
or execution-safety layer**, especially during streaming. Do not automatically
execute generated commands.

## Evaluation

The committed `evaluation/datasets/kbr.v1.questions.json` has **36 self-authored
development cases**, including **9 exact-code cases** and 4 live-state refusals.
It is based on the inspected articles, not an independently authored holdout.
Some facts use lexical alternatives; all scores need failure inspection.

Materialize exact code from the pinned source versions (no LLM-authored gold):

```bash
node scripts/confluence/prepare-evaluation.mjs
```

This writes a private, git-ignored JSONL. The frozen 2026-09-10 comparison uses
`evaluation/results/kbr.v1.prepared-v2.jsonl`. Existing output is not overwritten;
use a new `--output` for a later snapshot. Version or checksum mismatches require
review before regenerating expectations. Run after all expected files are READY:

```bash
make evaluate-answers WORKSPACE_ID=<uuid> \
  DATASET=/evaluation/results/kbr.v1.prepared-v2.jsonl CORPUS= \
  EVALUATION_OUTPUT=/evaluation/results/kbr-v1-answers.json
```

Freeze polling during comparisons and retain the manifest, embedding model,
parser version and inference parameters with the result. Prepared cases now carry
`source_page_id` and `source_version`; the runner rejects missing/stale sources.
It also compares document identities, versions and hashes before/after the run
and rejects a changing corpus. `--answer-max-tokens` optionally overrides the API
default (64..4096); `finish_reason` is recorded for each answer. Older datasets
without source pins retain their filename/hash checks.

New metrics: `code_block_coverage` and `code_order_accuracy`. They match literal
source blocks inside answer code fences and normalize only CRLF. They do **not**
prove absence of added commands, complete prerequisite coverage, or execution
safety. Existing Recall@k/MRR, facts, abstention, citation and latency metrics
remain available.

The frozen 36-case baseline is
`evaluation/results/kbr-v1-answers-baseline-800.json`; the accepted quality
candidate is `evaluation/results/kbr-v1-answers-candidate-20260910-800.json`, with
the same corpus and generation settings. Recall@1/5/10 changed from
65.625/81.25/84.375% to 68.75/93.75/96.875%; MRR from 0.719618 to 0.798735;
required-fact coverage from 70.6944% to 88.75%. Answerable non-abstention improved
from 25/32 to 32/32 while all four live-state cases still abstain. Exact code and
order improved from 4/9 to 9/9. Manual review found no added commands in those nine
answers. Context fact/source/gold-fact coverage is 100%; citation source coverage
is 31/32 because one exact command cites an equivalent duplicate page rather than
the pinned original.

The stored reports remain immutable. After fixing fact matching so citation labels
cannot satisfy numeric facts, token boundaries are respected, and regular hyphens
match spaces, an offline rescore gives 70.4167% for the baseline and 89.3056% for
the accepted 2026-09-10 candidate. A three-case prompt experiment improved the
known generation-gap split from 27.78% to 61.11%. After explicit authorization,
the same prompt was evaluated on all 36 cases and accepted as the working
configuration. The full report is
`evaluation/results/kbr-v1-answers-completeness-candidate-20260911-800.json`.
Its required-fact coverage is 93.4722%, +4.1666 percentage points over the
previous candidate rescored with the same metric. Retrieval is unchanged;
answerable/non-answerable behavior remains 32/32 and 4/4, exact values remain
5/5, exact code and order remain 9/9, and citation source coverage is now 32/32.
Manual review found no added or reordered commands.

Two real wording gaps remain: `kbr-001` omits `ostor-ctl`, and `kbr-016` omits
the test requests to a bucket. `kbr-022` contains all expected information, but
the frozen gold uses digits `2`/`3` while the answer says “два”/“три”; the report
therefore retains the conservative mechanical score of 3/5 rather than being
edited after inspection. A future dataset revision can encode these as explicit
lexical alternatives without mutating the frozen comparison artifact.

The comparison report is
`evaluation/results/kbr-v1-baseline-vs-candidate-20260910.json`: all 20 quality
gates pass, while search p50/p95 and answer p95 exceed the baseline latency gates.
The owner accepted latency as non-blocking for this iteration; the report therefore
correctly remains globally failed rather than hiding the regression. These are
self-authored development cases, not an independent acceptance benchmark.

The completeness comparison is
`evaluation/results/kbr-v1-candidate-vs-completeness-20260911.json`: all 20
quality checks pass. Only search p50 misses the default 20% latency gate
(223.95 ms versus a 211.2 ms maximum); search p95 and answer p50/p95 improve.
The comparison deliberately remains `passed=false`, while latency stays
non-blocking by the owner's decision.

## Tests / release gates

```bash
node scripts/confluence/sync.test.mjs
make local-test
make web-build
```

Python tests cover CDATA, exact whitespace, ordered lists, macro filtering,
XXE/invalid XML rejection, oversized chunks, context truncation and code metrics.
uv is installed and both API and worker use Python 3.12.14 through PyCharm.
For the 2026-09-10 quality changes, the broader suite without
sandbox-problematic TestClient cases has 148 passing and 4 skipped tests.
API TestClient tests can hang inside the terminal sandbox; run those tests with
the approved host execution context. Database integration tests require an
isolated migrated PostgreSQL instance and `RUN_DATABASE_TESTS=1`.

The full offline snapshot audit checks source/checksum/version consistency and
verbatim code content/order/atomicity. Run `app.confluence_audit` with the worker
interpreter, `--snapshot .storage/confluence/KBR` and a new private `--output`.
The 2026-09-09 imported-corpus audit
(`evaluation/results/kbr-parser-audit-imported.json`) passed all 298 pages and
2,120 code blocks (including HTML code/pre), producing 1,939 chunks; 42 oversized
chunks are explicitly flagged.
CDATA examples containing DOCTYPE remain literal code, while actual XML
declarations are rejected. Typed code placeholders prevent nested inline HTML
formatting, table cells or headings from corrupting code-block boundaries.

## Remaining enterprise work

- Per-document/group Confluence ACL synchronization is not implemented. Import
  uses a service account; retrieval is governed by existing tenant/workspace
  rules. Do not share this workspace beyond the approved audience.
- Polling does not provide instantaneous revocation. During a source outage the
  last indexed snapshot can remain searchable; a fail-closed freshness policy
  must be designed before strict enterprise access guarantees are claimed.
- No automatic PII/secret redaction or output-DLP is implemented. Blind redaction
  can corrupt technical commands; define typed placeholders and exceptions first.
- Webhooks require administrator approval, authenticated delivery verification,
  deduplication, ordering and a reconciliation fallback. They are a later step.
- Raw snapshots and manifests provide lineage, but not a centralized immutable
  audit service or operational alerting/SLO dashboard.
- A changed document is temporarily non-searchable while queued/processing.
  Chunk replacement commits transactionally; zero-downtime corpus-version
  switching and pinned historical-answer citations need further work.

Reference architecture sources:
[Atlassian webhooks](https://developer.atlassian.com/server/confluence/webhooks/),
[Confluent HTTP source polling](https://docs.confluent.io/cloud/current/connectors/cc-http-source.html).
