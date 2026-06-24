"""Tests for pluggable AI-provider adapters — no real AWS or network calls."""

from __future__ import annotations

import json
import subprocess

import pytest

from relay.adapters.ai import (
    AICompletion,
    BedrockAssistant,
    BedrockConverseAssistant,
    ClaudeCodeAssistant,
    NoOpAIAssistant,
    OpenAICompatAssistant,
    make_assistant,
)

# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestMakeAssistant:
    def test_returns_none_when_ai_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """make_assistant returns None when RELAY_AI_ENABLED is not 'true'."""
        monkeypatch.delenv("RELAY_AI_ENABLED", raising=False)
        assert make_assistant() is None

    def test_returns_none_for_provider_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """make_assistant returns None for explicit provider 'none'."""
        monkeypatch.setenv("RELAY_AI_ENABLED", "true")
        monkeypatch.setenv("RELAY_AI_PROVIDER", "none")
        assert make_assistant() is None

    def test_returns_bedrock_when_enabled_and_provider_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """make_assistant defaults to BedrockAssistant when provider is unset."""
        monkeypatch.setenv("RELAY_AI_ENABLED", "true")
        monkeypatch.delenv("RELAY_AI_PROVIDER", raising=False)
        result = make_assistant()
        assert isinstance(result, BedrockAssistant)

    def test_returns_bedrock_converse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """make_assistant returns BedrockConverseAssistant for 'bedrock-converse'."""
        monkeypatch.setenv("RELAY_AI_ENABLED", "true")
        monkeypatch.setenv("RELAY_AI_PROVIDER", "bedrock-converse")
        result = make_assistant()
        assert isinstance(result, BedrockConverseAssistant)

    def test_returns_openai_compat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """make_assistant returns OpenAICompatAssistant for 'openai'."""
        monkeypatch.setenv("RELAY_AI_ENABLED", "true")
        monkeypatch.setenv("RELAY_AI_PROVIDER", "openai")
        monkeypatch.setenv("RELAY_AI_BASE_URL", "https://api.openai.com/v1")
        monkeypatch.delenv("RELAY_AI_API_KEY_SECRET", raising=False)
        result = make_assistant()
        assert isinstance(result, OpenAICompatAssistant)

    def test_returns_claude_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """make_assistant returns ClaudeCodeAssistant for 'claude-code'."""
        monkeypatch.setenv("RELAY_AI_ENABLED", "true")
        monkeypatch.setenv("RELAY_AI_PROVIDER", "claude-code")
        result = make_assistant()
        assert isinstance(result, ClaudeCodeAssistant)

    def test_returns_none_for_unknown_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """make_assistant returns None and logs a warning for an unknown provider."""
        monkeypatch.setenv("RELAY_AI_ENABLED", "true")
        monkeypatch.setenv("RELAY_AI_PROVIDER", "not-a-real-provider")
        assert make_assistant() is None


# ---------------------------------------------------------------------------
# OpenAICompatAssistant tests
# ---------------------------------------------------------------------------

