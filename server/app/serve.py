"""Start Uvicorn with only the Docker host gateway trusted as a proxy."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import uvicorn

from app.public_origin import configured_public_origin


def default_ipv4_gateway(route_table: Path = Path("/proc/net/route")) -> str:
    for line in route_table.read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        if not int(fields[3], 16) & 0x2:
            continue
        gateway = ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, "little"))
        if gateway.is_unspecified or gateway.is_loopback:
            break
        return str(gateway)
    raise RuntimeError("no usable Docker IPv4 gateway for MAS proxy trust")


def main() -> None:
    trusted_peer = default_ipv4_gateway() if configured_public_origin() else "127.0.0.1"
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=trusted_peer,
    )


if __name__ == "__main__":
    main()
