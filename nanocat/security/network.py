"""Pure network-target classification helpers used by the security policy."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _is_local_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return any(address in network for network in _LOCAL_NETWORKS)


def is_local_url(url: str) -> bool:
    """Return True only when a URL is confirmed to target a local network.

    Invalid URLs and unresolved hostnames return False so callers can apply an
    innocent-assumption policy instead of blocking ambiguous input.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").rstrip(".").casefold()
    except ValueError:
        return False
    if not hostname:
        return False
    if hostname in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return _is_local_ip(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except (IndexError, ValueError):
            continue
    return bool(addresses) and all(_is_local_ip(address) for address in addresses)
