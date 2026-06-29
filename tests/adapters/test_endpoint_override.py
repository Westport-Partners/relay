"""Tests for the local-mock AWS endpoint override (collapse Step 6)."""

from __future__ import annotations

from relay.adapters.aws.endpoint import aws_endpoint_url, aws_resource_kwargs


def test_no_override_returns_none(monkeypatch):
    monkeypatch.delenv("RELAY_AWS_ENDPOINT_URL", raising=False)
    assert aws_endpoint_url() is None


def test_blank_override_is_none(monkeypatch):
    monkeypatch.setenv("RELAY_AWS_ENDPOINT_URL", "   ")
    assert aws_endpoint_url() is None


def test_resource_kwargs_empty_without_override(monkeypatch):
    monkeypatch.delenv("RELAY_AWS_ENDPOINT_URL", raising=False)
    assert aws_resource_kwargs() == {}


def test_resource_kwargs_passes_region_without_override(monkeypatch):
    monkeypatch.delenv("RELAY_AWS_ENDPOINT_URL", raising=False)
    assert aws_resource_kwargs("eu-west-1") == {"region_name": "eu-west-1"}


def test_resource_kwargs_with_override(monkeypatch):
    monkeypatch.setenv("RELAY_AWS_ENDPOINT_URL", "http://localhost:8000")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    kw = aws_resource_kwargs()
    assert kw["endpoint_url"] == "http://localhost:8000"
    assert kw["region_name"] == "us-east-1"


def test_resource_kwargs_override_defaults_region(monkeypatch):
    monkeypatch.setenv("RELAY_AWS_ENDPOINT_URL", "http://localhost:8000")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert aws_resource_kwargs()["region_name"] == "us-east-1"
