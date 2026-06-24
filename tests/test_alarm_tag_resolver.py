"""Tests for AlarmTagResolver — alarm + resource tag resolution.

Uses injected fake boto3 sessions to avoid real AWS calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from relay.adapters.aws.tag_resolver import AlarmTagResolver

# ---------------------------------------------------------------------------
# Fake session helpers
# ---------------------------------------------------------------------------


class _FakeCloudWatchClient:
    def __init__(self, tags: list[dict]):
        self._tags = tags
        self.call_count = 0

    def list_tags_for_resource(self, **_kwargs):
        self.call_count += 1
        return {"Tags": self._tags}


class _FakeLambdaClient:
    def __init__(self, tags: dict):
        self._tags = tags

    def list_tags(self, **_kwargs):
        return {"Tags": self._tags}


class _FakeSQSClient:
    def __init__(self, tags: dict):
        self._tags = tags

    def list_queue_tags(self, **_kwargs):
        return {"Tags": self._tags}


class _FakeECSClient:
    def __init__(self, tags: list[dict]):
        self._tags = tags

    def list_tags_for_resource(self, **_kwargs):
        return {"tags": self._tags}


class _FakeEC2Client:
    def __init__(self, tags: list[dict]):
        self._tags = tags

    def describe_tags(self, **_kwargs):
        return {"Tags": self._tags}


class _FakeSession:
    """Minimal fake boto3 session that returns per-service clients."""

    def __init__(self, *, cw_tags=None, lambda_tags=None, sqs_tags=None, ecs_tags=None, ec2_tags=None):
        self._cw = _FakeCloudWatchClient(cw_tags or [])
        self._lambda = _FakeLambdaClient(lambda_tags or {})
        self._sqs = _FakeSQSClient(sqs_tags or {})
        self._ecs = _FakeECSClient(ecs_tags or [])
        self._ec2 = _FakeEC2Client(ec2_tags or [])

    def client(self, service: str, region_name: str = ""):
        if service == "cloudwatch":
            return self._cw
        if service == "lambda":
            return self._lambda
        if service == "sqs":
            return self._sqs
        if service == "ecs":
            return self._ecs
        if service == "ec2":
            return self._ec2
        raise ValueError(f"Unexpected service: {service}")


# ---------------------------------------------------------------------------
# Helpers for building event detail dicts
# ---------------------------------------------------------------------------


def _detail(namespace: str = "", dimensions: list[dict] | None = None) -> dict:
    dims = dimensions or []
    return {
        "configuration": {
            "metrics": [
                {
                    "metricStat": {
                        "metric": {
                            "namespace": namespace,
                            "dimensions": dims,
                        }
                    }
                }
            ]
        }
    }


def _lambda_detail(function_name: str = "my-func") -> dict:
    return _detail("AWS/Lambda", [{"name": "FunctionName", "value": function_name}])


def _sqs_detail(queue_name: str = "my-queue") -> dict:
    return _detail("AWS/SQS", [{"name": "QueueName", "value": queue_name}])


def _ecs_detail(cluster: str = "my-cluster", service: str = "my-svc") -> dict:
    return _detail(
        "AWS/ECS",
        [
            {"name": "ClusterName", "value": cluster},
            {"name": "ServiceName", "value": service},
        ],
    )


def _ec2_detail(instance_id: str = "i-0abc1234567890def") -> dict:
    return _detail("AWS/EC2", [{"name": "InstanceId", "value": instance_id}])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_disabled_returns_empty():
    resolver = AlarmTagResolver(enabled=False)
    result = resolver.resolve(alarm_arn="arn:aws:cloudwatch:us-east-1:123:alarm:x", detail=_detail())
    assert result == {}


def test_resource_tags_win_on_conflict():
    """Resource tags must overwrite alarm tags when keys collide."""
    session = _FakeSession(
        cw_tags=[{"Key": "COMPONENT_ID", "Value": "alarm-value"}, {"Key": "alarm-only", "Value": "a"}],
        lambda_tags={"COMPONENT_ID": "resource-value", "resource-only": "r"},
    )
    resolver = AlarmTagResolver(
        account_id="123456789012",
        region="us-east-1",
        boto3_session=session,
    )
    tags = resolver.resolve(
        alarm_arn="arn:aws:cloudwatch:us-east-1:123:alarm:fn-alarm",
        detail=_lambda_detail(),
    )
    # Resource tag wins for the conflicting key
    assert tags["COMPONENT_ID"] == "resource-value"
    # Non-conflicting alarm tag still present
    assert tags["alarm-only"] == "a"
    # Non-conflicting resource tag present
    assert tags["resource-only"] == "r"


def test_lambda_resolver_builds_arn_and_returns_tags():
    session = _FakeSession(lambda_tags={"relay:app": "my-app", "GIT_SHA": "abc123"})
    resolver = AlarmTagResolver(
        account_id="111122223333",
        region="us-west-2",
        boto3_session=session,
    )
    tags = resolver.resolve(alarm_arn=None, detail=_lambda_detail("payments-processor"))
    assert tags["relay:app"] == "my-app"
    assert tags["GIT_SHA"] == "abc123"


def test_sqs_resolver_returns_tags():
    session = _FakeSession(sqs_tags={"relay:app": "queue-app", "COMPONENT_ID": "sqs-comp"})
    resolver = AlarmTagResolver(
        account_id="111122223333",
        region="us-east-1",
        boto3_session=session,
    )
    tags = resolver.resolve(alarm_arn=None, detail=_sqs_detail("my-queue"))
    assert tags["relay:app"] == "queue-app"
    assert tags["COMPONENT_ID"] == "sqs-comp"


def test_ecs_resolver_returns_tags():
    """ECS list_tags_for_resource returns lowercase key/value keys."""
    session = _FakeSession(
        ecs_tags=[{"key": "relay:app", "value": "ecs-app"}, {"key": "COMPONENT_ID", "value": "ecs-comp"}]
    )
    resolver = AlarmTagResolver(
        account_id="111122223333",
        region="eu-west-1",
        boto3_session=session,
    )
    tags = resolver.resolve(alarm_arn=None, detail=_ecs_detail())
    assert tags["relay:app"] == "ecs-app"
    assert tags["COMPONENT_ID"] == "ecs-comp"


def test_ec2_resolver_returns_tags():
    """EC2 describe_tags returns uppercase Key/Value keys."""
    session = _FakeSession(
        ec2_tags=[{"Key": "relay:app", "Value": "ec2-app"}, {"Key": "COMPONENT_ID", "Value": "ec2-comp"}]
    )
    resolver = AlarmTagResolver(
        account_id="111122223333",
        region="us-east-1",
        boto3_session=session,
    )
    tags = resolver.resolve(alarm_arn=None, detail=_ec2_detail("i-0abc1234567890def"))
    assert tags["relay:app"] == "ec2-app"
    assert tags["COMPONENT_ID"] == "ec2-comp"


def test_ec2_resolver_missing_instance_id_returns_empty():
    """When InstanceId dimension is absent, the resolver returns no resource tags."""
    session = _FakeSession(cw_tags=[{"Key": "alarm-tag", "Value": "yes"}])
    resolver = AlarmTagResolver(
        account_id="111122223333",
        region="us-east-1",
        boto3_session=session,
    )
    # Provide namespace but omit the InstanceId dimension
    detail = _detail("AWS/EC2", [])
    tags = resolver.resolve(
        alarm_arn="arn:aws:cloudwatch:us-east-1:111122223333:alarm:no-dims",
        detail=detail,
    )
    # Only alarm tags; no EC2 resource tags (no InstanceId to look up)
    assert tags == {"alarm-tag": "yes"}


def test_unknown_namespace_returns_only_alarm_tags():
    session = _FakeSession(cw_tags=[{"Key": "relay:app", "Value": "alarm-app"}])
    resolver = AlarmTagResolver(
        account_id="123",
        region="us-east-1",
        boto3_session=session,
    )
    tags = resolver.resolve(
        alarm_arn="arn:aws:cloudwatch:us-east-1:123:alarm:x",
        detail=_detail("AWS/RDS", [{"name": "DBInstanceIdentifier", "value": "my-db"}]),
    )
    # No resource resolver for RDS; only alarm tags
    assert tags == {"relay:app": "alarm-app"}


def test_failing_client_does_not_raise_returns_other_source():
    """When one source raises, the other source's tags are still returned."""

    class _BrokenSession:
        def client(self, service: str, **_kwargs):
            if service == "cloudwatch":
                raise RuntimeError("CW boom")
            if service == "lambda":
                m = MagicMock()
                m.list_tags.return_value = {"Tags": {"relay:app": "from-lambda"}}
                return m
            raise ValueError(service)

    resolver = AlarmTagResolver(
        account_id="123",
        region="us-east-1",
        boto3_session=_BrokenSession(),
    )
    tags = resolver.resolve(
        alarm_arn="arn:aws:cloudwatch:us-east-1:123:alarm:fn-alarm",
        detail=_lambda_detail(),
    )
    # Lambda resource tags still came through
    assert tags.get("relay:app") == "from-lambda"
    # Did not raise


