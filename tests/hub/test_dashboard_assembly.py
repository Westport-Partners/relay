"""Tests for dashboard assembly, flow endpoint, and tile-drift-repair.

Covers:
  1. Dashboard fragment assembly (manifest, HTML shell, ES module wiring)
  2. GET /incidents/{id}/flow — process-flow endpoint
  3. Tile open_incident_count drift: resolve endpoint + sweep reconciliation
"""

from __future__ import annotations

import threading
import threading as _threading  # noqa: F811 (explicit alias used below)
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import boto3
import pytest

# ---------------------------------------------------------------------------
# moto setup
# ---------------------------------------------------------------------------

try:
    from moto import mock_aws

    _HAS_MOTO = True
except ImportError:
    _HAS_MOTO = False

pytestmark = pytest.mark.skipif(not _HAS_MOTO, reason="moto not installed")

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from relay.core.model import (
    EscalationPolicy,
    EscalationStep,
    Incident,
    IncidentState,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)
from relay.hub.app import HubApp, HubState, SSEPublisher, SweepTimer
from relay.hub.fleet_store import FleetStore
from relay.hub.health import (
    FleetTile,
)

try:
    from fastapi.testclient import TestClient as _TestClient
except ImportError:
    _TestClient = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Fake clock helper (duplicated from test_hub_dashboard.py — no shared module)
# ---------------------------------------------------------------------------


