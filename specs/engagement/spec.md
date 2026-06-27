# Domain Spec: Engagement / Notification

**Owns:** delivering a page — dispatching the actual email and SMS notifications
to the contacts identified by the escalation ladder, and tracking engagement
state through to acknowledgement.

**Primary code:** `core/dispatcher.py` (`DualStreamDispatcher`, `Stream`),
`adapters/aws/sns_notifier.py` (`SNSNotifier`, `send`, `publish_direct`),
`core/model.py` (engagement state machine).
**status.md:** §4. **Related domains:** [escalation](../escalation/spec.md)
(calls the dispatcher for each ladder step), [contacts](../contacts/spec.md)
(email/phone addresses used here), [scheduling](../scheduling/spec.md)
(resolves roles → contacts before dispatch), [chatops](../chatops/spec.md)
(Teams webhook is a parallel notification channel),
[integrations-config](../integrations-config/spec.md) (lifecycle seam triggers
adapter notifications independently).

## What it does now

- **Email delivery** via SNS (`SNSNotifier.send`) — fully operational.
- **SMS delivery** (`SNSNotifier.publish_direct`) — code path exists; gated:
  requires `relay:enable_direct_sms` IAM grant and AWS SNS sandbox exit before
  direct-to-phone is available in production.
- **Dual-stream dispatch** (`DualStreamDispatcher`): every page is evaluated
  against two streams — `Stream.TEAM` (the team's SNS topic) and
  `Stream.CENTRAL` (upstream EventBridge for federated hubs). Each escalation
  step declares which streams to use. **`Stream.CENTRAL` must not be renamed** —
  it is the internal seam that enables distributed topology; renaming it would
  break the split-process future without a rewrite.
- **Add responders mid-incident:** the Hub exposes a manual page endpoint so an
  operator can page additional contacts after the ladder has fired.
- **Engagement state machine** on `core/model.py`: `TRIGGERED → ENGAGED →
  ACKNOWLEDGED`. Ack stops the escalation ladder; the state transition is
  recorded as a `TimelineEvent`.

## Key entities

- **`Stream`** — enum: `TEAM`, `CENTRAL`. Carried on each `EscalationStep`.
- **`DualStreamDispatcher`** — evaluates which streams to activate and calls the
  corresponding transport (SNS topic / EventBridge `PutEvents`).
- **`SNSNotifier`** — AWS SNS wrapper; `send` for topic-based email, `publish_direct`
  for direct SMS (gated).

## Invariants

- **AWS-free core:** `core/dispatcher.py` contains no `boto3`; all AWS I/O is in
  `adapters/aws/sns_notifier.py`.
- **`Stream.CENTRAL` must not be renamed** — it is the internal seam for the
  distributed-topology split; renaming it silently breaks the hot path.
- **Ack is terminal for the ladder** — once acknowledged, `DualStreamDispatcher`
  must not be called for further steps.

## Out of scope (non-goals)

- **Voice engagement** — call-out / phone ack is deliberately out of scope
  (status.md §4 ⛔); email + SMS only.
- **Inbound ack via SMS reply** — UI/console ack works today; inbound SMS ack is
  roadmap (`TODO` in `node/handler.py`, status.md §4 🗺️).
