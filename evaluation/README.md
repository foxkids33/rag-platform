# Evaluation datasets

Store versioned RAG evaluation datasets in `datasets/*.jsonl`. Each non-empty
line is one JSON object. Use stable filenames for the first TXT pilot because
database document UUIDs change after a clean rebuild.

Required fields:

- `id`: stable unique case identifier;
- `question`: user question.

Retrieval gold fields (choose one):

- `expected_filenames`: recommended for the TXT pilot;
- `expected_document_ids`: useful for a fixed, persistent environment.

Optional fields include `expected_abstention`, `question_type`,
`expected_answer`, `required_facts`, `forbidden_facts`, `difficulty`, and
`notes`.

Facts are matched deterministically after Unicode, case, punctuation, and PDF
line-break normalization. Use `||` inside one fact when multiple renderings are
equivalent, for example `"20 Тбайт||20 ТБ"`. Every item in `required_facts`
must be present; alternatives separated by `||` count as one item.

## Skala technical-review pilot

`datasets/skala-technical-reviews.v1.jsonl` contains 96 reviewed cases for the
eight TXT files listed in `corpora/skala-technical-reviews.v1.json`:

- 32 direct facts;
- 16 exact values, versions, and dates;
- 16 architecture questions;
- 12 product-disambiguation questions;
- 8 multi-document questions;
- 12 questions that must be refused.

The committed split files keep 72 cases in the tuning set and 24 cases in an
operational acceptance holdout. The holdout is stratified by question type and
source document:

- `splits/skala-technical-reviews.v1.dev.txt` — inspect failures and tune here;
- `splits/skala-technical-reviews.v1.acceptance.txt` — run only at milestone
  boundaries and do not use its per-case output for tuning.

Validate that the files are disjoint and cover all 96 cases:

```bash
make evaluate-splits-check
```

All v1 cases were evaluated before this split existed, so the acceptance file
is not a truly blind set. A future v2 acceptance dataset must be authored by a
reviewer, stored outside the developer workflow, and executed with case details
omitted.

Upload the original files without renaming or editing them. Before evaluation,
the runner checks that every manifest document exists in the workspace, is
`READY`, is enabled for search, and has the expected SHA-256. The original TXT
files are intentionally not committed to the repository.

Run retrieval-only evaluation from the repository root:

```bash
make evaluate WORKSPACE_ID=<uuid> \
  DATASET=/evaluation/datasets/example.jsonl CORPUS=
```

Include generation, abstention, and citation validity:

```bash
make evaluate-answers WORKSPACE_ID=<uuid> \
  DATASET=/evaluation/datasets/example.jsonl CORPUS=
```

Answer evaluation uses `ANSWER_TEMPERATURE=0.0` by default and records it as
`configuration.answer_temperature`. Override it only for an explicit
stochastic experiment; the checked dev and acceptance workflows require zero.
It also sends and records `QUERY_SELECTION_WEIGHT=0.30`. Use `0` only for an
explicit selector-off A/B run; checked reports require `0.30`, so an accidental
configuration mismatch cannot be accepted as a quality result.

For the Skala pilot the dataset and manifest are Makefile defaults. Save the
first runs under stable names:

```bash
make evaluate \
  WORKSPACE_ID=<uuid> \
  EVALUATION_OUTPUT=/evaluation/results/skala-v1-retrieval-407627d.json

make evaluate-answers \
  WORKSPACE_ID=<uuid> \
  EVALUATION_OUTPUT=/evaluation/results/skala-v1-answers-407627d.json
```

The default evaluation path does not request the current reranker. Run the
development split and create both JSON and Markdown failure inventories:

```bash
make evaluate-dev WORKSPACE_ID=<uuid>
```

This writes:

- `results/skala-v1-dev-latest.json`;
- `results/skala-v1-dev-failures.json`;
- `results/skala-v1-dev-failures.md`.

Each failing case is assigned to one or more layers: retrieval, reranker,
generation, citations, latency, or evaluation execution. Categories distinguish
ranking errors, incomplete Recall@10, missed/false abstention, missing required
facts in the answer, LLM context, gold-document chunks, or cited context,
exact-value errors, forbidden facts, citation failures, and slow cases. The
failure JSON and Markdown include both the search result filenames and the
smaller set of context filenames actually passed to the model.

Context diagnostics separate three failure modes:

- `gold_source_missing_from_context`: a required document was retrieved too low
  or was absent and did not reach the LLM;
- `gold_source_chunk_missing_required_facts`: the document reached the LLM, but
  the selected chunks did not contain every annotated fact;
