"""OpenAPI extension — exposes OpenAPI 3.x operations as ARC skills.

Core defines ``ExtensionContract``; this *implementation* is a package.
When enabled, the extension fetches one or more OpenAPI specs and
registers each operation as an ARC skill named
``openapi::<spec>::<operationId>``. A caller invokes the operation through
the normal skill path; the skill builds the HTTP request (path/query/body
from inputs), injects auth from an env var, calls it, and returns the JSON.

**Multi-spec:** like the mcp extension's ext-apps, several specs can be
configured at once; operations are namespaced by spec name.

THREAT MODEL: the spec URL and every operation's server URL are fetched /
called over HTTP. To avoid turning this into an SSRF primitive, both are
checked against ``_host_is_private`` (loopback / private / link-local
hosts are rejected) unless ``allow_private_hosts`` is set on the spec —
an explicit opt-in for talking to an internal service on purpose.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from arc.contracts.extension import ExtensionContract

logger = logging.getLogger(__name__)


def _parse_specs(config: dict) -> list[dict]:
    """Normalise config into a list of spec dicts.

    Accepts ``specs: [{name, url, auth_env, allow_private_hosts}, …]`` or a
    single ``spec_url`` shortcut. Returns ``[]`` when nothing configured.
    """
    specs = config.get("specs")
    if isinstance(specs, list) and specs:
        out = []
        for i, spec in enumerate(specs):
            if isinstance(spec, dict) and spec.get("url"):
                spec = dict(spec)
                spec.setdefault("name", spec.get("name") or f"spec{i}")
                out.append(spec)
        return out
    if config.get("spec_url"):
        return [{
            "name": config.get("name", "default"),
            "url": config["spec_url"],
            "auth_env": config.get("auth_env"),
            "allow_private_hosts": bool(config.get("allow_private_hosts")),
        }]
    return []


def _host_allowed(url: str, allow_private: bool) -> bool:
    """Reject private/loopback hosts unless explicitly allowed (SSRF guard)."""
    if allow_private:
        return True
    from urllib.parse import urlparse
    from arc.api.security import _host_is_private
    host = urlparse(url).hostname or ""
    return not _host_is_private(host)


class _OpenApiSkill:
    """ARC skill wrapping one OpenAPI operation."""

    def __init__(self, *, spec_name: str, operation_id: str, method: str,
                 url_template: str, auth_env: str | None, description: str,
                 allow_private: bool):
        self.name = f"openapi::{spec_name}::{operation_id}"
        self.description = description or f"{method.upper()} {url_template}"
        self._method = method.upper()
        self._url_template = url_template
        self._auth_env = auth_env
        self._allow_private = allow_private

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        import asyncio
        return await asyncio.to_thread(self._call, dict(inputs or {}))

    def _call(self, inputs: dict) -> dict[str, Any]:
        import requests
        # Path params: substitute {name} from inputs, the rest become query
        # params (or a JSON body for write methods).
        url = self._url_template
        remaining = dict(inputs)
        for key in list(remaining):
            placeholder = "{" + key + "}"
            if placeholder in url:
                url = url.replace(placeholder, str(remaining.pop(key)))

        if not _host_allowed(url, self._allow_private):
            return {"skill": self.name, "ok": False,
                    "error": f"refusing to call private host in {url}"}

        headers = {}
        if self._auth_env:
            token = os.environ.get(self._auth_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        try:
            if self._method in ("GET", "DELETE", "HEAD"):
                resp = requests.request(self._method, url, params=remaining,
                                        headers=headers, timeout=30)
            else:
                resp = requests.request(self._method, url, json=remaining,
                                        headers=headers, timeout=30)
        except Exception as exc:  # noqa: BLE001
            logger.debug("openapi call %s failed: %s", self.name, exc)
            return {"skill": self.name, "ok": False, "error": str(exc)}

        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:2000]
        return {"skill": self.name, "ok": resp.ok, "status": resp.status_code,
                "body": body}


class OpenApiExtension(ExtensionContract):
    name = "openapi"

    def __init__(self) -> None:
        self._skill_names: list[str] = []

    async def initialize(self, config: dict, registry: Any = None) -> None:
        specs = _parse_specs(config or {})
        if not specs:
            logger.info("openapi extension enabled but no specs configured — idle.")
            return
        if registry is None:
            logger.warning("openapi extension has no registry to register into — idle.")
            return
        import asyncio
        for spec in specs:
            try:
                await asyncio.to_thread(self._register_spec, spec, registry)
            except Exception as exc:  # noqa: BLE001 — one bad spec shouldn't kill the rest
                logger.warning("openapi spec %r failed: %s", spec.get("name"), exc)
        logger.info("openapi extension ready: %d skill(s) registered.", len(self._skill_names))

    def _register_spec(self, spec: dict, registry) -> None:
        import requests
        allow_private = bool(spec.get("allow_private_hosts"))
        if not _host_allowed(spec["url"], allow_private):
            raise ValueError(f"refusing to fetch spec from private host: {spec['url']}")
        doc = requests.get(spec["url"], timeout=30).json()

        # Resolve the base server URL (first servers entry, else spec URL origin).
        servers = doc.get("servers") or []
        if servers and isinstance(servers[0], dict) and servers[0].get("url"):
            base = servers[0]["url"].rstrip("/")
        else:
            from urllib.parse import urlparse
            p = urlparse(spec["url"])
            base = f"{p.scheme}://{p.netloc}"

        spec_name = spec["name"]
        auth_env = spec.get("auth_env")
        for path, path_item in (doc.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, op in path_item.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete", "head"):
                    continue
                if not isinstance(op, dict):
                    continue
                op_id = op.get("operationId") or f"{method}_{path.strip('/').replace('/', '_')}"
                skill = _OpenApiSkill(
                    spec_name=spec_name,
                    operation_id=op_id,
                    method=method,
                    url_template=base + path,
                    auth_env=auth_env,
                    description=op.get("summary") or op.get("description") or "",
                    allow_private=allow_private,
                )
                registry.register_skill(skill.name, skill)
                self._skill_names.append(skill.name)

    async def shutdown(self) -> None:
        self._skill_names.clear()
