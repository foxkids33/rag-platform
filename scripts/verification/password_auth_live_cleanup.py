"""Delete only the approved REST-smoke resources, never the test accounts.

Exact UUIDs, ownership, names, empty chats/KBs and the one synthetic filename
are checked before any DELETE. User identities and all IDs remain in the
owner-only fixture for the parent's separately approved account deactivation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import uuid
from datetime import UTC, datetime

import httpx
from password_auth_live_smoke import (
    BASE_URL,
    FIXTURE,
    PRIVATE,
    load_private_json,
    save_private_json,
)


def require(condition: bool, description: str) -> None:
    if not condition:
        raise RuntimeError(description)


async def cleanup() -> None:
    fixture = load_private_json(FIXTURE)
    require(fixture.get("base_url") == BASE_URL and fixture.get("synthetic_only") is True,
            "Fixture does not match the approved synthetic server")
    accounts = fixture.get("accounts", [])
    require(len(accounts) == 2 and {row.get("label") for row in accounts} == {"A", "B"},
            "Unexpected account inventory")
    context = ssl.create_default_context(cafile=str(PRIVATE / "tls.crt"))
    clients = []
    plan = []
    report = {"started_at": datetime.now(UTC).isoformat(), "deleted_workspaces": [],
              "deleted_knowledge_bases": [], "accounts_deleted": False,
              "fixture_preserved": True, "corporate_documents_modified": False}
    try:
        # Complete all ownership/content checks before deleting any resource.
        for account in accounts:
            client = httpx.AsyncClient(base_url=BASE_URL, verify=context, trust_env=False,
                                       follow_redirects=False, timeout=30)
            clients.append(client)
            login = await client.post("/api/v1/auth/login", json={
                "username": account["username"], "password": account["password"],
            }, headers={"Origin": BASE_URL})
            require(login.status_code == 200, "Test account login failed")
            principal = login.json()
            subject = principal.get("subject", "")
            require(subject.startswith("user:") and principal.get("roles") == [],
                    "Cleanup requires the approved non-administrator account")
            account["subject"] = subject
            account["user_id"] = str(uuid.UUID(subject.removeprefix("user:")))
            save_private_json(FIXTURE, fixture)
            client.headers.update({"Origin": BASE_URL, "X-CSRF-Token": principal["csrf_token"]})
            workspace_id = str(uuid.UUID(account["workspace_id"]))
            base_id = str(uuid.UUID(account["knowledge_base_id"]))
            workspaces = await client.get("/api/v1/workspaces")
            require(workspaces.status_code == 200, "Cannot inspect private workspace")
            workspace = next((row for row in workspaces.json() if row["id"] == workspace_id), None)
            if workspace is not None:
                require(workspace["user_id"] == subject and workspace["tenant_id"] == principal["tenant_id"]
                        and workspace["name"].startswith(f"SECURITY SMOKE {account['label']} — "),
                        "Workspace no longer matches its synthetic owner/name")
                documents = await client.get(f"/api/v1/workspaces/{workspace_id}/documents")
                require(documents.status_code == 200, "Cannot inspect synthetic documents")
                expected_documents = {account["document_id"]} if account.get("document_id") else set()
                require({row["id"] for row in documents.json()} == expected_documents,
                        "Unexpected documents appeared in the test workspace")
                for document in documents.json():
                    require(document["filename"] == "security-smoke-synthetic.txt"
                            and document["status"] not in {"QUEUED", "PROCESSING"},
                            "Synthetic document is unexpected or still being processed")
                conversations = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations")
                require(conversations.status_code == 200 and
                        {row["id"] for row in conversations.json()} == {account["conversation_id"]},
                        "Unexpected conversations appeared in the test workspace")
                detail = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{account['conversation_id']}")
                require(detail.status_code == 200 and detail.json().get("messages") == [],
                        "Synthetic conversation acquired messages; manual review is required")
            bases = await client.get("/api/v1/knowledge-bases")
            require(bases.status_code == 200, "Cannot inspect private knowledge bases")
            base = next((row for row in bases.json() if row["id"] == base_id), None)
            if base is not None:
                require(base["name"] == f"SECURITY SMOKE {account['label']} — empty private KB"
                        and base["active_version_id"] is None,
                        "Knowledge base no longer matches the synthetic fixture")
                versions = await client.get(f"/api/v1/knowledge-bases/{base_id}/versions")
                require(versions.status_code == 200 and versions.json() == [],
                        "Synthetic knowledge base acquired versions; manual review is required")
            plan.append((account, client, workspace is not None, base is not None))
        for account, client, has_workspace, has_base in plan:
            if has_workspace:
                deleted = await client.delete(f"/api/v1/workspaces/{account['workspace_id']}")
                require(deleted.status_code == 204, "Synthetic workspace deletion failed")
                account["workspace_deleted_at"] = datetime.now(UTC).isoformat()
                report["deleted_workspaces"].append(account["workspace_id"])
                save_private_json(FIXTURE, fixture)
            if has_base:
                deleted = await client.delete(f"/api/v1/knowledge-bases/{account['knowledge_base_id']}")
                require(deleted.status_code == 204, "Synthetic knowledge base deletion failed")
                account["knowledge_base_deleted_at"] = datetime.now(UTC).isoformat()
                report["deleted_knowledge_bases"].append(account["knowledge_base_id"])
                save_private_json(FIXTURE, fixture)
            require((await client.get("/api/v1/auth/me")).status_code == 200,
                    "The test account must remain active for the approved later deactivation")
            require((await client.post("/api/v1/auth/logout")).status_code == 204,
                    "Could not revoke cleanup's own login session")
        fixture["resource_cleanup_complete"] = True
        fixture["account_deactivation_pending_parent_approval"] = True
        save_private_json(FIXTURE, fixture)
        report["passed"] = True
        report["finished_at"] = datetime.now(UTC).isoformat()
        save_private_json(PRIVATE / "rest-security-cleanup.json", report)
        print(json.dumps({"passed": True, "workspaces_deleted": len(report["deleted_workspaces"]),
                          "knowledge_bases_deleted": len(report["deleted_knowledge_bases"]),
                          "accounts_retained": True, "identity_uuids_saved": True,
                          "fixture_file": str(FIXTURE)}))
    finally:
        for client in clients:
            await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete-synthetic-resources", action="store_true")
    if not parser.parse_args().delete_synthetic_resources:
        parser.print_help()
        return 0
    try:
        asyncio.run(cleanup())
        return 0
    except Exception as error:  # noqa: BLE001 - credentials/response bodies must never be logged
        print("Synthetic cleanup stopped:", type(error).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
