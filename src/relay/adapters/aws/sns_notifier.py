"""Notifier implementation using Amazon SNS for outbound SMS and email pages. SNS is send-only. Inbound SMS acknowledgement is handled by AWS End User Messaging SMS -> SNS -> Lambda, not here."""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from relay.core.model import Incident, Stream

logger = logging.getLogger(__name__)


class SNSNotifier:
    """Notifier that publishes page notifications to an Amazon SNS topic.

    SNS topic subscriptions (SMS/email) handle actual delivery to on-call
    contacts.  This class is responsible only for composing and publishing
    the message payload.

    Implements the Notifier protocol from relay.adapters.base.
    """

    def __init__(
        self,
        topic_arn: str | None = None,
        boto3_session: Any | None = None,
    ) -> None:
        """Initialise the notifier.

        Args:
            topic_arn:      The ARN of the team's notification SNS topic.
                            If None the caller must set it before calling send().
            boto3_session:  Pass a custom boto3.Session for cross-account roles
                            or unit-testing with moto.  Defaults to a new
                            boto3.session.Session() using ambient credentials.
        """
        self.topic_arn = topic_arn
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._sns = session.client("sns")

    def send(
        self,
        *,
        incident: Incident,
        contact_ids: list[str],
        stream: Stream,
    ) -> None:
        """Publish a page notification to the configured SNS topic.

        The topic's subscriptions (SMS/email) handle delivery.  contact_ids are
        informational for message content; actual routing is via SNS subscriptions.

        Args:
            incident:    The incident being paged about.
            contact_ids: Opaque contact IDs included in the message body so that
                         downstream processors know which on-call personnel were
                         notified.
            stream:      The routing stream (e.g. primary, secondary escalation).

        Raises:
            ClientError: On unrecoverable SNS API failure.
            ValueError:  If topic_arn has not been set.
        """
        if not self.topic_arn:
            raise ValueError("SNSNotifier.topic_arn must be set before calling send()")

        message_body: dict[str, Any] = {
            "correlation_id": incident.correlation_id,
            "severity": incident.severity,
            "alarm_name": incident.alarm_name,
            "app_name": incident.app_name,
            "account_id": incident.account_id,
            "stream": stream,
            "state": incident.state,
            "on_call": contact_ids,
        }

        subject = f"[{incident.severity}] {incident.alarm_name} — {incident.app_name}"

        try:
            self._sns.publish(
                TopicArn=self.topic_arn,
                Subject=subject[:100],  # SNS subject limit is 100 chars
                Message=json.dumps(message_body, default=str),
            )
        except ClientError:
            logger.exception(
                "Failed to publish SNS page for incident %s", incident.correlation_id
            )
            raise

        logger.info(
            "Published SNS page",
            extra={
                "correlation_id": incident.correlation_id,
                "topic_arn": self.topic_arn,
                "on_call_count": len(contact_ids),
            },
        )

        # TODO: support per-contact direct publish (e.g. direct SMS via SNS phone
        #       number) as an alternative to topic subscriptions for targeted pages
        #       that should not broadcast to the entire subscriber list.

    def publish_direct(self, phone_number: str, message: str) -> None:
        """Publish directly to a phone number (not a topic).

        Used for targeted SMS pages when a specific contact must be reached
        without broadcasting to all topic subscribers.

        Args:
            phone_number: E.164-formatted phone number (e.g. "+447700900000").
            message:      Plain-text message body (max 160 chars for single SMS).

        Raises:
            ClientError: On SNS API failure.
        """
        try:
            self._sns.publish(PhoneNumber=phone_number, Message=message[:1600])
        except ClientError:
            # Don't log the phone number — it's PII. The exception carries the
            # SNS-side error context needed to diagnose the failure.
            logger.exception("SNS publish_direct failed")
            raise

    def list_subscription_status_by_email(self, topic_arn: str) -> dict[str, str]:
        """Return ``{lowercased_email: "confirmed"|"pending"}`` for the topic's
        email subscriptions.

        Operators subscribe to the paging topic by email; this is the identifier
        the topic pages by, so subscription state on the Contacts screen is keyed
        on email. Paginates ``ListSubscriptionsByTopic`` (the list can span many
        pages on a busy topic) and is best-effort: any SNS error yields ``{}`` so
        the UI degrades to "unknown" rather than erroring.

        A subscription whose ``SubscriptionArn`` is still ``PendingConfirmation``
        is reported as ``"pending"`` (the confirmation email was sent but the
        recipient hasn't clicked it yet); a real ARN is ``"confirmed"``. When the
        same email appears more than once, ``"confirmed"`` wins.
        """
        status: dict[str, str] = {}
        next_token: str | None = None
        try:
            while True:
                if next_token:
                    resp = self._sns.list_subscriptions_by_topic(
                        TopicArn=topic_arn, NextToken=next_token
                    )
                else:
                    resp = self._sns.list_subscriptions_by_topic(TopicArn=topic_arn)
                for sub in resp.get("Subscriptions", []):
                    if sub.get("Protocol") != "email":
                        continue
                    endpoint = (sub.get("Endpoint") or "").strip().lower()
                    if not endpoint:
                        continue
                    arn = sub.get("SubscriptionArn", "")
                    if arn == "Deleted":
                        continue
                    if status.get(endpoint) == "confirmed":
                        continue  # keep the confirmed record over a duplicate
                    status[endpoint] = (
                        "pending" if arn in ("", "PendingConfirmation") else "confirmed"
                    )
                next_token = resp.get("NextToken")
                if not next_token:
                    break
        except ClientError:
            logger.warning(
                "list_subscriptions_by_topic failed for %s", topic_arn, exc_info=True
            )
        return status

    def subscribe_email(self, topic_arn: str, email: str) -> str:
        """Subscribe ``email`` to ``topic_arn`` (protocol=email).

        SNS immediately sends a confirmation email; the subscription stays in
        ``PendingConfirmation`` until the recipient clicks the link. Returns the
        raw ``SubscriptionArn`` from SNS (``"pending confirmation"`` for email).

        Raises:
            ClientError: On SNS API failure.
        """
        resp = self._sns.subscribe(
            TopicArn=topic_arn,
            Protocol="email",
            Endpoint=email,
            ReturnSubscriptionArn=False,
        )
        return str(resp.get("SubscriptionArn", ""))

    def publish_test(
        self,
        *,
        phone: str | None,
        email_topic_arn: str | None,
        message: str,
    ) -> dict[str, bool]:
        """Send a test page. Direct SMS to ``phone`` if given; also publish to a
        topic ARN if given (reaches email subscribers). Returns which channels
        fired. Channels are attempted independently so one failing (e.g. SMS
        sandbox limits) doesn't block the other.
        """
        result = {"sms": False, "topic": False}
        if phone:
            try:
                self.publish_direct(phone, message)
                result["sms"] = True
            except Exception:
                logger.warning("Test SMS to %s failed", phone, exc_info=True)
        if email_topic_arn:
            try:
                self._sns.publish(
                    TopicArn=email_topic_arn,
                    Subject="Relay test page",
                    Message=message,
                )
                result["topic"] = True
            except Exception:
                logger.warning("Test topic publish failed", exc_info=True)
        return result
