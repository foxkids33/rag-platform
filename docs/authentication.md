# Authentication and tenant isolation

The API supports two modes:

- `AUTH_ENABLED=false`: isolated local development. Every request runs as
  `AUTH_LOCAL_SUBJECT` in `AUTH_LOCAL_TENANT` with the tenant-admin role.
- `AUTH_ENABLED=true`: every business endpoint requires a signed OIDC bearer
  token. Health endpoints remain public.

Do not expose an environment with local authentication to an untrusted network.

## OIDC configuration

Set at least:

```env
AUTH_ENABLED=true
OIDC_ISSUER=https://identity.example.com/realms/company
OIDC_AUDIENCE=rag-api
OIDC_JWKS_URL=https://identity.example.com/realms/company/protocol/openid-connect/certs
OIDC_ALGORITHM=RS256
OIDC_TENANT_CLAIM=tenant_id
OIDC_ROLES_CLAIM=roles
AUTH_ADMIN_ROLE=rag-admin
```

Claim paths support dotted nested fields. For example, Keycloak-style roles
can use `OIDC_ROLES_CLAIM=realm_access.roles`. The subject (`sub`), expiration
(`exp`), issue time (`iat`), issuer, audience, signature, and configured tenant
claim are validated. Algorithms are fixed by configuration and never selected
from an untrusted token. Symmetric HMAC algorithms are rejected; use the
corporate provider's asymmetric signing keys.

Check the active identity:

```bash
curl -H "Authorization: Bearer $RAG_API_TOKEN" \
  http://localhost:8000/api/v1/auth/me
```

## Authorization model

- A normal user can create and access only workspaces owned by their token
  subject and only inside their tenant.
- A tenant administrator can access all workspaces in the same tenant and
  manage tenant-wide knowledge bases and their versions.
- Cross-tenant or foreign-workspace lookups return `404` to avoid exposing
  whether a resource exists.
- Workspace ownership is derived from the verified token. Clients cannot set
  `user_id` when creating a workspace.

Migration `0008_tenant_isolation` assigns pre-existing local data to tenant
`local` and owner `local-user`. Reassign legacy data explicitly before enabling
OIDC in an already shared deployment.

This layer is the application authorization boundary. PostgreSQL row-level
security, group/document ACL propagation, and frontend OIDC login are separate
hardening steps required before a multi-tenant enterprise rollout.
