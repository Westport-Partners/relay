"""AIAssistant implementation using the Bedrock Runtime ``converse`` API.

The Converse API normalises ALL Bedrock models (Claude, Llama, Mistral, Titan)
behind a single request/response schema, making it the preferred path for
multi-model deployments.  Select the model via ``RELAY_AI_MODEL_ID``; the same
cross-region inference profile id used by :class:`BedrockAssistant` works here.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from relay.adapters.base import AICompletion

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


class BedrockConverseAssistant:
    """Single-shot completions via the Bedrock Runtime Converse API.

    Normalises any Bedrock-hosted model behind one schema.
    """

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
        """Run one completion via the Converse API.

        Returns :class:`AICompletion` or ``None`` on any failure (never raises).
        """
        try:
            resp = self._bedrock().converse(
                modelId=self._model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens},
            )
            # Converse response: output.message.content is a list of {"text": "..."} blocks
            content_blocks = resp.get("output", {}).get("message", {}).get("content", [])
            text = "".join(block.get("text", "") for block in content_blocks)
            usage = resp.get("usage", {})
            return AICompletion(
                text=text.strip() or None,
                model=self._model_id,
                provider="bedrock-converse",
                input_tokens=usage.get("inputTokens"),
                output_tokens=usage.get("outputTokens"),
            )
        except Exception:
            logger.warning(
                "BedrockConverseAssistant.complete failed; falling back", exc_info=True
            )
            return None


__all__ = ["BedrockConverseAssistant"]
