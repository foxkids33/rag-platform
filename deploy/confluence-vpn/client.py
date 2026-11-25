"""Run the interactive Cisco client and its private display on the connector server."""

import os
import pathlib
import pwd
import secrets
import subprocess
import sys
import time
from xml.etree import ElementTree as ET

BIN = pathlib.Path("/opt/cisco/secureclient/bin")
USER = pwd.getpwnam("vpnuser")
HOME = pathlib.Path(USER.pw_dir)
RUNTIME = HOME / "runtime"
USER_ENV = {
    **os.environ,
    "HOME": str(HOME),
    "XDG_RUNTIME_DIR": str(RUNTIME),
    "DISPLAY": ":99",
    "XAUTHORITY": str(RUNTIME / "Xauthority"),
    "NO_AT_BRIDGE": "1",
    "LIBGL_ALWAYS_SOFTWARE": "1",
}
AS_USER = dict(user=USER.pw_uid, group=USER.pw_gid, extra_groups=[], env=USER_ENV)


def initialize_user():
    os.umask(0o077)
    RUNTIME.mkdir(mode=0o700)
    auth = pathlib.Path(USER_ENV["XAUTHORITY"])
    auth.touch(mode=0o600)
    subprocess.run(["xauth", "-q", "-f", str(auth)],
                   input=f"add :99 MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n",
                   text=True, check=True)
    prefs = ET.Element("AnyConnectPreferences")
    ET.SubElement(prefs, "DefaultHostName").text = os.environ["RAG_VPN_GATEWAY"]
    ET.ElementTree(prefs).write(HOME / ".anyconnect", encoding="utf-8")


def initialize():
    # Docker prepares its per-container DNS files before releasing this gate.
    for _ in range(200):
        if pathlib.Path("/run/rag-dns-ready").exists():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Container DNS initialization did not finish")

    subprocess.run([sys.executable, __file__, "init-user"], **AS_USER, check=True)
    xdir = pathlib.Path("/tmp/.X11-unix")
    xdir.mkdir(exist_ok=True)
    xdir.chmod(0o1777)
    pathlib.Path("/run/dbus").mkdir(exist_ok=True)
    subprocess.run(["dbus-daemon", "--system", "--fork", "--nopidfile"], check=True)


def start_ui():
    return subprocess.Popen(["dbus-run-session", "--", "sh", "-c",
                             "openbox >/dev/null 2>&1 & exec /opt/cisco/secureclient/bin/vpnui"],
                            **AS_USER)


def main():
    initialize()
    display = subprocess.Popen([
        "Xtigervnc", ":99", "-geometry", "1100x740", "-depth", "24",
        "-auth", USER_ENV["XAUTHORITY"], "-nolisten", "tcp", "-rfbport", "-1",
        "-rfbunixpath", "/run/rag-display/login.sock", "-rfbunixmode", "384",
        "-SecurityTypes", "None", "-AcceptCutText=0", "-SendCutText=0",
        "-desktop", "Cisco Secure Client — Confluence",
    ], **AS_USER)
    for _ in range(50):
        if display.poll() is not None:
            raise RuntimeError("Private display exited")
        if subprocess.run(["xdpyinfo"], **AS_USER, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("Private display did not start")
    agent = subprocess.Popen([BIN / "vpnagentd", "-execv_instance"])
    for _ in range(40):
        if agent.poll() is not None:
            raise RuntimeError("Cisco VPN agent exited")
        if pathlib.Path("/run/vpnagentd.pid").is_file():
            break
        time.sleep(0.2)
    ui = start_ui()
    while display.poll() is None and agent.poll() is None:
        time.sleep(3)
        if ui.poll() is not None:
            ui = start_ui()
    raise RuntimeError("Cisco agent or private display stopped")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["init-user"]:
            initialize_user()
        else:
            main()
    except Exception as exc:
        print(f"VPN startup: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
