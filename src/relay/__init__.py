"""Relay — a lightweight, self-hosted replacement for AWS Incident Manager.

Relay provides on-call scheduling, alert routing, escalation policies, and
incident lifecycle management without vendor lock-in.  It is composed of two
cooperating processes:

* **relay-node** – a lightweight agent that runs close to your workloads,
  receives alerts from monitoring systems, and forwards enriched events to the
  hub.
* **relay-hub** – the central coordinator that applies escalation policies,
  manages on-call schedules, and exposes a REST API for dashboards and
  integrations.
"""

__version__: str = "0.1.0"
