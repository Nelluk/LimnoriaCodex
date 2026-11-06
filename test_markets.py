"""Offline behavioral tests for Markets adapters, evidence, output and publishing."""
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from . import markets
from .test import DummyCodex, FakeIrc, FakeMsg, codex_wrapper


def event():
    return {"platform": "polymarket", "event_id": "house-lower", "title": "House seats (Lower Brackets)",
            "url": "https://polymarket.com/event/house-lower", "rules": "Seat count after the election.",
            "retrieved_at": "2026-11-05T15:00:00Z", "closed": False,
            "outcomes": [{"id": "170", "label": "170-174", "question": "170 to 174 seats?", "status": "active", "rules": "Seat count after the election.",
                          "accepting_orders": True, "volume_24h_usd": 500,
                          **markets.quote_fields("0.45", "0.49", "0.47")},
                         {"id": "old", "label": "Old contract", "status": "closed", "rules": "", **markets.quote_fields(.9, .95, .91)}]}


def overview():
    return {"irc_summary": "House seats: 170–174 ~47%",
            "report_markdown": "# House seats\n\nThe 170–174 bracket is around 47%.\n\n[Polymarket](https://polymarket.com/event/house-lower)\n"}


class MarketBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name
        self.e = event()
        markets.save_json(markets.evidence_path(self.directory, "polymarket", "house-lower"), self.e)

    def test_tool_timings_record_slow_failures_without_queries_or_error_text(self):
        server = markets.ToolServer(self.directory)
        args = {"platform": "polymarket", "query": "private nick and channel"}
        with mock.patch.object(markets, "search", side_effect=TimeoutError("secret error /home/person")):
            with self.assertRaises(TimeoutError):
                server.call("search_markets", args)
        rows = json.loads((Path(self.directory) / "tool-timings.json").read_text())
        self.assertEqual(rows[0]["status"], "error")
        self.assertGreaterEqual(rows[0]["duration_ms"], 0)
        self.assertNotIn("private", json.dumps(rows))
        self.assertNotIn("secret", json.dumps(rows))
        self.assertNotIn("/home", json.dumps(rows))

    def test_public_diagnostics_allowlist_and_cost_do_not_leak_private_fields(self):
        markets.save_json(Path(self.directory) / "diagnostics.json", {
            "duration_ms": 12000, "request_id": "private-id", "query": "private query",
            "quota": {"plan_type": "private account"},
            "tokens": {"input_tokens": 100000, "cached_input_tokens": 80000,
                       "output_tokens": 1000, "reasoning_output_tokens": 200,
                       "cache_write_input_tokens": 0, "nickname": "private nick"}})
        markets.save_json(Path(self.directory) / "tool-timings.json", [
            {"tool": "search_markets", "platform": "polymarket", "status": "success",
             "duration_ms": 300, "result_count": 4, "response_bytes": 1000,
             "query": "private query", "error": "/home/private"},
            {"tool": "<script>private</script>", "platform": "private", "status": "private",
             "result_count": "private", "duration_ms": "private"}])
        data = markets.diagnostic_data(self.directory, "gpt-6.1-terra", "low", 12500, "success")
        footer = markets.diagnostic_footer(data)
        self.assertNotIn("private", json.dumps(data) + footer)
        self.assertNotIn("<script>", footer)
        self.assertIn("<details>", footer)
        self.assertIn("<summary>Diagnostics</summary>", footer)
        self.assertIn("$0.0290", footer)  # Reasoning is already in output.
        self.assertAlmostEqual(markets.api_equivalent(data), .029)
        data["model"] = "gpt-6-luna"
        self.assertAlmostEqual(markets.api_equivalent(data), .0033)
        data["tokens"]["cache_write_input_tokens"] = 10
        self.assertIsNone(markets.api_equivalent(data))

    def test_absent_or_incomplete_diagnostics_never_invent_zero_cost(self):
        data = markets.diagnostic_data(self.directory, "custom/private-model", "low", 1000, "success")
        self.assertEqual(data["model"], "Other model")
        self.assertIsNone(markets.api_equivalent(data))
        self.assertIn("Unavailable", markets.diagnostic_footer(data))

    def test_midpoint_and_last_are_distinguished_and_bad_quotes_not_averaged(self):
        self.assertEqual(markets.quote_fields(.4, .5, .3)["price"], .45)
        self.assertEqual(markets.quote_fields(.6, .5, .3)["price_type"], "last")
        self.assertEqual(markets.quote_fields(0, 1, .3)["price"], .3)
        self.assertIsNone(markets.quote_fields("NaN", "Infinity", -1)["price"])
        self.assertEqual(markets.quote_fields(None, None, None, ".2")["price_type"], "reported")

    def test_poly_json_strings_and_yes_index_and_inactive_status(self):
        raw = {"id": "a", "outcomes": '["No", "Yes"]', "outcomePrices": '["0.1", "0.9"]',
               "active": True, "closed": False, "bestBid": .85, "bestAsk": .95}
        o = markets.poly_outcome(raw)
        self.assertAlmostEqual(o["price"], .9)
        raw.update(outcomes=["Red", "Blue"])
        self.assertIsNone(markets.poly_outcome(raw))

    def test_named_polymarket_sides_use_reported_prices_without_reusing_book(self):
        outcomes = markets.poly_outcomes({"id": "party", "outcomes": ["Democratic", "Republican"],
                     "outcomePrices": ["0.65", "0.35"], "active": True, "bestBid": .64, "bestAsk": .66})
        self.assertEqual([o["id"] for o in outcomes], ["party:0", "party:1"])
        self.assertEqual(outcomes[1]["price"], .35)
        self.assertEqual(outcomes[1]["price_type"], "reported")
        self.assertIsNone(outcomes[0]["bid"])

    def test_required_searches_cannot_be_skipped_but_explicit_platform_is_respected(self):
        coverage = [{"platform": "polymarket", "query": "2028", "status": "3 candidates"}]
        with self.assertRaises(ValueError):
            markets.validate_coverage("2028", coverage)
        markets.validate_coverage("Polymarket 2028", coverage)
        markets.validate_coverage("2028", coverage + [{"platform": "kalshi", "status": "failed"}])

    def test_detail_cache_and_focused_outcome_lookup_keep_one_snapshot(self):
        server = markets.ToolServer(self.directory)
        with mock.patch.object(markets, "get_event", side_effect=AssertionError("must reuse snapshot")):
            result = server.call("get_event", {"platform": "polymarket", "event_id": "house-lower", "outcome_query": "170"})
        self.assertEqual(result["outcomes"][0]["id"], "170")
        self.assertEqual(server.searches[-1]["status"], "cached details")
        self.assertEqual(result["retrieved_at"], self.e["retrieved_at"])

    def test_kalshi_dollars_contract_counts_and_rules(self):
        o = markets.kalshi_outcome({"ticker": "TEST-A", "yes_sub_title": "Candidate A", "status": "active",
                                   "yes_bid_dollars": ".2600", "yes_ask_dollars": ".2700", "volume_fp": "120.50",
                                   "rules_primary": "Wins and accepts nomination", "rules_secondary": "Before deadline"})
        self.assertEqual(o["price"], .265)
        self.assertEqual(o["volume_contracts"], 120.5)
        self.assertIn("accepts", o["rules"])
        self.assertNotIn("volume_usd", o)

    def test_search_exposes_both_bracket_candidates_and_refines_vague_query(self):
        response = {"events": [{"slug": "house", "title": "House seats", "markets": []},
                               {"slug": "house-lower", "title": "House seats (Lower Brackets)", "markets": []}], "hasMore": True}
        with mock.patch.object(markets, "fetch", return_value=response) as fetch:
            result = markets.search("polymarket", "House seats & brackets")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("%26", fetch.call_args.args[0])
        self.assertTrue(result["more"])

    def test_kalshi_event_uses_nested_or_top_level_markets(self):
        for nested in (True, False):
            e = {"event_ticker": "TEST-28", "series_ticker": "TEST", "title": "Test"}
            ms = [{"ticker": "TEST-28-A", "status": "active", "yes_sub_title": "A"}]
            data = {"event": {**e, **({"markets": ms} if nested else {})}, "markets": ms}
            with mock.patch.object(markets, "fetch", return_value=data):
                result = markets.get_event("kalshi", "TEST-28")
            self.assertFalse(result["closed"])
            self.assertEqual(result["outcomes"][0]["id"], "TEST-28-A")

    def test_daily_changes_use_provider_delta_and_matching_kalshi_quote_basis(self):
        poly = markets.poly_outcome({"id": "a", "outcomes": ["Yes", "No"], "outcomePrices": [.7, .3], "oneDayPriceChange": .12})
        self.assertEqual(poly["change_24h"], .12)
        kalshi = {"ticker": "TEST-A", "yes_bid_dollars": ".64", "yes_ask_dollars": ".66",
                  "previous_yes_bid_dollars": ".50", "previous_yes_ask_dollars": ".52"}
        self.assertAlmostEqual(markets.kalshi_outcome(kalshi)["change_24h"], .14)
        kalshi.pop("previous_yes_bid_dollars")
        kalshi["previous_price_dollars"] = ".51"
        self.assertIsNone(markets.kalshi_outcome(kalshi)["change_24h"])
        named = markets.poly_outcomes({"id": "party", "outcomes": ["Democratic", "Republican"],
                                      "outcomePrices": [.7, .3], "oneDayPriceChange": .1})
        self.assertEqual([o["change_24h"] for o in named], [.1, -.1])

    def test_upload_requires_201_and_valid_host_and_returns_markdown_link(self):
        class Response(io.BytesIO):
            status = 201
        with mock.patch.object(markets, "urlopen", return_value=Response(b'https://pasters.io/abc123\n')) as upload:
            self.assertEqual(markets.publish("# report"), "https://pasters.io/abc123.md")
            self.assertEqual(upload.call_args.args[0].data, b"# report")
        for body, status in [(b'https://evil.test/a', 201), (b'https://pasters.io/a', 206), (b'https://pasters.io/a?x=1', 201)]:
            response = Response(body)
            response.status = status
            with mock.patch.object(markets, "urlopen", return_value=response):
                with self.assertRaises(ValueError):
                    markets.publish("report")

    def test_tool_budget_and_failure_coverage_and_evidence_saved_before_selection(self):
        server = markets.ToolServer(self.directory)
        with mock.patch.object(markets, "get_event", return_value=self.e):
            data = server.call("get_event", {"platform": "polymarket", "event_id": "house-lower"})
        self.assertEqual(len(data["outcomes"]), 1)
        with mock.patch.object(markets, "search", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                server.call("search_markets", {"platform": "kalshi", "query": "nominee"})
        coverage = json.loads((Path(self.directory) / "searches.json").read_text())
        self.assertEqual(coverage[-1]["status"], "failed")
        server.calls = markets.MAX_CALLS
        with self.assertRaises(ValueError):
            server.call("search_markets", {"platform": "kalshi", "query": "nominee"})

    def test_wrapper_markets_is_isolated_and_schema_enabled(self):
        settings = codex_wrapper._runtime_settings("markets")
        self.assertEqual((settings["model"], settings["reasoning_effort"], settings["web_search"]), ("gpt-6.1-terra", "low", "disabled"))
        cmd = codex_wrapper._exec_command("codex", settings, {"agent_cwd": self.directory}, "/tmp/final", self.directory)
        joined = " ".join(cmd)
        self.assertIn("--output-schema", cmd)
        self.assertIn('mcp_servers.markets.enabled_tools=["search_markets","get_event","get_price_history"]', joined)
        self.assertNotIn("mcp_servers.soju_history", joined)
        self.assertIn('features.shell_tool=false', joined)
        with self.assertRaises(RuntimeError):
            codex_wrapper._runtime_settings("markets", web_search_context_size="high")

    def test_search_preview_exposes_uncertainty_without_closed_or_extra_contract_data(self):
        def market(label, price, closed=False):
            return {"id": label, "groupItemTitle": label, "active": True, "closed": closed,
                    "outcomes": ["Yes", "No"], "outcomePrices": [price, 1-price]}
        previews = markets.search_leaders([market("A", .51), market("B", .49), market("Closed", 1, True)], "polymarket")
        self.assertEqual(previews, [{"label": "A", "price": .51}, {"label": "B", "price": .49}])

    def test_history_samples_are_past_only_fresh_enough_and_keep_zero_prices(self):
        self.assertEqual(markets.history_snapshot([(100, 0), (201, .8)], 200, max_age=150)["price"], 0)
        self.assertIsNone(markets.history_snapshot([(100, .5)], 201, max_age=100))
        self.assertIsNone(markets.history_snapshot([(201, .8)], 200))

    def test_polymarket_history_keeps_timestamped_samples_and_named_side_tokens(self):
        as_of = 2_000_000
        data = {"history": [{"t": as_of + 1, "p": .99}, {"t": as_of - 60, "p": .04},
                            {"t": as_of - 86400 - 60, "p": .08}, {"t": as_of - 7 * 86400 - 60, "p": .6}]}
        with mock.patch.object(markets, "fetch", return_value=data) as fetch:
            result = markets.price_history(self.e, {**self.e["outcomes"][0], "_history_token": "1234"}, as_of)
        self.assertEqual(result["snapshots"]["latest"]["price"], .04)
        self.assertEqual(result["snapshots"]["24h_ago"]["price"], .08)
        self.assertEqual(result["snapshots"]["7d_ago"]["price"], .6)
        self.assertAlmostEqual(result["sampled_change_24h"], -.04)
        self.assertAlmostEqual(result["sampled_change_7d"], -.56)
        self.assertIn("market=1234", fetch.call_args.args[0])
        self.assertIn("startTs=", fetch.call_args.args[0])
        named = markets.poly_outcomes({"id": "party", "outcomes": ["D", "R"], "outcomePrices": [.6, .4], "clobTokenIds": '["11", "22"]'})
        self.assertEqual([o["_history_token"] for o in named], ["11", "22"])

    def test_kalshi_history_uses_one_quote_basis_without_last_trade_filling(self):
        as_of = 2_000_000
        def candle(at, bid, ask):
            return {"end_period_ts": at, "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask}, "price": {"close_dollars": ".9"}}
        data = {"candlesticks": [candle(as_of - 60, ".03", ".05"),
                                  candle(as_of - 86400 - 60, None, ".2"),
                                  candle(as_of - 7 * 86400 - 60, ".59", ".61")]}
        with mock.patch.object(markets, "fetch", return_value=data):
            result = markets.price_history({"platform": "kalshi", "series_id": "TEST"}, {"id": "TEST-A", "label": "A"}, as_of)
        self.assertEqual(result["snapshots"]["latest"]["price"], .04)
        self.assertIsNone(result["snapshots"]["24h_ago"])
        self.assertIsNone(result["sampled_change_24h"])
        self.assertEqual(result["snapshots"]["7d_ago"]["price"], .6)
        self.assertIn("midpoint", result["basis"])

    def test_history_batches_require_fetched_ids_are_cached_and_have_a_separate_cap(self):
        server = markets.ToolServer(self.directory)
        args = {"platform": "polymarket", "event_id": "house-lower", "outcome_ids": ["170"]}
        point = {"id": "170", "label": "170-174", "snapshots": {"latest": {"price": .47}}, "error": None}
        with mock.patch.object(markets, "price_history", return_value=point) as lookup:
            for _ in range(markets.MAX_HISTORY_CALLS):
                result = server.call("get_price_history", args)
            self.assertEqual(lookup.call_count, 1)
            with self.assertRaises(ValueError):
                server.call("get_price_history", args)
        self.assertEqual(result["outcomes"][0]["snapshots"]["latest"]["price"], .47)
        self.assertEqual(server.searches[-1]["operation"], "history")
        with mock.patch.object(markets, "price_history") as lookup:
            for ids in (["not-fetched"], ["170"] * 5):
                with self.assertRaises(ValueError):
                    markets.ToolServer(self.directory).call("get_price_history", {**args, "outcome_ids": ids})
            lookup.assert_not_called()

    def test_missing_history_is_explicit_without_fake_zero_or_source_failure(self):
        server = markets.ToolServer(self.directory)
        with mock.patch.object(markets, "price_history", side_effect=TimeoutError):
            result = server.call("get_price_history", {"platform": "polymarket", "event_id": "house-lower", "outcome_ids": ["170"]})
        self.assertEqual(result["outcomes"][0]["error"], "history_unavailable")
        self.assertEqual(result["outcomes"][0]["snapshots"], {})
        self.assertEqual(server.searches[-1]["status"], "history unavailable")

    def test_staged_wrapper_keeps_running_plugins_old_contract_until_restart(self):
        for format_name, expected in (("overview", {"irc_summary", "report_markdown"}),
                                      ("selection", {"interpretation", "groups"})):
            settings = codex_wrapper._runtime_settings("markets")
            settings["markets_output"] = format_name
            cmd = codex_wrapper._exec_command("codex", settings, {"agent_cwd": self.directory}, "/tmp/final", self.directory)
            schema_path = cmd[cmd.index("--output-schema") + 1]
            properties = json.loads(Path(schema_path).read_text())["properties"]
            self.assertEqual(set(properties), expected)
            if format_name == "overview":
                self.assertEqual(next(iter(properties)), "report_markdown")

    def test_model_prose_is_preserved_without_reconstructing_or_pairing_quotes(self):
        answer = overview()
        answer["irc_summary"] = "CO-04: Boebert ~4%, Laubacher ~96%"
        answer["report_markdown"] = "# CO-04\n\nBoth venues put Boebert near 4%.\n\n[Source](https://polymarket.com/event/house-lower)"
        text, report = markets.validate_output(answer, self.directory)
        self.assertEqual(text, answer["irc_summary"])
        self.assertIn("Both venues put Boebert near 4%", report)
        self.assertNotIn("4%/", markets.channel_summary(text))
        answer["report_markdown"] = "**[Source](https://polymarket.com/event/house-lower)**"
        self.assertIn("**[Source]", markets.validate_output(answer, self.directory)[1])

    def test_bad_output_envelopes_empty_content_and_unfetched_links_are_rejected(self):
        cases = [[], {"groups": []}, {**overview(), "irc_summary": " "},
                 {**overview(), "report_markdown": "x" * 12001},
                 {**overview(), "report_markdown": "\x02"},
                 {**overview(), "report_markdown": "[Source](https://evil.test/)"},
                 {**overview(), "report_markdown": "[Source](javascript:alert(1))"}]
        for answer in cases:
            with self.subTest(answer=str(answer)[:60]), self.assertRaises(ValueError):
                markets.validate_output(answer, self.directory)

    def test_report_markdown_survives_while_raw_html_and_control_characters_are_disabled(self):
        answer = overview()
        answer["report_markdown"] = "# Overview\n\n**Major move**\n\n<script>alert(1)</script>\x02"
        answer["irc_summary"] = "Result\x02\nhttps://evil.test/"
        text, report = markets.validate_output(answer, self.directory)
        self.assertEqual(text, "Result")
        self.assertIn("# Overview", report)
        self.assertIn("**Major move**", report)
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertNotIn("\x02", report)

    def test_irc_bold_and_topic_shortening_preserve_a_readable_summary_and_link(self):
        text = "Champion: Spurs ~23% | MVP: **Wembanyama ~36% (⬆️6 pp)** | Extra: " + "irrelevant " * 30
        suffix = " | https://pasters.io/a.md"
        result = markets.channel_summary(text, suffix, max_bytes=150)
        self.assertIn("Champion: Spurs ~23%", result)
        self.assertIn("\x02Wembanyama", result)
        self.assertNotIn("irrelevant", result)
        self.assertEqual(result.count("\x02") % 2, 0)
        self.assertTrue(result.endswith(suffix))
        self.assertLessEqual(len(result.encode()), 150)
        result = markets.channel_summary("**" + "⬆️ important " * 80, suffix)
        self.assertEqual(result.count("\x02") % 2, 0)
        self.assertLessEqual(len(result.encode()), 400)


class MarketsCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin, self.irc, self.msg = DummyCodex(), FakeIrc(), FakeMsg("Nelluk", "##test", "@markets house seats")
        self.plugin._config.update(marketsModel="gpt-6.1-terra", marketsEffort="low", marketsTimeoutSeconds=120, marketsPublishEnabled=True)
        self.paths = {"base": self.temp.name, "state": self.temp.name, "temp": self.temp.name, "output": self.temp.name}

    def invoke(self, bad=False, publish_error=False, source_failed=False):
        def wrapper(*args, **kwargs):
            directory = kwargs["markets_request_dir"]
            markets.save_json(markets.evidence_path(directory, "polymarket", "house-lower"), event())
            markets.save_json(Path(directory) / "searches.json", [
                {"platform": "polymarket", "query": "house seats", "status": "2 candidates"},
                {"platform": "kalshi", "query": "house seats", "status": "failed" if source_failed else "0 candidates"}])
            answer = overview()
            if bad:
                answer["report_markdown"] = "[Fake source](https://evil.test/)"
            return json.dumps(answer)
        with mock.patch.object(self.plugin, "_resolve_runtime_write_paths", return_value=self.paths), mock.patch.object(self.plugin, "_invoke_wrapper", side_effect=wrapper) as run, mock.patch.object(markets, "publish", side_effect=OSError if publish_error else None, return_value="https://pasters.io/test.md") as upload:
            self.plugin._handle_markets_request(self.irc, self.msg, "house seats")
        return run, upload

    def test_command_publishes_model_report_and_uses_no_chat_memory(self):
        run, upload = self.invoke()
        self.assertEqual(run.call_args.kwargs["context_policy"], "none")
        self.assertEqual(run.call_args.kwargs["model"], "gpt-6.1-terra")
        self.assertIn("170–174 ~47%", self.irc.replies[-1])
        self.assertIn("https://pasters.io/test.md", self.irc.replies[-1])
        self.assertEqual(self.plugin._active_executions, 0)
        self.assertEqual(len(list(Path(self.temp.name).glob("markets-*.md"))), 1)
        self.assertEqual([p for p in Path(self.temp.name).glob("markets-*") if p.is_dir()], [])
        self.assertIn("# House seats", upload.call_args.args[0])

    def test_old_120_second_setting_is_capped_and_upload_gets_remaining_budget(self):
        run, upload = self.invoke()
        self.assertGreater(run.call_args.args[2], 30)
        self.assertLessEqual(run.call_args.args[2], 34)
        self.assertLessEqual(upload.call_args.kwargs["timeout"], 4)
        self.assertIn("<summary>Diagnostics</summary>", upload.call_args.args[0])
        records = list(Path(self.temp.name).glob("markets-diagnostics-*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Nelluk", records[0].read_text())
        self.assertNotIn("##test", records[0].read_text())

    def test_timeout_preserves_inflight_tool_timing_without_publishing(self):
        from .plugin import WrapperTimeoutError
        def timeout(*args, **kwargs):
            directory = Path(kwargs["markets_request_dir"])
            markets.save_json(directory / "tool-timings.json", [{"tool": "get_event", "platform": "kalshi", "status": "running", "started_ms": 400}])
            raise WrapperTimeoutError("private error")
        with mock.patch.object(self.plugin, "_resolve_runtime_write_paths", return_value=self.paths), mock.patch.object(self.plugin, "_invoke_wrapper", side_effect=timeout), mock.patch.object(markets, "publish") as upload:
            self.plugin._handle_markets_request(self.irc, self.msg, "stephen miller")
        upload.assert_not_called()
        self.assertEqual(self.irc.replies[-1], "Markets search timed out. Please try again.")
        data = json.loads(next(Path(self.temp.name).glob("markets-diagnostics-*.json")).read_text())
        self.assertEqual(data["status"], "timeout")
        self.assertEqual(data["tools"][0]["status"], "running")
        self.assertEqual(self.plugin._active_executions, 0)

    def test_new_plugin_explicitly_requests_overview_wire_format(self):
        with mock.patch.object(self.plugin, "_run_child_process", return_value=types.SimpleNamespace(returncode=0, stdout="{}", stderr="")) as run:
            self.plugin._invoke_wrapper("/bin/echo", "query", 120, self.paths,
                mode="markets", context_policy="none", markets_request_dir=self.temp.name)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--markets-output") + 1], "overview")

    def test_bad_response_never_published_and_slot_released(self):
        _, upload = self.invoke(bad=True)
        self.assertFalse(upload.called)
        self.assertIn("produce an overview", self.irc.replies[-1])
        self.assertEqual(self.plugin._active_executions, 0)

    def test_upload_failure_preserves_overview_and_local_report(self):
        self.invoke(publish_error=True)
        self.assertIn("~47%", self.irc.replies[-1])
        self.assertIn("Report unavailable", self.irc.replies[-1])
        self.assertEqual(len(list(Path(self.temp.name).glob("markets-*.md"))), 1)

    def test_source_failure_is_disclosed_in_report_and_channel(self):
        _, upload = self.invoke(source_failed=True)
        self.assertIn("Some lookups failed", self.irc.replies[-1])
        self.assertIn("source coverage may be incomplete", upload.call_args.args[0])

    def test_busy_command_does_not_launch_or_publish(self):
        self.plugin._active_executions = 1
        with mock.patch.object(self.plugin, "_invoke_wrapper") as run:
            self.plugin._handle_markets_request(self.irc, self.msg, "nomination")
        run.assert_not_called()
        self.assertIn("busy", self.irc.replies[-1])
