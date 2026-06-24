"""Pluggable AI-provider adapters for Relay.

Re-exports the public surface so callers only need to import from
``relay.adapters.ai``.  Use :func:`make_assistant` for the standard
factory-from-environment path.
"""

from __future__ import annotations

from relay.adapters.ai.bedrock_assistant import BedrockAssistant, NoOpAIAssistant
from relay.adapters.ai.bedrock_converse import BedrockConverseAssistant
from relay.adapters.ai.claude_code_assistant import ClaudeCodeAssistant
from relay.adapters.ai.factory import make_assistant
from relay.adapters.ai.openai_compat import OpenAICompatAssistant
from relay.adapters.base import AICompletion

__all__ = [
    "AICompletion",
    "BedrockAssistant",
    "BedrockConverseAssistant",
    "ClaudeCodeAssistant",
    "NoOpAIAssistant",
    "OpenAICompatAssistant",
    "make_assistant",
]
