"""Pull CI-promoted source into a private checkout and publish it to the existing Compose project."""
import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path

STATE = Path("/var/lib/rag-delivery")
SOURCE = STATE / "source"
SITE = Path("/etc/rag-delivery")
COMPONENTS = ("api", "worker", "web", "connector")
SOURCES = {
    "api": ("apps/api/",), "worker": ("apps/worker/",),
    "web": ("apps/web/", "deploy/delivery/Dockerfile.web"),
    "connector": ("apps/confluence_connector/",),
}


def run(*args, output=False, **kwargs):
    result = subprocess.run(args, check=True, text="stdout" not in kwargs,
                            capture_output=output, **kwargs)
    return result.stdout.strip() if output else None


def git(*args):
    # STATE is private, but Docker preserves source file permissions when copying
    # them into non-root application images.
    return run("git", "-C", SOURCE, *args, output=True, umask=0o022)


def save(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def compose(*args, output=False):
    root = Path(os.environ["RAG_RUNTIME_DIR"])
    return run("docker", "compose", "--project-directory", root / "deploy",
               "--env-file", root / ".env.demo", "-p", os.environ["RAG_PROJECT"],
               "-f", SITE / "base.yml", "-f", SITE / "secure.json", *args, output=output)


def get_target():
    repo = os.environ["RAG_REPOSITORY"]
    if not (SOURCE / ".git").is_dir():
        SOURCE.mkdir(exist_ok=True)
        run("git", "init", "-q", SOURCE)
    # A fresh installation can start observing before the first green main build.
    ref = "refs/heads/deploy/production"
    if not git("ls-remote", repo, ref):
        return None
    git("fetch", "--quiet", "--no-tags", repo, f"{ref}:refs/remotes/release/green",
        "+refs/heads/main:refs/remotes/release/main")
    target = git("rev-parse", "refs/remotes/release/green")
    git("merge-base", "--is-ancestor", target, "refs/remotes/release/main")
    return target


def image(tag):
    return run("docker", "image", "inspect", "--format", "{{.Id}}", tag, output=True)


def current(service):
    container = ("rag-confluence-worker" if service == "connector"
                 else compose("ps", "--quiet", service, output=True))
    data = run("docker", "inspect", "--format", "{{.Config.Image}} {{.Image}} {{.State.Running}}", container, output=True)
    tag, image_id, running = data.split()
    if running != "true":
        raise ValueError(f"{service} is stopped")
    if "@" in tag or tag.startswith("sha256:") or not image_id.startswith("sha256:"):
        raise RuntimeError(f"{service} requires a site-owned mutable image alias")
    return {"alias": tag, "old": image_id}


def build(service, target, live):
    tag = f"rag-release-{service}:{target}"
    directory = SOURCE / ("apps/confluence_connector" if service == "connector" else f"apps/{service}")
    args = ["docker", "build", "-t", tag]
    if service in {"api", "worker"}:
        args += ["-f", directory / "Dockerfile.clean"]
    elif service == "web":
        base = "rag-delivery-site-web:base"
        if subprocess.run(["docker", "image", "inspect", base], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            run("docker", "tag", live["old"], base)
        args += ["-f", SOURCE / "deploy/delivery/Dockerfile.web", "--build-arg", f"SITE_WEB_IMAGE={base}"]
    run(*args, directory)
    checks = {
        "api": ("python", "-c", "from app.main import app; assert app.routes"),
        "worker": ("python", "-c", "from app.document_processing import parse_document; "
                   "assert parse_document('check.txt', b'ready')"),
        "web": ("sh", "-c", "test -r /usr/share/nginx/html/index.html && "
                "test -r /usr/share/nginx/html/runtime-config.js"),
        "connector": ("python", "-c", "from confluence_connector import runner"),
    }
    entrypoint, *command = checks[service]
    run("docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
        "--user", "10001:10001" if service == "connector" else "1000:1000",
        "--entrypoint", entrypoint, tag, *command)
    return image(tag)


def restart(components):
    # nginx resolves the API address at startup.
    services = [s for s in ("api", "worker", "web") if s in components or (s == "web" and "api" in components)]
    if services:
        compose("up", "-d", "--no-build", "--no-deps", "--pull", "never", "--force-recreate", *services)
    if "connector" in components:
        run("systemctl", "restart", "rag-confluence-worker.service")


def verify(components, state_key):
    base = os.environ["RAG_WEB_URL"].rstrip("/")
    args = ["curl", "--proto", "=https", "--silent", "--show-error", "--max-time", "5"]
    if ca := os.environ.get("RAG_CA_FILE"):
        args.extend(["--cacert", ca])
    for _ in range(40):
        try:
            health = json.loads(run(*args, "--fail", base + "/api/v1/health", output=True))
            protected = run(*args, "-o", "/dev/null", "-w", "%{http_code}", base + "/api/v1/workspaces", output=True)
            if health.get("status") == "ok" and protected == "401" and all(
                current(s)["old"] == values[state_key] for s, values in components.items()
            ):
                return
        except (subprocess.CalledProcessError, ValueError):
            pass
        time.sleep(3)
    raise RuntimeError("RAG health, access control or running image verification failed")


def backup_if_migrating(api, target):
    current_heads = compose("exec", "-T", "api", "alembic", "current", output=True)
    next_heads = run("docker", "run", "--rm", "--network", "none", api["new"], "alembic", "heads", output=True)
    def heads(value):
        return set(re.findall(r"^([a-zA-Z0-9_]+)(?:\s|$)", value, re.MULTILINE))

    if heads(current_heads) and heads(current_heads) == heads(next_heads):
        return
    path = STATE / f"before-{target}.dump"
    temp = path.with_suffix(".partial")
    with temp.open("wb") as db_backup:
        compose_cmd = ["docker", "exec", "-i", compose("ps", "--quiet", "postgres", output=True),
                       "sh", "-c", 'exec pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"']
        run(*compose_cmd, stdout=db_backup)
    if temp.stat().st_size == 0:
        raise RuntimeError("Empty database backup")
    temp.replace(path)


def switch(images, key):
    for value in images.values():
        run("docker", "tag", value[key], value["alias"])
    restart(images)
    verify(images, key)


def restore(transaction):
    print("Recovering previous running images", flush=True)
    switch(transaction["images"], "old")
    save(STATE / "failed.json", transaction["revision"])
    (STATE / "pending.json").unlink()


def main():
    pending = STATE / "pending.json"
    applied = STATE / "applied.json"
    failed = STATE / "failed.json"
    if pending.exists():
        old = json.loads(pending.read_text())
        if applied.exists() and json.loads(applied.read_text()) == old["revision"]:
            pending.unlink()
        else:
            restore(old)
    target = get_target()
    previous = json.loads(applied.read_text()) if applied.exists() else None
    if not target or target == previous or (failed.exists() and json.loads(failed.read_text()) == target):
        return
    if previous:
        git("merge-base", "--is-ancestor", previous, target)
    git("checkout", "--quiet", "--detach", target)
    paths = git("diff", "--name-only", previous, target, "--").splitlines() if previous else None
    selected = [s for s in COMPONENTS if paths is None or any(p.startswith(SOURCES[s]) for p in paths)]
    print(f"Publishing {target} ({', '.join(selected) or 'source changes only'})", flush=True)
    images = {}
    for service in selected:
        live = current(service)
        images[service] = {**live, "new": build(service, target, live)}
    if "api" in images:
        backup_if_migrating(images["api"], target)
    if images:
        transaction = {"revision": target, "images": images}
        save(pending, transaction)
        try:
            switch(images, "new")
        except Exception:
            restore(transaction)
            raise
    save(applied, target)
    pending.unlink(missing_ok=True)
    failed.unlink(missing_ok=True)
    print(f"Applied {target}", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / "publish.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        main()