class FakeClock:
    """Controllable clock for deterministic tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now: datetime = start or datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TABLE_NAME = "relay-fleet-test"


@pytest.fixture
def aws_session():
    """Return a moto-mocked boto3 session with the fleet table created."""
    with mock_aws():
        session = boto3.session.Session(region_name="us-east-1")
        ddb = session.resource("dynamodb")
        ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield session


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fleet_store(aws_session, clock) -> FleetStore:
    return FleetStore(
        table_name=TABLE_NAME,
        boto3_session=aws_session,
        clock=clock,
    )


@pytest.fixture
def hub_state(fleet_store, clock) -> HubState:
    return HubState(fleet_store=fleet_store, clock=clock)


# ===========================================================================
# Dashboard fragment assembly (#28 phase 2)
# ===========================================================================


class TestDashboardAssembly:
    """The dashboard markup/CSS is authored as ordered fragments under
    dashboard_parts/ and assembled at serve time; behavior is authored as native
    ES modules under dashboard_modules/ and served read-only at /static/dashboard/.
    These lock the contract: the manifest's fragments exist, the assembled shell
    is a single well-formed HTML page with one <style> pair and one module-script
    tag (no inline JS), the entry module exists, and every relative import between
    modules resolves to a real exported symbol."""

    def test_manifest_and_named_fragments_exist(self):
        from relay.hub.app import _DASHBOARD_PARTS_DIR

        manifest = _DASHBOARD_PARTS_DIR / "manifest.txt"
        assert manifest.is_file(), "dashboard_parts/manifest.txt must exist"
        names = [
            ln.strip()
            for ln in manifest.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        assert names, "manifest lists no fragments"
        for name in names:
            assert (_DASHBOARD_PARTS_DIR / name).is_file(), f"missing fragment: {name}"

    def test_assembled_html_is_well_formed_single_document(self):
        from relay.hub.app import _render_dashboard_html

        html = _render_dashboard_html()
        # The shell carries no inline JS — behavior loads as ES modules. There is
        # exactly one <style> pair and exactly one module script tag pointing at
        # the static entry module; no bare inline <script> remains.
        assert html.count("<script>") == 0, "no inline <script> — JS is ES modules"
        assert html.count('<script type="module"') == 1
        assert html.count("</script>") == 1
        assert '/static/dashboard/main.js' in html
        assert html.count("<style>") == 1
        assert html.count("</style>") == 1
        assert html.lstrip().startswith("<!"), "must start with a doctype"
        assert "</html>" in html
        # Substantial — guards against a truncated/empty assembly (CSS-dominated
        # now that the JS lives in modules).
        assert len(html) > 30_000

    def test_assembly_is_concatenation_in_manifest_order(self):
        from relay.hub.app import _DASHBOARD_PARTS_DIR, _render_dashboard_html

        names = [
            ln.strip()
            for ln in (_DASHBOARD_PARTS_DIR / "manifest.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        expected = "".join(
            (_DASHBOARD_PARTS_DIR / name).read_text(encoding="utf-8") for name in names
        )
        assert _render_dashboard_html() == expected

    def test_module_dir_and_entry_exist(self):
        from relay.hub.app import _DASHBOARD_MODULES_DIR

        assert _DASHBOARD_MODULES_DIR.is_dir(), "dashboard_modules/ must ship in the package"
        assert (_DASHBOARD_MODULES_DIR / "main.js").is_file(), "entry module main.js missing"

    def test_module_imports_resolve_to_real_exports(self):
        """Every `import { … } from './x.js'` must target a sibling module that
        actually exports each named symbol — catches a broken refactor that would
        only surface as a runtime error in the browser."""
        import re

        from relay.hub.app import _DASHBOARD_MODULES_DIR

        mods = {p.name: p.read_text(encoding="utf-8") for p in _DASHBOARD_MODULES_DIR.glob("*.js")}
        assert mods, "no ES modules found"

        def exported_names(text: str) -> set[str]:
            names: set[str] = set()
            for m in re.finditer(
                r"^export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z0-9_]+)",
                text,
                re.M,
            ):
                names.add(m.group(1))
            for m in re.finditer(r"^export\s*\{([^}]*)\}", text, re.M):
                for part in m.group(1).split(","):
                    nm = part.strip().split(" as ")[-1].strip()
                    if nm:
                        names.add(nm)
            return names

        exports = {name: exported_names(text) for name, text in mods.items()}

        problems: list[str] = []
        for name, text in mods.items():
            for m in re.finditer(r"import\s*\{([^}]*)\}\s*from\s*'\./([^']+)'", text):
                syms = [s.strip().split(" as ")[0].strip() for s in m.group(1).split(",") if s.strip()]
                target = m.group(2)
                if target not in mods:
                    problems.append(f"{name}: imports from missing module {target}")
                    continue
                for s in syms:
                    if s not in exports[target]:
                        problems.append(f"{name}: imports {{{s}}} from {target}, which does not export it")
        assert not problems, "broken ES-module imports:\n" + "\n".join(problems)


# ===========================================================================
# GET /incidents/{id}/flow  — process-flow endpoint (issue #20)
# ===========================================================================

_FLOW_T0 = datetime(2026, 6, 2, 8, 0, 0, tzinfo=UTC)


def _fev(
    cid: str,
    step_index: int,
    occurred_at: datetime,
    contact_ids: list[str] | None = None,
    event_id: str | None = None,
) -> TimelineEvent:
    """Build an escalation.page_sent TimelineEvent."""
    return TimelineEvent(
        event_id=event_id or f"fev-{step_index}",
        incident_id=cid,
        stream=Stream.TEAM,
        occurred_at=occurred_at,
        actor="system",
        event_type="escalation.page_sent",
        detail={
            "step_index": step_index,
            "contact_ids": contact_ids or [],
            "roles": [],
            "streams": ["TEAM"],
            "timeout_minutes": 5,
        },
    )


def _flow_incident(
    cid: str = "flow-inc-001",
    timeline: list[Any] | None = None,
    escalation_policy_id: str | None = None,
) -> Incident:
    return Incident(
        correlation_id=cid,
        account_id="123456789012",
        region="us-east-1",
        app_name="svc",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="test-alarm",
        state=IncidentState.TRIGGERED,
        timeline=timeline or [],
        escalation_policy_id=escalation_policy_id,
    )


class _FlowFakeIncidentStore:
    def __init__(self, incidents: list[Incident]) -> None:
        self._db = {i.correlation_id: i for i in incidents}

    def get_incident(self, cid: str) -> Incident | None:
        return self._db.get(cid)

    def list_open_incidents(self, account_id: str | None = None) -> list[Incident]:
        # Mirror the real store: only non-terminal incidents are "open".
        terminal = {IncidentState.RESOLVED, IncidentState.CLOSED}
        return [i for i in self._db.values() if i.state not in terminal]

    def list_incidents(self) -> list[Incident]:
        return list(self._db.values())

    def put_incident(self, inc: Incident) -> None:
        self._db[inc.correlation_id] = inc


class _FlowFakeContactStore:
    def __init__(self, contacts: dict[str, str]) -> None:
        # contacts is contact_id -> name
        from relay.core.model import Contact
        self._db = [
            Contact(contact_id=cid, name=name, email=f"{cid}@example.com")
            for cid, name in contacts.items()
        ]

    def list_contacts(self) -> list[Any]:
        return list(self._db)


def _flow_policy(policy_id: str = "flow-pol-1") -> EscalationPolicy:
    return EscalationPolicy(
        policy_id=policy_id,
        name="Flow Policy",
        team="team-flow",
        steps=[
            EscalationStep(
                step_index=0,
                contact_ids=["fc1"],
                timeout_minutes=5,
                notify_streams=[Stream.TEAM],
            ),
            EscalationStep(
                step_index=1,
                contact_ids=["fc2"],
                timeout_minutes=10,
                notify_streams=[Stream.TEAM],
            ),
        ],
    )


def _flow_client(
    incident: Incident | None = None,
    hub_config: object | None = None,
    contact_store: object | None = None,
) -> _TestClient:
    """Build a minimal HubApp TestClient wired for /incidents/{id}/flow tests."""
    if _TestClient is None:
        pytest.skip("fastapi/httpx not installed")

    store = _FlowFakeIncidentStore([incident] if incident is not None else [])

    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = store
    app_obj._contact_store = contact_store
    app_obj._config = hub_config
    app_obj._schedule_store = None
    app_obj._settings_store = None
    app_obj._notifier = None
    app_obj._paging_topic_arn = None

    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = _threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()

    return _TestClient(app_obj.build_fastapi_app())


class TestFlowEndpoint:
    """GET /incidents/{id}/flow — process-flow endpoint."""

    def test_404_for_unknown_incident(self):
        c = _flow_client()
        r = c.get("/incidents/no-such-id/flow")
        assert r.status_code == 404

    def test_config_backed_source(self):
        """Config has a matching policy → source=='config', expected_steps from policy."""
        policy = _flow_policy("flow-pol-1")
        # Fake hub config with escalation.policies
        hub_config = SimpleNamespace(
            escalation=SimpleNamespace(policies=[policy]),
            routing=None,
        )
        timeline = [_fev("fci-config", 0, _FLOW_T0, contact_ids=["fc1"])]
        inc = _flow_incident(
            "fci-config",
            timeline=timeline,
            escalation_policy_id="flow-pol-1",
        )
        c = _flow_client(
            incident=inc,
            hub_config=hub_config,
            contact_store=_FlowFakeContactStore({"fc1": "Alice", "fc2": "Bob"}),
        )
        r = c.get("/incidents/fci-config/flow")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "config"
        assert len(body["expected_steps"]) == 2
        assert body["expected_steps"][0]["reached"] is True
        assert body["expected_steps"][1]["reached"] is False
        assert body["fallback"] is False

    def test_derived_when_no_config_escalation(self):
        """No policy in config → source=='derived' (ladder inferred from page_sent events)."""
        timeline = [
            _fev("fci-derived", 0, _FLOW_T0, contact_ids=["fc1"]),
            _fev("fci-derived", 1, _FLOW_T0 + timedelta(seconds=60), contact_ids=["fc2"]),
        ]
        inc = _flow_incident("fci-derived", timeline=timeline)
        # Config with no escalation attr → policy lookup is skipped
        c = _flow_client(
            incident=inc,
            hub_config=None,
            contact_store=_FlowFakeContactStore({"fc1": "Alice", "fc2": "Bob"}),
        )
        r = c.get("/incidents/fci-derived/flow")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "derived"
        assert len(body["expected_steps"]) == 2
        assert all(s["reached"] for s in body["expected_steps"])
        assert body["fallback"] is False

    def test_none_fallback_no_policy_no_page_sent(self):
        """No policy + no page_sent events → source=='none', fallback True."""
        inc = _flow_incident("fci-none")
        c = _flow_client(incident=inc, hub_config=None)
        r = c.get("/incidents/fci-none/flow")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "none"
        assert body["expected_steps"] == []
        assert body["fallback"] is True

    def test_policy_id_from_triggered_event(self):
        """incident.escalation_policy_id is None but triggered event carries policy_id."""
        policy = _flow_policy("flow-pol-trig")
        hub_config = SimpleNamespace(
            escalation=SimpleNamespace(policies=[policy]),
            routing=None,
        )
        timeline = [
            TimelineEvent(
                event_id="trig-1",
                incident_id="fci-trig",
                stream=Stream.TEAM,
                occurred_at=_FLOW_T0,
                actor="system",
                event_type="incident.triggered",
                detail={"policy_id": "flow-pol-trig", "alarm_name": "alarm"},
            ),
            _fev("fci-trig", 0, _FLOW_T0 + timedelta(seconds=5), contact_ids=["fc1"]),
        ]
        inc = _flow_incident(
            "fci-trig",
            timeline=timeline,
            escalation_policy_id=None,  # the field is None
        )
        c = _flow_client(
            incident=inc,
            hub_config=hub_config,
            contact_store=_FlowFakeContactStore({"fc1": "Alice", "fc2": "Bob"}),
        )
        r = c.get("/incidents/fci-trig/flow")
        assert r.status_code == 200
        body = r.json()
        # Route resolved the policy from the triggered event → config source
        assert body["source"] == "config"
        assert body["policy_id"] == "flow-pol-trig"


# ===========================================================================
# 10. Tile open_incident_count drift: resolve endpoint + sweep reconciliation
# ===========================================================================


class TestTileDriftRepair:
    """Derive-and-self-heal approach: resolve/ack/ignore endpoints and sweep
    reconciliation must never leave open_incident_count drifted on a tile."""

    def _make_open_incident(
        self,
        correlation_id: str = "drift-inc-001",
        account_id: str = "123456789012",
        app_name: str = "drift-app",
        environment: str = "unrouted",
        deployment_id: str = "drift-app",
        state: IncidentState = IncidentState.TRIGGERED,
    ) -> Incident:
        now = datetime.now(UTC)
        return Incident(
            correlation_id=correlation_id,
            account_id=account_id,
            region="us-east-1",
            app_name=app_name,
            environment=environment,
            deployment_id=deployment_id,
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="drift-alarm",
            state=state,
            created_at=now,
            updated_at=now,
        )

    def _build_client_and_sse(
        self,
        incidents: list[Incident],
        hub_state: HubState,
    ) -> tuple[_TestClient, SSEPublisher]:
        if _TestClient is None:
            pytest.skip("fastapi/httpx not installed")

        store = _FlowFakeIncidentStore(incidents)

        app_obj = HubApp.__new__(HubApp)
        app_obj._incident_store = store
        app_obj._contact_store = None
        app_obj._config = None
        app_obj._schedule_store = None
        app_obj._settings_store = None
        app_obj._notifier = None
        app_obj._paging_topic_arn = None
        app_obj._hub_state = hub_state
        pub = SSEPublisher()
        app_obj._sse_publisher = pub

        return _TestClient(app_obj.build_fastapi_app()), pub

    def test_resolve_endpoint_recomputes_tile_count(
        self, fleet_store, hub_state, clock, monkeypatch
    ):
        """Resolving an incident via /incidents/{id}/resolve must decrement the
        tile's open_incident_count to the correct derived value and emit an SSE
        delta — proving the tile never stays phantom-red after a UI resolve."""
        # dev auth gives the endpoint a fixed writer identity so the write path
        # actually runs (otherwise require_writer 403s and the recompute is
        # never reached — a vacuous pass).
        monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
        monkeypatch.setenv("RELAY_DEV_USER", "tester")

        # Prime fleet tile with one open incident via the ingest path.
        inc = self._make_open_incident(state=IncidentState.TRIGGERED)
        hub_state.update_app(inc)
        tile_before = hub_state.get_tile(inc.account_id, inc.app_name)
        assert tile_before is not None
        assert tile_before.open_incidents == 1

        # Transition incident to RESOLVED in the fake store (simulating what the
        # endpoint will do via put_incident).
        resolved = self._make_open_incident(state=IncidentState.RESOLVED)

        # Build a fake store that starts with the resolved incident so that
        # list_open_incidents() returns zero open after the resolve.
        store = _FlowFakeIncidentStore([resolved])

        app_obj = HubApp.__new__(HubApp)
        app_obj._incident_store = store
        app_obj._contact_store = None
        app_obj._config = None
        app_obj._schedule_store = None
        app_obj._settings_store = None
        app_obj._notifier = None
        app_obj._paging_topic_arn = None
        app_obj._hub_state = hub_state
        pub = SSEPublisher()
        app_obj._sse_publisher = pub

        client = _TestClient(app_obj.build_fastapi_app())
        q = pub.subscribe()

        # Call the resolve endpoint — must succeed under dev auth.
        r = client.post("/incidents/drift-inc-001/resolve")
        assert r.status_code == 200, r.text

        # Tile must now reflect zero open incidents.
        tile_after = hub_state.get_tile(inc.account_id, inc.app_name)
        assert tile_after is not None
        assert tile_after.open_incidents == 0

        # An SSE delta must have been emitted.
        assert not q.empty()
        msg = q.get_nowait()
        assert "delta" in msg

    def test_sweep_reconciles_drifted_tile_count(self, fleet_store, hub_state, clock):
        """Sweep reconciliation must correct a tile whose open_incident_count is
        artificially high (simulating drift from UI writes that bypassed the ingest
        bus decrement)."""
        # Register a heartbeat so the tile exists in DynamoDB.
        hb_ts = clock()
        fleet_store.record_heartbeat("drift-acct", "drift-svc", hb_ts)

        # Apply one TRIGGERED incident to get the tile into the cache with count=1.
        inc_open = Incident(
            correlation_id="sweep-drift-001",
            account_id="drift-acct",
            region="us-east-1",
            app_name="drift-svc",
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="sweep-alarm",
            state=IncidentState.TRIGGERED,
            created_at=clock(),
            updated_at=clock(),
        )
        hub_state.update_app(inc_open)

        # Artificially inflate the cached tile count to 5 (simulating drift).
        with hub_state.lock:
            key = list(hub_state._tiles.keys())[0]
            original = hub_state._tiles[key]
            drifted = FleetTile(
                account_id=original.account_id,
                app_name=original.app_name,
                environment=original.environment,
                deployment_id=original.deployment_id,
                service_path=original.service_path,
                org_path=original.org_path,
                metadata=original.metadata,
                on_call=original.on_call,
                status="red",
                liveness=original.liveness,
                open_incidents=5,
                worst_severity=Severity.SEV2,
                last_heartbeat_at=original.last_heartbeat_at,
                registered_at=original.registered_at,
                last_updated=clock(),
            )
            hub_state._tiles[key] = drifted

        # Verify the inflation is in place.
        assert hub_state.cached_tile(key).open_incidents == 5

        # Build a fake incident store that reports only the one real open incident.
        incident_store_fake = _FlowFakeIncidentStore([inc_open])

        pub = SSEPublisher()
        q = pub.subscribe()
        shutdown = threading.Event()

        sweep = SweepTimer(
            hub_state=hub_state,
            sse_publisher=pub,
            shutdown_event=shutdown,
            sweep_interval=0,
            ping_interval=9999,
            incident_store=incident_store_fake,
        )

        # Run one sweep — reconciliation must fire.
        sweep._do_sweep()

        # After sweep the tile count must match the actual open incident count.
        tile_after = hub_state.cached_tile(key)
        assert tile_after is not None
        assert tile_after.open_incidents == 1

        # An SSE delta must have been emitted for the corrected tile.
        assert not q.empty()
        msg = q.get_nowait()
        assert "delta" in msg