def _detail_dict_dimensions(namespace: str, dimensions: dict[str, str]) -> dict:
    """Detail with dimensions as a JSON object — the real EventBridge shape.

    Real "CloudWatch Alarm State Change" events carry ``dimensions`` as a
    ``{"FunctionName": "..."}`` object, not a list of ``{name, value}`` dicts.
    """
    return {
        "configuration": {
            "metrics": [
                {
                    "metricStat": {
                        "metric": {
                            "namespace": namespace,
                            "dimensions": dimensions,
                        }
                    }
                }
            ]
        }
    }


def test_dimensions_as_object_real_eventbridge_shape():
    """Regression: dimensions delivered as a dict must resolve resource tags.

    The live EventBridge alarm event uses an object for ``dimensions``; an
    earlier extractor assumed a list and raised AttributeError ('str' has no
    'get'), silently dropping all resource tags.
    """
    session = _FakeSession(lambda_tags={"COMPONENT_ID": "relay-node-demo", "GIT_SHA": "abc1234"})
    resolver = AlarmTagResolver(
        account_id="111111111111",
        region="us-east-1",
        boto3_session=session,
    )
    tags = resolver.resolve(
        alarm_arn=None,
        detail=_detail_dict_dimensions("AWS/Lambda", {"FunctionName": "relay-westport-node"}),
    )
    assert tags["COMPONENT_ID"] == "relay-node-demo"
    assert tags["GIT_SHA"] == "abc1234"


def test_extract_dimensions_handles_both_shapes():
    """The extractor accepts both the object and the list-of-{name,value} forms."""
    obj_detail = _detail_dict_dimensions("AWS/Lambda", {"FunctionName": "fn-a"})
    list_detail = _lambda_detail("fn-b")
    assert AlarmTagResolver._extract_dimensions(obj_detail["detail"] if "detail" in obj_detail else obj_detail) == {
        "FunctionName": "fn-a"
    }
    assert AlarmTagResolver._extract_dimensions(list_detail) == {"FunctionName": "fn-b"}


def test_caching_prevents_repeated_calls():
    session = _FakeSession(
        cw_tags=[{"Key": "env", "Value": "prod"}],
    )
    resolver = AlarmTagResolver(
        account_id="123",
        region="us-east-1",
        boto3_session=session,
    )
    arn = "arn:aws:cloudwatch:us-east-1:123:alarm:my-alarm"
    resolver.resolve(alarm_arn=arn, detail=_detail())
    resolver.resolve(alarm_arn=arn, detail=_detail())
    # CloudWatch client should only have been called once (cached second time)
    assert session._cw.call_count == 1
