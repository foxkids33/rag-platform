"""Manage only the RAG VPN container. Invoked by its systemd unit on the host."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

NAME = "rag-confluence-vpn"
IMAGE = os.environ.get("RAG_VPN_IMAGE", "rag-confluence-vpn:local")
LABEL = "ru.skala.rag.confluence-vpn"
NETWORK = os.environ.get("RAG_DOCKER_NETWORK", "rag_default")
ROOT = Path(__file__).resolve().parent
DISPLAY_DIR = Path("/run/rag-confluence-vpn/display")


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def owned():
    ids = docker("ps", "-aq", "--no-trunc", "--filter", f"name=^/{NAME}$").stdout.split()
    if not ids:
        return None
    info = json.loads(docker("inspect", ids[0]).stdout)[0]
    if (info["Name"] != "/" + NAME or info["Config"]["Labels"].get(LABEL) != "1"
            or info["Config"]["Image"] != IMAGE):
        raise RuntimeError("Container name is in use by another installation")
    return info


def stop():
    info = owned()
    if info:
        docker("rm", "-f", info["Id"])


def replace_files_inside():
    # Called only after entering this container's private mount namespace and root.
    for name in ("resolv.conf", "hosts"):
        path = Path("/etc") / name
        data = path.read_bytes()
        if len(data) > 65536 or (name == "resolv.conf" and b"nameserver" not in data):
            raise RuntimeError(f"Unexpected initial contents of {path}")
        fd, tmp = tempfile.mkstemp(dir="/etc", prefix=".rag-dns-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                os.fchmod(fd, 0o644)
            subprocess.run(["/bin/umount", "--", str(path)], check=True)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    Path("/run/rag-dns-ready").touch(mode=0o600)


def prepare_dns(cid):
    info = owned()
    if (not info or info["Id"] != cid or not info["State"]["Running"]
            or info["HostConfig"]["Privileged"] or info["HostConfig"]["NetworkMode"] != NETWORK):
        raise RuntimeError("Unexpected VPN container identity or isolation")
    pid = str(info["State"]["Pid"])
    proc = Path("/proc") / pid
    for ns in ("mnt", "net"):
        if os.readlink(proc / "ns" / ns) == os.readlink(Path("/proc/self/ns") / ns):
            raise RuntimeError("A host namespace was detected")
    mounts = [line.split(" - ", 1) for line in (proc / "mountinfo").read_text().splitlines()]
    for path in ("/", "/etc/resolv.conf", "/etc/hosts"):
        entries = [(a.split(), b) for a, b in mounts if a.split()[4] == path]
        if len(entries) != 1:
            raise RuntimeError(f"Unexpected mount layout for {path}")
        fields, fs = entries[0]
        if any(f.startswith(("shared:", "master:", "propagate_from:")) for f in fields[6:]):
            raise RuntimeError(f"Mount propagation enabled on {path}")
        if path == "/":
            if not fs.startswith("overlay "):
                raise RuntimeError("Expected a private Docker overlay")
        elif not fields[3].endswith(f"/{cid}/{Path(path).name}"):
            raise RuntimeError(f"Unexpected Docker mount for {path}")
    # Give Cisco ordinary files in the container; the host configuration never moves.
    subprocess.run(["nsenter", "--target", pid, "--mount", "--net", "--pid", "--root", "--wd",
                    "--", "/usr/bin/python3", "/opt/rag-vpn/host.py", "prepare-files"],
                   check=True, timeout=12)


def main():
    stop()
    DISPLAY_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chown(DISPLAY_DIR, 1000, 1000)
    (DISPLAY_DIR / "login.sock").unlink(missing_ok=True)
    cmd = ["run", "--rm", "-d", "--init", "--name", NAME, "--label", f"{LABEL}=1",
           "--env", "RAG_VPN_GATEWAY", "--hostname", NAME, "--network", NETWORK, "--device", "/dev/net/tun",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "1g",
           "--cpus", "1.25", "--pids-limit", "240", "--shm-size", "160m", "--log-driver", "none",
           "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=96m",
           "--tmpfs", "/home/vpnuser:rw,nosuid,nodev,uid=1000,gid=1000,mode=0700,size=160m",
           "-v", f"{DISPLAY_DIR}:/run/rag-display:rw,Z",
           "-v", f"{ROOT}:/opt/rag-vpn:ro,z"]
    for capability in ("NET_ADMIN", "NET_RAW", "SETUID", "SETGID"):
        cmd.extend(["--cap-add", capability])
    cid = docker(*cmd, "--entrypoint", "/usr/bin/python3", IMAGE, "/opt/rag-vpn/client.py").stdout.strip()
    try:
        prepare_dns(cid)
        result = docker("wait", cid)
        if result.stdout.strip() != "0":
            raise RuntimeError("The VPN container exited")
    finally:
        if (info := owned()) and info["Id"] == cid:
            docker("rm", "-f", cid, check=False)


if __name__ == "__main__":
    if sys.argv[1:] == ["prepare-files"]:
        replace_files_inside()
    elif sys.argv[1:] == ["stop"]:
        stop()
    elif len(sys.argv) == 1:
        main()
    else:
        sys.exit("Unknown command")
