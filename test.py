"""Unit tests for Codex plugin."""

import importlib.util
import os
import shutil
import threading
import tempfile
import time
import types
import unittest
from collections import defaultdict, deque
from pathlib import Path
from subprocess import TimeoutExpired
from unittest import mock

from . import plugin as codex_plugin
from .plugin import Codex, WrapperExecutionError, WrapperTimeoutError


WRAPPER_PATH = Path(__file__).with_name("scripts").joinpath("codex_wrapper.py")
WRAPPER_SPEC = importlib.util.spec_from_file_location("codex_wrapper_module", WRAPPER_PATH)
codex_wrapper = importlib.util.module_from_spec(WRAPPER_SPEC)
WRAPPER_SPEC.loader.exec_module(codex_wrapper)


class FakeIrc:
    def __init__(self, nick="CodexBot"):
        self.nick = nick
        self.replies = []

    def isChannel(self, target):
        return isinstance(target, str) and target.startswith("#")

    def reply(self, text, prefixNick=False):
        self.replies.append(text)


class FakeMsg:
    def __init__(self, nick, channel, text, prefix=None):
        self.nick = nick
        self.prefix = prefix or f"{nick}!user@example.com"
        self.args = [channel, text]
        self.channel = channel


class DummyCodex(Codex):
    def __init__(self):
        self._config = {
            "timeoutSeconds": 90,
            "maxContextLines": 20,
            "persistentMemoryEnabled": False,
            "memoryMaxExchanges": 8,
            "cooldownSeconds": 0,
            "allowedChannels": [],
        }
        self._context_buffers = defaultdict(deque)
        self._long_context_buffers = defaultdict(deque)
        self._execution_lock = threading.Lock()
        self._cooldown_lock = threading.Lock()
        self._memory_lock = threading.Lock()
        self._active_executions = 0
        self._last_request_by_user = {}
        self._memory_state = None
        self._memory_path = "/tmp/codex-test-base/persistent_memory.json"
        self.WRAPPER_PATH = "/bin/echo"
        self.WRAPPER_WRITABLE_BASE = "/tmp/codex-test-base"
        self.log = types.SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
        )

    def registryValue(self, name, channel=None):
        return self._config[name]

    def _resolve_runtime_write_paths(self):
        return {
            "base": "/tmp/codex-test-base",
            "state": "/tmp/codex-test-base/state",
            "output": "/tmp/codex-test-base/output",
            "temp": "/tmp/codex-test-base/tmp",
        }

    def _memory_storage_path(self):
        return self._memory_path


