"""Unit tests for OTEL resource attribute enrichment (no real network)."""

from __future__ import annotations

import json
import socket
import sys
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest
from fastapi import FastAPI

from fastapi_observability import _resource as r
from fastapi_observability import tracing as t
from fastapi_observability.adapters import ecs


@pytest.fixture(autouse=True)
def _clear_ecs_env(monkeypatch):
    """Default each test to a non-ECS environment unless it opts in."""
    for key in (
        "ECS_CONTAINER_METADATA_URI_V4",
        "ECS_CONTAINER_METADATA_URI",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "HOSTNAME",
        "APP_VERSION",
        "ENV",
        "SERVICE",
        "OTEL_SERVICE_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    r.clear_resource_enrichers()


def test_process_runtime_attributes_always_present():
    attrs = r.process_runtime_attributes()
    assert attrs["process.runtime.name"] == sys.implementation.name
    assert attrs["process.runtime.version"]
    assert attrs["process.runtime.description"] == sys.version
    assert "process.pid" not in attrs
    assert "process.command" not in attrs


def test_task_id_from_arn_uses_last_segment_only():
    arn = (
        "arn:aws:ecs:us-west-2:123456789012:task/"
        "my-cluster/abcdef0123456789abcdef0123456789"
    )
    assert ecs.task_id_from_arn(arn) == "abcdef0123456789abcdef0123456789"
    # Must not return the account id segment.
    assert ecs.task_id_from_arn(arn) != "123456789012"
    assert ecs.task_id_from_arn("") is None
    assert ecs.task_id_from_arn("simple-id") == "simple-id"


def test_region_from_availability_zone():
    assert ecs.region_from_availability_zone("us-west-2a") == "us-west-2"
    assert ecs.region_from_availability_zone("eu-central-1b") == "eu-central-1"
    assert ecs.region_from_availability_zone("") is None


def test_build_resource_attributes_local_no_cloud(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "local-dev-host")
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    monkeypatch.setenv("ENV", "dev")

    attrs = r.build_resource_attributes("svc-a")

    assert attrs["service.name"] == "svc-a"
    assert attrs["service.version"] == "9.9.9"
    assert attrs["deployment.environment"] == "dev"
    assert attrs["service.instance.id"] == "local-dev-host"
    assert attrs["process.runtime.name"]
    assert attrs["process.runtime.version"]
    assert attrs["process.runtime.description"]
    assert "cloud.provider" not in attrs
    assert "cloud.platform" not in attrs
    assert "cloud.region" not in attrs
    assert "cloud.account.id" not in attrs


def test_build_resource_attributes_hostname_fallback(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "socket-host.local")

    attrs = r.build_resource_attributes("svc-b")
    assert attrs["service.instance.id"] == "socket-host.local"
    assert "cloud.account.id" not in attrs


def test_build_resource_attributes_ecs_with_metadata(monkeypatch):
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/meta"
    )
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    task_arn = (
        "arn:aws:ecs:us-west-2:123456789012:task/"
        "cluster/taskiddeadbeefcafebabe0123456789ab"
    )
    meta = {
        "TaskARN": task_arn,
        "AvailabilityZone": "us-west-2c",
    }
    monkeypatch.setattr(ecs, "fetch_ecs_task_metadata", lambda: meta)

    attrs = r.build_resource_attributes("demo-api")

    assert attrs["cloud.provider"] == "aws"
    assert attrs["cloud.platform"] == "aws_ecs"
    assert attrs["cloud.region"] == "us-west-2"
    assert attrs["service.instance.id"] == "taskiddeadbeefcafebabe0123456789ab"
    assert "cloud.account.id" not in attrs
    # Full ARN / account segment must never appear as resource values we set.
    assert "123456789012" not in attrs.values()
    assert task_arn not in attrs.values()


def test_aws_region_wins_over_az(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://meta/v4")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setattr(
        ecs,
        "fetch_ecs_task_metadata",
        lambda: {
            "TaskARN": "arn:aws:ecs:eu-west-1:999:task/c/tid",
            "AvailabilityZone": "us-west-2a",
        },
    )
    attrs = r.build_resource_attributes("svc")
    assert attrs["cloud.region"] == "eu-west-1"


def test_region_from_az_when_env_missing(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://meta/v4")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setattr(
        ecs,
        "fetch_ecs_task_metadata",
        lambda: {
            "TaskARN": "arn:aws:ecs:us-east-1:1:task/c/tid99",
            "AvailabilityZone": "us-east-1a",
        },
    )
    attrs = r.build_resource_attributes("svc")
    assert attrs["cloud.region"] == "us-east-1"
    assert attrs["service.instance.id"] == "tid99"


def test_ecs_uri_present_but_metadata_fetch_fails(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://meta/v4")
    monkeypatch.setenv("HOSTNAME", "fallback-host")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.setattr(ecs, "fetch_ecs_task_metadata", lambda: None)

    attrs = r.build_resource_attributes("svc")

    assert attrs["cloud.provider"] == "aws"
    assert attrs["cloud.platform"] == "aws_ecs"
    assert attrs["cloud.region"] == "ap-southeast-2"
    assert attrs["service.instance.id"] == "fallback-host"
    assert "cloud.account.id" not in attrs


def test_fetch_ecs_task_metadata_fail_open(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://meta/v4")

    def boom(*_a, **_k):
        raise URLError("connection refused")

    monkeypatch.setattr(ecs, "urlopen", boom)
    assert ecs.fetch_ecs_task_metadata() is None


def test_fetch_ecs_task_metadata_parses_json(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://meta/v4/")
    body = json.dumps(
        {"TaskARN": "arn:aws:ecs:us-west-2:1:task/c/abc123"}
    ).encode("utf-8")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    monkeypatch.setattr(ecs, "urlopen", lambda *_a, **_k: _Resp())
    meta = ecs.fetch_ecs_task_metadata()
    assert meta is not None
    assert meta["TaskARN"].endswith("/abc123")


def test_enrichers_disabled_with_empty_sequence(monkeypatch):
    """Pass enrichers=() to skip auto ECS detection entirely."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://meta/v4")
    monkeypatch.setenv("HOSTNAME", "local-only")
    attrs = r.build_resource_attributes("svc", enrichers=())
    assert "cloud.provider" not in attrs
    assert attrs["service.instance.id"] == "local-only"


def test_custom_resource_enricher(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "host-x")

    def k8s_like() -> dict:
        return {"cloud.provider": "gcp", "service.instance.id": "pod-abc"}

    attrs = r.build_resource_attributes("svc", enrichers=[k8s_like])
    assert attrs["cloud.provider"] == "gcp"
    assert attrs["service.instance.id"] == "pod-abc"


def test_setup_tracing_passes_enriched_resource_attrs(monkeypatch):
    """setup_tracing wires build_resource_attributes into Resource.create."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("HOSTNAME", "trace-host")
    monkeypatch.setattr(t, "OTLPSpanExporter", lambda *a, **k: MagicMock())
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI", raising=False)

    captured: dict = {}
    real_create = t.Resource.create

    def capture_create(attributes=None, schema_url=None):
        if attributes:
            captured.update(dict(attributes))
        return real_create(attributes, schema_url)

    monkeypatch.setattr(t.Resource, "create", staticmethod(capture_create))

    app = FastAPI()
    t.shutdown_tracing()
    t._tracer_provider = None
    assert t.setup_tracing(app) is True
    try:
        assert captured.get("service.instance.id") == "trace-host"
        assert captured.get("process.runtime.name")
        assert "cloud.account.id" not in captured
        assert "cloud.provider" not in captured
    finally:
        t.shutdown_tracing()
