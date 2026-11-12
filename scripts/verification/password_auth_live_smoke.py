"""Explicit, synthetic-only REST security smoke for the prepared HTTPS demo.

Nothing runs without --run. Credentials stay in owner-only local artifacts;
the test never imports corporate documents or invokes a model for answers.
Created accounts/workspaces are retained for the separately authorized UI test.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
import secrets
import ssl
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".storage/secure-upgrade-20261105"
FIXTURE = PRIVATE / "ui-test-accounts.json"
REPORT = PRIVATE / "rest-security-smoke.json"
BASE_URL = "https://192.0.2.122:15173"


def anonymous_upload_preflight(context: ssl.SSLContext, path: str) -> int:
    """Send headers only, not a large body: auth must reject before upload."""
    connection = http.client.HTTPSConnection("192.0.2.122", 15173, context=context, timeout=8)
    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Type", "multipart/form-data; boundary=security-smoke")
        connection.putheader("Content-Length", str(50 * 1024 * 1024))
        connection.putheader("Expect", "100-continue")
        connection.endheaders()
        return connection.getresponse().status
    finally:
        connection.close()


def load_private_json(path: Path) -> dict:
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise RuntimeError("Private artifact permissions or owner are invalid")
    return json.loads(path.read_text(encoding="utf-8"))


def save_private_json(path: Path, payload: dict) -> None:
    if path.exists():
        load_private_json(path)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


class Smoke:
    def __init__(self):
        self.report = {"checked_at": datetime.now(UTC).isoformat(), "base_url": BASE_URL,
                       "checks": [], "corporate_documents_modified": False,
                       "answer_model_calls": 0, "test_fixture_retained": True}

    def check(self, name: str, condition: bool, **safe_details) -> None:
        self.report["checks"].append({"name": name, "passed": bool(condition), **safe_details})
        save_private_json(REPORT, self.report)
        if not condition:
            raise RuntimeError("Security assertion failed: " + name)

    async def expect(self, client, method, path, expected, **kwargs):
        response = await client.request(method, path, **kwargs)
        self.check(f"{method} {path}", response.status_code == expected,
                   expected=expected, actual=response.status_code)
        return response

    async def run(self):
        credentials = load_private_json(PRIVATE / "admin-credentials.json")
        username = credentials.get("username") or credentials.get("login")
        password = credentials.get("password")
        if not username or not password:
            raise RuntimeError("Administrator credential structure is invalid")
        if FIXTURE.exists():
            fixture = load_private_json(FIXTURE)
            if fixture.get("base_url") != BASE_URL:
                raise RuntimeError("Test fixture belongs to a different server")
        else:
            suffix = secrets.token_hex(4)
            fixture = {"created_at": datetime.now(UTC).isoformat(), "base_url": BASE_URL,
                       "synthetic_only": True, "accounts": [
                           {"label": label, "username": f"security-{label.lower()}-20261105-{suffix}",
                            "password": secrets.token_urlsafe(24)} for label in ("A", "B")
                       ]}
            save_private_json(FIXTURE, fixture)
        accounts = fixture["accounts"]
        if len(accounts) != 2 or {account["label"] for account in accounts} != {"A", "B"}:
            raise RuntimeError("Unexpected test accounts; refusing to change them")
        context = ssl.create_default_context(cafile=str(PRIVATE / "tls.crt"))
        options = {"base_url": BASE_URL, "verify": context, "trust_env": False,
                   "timeout": 40, "follow_redirects": False}
        async with (
            httpx.AsyncClient(**options) as anonymous,
            httpx.AsyncClient(**options) as admin,
            httpx.AsyncClient(**options) as a,
            httpx.AsyncClient(**options) as b,
        ):
            for path in ("/api/v1/auth/me", "/api/v1/workspaces", "/api/v1/knowledge-bases"):
                await self.expect(anonymous, "GET", path, 401)
            for path in (
                "/api/v1/workspaces/00000000-0000-0000-0000-000000000000/documents",
                "/api/v1/knowledge-bases/00000000-0000-0000-0000-000000000000/versions/00000000-0000-0000-0000-000000000000/documents",
            ):
                status = await asyncio.to_thread(anonymous_upload_preflight, context, path)
                self.check("Anonymous upload rejected before body transfer: " + path,
                           status == 401, actual=status, body_bytes_sent=0)
            await self.expect(anonymous, "GET", "/presentation/demo-manifest.json", 401)
            admin_login = await self.expect(admin, "POST", "/api/v1/auth/login", 200,
                json={"username": username, "password": password}, headers={"Origin": BASE_URL})
            admin.headers.update({"Origin": BASE_URL, "X-CSRF-Token": admin_login.json()["csrf_token"]})
            baseline_response = await self.expect(admin, "GET", "/api/v1/workspaces", 200)
            legacy_ids = {row["id"] for row in baseline_response.json()
                          if not row["name"].startswith("SECURITY SMOKE ")}
            self.check("Administrator can see legacy workspaces", bool(legacy_ids), count=len(legacy_ids))
            for account, client in zip(accounts, (a, b), strict=True):
                endpoint = "/api/v1/auth/login" if account.get("registered") else "/api/v1/auth/register"
                result = await client.post(endpoint,
                    json={"username": account["username"], "password": account["password"]},
                    headers={"Origin": BASE_URL})
                if endpoint.endswith("register") and result.status_code == 409:
                    result = await client.post("/api/v1/auth/login",
                        json={"username": account["username"], "password": account["password"]},
                        headers={"Origin": BASE_URL})
                self.check(f"Account {account['label']} authenticated as ordinary user",
                           result.status_code in {200, 201} and result.json().get("roles") == [],
                           actual=result.status_code)
                account["registered"] = True
                save_private_json(FIXTURE, fixture)
                client.headers.update({"Origin": BASE_URL, "X-CSRF-Token": result.json()["csrf_token"]})
                cookie_header = result.headers.get("set-cookie", "")
                self.check(f"Account {account['label']} secure opaque cookie",
                           all(flag in cookie_header for flag in ("__Host-rag_session=", "HttpOnly", "Secure", "SameSite=lax", "Path=/")))
            a_account, b_account = accounts
            forged_password = secrets.token_urlsafe(24)
            forged = await self.expect(anonymous, "POST", "/api/v1/auth/register", 422,
                json={"username": "security-forged-" + secrets.token_hex(4),
                      "password": forged_password, "roles": ["rag-admin"]},
                headers={"Origin": BASE_URL})
            self.check("Validation does not reflect password", forged_password not in forged.text)
            a_csrf = a.headers.pop("X-CSRF-Token")
            await self.expect(a, "POST", "/api/v1/workspaces", 403,
                              json={"name": "SECURITY SMOKE MUST NOT CREATE"})
            a.headers["X-CSRF-Token"] = a_csrf
            await self.expect(a, "POST", "/api/v1/workspaces", 403,
                              json={"name": "SECURITY SMOKE MUST NOT CREATE"},
                              headers={"Origin": "https://untrusted.invalid"})
            for account, client in zip(accounts, (a, b), strict=True):
                if not account.get("workspace_id"):
                    response = await self.expect(client, "POST", "/api/v1/workspaces", 201,
                        json={"name": f"SECURITY SMOKE {account['label']} — synthetic private"})
                    account["workspace_id"] = response.json()["id"]
                    save_private_json(FIXTURE, fixture)
                workspace = account["workspace_id"]
                if not account.get("conversation_id"):
                    response = await self.expect(client, "POST", f"/api/v1/workspaces/{workspace}/conversations", 201,
                        json={"title": f"SECURITY SMOKE {account['label']} — synthetic chat"})
                    account["conversation_id"] = response.json()["id"]
                    save_private_json(FIXTURE, fixture)
                if not account.get("knowledge_base_id"):
                    response = await self.expect(client, "POST", "/api/v1/knowledge-bases", 201,
                        json={"name": f"SECURITY SMOKE {account['label']} — empty private KB"})
                    account["knowledge_base_id"] = response.json()["id"]
                    save_private_json(FIXTURE, fixture)
            if not b_account.get("document_id"):
                marker = "SECURITY-SMOKE-B-" + secrets.token_hex(6)
                response = await self.expect(b, "POST", f"/api/v1/workspaces/{b_account['workspace_id']}/documents", 201,
                    files={"file": ("security-smoke-synthetic.txt", f"Synthetic private test record. Marker {marker}. No corporate content.\n".encode(), "text/plain")})
                b_account["document_id"] = response.json()["id"]
                save_private_json(FIXTURE, fixture)
                self.report["synthetic_document_uploads"] = 1
            for owner, other, other_account in ((a, b, b_account), (b, a, a_account)):
                own = a_account if owner is a else b_account
                visible = (await self.expect(owner, "GET", "/api/v1/workspaces", 200)).json()
                self.check(f"Account {own['label']} sees only its private spaces",
                           own["workspace_id"] in {row["id"] for row in visible}
                           and other_account["workspace_id"] not in {row["id"] for row in visible}
                           and not legacy_ids.intersection(row["id"] for row in visible))
                other_ws = other_account["workspace_id"]
                for path in (
                    f"/api/v1/workspaces/{other_ws}/documents",
                    f"/api/v1/workspaces/{other_ws}/conversations",
                    f"/api/v1/workspaces/{other_ws}/conversations/{other_account['conversation_id']}",
                    f"/api/v1/knowledge-bases/{other_account['knowledge_base_id']}/versions",
                ):
                    await self.expect(owner, "GET", path, 404)
                await self.expect(owner, "GET", f"/api/v1/workspaces/{own['workspace_id']}/conversations/{other_account['conversation_id']}", 404)
                await self.expect(owner, "POST", f"/api/v1/workspaces/{other_ws}/search", 404,
                                  json={"query": "SECURITY SMOKE", "mode": "lexical", "rerank": False})
                await self.expect(owner, "GET", "/api/v1/integrations/confluence/connector", 403)
                await self.expect(owner, "GET", "/presentation/demo-manifest.json", 403)
            await self.expect(a, "PATCH", f"/api/v1/workspaces/{a_account['workspace_id']}/documents/{b_account['document_id']}", 404,
                              json={"search_enabled": False})
            visible_admin = (await self.expect(admin, "GET", "/api/v1/workspaces", 200)).json()
            visible_ids = {row["id"] for row in visible_admin}
            self.check("Administrator sees both new spaces and all legacy spaces",
                       legacy_ids | {row["workspace_id"] for row in accounts} <= visible_ids)
            for account in accounts:
                await self.expect(admin, "GET", f"/api/v1/workspaces/{account['workspace_id']}/conversations/{account['conversation_id']}", 200)
                await self.expect(admin, "GET", f"/api/v1/knowledge-bases/{account['knowledge_base_id']}/versions", 200)
            old_token = a.cookies.get("__Host-rag_session")
            await self.expect(a, "POST", "/api/v1/auth/logout", 204)
            await self.expect(a, "GET", "/api/v1/auth/me", 401)
            await self.expect(anonymous, "GET", "/api/v1/auth/me", 401,
                              headers={"Cookie": f"__Host-rag_session={old_token}"})
            await self.expect(b, "GET", "/api/v1/auth/me", 200)
            self.report["passed"] = True
            save_private_json(REPORT, self.report)
            print(json.dumps({"passed": True, "checks": len(self.report["checks"]),
                              "fixture_file": str(FIXTURE), "report_file": str(REPORT),
                              "accounts_retained_for_ui": True, "passwords_displayed": False}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Create the two approved synthetic accounts on HTTPS15173")
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return 0
    os.umask(0o077)
    try:
        asyncio.run(Smoke().run())
        return 0
    except Exception as error:  # noqa: BLE001 - never expose credential-bearing client errors
        # Do not print exception payloads, response bodies, passwords or cookies.
        print("Security smoke stopped:", type(error).__name__, file=sys.stderr)
        print("Read the private, credential-free report for the last assertion.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
