#!/usr/bin/env python3
"""Restricted OpenConnect network hook for an isolated Confluence VPN container.

The container launcher provides the security boundary: a separate network
namespace, NET_ADMIN only, no host networking. RAG_VPN_ISOLATED is an additional
guard against accidentally invoking this script on the host, not an alternative
to that isolation. VPN-provided DNS, default routes and split-network lists are
deliberately ignored. Only the fixed interface and Confluence /32 are managed.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Callable


INTERFACE = "rag-vpn0"
TARGET = "192.0.2.104/32"
DEFAULT_MTU = 1340
REASONS = {"pre-init", "connect", "reconnect", "attempt-reconnect", "disconnect"}
Runner = Callable[[Sequence[str]], None]


class HookError(Exception):
    """A safe, non-secret-bearing error suitable for operational logs."""


def _configuration(env: Mapping[str, str]) -> tuple[str, str | None, int | None]:
    if env.get("RAG_VPN_ISOLATED") != "1":
        raise HookError("isolated container marker is required")
    if env.get("TUNDEV") != INTERFACE:
        raise HookError("unexpected VPN interface")
    reason = env.get("reason", "")
    if reason not in REASONS:
        raise HookError("unsupported VPN hook reason")
    if reason not in {"connect", "reconnect"}:
        return reason, None, None

    try:
        address = ipaddress.IPv4Address(env.get("INTERNAL_IP4_ADDRESS", ""))
    except ipaddress.AddressValueError as exc:
        raise HookError("valid VPN IPv4 address is required") from exc
    if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_reserved:
        raise HookError("unusable VPN IPv4 address")

    raw_mtu = env.get("INTERNAL_IP4_MTU") or str(DEFAULT_MTU)
    if not raw_mtu.isascii() or not raw_mtu.isdecimal():
        raise HookError("invalid VPN MTU")
    mtu = int(raw_mtu)
    if not 576 <= mtu <= 9000:
        raise HookError("VPN MTU is outside the supported range")
    return reason, str(address), mtu


def configure(env: Mapping[str, str], run: Runner) -> None:
    """Apply validated commands; tests inject a runner without touching networking."""
    reason, address, mtu = _configuration(env)
    if reason in {"pre-init", "attempt-reconnect"}:
        return
    if reason == "disconnect":
        # Scope deletion to both our target and device, preserving a route to
        # the same destination on any other interface. Cleanup is best effort
        # if OpenConnect has already closed the TUN.
        run(("-4", "route", "del", TARGET, "dev", INTERFACE))
        run(("-4", "address", "flush", "dev", INTERFACE, "scope", "global"))
        return

    assert address is not None and mtu is not None
    run(("link", "set", "dev", INTERFACE, "mtu", str(mtu), "up"))
    # A reconnect may receive a different address. This is the private,
    # dedicated TUN: remove stale IPv4 addresses before installing the new /32.
    run(("-4", "address", "flush", "dev", INTERFACE, "scope", "global"))
    run(("-4", "address", "add", f"{address}/32", "dev", INTERFACE))
    run(("-4", "route", "replace", TARGET, "dev", INTERFACE, "src", address))


def main() -> int:
    ip_binary = shutil.which("ip", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if not ip_binary:
        print("Confluence VPN hook: iproute2 is unavailable", file=sys.stderr)
        return 1

    def run(arguments: Sequence[str]) -> None:
        result = subprocess.run(
            [ip_binary, *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if result.returncode:
            # A closed TUN has already had its addresses/routes removed by the
            # kernel. Treat disconnect cleanup as best effort only.
            if os.environ.get("reason") == "disconnect":
                return
            raise HookError("interface or route configuration failed")

    try:
        configure(os.environ, run)
    except (HookError, OSError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, HookError) else "network command failed"
        print(f"Confluence VPN hook: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
