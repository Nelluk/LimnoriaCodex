"""Offline behavioral tests for Markets adapters, evidence, output and publishing."""
import io
import json
import tempfile
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


def selection():
    return {"interpretation": "Republican House seats after the midterms.", "groups": [
        {"label": "House seats", "note": "Lower brackets distinguish the likely seat counts.",
         "events": [{"platform": "polymarket", "event_id": "house-lower", "outcome_ids": ["170"]}]}]}


class MarketBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name
        self.e = event()
        markets.save_json(markets.evidence_path(self.directory, "polymarket", "house-lower"), self.e)

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

    def test_unknown_closed_duplicate_or_unfetched_selections_are_rejected(self):
        for bad_id in ("invented", "old"):
            s = selection()
            s["groups"][0]["events"][0]["outcome_ids"] = [bad_id]
            with self.assertRaises(ValueError):
                markets.validate_selection(s, self.directory)
        s = selection()
        s["groups"][0]["events"][0]["event_id"] = "unfetched"
        with self.assertRaises(ValueError):
            markets.validate_selection(s, self.directory)
        s = selection()
        s["groups"] *= 2
        with self.assertRaises(ValueError):
            markets.validate_selection(s, self.directory)
        self.e["outcomes"][0]["accepting_orders"] = False
        markets.save_json(markets.evidence_path(self.directory, "polymarket", "house-lower"), self.e)
        with self.assertRaises(ValueError):
            markets.validate_selection(selection(), self.directory)

    def test_render_uses_evidence_quotes_and_escapes_untrusted_markdown(self):
        s = selection()
        groups = markets.validate_selection(s, self.directory)
        groups[0]["label"] = '<script>alert(1)</script> [click](https://evil.test)'
        report = markets.render("House seats", s, groups, [])
        self.assertIn("47.0% mid", report)
        self.assertIn("45.0¢", report)
        self.assertNotIn("<script>", report)
        self.assertNotIn("[click](https://evil.test)", report)
        self.assertNotIn("Old contract", report)
        summary = markets.summary(groups, " | https://pasters.io/abc.md", max_bytes=170)
        self.assertLessEqual(len(summary.encode()), 170)
        self.assertTrue(summary.endswith("https://pasters.io/abc.md"))

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
        self.assertIn('mcp_servers.markets.enabled_tools=["search_markets","get_event"]', joined)
        self.assertNotIn("mcp_servers.soju_history", joined)
        self.assertIn('features.shell_tool=false', joined)
        with self.assertRaises(RuntimeError):
            codex_wrapper._runtime_settings("markets", web_search_context_size="high")


class MarketsCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin, self.irc, self.msg = DummyCodex(), FakeIrc(), FakeMsg("Nelluk", "##test", "@markets house seats")
        self.plugin._config.update(marketsModel="gpt-6.1-terra", marketsEffort="low", marketsTimeoutSeconds=120, marketsPublishEnabled=True)
        self.paths = {"base": self.temp.name, "state": self.temp.name, "temp": self.temp.name, "output": self.temp.name}

    def invoke(self, bad=False, publish_error=False):
        def wrapper(*args, **kwargs):
            directory = kwargs["markets_request_dir"]
            markets.save_json(markets.evidence_path(directory, "polymarket", "house-lower"), event())
            markets.save_json(Path(directory) / "searches.json", [{"platform": "polymarket", "query": "house seats", "status": "2 candidates"}, {"platform": "kalshi", "query": "house seats", "status": "0 candidates"}])
            s = selection()
            if bad:
                s["groups"][0]["events"][0]["outcome_ids"] = ["invented"]
            return json.dumps(s)
        with mock.patch.object(self.plugin, "_resolve_runtime_write_paths", return_value=self.paths), mock.patch.object(self.plugin, "_invoke_wrapper", side_effect=wrapper) as run, mock.patch.object(markets, "publish", side_effect=OSError if publish_error else None, return_value="https://pasters.io/test.md") as upload:
            self.plugin._handle_markets_request(self.irc, self.msg, "house seats")
        return run, upload

    def test_command_uses_no_chat_memory_and_publishes_verified_report(self):
        run, upload = self.invoke()
        self.assertEqual(run.call_args.kwargs["context_policy"], "none")
        self.assertEqual(run.call_args.kwargs["model"], "gpt-6.1-terra")
        self.assertIn("47.0% mid", self.irc.replies[-1])
        self.assertIn("https://pasters.io/test.md", self.irc.replies[-1])
        self.assertEqual(self.plugin._active_executions, 0)
        self.assertEqual(len(list(Path(self.temp.name).glob("markets-*.md"))), 1)
        self.assertEqual([p for p in Path(self.temp.name).glob("markets-*") if p.is_dir()], [])
        self.assertTrue(upload.called)

    def test_invalid_selection_never_published_and_slot_released(self):
        _, upload = self.invoke(bad=True)
        self.assertFalse(upload.called)
        self.assertIn("verified answer", self.irc.replies[-1])
        self.assertEqual(self.plugin._active_executions, 0)

    def test_upload_failure_preserves_summary_and_local_report(self):
        self.invoke(publish_error=True)
        self.assertIn("47.0% mid", self.irc.replies[-1])
        self.assertIn("Report unavailable", self.irc.replies[-1])
        self.assertEqual(len(list(Path(self.temp.name).glob("markets-*.md"))), 1)

    def test_busy_command_does_not_launch_or_publish(self):
        self.plugin._active_executions = 1
        with mock.patch.object(self.plugin, "_invoke_wrapper") as run:
            self.plugin._handle_markets_request(self.irc, self.msg, "nomination")
        run.assert_not_called()
        self.assertIn("busy", self.irc.replies[-1])
