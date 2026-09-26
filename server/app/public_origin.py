"""The single authoritative MAS origin when public deployment is enabled."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from fastapi import Request


def configured_public_origin() -> str | None:
    value = os.getenv("MAS_PUBLIC_ORIGIN", "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or not re.fullmatch(r"[a-z0-9.-]+", hostname)
        or value != f"https://{hostname}"
    ):
        raise RuntimeError("MAS_PUBLIC_ORIGIN must be a bare HTTPS DNS origin")
    return value


def agent_resource_url(request: Request, resource_path: str) -> str:
    route_url = request.url_for("agent_resource", resource_path=resource_path)
    public_origin = configured_public_origin()
    return f"{public_origin}{route_url.path}" if public_origin else str(route_url)
