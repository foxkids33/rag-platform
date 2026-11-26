"""Provision personal Confluence for the site override consumed by rag-delivery."""
import argparse
import base64
import fcntl
import json
import os
import secrets
import ssl
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit


SITE = Path("/etc/rag-delivery")
TRANSPORT = Path("/var/lib/rag-confluence-transport")
SOCKET_DIR = "/run/rag-confluence-transport"
KEY_PATH = "/run/secrets/confluence_personal_key"
CA_PATH = "/run/secrets/confluence_personal_ca.pem"


def publish(path, data, *, gid=None, mode=0o600):
    with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        name = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            if gid is not None:
                os.fchown(temp.fileno(), -1, gid)
            os.fchmod(temp.fileno(), mode)
            os.fsync(temp.fileno())
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


def environment(service):
    entries = service.setdefault("environment", {})
    if isinstance(entries, list):
        # Compose supports KEY, KEY= and KEY=a=b in the list form.
        mapped = {}
        for entry in entries:
            key, equals, value = entry.partition("=")
            mapped[key] = value if equals else None
        service["environment"] = mapped
    return service["environment"]


def add_mount(volumes, source, target):
    for entry in volumes:
        if isinstance(entry, str):
            fields = entry.split(":")
            destination = fields[0] if len(fields) == 1 else fields[1]
            if destination != target:
                continue
            if len(fields) == 3 and fields[:2] == [str(source), target] and "ro" in fields[2].split(","):
                return
        elif entry.get("target") != target:
            continue
        elif entry.get("type") == "bind" and entry.get("source") == str(source) and entry.get("read_only") is True:
            return
        raise ValueError(f"Existing mount conflicts with {target}; inspect the site override")
    volumes.append({"type": "bind", "source": str(source), "target": target, "read_only": True,
                    "bind": {"create_host_path": False, "selinux": "z"}})


def provision(site, host, transport, gid, ca_file=None):
    origin = urlsplit(host)
    if (origin.scheme != "https" or not origin.hostname or origin.port not in (443, None)
            or origin.username is not None or origin.password is not None or origin.query or origin.fragment):
        raise ValueError("Confluence requires a fixed HTTPS origin on port 443")
    if not transport.is_absolute() or not transport.is_dir():
        raise ValueError("The VPN transport directory must exist")
    secure = site / "secure.json"
    original = secure.read_bytes()
    config = json.loads(original)
    api = config["services"].setdefault("api", {})
    env = environment(api)
    key_configured = bool(env.get("CONFLUENCE_PERSONAL_KEY_FILE"))
    volumes = api.setdefault("volumes", [])
    key = site / "confluence-personal.key"
    desired = {"CONFLUENCE_BASE_URL": host.rstrip("/"), "CONFLUENCE_DEPLOYMENT": "data_center",
               "CONFLUENCE_AUTH_MODE": "bearer", "CONFLUENCE_PERSONAL_SOCKET": f"{SOCKET_DIR}/confluence.sock",
               "CONFLUENCE_PERSONAL_KEY_FILE": KEY_PATH}
    add_mount(volumes, transport, SOCKET_DIR)
    add_mount(volumes, key, KEY_PATH)
    if ca_file:
        if not ca_file.is_absolute() or b"PRIVATE KEY" in ca_file.read_bytes():
            raise ValueError("Supply an absolute path to a public Confluence CA bundle")
        ssl.create_default_context(cafile=str(ca_file))
        add_mount(volumes, ca_file, CA_PATH)
        desired["CONFLUENCE_PERSONAL_CA_FILE"] = CA_PATH
    for name, value in desired.items():
        if env.get(name) not in (None, "", value):
            raise ValueError(f"Existing {name} differs; inspect the site override")
        env[name] = value

    if key.exists():
        info = key.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_gid != gid or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o440
                or len(base64.b64decode(key.read_bytes().strip(), altchars=b"-_", validate=True)) != 32):
            raise ValueError("Existing personal key has unexpected permissions or format; preserve and inspect it")
    elif key_configured:
        raise ValueError("The configured personal key is missing; restore it from backup")
    else:
        publish(key, base64.urlsafe_b64encode(secrets.token_bytes(32)) + b"\n", gid=gid, mode=0o440)
    if json.loads(original) != config:
        backup = site / "secure.before-personal.json"
        if not backup.exists():
            publish(backup, original)
        publish(secure, (json.dumps(config, indent=2) + "\n").encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-gid", type=int, default=1000)
    parser.add_argument("--ca-file", type=Path)
    args = parser.parse_args()
    if os.geteuid():
        parser.error("Run as the owner of the delivery configuration (root)")
    with Path("/var/lib/rag-delivery/publish.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        provision(SITE, args.base_url, TRANSPORT, args.api_gid, args.ca_file)


if __name__ == "__main__":
    main()
