# Confluence integration

For the full KBR snapshot, exact-code parsing, evaluation and optional polling
workflow, see [KBR RAG](kbr-rag.md). The current importer stores a versioned
`.confluence.json` envelope, not flattened HTML. Upgrade API and worker together.

The integration imports Confluence pages into a workspace and then uses the
same durable ingestion, chunking, embedding and retrieval path as uploaded
files. It supports both Confluence Cloud REST v2 and Confluence Data Center.

## Configuration

The server fixes the Confluence host, deployment type and authentication mode.
An interactive user enters personal credentials in the Confluence tab. The
token is sent only in the connection-check/import request, represented as a
masked secret in backend validation and never stored in PostgreSQL, object
storage or browser local storage. Production UI/API traffic must use HTTPS.

For Confluence Cloud with an API token:

```dotenv
CONFLUENCE_ENABLED=true
CONFLUENCE_DEPLOYMENT=cloud
CONFLUENCE_BASE_URL=https://company.atlassian.net
CONFLUENCE_AUTH_MODE=basic
CONFLUENCE_EMAIL=
CONFLUENCE_API_TOKEN=
```

For Confluence Data Center with a personal access token:

```dotenv
CONFLUENCE_ENABLED=true
CONFLUENCE_DEPLOYMENT=data_center
CONFLUENCE_BASE_URL=https://confluence.internal.example
CONFLUENCE_AUTH_MODE=bearer
CONFLUENCE_ACCESS_TOKEN=
```

HTTPS is required by default. An isolated internal HTTP deployment must opt in
with `CONFLUENCE_ALLOW_INSECURE_HTTP=true`. Keep TLS verification enabled; use a
trusted corporate CA in the container instead of disabling verification.

## API and UI

The dedicated Confluence tab is available to an authenticated user. The user
enters an Atlassian e-mail and API token for Cloud, or a personal access token
for Data Center, verifies the connection, then imports page IDs or a space key.
The API still checks workspace access, so a regular user can import only into a
workspace available to that principal.

The equivalent API request is:

```http
POST /api/v1/workspaces/{workspace_id}/imports/confluence
Content-Type: application/json

{
  "page_ids": ["123456", "234567"],
  "credentials": {
    "email": "user@company.example",
    "token": "request-only-secret"
  }
}
```

or:

```json
{
  "space_key": "ENG",
  "credentials": {
    "email": "user@company.example",
    "token": "request-only-secret"
  }
}
```

The request fetches at most `CONFLUENCE_MAX_PAGES_PER_IMPORT` pages and returns
`202` after durable ingestion jobs have been created. Repeating an import is
idempotent by `(workspace, source, page id)`: unchanged content is skipped,
changed content replaces the prior source and is reindexed. `force=true`
reindexes unchanged READY content. When importing several page IDs, an
unavailable page is reported as `failed` without cancelling successful pages.

## Permissions boundary

Confluence applies its page permissions while the importing user's account fetches
content. The current RAG workspace does **not** replicate per-page Confluence
ACLs. After import, access follows the workspace's own tenant/user rules. Use a
least-privilege account and do not mix pages with different audiences in one
workspace until document/group ACL propagation is implemented. Personal tokens
reduce over-broad source access during import, but do not replace source ACL
propagation after the page has been indexed.

Cloud implementation uses `/wiki/api/v2/pages` with the storage body format;
Data Center uses `/rest/api/content` with `body.storage` expansion. Pagination
links are accepted only when they remain on the configured Confluence host.
The connector imports structured page bodies and source metadata. Attachments, comments,
whiteboards and embedded attachment contents are not downloaded.