class CodexPluginUnitTest(unittest.TestCase):
    def setUp(self):
        self.plugin = DummyCodex()
        self.tmpdir = tempfile.mkdtemp(prefix="codex-plugin-test-")
        self.plugin._memory_path = os.path.join(self.tmpdir, "persistent_memory.json")
        self.irc = FakeIrc()
        self.msg = FakeMsg("alice", "#test", "@codex hi")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_success_path(self):
        with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Answer") as wrapped:
            self.plugin._handle_codex_request(self.irc, self.msg, "What is 2+2?")

        self.assertEqual(self.irc.replies, ["Answer"])
        wrapped.assert_called_once()
        full_prompt = wrapped.call_args.args[1]
        self.assertIn("OPTIONAL INCIDENTAL CHANNEL CONTEXT", full_prompt)
        self.assertIn("If the USER QUERY is understandable on its own, ignore optional channel context entirely.", full_prompt)
        self.assertIn("Do not import topics, entities, assumptions, or constraints from channel lines just because they are nearby.", full_prompt)
        self.assertIn("Ignore incidental channel context when the query stands on its own.", full_prompt)
        self.assertIn("Never run shell/system commands", full_prompt)
        self.assertIn("USER QUERY:\nWhat is 2+2?", full_prompt)
        self.assertEqual(wrapped.call_args.args[2], 90)
        self.assertEqual(wrapped.call_args.kwargs["mode"], "normal")

    def test_empty_prompt(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ")
        self.assertEqual(self.irc.replies, ["Please provide a prompt. Usage: @codex <prompt>"])

    def test_empty_prompt_high_mode(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode="high")
        self.assertEqual(
            self.irc.replies,
            ["Please provide a prompt. Usage: @codexhigh <prompt>"],
        )

    def test_empty_prompt_long_mode(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode="long")
        self.assertEqual(
            self.irc.replies,
            ["Please provide a prompt. Usage: @codexlong <prompt>"],
        )

    def test_high_mode_uses_high_prompt_and_shared_timeout(self):
        with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Answer") as wrapped:
            self.plugin._handle_codex_request(
                self.irc,
                self.msg,
                "What changed this week?",
                mode="high",
            )

        self.assertEqual(self.irc.replies, ["Answer"])
        full_prompt = wrapped.call_args.args[1]
        self.assertEqual(wrapped.call_args.args[2], 90)
        self.assertEqual(wrapped.call_args.kwargs["mode"], "high")
        self.assertIn("OPTIONAL INCIDENTAL CHANNEL CONTEXT", full_prompt)
        self.assertIn("ignore optional channel context entirely", full_prompt)
        self.assertIn("Spend extra effort verifying current facts before answering.", full_prompt)
        self.assertIn("search more thoroughly than normal mode", full_prompt)
        self.assertIn("Use multiple searches when needed", full_prompt)
        self.assertNotIn("Use one brief search pass", full_prompt)

    def test_long_mode_uses_long_prompt_and_high_wrapper_mode(self):
        self.plugin._config["maxContextLines"] = 1
        timestamp = time.mktime((2026, 5, 13, 20, 3, 0, 0, 0, -1))
        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            self.plugin.doPrivmsg(self.irc, FakeMsg("alice", "#test", "first long detail"))
            self.plugin.doPrivmsg(self.irc, FakeMsg("bob", "#test", "second long detail"))

        with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Answer") as wrapped:
            self.plugin._handle_codex_request(
                self.irc,
                self.msg,
                "What happened earlier?",
                mode="long",
            )

        self.assertEqual(self.irc.replies, ["Answer"])
        full_prompt = wrapped.call_args.args[1]
        self.assertEqual(wrapped.call_args.args[2], 90)
        self.assertEqual(wrapped.call_args.kwargs["mode"], "high")
        self.assertIn("long-context transcript analysis request", full_prompt)
        self.assertIn("PRIMARY TRANSCRIPT CONTEXT", full_prompt)
        self.assertIn("Answer the USER QUERY above using the primary transcript context.", full_prompt)
        self.assertNotIn("ignore optional channel context entirely", full_prompt)
        self.assertIn("=== 2026-05-13 20:00 local ===", full_prompt)
        self.assertIn("[20:03] alice: first long detail", full_prompt)
        self.assertIn("[20:03] bob: second long detail", full_prompt)
        self.assertIn("Do not use live web search unless", full_prompt)
        self.assertNotIn("Default to up-to-date answers", full_prompt)

    def test_timeout_error_reply(self):
        with mock.patch.object(
            self.plugin,
            "_invoke_wrapper",
            side_effect=WrapperTimeoutError("timed out"),
        ):
            self.plugin._handle_codex_request(self.irc, self.msg, "hello")

        self.assertEqual(
            self.irc.replies,
            ["Codex timed out. Please try a shorter request."],
        )

    def test_nonzero_error_reply(self):
        with mock.patch.object(
            self.plugin,
            "_invoke_wrapper",
            side_effect=WrapperExecutionError("exit status 2"),
        ):
            self.plugin._handle_codex_request(self.irc, self.msg, "hello")

        self.assertEqual(
            self.irc.replies,
            ["Codex request failed. Please try again in a moment."],
        )

    def test_runtime_preflight_error(self):
        with mock.patch.object(
            self.plugin,
            "_resolve_runtime_write_paths",
            side_effect=WrapperExecutionError("state dir not writable: /readonly/path"),
        ):
            self.plugin._handle_codex_request(self.irc, self.msg, "hello")

        self.assertIn("Codex runtime path error:", self.irc.replies[0])
        self.assertIn("/readonly/path", self.irc.replies[0])

    def test_long_output_uses_single_limnoria_reply(self):
        self.plugin.MAX_REPLY_CHARS = 500
        long_text = " ".join(["word"] * 80)

        with mock.patch.object(self.plugin, "_invoke_wrapper", return_value=long_text):
            self.plugin._handle_codex_request(self.irc, self.msg, "split this")

        self.assertEqual(len(self.irc.replies), 1)
        self.assertLessEqual(len(self.irc.replies[0]), 500)

    def test_reply_preparation_truncates_to_reply_cap(self):
        self.plugin.MAX_REPLY_CHARS = 120
        reply = self.plugin._prepare_reply_text(" ".join(["word"] * 100))

        self.assertLessEqual(len(reply), 120)
        self.assertTrue(reply.endswith("..."))

    def test_reply_sanitization_removes_markdown_and_urls(self):
        output = (
            "- **Result** ([example](https://example.com/path))\n"
            "- more info https://news.example.com/story\n"
        )
        reply = self.plugin._prepare_reply_text(output)
        self.assertEqual(reply, "Result (example) more info")

    def test_bulleted_reply_drops_preamble_and_followup(self):
        output = (
            "Today is Tuesday.\n"
            "Notable updates:\n"
            "- one\n"
            "- two\n"
            "If you tell me a team, I can narrow it down.\n"
        )
        reply = self.plugin._prepare_reply_text(output)
        self.assertEqual(reply, "one two")

    def test_context_ring_buffer_behavior(self):
        self.plugin._config["maxContextLines"] = 3
        self.plugin.CONTEXT_LINE_CHARS = 60
        chan = "#test"

        self.plugin.doPrivmsg(self.irc, FakeMsg("alice", chan, "hello \x02bold\x02 text"))
        self.assertEqual(self.plugin._get_context_lines(chan), ["alice: hello bold text"])

        self.plugin.doPrivmsg(self.irc, FakeMsg("CodexBot", chan, "bot line"))
        self.plugin.doPrivmsg(self.irc, FakeMsg("bob", chan, "@codex ignore this"))
        self.plugin.doPrivmsg(self.irc, FakeMsg("carol", chan, "one"))
        self.plugin.doPrivmsg(self.irc, FakeMsg("dave", chan, "two"))
        self.plugin.doPrivmsg(self.irc, FakeMsg("erin", chan, "three"))

        self.assertEqual(
            self.plugin._get_context_lines(chan),
            ["carol: one", "dave: two", "erin: three"],
        )

    def test_long_context_buffer_keeps_more_lines_than_normal_context(self):
        self.plugin._config["maxContextLines"] = 2
        self.plugin.LONG_CONTEXT_LINES = 4
        chan = "#test"
        timestamp = time.mktime((2026, 5, 13, 20, 3, 0, 0, 0, -1))

        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            for idx in range(1, 7):
                self.plugin.doPrivmsg(self.irc, FakeMsg(f"user{idx}", chan, f"line {idx}"))

        self.assertEqual(
            self.plugin._get_context_lines(chan),
            ["user5: line 5", "user6: line 6"],
        )
        self.assertEqual(
            self.plugin._get_long_context_lines(chan),
            [
                "=== 2026-05-13 20:00 local ===",
                "[20:03] user3: line 3",
                "[20:03] user4: line 4",
                "[20:03] user5: line 5",
                "[20:03] user6: line 6",
            ],
        )

    def test_long_context_adds_new_marker_when_hour_changes(self):
        first = time.mktime((2026, 5, 13, 23, 59, 0, 0, 0, -1))
        second = time.mktime((2026, 5, 14, 0, 1, 0, 0, 0, -1))

        self.assertEqual(
            self.plugin._format_long_context_lines(
                [
                    {"ts": first, "text": "alice: before midnight"},
                    {"ts": second, "text": "bob: after midnight"},
                ]
            ),
            [
                "=== 2026-05-13 23:00 local ===",
                "[23:59] alice: before midnight",
                "=== 2026-05-14 00:00 local ===",
                "[00:01] bob: after midnight",
            ],
        )

    def test_private_message_context_capture(self):
        target = "CodexBot"

        self.plugin.doPrivmsg(self.irc, FakeMsg("alice", target, "first detail"))
        self.plugin.doPrivmsg(self.irc, FakeMsg("alice", target, "@codex included cmd"))
        self.plugin.doPrivmsg(self.irc, FakeMsg("alice", target, "second detail"))

        self.assertEqual(
            self.plugin._get_context_lines("alice"),
            [
                "alice: first detail",
                "alice: @codex included cmd",
                "alice: second detail",
            ],
        )

    def test_persistent_memory_prompt_includes_successful_prior_exchange(self):
        self.plugin._config["persistentMemoryEnabled"] = True
        timestamp = time.mktime((2026, 5, 13, 20, 3, 0, 0, 0, -1))

        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="First answer"):
                self.plugin._handle_codex_request(self.irc, self.msg, "first question?")

            with mock.patch.object(
                self.plugin,
                "_invoke_wrapper",
                return_value="Second answer",
            ) as wrapped:
                self.plugin._handle_codex_request(self.irc, self.msg, "second question?")

        second_prompt = wrapped.call_args.args[1]
        self.assertIn("RECENT CODEX EXCHANGES", second_prompt)
        self.assertIn("[2026-05-13 20:03] Q: first question? | A: First answer", second_prompt)
        self.assertTrue(os.path.isfile(self.plugin._memory_path))

    def test_long_mode_records_successful_exchange_in_memory(self):
        self.plugin._config["persistentMemoryEnabled"] = True
        timestamp = time.mktime((2026, 5, 13, 20, 3, 0, 0, 0, -1))

        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Long answer"):
                self.plugin._handle_codex_request(
                    self.irc,
                    self.msg,
                    "long question?",
                    mode="long",
                )

        self.assertEqual(
            self.plugin._memory_lines_for_prompt("#test"),
            ["[2026-05-13 20:03] Q: long question? | A: Long answer"],
        )

    def test_persistent_memory_orders_by_reserved_sequence(self):
        self.plugin._config["persistentMemoryEnabled"] = True
        context = "#test"

        seq_one = self.plugin._reserve_memory_sequence(context)
        seq_two = self.plugin._reserve_memory_sequence(context)

        first = time.mktime((2026, 5, 13, 20, 1, 0, 0, 0, -1))
        second = time.mktime((2026, 5, 13, 20, 2, 0, 0, 0, -1))
        with mock.patch.object(codex_plugin.time, "time", return_value=second):
            self.plugin._record_persistent_exchange(context, "q2", "a2", seq_two)
        with mock.patch.object(codex_plugin.time, "time", return_value=first):
            self.plugin._record_persistent_exchange(context, "q1", "a1", seq_one)

        self.assertEqual(
            self.plugin._memory_lines_for_prompt(context),
            ["[2026-05-13 20:01] Q: q1 | A: a1", "[2026-05-13 20:02] Q: q2 | A: a2"],
        )

    def test_persistent_memory_prunes_to_max_exchanges(self):
        self.plugin._config["persistentMemoryEnabled"] = True
        self.plugin._config["memoryMaxExchanges"] = 2
        context = "#test"

        seq_one = self.plugin._reserve_memory_sequence(context)
        seq_two = self.plugin._reserve_memory_sequence(context)
        seq_three = self.plugin._reserve_memory_sequence(context)

        first = time.mktime((2026, 5, 13, 20, 1, 0, 0, 0, -1))
        second = time.mktime((2026, 5, 13, 20, 2, 0, 0, 0, -1))
        third = time.mktime((2026, 5, 13, 20, 3, 0, 0, 0, -1))
        with mock.patch.object(codex_plugin.time, "time", return_value=first):
            self.plugin._record_persistent_exchange(context, "q1", "a1", seq_one)
        with mock.patch.object(codex_plugin.time, "time", return_value=second):
            self.plugin._record_persistent_exchange(context, "q2", "a2", seq_two)
        with mock.patch.object(codex_plugin.time, "time", return_value=third):
            self.plugin._record_persistent_exchange(context, "q3", "a3", seq_three)

        self.assertEqual(
            self.plugin._memory_lines_for_prompt(context),
            ["[2026-05-13 20:02] Q: q2 | A: a2", "[2026-05-13 20:03] Q: q3 | A: a3"],
        )

    def test_concurrency_cap_rejects_when_busy(self):
        started = threading.Event()
        release = threading.Event()

        def blocking_wrapper(*args, **kwargs):
            started.set()
            release.wait(timeout=2)
            return "done"

        irc_one = FakeIrc()
        irc_two = FakeIrc()
        msg_one = FakeMsg("alice", "#test", "@codex one")
        msg_two = FakeMsg("bob", "#test", "@codex two")

        with mock.patch.object(self.plugin, "_invoke_wrapper", side_effect=blocking_wrapper):
            thread = threading.Thread(
                target=self.plugin._handle_codex_request,
                args=(irc_one, msg_one, "first"),
            )
            thread.start()
            started.wait(timeout=1)
            self.plugin._handle_codex_request(irc_two, msg_two, "second")
            release.set()
            thread.join(timeout=2)

        self.assertEqual(irc_two.replies, ["Codex is busy right now. Please try again shortly."])
        self.assertEqual(irc_one.replies, ["done"])

    def test_subprocess_boundary_mocked(self):
        completed = types.SimpleNamespace(returncode=0, stdout="wrapper output\n", stderr="")

        with mock.patch.object(self.plugin, "_run_child_process", return_value=completed) as run_mock:
            output = self.plugin._invoke_wrapper(
                "/tmp/wrapper",
                "prompt text",
                42,
                self.plugin._resolve_runtime_write_paths(),
                mode="normal",
            )

        self.assertEqual(output, "wrapper output")
        self.assertEqual(
            run_mock.call_args.args[0],
            ["/tmp/wrapper", "--timeout", "42", "--mode", "normal"],
        )
        self.assertEqual(run_mock.call_args.args[1], "prompt text")
        self.assertIn("CODEX_WRAPPER_WRITE_BASE", run_mock.call_args.args[2])

    def test_high_mode_wrapper_uses_hardcoded_overrides(self):
        completed = types.SimpleNamespace(returncode=0, stdout="wrapper output\n", stderr="")

        with mock.patch.object(self.plugin, "_run_child_process", return_value=completed) as run_mock:
            output = self.plugin._invoke_wrapper(
                "/tmp/wrapper",
                "prompt text",
                42,
                self.plugin._resolve_runtime_write_paths(),
                mode="high",
            )

        self.assertEqual(output, "wrapper output")
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[:4], ["/tmp/wrapper", "--timeout", "42", "--mode"])
        self.assertIn("high", cmd)
        self.assertIn("--reasoning-effort", cmd)
        self.assertIn("--web-search-context-size", cmd)
        self.assertEqual(cmd[cmd.index("--reasoning-effort") + 1], "medium")
        self.assertEqual(cmd[cmd.index("--web-search-context-size") + 1], "high")

    def test_subprocess_timeout_maps_to_wrapper_timeout(self):
        with mock.patch(
            "plugins.Codex.plugin.Codex._run_child_process",
            side_effect=TimeoutExpired(cmd=["x"], timeout=1),
        ):
            with self.assertRaises(WrapperTimeoutError):
                self.plugin._invoke_wrapper(
                    "/tmp/wrapper",
                    "prompt",
                    5,
                    self.plugin._resolve_runtime_write_paths(),
                    mode="normal",
                )


class CodexWrapperUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="codex-wrapper-test-")
        self.source_home = os.path.join(self.tmpdir, "source")
        self.runtime_home = os.path.join(self.tmpdir, "runtime")
        os.makedirs(self.source_home, exist_ok=True)
        os.makedirs(self.runtime_home, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_prepare_codex_home_bootstraps_missing_files(self):
        source_auth = os.path.join(self.source_home, "auth.json")
        source_version = os.path.join(self.source_home, "version.json")
        with open(source_auth, "w", encoding="utf-8") as handle:
            handle.write("source-auth")
        with open(source_version, "w", encoding="utf-8") as handle:
            handle.write("source-version")

        with mock.patch.dict(
            os.environ,
            {codex_wrapper.CODEX_HOME_SOURCE_ENV: self.source_home},
            clear=False,
        ):
            codex_wrapper._prepare_codex_home(
                self.runtime_home,
                codex_wrapper._runtime_config_text(
                    codex_wrapper._runtime_settings(codex_wrapper.MODE_NORMAL)
                ),
            )

        with open(os.path.join(self.runtime_home, "auth.json"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "source-auth")
        with open(os.path.join(self.runtime_home, "version.json"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "source-version")
        with open(os.path.join(self.runtime_home, "config.toml"), "r", encoding="utf-8") as handle:
            config_text = handle.read()
        self.assertIn('cli_auth_credentials_store = "file"', config_text)

    def test_prepare_codex_home_preserves_existing_runtime_auth(self):
        with open(os.path.join(self.source_home, "auth.json"), "w", encoding="utf-8") as handle:
            handle.write("source-auth")
        with open(os.path.join(self.runtime_home, "auth.json"), "w", encoding="utf-8") as handle:
            handle.write("runtime-auth")

        with mock.patch.dict(
            os.environ,
            {codex_wrapper.CODEX_HOME_SOURCE_ENV: self.source_home},
            clear=False,
        ):
            codex_wrapper._prepare_codex_home(
                self.runtime_home,
                codex_wrapper._runtime_config_text(
                    codex_wrapper._runtime_settings(codex_wrapper.MODE_NORMAL)
                ),
            )

        with open(os.path.join(self.runtime_home, "auth.json"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "runtime-auth")
        with open(os.path.join(self.runtime_home, "config.toml"), "r", encoding="utf-8") as handle:
            config_text = handle.read()
        self.assertIn('cli_auth_credentials_store = "file"', config_text)

    def test_runtime_config_text_high_mode_includes_web_search_context(self):
        settings = codex_wrapper._runtime_settings(codex_wrapper.MODE_HIGH)
        config_text = codex_wrapper._runtime_config_text(settings)
        self.assertIn('model_reasoning_effort = "medium"', config_text)
        self.assertIn('web_search = "live"', config_text)
        self.assertIn('tools.web_search = { context_size = "high" }', config_text)

    def test_codex_api_body_includes_streaming_web_search_tool(self):
        settings = codex_wrapper._runtime_settings(codex_wrapper.MODE_NORMAL)
        body = codex_wrapper._codex_api_body("prompt text", settings)

        self.assertEqual(body["model"], "gpt-5.5")
        self.assertEqual(body["input"], [{"role": "user", "content": "prompt text"}])
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["text"], {"verbosity": "low"})
        self.assertTrue(body["stream"])
        self.assertEqual(body["tools"], [{"type": "web_search"}])

    def test_codex_api_body_high_mode_keeps_web_search_tool_shape(self):
        settings = codex_wrapper._runtime_settings(codex_wrapper.MODE_HIGH)
        body = codex_wrapper._codex_api_body("prompt text", settings)

        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertEqual(body["tools"], [{"type": "web_search"}])

    def test_has_non_fatal_rollout_error(self):
        self.assertTrue(
            codex_wrapper._has_non_fatal_rollout_error(
                "failed to record rollout items: thread 123 not found"
            )
        )
        self.assertFalse(codex_wrapper._has_non_fatal_rollout_error("auth failed"))

    def test_codex_child_env_scrubs_inherited_codex_control_variables(self):
        layout = {
            "code_home": self.runtime_home,
            "temp": self.tmpdir,
            "agent_cwd": os.path.join(self.tmpdir, "agent-cwd"),
        }
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": "thread-from-parent",
                "CODEX_CI": "1",
                "CODEX_SANDBOX_NETWORK_DISABLED": "1",
                "CODEX_WRAPPER_TIMEOUT": "90",
                "PATH": "/usr/bin",
            },
            clear=False,
        ):
            child_env = codex_wrapper._codex_child_env("/opt/codex/bin/codex", layout)

        self.assertEqual(child_env["CODEX_HOME"], self.runtime_home)
        self.assertNotIn("CODEX_THREAD_ID", child_env)
        self.assertNotIn("CODEX_CI", child_env)
        self.assertNotIn("CODEX_SANDBOX_NETWORK_DISABLED", child_env)
        self.assertNotIn("CODEX_WRAPPER_TIMEOUT", child_env)
        self.assertTrue(child_env["PATH"].startswith("/opt/codex/bin:"))


if __name__ == "__main__":
    unittest.main()
