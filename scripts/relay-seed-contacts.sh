#!/usr/bin/env bash
# relay-seed-contacts.sh — load contacts from a YAML file into the Relay
# DynamoDB table (contacts are PII and live in DynamoDB, not Git).
#
# Usage:  ./scripts/relay-seed-contacts.sh [contacts.yaml] [table_name]
#   contacts.yaml  default: config/contacts.test.yaml
#   table_name     default: $RELAY_TABLE_NAME, else relay-<team> / relay-hub-fleet
#
# Requires AWS credentials with dynamodb:PutItem on the table.
set -euo pipefail

RELAY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTACTS_FILE="${1:-${RELAY_ROOT}/config/contacts.test.yaml}"
TABLE="${2:-${RELAY_TABLE_NAME:-relay-hub-fleet}}"

PY="${RELAY_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "Seeding contacts from ${CONTACTS_FILE} into DynamoDB table ${TABLE}" >&2

"$PY" - "$CONTACTS_FILE" "$TABLE" <<'PYEOF'
import sys, yaml, boto3
from relay.core.model import Contact
from relay.adapters.aws.dynamo_stores import DynamoContactStore

path, table = sys.argv[1], sys.argv[2]
data = yaml.safe_load(open(path))
store = DynamoContactStore(table)
n = 0
for c in data.get("contacts", []):
    contact = Contact.model_validate(c)
    store.put_contact(contact)
    print(f"  + {contact.contact_id}  {contact.name}  <{contact.email}>", file=sys.stderr)
    n += 1
print(f"Seeded {n} contacts.", file=sys.stderr)
PYEOF
