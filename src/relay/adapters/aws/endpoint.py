"""Shared AWS endpoint override for local-mock runs.

When ``RELAY_AWS_ENDPOINT_URL`` is set (e.g. to a DynamoDB-Local or LocalStack
endpoint), every boto3 DynamoDB resource/client in Relay routes there instead of
real AWS. This is the seam that makes ``RELAY_RUNTIME=local-mock`` work fully
offline (collapsed-single-container plan §6): one env var, no code branches in
the stores.

Usage::

    session.resource("dynamodb", **aws_resource_kwargs(region))

Returns an empty dict when no override is set, so production behaviour is
unchanged.
"""

from __future__ import annotations

import os
from typing import Any

_ENDPOINT_ENV = "RELAY_AWS_ENDPOINT_URL"


def aws_endpoint_url() -> str | None:
    """Return the configured local AWS endpoint URL, or None for real AWS."""
    val = os.environ.get(_ENDPOINT_ENV, "").strip()
    return val or None


def aws_resource_kwargs(region: str | None = None) -> dict[str, Any]:
    """boto3 resource/client kwargs: endpoint_url + region when overriding.

    DynamoDB-Local also needs *some* region + credentials; the docker-compose
    harness sets dummy AWS_* env vars, and we default the region here so a bare
    ``local-mock`` run without AWS_REGION still resolves an endpoint.
    """
    endpoint = aws_endpoint_url()
    if not endpoint:
        return {"region_name": region} if region else {}
    return {
        "endpoint_url": endpoint,
        "region_name": region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1",
    }
