"""Skeleton adapter — copy this folder to ``<name>/`` to start a new adapter.

The leading underscore means the registry SKIPS this package (it is a template,
not a live adapter). See ``src/relay/adapters/README.md`` for the contract.
"""

from __future__ import annotations

from relay.adapters.integrations._template.adapter import MANIFEST, build
from relay.adapters.integrations._template.listener import TemplateListener

__all__ = ["MANIFEST", "TemplateListener", "build"]
