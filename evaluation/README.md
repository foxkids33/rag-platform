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

The schema-v3 report contains Recall@1/5/10, MRR, nDCG@10, overall and
class-balanced abstention accuracy, required-fact coverage, exact-value
accuracy, forbidden-fact violation rate, citation syntax validity, cited-source
fact coverage, cited gold-document coverage, p50/p95 latency, and breakdowns by
question type and expected filename.

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
  GATES=/evaluation/quality-gates.example.json
```

Exit code `3` means that at least one comparison or quality gate failed.

Reports are written to `evaluation/results/` and should not be committed when
they contain internal questions or answers.
