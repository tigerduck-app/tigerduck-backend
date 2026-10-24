"""Which monitored hosts the watcher may connect to."""

from __future__ import annotations

import socket

import pytest

from server.tls_pins.targets import (
    TargetRefused,
    address_allowed,
    check_target,
    resolve,
    under_domains,
)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("www.ntust.edu.tw", ("www.ntust.edu.tw", 443)),
        ("mail.ntust.edu.tw:993", ("mail.ntust.edu.tw", 993)),
        ("mail.ntust.edu.tw:465", ("mail.ntust.edu.tw", 465)),
    ],
)
def test_a_host_on_a_tls_port_is_allowed(target, expected) -> None:
    assert check_target(target) == expected


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("140.118.1.1", "an IP address"),
        ("140.118.1.1:443", "an IP address"),
        ("[::1]:443", "not a host name"),
        ("localhost", "not a host name"),
        ("https://www.ntust.edu.tw/", "not host or host:port"),
        ("www.ntust.edu.tw:8443", "port must be"),
        ("www.ntust.edu.tw:80", "port must be"),
        ("Www.ntust.edu.tw", "not a host name"),
    ],
)
def test_an_ip_literal_a_bad_name_or_another_port_is_refused(target, reason) -> None:
    with pytest.raises(TargetRefused, match=reason):
        check_target(target)


def test_a_host_must_sit_under_its_sets_domains() -> None:
    subdomains = [{"name": "ntust.edu.tw", "include_subdomains": True}]
    exact = [{"name": "api.lib.ntust.edu.tw", "include_subdomains": False}]

    assert under_domains("ntust.edu.tw", subdomains)
    assert under_domains("moodle2.ntust.edu.tw", subdomains)
    assert not under_domains("evilntust.edu.tw", subdomains)
    assert not under_domains("ntust.edu.tw.example", subdomains)
    assert under_domains("api.lib.ntust.edu.tw", exact)
    assert not under_domains("x.api.lib.ntust.edu.tw", exact)


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.7", "127.0.0.1", "169.254.169.254", "100.64.0.1", "192.168.1.1",
        "::1", "fe80::1%en0", "fd00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.7",
        "64:ff9b::a00:7", "64:ff9b::808:808",
    ],
)
def test_non_global_and_nat64_addresses_are_refused(address) -> None:
    assert not address_allowed(address)


def test_public_addresses_are_allowed() -> None:
    assert address_allowed("140.118.31.99")
    assert address_allowed("2001:4860:4860::8888")


def _answer(monkeypatch, *addresses: str) -> None:
    def getaddrinfo(host, port, *args, **kwargs):
        if not addresses:
            raise socket.gaierror("no such host")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_resolve_returns_the_addresses_to_connect_to(monkeypatch) -> None:
    _answer(monkeypatch, "140.118.31.99", "140.118.31.99", "2001:db8:1::1")

    with pytest.raises(TargetRefused):
        resolve("www.ntust.edu.tw", 443)  # 2001:db8::/32 is documentation space

    _answer(monkeypatch, "140.118.31.99")
    assert resolve("www.ntust.edu.tw", 443) == [("140.118.31.99", 443)]


@pytest.mark.parametrize("addresses", [("10.0.0.7",), ("140.118.31.99", "64:ff9b::a00:7"), ()])
def test_resolve_refuses_a_name_with_any_non_global_address_or_none(
    monkeypatch, addresses
) -> None:
    _answer(monkeypatch, *addresses)

    with pytest.raises(TargetRefused):
        resolve("www.ntust.edu.tw", 443)
