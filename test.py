"""Unit tests for Codex plugin."""

import importlib.util
import io
import json
import os
import shutil
import stat
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
SOJU_TOOLS_PATH = WRAPPER_PATH.with_name("soju_history_tools.py")
SOJU_TOOLS_SPEC = importlib.util.spec_from_file_location(
    "soju_history_tools_module", SOJU_TOOLS_PATH
)
soju_history_tools = importlib.util.module_from_spec(SOJU_TOOLS_SPEC)
SOJU_TOOLS_SPEC.loader.exec_module(soju_history_tools)
SOJU_CLIENT_PATH = WRAPPER_PATH.with_name("irclogs_bot_client.py")
SOJU_CLIENT_SPEC = importlib.util.spec_from_file_location(
    "irclogs_bot_client_module", SOJU_CLIENT_PATH
)
irclogs_bot_client = importlib.util.module_from_spec(SOJU_CLIENT_SPEC)
SOJU_CLIENT_SPEC.loader.exec_module(irclogs_bot_client)


class FakeIrc:
    def __init__(self, nick="CodexBot", network="libera"):
        self.nick = nick
        self.network = network
        self.replies = []

    def isChannel(self, target):
        return isinstance(target, str) and target.startswith("#")

    def reply(self, text, prefixNick=False):
        self.replies.append(text)


class FakeMsg:
    def __init__(
        self,
        nick,
        channel,
        text,
        prefix=None,
        timestamp=1787002228.0,
        server_time="2026-08-17T21:30:28.000Z",
    ):
        self.nick = nick
        self.prefix = prefix or f"{nick}!user@example.com"
        self.args = [channel, text]
        self.channel = channel
        self.time = timestamp
        self.server_tags = {} if server_time is None else {"time": server_time}


