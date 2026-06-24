"""Discoverable lifecycle-integration adapters.

Every package in this folder that exposes a module-level ``MANIFEST`` (in its
``adapter.py``) is auto-discovered by ``relay.adapters.registry`` and wired into
the Hub — no Hub edit needed. Drop a new adapter folder here to add an
integration; see ``README.md`` for the contract and ``_template/`` for a
skeleton.

This is the ONLY folder the registry scans. The sibling ``aws/`` (platform
substrate) and ``ai/`` (AI providers) packages are not lifecycle adapters and
live outside it.
"""
