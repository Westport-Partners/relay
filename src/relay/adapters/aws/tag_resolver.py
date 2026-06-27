"""relay.adapters.aws.tag_resolver — best-effort alarm resource tag resolution.

CloudWatch Alarm State Change events delivered via EventBridge carry NO resource
tags — the payload contains only alarm configuration and state metadata. This
module closes that gap by fetching tags on the Node side, where the team's IAM
grants access to their own account's resources.

Two tag sources, merged (resource-first, alarm second):
  1. Resource tags pulled from the monitored AWS resource itself (Lambda, SQS,
     ECS service, …).  These carry the shop's canonical tagging taxonomy
     (COMPONENT_ID, GIT_SHA, GITLAB_PIPELINE_URL, relay:*) and WIN on any key
     conflict with alarm tags — resource tags are the ground truth.
  2. Alarm tags from cloudwatch.list_tags_for_resource() keyed on the alarm ARN.
     Added via setdefault so they fill in keys the resource tags did not supply.

Everything here is best-effort: each fetch runs inside its own try/except and
logs a warning on failure before continuing.  A complete outage (no IAM perms,
throttle, network error) degrades gracefully to an empty tag dict — it never
breaks alarm processing.

Enable / disable with RELAY_RESOLVE_ALARM_TAGS (default: true).  The Node's IAM
already allows cloudwatch:ListTagsForResource on the node_stack.py baseline; the
resource-level calls (lambda:ListTags, sqs:ListQueueTags, ecs:ListTagsForResource)
require additional perms granted when the flag is true.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _lambda_resource(dims: dict[str, str], region: str, account: str) -> Callable[[Any], dict[str, str]] | None:
    """Resolver for AWS/Lambda metrics — tags the Lambda function directly."""
    name = dims.get("FunctionName")
    if not name:
        return None
    arn = f"arn:aws:lambda:{region}:{account}:function:{name}"

    def fetch(session: Any) -> dict[str, str]:
        return session.client("lambda", region_name=region).list_tags(Resource=arn).get("Tags", {}) or {}

    return fetch


def _sqs_resource(dims: dict[str, str], region: str, account: str) -> Callable[[Any], dict[str, str]] | None:
    """Resolver for AWS/SQS metrics — tags the SQS queue directly."""
    name = dims.get("QueueName")
    if not name:
        return None
    url = f"https://sqs.{region}.amazonaws.com/{account}/{name}"

    def fetch(session: Any) -> dict[str, str]:
        return session.client("sqs", region_name=region).list_queue_tags(QueueUrl=url).get("Tags", {}) or {}

    return fetch


def _ecs_resource(dims: dict[str, str], region: str, account: str) -> Callable[[Any], dict[str, str]] | None:
    """Resolver for AWS/ECS metrics — tags the ECS service directly."""
    cluster = dims.get("ClusterName")
    service = dims.get("ServiceName")
    if not (cluster and service):
        return None
    arn = f"arn:aws:ecs:{region}:{account}:service/{cluster}/{service}"

    def fetch(session: Any) -> dict[str, str]:
        return {
            t["key"]: t.get("value", "")
            for t in session.client("ecs", region_name=region)
            .list_tags_for_resource(resourceArn=arn)
            .get("tags", [])
        }

    return fetch


def _ec2_resource(dims: dict[str, str], region: str, account: str) -> Callable[[Any], dict[str, str]] | None:
    """Resolver for AWS/EC2 metrics — tags the EC2 instance directly."""
    instance_id = dims.get("InstanceId")
    if not instance_id:
        return None

    def fetch(session: Any) -> dict[str, str]:
        resp = session.client("ec2", region_name=region).describe_tags(
            Filters=[{"Name": "resource-id", "Values": [instance_id]}]
        )
        return {t["Key"]: t.get("Value", "") for t in resp.get("Tags", [])}

    return fetch


_RESOURCE_RESOLVERS = {
    "AWS/Lambda": _lambda_resource,
    "AWS/SQS": _sqs_resource,
    "AWS/ECS": _ecs_resource,
    "AWS/EC2": _ec2_resource,
}


class AlarmTagResolver:
    """Fetches and merges resource + alarm tags for a CloudWatch alarm.

    Args:
        account_id: The team's AWS account ID (used to build resource ARNs).
        region: AWS region for all service calls.
        enabled: When False, all tag resolution is skipped and ``{}`` is
            returned immediately. Driven by RELAY_RESOLVE_ALARM_TAGS (default
            true — degrades gracefully when IAM perms are absent).
        boto3_session: Optional injected session for tests.
    """

    def __init__(
        self,
        account_id: str = "",
        region: str | None = None,
        enabled: bool | None = None,
        boto3_session: Any | None = None,
    ) -> None:
        self._account_id = account_id
        self._region = region or os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION"
        )
        if enabled is None:
            enabled = os.environ.get("RELAY_RESOLVE_ALARM_TAGS", "true").lower() in (
                "1",
                "true",
                "yes",
            )
        self._enabled = enabled
        self._session = boto3_session
        # Warm-Lambda cache keyed by alarm ARN so repeated invocations for the
        # same alarm (e.g. flapping) do not make redundant API calls.
        self._cache: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, *, alarm_arn: str | None, detail: dict[str, Any]) -> dict[str, str]:
        """Fetch and merge tags for the given alarm. Never raises.

        Resource tags WIN on key conflict with alarm tags (resource-first
        design: the shop's canonical tags like COMPONENT_ID live on the
        resource, not the alarm).  Non-conflicting alarm tags are still
        included via setdefault.

        Args:
            alarm_arn: The CloudWatch alarm ARN (may be None for older alarms
                that predate ARN support).
            detail: The raw ``detail`` dict from the EventBridge event, used
                to extract metric dimensions for resource tag lookup.

        Returns:
            Merged tag dict, or ``{}`` if disabled or all fetches failed.
        """
        if not self._enabled:
            return {}

        # Return cached result for this alarm ARN (avoids repeated API calls
        # on warm Lambda invocations for the same flapping alarm).
        if alarm_arn and alarm_arn in self._cache:
            return self._cache[alarm_arn]

        tags: dict[str, str] = {}

        # Source 1 — Resource tags (WIN on conflict; applied first so setdefault
        # in source 2 cannot overwrite them).
        try:
            resource_tags = self._resource_tags(detail)
            tags.update(resource_tags)
        except Exception:
            logger.warning("resource tag fetch failed; continuing", exc_info=True)

        # Source 2 — Alarm tags (fill gaps only via setdefault).
        try:
            alarm_tags = self._alarm_tags(alarm_arn)
            for k, v in alarm_tags.items():
                tags.setdefault(k, v)
        except Exception:
            logger.warning("alarm tag fetch failed; continuing", exc_info=True)

        if alarm_arn:
            self._cache[alarm_arn] = tags
        return tags

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _alarm_tags(self, alarm_arn: str | None) -> dict[str, str]:
        """Fetch tags from CloudWatch for the alarm ARN itself."""
        if not alarm_arn:
            return {}
        import boto3  # local import: only when resolution is enabled

        session = self._session or boto3.session.Session()
        client = session.client("cloudwatch", region_name=self._region)
        resp = client.list_tags_for_resource(ResourceARN=alarm_arn)
        return {t["Key"]: t.get("Value", "") for t in resp.get("Tags", [])}

    def _resource_tags(self, detail: dict[str, Any]) -> dict[str, str]:
        """Fetch tags from the monitored AWS resource (Lambda, SQS, ECS, …)."""
        namespace: str = (
            detail.get("configuration", {})
            .get("metrics", [{}])[0]
            .get("metricStat", {})
            .get("metric", {})
            .get("namespace", "")
        )
        dims = self._extract_dimensions(detail)
        if not namespace or not dims:
            return {}

        resolver_fn = _RESOURCE_RESOLVERS.get(namespace)
        if resolver_fn is None:
            return {}

        fetch = resolver_fn(dims, self._region or "", self._account_id)
        if fetch is None:
            return {}

        import boto3  # local import: only when resolution is enabled

        session = self._session or boto3.session.Session()
        result: dict[str, str] = fetch(session)
        return result

    @staticmethod
    def _extract_dimensions(detail: dict[str, Any]) -> dict[str, str]:
        """Extract metric dimensions from the event detail as a flat dict.

        Iterates the metrics list and returns the first non-empty dimensions
        mapping it finds.  Returns ``{}`` if no dimensions are present.

        Real EventBridge "CloudWatch Alarm State Change" events carry
        ``dimensions`` as a JSON object (``{"FunctionName": "..."}``).  Some SDK
        shapes instead use a list of ``{"name": ..., "value": ...}`` objects.
        Both forms are handled.
        """
        for m in detail.get("configuration", {}).get("metrics", []) or []:
            dims = m.get("metricStat", {}).get("metric", {}).get("dimensions")
            if not dims:
                continue
            if isinstance(dims, dict):
                result = {str(k): str(v) for k, v in dims.items() if k}
            else:
                result = {
                    d["name"]: d.get("value", "")
                    for d in dims
                    if isinstance(d, dict) and d.get("name")
                }
            if result:
                return result
        return {}
