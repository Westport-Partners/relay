#!/usr/bin/env python3
"""Create the Relay DynamoDB table in a local DynamoDB endpoint + seed demo data.

For the offline local-mock harness (collapsed-single-container plan §6). Reads
``RELAY_AWS_ENDPOINT_URL`` (e.g. http://localhost:8000 for DynamoDB-Local) and
``RELAY_TABLE_NAME`` (default ``relay-local``), creates the single table with the
``incident-status-index`` GSI to match RelayDataStack, then seeds the test
contacts so a fresh ``docker compose up`` can fire an incident and page someone.

Idempotent: skips creation if the table already exists.

Usage (inside the harness; AWS_* dummies + endpoint are set by docker-compose):
    python scripts/relay-local-bootstrap.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _endpoint() -> str:
    ep = os.environ.get("RELAY_AWS_ENDPOINT_URL", "").strip()
    if not ep:
        sys.exit(
            "RELAY_AWS_ENDPOINT_URL must point at a local DynamoDB "
            "(e.g. http://localhost:8000) — refusing to touch real AWS."
        )
    return ep


def main() -> None:
    endpoint = _endpoint()
    table_name = os.environ.get("RELAY_TABLE_NAME", "relay-local")
    region = os.environ.get("AWS_REGION", "us-east-1")

    ddb = boto3.client("dynamodb", endpoint_url=endpoint, region_name=region)

    existing = ddb.list_tables().get("TableNames", [])
    if table_name in existing:
        print(f"Table {table_name!r} already exists at {endpoint}; skipping create.")
    else:
        print(f"Creating table {table_name!r} at {endpoint} ...")
        ddb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "opened_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "incident-status-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "opened_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.get_waiter("table_exists").wait(TableName=table_name)
        print("  table ready.")

    # Seed demo contacts (best-effort; the stores honor RELAY_AWS_ENDPOINT_URL).
    contacts_file = REPO_ROOT / "config" / "contacts.test.yaml"
    if contacts_file.exists():
        try:
            import yaml

            from relay.adapters.aws.dynamo_stores import DynamoContactStore
            from relay.core.model import Contact

            store = DynamoContactStore(table_name)
            data = yaml.safe_load(contacts_file.read_text()) or {}
            n = 0
            for c in data.get("contacts", []):
                store.put_contact(Contact.model_validate(c))
                n += 1
            print(f"Seeded {n} demo contacts from {contacts_file.name}.")
        except ClientError:
            print("Contact seed failed (non-fatal).", file=sys.stderr)
    print("Local bootstrap complete.")


if __name__ == "__main__":
    main()