_OPENAI_SUCCESS_BODY = json.dumps({
    "choices": [{"message": {"content": "Hello from AI!"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
})


class TestOpenAICompatAssistant:
    def test_complete_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """complete() returns AICompletion with correct text/provider/tokens on 200."""
        monkeypatch.setenv("RELAY_AI_MODEL_ID", "gpt-4o")

        def fake_http(url: str, headers: dict, body: str) -> tuple[int, str]:
            return 200, _OPENAI_SUCCESS_BODY

        assistant = OpenAICompatAssistant(
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model_id="gpt-4o",
            http_fn=fake_http,
        )
        result = assistant.complete(system="You are helpful.", prompt="Say hello.")

        assert isinstance(result, AICompletion)
        assert result.text == "Hello from AI!"
        assert result.provider == "openai"
        assert result.model == "gpt-4o"
        assert result.input_tokens == 10
        assert result.output_tokens == 5

    def test_complete_non_2xx_returns_none(self) -> None:
        """complete() returns None on a non-2xx HTTP response."""
        def fake_http(url: str, headers: dict, body: str) -> tuple[int, str]:
            return 500, "Internal Server Error"

        assistant = OpenAICompatAssistant(
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model_id="gpt-4o",
            http_fn=fake_http,
        )
        result = assistant.complete(system="sys", prompt="user")

        assert result is None

    def test_complete_http_fn_raises_returns_none(self) -> None:
        """complete() returns None (never raises) when http_fn raises."""
        def fake_http(url: str, headers: dict, body: str) -> tuple[int, str]:
            raise ConnectionError("network unreachable")

        assistant = OpenAICompatAssistant(
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model_id="gpt-4o",
            http_fn=fake_http,
        )
        result = assistant.complete(system="sys", prompt="user")

        assert result is None

    def test_complete_no_model_id_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """complete() returns None when no model_id is set."""
        monkeypatch.delenv("RELAY_AI_MODEL_ID", raising=False)

        assistant = OpenAICompatAssistant(
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            http_fn=lambda *_: (200, _OPENAI_SUCCESS_BODY),
        )
        result = assistant.complete(system="sys", prompt="user")

        assert result is None

    def test_bearer_token_in_header(self) -> None:
        """complete() includes Authorization: Bearer header when api_key is set."""
        captured: list[dict] = []

        def fake_http(url: str, headers: dict, body: str) -> tuple[int, str]:
            captured.append(headers)
            return 200, _OPENAI_SUCCESS_BODY

        assistant = OpenAICompatAssistant(
            base_url="https://api.openai.com/v1",
            api_key="sk-secret",
            model_id="gpt-4o",
            http_fn=fake_http,
        )
        assistant.complete(system="sys", prompt="user")

        assert captured[0]["Authorization"] == "Bearer sk-secret"

    def test_no_auth_header_without_api_key(self) -> None:
        """complete() omits the Authorization header when api_key is not set."""
        captured: list[dict] = []

        def fake_http(url: str, headers: dict, body: str) -> tuple[int, str]:
            captured.append(headers)
            return 200, _OPENAI_SUCCESS_BODY

        assistant = OpenAICompatAssistant(
            base_url="http://localhost:11434/v1",
            model_id="llama3",
            http_fn=fake_http,
        )
        assistant.complete(system="sys", prompt="user")

        assert "Authorization" not in captured[0]


# ---------------------------------------------------------------------------
# BedrockConverseAssistant tests
# ---------------------------------------------------------------------------

_CONVERSE_SUCCESS_RESP = {
    "output": {
        "message": {
            "content": [{"text": "Hello from Converse!"}],
        }
    },
    "usage": {"inputTokens": 12, "outputTokens": 7},
}


class TestBedrockConverseAssistant:
    def test_complete_success(self) -> None:
        """complete() returns AICompletion with joined text and mapped usage."""

        class FakeConverseClient:
            def converse(self, **kwargs):
                return _CONVERSE_SUCCESS_RESP

        assistant = BedrockConverseAssistant(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            client=FakeConverseClient(),
        )
        result = assistant.complete(system="sys", prompt="user")

        assert isinstance(result, AICompletion)
        assert result.text == "Hello from Converse!"
        assert result.provider == "bedrock-converse"
        assert result.input_tokens == 12
        assert result.output_tokens == 7

    def test_complete_multiple_content_blocks_joined(self) -> None:
        """complete() joins multiple text blocks in the response."""

        class FakeClient:
            def converse(self, **kwargs):
                return {
                    "output": {
                        "message": {
                            "content": [{"text": "Hello "}, {"text": "world"}],
                        }
                    },
                    "usage": {},
                }

        assistant = BedrockConverseAssistant(client=FakeClient())
        result = assistant.complete(system="sys", prompt="user")

        assert result is not None
        assert result.text == "Hello world"

    def test_complete_client_raises_returns_none(self) -> None:
        """complete() returns None (never raises) when the client raises."""

        class FailingClient:
            def converse(self, **kwargs):
                raise RuntimeError("throttled")

        assistant = BedrockConverseAssistant(client=FailingClient())
        result = assistant.complete(system="sys", prompt="user")

        assert result is None

    def test_noop_returns_none(self) -> None:
        """NoOpAIAssistant always returns None."""
        noop = NoOpAIAssistant()
        assert noop.complete(system="s", prompt="p") is None


# ---------------------------------------------------------------------------
# ClaudeCodeAssistant tests (headless agent loop — hermetic via injected run_fn)
# ---------------------------------------------------------------------------


class TestClaudeCodeAssistant:
    def test_success_returns_completion(self) -> None:
        """A 0-exit run returns its stdout as the completion text."""
        captured = {}

        def fake_run(cmd, stdin, timeout):
            captured["cmd"] = cmd
            captured["stdin"] = stdin
            return 0, "## Findings\nroot cause: bad deploy\n", ""

        a = ClaudeCodeAssistant(run_fn=fake_run, skills_dir="/app/skills", model="")
        out = a.complete(system="You are an SRE.", prompt="Investigate c-1")
        assert isinstance(out, AICompletion)
        assert out.provider == "claude-code"
        assert "root cause" in out.text
        # system prompt is folded into the piped stdin
        assert "You are an SRE." in captured["stdin"]
        assert "Investigate c-1" in captured["stdin"]
        # read-only invocation: headless print mode + allow-list + skills dir
        assert "-p" in captured["cmd"]
        assert "--allowed-tools" in captured["cmd"]
        assert "/app/skills" in captured["cmd"]

    def test_nonzero_exit_returns_none(self) -> None:
        """A non-zero exit degrades to None (deterministic fallback upstream)."""
        a = ClaudeCodeAssistant(run_fn=lambda c, s, t: (1, "", "boom"))
        assert a.complete(system="s", prompt="p") is None

    def test_empty_stdout_returns_completion_with_none_text(self) -> None:
        """Empty output yields AICompletion(text=None), not a crash."""
        a = ClaudeCodeAssistant(run_fn=lambda c, s, t: (0, "   ", ""))
        out = a.complete(system="s", prompt="p")
        assert isinstance(out, AICompletion)
        assert out.text is None

    def test_timeout_returns_none(self) -> None:
        """A timed-out investigation returns None, never raises."""

        def fake_run(cmd, stdin, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)

        a = ClaudeCodeAssistant(run_fn=fake_run)
        assert a.complete(system="s", prompt="p") is None

    def test_run_fn_exception_returns_none(self) -> None:
        """Any unexpected error degrades to None (never raises)."""

        def fake_run(cmd, stdin, timeout):
            raise RuntimeError("unexpected")

        a = ClaudeCodeAssistant(run_fn=fake_run)
        assert a.complete(system="s", prompt="p") is None

    def test_missing_binary_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the claude binary isn't on PATH (and no run_fn), returns None."""
        monkeypatch.setattr("shutil.which", lambda _b: None)
        a = ClaudeCodeAssistant(binary="definitely-not-a-real-binary")
        assert a.complete(system="s", prompt="p") is None
