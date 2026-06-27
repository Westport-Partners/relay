"""Deterministic fake-org generator for Relay test environments.

Builds a believable "generic government agency" org and the people who run it,
so a local Relay Hub can be populated with realistic data and have fake
incidents fired at it. Modeled on a patent/trademark agency's structure (four
top-tier product lines) but deliberately generic — no real agency is named.

The four product lines:
  * Primary Product Line    — the agency's flagship mission service.
  * Secondary Product Line  — its second mission service.
  * Infrastructure          — the internal-services product line (platform).
  * Administrative          — running the org (timesheets, HR, finance).

Hierarchy follows Relay's canonical model:
    product_line > product > component > deployment
where each deployment leaf is a GitLab project deployed into one environment
(prod / test / dev) of one AWS account.

Everything is seeded (Faker.seed + random.seed) so the same world regenerates
identically on every run — IDs are stable, so re-running the bootstrap upserts
rather than duplicating. Faker.phone_number() is intentionally NOT used (it can
emit real, dialable numbers and the /contacts/{id}/test endpoint sends real
SMS); phones come from the reserved +1-555-0100xxx test range instead.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

try:
    from faker import Faker
except ImportError as exc:  # pragma: no cover - guidance only
    raise SystemExit(
        "Faker is required: pip install 'faker>=30.0' "
        "(or pip install -e '.[dev]' in this repo)."
    ) from exc

SEED = 42

# Environments mirror config/environments.yaml. Each maps to a fake 12-digit
# AWS account id so incident account_environment_map resolution is realistic.
ENVIRONMENTS: dict[str, str] = {
    "prod": "111111111111",
    "test": "222222222222",
    "dev": "333333333333",
}

# Region pool for deployment leaves (kept small + us-gov-flavored-but-generic).
REGIONS = ["us-east-1", "us-west-2"]


# ---------------------------------------------------------------------------
# Static structural skeleton (names are fixed; people/metadata are Faker-filled)
# ---------------------------------------------------------------------------
#
# Each product line -> products -> components. A component becomes one GitLab
# project (the gitlab_project slug) and is deployed into one-or-more
# environments; every (component, environment) pair is a deployment leaf/tile.

_SKELETON: dict[str, dict[str, Any]] = {
    "pl-primary": {
        "name": "Primary Product Line",
        "team": "team-primary",
        "products": {
            "prd-primary-filing": {
                "name": "Application Filing",
                "components": {
                    "cmp-intake": ("Intake Service", "primary-intake-api"),
                    "cmp-validation": ("Validation Engine", "primary-validation-svc"),
                },
            },
            "prd-primary-exam": {
                "name": "Examination",
                "components": {
                    "cmp-workbench": ("Examiner Workbench", "primary-workbench-web"),
                    "cmp-search": ("Prior-Art Search", "primary-search-svc"),
                },
            },
        },
    },
    "pl-secondary": {
        "name": "Secondary Product Line",
        "team": "team-secondary",
        "products": {
            "prd-secondary-reg": {
                "name": "Registration",
                "components": {
                    "cmp-filing": ("Online Filing", "secondary-filing-api"),
                    "cmp-classify": ("Classification", "secondary-classify-svc"),
                },
            },
        },
    },
    "pl-infra": {
        "name": "Infrastructure",
        "team": "team-infra",
        "products": {
            "prd-platform": {
                "name": "Shared Platform",
                "components": {
                    "cmp-identity": ("Identity & Access", "infra-sso"),
                    "cmp-eventbus": ("Event Bus", "infra-event-bus"),
                    "cmp-datalake": ("Data Platform", "infra-data-lake"),
                    "cmp-observability": ("Observability", "infra-observability"),
                },
            },
        },
    },
    "pl-admin": {
        "name": "Administrative",
        "team": "team-admin",
        "products": {
            "prd-workforce": {
                "name": "Workforce Systems",
                "components": {
                    "cmp-timesheets": ("Timesheets", "admin-timesheets-web"),
                    "cmp-hr": ("HR Portal", "admin-hr-portal"),
                    "cmp-finance": ("Financial Management", "admin-finance-api"),
                },
            },
        },
    },
}

# Which environments each product line deploys into. Mission lines run the full
# prod/test/dev set; back-office lines are leaner — keeps the leaf count near 40
# and makes the environments look realistically uneven.
_PL_ENVIRONMENTS: dict[str, list[str]] = {
    "pl-primary": ["prod", "test", "dev"],
    "pl-secondary": ["prod", "test", "dev"],
    "pl-infra": ["prod", "test", "dev"],
    "pl-admin": ["prod", "test", "dev"],
}


@dataclass
class Deployment:
    """One deployment leaf — a (component, environment) tile."""

    deployment_id: str
    name: str
    parent: str            # component node id
    product_line: str      # pl-* id (for incident scenario weighting)
    team: str
    environment: str
    account_id: str
    region: str
    gitlab_project: str
    runbook: str


@dataclass
class Person:
    contact_id: str
    name: str
    email: str
    phone: str
    team: str
    title: str
    manager: bool = False
    available: bool = True
    slots: dict[str, list[str]] = field(default_factory=dict)
    roles: list[str] = field(default_factory=lambda: ["primary", "secondary"])
    ooo: dict[str, str] | None = None


@dataclass
class World:
    nodes: list[dict[str, Any]]          # flat OrgNode dicts (catalog.yaml shape)
    deployments: list[Deployment]
    people: list[Person]
    environments: dict[str, str]         # env -> account_id

    def deployment_index(self) -> dict[str, Deployment]:
        return {d.deployment_id: d for d in self.deployments}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _phone(n: int) -> str:
    """A fictional reserved-range phone — never a real, dialable number."""
    return f"+1555010{n:04d}"


def build_world(seed: int = SEED) -> World:
    """Construct the full deterministic world."""
    fake = Faker("en_US")
    Faker.seed(seed)
    random.seed(seed)

    nodes: list[dict[str, Any]] = []
    deployments: list[Deployment] = []

    for pl_id, pl in _SKELETON.items():
        team = pl["team"]
        nodes.append(
            {
                "id": pl_id,
                "name": pl["name"],
                "level": "product_line",
                "parent": None,
                "owner_ref": team,
            }
        )
        for prd_id, prd in pl["products"].items():
            nodes.append(
                {
                    "id": prd_id,
                    "name": prd["name"],
                    "level": "product",
                    "parent": pl_id,
                    "owner_ref": team,
                }
            )
            for cmp_id, (cmp_name, gl_slug) in prd["components"].items():
                nodes.append(
                    {
                        "id": cmp_id,
                        "name": cmp_name,
                        "level": "component",
                        "parent": prd_id,
                    }
                )
                for env in _PL_ENVIRONMENTS[pl_id]:
                    dep_id = f"{gl_slug}-{env}"
                    region = random.choice(REGIONS)
                    runbook = (
                        f"https://gitlab.example.com/agency/{gl_slug}/-/blob/main/RUNBOOK.md"
                    )
                    nodes.append(
                        {
                            "id": dep_id,
                            "name": dep_id,
                            "level": "deployment",
                            "parent": cmp_id,
                            "owner_ref": team,
                            "metadata": {
                                "gitlab_project": f"agency/{gl_slug}",
                                "region": region,
                                "runbook": runbook,
                                "environment": env,
                            },
                        }
                    )
                    deployments.append(
                        Deployment(
                            deployment_id=dep_id,
                            name=dep_id,
                            parent=cmp_id,
                            product_line=pl_id,
                            team=team,
                            environment=env,
                            account_id=ENVIRONMENTS[env],
                            region=region,
                            gitlab_project=f"agency/{gl_slug}",
                            runbook=runbook,
                        )
                    )

    people = _build_people(fake)

    return World(
        nodes=nodes,
        deployments=deployments,
        people=people,
        environments=dict(ENVIRONMENTS),
    )


def _build_people(fake: Faker) -> list[Person]:
    """~25 contacts spread across the four teams.

    Each team gets a manager (MANAGER-eligible) plus a handful of responders.
    A couple of people are set OOO and one is left with sparse slots to create
    realistic coverage gaps the scheduling UI can highlight.
    """
    teams = ["team-primary", "team-secondary", "team-infra", "team-admin"]
    # Headcount per team (sums to 25).
    headcount = {"team-primary": 8, "team-secondary": 6, "team-infra": 6, "team-admin": 5}

    full_week = {
        "mon": ["night", "day", "evening"],
        "tue": ["night", "day", "evening"],
        "wed": ["night", "day", "evening"],
        "thu": ["night", "day", "evening"],
        "fri": ["night", "day", "evening"],
        "sat": ["day", "evening"],
        "sun": ["day", "evening"],
    }
    weekday_only = {k: v for k, v in full_week.items() if k not in ("sat", "sun")}
    sparse = {"mon": ["day"], "wed": ["day"], "fri": ["day"]}

    people: list[Person] = []
    seen_ids: set[str] = set()
    ooo_budget = 2  # at most two people OOO, for visible-but-not-crippling gaps

    for team in teams:
        for i in range(headcount[team]):
            name = fake.unique.name()
            # Stable contact_id from name; disambiguate collisions deterministically.
            base = "cnt-" + _slug(name)
            cid = base
            suffix = 1
            while cid in seen_ids:
                suffix += 1
                cid = f"{base}-{suffix}"
            seen_ids.add(cid)

            is_manager = i == 0  # first person per team is the manager
            n = len(people) + 101
            email = f"{_slug(name)}@agency.example.gov".replace("--", "-")

            roles = ["primary", "secondary", "manager"] if is_manager else ["primary", "secondary"]

            # Slot pattern: manager works weekdays; one responder per team is
            # sparse (gap source); the rest cover the full week.
            if is_manager:
                slots = {k: list(v) for k, v in weekday_only.items()}
            elif i == headcount[team] - 1:
                slots = {k: list(v) for k, v in sparse.items()}
            else:
                slots = {k: list(v) for k, v in full_week.items()}

            ooo = None
            # Put the 2nd responder of the first two teams OOO this week.
            if i == 1 and ooo_budget > 0:
                ooo = {"start": "2026-06-22", "end": "2026-06-28"}
                ooo_budget -= 1

            people.append(
                Person(
                    contact_id=cid,
                    name=name,
                    email=email,
                    phone=_phone(n),
                    team=team,
                    title=("Engineering Manager" if is_manager else fake.job()),
                    manager=is_manager,
                    available=True,
                    slots=slots,
                    roles=roles,
                    ooo=ooo,
                )
            )

    return people


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def world_to_catalog_yaml(world: World) -> str:
    """Render the catalog.yaml (CatalogConfig: flat list of OrgNodes)."""
    # Drop empty metadata/owner_ref keys so the file stays clean + matches schema.
    clean_nodes = []
    for n in world.nodes:
        node = {k: v for k, v in n.items() if v is not None}
        clean_nodes.append(node)
    return yaml.safe_dump({"nodes": clean_nodes}, sort_keys=False, width=100)


def world_to_contacts_yaml(world: World) -> str:
    """Render a contacts YAML compatible with relay-seed-contacts.sh."""
    contacts = [
        {"contact_id": p.contact_id, "name": p.name, "email": p.email, "phone": p.phone}
        for p in world.people
    ]
    return yaml.safe_dump({"contacts": contacts}, sort_keys=False, width=100)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the Relay test-env world.")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument(
        "--emit",
        choices=["summary", "catalog", "contacts", "json"],
        default="summary",
    )
    args = ap.parse_args()

    world = build_world(args.seed)

    if args.emit == "catalog":
        print(world_to_catalog_yaml(world), end="")
    elif args.emit == "contacts":
        print(world_to_contacts_yaml(world), end="")
    elif args.emit == "json":
        print(
            json.dumps(
                {
                    "nodes": world.nodes,
                    "deployments": [asdict(d) for d in world.deployments],
                    "people": [asdict(p) for p in world.people],
                    "environments": world.environments,
                },
                indent=2,
            )
        )
    else:
        n_pl = sum(1 for n in world.nodes if n["level"] == "product_line")
        n_prd = sum(1 for n in world.nodes if n["level"] == "product")
        n_cmp = sum(1 for n in world.nodes if n["level"] == "component")
        n_dep = len(world.deployments)
        print(f"World (seed={args.seed}):")
        print(f"  product lines : {n_pl}")
        print(f"  products      : {n_prd}")
        print(f"  components    : {n_cmp}")
        print(f"  deployments   : {n_dep}")
        print(f"  contacts      : {len(world.people)}")
        print(f"  environments  : {', '.join(world.environments)}")
        by_env: dict[str, int] = {}
        for d in world.deployments:
            by_env[d.environment] = by_env.get(d.environment, 0) + 1
        print(f"  tiles/env     : {by_env}")


if __name__ == "__main__":
    main()