- `model_abstained_with_available_context`: at least one annotated fact was in
  a context chunk from an expected document, but the model still refused.

At a milestone boundary, run the 24-case acceptance holdout. The aggregate
report is saved without questions, answers, sources, or other per-case details:

```bash
make evaluate-acceptance WORKSPACE_ID=<uuid>
```

Run the current non-regression floors together with each split:

```bash
make evaluate-dev-check WORKSPACE_ID=<uuid>
make evaluate-acceptance-check WORKSPACE_ID=<uuid>
```

The baseline gates are calibrated to the accepted no-rerank schema-v3 run and
protect the split size, execution mode, error count, quality metrics, and p95
latency. The dev gate additionally requires at least 75% correct unanswerable
abstention while retaining at least 40% non-abstention on answerable cases; this
prevents both missed refusals and a trivial refuse-everything strategy.
It also protects the first context diagnostic baseline: at least 0.40 context
fact coverage, 0.85 expected-document context coverage, and 0.36 fact coverage
inside expected-document chunks. These are regression floors, not product
targets.
`quality-gates.acceptance-target.json` contains the next product-quality targets;
it is expected to fail until the quality sprint is complete.

The schema-v3 report contains Recall@1/5/10, MRR, nDCG@10, overall and
class-balanced abstention accuracy, required-fact coverage, exact-value
accuracy, forbidden-fact violation rate, citation syntax validity, cited-source
fact coverage, cited gold-document coverage, p50/p95 latency, and breakdowns by
question type and expected filename. It also reports three pre-generation
metrics for every answerable case, including abstentions:

- `context_source_coverage`: share of expected documents present in the LLM
  context;
- `context_fact_coverage`: share of required facts found anywhere in that
  context;
- `gold_context_fact_coverage`: share of required facts found specifically in
  context chunks from expected documents, avoiding credit for a coincidental
  value in a distractor document.

Citation metrics remain post-generation metrics and are evaluated only for
non-abstained answers. The per-case report now preserves
`cited_source_indices`, while the aggregate context metrics make abstained cases
observable instead of silently excluding them from context diagnosis.

The per-case answer diagnostics also record `abstention_reason`, distinguishing
the deterministic retrieval quality gate from a model decision that the
selected context does not support a reliable answer. The final SSE `done`
event, persisted chat metadata, JSON answer endpoint, and evaluator use the
same effective abstention value.
Grounded partial answers are not abstentions: when at least one material part of
the question is supported, the answer should cite that part and identify the
missing part. A question that exclusively requests an absent exact number,
date, or version should still abstain. Contextual abstentions may retain a short
grounded explanation and cited sources while remaining machine-readable through
`abstained=true`.

The report also records how often reranking was actually applied. A requested
reranker can fail open or be disabled by the active Compose stack, so do not
interpret `configuration.rerank=true` as proof that reranking ran. Start the
TXT stack with reranking, without the large Docling dependency, and verify it:

```bash
make dev-rerank
make ps-rerank
```

Then run the same evaluation commands. `execution.rerank.application_rate`
must be `1.0` for a reranked baseline.

For an apples-to-apples comparison, create a schema-v3 baseline without the
reranker first, then rerun after `make dev-rerank`:

```bash
make evaluate-answers RERANK=false \
  WORKSPACE_ID=<uuid> \
  EVALUATION_OUTPUT=/evaluation/results/skala-v1-answers-v3-no-rerank.json

make dev-rerank
make evaluate-answers RERANK=true \
  WORKSPACE_ID=<uuid> \
  EVALUATION_OUTPUT=/evaluation/results/skala-v1-answers-v3-rerank.json
```

Compare a later candidate with the saved baseline. A quality metric may fall by
at most 0.02 and latency may grow by at most 20% by default:

```bash
make evaluate-compare \
  BASELINE=/evaluation/results/skala-v1-answers-407627d.json \
  CANDIDATE=/evaluation/results/skala-v1-answers-candidate.json
```

Fixed acceptance thresholds can be applied separately after the first honest
baseline establishes realistic targets:

```bash
make evaluate-compare \
  CANDIDATE=/evaluation/results/skala-v1-answers-candidate.json \
  GATES=/evaluation/quality-gates.acceptance-target.json
```

Exit code `3` means that at least one comparison or quality gate failed.

When OIDC authentication is enabled, pass a short-lived access token through
the environment so it is not written to the report or command line:

```bash
RAG_API_TOKEN='<access-token>' make evaluate WORKSPACE_ID=<uuid>
```

Reports are written to `evaluation/results/` and should not be committed when
they contain internal questions or answers.
