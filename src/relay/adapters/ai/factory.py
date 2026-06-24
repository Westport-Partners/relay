"""Factory for creating an :class:`~relay.adapters.base.AIAssistant` from environment variables.

Controlled by two env vars:

- ``RELAY_AI_ENABLED`` — must be ``"true"`` (case-insensitive) to activate AI;
  all other values (including unset) return ``None`` immediately.
- ``RELAY_AI_PROVIDER`` — selects the backend when AI is enabled:
  ``"bedrock"`` (default when unset), ``"bedrock-converse"``, ``"openai"``,
  ``"claude-code"`` (headless agent loop + skill pack), ``"none"``/``""``
  (explicit disable).

All construction errors are swallowed; the factory never raises.
"""

from __future__ import annotations

import logging
import os

from relay.adapters.ai.bedrock_assistant import BedrockAssistant
from relay.adapters.ai.bedrock_converse import BedrockConverseAssistant
from relay.adapters.ai.claude_code_assistant import ClaudeCodeAssistant
from relay.adapters.ai.openai_compat import OpenAICompatAssistant
from relay.adapters.base import AIAssistant

logger = logging.getLogger(__name__)


def _load_key() -> str | None:
    """Load the AI API key from Secrets Manager or fall back to env var.

    Checks ``RELAY_AI_API_KEY_SECRET`` (a Secrets Manager secret *name*) first.
    Falls back to the plaintext ``RELAY_AI_API_KEY`` env var (dev use only).

    Returns:
        The key string, or ``None`` when unavailable.
    """
    secret_name = os.environ.get("RELAY_AI_API_KEY_SECRET", "").strip()
    if secret_name:
        try:
            import boto3

            client = boto3.client("secretsmanager")
            response = client.get_secret_value(SecretId=secret_name)
            return response.get("SecretString")
        except Exception:
            logger.warning(
                "factory._load_key: failed to fetch secret %r from Secrets Manager;"
                " falling back to RELAY_AI_API_KEY env var",
                secret_name,
                exc_info=True,
            )

    return os.environ.get("RELAY_AI_API_KEY")


def make_assistant() -> AIAssistant | None:
    """Build an :class:`~relay.adapters.base.AIAssistant` from environment configuration.

    Returns:
        A concrete assistant, or ``None`` when AI is disabled or the provider
        is unknown / construction fails.
    """
    if os.environ.get("RELAY_AI_ENABLED", "").strip().lower() != "true":
        return None

    provider = os.environ.get("RELAY_AI_PROVIDER", "").strip().lower()
    # Default to bedrock when RELAY_AI_ENABLED=true and RELAY_AI_PROVIDER is unset
    if not provider:
        provider = "bedrock"

    if provider in {"", "none"}:
        return None

    try:
        if provider == "bedrock":
            return BedrockAssistant()
        if provider == "bedrock-converse":
            return BedrockConverseAssistant()
        if provider == "openai":
            return OpenAICompatAssistant(
                base_url=os.environ.get("RELAY_AI_BASE_URL"),
                api_key=_load_key(),
            )
        if provider == "claude-code":
            return ClaudeCodeAssistant()
        logger.warning("unknown RELAY_AI_PROVIDER=%r; AI disabled", provider)
        return None
    except Exception:
        logger.warning(
            "make_assistant: failed to construct assistant for provider %r",
            provider,
            exc_info=True,
        )
        return None


__all__ = ["make_assistant"]
