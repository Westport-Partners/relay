"""AIAssistant implementation that speaks the OpenAI Chat Completions wire protocol.

A single adapter covers OpenAI, Azure OpenAI, Gemini's OpenAI-compatible
endpoint, OpenRouter, and local/self-hosted Ollama/vLLM deployments.  Point it
at any base URL that exposes ``POST /chat/completions`` and it will work.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from relay.adapters.base import AICompletion

logger = logging.getLogger(__name__)


class OpenAICompatAssistant:
    """Single-shot completions via the OpenAI Chat Completions API (or compatible).

    Works with OpenAI, Azure OpenAI, Gemini OpenAI-compat, OpenRouter, Ollama,
    and vLLM — any endpoint that accepts ``POST /chat/completions`` with the
    standard JSON schema.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model_id: str | None = None,
        http_fn: Any | None = None,
    ) -> None:
        raw_url = base_url or os.environ.get("RELAY_AI_BASE_URL", "")
        self._base_url = raw_url.rstrip("/")
        self._api_key = api_key or os.environ.get("RELAY_AI_API_KEY")
        self._model_id = model_id or os.environ.get("RELAY_AI_MODEL_ID", "")
        self._http_fn = http_fn  # injectable for tests; None -> use urllib

    def _do_request(self, url: str, headers: dict[str, str], body: str) -> tuple[int, str]:
        """Execute an HTTP POST using stdlib urllib.

        Args:
            url:     Full request URL.
            headers: Request headers dict.
            body:    JSON-encoded request body string.

        Returns:
            Tuple of ``(status_code, response_text)``.  On ``HTTPError`` returns
            ``(exc.code, "")`` rather than raising.
        """
        data = body.encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, ""

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 1024
    ) -> AICompletion | None:
        """Run one completion. Returns :class:`AICompletion` or ``None`` on any failure (never raises)."""
        try:
            if not self._model_id:
                logger.warning(
                    "OpenAICompatAssistant: no model_id configured"
                    " (set RELAY_AI_MODEL_ID or pass model_id=); AI disabled"
                )
                return None

            url = f"{self._base_url}/chat/completions"
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            body = json.dumps({
                "model": self._model_id,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            })

            if self._http_fn is not None:
                status, response_text = self._http_fn(url, headers, body)
            else:
                status, response_text = self._do_request(url, headers, body)

            if not (200 <= status < 300):
                logger.warning(
                    "OpenAICompatAssistant.complete received HTTP %s", status
                )
                return None

            parsed = json.loads(response_text)
            text = parsed["choices"][0]["message"]["content"]
            usage = parsed.get("usage", {})
            return AICompletion(
                text=text.strip() or None,
                model=self._model_id,
                provider="openai",
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        except Exception:
            logger.warning(
                "OpenAICompatAssistant.complete failed; falling back", exc_info=True
            )
            return None


__all__ = ["OpenAICompatAssistant"]
