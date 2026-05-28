"""Authenticate the chat to sim2l catalog + results services.

Both services keep their own session table and require an
``X-Session-ID`` header on every write. The chat used to push without
one — getting 401s that were silently swallowed.

This module provides:

  * :class:`ServiceAuth` — a small immutable record of what was learned
    (per-service session ids + the username we logged in as).
  * :func:`login_to_services` — fires POST /session/login on each
    configured service URL and returns a populated ``ServiceAuth``.
  * :func:`resolve_credentials` — reads username/password from env vars
    so the CLI doesn't have to thread them everywhere.

Failures don't raise — every helper returns a ``ServiceAuth`` whose
``errors`` list explains what went wrong. The chat layer decides
whether to continue without persistence or abort.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional


logger = logging.getLogger(__name__)


_DEFAULT_CATALOG_URL = "http://localhost:8002"
_DEFAULT_RESULTS_URL = "http://localhost:8003"
_DEFAULT_CACHE_URL = "http://localhost:8001"

# Sim2l ships with a default admin account when bootstrap_admin runs.
# We default to that for the chat — same trust boundary as the user's
# local services. Override via env if the deployment uses different creds.
_DEFAULT_USERNAME = "admin"
_DEFAULT_PASSWORD = "admin"

_LOGIN_TIMEOUT = 5  # seconds — services are local, anything slower is broken


@dataclass(frozen=True)
class ServiceAuth:
    """Per-service session ids + diagnostics.

    A field that's ``None`` means the login for that service didn't
    succeed; check ``errors`` for the reason. The chat treats partial
    success as "best effort": pushes to the services we DID log into,
    skips the ones we didn't.
    """
    username: Optional[str] = None
    catalog_session: Optional[str] = None
    results_session: Optional[str] = None
    cache_session: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    @property
    def authenticated(self) -> bool:
        """True when at least the catalog AND results sessions are set.

        These two are the ones the runtime adapter actually pushes to;
        the cache session is a bonus for catalog-reuse lookups.
        """
        return bool(self.catalog_session and self.results_session)

    @property
    def partial(self) -> bool:
        """True when SOME services authenticated but not all.

        The chat surfaces this distinctly from ``authenticated`` so the
        user can decide whether to continue with reduced persistence.
        """
        any_ok = bool(self.catalog_session or self.results_session or self.cache_session)
        return any_ok and not self.authenticated


def resolve_credentials(
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Read sim2l credentials from env vars when not supplied.

    Lookup order: explicit args, ``SIM2L_USERNAME`` / ``SIM2L_PASSWORD``,
    finally the documented sim2l default ``admin / admin``.
    """
    u = username or os.environ.get("SIM2L_USERNAME") or _DEFAULT_USERNAME
    p = password or os.environ.get("SIM2L_PASSWORD") or _DEFAULT_PASSWORD
    return u, p


def _service_url(env_var: str, default: str) -> str:
    return os.environ.get(env_var, default).rstrip("/")


def _login_one(
    base_url: str,
    username: str,
    password: str,
    *,
    service_label: str,
    timeout: float = _LOGIN_TIMEOUT,
) -> tuple[Optional[str], Optional[str]]:
    """POST to <base_url>/session/login. Returns (session_id, error_msg).

    Either session_id is set (success) or error_msg is set (failure).
    Never raises.
    """
    import requests
    try:
        resp = requests.post(
            f"{base_url}/session/login",
            json={"username": username, "password": password},
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError:
        return None, f"{service_label} at {base_url} is not reachable"
    except requests.exceptions.Timeout:
        return None, f"{service_label} at {base_url} did not respond in {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return None, f"{service_label} request failed: {type(exc).__name__}"

    if resp.status_code == 200:
        try:
            sid = resp.json().get("session_id")
        except ValueError:
            return None, f"{service_label} returned non-JSON payload"
        if not sid:
            return None, f"{service_label} response missing 'session_id'"
        return sid, None

    if resp.status_code == 401:
        return None, f"{service_label} rejected credentials (401)"
    if resp.status_code == 429:
        return None, f"{service_label} rate-limited login (429)"
    return None, f"{service_label} login returned HTTP {resp.status_code}"


def login_to_services(
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    catalog_url: Optional[str] = None,
    results_url: Optional[str] = None,
    cache_url: Optional[str] = None,
    timeout: float = _LOGIN_TIMEOUT,
) -> ServiceAuth:
    """Authenticate to catalog + results (and best-effort cache).

    Returns a :class:`ServiceAuth` describing the outcome. Never raises.

    URL parameters default to ``$SIM2L_*_URL`` env vars then the
    documented localhost ports. The trailing slash (if any) is
    stripped so callers can concatenate paths safely.
    """
    u, p = resolve_credentials(username, password)
    cat_url = (catalog_url or _service_url("SIM2L_CATALOG_URL", _DEFAULT_CATALOG_URL)).rstrip("/")
    res_url = (results_url or _service_url("SIM2L_RESULTS_URL", _DEFAULT_RESULTS_URL)).rstrip("/")
    cac_url = (cache_url or _service_url("SIM2L_CACHE_URL", _DEFAULT_CACHE_URL)).rstrip("/")

    errors: list[str] = []
    cat_sid, err = _login_one(cat_url, u, p, service_label="catalog", timeout=timeout)
    if err:
        errors.append(err)
    res_sid, err = _login_one(res_url, u, p, service_label="results", timeout=timeout)
    if err:
        errors.append(err)
    cac_sid, err = _login_one(cac_url, u, p, service_label="cache", timeout=timeout)
    if err:
        errors.append(err)

    return ServiceAuth(
        username=u,
        catalog_session=cat_sid,
        results_session=res_sid,
        cache_session=cac_sid,
        errors=errors,
    )
