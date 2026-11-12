"""Unit checks only: these tests never execute ip or change network state."""

import importlib.util
from pathlib import Path

import pytest


HOOK_PATH = Path(__file__).resolve().parents[1] / "apps/confluence_vpn/route_hook.py"
SPEC = importlib.util.spec_from_file_location("confluence_vpn_route_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def environment(**overrides):
    return {
        "RAG_VPN_ISOLATED": "1",
        "TUNDEV": "rag-vpn0",
        "reason": "connect",
        "INTERNAL_IP4_ADDRESS": "10.32.64.100",
        # Malicious/broad server-provided network values must be ignored.
        "INTERNAL_IP4_NETMASK": "0.0.0.0",
        "INTERNAL_IP4_DNS": "192.0.2.131 192.0.2.132",
        "CISCO_SPLIT_INC": "1",
        "CISCO_SPLIT_INC_0_ADDR": "0.0.0.0",
        "CISCO_SPLIT_INC_0_MASKLEN": "0",
        **overrides,
    }


@pytest.mark.parametrize("reason", ["connect", "reconnect"])
def test_connect_changes_only_tun_and_single_target(reason):
    commands = []
    hook.configure(environment(reason=reason), commands.append)
    assert commands == [
        ("link", "set", "dev", "rag-vpn0", "mtu", "1340", "up"),
        ("-4", "address", "flush", "dev", "rag-vpn0", "scope", "global"),
        ("-4", "address", "add", "10.32.64.100/32", "dev", "rag-vpn0"),
        ("-4", "route", "replace", "192.0.2.104/32", "dev", "rag-vpn0", "src", "10.32.64.100"),
    ]


@pytest.mark.parametrize("reason", ["pre-init", "attempt-reconnect"])
def test_lifecycle_noops_do_not_need_an_assigned_address(reason):
    commands = []
    hook.configure(environment(reason=reason, INTERNAL_IP4_ADDRESS=""), commands.append)
    assert commands == []


def test_disconnect_cleanup_is_limited_to_its_interface():
    commands = []
    hook.configure(environment(reason="disconnect", INTERNAL_IP4_ADDRESS=""), commands.append)
    assert commands == [
        ("-4", "route", "del", "192.0.2.104/32", "dev", "rag-vpn0"),
        ("-4", "address", "flush", "dev", "rag-vpn0", "scope", "global"),
    ]


def test_uses_valid_server_mtu():
    commands = []
    hook.configure(environment(INTERNAL_IP4_MTU="1412"), commands.append)
    assert commands[0] == ("link", "set", "dev", "rag-vpn0", "mtu", "1412", "up")


@pytest.mark.parametrize("overrides", [
    {"RAG_VPN_ISOLATED": ""},
    {"TUNDEV": "eth0"},
    {"TUNDEV": "rag-vpn0; echo unsafe"},
    {"reason": "anything-else"},
    {"INTERNAL_IP4_ADDRESS": "10.0.0.1/8"},
    {"INTERNAL_IP4_ADDRESS": "::1"},
    {"INTERNAL_IP4_ADDRESS": "127.0.0.1"},
    {"INTERNAL_IP4_ADDRESS": "0.0.0.0"},
    {"INTERNAL_IP4_ADDRESS": "224.0.0.1"},
    {"INTERNAL_IP4_ADDRESS": "255.255.255.255"},
    {"INTERNAL_IP4_MTU": "575"},
    {"INTERNAL_IP4_MTU": "9001"},
    {"INTERNAL_IP4_MTU": "1400; unsafe"},
    {"INTERNAL_IP4_MTU": "١٤٠٠"},
])
def test_invalid_configuration_makes_no_network_changes(overrides):
    commands = []
    with pytest.raises(hook.HookError):
        hook.configure(environment(**overrides), commands.append)
    assert commands == []
