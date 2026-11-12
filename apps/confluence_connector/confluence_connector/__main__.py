import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import dotenv_values

from confluence_connector.config import Settings
from confluence_connector.confluence import ConfluenceClient
from confluence_connector.runner import RagClient, run


def load_settings(args: argparse.Namespace) -> Settings:
    # Explicit opt-in: reuse laptop secrets without copying them to the server.
    values = dotenv_values(args.env_file) if Path(args.env_file).exists() else {}
    fallback = {}
    if args.project_env:
        if not Path(args.project_env).is_file():
            raise ValueError("Project environment file does not exist")
        project = dotenv_values(args.project_env)
        mapping = {
            "confluence_url": "CONFLUENCE_BASE_URL",
            "confluence_auth_mode": "CONFLUENCE_AUTH_MODE",
            "confluence_username": "CONFLUENCE_EMAIL",
            "confluence_token": "CONFLUENCE_API_TOKEN",
        }
        for name, source in mapping.items():
            if name == "confluence_token" and any(
                key in values or key in os.environ
                for key in ("CONFLUENCE_TOKEN_FILE", "CONFLUENCE_COOKIE_FILE")
            ):
                continue
            if name.upper() not in values and name.upper() not in os.environ:
                value = project.get(source)
                if name == "confluence_token":
                    value = project.get("CONFLUENCE_ACCESS_TOKEN") or value
                if value:
                    fallback[name] = value
    if args.rag_url:
        fallback["rag_url"] = args.rag_url
    if args.allow_http:
        fallback["rag_allow_insecure_http"] = True
    if getattr(args, "credentials_file", None):
        fallback["rag_credential_file"] = args.credentials_file
    return Settings(_env_file=args.env_file, **fallback)


async def discover_accessible_spaces(settings: Settings) -> Settings:
    """Opt in to all spaces visible to the same configured Confluence account."""
    confluence = ConfluenceClient(settings)
    try:
        spaces = await confluence.list_spaces(include_all_accessible=True)
    finally:
        await confluence.close()
    if not spaces:
        raise RuntimeError("No Confluence spaces are visible to the configured account")
    keys = [space["key"] for space in spaces]
    logging.info("Using all %s Confluence spaces visible to the configured account", len(keys))
    return settings.model_copy(update={"confluence_allowed_spaces": ",".join(keys)})


async def start(args: argparse.Namespace, settings: Settings) -> None:
    if args.all_accessible_spaces:
        settings = await discover_accessible_spaces(settings)
    if args.check:
        await check(settings)
    else:
        await run(settings, enroll_pilot=args.enroll_pilot)


async def check(settings: Settings) -> None:
    rag = RagClient(settings)
    confluence = ConfluenceClient(settings)
    try:
        response = await rag.client.get(f"{rag.root}/api/v1/health")
        response.raise_for_status()
        print("RAG HTTP: OK")
        await confluence.check_connection()
        spaces = await confluence.list_spaces()
        print(f"Confluence REST: OK; allowed spaces visible: {len(spaces)}")
        if not spaces:
            raise RuntimeError("No configured spaces are visible to this Confluence account")
        response = await rag.client.get(f"{rag.root}/api/v1/integrations/confluence/connector")
        if response.status_code == 404:
            raise RuntimeError("Server has no connector API: deploy the connector update first")
        if response.status_code in {401, 403}:
            # The administration endpoint now requires a browser session. A
            # connector has a separate destination-bound machine credential;
            # verify it without enrolling again or leasing a queued job.
            if not rag._load_credential():
                raise RuntimeError(
                    "RAG user API is protected and no saved connector credential exists; "
                    "enroll this connector with an administrator-issued token first"
                )
            await rag.heartbeat()
            print("RAG connector machine API: OK (authenticated heartbeat)")
            return
        response.raise_for_status()
        if not response.json().get("enabled"):
            raise RuntimeError("Server connector API is disabled")
        print("RAG connector API: OK")
    finally:
        await confluence.close()
        await rag.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Outbound laptop Confluence connector")
    parser.add_argument("--env-file", default=".env.connector")
    parser.add_argument(
        "--project-env", help="Read Confluence defaults from the laptop project .env"
    )
    parser.add_argument("--rag-url")
    parser.add_argument(
        "--credentials-file", type=Path,
        help="Explicit private credential file; never reused for another RAG_URL",
    )
    parser.add_argument(
        "--all-accessible-spaces", action="store_true",
        help="Register every space visible to the configured Confluence account (no ACL bypass)",
    )
    parser.add_argument(
        "--allow-http", action="store_true", help="Allow the internal HTTP pilot endpoint"
    )
    parser.add_argument("--check", action="store_true", help="Check without enrolling or importing")
    parser.add_argument(
        "--enroll-pilot", action="store_true", help="Create enrollment on a pilot without OIDC"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings(args)
        asyncio.run(start(args, settings))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Validation/transport exceptions can contain input secrets.
        logging.error(
            "Connector stopped (%s); check configuration and connectivity", type(exc).__name__
        )
        if isinstance(exc, RuntimeError):
            logging.error("%s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
