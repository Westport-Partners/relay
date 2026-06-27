# Domain Spec: Contacts

**Owns:** the contact directory — the set of people who can be paged, their
notification channels, and the CRUD operations that keep the directory current.

**Primary code:** `adapters/aws/dynamo_stores.py` (`DynamoContactStore`),
`core/model.py` (`Contact`, channel types), `hub/app.py` (`/contacts` routes).
**status.md:** §2. **Related domains:** [scheduling](../scheduling/spec.md)
(contacts are assigned to roles in slots), [engagement](../engagement/spec.md)
(channels here are the delivery addresses used when paging),
[ui](../ui/spec.md) (searchable Contacts directory view).

## What it does now

- A **Contact** stores a person's name, email address, and phone number for SMS
  delivery. Both email and SMS channels are modeled on the core type.
- **Full CRUD** via `hub/app.py` (`GET/POST/PUT/DELETE /contacts`); the UI
  renders a searchable directory.
- **PII is stored only in DynamoDB** — never written to Git or any config file.
- Channel activation (SNS subscription management) substitutes for AWS Incident
  Manager's per-channel activation handshake; there is no Relay-native opt-in
  flow.

## Key entities

- **`Contact`** — `{ id, name, email, phone }`.
- **`DynamoContactStore`** — the sole persistence layer; partitioned under the
  deployment's DynamoDB table.

## Invariants

- **PII in DynamoDB only** — contacts must never appear in `catalog.yaml`,
  `routing.yaml`, or any file committed to Git.
- **AWS-free core:** the `Contact` model and channel types live in `core/model.py`
  with no `boto3`; all storage is behind the `DynamoContactStore` adapter.

## Out of scope (non-goals)

- Active Directory / LDAP import — identity is self-service (name/email/phone
  entered directly in the UI).
- Per-channel activation handshake (Relay relies on SNS subscription management
  instead of IM's START/STOP opt-in flow — status.md §2 ⛔/🟡).
