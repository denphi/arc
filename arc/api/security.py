"""Security helpers for ARC's FastAPI surface.

Review item #T4: ``/provider/models`` previously took a caller-supplied
``base_url`` and dispatched ``OpenWebUIProvider(base_url=...)`` against it
without authentication. That was an unauthenticated SSRF primitive — any
client that could reach the API could probe internal services or AWS
metadata via the OpenWebUI provider's HTTP client.

We harden the surface in three layers:

1. **Bearer token (optional, opt-in).** Set ``ARC_API_TOKEN`` or
   ``[api].token`` in ``arc.toml`` to require ``Authorization: Bearer
   <token>`` on protected routes. When unset, requests pass through — the
   default-open behaviour preserves the existing dev-loop ergonomics.
2. **base_url allowlist (always on for ``openwebui``).** ``/provider/models``
   only forwards to URLs whose origin matches an entry in
   ``[api].provider_base_url_allowlist`` (or ``ARC_PROVIDER_ALLOWLIST``,
   comma-separated). The default allowlist contains only the upstream
   ``OpenWebUIProvider`` default so out-of-the-box deployments don't open
   an SSRF.
3. **Loopback/private-IP block.** Even when no allowlist is configured we
   reject base URLs that resolve to ``localhost``/``127.0.0.0/8``/private
   ranges/link-local — there's no legitimate reason for the API to fetch
   models from such hosts on behalf of a remote caller.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable
from urllib.parse import urlparse

from fastapi import Header, HTTPException

from arc.core.config import load_arc_toml

# Default allowlist matches the bundled OpenWebUIProvider default. Deployments
# that point at other gateways must opt-in explicitly.
_DEFAULT_ALLOWLIST = ("https://genai.rcac.purdue.edu",)


@dataclass(frozen=True)
class SecurityConfig:
    """Resolved security knobs for the ARC API."""

    token: str | None = None
    base_url_allowlist: tuple[str, ...] = field(default_factory=tuple)
    allow_private_hosts: bool = False


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip().rstrip("/") for item in value.split(",") if item.strip())


@lru_cache(maxsize=1)
def load_security_config() -> SecurityConfig:
    """Read security configuration from ``arc.toml`` + environment.

    Environment variables override config-file values so operators can flip
    settings without editing the bundled TOML. The result is cached for the
    process lifetime; tests can call ``load_security_config.cache_clear()``
    to reset.
    """
    _, config = load_arc_toml()
    api_cfg = config.get("api", {}) if isinstance(config, dict) else {}

    token = os.environ.get("ARC_API_TOKEN") or api_cfg.get("token") or None

    allowlist_env = _split_csv(os.environ.get("ARC_PROVIDER_ALLOWLIST"))
    allowlist_cfg = tuple(
        str(item).rstrip("/")
        for item in (api_cfg.get("provider_base_url_allowlist") or [])
        if isinstance(item, str) and item.strip()
    )
    if allowlist_env:
        allowlist = allowlist_env
    elif allowlist_cfg:
        allowlist = allowlist_cfg
    else:
        allowlist = _DEFAULT_ALLOWLIST

    allow_private = bool(api_cfg.get("allow_private_provider_hosts", False))
    if os.environ.get("ARC_ALLOW_PRIVATE_PROVIDER_HOSTS", "").lower() in {"1", "true", "yes"}:
        allow_private = True

    return SecurityConfig(
        token=token,
        base_url_allowlist=allowlist,
        allow_private_hosts=allow_private,
    )


def tokens_match(supplied: str | None, expected: str | None) -> bool:
    """Constant-time bearer-token comparison.

    ``==`` on a token short-circuits at the first differing byte, so response
    timing leaks a prefix-match oracle to anyone who can send requests. Shared
    with the UI's SSE dependency so both gates compare the same way.
    """
    if not supplied or not expected:
        return False
    return secrets.compare_digest(supplied, expected)


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: enforce the bearer token when one is configured.

    When ``ARC_API_TOKEN``/``[api].token`` is unset we let the request
    through. This keeps the default-open posture but lets operators flip a
    single switch to lock the surface down.
    """
    cfg = load_security_config()
    if not cfg.token:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization required")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not tokens_match(parts[1], cfg.token):
        # 401 even on bad scheme; clients shouldn't be probing the difference.
        raise HTTPException(status_code=401, detail="Invalid credentials")


def _host_is_private(host: str) -> bool:
    """Return True when ``host`` resolves to a loopback/private/link-local IP."""
    try:
        addr_info = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable — treat as private/unsafe; we won't make an outbound
        # call to a name the local resolver can't even map.
        return True
    for entry in addr_info:
        ip = entry[4][0]
        try:
            addr = ipaddress.ip_address(ip.split("%", 1)[0])
        except ValueError:
            return True
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            return True
    return False


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def validate_provider_base_url(base_url: str, allowlist: Iterable[str] | None = None) -> str:
    """Validate ``base_url`` against the allowlist + private-host policy.

    Returns the cleaned URL (trailing slash stripped) so callers can pass it
    straight to the provider. Raises ``HTTPException`` on rejection so it
    can be used directly from a FastAPI route.
    """
    cfg = load_security_config()
    candidates = tuple(allowlist) if allowlist is not None else cfg.base_url_allowlist

    if not isinstance(base_url, str) or not base_url.strip():
        raise HTTPException(status_code=400, detail="base_url is required")

    cleaned = base_url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Unsupported base_url scheme")

    # Reject embedded credentials — they're a common pivot ("@evil.tld")
    # against naive URL parsing.
    if "@" in (parsed.netloc or ""):
        raise HTTPException(status_code=400, detail="base_url must not embed credentials")

    if candidates:
        request_origin = _origin(cleaned)
        for allowed in candidates:
            allowed_origin = _origin(allowed)
            if request_origin == allowed_origin:
                break
            # Tolerate the case where the allowlist omits an explicit port.
            if (
                request_origin[0] == allowed_origin[0]
                and request_origin[1] == allowed_origin[1]
                and allowed_origin[2] is None
            ):
                break
        else:
            raise HTTPException(
                status_code=400,
                detail="base_url is not in the configured allowlist",
            )

    if not cfg.allow_private_hosts and _host_is_private(parsed.hostname):
        raise HTTPException(
            status_code=400,
            detail="base_url resolves to a non-public host",
        )

    return cleaned