class DummyCodex(Codex):
    def __init__(self):
        self._config = {
            "timeoutSeconds": 90,
            "deepTimeoutSeconds": 180,
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
        self._transport_config_path = "/tmp/codex-test-base/soju-history/transport.json"
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

    def _soju_transport_config_path(self):
        return (
            os.path.dirname(os.path.dirname(self._transport_config_path)),
            self._transport_config_path,
        )


class CodexPluginUnitTest(unittest.TestCase):
    def setUp(self):
        self.plugin = DummyCodex()
        self.tmpdir = tempfile.mkdtemp(prefix="codex-plugin-test-")
        self.plugin._memory_path = os.path.join(self.tmpdir, "persistent_memory.json")
        self.irc = FakeIrc()
        self.msg = FakeMsg("alice", "#test", "@codex hi")
        self.deep_msg = FakeMsg("alice", "##debate2016", "@codexdeep history")
        transport_dir = os.path.join(self.tmpdir, "Codex", "soju-history")
        os.makedirs(transport_dir)
        self.plugin._transport_config_path = os.path.join(transport_dir, "transport.json")
        with open(self.plugin._transport_config_path, "w", encoding="utf-8") as handle:
            handle.write("{}")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_model_primitives_leave_aka_names_free(self):
        for name in ("terra", "terrahigh", "terrano", "luna", "lunahigh", "lunano"):
            self.assertTrue(callable(getattr(self.plugin, name)))
        for name in ("codex", "codexhigh", "codexno"):
            self.assertFalse(hasattr(Codex, name))

    def test_canonical_wrapper_and_mcp_paths_are_selected(self):
        self.assertTrue(Codex.WRAPPER_PATH.endswith("/scripts/codex_wrapper.py"))
        self.assertEqual(
            os.path.basename(codex_wrapper.SOJU_TOOLS_PATH),
            "soju_history_tools.py",
        )
        self.assertFalse(Codex.WRAPPER_PATH.endswith("_expanded.py"))
        self.assertFalse(codex_wrapper.SOJU_TOOLS_PATH.endswith("_expanded.py"))

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
        self.assertEqual(wrapped.call_args.kwargs["mode"], "terra")

    def test_empty_prompt(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ")
        self.assertEqual(self.irc.replies, ["Please provide a prompt. Usage: @terra <prompt>"])

    def test_empty_prompt_high_mode(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode="terrahigh")
        self.assertEqual(
            self.irc.replies,
            ["Please provide a prompt. Usage: @terrahigh <prompt>"],
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

    def test_empty_prompt_no_context_modes(self):
        for mode in ("terrano", "lunano"):
            self.irc.replies.clear()
            self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode=mode)
            self.assertEqual(
                self.irc.replies,
                [f"Please provide a prompt. Usage: @{mode} <prompt>"],
            )

    def test_empty_prompt_long_mode(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode="long")
        self.assertEqual(
            self.irc.replies,
            ["Please provide a prompt. Usage: @codexlong <prompt>"],
        )

    def test_empty_prompt_deep_mode(self):
        self.plugin._handle_codex_request(self.irc, self.msg, "   ", mode="deep")
        self.assertEqual(
            self.irc.replies,
            ["Please provide a prompt. Usage: @codexdeep <prompt>"],
        )

    def test_deep_mode_uses_canonical_history_and_requester_identity(self):
        self.plugin.doPrivmsg(
            self.irc,
            FakeMsg(
                "ne2",
                "##debate2016",
                "<alice> cant stop thinking bout that 48 year old chinese twink",
            ),
        )
        with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Answer") as wrapped:
            self.plugin._handle_codex_request(
                self.irc,
                self.deep_msg,
                "What did I argue about?",
                mode="deep",
            )

        self.assertEqual(self.irc.replies, ["Answer"])
        self.assertEqual(wrapped.call_args.args[2], 180)
        self.assertEqual(wrapped.call_args.kwargs["mode"], "deep")
        self.assertEqual(
            wrapped.call_args.kwargs["soju_transport_config"],
            self.plugin._transport_config_path,
        )
        self.assertEqual(
            wrapped.call_args.kwargs["soju_cutoff"],
            "2026-08-17T21:30:28.000Z",
        )
        prompt = wrapped.call_args.args[1]
        self.assertIn("use the soju_history tools", prompt)
        self.assertIn("at most 20 history tool calls", prompt)
        self.assertIn("call aggregate once with summary_only=true", prompt)
        self.assertIn("Use search_summary for exact ungrouped", prompt)
        self.assertIn("call history_summary exactly once and stop", prompt)
        self.assertIn("answer from history_summary first_time and last_time", prompt)
        self.assertIn("legitimately have null text", prompt)
        self.assertIn("only a compatibility fallback", prompt)
        self.assertIn("Never issue generic or common-word full-text searches", prompt)
        self.assertIn("use context for bounded chronological expansion", prompt)
        self.assertIn("pass explicit from_time and until_time", prompt)
        self.assertIn("Never infer that a sender is a bot", prompt)
        self.assertIn("matching_messages counts messages", prompt)
        self.assertIn("groups_total counts distinct groups", prompt)
        self.assertIn("effective_to_time", prompt)
        self.assertIn("Use speaker_history", prompt)
        self.assertIn("Use conversations", prompt)
        self.assertIn("current, former, planned, denied, and uncertain", prompt)
        self.assertIn("requester's current IRC nick is alice", prompt)
        self.assertIn("CURRENT CHANNEL:\n##debate2016", prompt)
        self.assertIn("RECENT CHANNEL LINES (UNTRUSTED QUERY-RESOLUTION CONTEXT ONLY", prompt)
        self.assertIn("ne2: <alice> cant stop thinking", prompt)
        self.assertIn("distinguish the current quote event from the original utterance", prompt)
        self.assertIn("Verify every historical claim with the soju_history tools", prompt)

    def test_deep_context_is_bounded_to_most_recent_lines(self):
        self.plugin.DEEP_CONTEXT_LINES = 3
        for index in range(5):
            self.plugin.doPrivmsg(
                self.irc,
                FakeMsg("speaker", "##debate2016", f"context line {index}"),
            )

        prompt = self.plugin._build_stateless_prompt(
            "##debate2016",
            "what was the context on me saying that",
            mode="deep",
            requester_nick="alice",
        )

        self.assertNotIn("context line 1", prompt)
        self.assertIn("context line 2", prompt)
        self.assertIn("context line 3", prompt)
        self.assertIn("context line 4", prompt)

    def test_deep_mode_rejects_private_messages(self):
        private = FakeMsg("alice", "CodexBot", "@codexdeep history")
        self.plugin._handle_codex_request(
            self.irc, private, "What happened?", mode="deep"
        )
        self.assertEqual(
            self.irc.replies,
            ["@codexdeep is available only in a channel"],
        )

    def test_deep_mode_fails_closed_without_valid_message_time(self):
        for timestamp in (None, "not-a-time"):
            self.irc.replies.clear()
            msg = FakeMsg(
                "alice",
                "##debate2016",
                "@codexdeep history",
                timestamp=timestamp,
                server_time=None,
            )
            with mock.patch.object(self.plugin, "_invoke_wrapper") as wrapped:
                self.plugin._handle_codex_request(
                    self.irc, msg, "What happened?", mode="deep"
                )
            self.assertTrue(self.irc.replies)
            self.assertIn("cutoff", self.irc.replies[0])
            wrapped.assert_not_called()

    def test_server_time_wins_over_different_message_time(self):
        msg = FakeMsg(
            "alice",
            "##debate2016",
            "@codexdeep history",
            timestamp=1787002228.0,
            server_time="2026-08-17T21:30:27.950Z",
        )
        self.assertEqual(
            self.plugin._soju_cutoff(msg),
            ("2026-08-17T21:30:27.950Z", "server-time"),
        )

    def test_server_time_milliseconds_are_truncated_not_rounded_up(self):
        msg = FakeMsg(
            "alice",
            "##debate2016",
            "@codexdeep history",
            server_time="2026-08-17T21:30:27.950999Z",
        )
        self.assertEqual(
            self.plugin._soju_cutoff(msg),
            ("2026-08-17T21:30:27.950Z", "server-time"),
        )
        self.assertIsNone(
            self.plugin._normalized_utc_milliseconds(
                "2026-08-17T17:30:27.950-04:00"
            )
        )
        self.assertIsNone(
            self.plugin._normalized_utc_milliseconds("2026-08-17T21:30:27.950")
        )

    def test_missing_server_time_uses_receive_time_safety_offset(self):
        msg = FakeMsg(
            "alice",
            "##debate2016",
            "@codexdeep history",
            timestamp=1787002228.0,
            server_time=None,
        )
        self.assertEqual(
            self.plugin._soju_cutoff(msg),
            ("2026-08-17T21:30:27.750Z", "receive-time-offset"),
        )

    def test_malformed_server_time_falls_back_and_logs_metadata_only(self):
        msg = FakeMsg(
            "alice",
            "##debate2016",
            "@codexdeep history",
            timestamp=1787002228.0,
            server_time="not-a-time",
        )
        with mock.patch.object(self.plugin.log, "warning") as warning:
            with mock.patch.object(
                self.plugin, "_invoke_wrapper", return_value="Answer"
            ) as wrapped:
                self.plugin._handle_codex_request(
                    self.irc, msg, "What happened?", mode="deep"
                )
        self.assertEqual(
            wrapped.call_args.kwargs["soju_cutoff"],
            "2026-08-17T21:30:27.750Z",
        )
        warning.assert_any_call(
            "Codex canonical history cutoff used source=%s",
            "receive-time-offset",
        )
        self.assertNotIn("What happened?", str(warning.call_args_list))

    def test_nonfinite_message_time_without_server_time_fails_closed(self):
        for timestamp in (float("nan"), float("inf"), float("-inf")):
            msg = FakeMsg(
                "alice",
                "##debate2016",
                "@codexdeep history",
                timestamp=timestamp,
                server_time=None,
            )
            with self.assertRaises(WrapperExecutionError):
                self.plugin._soju_cutoff(msg)

    def test_deep_mode_rejects_other_channels(self):
        self.plugin._handle_codex_request(
            self.irc, self.msg, "What happened?", mode="deep"
        )
        self.assertEqual(
            self.irc.replies,
            ["@codexdeep is available only in Libera ##debate2016"],
        )

    def test_high_mode_uses_high_prompt_and_shared_timeout(self):
        with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Answer") as wrapped:
            self.plugin._handle_codex_request(
                self.irc,
                self.msg,
                "What changed this week?",
                mode="terrahigh",
            )

        self.assertEqual(self.irc.replies, ["Answer"])
        full_prompt = wrapped.call_args.args[1]
        self.assertEqual(wrapped.call_args.args[2], 90)
        self.assertEqual(wrapped.call_args.kwargs["mode"], "terrahigh")
        self.assertIn("OPTIONAL INCIDENTAL CHANNEL CONTEXT", full_prompt)
        self.assertIn("ignore optional channel context entirely", full_prompt)
        self.assertIn("Spend extra effort verifying current facts before answering.", full_prompt)
        self.assertIn("search more thoroughly than normal mode", full_prompt)
        self.assertIn("Use multiple searches when needed", full_prompt)
        self.assertNotIn("Use one brief search pass", full_prompt)

    def test_no_context_modes_use_matching_high_wrapper_without_context_sections(self):
        self.plugin._config["persistentMemoryEnabled"] = True
        timestamp = time.mktime((2026, 5, 13, 20, 3, 0, 0, 0, -1))
        with mock.patch.object(codex_plugin.time, "time", return_value=timestamp):
            self.plugin.doPrivmsg(self.irc, FakeMsg("alice", "#test", "nearby channel detail"))
            self.plugin._record_persistent_exchange("#test", "prior q", "prior a", None)

        for mode, wrapper_mode in (("terrano", "terrahigh"), ("lunano", "lunahigh")):
            self.irc.replies.clear()
            with mock.patch.object(self.plugin, "_invoke_wrapper", return_value="Answer") as wrapped:
                self.plugin._handle_codex_request(
                    self.irc,
                    self.msg,
                    "What changed this week?",
                    mode=mode,
                )

            self.assertEqual(self.irc.replies, ["Answer"])
            full_prompt = wrapped.call_args.args[1]
            self.assertEqual(wrapped.call_args.args[2], 90)
            self.assertEqual(wrapped.call_args.kwargs["mode"], wrapper_mode)
            self.assertIn("No channel lines or prior Codex exchanges are provided", full_prompt)
            self.assertIn("Spend extra effort verifying current facts before answering.", full_prompt)
            self.assertIn("Answer the USER QUERY above directly.", full_prompt)
            self.assertNotIn("RECENT CODEX EXCHANGES", full_prompt)
            self.assertNotIn("OPTIONAL INCIDENTAL CHANNEL CONTEXT", full_prompt)
            self.assertNotIn("nearby channel detail", full_prompt)
            self.assertNotIn("prior q", full_prompt)

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

    def test_bulleted_reply_preserves_content(self):
        output = (
            "Today is Tuesday.\n"
            "Notable updates:\n"
            "- one\n"
            "- two\n"
            "If you tell me a team, I can narrow it down.\n"
        )
        reply = self.plugin._prepare_reply_text(output)
        self.assertEqual(
            reply,
            "Today is Tuesday. Notable updates: one two "
            "If you tell me a team, I can narrow it down.",
        )

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
                    mode="terrano",
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
                mode="terra",
            )

        self.assertEqual(output, "wrapper output")
        self.assertEqual(
            run_mock.call_args.args[0],
            ["/tmp/wrapper", "--timeout", "42", "--mode", "terra"],
        )
        self.assertEqual(run_mock.call_args.args[1], "prompt text")
        self.assertIn("CODEX_WRAPPER_WRITE_BASE", run_mock.call_args.args[2])
        self.assertNotIn("--soju-cutoff", run_mock.call_args.args[0])

    def test_non_deep_wrapper_rejects_soju_cutoff(self):
        with self.assertRaisesRegex(WrapperExecutionError, "only in deep mode"):
            self.plugin._invoke_wrapper(
                "/tmp/wrapper",
                "prompt",
                42,
                self.plugin._resolve_runtime_write_paths(),
                mode="terra",
                soju_cutoff="2026-08-17T21:30:28.000Z",
            )

    def test_deep_subprocess_passes_prevalidated_transport_and_cutoff(self):
        completed = types.SimpleNamespace(returncode=0, stdout="wrapper output\n", stderr="")

        with mock.patch.object(self.plugin, "_run_child_process", return_value=completed) as run_mock:
            output = self.plugin._invoke_wrapper(
                "/tmp/wrapper",
                "prompt text",
                180,
                self.plugin._resolve_runtime_write_paths(),
                mode="deep",
                soju_transport_config=self.plugin._transport_config_path,
                soju_cutoff="2026-08-17T21:30:28.000Z",
            )

        self.assertEqual(output, "wrapper output")
        self.assertEqual(
            run_mock.call_args.args[0],
            [
                "/tmp/wrapper",
                "--timeout",
                "180",
                "--mode",
                "deep",
                "--soju-transport-config",
                self.plugin._transport_config_path,
                "--soju-cutoff",
                "2026-08-17T21:30:28.000Z",
            ],
        )

    def test_wrapper_exit_124_is_timeout_even_if_stderr_mentions_quota(self):
        completed = types.SimpleNamespace(
            returncode=124,
            stdout="",
            stderr="old retrieved log line said usage limit",
        )
        with mock.patch.object(
            self.plugin, "_run_child_process", return_value=completed
        ):
            with self.assertRaises(WrapperTimeoutError):
                self.plugin._invoke_wrapper(
                    "/tmp/wrapper",
                    "prompt text",
                    180,
                    self.plugin._resolve_runtime_write_paths(),
                    mode="deep",
                    soju_transport_config=self.plugin._transport_config_path,
                    soju_cutoff="2026-08-17T21:30:28.000Z",
                )

    def test_high_mode_wrapper_uses_hardcoded_overrides(self):
        completed = types.SimpleNamespace(returncode=0, stdout="wrapper output\n", stderr="")

        with mock.patch.object(self.plugin, "_run_child_process", return_value=completed) as run_mock:
            output = self.plugin._invoke_wrapper(
                "/tmp/wrapper",
                "prompt text",
                42,
                self.plugin._resolve_runtime_write_paths(),
                mode="terrahigh",
            )

        self.assertEqual(output, "wrapper output")
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[:4], ["/tmp/wrapper", "--timeout", "42", "--mode"])
        self.assertIn("terrahigh", cmd)
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
                    mode="terra",
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
        terra = codex_wrapper._runtime_settings(codex_wrapper.MODE_TERRA)
        terra_high = codex_wrapper._runtime_settings(codex_wrapper.MODE_TERRA_HIGH)
        self.assertEqual(terra["model"], "gpt-5.6-terra")
        self.assertEqual(terra["reasoning_effort"], "medium")
        self.assertEqual(terra_high["model"], "gpt-5.6-terra")
        self.assertEqual(terra_high["reasoning_effort"], "high")
        luna = codex_wrapper._runtime_settings(codex_wrapper.MODE_LUNA)
        luna_high = codex_wrapper._runtime_settings(codex_wrapper.MODE_LUNA_HIGH)
        self.assertEqual(luna["model"], "gpt-5.6-luna")
        self.assertEqual(luna["reasoning_effort"], "medium")
        self.assertEqual(luna_high["model"], "gpt-5.6-luna")
        self.assertEqual(luna_high["reasoning_effort"], "high")
        deep = codex_wrapper._runtime_settings(codex_wrapper.MODE_DEEP)
        self.assertEqual(deep["model"], "gpt-5.6-luna")
        self.assertEqual(deep["reasoning_effort"], "high")
        self.assertEqual(deep["web_search"], "disabled")
        self.assertTrue(deep["soju_history"])
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
        settings = codex_wrapper._runtime_settings(codex_wrapper.MODE_TERRA_HIGH)
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

    def test_deep_exec_exposes_only_allowlisted_soju_mcp_tools(self):
        settings = codex_wrapper._runtime_settings(codex_wrapper.MODE_DEEP)
        layout = {"agent_cwd": os.path.join(self.tmpdir, "agent-cwd")}
        cmd = codex_wrapper._exec_command(
            "/opt/codex/bin/codex",
            settings,
            layout,
            os.path.join(self.tmpdir, "last-message.txt"),
        )

        self.assertIn('web_search="disabled"', cmd)
        self.assertIn("features.shell_tool=false", cmd)
        self.assertTrue(
            any(item.startswith("mcp_servers.soju_history.command=") for item in cmd)
        )
        self.assertIn(
            'mcp_servers.soju_history.enabled_tools=["search","search_summary","history_summary","context","conversations","speaker_history","aggregate"]',
            cmd,
        )
        self.assertIn(
            'mcp_servers.soju_history.default_tools_approval_mode="approve"', cmd
        )
        self.assertIn(
            'mcp_servers.soju_history.env_vars=["CODEX_SOJU_TRANSPORT_CONFIG","CODEX_SOJU_CUTOFF","CODEX_SOJU_TELEMETRY_PATH","CODEX_SOJU_REQUEST_ID"]',
            cmd,
        )

    def test_deep_child_env_includes_transport_config_and_cutoff(self):
        selected = os.path.join(self.tmpdir, "transport.json")
        layout = {
            "temp": self.tmpdir,
            "agent_cwd": os.path.join(self.tmpdir, "agent-cwd"),
            "output": self.tmpdir,
        }
        child_env = codex_wrapper._codex_child_env(
            "/opt/codex/bin/codex",
            layout,
            code_home=self.runtime_home,
            soju_transport_config=selected,
            soju_cutoff="2026-08-17T21:30:28.000Z",
            soju_request_id="request-abc123",
        )
        self.assertEqual(child_env["CODEX_SOJU_TRANSPORT_CONFIG"], selected)
        self.assertEqual(
            child_env["CODEX_SOJU_CUTOFF"], "2026-08-17T21:30:28.000Z"
        )
        self.assertEqual(
            child_env["CODEX_SOJU_TELEMETRY_PATH"],
            os.path.join(self.tmpdir, "soju-tool-telemetry.jsonl"),
        )
        self.assertEqual(child_env["CODEX_SOJU_REQUEST_ID"], "request-abc123")

    def test_deep_transport_config_is_required(self):
        with self.assertRaisesRegex(RuntimeError, "requires --soju-transport-config"):
            codex_wrapper._resolve_soju_transport_config(None)

    def test_deep_cutoff_is_required_and_validated(self):
        with self.assertRaisesRegex(RuntimeError, "requires --soju-cutoff"):
            codex_wrapper._resolve_soju_cutoff(None)
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            codex_wrapper._resolve_soju_cutoff("not-a-time")
        with self.assertRaisesRegex(RuntimeError, "must be UTC"):
            codex_wrapper._resolve_soju_cutoff("2026-08-17T21:30:28-04:00")
        self.assertEqual(
            codex_wrapper._resolve_soju_cutoff("2026-08-17T21:30:28Z"),
            "2026-08-17T21:30:28.000Z",
        )

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

    def test_turn_usage_extracts_exact_final_counters(self):
        stream = "\n".join(
            [
                '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":10,"reasoning_output_tokens":4}}',
                "not json",
                '{"type":"turn.completed","usage":{"input_tokens":250,"cached_input_tokens":80,"output_tokens":30,"reasoning_output_tokens":12,"cache_write_input_tokens":5}}',
            ]
        )
        self.assertEqual(
            codex_wrapper._turn_usage(stream),
            {
                "input_tokens": 250,
                "cached_input_tokens": 80,
                "output_tokens": 30,
                "reasoning_output_tokens": 12,
                "cache_write_input_tokens": 5,
                "total_tokens": 280,
            },
        )
        self.assertIsNone(codex_wrapper._turn_usage('{"type":"turn.started"}'))

    def test_sanitize_rate_limits_preserves_dynamic_buckets(self):
        result = codex_wrapper._sanitize_rate_limits(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "limitName": None,
                        "planType": "plus",
                        "primary": {
                            "usedPercent": 17,
                            "windowDurationMins": 300,
                            "resetsAt": 1780000000,
                        },
                        "secondary": None,
                        "credits": {
                            "hasCredits": False,
                            "unlimited": False,
                            "balance": "0",
                            "futurePrivateField": "excluded",
                        },
                        "unrecognized": "excluded",
                    },
                    "another_model": {
                        "limitId": "another_model",
                        "limitName": "Another model",
                        "primary": {"usedPercent": 3},
                    },
                },
                "rateLimitResetCredits": {
                    "availableCount": 2,
                    "credits": [{"id": "private-credit-id"}],
                },
            }
        )
        self.assertEqual(set(result["buckets"]), {"codex", "another_model"})
        self.assertEqual(
            result["buckets"]["codex"]["primary"]["window_duration_minutes"],
            300,
        )
        self.assertEqual(result["rate_limit_reset_credits_available"], 2)
        rendered = json.dumps(result)
        self.assertNotIn("futurePrivateField", rendered)
        self.assertNotIn("private-credit-id", rendered)
        self.assertNotIn("unrecognized", rendered)

    def test_quota_snapshot_uses_documented_app_server_sequence(self):
        fake_proc = types.SimpleNamespace(
            stdin=io.BytesIO(),
            stdout=object(),
            pid=1234,
        )
        responses = [
            ({"id": 1, "result": {"userAgent": "test"}}, b""),
            (
                {
                    "id": 2,
                    "result": {
                        "rateLimits": {
                            "limitId": "codex",
                            "primary": {"usedPercent": 9},
                        }
                    },
                },
                b"",
            ),
        ]
        with mock.patch.object(
            codex_wrapper.subprocess, "Popen", return_value=fake_proc
        ) as popen_mock, mock.patch.object(
            codex_wrapper, "_read_rpc_response", side_effect=responses
        ), mock.patch.object(codex_wrapper, "_stop_app_server") as stop_mock:
            result = codex_wrapper._read_quota_snapshot(
                "/opt/codex/bin/codex",
                {"CODEX_HOME": self.source_home},
                self.tmpdir,
            )

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["buckets"]["codex"]["primary"]["used_percent"], 9)
        self.assertEqual(
            popen_mock.call_args.args[0],
            ["/opt/codex/bin/codex", "app-server", "--listen", "stdio://"],
        )
        sent = fake_proc.stdin.getvalue().decode("utf-8")
        self.assertIn('"method":"initialize"', sent)
        self.assertIn('"method":"initialized"', sent)
        self.assertIn('"method":"account/rateLimits/read"', sent)
        self.assertNotIn("turn/start", sent)
        stop_mock.assert_called_once_with(fake_proc)

    def test_quota_snapshot_timeout_is_nonfatal(self):
        fake_proc = types.SimpleNamespace(
            stdin=io.BytesIO(),
            stdout=object(),
            pid=1234,
        )
        with mock.patch.object(
            codex_wrapper.subprocess, "Popen", return_value=fake_proc
        ), mock.patch.object(
            codex_wrapper,
            "_read_rpc_response",
            side_effect=TimeoutError("slow"),
        ), mock.patch.object(codex_wrapper, "_stop_app_server") as stop_mock:
            result = codex_wrapper._read_quota_snapshot(
                "/opt/codex/bin/codex",
                {"CODEX_HOME": self.source_home},
                self.tmpdir,
            )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"], "timeout")
        stop_mock.assert_called_once_with(fake_proc)

    def test_usage_record_is_private_jsonl(self):
        layout = {"output": self.tmpdir}
        record = {
            "schema_version": 1,
            "mode": "deep",
            "tokens": {"total_tokens": 123},
        }
        self.assertTrue(codex_wrapper._append_usage_record(layout, record))
        path = os.path.join(self.tmpdir, codex_wrapper.USAGE_LOG_FILENAME)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.loads(handle.readline()), record)

    def test_usage_record_rotates_at_size_limit(self):
        layout = {"output": self.tmpdir}
        path = os.path.join(self.tmpdir, codex_wrapper.USAGE_LOG_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("old telemetry")
        with mock.patch.object(codex_wrapper, "USAGE_LOG_MAX_BYTES", 5):
            self.assertTrue(
                codex_wrapper._append_usage_record(layout, {"mode": "terra"})
            )
        with open(path + ".1", "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old telemetry")
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.loads(handle.readline()), {"mode": "terra"})


class IrclogsBotClientUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="irclogs-client-test-")
        self.identity = os.path.join(self.tmpdir, "identity")
        self.known_hosts = os.path.join(self.tmpdir, "known_hosts")
        self.config_path = os.path.join(self.tmpdir, "transport.json")
        for path in (self.identity, self.known_hosts):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("test material")
            os.chmod(path, 0o600)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "destination": "bot@example.test",
                    "identity_file": "identity",
                    "known_hosts_file": "known_hosts",
                },
                handle,
            )
        os.chmod(self.config_path, 0o600)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_loads_fixed_private_transport_config(self):
        with mock.patch.dict(
            os.environ, {irclogs_bot_client.CONFIG_ENV: self.config_path}, clear=False
        ):
            destination, identity, known_hosts = irclogs_bot_client._load_config()
        self.assertEqual(destination, "bot@example.test")
        self.assertEqual(identity, self.identity)
        self.assertEqual(known_hosts, self.known_hosts)

    def test_rejects_unknown_config_fields(self):
        with open(self.config_path, "r+", encoding="utf-8") as handle:
            config = json.load(handle)
            config["remote_command"] = "forbidden"
            handle.seek(0)
            json.dump(config, handle)
            handle.truncate()
        with mock.patch.dict(
            os.environ, {irclogs_bot_client.CONFIG_ENV: self.config_path}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected fields"):
                irclogs_bot_client._load_config()

    def test_rejects_group_readable_identity(self):
        os.chmod(self.identity, 0o640)
        with mock.patch.dict(
            os.environ, {irclogs_bot_client.CONFIG_ENV: self.config_path}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "permissions are too broad"):
                irclogs_bot_client._load_config()


class SojuHistoryToolsUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="soju-history-tools-test-")
        self.config_path = os.path.join(self.tmpdir, "transport.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                soju_history_tools.TRANSPORT_CONFIG_ENV: self.config_path,
                soju_history_tools.CUTOFF_ENV: "2026-08-17T21:30:28.000Z",
            },
            clear=False,
        )
        self.env_patch.start()
        soju_history_tools._tool_call_count = 0

    def tearDown(self):
        self.env_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_exposes_only_canonical_history_tools_with_policy_caps(self):
        definitions = {
            item["name"]: item for item in soju_history_tools._tool_definitions()
        }
        self.assertEqual(
            set(definitions),
            {
                "search",
                "search_summary",
                "history_summary",
                "context",
                "conversations",
                "speaker_history",
                "aggregate",
            },
        )
        self.assertEqual(
            definitions["search"]["inputSchema"]["properties"]["limit"]["maximum"],
            20,
        )
        self.assertEqual(
            definitions["conversations"]["inputSchema"]["properties"]["limit"]["maximum"],
            5,
        )
        self.assertEqual(
            definitions["speaker_history"]["inputSchema"]["properties"]["limit"]["maximum"],
            80,
        )
        self.assertEqual(
            definitions["aggregate"]["inputSchema"]["properties"]["limit"]["maximum"],
            50,
        )
        aggregate_properties = definitions["aggregate"]["inputSchema"]["properties"]
        self.assertIn("summary_only", aggregate_properties)
        self.assertEqual(aggregate_properties["summary_only"]["type"], "boolean")
        self.assertNotIn("to_time", aggregate_properties)
        for definition in definitions.values():
            self.assertFalse(definition["inputSchema"]["additionalProperties"])
            self.assertNotIn("to_time", definition["inputSchema"]["properties"])
        history_properties = definitions["history_summary"]["inputSchema"][
            "properties"
        ]
        self.assertEqual(set(history_properties), {"from_time", "until_time"})
        for forbidden in (
            "query",
            "limit",
            "order",
            "sender",
            "scope",
            "target",
            "network",
        ):
            self.assertNotIn(forbidden, history_properties)

    def test_speaker_history_maps_to_hyphenated_transport_command(self):
        completed = types.SimpleNamespace(
            returncode=0,
            stdout='{"type":"speaker_meta","messages":0}\n',
        )
        with mock.patch.object(
            soju_history_tools.subprocess, "run", return_value=completed
        ) as run_mock:
            result = soju_history_tools._call_transport(
                "speaker-history",
                {"speaker_group": ["alice"], "mode": "sample", "limit": 10},
            )
        request = json.loads(run_mock.call_args.kwargs["input"])
        self.assertEqual(request["command"], "speaker-history")
        self.assertEqual(request["speaker_group"], ["alice"])
        self.assertEqual(request["limit"], 10)
        self.assertEqual(request["to_time"], "2026-08-17T21:30:28.000Z")
        self.assertIn('"speaker_meta"', result)
        child_env = run_mock.call_args.kwargs["env"]
        self.assertEqual(child_env["CODEX_SOJU_TRANSPORT_CONFIG"], self.config_path)
        self.assertNotIn("CODEX_HOME", child_env)

    def test_search_summary_preserves_zero_one_and_two_edge_messages(self):
        cases = (
            (
                0,
                '{"type":"search_meta","matching_messages":0,'
                '"messages_returned":0,"effective_to_time":'
                '"2026-08-17T21:30:28.000Z"}\n',
                [],
            ),
            (
                1,
                '{"type":"search_meta","matching_messages":1,'
                '"messages_returned":1,"effective_to_time":'
                '"2026-08-17T21:30:28.000Z"}\n'
                '{"id":41,"time":"2021-01-02T03:04:05.000Z",'
                '"sender":"alice","text":"evidence one",'
                '"edge":"first_and_last"}\n',
                ["first_and_last"],
            ),
            (
                2,
                '{"type":"search_meta","matching_messages":544,'
                '"messages_returned":2,"effective_to_time":'
                '"2026-08-17T21:30:28.000Z"}\n'
                '{"id":41,"time":"2021-01-02T03:04:05.000Z",'
                '"sender":"alice","text":"first evidence","edge":"first"}\n'
                '{"id":99,"time":"2025-06-07T08:09:10.000Z",'
                '"sender":"bob","text":"last evidence","edge":"last"}\n',
                ["first", "last"],
            ),
        )
        for expected_messages, stdout, expected_edges in cases:
            completed = types.SimpleNamespace(returncode=0, stdout=stdout)
            with mock.patch.object(
                soju_history_tools.subprocess, "run", return_value=completed
            ) as run_mock:
                rendered = soju_history_tools._call_transport(
                    "search-summary", {"query": "topic-alpha"}
                )
            records = [json.loads(line) for line in rendered.splitlines()]
            self.assertEqual(records[0]["messages_returned"], expected_messages)
            self.assertEqual(
                [record["edge"] for record in records[1:]], expected_edges
            )
            if expected_messages == 2:
                self.assertEqual(records[0]["matching_messages"], 544)
                self.assertEqual(records[1]["id"], 41)
                self.assertEqual(records[2]["time"], "2025-06-07T08:09:10.000Z")
            request = json.loads(run_mock.call_args.kwargs["input"])
            self.assertEqual(request["command"], "search-summary")
            self.assertNotIn("limit", request)
            self.assertNotIn("order", request)

    def test_history_summary_preserves_zero_one_and_two_boundaries_and_null_text(self):
        cases = (
            (
                '{"type":"history_meta","matching_messages":0,'
                '"first_time":null,"last_time":null,"messages_returned":0,'
                '"from_time":null,"effective_to_time":'
                '"2026-08-17T21:30:28.000Z"}\n',
                [],
            ),
            (
                '{"type":"history_meta","matching_messages":1,'
                '"first_time":"2020-01-01T00:00:00.000Z",'
                '"last_time":"2020-01-01T00:00:00.000Z",'
                '"messages_returned":1,"from_time":null,'
                '"effective_to_time":"2026-08-17T21:30:28.000Z"}\n'
                '{"id":1,"time":"2020-01-01T00:00:00.000Z",'
                '"network":"libera","target":"##debate2016",'
                '"sender":"alice","text":"only message",'
                '"edge":"first_and_last"}\n',
                ["first_and_last"],
            ),
            (
                '{"type":"history_meta","matching_messages":3291018,'
                '"first_time":"2018-04-17T00:32:56.000Z",'
                '"last_time":"2026-08-17T21:30:27.000Z",'
                '"messages_returned":2,"from_time":null,'
                '"effective_to_time":"2026-08-17T21:30:28.000Z"}\n'
                '{"id":1,"time":"2018-04-17T00:32:56.000Z",'
                '"network":"freenode","target":"#debate2016",'
                '"sender":"server","text":null,"edge":"first"}\n'
                '{"id":2,"time":"2026-08-17T21:30:27.000Z",'
                '"network":"libera","target":"##debate2016",'
                '"sender":"alice","text":"latest message","edge":"last"}\n',
                ["first", "last"],
            ),
        )
        for stdout, expected_edges in cases:
            completed = types.SimpleNamespace(returncode=0, stdout=stdout)
            with mock.patch.object(
                soju_history_tools.subprocess, "run", return_value=completed
            ) as run_mock:
                rendered = soju_history_tools._call_transport(
                    "history-summary",
                    {
                        "from_time": "2018-01-01T00:00:00Z",
                        "until_time": "2030-01-01T00:00:00Z",
                    },
                )
            records = [json.loads(line) for line in rendered.splitlines()]
            self.assertEqual(
                [record["edge"] for record in records[1:]], expected_edges
            )
            request = json.loads(run_mock.call_args.kwargs["input"])
            self.assertEqual(
                request,
                {
                    "command": "history-summary",
                    "from_time": "2018-01-01T00:00:00.000Z",
                    "to_time": "2026-08-17T21:30:28.000Z",
                },
            )
            if len(records) == 3:
                self.assertEqual(records[0]["matching_messages"], 3291018)
                self.assertIsNone(records[1]["text"])
                self.assertEqual(records[1]["id"], 1)
                self.assertEqual(records[2]["edge"], "last")
                telemetry = soju_history_tools._response_telemetry(records)
                self.assertEqual(telemetry["matching_messages"], 3291018)
                self.assertEqual(telemetry["messages_returned"], 2)

    def test_context_schema_and_request_are_bounded_and_scope_free(self):
        context_schema = soju_history_tools.TOOLS["context"]["inputSchema"]
        properties = context_schema["properties"]
        self.assertEqual(properties["id"]["minimum"], 1)
        self.assertEqual(properties["before"]["maximum"], 8)
        self.assertEqual(properties["after"]["maximum"], 12)
        for forbidden in ("network", "target", "scope", "to_time"):
            self.assertNotIn(forbidden, properties)

        completed = types.SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"context_meta","anchor_id":6420021,"before":6,'
                '"after":10,"messages_returned":1,"network":"freenode",'
                '"target":"#debate2016"}\n'
                '{"id":6420021,"time":"2020-01-01T00:00:00.000Z",'
                '"sender":"alice","text":"anchor evidence","match":true,'
                '"network":"freenode","target":"#debate2016"}\n'
            ),
        )
        with mock.patch.object(
            soju_history_tools.subprocess, "run", return_value=completed
        ) as run_mock:
            rendered = soju_history_tools._call_transport(
                "context",
                {
                    "id": 6420021,
                    "before": 6,
                    "after": 10,
                    "to_time": "2099-01-01T00:00:00.000Z",
                },
            )
        request = json.loads(run_mock.call_args.kwargs["input"])
        self.assertEqual(
            request,
            {"command": "context", "id": 6420021, "before": 6, "after": 10},
        )
        records = [json.loads(line) for line in rendered.splitlines()]
        self.assertEqual(records[0]["network"], "freenode")
        self.assertEqual(records[1]["id"], 6420021)
        self.assertEqual(records[1]["time"], "2020-01-01T00:00:00.000Z")
        self.assertEqual(records[1]["text"], "anchor evidence")
        self.assertTrue(records[1]["match"])
        self.assertNotIn("network", records[1])
        self.assertNotIn("target", records[1])

        for field in ("network", "target", "scope"):
            with self.assertRaisesRegex(soju_history_tools.ToolError, "scope is fixed"):
                soju_history_tools._prepare_request(
                    "context", {"id": 6420021, field: "injected"}
                )

    def test_date_ranges_are_normalized_and_capped_by_trusted_cutoff(self):
        request = soju_history_tools._prepare_request(
            "search-summary",
            {
                "query": "topic-alpha",
                "from_time": "2020-01-01T00:00:00Z",
                "until_time": "2030-01-01T00:00:00.000Z",
                "to_time": "2099-01-01T00:00:00.000Z",
            },
        )
        self.assertEqual(request["from_time"], "2020-01-01T00:00:00.000Z")
        self.assertEqual(request["to_time"], "2026-08-17T21:30:28.000Z")
        self.assertNotIn("until_time", request)

        earlier = soju_history_tools._prepare_request(
            "aggregate",
            {
                "group_by": "year",
                "until_time": "2024-01-01T00:00:00Z",
            },
        )
        self.assertEqual(earlier["to_time"], "2024-01-01T00:00:00.000Z")

    def test_invalid_or_reversed_date_ranges_are_rejected(self):
        for arguments in (
            {"query": "x", "from_time": "not-a-time"},
            {"query": "x", "until_time": "2026-01-01"},
            {
                "query": "x",
                "from_time": "2024-01-01T00:00:00Z",
                "until_time": "2024-01-01T00:00:00Z",
            },
            {
                "query": "x",
                "from_time": "2025-01-01T00:00:00Z",
                "until_time": "2024-01-01T00:00:00Z",
            },
        ):
            with self.assertRaises(soju_history_tools.ToolError):
                soju_history_tools._prepare_request("search", arguments)

    def test_exclude_senders_is_only_available_on_supported_operations(self):
        supported = {"search", "search_summary", "conversations", "aggregate"}
        for name, tool in soju_history_tools.TOOLS.items():
            properties = tool["inputSchema"]["properties"]
            self.assertEqual("exclude_senders" in properties, name in supported)
        request = soju_history_tools._prepare_request(
            "aggregate",
            {"group_by": "sender", "exclude_senders": ["bot_a", "bot_b"]},
        )
        self.assertEqual(request["exclude_senders"], ["bot_a", "bot_b"])
        with self.assertRaisesRegex(soju_history_tools.ToolError, "not supported"):
            soju_history_tools._prepare_request(
                "speaker-history",
                {"speaker_group": ["alice"], "exclude_senders": ["bot_a"]},
            )

    def test_aggregate_supports_week_and_year_with_monday_description(self):
        aggregate = soju_history_tools.TOOLS["aggregate"]
        choices = aggregate["inputSchema"]["properties"]["group_by"]["enum"]
        self.assertEqual(choices, ["sender", "day", "week", "month", "year"])
        self.assertIn("UTC Monday date", aggregate["description"])
        self.assertIn("not an ISO week number", aggregate["description"])

    def test_compact_conversation_preserves_completeness_and_evidence(self):
        records = [
            {
                "type": "conversation_meta",
                "candidates_returned": 1,
                "candidates_truncated": False,
                "scan_complete": True,
            },
            {
                "type": "conversation",
                "network": "libera",
                "target": "##debate2016",
                "messages": [
                    {
                        "id": 7,
                        "time": "2025-01-01T00:00:00.000Z",
                        "sender": "alice",
                        "text": "preserved evidence",
                        "match": True,
                        "network": "libera",
                        "target": "##debate2016",
                    }
                ],
            },
        ]
        compact = soju_history_tools._compact_records("conversations", records)
        self.assertTrue(compact[0]["scan_complete"])
        message = compact[1]["messages"][0]
        self.assertEqual(message["id"], 7)
        self.assertEqual(message["time"], "2025-01-01T00:00:00.000Z")
        self.assertEqual(message["text"], "preserved evidence")
        self.assertTrue(message["match"])
        self.assertNotIn("network", message)
        self.assertNotIn("target", message)

    def test_tool_telemetry_contains_metadata_but_no_query_or_message_body(self):
        telemetry_path = os.path.join(self.tmpdir, "soju-tool-telemetry.jsonl")
        completed = types.SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"search_meta","matching_messages":1,'
                '"messages_returned":1}\n'
                '{"id":7,"time":"2025-01-01T00:00:00.000Z",'
                '"sender":"alice","text":"SECRET MESSAGE BODY",'
                '"edge":"first_and_last"}\n'
            ),
        )
        with mock.patch.dict(
            os.environ,
            {
                soju_history_tools.TELEMETRY_PATH_ENV: telemetry_path,
                soju_history_tools.REQUEST_ID_ENV: "parent-request-123",
            },
            clear=False,
        ), mock.patch.object(
            soju_history_tools.subprocess, "run", return_value=completed
        ):
            soju_history_tools._call_transport(
                "search-summary",
                {"query": "SECRET QUERY TEXT"},
                call_index=4,
            )
            with self.assertRaises(soju_history_tools.ToolError):
                soju_history_tools._call_transport(
                    "search",
                    {
                        "query": "ANOTHER SECRET QUERY",
                        "from_time": "2025-01-01T00:00:00Z",
                        "until_time": "2024-01-01T00:00:00Z",
                    },
                    call_index=5,
                )
        with open(telemetry_path, "r", encoding="utf-8") as handle:
            telemetry_text = handle.read()
        telemetry_records = [
            json.loads(line) for line in telemetry_text.splitlines()
        ]
        telemetry = telemetry_records[0]
        self.assertEqual(telemetry["tool"], "search-summary")
        self.assertEqual(telemetry["status"], "success")
        self.assertRegex(
            telemetry["started_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        )
        self.assertEqual(telemetry["request_id"], "parent-request-123")
        self.assertEqual(telemetry["call_index"], 4)
        self.assertEqual(telemetry["matching_messages"], 1)
        self.assertEqual(telemetry["messages_returned"], 1)
        self.assertIn("duration_ms", telemetry)
        self.assertIn("response_bytes", telemetry)
        self.assertEqual(telemetry_records[1]["status"], "rejection")
        self.assertEqual(telemetry_records[1]["tool"], "search")
        self.assertEqual(telemetry_records[1]["request_id"], "parent-request-123")
        self.assertEqual(telemetry_records[1]["call_index"], 5)
        self.assertNotIn("SECRET QUERY TEXT", telemetry_text)
        self.assertNotIn("ANOTHER SECRET QUERY", telemetry_text)
        self.assertNotIn("SECRET MESSAGE BODY", telemetry_text)

    def test_transport_cutoff_overwrites_argument_value(self):
        completed = types.SimpleNamespace(
            returncode=0,
            stdout=(
                '{"matching_messages":2089,"groups_total":839,'
                '"groups_returned":0,"groups_truncated":false,'
                '"summary_only":true}\n'
            ),
        )
        with mock.patch.object(
            soju_history_tools.subprocess, "run", return_value=completed
        ) as run_mock:
            result = soju_history_tools._call_transport(
                "aggregate",
                {
                    "query": "topic-beta",
                    "group_by": "day",
                    "summary_only": True,
                    "to_time": "1900-01-01T00:00:00.000Z",
                },
            )
        request = json.loads(run_mock.call_args.kwargs["input"])
        self.assertEqual(request["to_time"], "2026-08-17T21:30:28.000Z")
        response = json.loads(result)
        self.assertEqual(response["groups_total"], 839)
        self.assertEqual(response["matching_messages"], 2089)

    def test_missing_or_malformed_transport_cutoff_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(soju_history_tools.CUTOFF_ENV, None)
            with self.assertRaises(soju_history_tools.ToolError):
                soju_history_tools._trusted_cutoff()
        for value in ("not-a-time", "2026-08-17T21:30:28-04:00"):
            with mock.patch.dict(
                os.environ, {soju_history_tools.CUTOFF_ENV: value}, clear=False
            ):
                with self.assertRaises(soju_history_tools.ToolError):
                    soju_history_tools._trusted_cutoff()

    def test_remote_policy_error_is_returned_safely(self):
        completed = types.SimpleNamespace(
            returncode=0,
            stdout='{"error":"order must be asc or desc"}\n',
        )
        with mock.patch.object(
            soju_history_tools.subprocess, "run", return_value=completed
        ):
            with self.assertRaisesRegex(
                soju_history_tools.ToolError, "order must be asc or desc"
            ):
                soju_history_tools._call_transport(
                    "search", {"query": "needle", "order": "sideways"}
                )

    def test_empty_success_is_a_clear_no_match(self):
        completed = types.SimpleNamespace(returncode=0, stdout="")
        with mock.patch.object(
            soju_history_tools.subprocess, "run", return_value=completed
        ):
            result = soju_history_tools._call_transport(
                "search", {"query": "missing"}
            )
        self.assertEqual(result, "No matching history was found.")

    def test_transport_timeout_is_safe(self):
        with mock.patch.object(
            soju_history_tools.subprocess,
            "run",
            side_effect=TimeoutExpired(cmd="client", timeout=30),
        ):
            with self.assertRaisesRegex(soju_history_tools.ToolError, "timed out"):
                soju_history_tools._call_transport("aggregate", {})

    def test_mcp_budget_stops_runaway_queries(self):
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "aggregate", "arguments": {}},
        }
        with mock.patch.object(soju_history_tools, "MAX_TOOL_CALLS", 1), mock.patch.object(
            soju_history_tools, "_call_transport", return_value="metadata"
        ) as call_mock:
            first = soju_history_tools._handle_request(request)
            second = soju_history_tools._handle_request(request)
        self.assertFalse(first["result"]["isError"])
        self.assertTrue(second["result"]["isError"])
        call_mock.assert_called_once_with("aggregate", {}, call_index=1)



if __name__ == "__main__":
    unittest.main()
