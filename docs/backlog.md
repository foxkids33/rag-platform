# Technical backlog

## Retrieval exact values and short documents

Deferred after source-mode verification:

- detect identifiers such as `BLUE-KB-942`, serial numbers and mixed alphanumeric codes;
- normalize hyphens, underscores, punctuation and case for exact matching;
- add an exact-identifier retrieval channel with priority over weak dense matches;
- improve scoring for documents that contain only one short chunk;
- refuse exact-value questions when the requested value is absent from selected sources.

The workspace/KB source filters are working independently from this issue.
