"""Tests for arc.api.security (review item #T4)."""

import pytest
from fastapi import HTTPException

from arc.api.security import (
    load_security_config,
    require_api_token,
    validate_provider_base_url,
)


def test_token_pass_through_when_unset():
    # No token configured → dependency must be a no-op.
    require_api_token(authorization=None)
    require_api_token(authorization="Bearer anything")


def test_token_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("ARC_API_TOKEN", "s3cret")
    load_security_config.cache_clear()
    with pytest.raises(HTTPException) as exc:
        require_api_token(authorization=None)
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        require_api_token(authorization="Basic abc")
    with pytest.raises(HTTPException):
        require_api_token(authorization="Bearer wrong")
    require_api_token(authorization="Bearer s3cret")


def test_base_url_allowlist_default_blocks_arbitrary_hosts(monkeypatch):
    load_security_config.cache_clear()
    with pytest.raises(HTTPException) as exc:
        validate_provider_base_url("https://attacker.example.com/api")
    assert exc.value.status_code == 400


def test_base_url_allowlist_accepts_configured_origin(monkeypatch):
    monkeypatch.setenv("ARC_PROVIDER_ALLOWLIST", "https://gateway.example.com")
    load_security_config.cache_clear()
    # Stub the private-host probe so the test is deterministic offline.
    monkeypatch.setattr("arc.api.security._host_is_private", lambda host: False)
    cleaned = validate_provider_base_url("https://gateway.example.com/api/")
    assert cleaned == "https://gateway.example.com/api"


def test_base_url_rejects_loopback(monkeypatch):
    monkeypatch.setenv("ARC_PROVIDER_ALLOWLIST", "http://127.0.0.1:8000")
    load_security_config.cache_clear()
    with pytest.raises(HTTPException) as exc:
        validate_provider_base_url("http://127.0.0.1:8000/api")
    # Allowlist matched, but private-host policy rejected it.
    assert exc.value.status_code == 400
    assert "non-public" in exc.value.detail


def test_base_url_loopback_allowed_when_opted_in(monkeypatch):
    monkeypatch.setenv("ARC_PROVIDER_ALLOWLIST", "http://127.0.0.1:8000")
    monkeypatch.setenv("ARC_ALLOW_PRIVATE_PROVIDER_HOSTS", "1")
    load_security_config.cache_clear()
    cleaned = validate_provider_base_url("http://127.0.0.1:8000/api")
    assert cleaned == "http://127.0.0.1:8000/api"


def test_base_url_rejects_userinfo(monkeypatch):
    monkeypatch.setenv("ARC_PROVIDER_ALLOWLIST", "https://gateway.example.com")
    load_security_config.cache_clear()
    monkeypatch.setattr("arc.api.security._host_is_private", lambda host: False)
    with pytest.raises(HTTPException):
        validate_provider_base_url("https://user:pw@gateway.example.com/api")


def test_base_url_requires_http_scheme(monkeypatch):
    monkeypatch.setenv("ARC_PROVIDER_ALLOWLIST", "https://gateway.example.com")
    load_security_config.cache_clear()
    with pytest.raises(HTTPException):
        validate_provider_base_url("ftp://gateway.example.com/api")
    with pytest.raises(HTTPException):
        validate_provider_base_url("")
