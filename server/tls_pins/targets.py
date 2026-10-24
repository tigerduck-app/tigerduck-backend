"""Which hosts the watcher may connect to: names under their set's domains,
on the TLS ports the apps use, resolving to public addresses only. Checked
when the portal saves a host and again before each probe."""

from __future__ import annotations

import ipaddress
import re
import socket

ALLOWED_PORTS = frozenset({443, 465, 993})
_HOST_NAME = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
# NAT64, 6to4 and Teredo reach IPv4 addresses, private ones included.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class TargetRefused(ValueError):
    pass


def parse_target(target: str) -> tuple[str, int]:
    """"host" or "host:port"; port 443 when left out."""
    host, sep, port = target.rpartition(":")
    return (host, int(port)) if sep else (target, 443)


def check_target(target: str) -> tuple[str, int]:
    """`target` as (host, port) when it names a host on an allowed port."""
    try:
        host, port = parse_target(target)
    except ValueError:
        raise TargetRefused("not host or host:port") from None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise TargetRefused("an IP address, not a host name")
    if not _HOST_NAME.fullmatch(host):
        raise TargetRefused("not a host name")
    if port not in ALLOWED_PORTS:
        raise TargetRefused("port must be 443, 465 or 993")
    return host, port


def under_domains(host: str, domains: list[dict]) -> bool:
    return any(
        host == d["name"] or (d.get("include_subdomains") and host.endswith("." + d["name"]))
        for d in domains
    )


def address_allowed(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64 or ip.sixtofour is not None or ip.teredo is not None:
            return False
        ip = ip.ipv4_mapped or ip
    return ip.is_global


def resolve(host: str, port: int) -> list[tuple[str, int]]:
    """The addresses to connect to for `host`; refused when it does not
    resolve or any of its addresses is not public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise TargetRefused("does not resolve") from None
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    refused = [a for a in addresses if not address_allowed(a)]
    if refused or not addresses:
        raise TargetRefused(f"resolves to a non-global address ({', '.join(refused)})")
    return [(address, port) for address in addresses]
