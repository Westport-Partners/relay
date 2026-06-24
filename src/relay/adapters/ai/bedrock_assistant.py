"""AIAssistant implementation backed by Amazon Bedrock (single-shot completions).

Keeps incident data inside AWS (docs/AI.md §5 data-sensitivity). Used by the
Hub to draft incident briefings and after-action reports. Degrades gracefully:
any failure (no creds, model error, throttle, over budget) returns None so the
caller falls back to deterministic output — AI never gates incident handling.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from relay.adapters.base import AICompletion

logger = logging.getLogger(__name__)

# Default to a fast, inexpensive Claude model on Bedrock; override per deploy
# via RELAY_AI_MODEL_ID. Modern Claude models on Bedrock are invoked through a
# cross-region INFERENCE PROFILE id (the "us." prefix), not the raw model id —
# raw on-demand ids for older models are now legacy/EOL and return
# ResourceNotFoundException.
_DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


class BedrockAssistant:
    """Single-shot completions via the Bedrock Anthropic Messages API."""

    def __init__(
        self,
        model_id: str | None = None,
        region: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._model_id = model_id or os.environ.get("RELAY_AI_MODEL_ID", _DEFAULT_MODEL)
        self._region = region or os.environ.get("AWS_REGION") or "us-east-1"
        self._client = client  # lazy-created on first use unless injected

    def _bedrock(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 1024
    ) -> AICompletion | None:
        """Run one completion. Returns AICompletion or None on any failure (never raises)."""
        try:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            })
            resp = self._bedrock().invoke_model(modelId=self._model_id, body=body)
            payload = json.loads(resp["body"].read())
            # Anthropic Messages API: {"content": [{"type":"text","text": "..."}]}
            parts = payload.get("content", [])
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            usage = payload.get("usage", {})
            return AICompletion(
                text=text.strip() or None,
                model=self._model_id,
                provider="bedrock",
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        except Exception:
            logger.warning("BedrockAssistant.complete failed; falling back", exc_info=True)
            return None


class NoOpAIAssistant:
    """An assistant that always declines — forces deterministic fallback."""

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 1024
    ) -> AICompletion | None:
        return None


__all__ = ["BedrockAssistant", "NoOpAIAssistant"]
