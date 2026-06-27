"""Relay test-environment harness — populate a running Hub and simulate a site.

Drives a running Relay Hub entirely over its HTTP API to make a fresh, empty
instance look like a real agency's incident-management deployment:

  1. waits for the Hub to be healthy
  2. heartbeats every deployment leaf so the big-board fills with LIVE tiles
     (and keeps heartbeating on a cadence so they stay green)
  3. seeds the people (contacts) and their on-call availability, then
     auto-generates this week's schedule
  4. seeds a few routing + ignore rules to show mission-vs-back-office routing
  5. fires fake incidents — either a one-shot burst (--once) or a slow drip
     (default) so the board visibly evolves during a demo

The world (org hierarchy + people) is generated deterministically by world.py,
so IDs are stable and re-running the harness upserts rather than duplicating.

Run it locally against a container:
    python tools/testenv/harness.py --base-url http://localhost:8080

or let the container run it for you (see RELAY_DEMO in docker-compose.yml).

Writes require the Hub to be in dev/alb auth mode; the local-mock compose stack
sets RELAY_AUTH_MODE=dev so any request is an authenticated 'operator'.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from typing import Any

import httpx

# Allow running both as `python tools/testenv/harness.py` (CWD=repo root) and as
# `python harness.py` from inside this dir — put our own dir on sys.path so the
# sibling world.py imports cleanly either way.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from world import Deployment, World, build_world  # noqa: E402

# Reserved synthetic seed so incident choices are repeatable per run-offset.
_RNG = random.Random(42)

SHIFT_LABELS = ("night", "day", "evening")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class Hub:
    """Thin typed wrapper over the Hub HTTP API."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        # Dev-mode auth ignores header contents, but send a bearer anyway so the
        # same harness works unchanged if the Hub is later switched to alb mode
        # behind a header-injecting proxy.
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": "Bearer relay-testenv-operator"},
        )

    def close(self) -> None:
        self._client.close()

    def wait_healthy(self, attempts: int = 60, delay: float = 2.0) -> None:
        last: Exception | None = None
        for i in range(attempts):
            try:
                r = self._client.get("/health")
                if r.status_code == 200 and r.json().get("status") == "ok":
                    print(f"  hub healthy after {i + 1} attempt(s)")
                    return
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(delay)
        raise SystemExit(f"Hub never became healthy at {self.base_url}: {last}")

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self._client.post(path, json=body)

    def put(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self._client.put(path, json=body)

    def get(self, path: str) -> httpx.Response:
        return self._client.get(path)


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _org_path_for(world: World, dep: Deployment) -> list[dict[str, Any]]:
    """Root->leaf org_path dicts for a deployment (heartbeat shape)."""
    by_id = {n["id"]: n for n in world.nodes}
    chain: list[dict[str, Any]] = []
    cur: str | None = dep.deployment_id
    while cur is not None:
        node = by_id.get(cur)
        if node is None:
            break
        entry: dict[str, Any] = {
            "id": node["id"],
            "name": node["name"],
            "level": node["level"],
            "parent": node.get("parent"),
        }
        if node.get("metadata"):
            entry["metadata"] = node["metadata"]
        if node.get("owner_ref"):
            entry["owner_ref"] = node["owner_ref"]
        chain.append(entry)
        cur = node.get("parent")
    return list(reversed(chain))


def _heartbeat_body(world: World, dep: Deployment) -> dict[str, Any]:
    org_path = _org_path_for(world, dep)
    service_path = [e["name"] for e in org_path]
    return {
        "relay_event": "heartbeat",
        "account_id": dep.account_id,
        "app_name": dep.deployment_id,
        "environment": dep.environment,
        "deployment_id": dep.deployment_id,
        "service_path": service_path,
        "org_path": org_path,
        "metadata": {
            "gitlab_project": dep.gitlab_project,
            "region": dep.region,
            "runbook": dep.runbook,
            "owner": dep.team,
        },
    }


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def heartbeat_round(hub: Hub, world: World) -> int:
    ok = 0
    for dep in world.deployments:
        r = hub.post("/ingest/heartbeat", _heartbeat_body(world, dep))
        if r.status_code == 200:
            ok += 1
        else:
            print(f"  ! heartbeat {dep.deployment_id} -> {r.status_code} {r.text[:120]}")
    return ok


def seed_contacts(hub: Hub, world: World) -> int:
    ok = 0
    for p in world.people:
        r = hub.post(
            "/contacts",
            {"contact_id": p.contact_id, "name": p.name, "email": p.email, "phone": p.phone},
        )
        if r.status_code == 200:
            ok += 1
        else:
            print(f"  ! contact {p.contact_id} -> {r.status_code} {r.text[:120]}")
    return ok


def seed_availability(hub: Hub, world: World) -> int:
    ok = 0
    for p in world.people:
        body: dict[str, Any] = {
            "available": p.available,
            "slots": p.slots,
            "roles": p.roles,
            "ooo": p.ooo,
        }
        r = hub.put(f"/availability/{p.contact_id}", body)
        if r.status_code == 200:
            ok += 1
        else:
            print(f"  ! availability {p.contact_id} -> {r.status_code} {r.text[:120]}")
    return ok


def auto_schedule(hub: Hub) -> None:
    r = hub.post("/schedule/auto", {})
    if r.status_code == 200:
        data = r.json()
        cov = data.get("coverage")
        gaps = data.get("gaps")
        print(f"  schedule generated: coverage={cov} gaps={gaps}")
    else:
        print(f"  ! schedule/auto -> {r.status_code} {r.text[:160]}")


def seed_rules(hub: Hub) -> None:
    """A few routing + ignore rules to show mission-vs-back-office handling.

    Escalation policies (pol-critical / pol-standard) come from the Hub's seeded
    config/escalation.yaml. These rules are best-effort: the Hub already seeds
    routing.yaml on first boot, so a 409/duplicate here is harmless.
    """
    routing = [
        {
            "rule_id": "te-rds-critical",
            "priority": 10,
            "namespace_prefix": "AWS/RDS",
            "severity_override": "SEV1",
            "escalation_policy_id": "pol-critical",
            "streams": ["TEAM", "CENTRAL"],
        },
        {
            "rule_id": "te-admin-team-only",
            "priority": 50,
            "tag_filters": {"owner": "team-admin"},
            "severity_override": "SEV3",
            "escalation_policy_id": "pol-standard",
            "streams": ["TEAM"],
        },
    ]
    for body in routing:
        r = hub.post("/routing-rules", body)
        flag = "ok" if r.status_code == 200 else f"{r.status_code} {r.text[:80]}"
        print(f"  routing-rule {body['rule_id']}: {flag}")

    ignore = {
        "name": "Ignore dev-account smoke noise",
        "environment": "dev",
        "alarm_name_prefix": "noise-",
        "note": "Dev-account synthetic noise — never page.",
        "enabled": True,
    }
    r = hub.post("/rules", ignore)
    flag = "ok" if r.status_code == 200 else f"{r.status_code} {r.text[:80]}"
    print(f"  ignore-rule {ignore['name']!r}: {flag}")


# ---------------------------------------------------------------------------
# Incident scenarios
# ---------------------------------------------------------------------------


def _fire_synthetic(
    hub: Hub, dep: Deployment, severity: str, alarm_name: str
) -> str | None:
    body = {
        "app_name": dep.deployment_id,
        "account_id": dep.account_id,
        "region": dep.region,
        "severity": severity,
        "alarm_name": alarm_name,
        "environment": dep.environment,
        "deployment_id": dep.deployment_id,
    }
    r = hub.post("/synthetic/incident", body)
    if r.status_code != 200:
        print(f"  ! synthetic {dep.deployment_id} -> {r.status_code} {r.text[:120]}")
        return None
    data = r.json()
    return data.get("correlation_id") or (data.get("incident") or {}).get("correlation_id")


def _prod_leaf(world: World, product_line: str) -> Deployment | None:
    cands = [
        d
        for d in world.deployments
        if d.product_line == product_line and d.environment == "prod"
    ]
    return _RNG.choice(cands) if cands else None


def scenario_burst(hub: Hub, world: World) -> None:
    """One representative incident per product line, plus a couple of acks.

    Mission lines get high-severity prod incidents; Administrative gets a
    team-only SEV3. We acknowledge one to show the ACKNOWLEDGED (amber) state.
    """
    plan = [
        ("pl-primary", "SEV1", "primary-prod-error-rate-high"),
        ("pl-secondary", "SEV2", "secondary-prod-latency-high"),
        ("pl-infra", "SEV2", "infra-canary-failed"),
        ("pl-admin", "SEV3", "admin-queue-backlog"),
    ]
    fired: list[tuple[str, str]] = []
    for pl, sev, alarm in plan:
        dep = _prod_leaf(world, pl)
        if dep is None:
            continue
        cid = _fire_synthetic(hub, dep, sev, alarm)
        if cid:
            fired.append((cid, dep.deployment_id))
            print(f"  fired {sev:4s} {alarm} on {dep.deployment_id} ({cid[:8]})")

    # Acknowledge the second incident to demonstrate the amber/ack state.
    if len(fired) >= 2:
        cid, dep_id = fired[1]
        r = hub.post(f"/incidents/{cid}/acknowledge", {})
        print(f"  acknowledged {dep_id} ({cid[:8]}): "
              f"{'ok' if r.status_code == 200 else r.status_code}")


def scenario_drip_step(hub: Hub, world: World, step: int) -> None:
    """One incident per drip tick, weighted toward mission lines.

    Occasionally resolves an older open incident so the board breathes instead
    of only accumulating red.
    """
    weights = {
        "pl-primary": 4,
        "pl-secondary": 3,
        "pl-infra": 2,
        "pl-admin": 1,
    }
    pool: list[str] = []
    for pl, w in weights.items():
        pool.extend([pl] * w)
    pl = _RNG.choice(pool)
    sev = _RNG.choice(["SEV2", "SEV3", "SEV3", "SEV4"] if pl != "pl-primary"
                       else ["SEV1", "SEV2", "SEV2", "SEV3"])
    candidates = [d for d in world.deployments if d.product_line == pl]
    dep = _RNG.choice(candidates)
    alarm = f"{dep.deployment_id}-{_RNG.choice(['cpu', 'errors', 'latency', 'canary'])}-high"
    cid = _fire_synthetic(hub, dep, sev, alarm)
    if cid:
        print(f"  [drip {step}] {sev:4s} {alarm} on {dep.deployment_id} ({cid[:8]})")

    # Every 4th tick, resolve the oldest open incident to keep the board moving.
    if step % 4 == 0:
        r = hub.get("/incidents")
        if r.status_code == 200 and r.json():
            oldest = r.json()[-1]
            ocid = oldest["correlation_id"]
            hub.post(f"/incidents/{ocid}/resolve", {})
            print(f"  [drip {step}] resolved {oldest['app_name']} ({ocid[:8]})")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def populate_static(hub: Hub, world: World) -> None:
    print("Heartbeating deployments (registering fleet tiles)...")
    n = heartbeat_round(hub, world)
    print(f"  {n}/{len(world.deployments)} tiles registered LIVE")

    print("Seeding contacts...")
    print(f"  {seed_contacts(hub, world)}/{len(world.people)} contacts")

    print("Seeding availability...")
    print(f"  {seed_availability(hub, world)}/{len(world.people)} availability records")

    print("Generating this week's on-call schedule...")
    auto_schedule(hub)

    print("Seeding routing + ignore rules...")
    seed_rules(hub)


def run(args: argparse.Namespace) -> None:
    world = build_world(args.seed)
    hub = Hub(args.base_url)
    try:
        print(f"Relay test-env harness -> {hub.base_url}")
        hub.wait_healthy()
        populate_static(hub, world)

        if args.once:
            print("Firing one-shot incident burst...")
            scenario_burst(hub, world)
            print(f"\nDone. Open {hub.base_url}/ to view the board.")
            print("Note: tiles stay LIVE ~2 min without --once's single heartbeat; "
                  "use drip mode (default) for a continuously-live demo.")
            return

        # Drip mode: keep heartbeating (tiles stay green) and trickle incidents.
        print(f"Entering drip mode (incident every {args.interval}s, "
              f"heartbeat every {args.heartbeat_interval}s). Ctrl-C to stop.")
        print(f"Open {hub.base_url}/ to watch the board evolve.")
        scenario_burst(hub, world)  # seed an interesting starting state
        step = 0
        last_hb = time.monotonic()
        while True:
            time.sleep(args.interval)
            now = time.monotonic()
            if now - last_hb >= args.heartbeat_interval:
                heartbeat_round(hub, world)
                last_hb = now
            step += 1
            scenario_drip_step(hub, world, step)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        hub.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Populate + simulate a Relay test environment.")
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--once", action="store_true",
                    help="seed + one incident burst, then exit (no drip loop)")
    ap.add_argument("--interval", type=float, default=20.0,
                    help="seconds between drip incidents (default 20)")
    ap.add_argument("--heartbeat-interval", type=float, default=45.0,
                    help="seconds between heartbeat rounds in drip mode (default 45)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
