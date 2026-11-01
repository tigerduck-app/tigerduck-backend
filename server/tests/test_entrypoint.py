"""uvicorn reads no proxy header itself: the app takes X-Forwarded-For only
from the addresses `forwarded_allow_ips` names, which docker-compose.yml
sets for production."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_uvicorn_leaves_the_forwarded_headers_to_the_app() -> None:
    entrypoint = (REPO_ROOT / "entrypoint.sh").read_text()
    assert "--no-proxy-headers" in entrypoint
    assert "--forwarded-allow-ips" not in entrypoint


def test_production_trusts_only_the_proxies_on_its_docker_networks() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    value = compose["services"]["backend"]["environment"]["TIGERDUCK_FORWARDED_ALLOW_IPS"]
    # uvicorn keeps an entry it cannot parse as a name, which no address equals.
    networks = [ipaddress.ip_network(item.strip()) for item in value.split(",")]
    assert networks
    # nginx-proxy-manager already resolves the client from Cloudflare, so a
    # public range would only let an address the client wrote through.
    assert all(network.is_private for network in networks), value
