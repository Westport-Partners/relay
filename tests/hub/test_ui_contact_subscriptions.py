"""Tests for the Contacts screen SNS subscription-state endpoints (#78).

Covers:
  - GET /contacts/subscriptions — per-contact status matched on email, cached.
  - POST /contacts/{id}/subscribe — writer-gated email subscribe.
  - SNSNotifier.list_subscription_status_by_email pagination + protocol filter.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from relay.core.model import Contact  # noqa: E402
from relay.hub.app import HubApp, HubState, SSEPublisher  # noqa: E402


class _FakeContactStore:
    def __init__(self, contacts):
        self._db = {c.contact_id: c for c in contacts}

    def list_contacts(self):
        return list(self._db.values())

    def get_contact(self, cid):
        return self._db.get(cid)


class _FakeNotifier:
    """Records calls; returns a canned email-subscription map."""

    def __init__(self, status_map=None, fail=False):
        self._status = status_map or {}
        self._fail = fail
        self.list_calls = 0
        self.subscribed: list[tuple[str, str]] = []

    def list_subscription_status_by_email(self, topic_arn):
        self.list_calls += 1
        if self._fail:
            raise RuntimeError("boom")
        return dict(self._status)

    def subscribe_email(self, topic_arn, email):
        self.subscribed.append((topic_arn, email))
        # Simulate SNS moving the endpoint into the map as pending.
        self._status[email.strip().lower()] = "pending"
        return "pending confirmation"


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    monkeypatch.delenv("RELAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("RELAY_DEV_USER", raising=False)
    yield


def _client(contacts, notifier=None, topic="arn:aws:sns:us-east-1:1:relay-paging"):
    app_obj = HubApp.__new__(HubApp)
    app_obj._contact_store = _FakeContactStore(contacts)
    app_obj._notifier = notifier
    app_obj._paging_topic_arn = topic
    app_obj._settings_store = None
    app_obj._incident_store = None
    app_obj._schedule_store = None
    app_obj._config = None
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    return TestClient(app_obj.build_fastapi_app())


def _contacts():
    return [
        Contact(contact_id="c1", name="Alice", email="alice@example.com"),
        Contact(contact_id="c2", name="Bob", email="bob@example.com"),
        Contact(contact_id="c3", name="Carol", phone="+15550100"),  # no email
    ]


def test_subscription_status_matches_on_email():
    notifier = _FakeNotifier({"alice@example.com": "confirmed"})
    c = _client(_contacts(), notifier)
    body = c.get("/contacts/subscriptions").json()
    assert body["available"] is True
    st = body["statuses"]
    assert st["c1"] == "confirmed"
    assert st["c2"] == "unsubscribed"
    assert st["c3"] == "no_email"


def test_subscription_status_case_insensitive_email():
    notifier = _FakeNotifier({"alice@example.com": "confirmed"})
    contacts = [Contact(contact_id="c1", name="Alice", email="Alice@Example.COM")]
    c = _client(contacts, notifier)
    assert c.get("/contacts/subscriptions").json()["statuses"]["c1"] == "confirmed"


def test_subscription_status_unavailable_without_topic():
    notifier = _FakeNotifier({})
    c = _client(_contacts(), notifier, topic=None)
    body = c.get("/contacts/subscriptions").json()
    assert body["available"] is False
    # Email contacts report "unknown"; phone-only still "no_email".
    assert body["statuses"]["c1"] == "unknown"
    assert body["statuses"]["c3"] == "no_email"
    assert notifier.list_calls == 0  # never lists when no topic


def test_subscription_list_is_cached_across_calls():
    notifier = _FakeNotifier({"alice@example.com": "confirmed"})
    c = _client(_contacts(), notifier)
    c.get("/contacts/subscriptions")
    c.get("/contacts/subscriptions")
    c.get("/contacts/subscriptions")
    assert notifier.list_calls == 1  # TTL cache — one SNS list serves all


def test_subscribe_requires_auth():
    notifier = _FakeNotifier({})
    c = _client(_contacts(), notifier)
    assert c.post("/contacts/c1/subscribe").status_code == 403


def test_subscribe_adds_email_and_flips_to_pending(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    notifier = _FakeNotifier({})
    c = _client(_contacts(), notifier)
    r = c.post("/contacts/c1/subscribe")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "pending"}
    assert notifier.subscribed == [
        ("arn:aws:sns:us-east-1:1:relay-paging", "alice@example.com")
    ]
    # Force-refresh means the follow-up status reflects pending immediately.
    assert c.get("/contacts/subscriptions").json()["statuses"]["c1"] == "pending"


def test_subscribe_rejects_contact_without_email(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    notifier = _FakeNotifier({})
    c = _client(_contacts(), notifier)
    assert c.post("/contacts/c3/subscribe").status_code == 422


def test_subscribe_404_for_unknown_contact(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client(_contacts(), _FakeNotifier({}))
    assert c.post("/contacts/nope/subscribe").status_code == 404


def test_subscription_status_degrades_on_sns_error():
    notifier = _FakeNotifier({}, fail=True)
    c = _client(_contacts(), notifier)
    body = c.get("/contacts/subscriptions").json()
    # SNS error → empty map → email contacts look "unsubscribed" (topic available).
    assert body["available"] is True
    assert body["statuses"]["c1"] == "unsubscribed"


# ---------------------------------------------------------------------------
# SNSNotifier unit tests (paginated list + protocol filtering)
# ---------------------------------------------------------------------------


class _FakeSnsClient:
    def __init__(self, pages):
        self._pages = pages
        self.subscribe_calls: list[dict[str, object]] = []

    def list_subscriptions_by_topic(self, **kwargs):
        token = kwargs.get("NextToken")
        idx = 0 if token is None else int(token)
        return self._pages[idx]

    def subscribe(self, **kwargs):
        self.subscribe_calls.append(kwargs)
        return {"SubscriptionArn": "pending confirmation"}


def _notifier_with(pages):
    from relay.adapters.aws.sns_notifier import SNSNotifier

    n = SNSNotifier.__new__(SNSNotifier)
    n.topic_arn = "arn:topic"
    n._sns = _FakeSnsClient(pages)
    return n


def test_list_subscription_status_paginates_and_filters_protocol():
    pages = [
        {
            "Subscriptions": [
                {"Protocol": "email", "Endpoint": "A@x.com", "SubscriptionArn": "arn:1"},
                {"Protocol": "sms", "Endpoint": "+15550100", "SubscriptionArn": "arn:2"},
            ],
            "NextToken": "1",
        },
        {
            "Subscriptions": [
                {"Protocol": "email", "Endpoint": "b@x.com",
                 "SubscriptionArn": "PendingConfirmation"},
            ],
        },
    ]
    n = _notifier_with(pages)
    status = n.list_subscription_status_by_email("arn:topic")
    assert status == {"a@x.com": "confirmed", "b@x.com": "pending"}


def test_list_subscription_confirmed_wins_over_duplicate():
    pages = [
        {
            "Subscriptions": [
                {"Protocol": "email", "Endpoint": "a@x.com",
                 "SubscriptionArn": "PendingConfirmation"},
                {"Protocol": "email", "Endpoint": "a@x.com", "SubscriptionArn": "arn:1"},
            ],
        },
    ]
    n = _notifier_with(pages)
    assert n.list_subscription_status_by_email("arn:topic") == {"a@x.com": "confirmed"}


def test_subscribe_email_calls_sns():
    n = _notifier_with([{"Subscriptions": []}])
    arn = n.subscribe_email("arn:topic", "new@x.com")
    assert arn == "pending confirmation"
    assert n._sns.subscribe_calls[0]["Protocol"] == "email"
    assert n._sns.subscribe_calls[0]["Endpoint"] == "new@x.com"
