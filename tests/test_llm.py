"""Tests for agents/: schema checks, backends (mocked), complete_json. No model is called."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents import llm
from agents.llm import (ClaudeCodeClient, FakeClient, LLMError, OllamaClient,
                        complete_json, get_client)
from agents.prompting import load_prompt
from agents.schema import validate
from chaos import hooks

SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["low", "high"]},
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "runs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["severity", "score", "runs"],
    "additionalProperties": False,
}
GOOD = {"severity": "high", "score": 42, "runs": ["r1"]}


class ResultsDirMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = hooks.RESULTS_DIR
        hooks.RESULTS_DIR = Path(self.tmp.name)

    def tearDown(self):
        hooks.RESULTS_DIR = self._orig
        self.tmp.cleanup()

    def log_lines(self):
        path = hooks.RESULTS_DIR / "llm_calls.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class SchemaTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate(SCHEMA, GOOD), [])

    def test_reports_each_problem(self):
        errs = validate(SCHEMA, {"severity": "medium", "score": 120, "runs": [], "extra": 1})
        text = " ".join(errs)
        for needle in ["not one of", "maximum", "fewer than 1", "unexpected key 'extra'"]:
            self.assertIn(needle, text)

    def test_missing_and_wrong_type(self):
        errs = validate(SCHEMA, {"severity": "low", "score": "high"})
        self.assertTrue(any("missing required key 'runs'" in e for e in errs))
        self.assertTrue(any("$.score: expected number" in e for e in errs))

    def test_bool_is_not_a_number(self):
        self.assertTrue(validate({"type": "number"}, True))
        self.assertTrue(validate({"type": "integer"}, 1.5))
        self.assertEqual(validate({"type": ["string", "null"]}, None), [])


class CompleteJsonTests(ResultsDirMixin, unittest.TestCase):
    def test_returns_valid_answer_and_logs(self):
        client = FakeClient(responses=[GOOD])
        self.assertEqual(complete_json(client, name="t", system="s", prompt="p", schema=SCHEMA), GOOD)
        (entry,) = self.log_lines()
        self.assertEqual((entry["agent"], entry["backend"], entry["ok"]), ("t", "fake", True))
        self.assertEqual(len(entry["prompt_version"]), 12)

    def test_retries_once_with_the_errors(self):
        client = FakeClient(responses=[{"severity": "huge"}, GOOD])
        self.assertEqual(complete_json(client, name="t", system="s", prompt="p", schema=SCHEMA), GOOD)
        self.assertIn("was rejected", client.calls[1]["prompt"])
        self.assertIn("not one of", client.calls[1]["prompt"])
        self.assertEqual([e["ok"] for e in self.log_lines()], [False, True])

    def test_gives_up_after_retries(self):
        client = FakeClient(responses=[{}, {}])
        with self.assertRaises(LLMError):
            complete_json(client, name="t", system="s", prompt="p", schema=SCHEMA)
        self.assertEqual(len(client.calls), 2)

    def test_backend_error_is_retried_then_raised(self):
        client = FakeClient(responses=[LLMError("down"), LLMError("still down")])
        with self.assertRaisesRegex(LLMError, "still down"):
            complete_json(client, name="t", system="s", prompt="p", schema=SCHEMA)


def completed(stdout, code=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


class ClaudeCodeClientTests(unittest.TestCase):
    def run_with(self, stdout, code=0, **kw):
        seen = {}

        def runner(cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            return completed(stdout, code)

        client = ClaudeCodeClient(runner=runner, binary="claude-test-bin", **kw)
        return client, seen

    def test_uses_structured_output_and_print_mode(self):
        out = json.dumps({"structured_output": GOOD, "result": "", "total_cost_usd": 0.01,
                          "modelUsage": {"claude-sonnet-x": {}}})
        client, seen = self.run_with(out)
        res = client.generate("SYS", "PROMPT", SCHEMA)
        self.assertEqual(res.data, GOOD)
        self.assertEqual((res.cost_usd, res.model), (0.01, "claude-sonnet-x"))
        cmd = seen["cmd"]
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), SCHEMA)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("SYS", seen["kwargs"]["input"])
        self.assertIn("PROMPT", seen["kwargs"]["input"])
        self.assertNotEqual(seen["kwargs"]["cwd"], os.getcwd())  # isolated temp folder

    def test_model_flag(self):
        client, seen = self.run_with(json.dumps({"structured_output": GOOD}), model="haiku")
        client.generate("s", "p", SCHEMA)
        self.assertEqual(seen["cmd"][seen["cmd"].index("--model") + 1], "haiku")

    def test_falls_back_to_result_text(self):
        out = json.dumps({"result": "```json\n" + json.dumps(GOOD) + "\n```"})
        client, _ = self.run_with(out)
        self.assertEqual(client.generate("s", "p", SCHEMA).data, GOOD)

    def test_error_result_raises(self):
        client, _ = self.run_with(json.dumps({"is_error": True, "result": "Not logged in"}), code=1)
        with self.assertRaisesRegex(LLMError, "Not logged in"):
            client.generate("s", "p", SCHEMA)

    def test_non_json_output_raises(self):
        client, _ = self.run_with("boom", code=2)
        with self.assertRaisesRegex(LLMError, "without JSON"):
            client.generate("s", "p", SCHEMA)

    def test_missing_cli_raises_helpful_error(self):
        def runner(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        with self.assertRaisesRegex(LLMError, "not found"):
            ClaudeCodeClient(runner=runner, binary="nope").generate("s", "p", SCHEMA)

    def test_timeout_raises(self):
        def runner(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        with self.assertRaisesRegex(LLMError, "timed out"):
            ClaudeCodeClient(runner=runner, binary="x", timeout_s=1).generate("s", "p", SCHEMA)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class OllamaClientTests(unittest.TestCase):
    def test_sends_schema_and_parses_content(self):
        seen = {}

        def opener(req, timeout):
            seen["url"], seen["body"] = req.full_url, json.loads(req.data)
            return FakeResponse(json.dumps({"message": {"content": json.dumps(GOOD)}}).encode())

        client = OllamaClient(model="m1", host="localhost:11434", opener=opener)
        self.assertEqual(client.generate("SYS", "P", SCHEMA).data, GOOD)
        self.assertEqual(seen["url"], "http://localhost:11434/api/chat")
        self.assertEqual(seen["body"]["format"], SCHEMA)
        self.assertEqual(seen["body"]["model"], "m1")
        self.assertFalse(seen["body"]["stream"])

    def test_unreachable_raises(self):
        def opener(req, timeout):
            raise OSError("connection refused")

        with self.assertRaisesRegex(LLMError, "not reachable"):
            OllamaClient(opener=opener).generate("s", "p", SCHEMA)

    def test_bad_json_raises(self):
        opener = lambda req, timeout: FakeResponse(json.dumps({"message": {"content": "nope"}}).encode())
        with self.assertRaisesRegex(LLMError, "valid JSON"):
            OllamaClient(opener=opener).generate("s", "p", SCHEMA)


class GetClientTests(unittest.TestCase):
    def test_default_is_claude_code(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FAULTORBIT_LLM", None)
            self.assertIsInstance(get_client(), ClaudeCodeClient)

    def test_env_selects_backend(self):
        with mock.patch.dict(os.environ, {"FAULTORBIT_LLM": "Ollama"}):
            self.assertIsInstance(get_client(), OllamaClient)

    def test_unknown_backend(self):
        with self.assertRaises(LLMError):
            get_client("gpt")


class CheckCommandTests(ResultsDirMixin, unittest.TestCase):
    def test_check_with_fake_backend(self):
        fake = FakeClient(responses=[{"ok": True, "message": "FaultOrbit backend works"}])
        with mock.patch.object(llm, "get_client", return_value=fake), \
                mock.patch("builtins.print") as out:
            self.assertEqual(llm._main(["check"]), 0)
        self.assertIn("OK: FaultOrbit backend works", " ".join(str(c.args[0]) for c in out.call_args_list))


class PromptLoaderTests(unittest.TestCase):
    def test_loads_readme_and_rejects_unknown(self):
        self.assertIn("Agent prompts", load_prompt("README"))
        with self.assertRaises(FileNotFoundError):
            load_prompt("does-not-exist")


if __name__ == "__main__":
    unittest.main()
