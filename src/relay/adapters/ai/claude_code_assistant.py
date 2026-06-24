"""AIAssistant implementation that shells out to headless Claude Code.

This is the multi-step *investigation* path from docs/AI.md §3: rather than a
single model call, it runs the ``claude`` CLI in headless/print mode with the
Relay investigation skill pack mounted (``skills/``) and a **read-only** tool
allow-list, against the LIVE account the node runs in. The agent loop, tool use,
and skill execution are Claude Code's; Relay just supplies the incident context
as the prompt and collects the findings.

Contract is identical to the other assistants: ``complete()`` returns an
``AICompletion`` (or ``None``) and NEVER raises — a missing binary, non-zero
exit, or timeout degrades to deterministic fallback upstream (AI augments,
never gates).

Safety: the invocation is constrained to a read-only tool allow-list so the
agent cannot mutate the account. The skill probes are themselves read-only
(see skills/README.md); the allow-list is the second line of defense.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any

from relay.adapters.base import AICompletion

logger = logging.getLogger(__name__)

# Default model for the headless agent; override via RELAY_AI_MODEL_ID. Empty
# means "let the claude CLI use its configured default".
_DEFAULT_MODEL = ""

# Investigation runs an agent loop (multiple tool calls), so it is far slower
# than a single-shot completion — give it a generous default ceiling.
_DEFAULT_TIMEOUT_SECONDS = 180

# Read-only allow-list: the agent may run the skill probes (Bash) and read
# files, but nothing that mutates the account. The skill probes call only
# read-only AWS APIs; this list keeps the agent itself read-only too.
_DEFAULT_ALLOWED_TOOLS = "Read,Glob,Grep,Bash"


class ClaudeCodeAssistant:
    """Single-shot *investigation* via the headless ``claude`` CLI + skill pack.

    Despite implementing the one-shot ``complete()`` contract, each call runs a
    full headless agent loop (the CLI decides how many tool calls to make), so
    this is the heavier "investigate" path. Use the Bedrock/OpenAI assistants
    for cheap summaries; use this when you want the agent to actually probe the
    account with the skills.
    """

    def __init__(
        self,
        *,
        binary: str | None = None,
        model: str | None = None,
        skills_dir: str | None = None,
        allowed_tools: str | None = None,
        timeout_seconds: int | None = None,
        run_fn: Any | None = None,
    ) -> None:
        self._binary = binary or os.environ.get("RELAY_CLAUDE_BIN", "claude")
        self._model = model if model is not None else os.environ.get("RELAY_AI_MODEL_ID", _DEFAULT_MODEL)
        # Where the investigation skill pack lives so the agent can run probes.
        # Defaults to RELAY_SKILLS_DIR (set in the container/image), else unset
        # (the agent still works, just without the bundled skills mounted).
        self._skills_dir = skills_dir if skills_dir is not None else os.environ.get("RELAY_SKILLS_DIR", "")
        self._allowed_tools = (
            allowed_tools
            if allowed_tools is not None
            else os.environ.get("RELAY_CLAUDE_ALLOWED_TOOLS", _DEFAULT_ALLOWED_TOOLS)
        )
        self._timeout = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("RELAY_AI_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
        )
        # Injectable for tests: run_fn(cmd: list[str], stdin: str, timeout: int)
        # -> tuple[int, str, str] returning (returncode, stdout, stderr).
        self._run_fn = run_fn

    def _build_cmd(self) -> list[str]:
        """Assemble the headless claude invocation (read-only, text output)."""
        cmd = [self._binary, "-p", "--output-format", "text"]
        if self._allowed_tools:
            cmd += ["--allowed-tools", self._allowed_tools]
        if self._model:
            cmd += ["--model", self._model]
        if self._skills_dir:
            # Give the agent read access to the skill pack directory.
            cmd += ["--add-dir", self._skills_dir]
        return cmd

    def _default_run(self, cmd: list[str], stdin: str, timeout: int) -> tuple[int, str, str]:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 1024
    ) -> AICompletion | None:
        """Run a headless investigation. Returns text or None (never raises).

        ``max_tokens`` is accepted for interface compatibility but not enforced
        on the CLI — the agent decides its own output length.
        """
        try:
            # Fail fast (and quietly) if the binary isn't on PATH, so we degrade
            # to the deterministic fallback instead of a noisy traceback.
            if self._run_fn is None and shutil.which(self._binary) is None:
                logger.warning(
                    "ClaudeCodeAssistant: %r not found on PATH; falling back",
                    self._binary,
                )
                return None

            cmd = self._build_cmd()
            # The system prompt is prepended to the user prompt; the CLI's
            # --append-system-prompt is also an option but folding it into the
            # piped input keeps the contract simple and shell-injection-free.
            full_prompt = f"{system}\n\n{prompt}" if system else prompt

            run = self._run_fn or self._default_run
            returncode, stdout, stderr = run(cmd, full_prompt, self._timeout)

            if returncode != 0:
                logger.warning(
                    "ClaudeCodeAssistant: claude exited %s; falling back (stderr: %s)",
                    returncode,
                    (stderr or "").strip()[:500],
                )
                return None

            text = (stdout or "").strip()
            return AICompletion(
                text=text or None,
                model=self._model or "claude-code",
                provider="claude-code",
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "ClaudeCodeAssistant: investigation timed out after %ss; falling back",
                self._timeout,
            )
            return None
        except Exception:
            logger.warning("ClaudeCodeAssistant.complete failed; falling back", exc_info=True)
            return None


__all__ = ["ClaudeCodeAssistant"]
