"""Clean-install safety contracts. Stdlib only; no Docker or network required."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("clean_profile", SOURCE_ROOT / "scripts/deploy/clean_profile.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class CleanProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "deploy").mkdir()
        (self.root / profile.TEMPLATE).write_text((SOURCE_ROOT / profile.TEMPLATE).read_text())
        (self.root / profile.COMPOSE).write_text("services: {}\n")
        self.env_file = self.root / "installation.env"
        with redirect_stdout(io.StringIO()):
            profile.setup(self.env_file, "pilot", root=self.root)

    def update(self, **values):
        content = profile.parse_env(self.env_file)
        content.update(values)
        self.env_file.write_text("".join(f"{key}={value}\n" for key, value in content.items()))
        self.env_file.chmod(0o600)

    def configure(self):
        self.update(
            VLLM_BASE_URL="https://llm.example.invalid/v1", VLLM_MODEL="team-chat",
            EMBEDDING_BASE_URL="https://vectors.example.invalid", EMBEDDING_MODEL="team-embed",
            EMBEDDING_DIM="384", CONFLUENCE_BASE_URL="https://wiki.example.invalid",
            CONFLUENCE_ALLOWED_SPACES="DOCS,OPS", CONFLUENCE_ACCESS_TOKEN="synthetic-test-token",
        )

    def docker(self, args, **kwargs):
        if args[:3] == ["docker", "context", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([{"Endpoints": {"docker": {"Host": "unix:///tmp/docker-test.sock"}}}]))
        return subprocess.CompletedProcess(args, 0)

    def test_setup_uses_private_fresh_secrets_and_does_not_read_existing_env(self):
        old = self.root / ".env"
        old.write_text("existing install must remain untouched")
        other = self.root / "second.env"
        with redirect_stdout(io.StringIO()):
            profile.setup(other, "pilot", root=self.root)
        one = profile.parse_env(self.env_file)
        two = profile.parse_env(other)
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        for key in ["COMPOSE_PROJECT_NAME", "POSTGRES_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"]:
            self.assertNotEqual(one[key], two[key])
        self.assertEqual(old.read_text(), "existing install must remain untouched")
        with self.assertRaises(FileExistsError):
            profile.setup(other, "pilot", root=self.root)
        with self.assertRaises(profile.CleanError):
            profile.setup(old, "pilot", root=self.root)

    def test_setup_rejects_symlinks(self):
        link = self.root / "link.env"
        link.symlink_to(self.env_file)
        with self.assertRaises(profile.CleanError):
            profile.setup(link, "pilot", root=self.root)
        with self.assertRaises(profile.CleanError):
            profile.validate_env(link, root=self.root)

    def test_literal_parser_rejects_duplicate_shell_or_control_values(self):
        for content in ["ONE=1\nONE=2\n", "VALUE=$(id)\n", "VALUE=\"quoted\"\n", "VALUE=trail \n", "VALUE=one\tvalue\n"]:
            with self.subTest(content=repr(content)):
                self.env_file.write_text(content)
                with self.assertRaises(profile.CleanError):
                    profile.parse_env(self.env_file)

    def test_incomplete_template_can_build_and_stop_but_cannot_start(self):
        profile.validate_env(self.env_file, root=self.root, connections=False)
        with self.assertRaises(profile.CleanError):
            profile.validate_env(self.env_file, root=self.root)
        with patch.object(profile.subprocess, "run", side_effect=self.docker) as run:
            for command in ["build", "down", "ps", "logs"]:
                self.assertEqual(profile.compose(self.env_file, command, root=self.root), 0)
            self.assertEqual(run.call_count, 8)
            self.assertTrue(all("--volumes" not in call.args[0] and "-v" not in call.args[0] for call in run.call_args_list))

    def test_start_requires_explicit_local_pilot(self):
        self.configure()
        with patch.object(profile.subprocess, "run", side_effect=self.docker) as run:
            with self.assertRaisesRegex(profile.CleanError, "allow-local-pilot"):
                profile.compose(self.env_file, "up", root=self.root)
            run.assert_not_called()
            self.assertEqual(profile.compose(self.env_file, "up", root=self.root, allow_local_pilot=True), 0)

    def test_rejects_unsafe_origins_missing_credentials_and_spaces(self):
        self.configure()
        baseline = self.env_file.read_text()
        cases = [
            {"RAG_BIND_HOST": "0.0.0.0"}, {"WEB_ORIGIN": "http://localhost:18573"},
            {"CONFLUENCE_BASE_URL": "http://wiki.example.invalid"},
            {"CONFLUENCE_BASE_URL": "https://name:secret@wiki.example.invalid"},
            {"CONFLUENCE_ACCESS_TOKEN": ""}, {"CONFLUENCE_ALLOWED_SPACES": "*"},
            {"CONFLUENCE_ALLOWED_SPACES": "DOCS,,OPS"}, {"EMBEDDING_DIM": "3000"},
            {"VLLM_BASE_URL": "https://llm.example.invalid?token=secret"},
        ]
        for values in cases:
            with self.subTest(keys=list(values)):
                self.env_file.write_text(baseline)
                self.update(**values)
                with self.assertRaises(profile.CleanError):
                    profile.validate_env(self.env_file, root=self.root)

    def test_basic_credentials_require_both_username_and_token(self):
        self.configure()
        self.update(CONFLUENCE_AUTH_MODE="basic", CONFLUENCE_ACCESS_TOKEN="")
        with self.assertRaises(profile.CleanError):
            profile.validate_env(self.env_file, root=self.root)
        self.update(CONFLUENCE_EMAIL="account@example.invalid", CONFLUENCE_API_TOKEN="synthetic-token")
        profile.validate_env(self.env_file, root=self.root)

    def test_permissions_and_project_name_cannot_target_existing_stack(self):
        self.configure()
        self.env_file.chmod(0o644)
        with self.assertRaises(profile.CleanError):
            profile.validate_env(self.env_file, root=self.root)
        self.update(COMPOSE_PROJECT_NAME="rag-existing")
        with self.assertRaises(profile.CleanError):
            profile.validate_env(self.env_file, root=self.root)

    def test_parent_compose_and_secret_variables_cannot_override_selected_file(self):
        with patch.dict(os.environ, {"COMPOSE_FILE": "other.yml", "COMPOSE_PROJECT_NAME": "existing", "POSTGRES_PASSWORD": "inherited"}), patch.object(profile.subprocess, "run", side_effect=self.docker) as run:
            profile.compose(self.env_file, "build", root=self.root)
        args, child = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertNotIn("COMPOSE_FILE", child)
        self.assertEqual(child["POSTGRES_PASSWORD"], profile.parse_env(self.env_file)["POSTGRES_PASSWORD"])
        self.assertIn(str(self.env_file.resolve()), args)
        self.assertNotIn("existing", args)
        self.assertIn(str(self.root / profile.COMPOSE), args)
        self.assertEqual(args[args.index("--project-directory") + 1], str(self.root / "deploy"))

    def test_remote_docker_host_or_context_is_rejected(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://remote.example.invalid"}):
            with self.assertRaises(profile.CleanError):
                profile.compose(self.env_file, "build", root=self.root)
        remote = subprocess.CompletedProcess([], 0, json.dumps([{"Endpoints": {"docker": {"Host": "tcp://remote.example.invalid:2376"}}}]))
        with patch.dict(os.environ, {"DOCKER_HOST": ""}), patch.object(profile.subprocess, "run", return_value=remote):
            with self.assertRaises(profile.CleanError):
                profile.compose(self.env_file, "build", root=self.root)

    def test_oidc_requires_complete_secure_auth_settings(self):
        self.configure()
        self.update(RAG_CLEAN_MODE="oidc", AUTH_ENABLED="true", AUTH_ALLOW_UNAUTHENTICATED_PILOT="false")
        with self.assertRaises(profile.CleanError):
            profile.validate_env(self.env_file, root=self.root)
        self.update(
            WEB_ORIGIN="https://rag.example.invalid", OIDC_ISSUER="https://sso.example.invalid/realm/team",
            OIDC_JWKS_URL="https://sso.example.invalid/keys", OIDC_AUDIENCE="rag-api", OIDC_CLIENT_ID="rag-web",
            OIDC_REDIRECT_URI="https://rag.example.invalid/callback",
            OIDC_POST_LOGOUT_REDIRECT_URI="https://rag.example.invalid/",
        )
        profile.validate_env(self.env_file, root=self.root)
        self.update(OIDC_ALGORITHM="HS256")
        with self.assertRaises(profile.CleanError):
            profile.validate_env(self.env_file, root=self.root)

    def package_fixture(self):
        files = [".gitignore", "CLEAN_INSTALL.md", "docs/confluence-clean-deployment.md", "apps/object_store/Dockerfile.clean", "apps/object_store/.dockerignore"]
        files.extend("scripts/deploy/" + name for name in ["clean_profile.py", "compose-clean.sh", "setup-clean-env.sh", "package-clean.sh"])
        for app in ["api", "worker"]:
            files.extend(f"apps/{app}/{name}" for name in ["Dockerfile.clean", ".dockerignore", "pyproject.toml", "uv.lock", "app/main.py"])
        files.extend(["apps/api/alembic.ini", "apps/api/alembic/env.py", "apps/web/Dockerfile", "apps/web/src/main.tsx", "apps/web/public/runtime-config.js"])
        for name in files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic source\n")
        for name in [".env", ".storage/corpus.json", "apps/api/tests/private_fixture.txt", "apps/web/public/presentation-offline.zip", "apps/web/public/presentation/demo.json", "apps/web/node_modules/library.js", "evaluation/corpus.json"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private material must never enter archive")

    def test_package_is_allowlisted_and_manifest_hashes_match_archived_bytes(self):
        self.package_fixture()
        output = self.root / "source.tar.gz"
        with redirect_stdout(io.StringIO()):
            profile.package(output, root=self.root)
        checksum = Path(str(output) + ".sha256").read_text().split()[0]
        self.assertEqual(checksum, hashlib.sha256(output.read_bytes()).hexdigest())
        with tarfile.open(output) as archive:
            manifest = json.load(archive.extractfile("CLEAN_SOURCE_MANIFEST.json"))["sha256"]
            self.assertEqual(set(archive.getnames()), set(manifest) | {"CLEAN_SOURCE_MANIFEST.json"})
            for name, expected in manifest.items():
                self.assertEqual(hashlib.sha256(archive.extractfile(name).read()).hexdigest(), expected)
            self.assertFalse(any("presentation" in name or "node_modules" in name or name.endswith(".env") or ".storage" in name or "/tests/" in name for name in archive.getnames()))
        with self.assertRaises(profile.CleanError):
            profile.package(output, root=self.root)

    def test_package_rejects_symlinked_application_sources(self):
        self.package_fixture()
        (self.root / "apps/api/app/escape.py").symlink_to(self.root / ".env")
        with self.assertRaises(profile.CleanError):
            profile.package_files(self.root)


if __name__ == "__main__":
    unittest.main()
