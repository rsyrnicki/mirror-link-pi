from __future__ import annotations

import json

import pytest

from mlpi import netinfo


def _runner_for(payload):
    def runner(_argv):
        return json.dumps(payload)
    return runner


def test_interface_address_returns_first_ipv4():
    payload = [{
        "ifname": "usb0",
        "addr_info": [
            {"family": "inet6", "local": "fe80::1"},
            {"family": "inet", "local": "192.168.7.2", "prefixlen": 24},
        ],
    }]
    assert netinfo.interface_address("usb0", runner=_runner_for(payload)) == "192.168.7.2"


def test_interface_address_raises_when_missing():
    with pytest.raises(netinfo.InterfaceNotFoundError):
        netinfo.interface_address("usb0", runner=_runner_for([]))


def test_interface_address_raises_when_no_ipv4():
    payload = [{"ifname": "usb0", "addr_info": [{"family": "inet6", "local": "fe80::1"}]}]
    with pytest.raises(netinfo.InterfaceNotFoundError):
        netinfo.interface_address("usb0", runner=_runner_for(payload))


def test_list_interfaces_skips_loopback_and_v6_only():
    payload = [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
        {"ifname": "wlp0s1", "addr_info": [{"family": "inet", "local": "10.0.0.5"}]},
        {"ifname": "v6only", "addr_info": [{"family": "inet6", "local": "fe80::1"}]},
    ]
    assert netinfo.list_interfaces(runner=_runner_for(payload)) == ["wlp0s1"]
