"""Model backends for FaultOrbit's agents, plus one safe way to call them.

Backends (pick with the FAULTORBIT_LLM environment variable):

    claude-code  (default) runs the Claude Code CLI on this machine in print mode.
                 It uses your Claude subscription login, so no API key is needed.
    ollama       a local model served by Ollama (free, offline).
    fake         canned answers, for tests. Never calls a model.

Agents never call a backend directly. They call `complete_json`, which asks for
JSON matching a schema, validates it, retries once with the errors if it doesn't
match, and logs every attempt to results/llm_calls.jsonl.

Check a backend works:  python -m agents.llm check
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from agents.schema import validate
from chaos import hooks


class LLMError(Exception):
    """The backend failed, or its answer never matched the schema."""


@dataclass
class LLMResult:
    data: object
    backend: str
    model: str | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    raw: str | None = None


def _strip_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _parse_json(text):
    try:
        return json.loads(_strip_fences(text))
    except (json.JSONDecodeError, TypeError) as e:
        raise LLMError(f"model did not return valid JSON: {e}") from e


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class ClaudeCodeClient:
    """Runs `claude -p` (Claude Code print mode) with a JSON schema.

    Claude Code must be installed and logged in on this machine. The call runs in
    an empty temporary folder with permission prompts denied, so Claude only
    sees the prompt we send; it can't read or change the repo.
    """

    name = "claude-code"

    def __init__(self, model=None, binary=None, timeout_s=300, runner=subprocess.run):
        self.model = model or os.environ.get("FAULTORBIT_CLAUDE_MODEL") or None
        self.binary = binary or os.environ.get("FAULTORBIT_CLAUDE_BIN") or "claude"
        self.timeout_s = timeout_s
        self._run = runner

    def generate(self, system, prompt, schema):
        binary = shutil.which(self.binary) or self.binary
        cmd = [binary, "-p", "--output-format", "json",
               "--json-schema", json.dumps(schema, separators=(",", ":")),
               "--permission-mode", "dontAsk"]
        if self.model:
            cmd += ["--model", self.model]
        # System instructions travel in the prompt (stdin), which avoids shell
        # quoting problems with long text on Windows.
        full_prompt = f"{system.strip()}\n\n---\n\n{prompt.strip()}"
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="faultorbit-llm-") as cwd:
            try:
                proc = self._run(cmd, input=full_prompt, capture_output=True, text=True,
                                 encoding="utf-8", timeout=self.timeout_s, cwd=cwd)
            except FileNotFoundError as e:
                raise LLMError(
                    f"Claude Code CLI not found ({self.binary!r}). Install it and log in, "
                    "or set FAULTORBIT_CLAUDE_BIN to its path."
                ) from e
            except subprocess.TimeoutExpired as e:
                raise LLMError(f"claude timed out after {self.timeout_s}s") from e
        duration = time.monotonic() - start

        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            raise LLMError(f"claude exited {proc.returncode} without JSON output: {detail}")
        if proc.returncode != 0 or out.get("is_error"):
            raise LLMError(f"claude failed: {str(out.get('result', proc.stderr))[:500]}")

        data = out.get("structured_output")
        if data is None:
            data = _parse_json(out.get("result", ""))
        model = self.model or next(iter(out.get("modelUsage") or {}), None)
        return LLMResult(data=data, backend=self.name, model=model,
                         cost_usd=out.get("total_cost_usd"), duration_s=round(duration, 2),
                         raw=out.get("result"))


class OllamaClient:
    """Calls a local Ollama server's /api/chat with a JSON schema as the format."""

    name = "ollama"

    def __init__(self, model=None, host=None, timeout_s=300, opener=urllib.request.urlopen):
        self.model = model or os.environ.get("FAULTORBIT_OLLAMA_MODEL") or "llama3.1:8b"
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.timeout_s = timeout_s
        self._open = opener

    def generate(self, system, prompt, schema):
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0},
        }).encode("utf-8")
        req = urllib.request.Request(f"{self.host}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        start = time.monotonic()
        try:
            with self._open(req, timeout=self.timeout_s) as r:
                out = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"Ollama not reachable at {self.host}: {e}. Is `ollama serve` running?") from e
        content = (out.get("message") or {}).get("content", "")
        return LLMResult(data=_parse_json(content), backend=self.name, model=self.model,
                         cost_usd=0.0, duration_s=round(time.monotonic() - start, 2), raw=content)


