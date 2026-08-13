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

Run retrieval-only evaluation from the repository root:

```bash
make evaluate WORKSPACE_ID=<uuid> DATASET=/evaluation/datasets/example.jsonl
```

Include generation, abstention, and citation validity:

```bash
make evaluate-answers WORKSPACE_ID=<uuid> DATASET=/evaluation/datasets/example.jsonl
```

Reports are written to `evaluation/results/` and should not be committed when
they contain internal questions or answers.
