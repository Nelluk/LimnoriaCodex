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

    def test_empty_prompt_luna_modes(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode="luna")
        self.assertEqual(self.irc.replies, ["Please provide a prompt. Usage: @luna <prompt>"])

        self.irc.replies.clear()
        self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode="lunahigh")
        self.assertEqual(
            self.irc.replies,
            ["Please provide a prompt. Usage: @lunahigh <prompt>"],
        )

    def test_luna_modes_use_expected_prompt_and_wrapper_modes(self):
        for mode, expects_high in (("luna", False), ("lunahigh", True)):
            self.irc.replies.clear()
            with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Answer") as wrapped:
                self.plugin._handle_codex_request(self.irc, self.msg, "Compare models", mode=mode)

            self.assertEqual(wrapped.call_args.kwargs["mode"], mode)
            full_prompt = wrapped.call_args.args[1]
            self.assertEqual(
                "Spend extra effort verifying current facts before answering." in full_prompt,
                expects_high,
            )

    def test_empty_prompt_no_context_mode(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode="no")
        self.assertEqual(
            self.irc.replies,
            ["Please provide a prompt. Usage: @codexno <prompt>"],
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

    def test_no_context_mode_uses_high_wrapper_without_context_sections(self):
        self.plugin._config["persistentMemoryEnabled"] = True
        timestamp = time.mktime((2026, 5, 13, 20, 3, 0, 0, 0, -1))
        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            self.plugin.doPrivmsg(self.irc, FakeMsg("alice", "#test", "nearby channel detail"))
            self.plugin._record_persistent_exchange("#test", "prior q", "prior a", None)

        with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Answer") as wrapped:
            self.plugin._handle_codex_request(
                self.irc,
                self.msg,
                "What changed this week?",
                mode="no",
            )

        self.assertEqual(self.irc.replies, ["Answer"])
        full_prompt = wrapped.call_args.args[1]
        self.assertEqual(wrapped.call_args.args[2], 90)
        self.assertEqual(wrapped.call_args.kwargs["mode"], "high")
        self.assertIn("No channel lines or prior Codex exchanges are provided", full_prompt)
        self.assertIn("Spend extra effort verifying current facts before answering.", full_prompt)
        self.assertIn("Answer the USER QUERY above directly.", full_prompt)
        self.assertIn("USER QUERY:\nWhat changed this week?", full_prompt)
        self.assertNotIn("RECENT CODEX EXCHANGES", full_prompt)
        self.assertNotIn("OPTIONAL INCIDENTAL CHANNEL CONTEXT", full_prompt)
        self.assertNotIn("PRIMARY TRANSCRIPT CONTEXT", full_prompt)
        self.assertNotIn("nearby channel detail", full_prompt)
        self.assertNotIn("prior q", full_prompt)
        self.assertNotIn("prior a", full_prompt)

    def test_long_mode_uses_long_prompt_and_luna_high_wrapper_mode(self):
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
        self.assertEqual(wrapped.call_args.kwargs["mode"], "lunahigh")
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
            ["Codex request failed. Nelluk has been notified."],
        )

    def test_auth_error_reply_is_actionable(self):
        with mock.patch.object(
            self.plugin,
            "_invoke_wrapper",
            side_effect=WrapperExecutionError("401 Unauthorized: refresh token expired"),
        ):
            self.plugin._handle_codex_request(self.irc, self.msg, "hello")

        self.assertEqual(
            self.irc.replies,
            ["Codex authentication needs attention. Nelluk should run codex login."],
        )

    def test_quota_error_reply_includes_reset_when_available(self):
        with mock.patch.object(
            self.plugin,
            "_invoke_wrapper",
            side_effect=WrapperExecutionError(
                "You've hit your usage limit. Try again at 12:15 PM. request_id=secret"
            ),
        ):
            self.plugin._handle_codex_request(self.irc, self.msg, "hello")

        self.assertEqual(
            self.irc.replies,
            ["Codex usage limit reached. Try again after 12:15 PM."],
        )

    def test_quota_error_reply_without_reset_is_still_specific(self):
        self.assertEqual(
            self.plugin._friendly_wrapper_error("insufficient_quota"),
            "Codex usage limit reached. Please try again later.",
        )

    def test_network_error_reply_is_specific(self):
        self.assertEqual(
            self.plugin._friendly_wrapper_error("503 Service Unavailable"),
            "Codex is temporarily unreachable. Please try again in a moment.",
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

    def test_url_only_reply_preserves_first_url(self):
        output = (
            "https://x.com/example/status/12345\n"
            "https://example.com/extra-source\n"
        )
        reply = self.plugin._prepare_reply_text(output)
        self.assertEqual(reply, "https://x.com/example/status/12345")

    def test_url_fallback_strips_sentence_punctuation(self):
        reply = self.plugin._prepare_reply_text("https://example.com/tweet.")
        self.assertEqual(reply, "https://example.com/tweet")

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

        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            self.assertEqual(
                self.plugin._memory_lines_for_prompt("#test"),
                ["[2026-05-13 20:03] Q: long question? | A: Long answer"],
            )

    def test_no_context_mode_records_successful_exchange_in_memory(self):
        self.plugin._config["persistentMemoryEnabled"] = True
        timestamp = time.mktime((2026, 5, 13, 20, 3, 0, 0, 0, -1))

        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="No-context answer"):
                self.plugin._handle_codex_request(
                    self.irc,
                    self.msg,
                    "no-context question?",
                    mode="no",
                )

        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            self.assertEqual(
                self.plugin._memory_lines_for_prompt("#test"),
                ["[2026-05-13 20:03] Q: no-context question? | A: No-context answer"],
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

        with mock.patch.object(codex_plugin.time, "time", return_value=second):
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

        with mock.patch.object(codex_plugin.time, "time", return_value=third):
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
        self.assertEqual(cmd[cmd.index("--reasoning-effort") + 1], "high")
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

    def test_model_mode_mappings(self):
        normal = codex_wrapper._runtime_settings(codex_wrapper.MODE_NORMAL)
        high = codex_wrapper._runtime_settings(codex_wrapper.MODE_HIGH)
        self.assertEqual(normal["model"], "gpt-5.6-terra")
        self.assertEqual(normal["reasoning_effort"], "medium")
        self.assertEqual(high["model"], "gpt-5.6-terra")
        self.assertEqual(high["reasoning_effort"], "high")
        luna = codex_wrapper._runtime_settings(codex_wrapper.MODE_LUNA)
        luna_high = codex_wrapper._runtime_settings(codex_wrapper.MODE_LUNA_HIGH)
        self.assertEqual(luna["model"], "gpt-5.6-luna")
        self.assertEqual(luna["reasoning_effort"], "medium")
        self.assertEqual(luna_high["model"], "gpt-5.6-luna")
        self.assertEqual(luna_high["reasoning_effort"], "high")
        self.assertNotIn("minimal", codex_wrapper.ALLOWED_REASONING_EFFORTS)
        self.assertIn("none", codex_wrapper.ALLOWED_REASONING_EFFORTS)
        self.assertIn("max", codex_wrapper.ALLOWED_REASONING_EFFORTS)

    def test_has_non_fatal_rollout_error(self):
        self.assertTrue(
            codex_wrapper._has_non_fatal_rollout_error(
                "failed to record rollout items: thread 123 not found"
            )
        )
        self.assertFalse(codex_wrapper._has_non_fatal_rollout_error("auth failed"))

    def test_codex_child_env_scrubs_inherited_codex_control_variables(self):
        layout = {
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
                "BOT_SECRET": "do-not-inherit",
                "PATH": "/usr/bin",
            },
            clear=False,
        ):
            child_env = codex_wrapper._codex_child_env(
                "/opt/codex/bin/codex",
                layout,
                code_home=self.runtime_home,
            )

        self.assertEqual(child_env["CODEX_HOME"], self.runtime_home)
        self.assertNotIn("CODEX_THREAD_ID", child_env)
        self.assertNotIn("CODEX_CI", child_env)
        self.assertNotIn("CODEX_SANDBOX_NETWORK_DISABLED", child_env)
        self.assertNotIn("CODEX_WRAPPER_TIMEOUT", child_env)
        self.assertNotIn("BOT_SECRET", child_env)
        self.assertTrue(child_env["PATH"].startswith("/opt/codex/bin:"))

    def test_resolve_exec_codex_home_requires_shared_file_auth(self):
        auth_path = os.path.join(self.source_home, "auth.json")
        with open(auth_path, "w", encoding="utf-8") as handle:
            handle.write("shared-auth")

        with mock.patch.dict(
            os.environ,
            {codex_wrapper.EXEC_CODEX_HOME_ENV: self.source_home},
            clear=False,
        ):
            self.assertEqual(
                codex_wrapper._resolve_exec_codex_home(),
                self.source_home,
            )

        os.unlink(auth_path)
        with mock.patch.dict(
            os.environ,
            {codex_wrapper.EXEC_CODEX_HOME_ENV: self.source_home},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "auth file does not exist"):
                codex_wrapper._resolve_exec_codex_home()

    def test_resolve_codex_binary_skips_nested_session_launcher(self):
        installed = "/home/test/.nvm/versions/node/v20/bin/codex"
        nested = "/home/test/.codex/tmp/arg0/session/codex"
        with mock.patch.dict(os.environ, {"HOME": "/home/test", "CODEX_BIN": ""}), \
             mock.patch.object(codex_wrapper.shutil, "which", return_value=nested), \
             mock.patch.object(codex_wrapper.glob, "glob", return_value=[installed]), \
             mock.patch.object(
                 codex_wrapper.os.path,
                 "isfile",
                 side_effect=lambda path: path == installed,
             ), \
             mock.patch.object(codex_wrapper.os, "access", return_value=True):
            self.assertEqual(codex_wrapper._resolve_codex_binary(), installed)

    def test_codex_child_env_can_use_shared_auth_home(self):
        layout = {
            "temp": self.tmpdir,
            "agent_cwd": os.path.join(self.tmpdir, "agent-cwd"),
        }
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "BOT_PASSWORD": "secret",
                "CODEX_THREAD_ID": "parent-thread",
            },
            clear=False,
        ):
            child_env = codex_wrapper._codex_child_env(
                "/opt/codex/bin/codex",
                layout,
                code_home=self.source_home,
            )

        self.assertEqual(child_env["CODEX_HOME"], self.source_home)
        self.assertEqual(child_env["PWD"], layout["agent_cwd"])
        self.assertNotIn("BOT_PASSWORD", child_env)
        self.assertNotIn("CODEX_THREAD_ID", child_env)

    def test_exec_command_is_ephemeral_strict_and_tool_disabled(self):
        settings = codex_wrapper._runtime_settings(codex_wrapper.MODE_HIGH)
        layout = {"agent_cwd": os.path.join(self.tmpdir, "agent-cwd")}
        cmd = codex_wrapper._exec_command(
            "/opt/codex/bin/codex",
            settings,
            layout,
            os.path.join(self.tmpdir, "last-message.txt"),
        )

        self.assertIn("--strict-config", cmd)
        self.assertIn("--ephemeral", cmd)
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ignore-rules", cmd)
        self.assertIn("--json", cmd)
        self.assertIn("read-only", cmd)
        self.assertIn('approval_policy="never"', cmd)
        self.assertIn('history.persistence="none"', cmd)
        self.assertIn('shell_environment_policy.inherit="none"', cmd)
        self.assertIn('web_search="live"', cmd)
        self.assertIn('tools.web_search.context_size="high"', cmd)
        for feature in codex_wrapper.EXEC_DISABLED_FEATURES:
            self.assertIn(f"features.{feature}=false", cmd)
        self.assertEqual(cmd[-1], "-")

    def test_structured_error_detail_extracts_failed_turn_message(self):
        stream = "\n".join(
            [
                '{"type":"thread.started","thread_id":"abc"}',
                '{"type":"turn.failed","error":{"message":"Usage limit reached. Try again at 12:15 PM."}}',
            ]
        )
        self.assertEqual(
            codex_wrapper._structured_error_detail(stream),
            "Usage limit reached. Try again at 12:15 PM.",
        )


if __name__ == "__main__":
    unittest.main()