@dataclass
class FakeClient:
    """Test double: returns queued answers (dicts) or the result of a function."""

    responses: list = field(default_factory=list)
    responder: object = None
    calls: list = field(default_factory=list)
    name: str = "fake"

    def generate(self, system, prompt, schema):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self.responder is not None:
            data = self.responder(system, prompt, schema)
        elif self.responses:
            data = self.responses.pop(0)
        else:
            raise LLMError("FakeClient has no more responses")
        if isinstance(data, Exception):
            raise data
        return LLMResult(data=data, backend=self.name, model="fake", cost_usd=0.0, duration_s=0.0)


BACKENDS = {"claude-code": ClaudeCodeClient, "ollama": OllamaClient, "fake": FakeClient}


def get_client(name=None, **kwargs):
    """Backend from the argument, else FAULTORBIT_LLM, else claude-code."""
    name = (name or os.environ.get("FAULTORBIT_LLM") or "claude-code").strip().lower()
    if name not in BACKENDS:
        raise LLMError(f"unknown backend {name!r}; choose one of {', '.join(BACKENDS)}")
    return BACKENDS[name](**kwargs)


# --------------------------------------------------------------------------- #
# The one way agents call a model
# --------------------------------------------------------------------------- #

def prompt_version(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _log(entry):
    try:
        hooks.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(hooks.RESULTS_DIR / "llm_calls.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # logging must never break an agent


def complete_json(client, *, name, system, prompt, schema, retries=1):
    """Ask `client` for JSON matching `schema`; validate, retry with errors, log.

    Returns the parsed, validated data. Raises LLMError if every attempt fails.
    """
    version = prompt_version(system)
    attempt_prompt = prompt
    last_error = None
    for attempt in range(1, retries + 2):
        entry = {"t": time.time(), "agent": name, "backend": getattr(client, "name", "?"),
                 "prompt_version": version, "attempt": attempt}
        try:
            result = client.generate(system, attempt_prompt, schema)
            errors = validate(schema, result.data)
            entry.update(model=result.model, cost_usd=result.cost_usd,
                         duration_s=result.duration_s, ok=not errors, errors=errors[:10])
            _log(entry)
            if not errors:
                return result.data
            last_error = LLMError("answer did not match the schema: " + "; ".join(errors[:5]))
            attempt_prompt = (
                f"{prompt}\n\nYour previous answer was rejected because it did not match "
                f"the required JSON schema:\n- " + "\n- ".join(errors[:10]) +
                "\nReturn corrected JSON only."
            )
        except LLMError as e:
            entry.update(ok=False, errors=[str(e)])
            _log(entry)
            last_error = e
    raise last_error


# --------------------------------------------------------------------------- #
# python -m agents.llm check
# --------------------------------------------------------------------------- #

CHECK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "message": {"type": "string"}},
    "required": ["ok", "message"],
    "additionalProperties": False,
}


def _main(argv):
    if not argv or argv[0] != "check":
        print("usage: python -m agents.llm check [backend]")
        return 2
    client = get_client(argv[1] if len(argv) > 1 else None)
    print(f"backend: {client.name}")
    try:
        data = complete_json(client, name="check",
                             system="You are a health check for a software tool.",
                             prompt='Reply with ok=true and message="FaultOrbit backend works".',
                             schema=CHECK_SCHEMA)
    except LLMError as e:
        print(f"FAILED: {e}")
        return 1
    print(f"OK: {data['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
