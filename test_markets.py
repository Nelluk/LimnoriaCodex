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
        self.assertIn("47%", report)
        self.assertIn("| Outcome | [Polymarket]", report)
        self.assertNotIn("% mid", report)
        self.assertNotIn("<script>", report)
        self.assertNotIn("[click](https://evil.test)", report)
        self.assertNotIn("Old contract", report)
        summary = markets.summary(groups, " | https://pasters.io/abc.md", max_bytes=170)
        self.assertLessEqual(len(summary.encode()), 170)
        self.assertTrue(summary.endswith("https://pasters.io/abc.md"))

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

    def test_channel_highlights_only_large_changes_and_has_balanced_formatting(self):
        s = selection()
        groups = markets.validate_selection(s, self.directory)
        outcome = groups[0]["events"][0][1][0]
        outcome["change_24h"] = .061
        text = markets.summary(groups)
        self.assertIn("47% (⬆️6.1 pp)", text)
        self.assertNotIn("mid", text)
        self.assertEqual(text.count("\x02") % 2, 0)
        outcome["change_24h"] = -.049
        self.assertNotIn("🔻", markets.summary(groups))
        outcome["change_24h"] = -.05
        self.assertIn("🔻5 pp", markets.summary(groups))
        report = markets.render("House seats", s, groups, [])
        self.assertIn("**47%**", report)
        self.assertIn("**🔻5 pp**", report)

    def test_equivalent_outcomes_align_without_repeating_names_or_merging_thresholds(self):
        s = selection()
        groups = markets.validate_selection(s, self.directory)
        left = {**groups[0]["events"][0][1][0], "label": "J.D. Vance", "price": .47}
        right = {**left, "label": "JD Vance", "price": .45}
        e1 = {**event(), "outcomes": [left]}
        e2 = {**event(), "platform": "kalshi", "outcomes": [right]}
        groups[0]["events"] = [(e1, [left]), (e2, [right])]
        text = markets.summary(groups)
        self.assertEqual(text.count("Vance"), 1)
        self.assertIn("47%/45%", text)
        report = markets.render("Nominee", s, groups, [])
        self.assertIn("| J\\.D\\. Vance | 47% | — | 45% | — |", report)
        self.assertNotEqual(markets.outcome_key("At least 174"), markets.outcome_key("170-174"))
        self.assertEqual(markets.outcome_key("Democratic party"), markets.outcome_key("Democratic"))

    def test_summary_prunes_whole_outcomes_before_cutting_any_quote(self):
        groups = markets.validate_selection(selection(), self.directory)
        event_record, outcomes = groups[0]["events"][0]
        outcomes[:] = [{**outcomes[0], "id": str(i), "label": "Candidate " + str(i), "change_24h": .1} for i in range(3)]
        event_record["outcomes"] = outcomes
        text = markets.summary(groups, " | https://pasters.io/a.md", max_bytes=135)
        self.assertLessEqual(len(text.encode()), 135)
        self.assertEqual(text.count("\x02") % 2, 0)
        self.assertIn("47%", text)
        self.assertNotIn("Candidate 2", text)
        self.assertTrue(text.endswith("https://pasters.io/a.md"))

    def test_report_orders_brackets_and_omits_unselected_zero_price_placeholders(self):
        groups = markets.validate_selection(selection(), self.directory)
        e, selected = groups[0]["events"][0]
        base = selected[0]
        e["outcomes"] = [{**base, "id": str(i), "label": label, "price": price} for i, (label, price) in enumerate([
            ("175-179", .53), ("Below 170", .01), ("170-174", .46), ("190+", .001)])]
        groups[0]["events"] = [(e, [e["outcomes"][0]])]
        rows = markets.comparison_rows(groups[0]["events"])
        self.assertEqual([label for label, _ in rows], ["Below 170", "170-174", "175-179"])
        # Explicitly selected long-shot outcomes are retained.
        groups[0]["events"] = [(e, [e["outcomes"][-1]])]
        self.assertIn("190+", [label for label, _ in markets.comparison_rows(groups[0]["events"])])

    def sports_event(self, platform, title, labels, selected=None):
        e = {**event(), "platform": platform, "title": title, "event_id": "test-" + platform}
        base = e["outcomes"][0]
        e["outcomes"] = [{**base, "id": platform + str(i), "label": label, "price": .23 - i * .03} for i, label in enumerate(labels)]
        return e, [e["outcomes"][i] for i in (selected if selected is not None else range(min(3, len(labels))))]

    def test_nba_full_team_names_and_city_names_share_one_row_in_both_source_orders(self):
        poly = self.sports_event("polymarket", "NBA: 2027 Champion", ["San Antonio Spurs", "Philadelphia 76ers", "Oklahoma City Thunder"])
        kalshi = self.sports_event("kalshi", "2027 Pro Basketball Champion", ["San Antonio", "Philadelphia", "Oklahoma City"])
        for events in ([poly, kalshi], [kalshi, poly]):
            rows = markets.comparison_rows(events, selected_only=True)
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(all(o is not None for o in quotes) for _, quotes in rows))
            self.assertIn("San Antonio Spurs", [label for label, _ in rows])
            groups = [{"label": "2027 champion", "note": "", "events": events}]
            text = markets.summary(groups)
            self.assertEqual(text.count("San Antonio"), 1)
            self.assertNotIn("/—", text)

    def test_counterpart_is_filled_from_fetched_quotes_even_when_not_selected(self):
        poly = self.sports_event("polymarket", "NBA Champion", ["San Antonio Spurs", "Philadelphia 76ers"], selected=[0])
        kalshi = self.sports_event("kalshi", "Pro Basketball Champion", ["San Antonio", "Philadelphia"], selected=[1])
        rows = markets.comparison_rows([poly, kalshi], selected_only=True)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(all(o is not None for o in quotes) for _, quotes in rows))

    def test_city_with_multiple_teams_is_not_guessed_from_selected_leaders(self):
        poly = self.sports_event("polymarket", "NBA Champion", ["Los Angeles Lakers", "Los Angeles Clippers"], selected=[0])
        kalshi = self.sports_event("kalshi", "Pro Basketball Champion", ["Los Angeles"], selected=[0])
        rows = markets.comparison_rows([poly, kalshi], selected_only=True)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(sum(o is not None for o in quotes) == 1 for _, quotes in rows))

    def test_team_initials_disambiguate_city_with_two_teams(self):
        poly = self.sports_event("polymarket", "NBA Champion", ["Los Angeles Lakers", "Los Angeles Clippers"])
        kalshi = self.sports_event("kalshi", "Pro Basketball Champion", ["Los Angeles L", "Los Angeles C"])
        rows = markets.comparison_rows([poly, kalshi], selected_only=True)
        self.assertEqual(len(rows), 2)
        for label, quotes in rows:
            self.assertTrue(all(o is not None for o in quotes))
            self.assertEqual(quotes[1]["label"], "Los Angeles L" if label.endswith("Lakers") else "Los Angeles C")

    def test_prefix_matching_does_not_apply_to_player_awards_or_unrelated_markets(self):
        for title in ("NBA MVP", "Election winner"):
            left = self.sports_event("polymarket", title, ["Nikola Jokic"])
            right = self.sports_event("kalshi", title, ["Nikola"])
            self.assertEqual(len(markets.comparison_rows([left, right], selected_only=True)), 2)

    def test_accented_player_names_align_without_collapsing_same_event_contracts(self):
        left = self.sports_event("polymarket", "NBA MVP", ["Nikola Jokic"])
        right = self.sports_event("kalshi", "Pro Basketball MVP", ["Nikola Jokić"])
        rows = markets.comparison_rows([left, right], selected_only=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(o is not None for o in rows[0][1]))
        left = self.sports_event("polymarket", "Election", ["José Garcia", "Jose Garcia"])
        right = self.sports_event("kalshi", "Election", ["Jose Garcia"])
        self.assertEqual(len(markets.comparison_rows([left, right], selected_only=True)), 3)

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
        self.assertIn("47%", self.irc.replies[-1])
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
        self.assertIn("47%", self.irc.replies[-1])
        self.assertIn("Report unavailable", self.irc.replies[-1])
        self.assertEqual(len(list(Path(self.temp.name).glob("markets-*.md"))), 1)

    def test_busy_command_does_not_launch_or_publish(self):
        self.plugin._active_executions = 1
        with mock.patch.object(self.plugin, "_invoke_wrapper") as run:
            self.plugin._handle_markets_request(self.irc, self.msg, "nomination")
        run.assert_not_called()
        self.assertIn("busy", self.irc.replies[-1])
