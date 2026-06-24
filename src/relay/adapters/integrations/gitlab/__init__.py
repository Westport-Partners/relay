"""GitLab incident adapter (issues + DORA).

Public surface: the sink (HTTP client + ``from_env``/``test_token``), the
listener, and the registry ``MANIFEST``.
"""

from __future__ import annotations

from relay.adapters.integrations.gitlab.adapter import MANIFEST, build
from relay.adapters.integrations.gitlab.listener import GitLabListener
from relay.adapters.integrations.gitlab.sink import GitLabConfig, GitLabSink

__all__ = ["MANIFEST", "GitLabConfig", "GitLabSink", "GitLabListener", "build"]
