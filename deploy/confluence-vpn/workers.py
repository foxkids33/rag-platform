"""Keep the connector containers on the same DNS configuration as Cisco."""

import os
from pathlib import Path
import subprocess
import time

from host import ROOT, owned

RESOLVER = Path("/run/rag-confluence-vpn/connector-resolv.conf")


def sync_resolver(source):
    data = source.read_bytes()
    if not 0 < len(data) <= 65536 or not any(
        l.strip().startswith(b"nameserver ") for l in data.splitlines()
    ):
        raise RuntimeError("Cisco resolver configuration is unavailable")
    if RESOLVER.exists() and RESOLVER.read_bytes() == data:
        return
    # Docker bind-mounts this inode; update it in place across VPN reconnects.
    fd = os.open(RESOLVER, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "r+b") as dst:
        dst.write(data)
        dst.truncate()
        dst.flush()


def main():
    for _ in range(80):
        info = owned()
        if info and info["State"]["Running"]:
            proc = Path("/proc") / str(info["State"]["Pid"]) / "root"
            if (proc / "run/rag-dns-ready").exists():
                break
        time.sleep(0.25)
    else:
        raise RuntimeError("VPN container did not finish starting")
    source = proc / "etc/resolv.conf"
    sync_resolver(source)
    compose = ["docker", "compose", "-f", str(ROOT / "worker.compose.yaml")]
    process = subprocess.Popen(compose + ["up", "--no-build", "--pull", "never",
                                          "--force-recreate", "--abort-on-container-exit"])
    try:
        while process.poll() is None:
            sync_resolver(source)
            time.sleep(2)
        return process.returncode
    finally:
        if process.poll() is None:
            subprocess.run(compose + ["down", "--timeout", "8"], check=False)
            process.wait(timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
