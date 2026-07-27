"""Cross-module identity defaults: metrics RED labels and OTEL resources stay aligned."""

from __future__ import annotations

from fastapi_observability import _identity as ident
from fastapi_observability import _resource as r
from fastapi_observability import metrics as m
from fastapi_observability import tracing as t


def test_identity_defaults_are_single_source():
    assert ident.DEFAULT_SERVICE_NAME == "app"  # neutral OSS default
    assert ident.DEFAULT_ENV == "prod"
    assert ident.DEFAULT_VERSION == "unknown"
    # Public metrics surface re-exports the same function object.
    assert m.identity_labels is ident.identity_labels
    # Tracing service fallback shares the same constant (OTEL_SERVICE_NAME still wins).
    assert t.DEFAULT_SERVICE_NAME is ident.DEFAULT_SERVICE_NAME


def test_resource_attributes_use_shared_env_version_defaults(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setenv("HOSTNAME", "host-a")

    attrs = r.build_resource_attributes("svc-from-arg")
    service, env, version = m.identity_labels()

    assert service == ident.DEFAULT_SERVICE_NAME
    assert attrs["service.name"] == "svc-from-arg"
    assert attrs["deployment.environment"] == env == ident.DEFAULT_ENV
    assert attrs["service.version"] == version == ident.DEFAULT_VERSION


def test_identity_labels_env_override_matches_resource(monkeypatch):
    monkeypatch.setenv("SERVICE", "billing-api")
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.setenv("APP_VERSION", "2.0.0")
    monkeypatch.setenv("HOSTNAME", "host-b")

    service, env, version = m.identity_labels()
    attrs = r.build_resource_attributes(service)

    assert (service, env, version) == ("billing-api", "staging", "2.0.0")
    assert attrs["service.name"] == "billing-api"
    assert attrs["deployment.environment"] == "staging"
    assert attrs["service.version"] == "2.0.0"


def test_identity_labels_cache_freezes_until_reset(monkeypatch):
    """First call freezes env identity; reset re-reads (tests / rare reconfig)."""
    monkeypatch.setenv("SERVICE", "first-svc")
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("APP_VERSION", "1.0.0")
    assert m.identity_labels() == ("first-svc", "dev", "1.0.0")

    monkeypatch.setenv("SERVICE", "second-svc")
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("APP_VERSION", "2.0.0")
    # Still frozen from first read.
    assert m.identity_labels() == ("first-svc", "dev", "1.0.0")

    ident.reset_identity_labels()
    assert m.identity_labels() == ("second-svc", "prod", "2.0.0")
