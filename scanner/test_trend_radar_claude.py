"""Tests for the Claude-powered editorial radar spike. Every Anthropic
call is mocked -- no live network/API access happens in this file. Uses
the same tempdir + explicit db path convention as test_trend_radar.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCANNER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCANNER_DIR))

from trend_radar import db as trdb
from trend_radar.claude_client import MissingApiKeyError, UsageTotals, resolve_model
from trend_radar.claude_discovery import DiscoveryValidationError, validate_discovery_output
from trend_radar.claude_editorial import EditorialValidationError, validate_final_output
from trend_radar.claude_evidence import build_evidence_packet
from trend_radar.claude_expansion import MAX_TOTAL_SEARCHES, run_expansion
from trend_radar.claude_schemas import (
    Candidate,
    ClaudeScanOutput,
    DiscoveryOutput,
    DiscoveryWave,
    EvidenceItem,
    ExpansionQueryProposal,
)
from trend_radar.models import RawSignalInput
from trend_radar.signals import upsert_signal, utc_now_iso


class ClaudeTestCase(unittest.TestCase):
    def connect(self, path: Path):
        conn = trdb.connect(path)
        self.addCleanup(conn.close)
        return conn

    def seed_source_and_run(self, conn, source_id="publisher:fixture", lane="publisher", platform="rss"):
        conn.execute(
            """INSERT OR IGNORE INTO sources (
                source_id, name, lane, platform, canonical_identifier, collection_method,
                source_role, active, access_status, config_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (source_id, "Fixture Publisher", lane, platform, "https://example.com/feed", "rss_atom", "signal", 1, "ok", "{}"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO collection_runs (run_id, started_at, lanes, status) VALUES (?,?,?,?)",
            ("run1", utc_now_iso(), lane, "completed"),
        )
        conn.commit()

    def add_signal(self, conn, **overrides):
        defaults = dict(
            source_id="publisher:fixture", lane="publisher", platform="rss", source_name="Fixture Publisher",
            source_role="signal", url="https://example.com/a", title="A fitness story",
            body_text="Some body text about a workout.", author_or_account="Fixture Publisher",
            published_at=utc_now_iso(), timestamp_precision="exact",
        )
        defaults.update(overrides)
        signal = RawSignalInput(**defaults)
        signal_id, _ = upsert_signal(conn, signal, run_id="run1")
        conn.commit()
        return signal_id


# --- 1. Missing API key fails safely -----------------------------------

class MissingApiKeyTests(ClaudeTestCase):
    def test_missing_api_key_raises_clear_error_not_sdk_crash(self):
        from trend_radar.claude_client import build_client

        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(MissingApiKeyError) as ctx:
                build_client()
            self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_claude_scan_cli_reports_missing_key_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            self.add_signal(conn)

            env = dict(os.environ)
            env.pop("ANTHROPIC_API_KEY", None)
            with mock.patch.dict(os.environ, env, clear=True):
                from trend_radar.claude_scan import run_claude_scan

                with self.assertRaises(MissingApiKeyError):
                    run_claude_scan(conn)


# --- 2. No API request at import time -----------------------------------

class ImportSafetyTests(unittest.TestCase):
    def test_importing_claude_modules_makes_no_api_call(self):
        # If any of these imports constructed a real client or made a
        # network call, this would raise/hang without a key or network.
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            import importlib

            for mod in (
                "trend_radar.claude_client",
                "trend_radar.claude_schemas",
                "trend_radar.claude_evidence",
                "trend_radar.claude_prompts",
                "trend_radar.claude_discovery",
                "trend_radar.claude_expansion",
                "trend_radar.claude_editorial",
                "trend_radar.claude_scan",
                "trend_radar.cli",
            ):
                importlib.import_module(mod)  # must not raise, must not touch network

    def test_model_resolution_never_reads_api_key(self):
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("MFO_TREND_MODEL", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_model(), "claude-sonnet-5-5")
            self.assertEqual(resolve_model("claude-opus-5"), "claude-opus-5")
            env2 = dict(env, MFO_TREND_MODEL="claude-haiku-4-5-20251001")
        with mock.patch.dict(os.environ, env2, clear=True):
            self.assertEqual(resolve_model(), "claude-haiku-4-5-20251001")


# --- 3. Evidence packet construction + 4. Exact dedup --------------------

class EvidencePacketTests(ClaudeTestCase):
    def test_packet_contains_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            self.add_signal(conn, title="A story", url="https://example.com/one")
            records = build_evidence_packet(conn, lookback_days=30, max_signals=100)
            self.assertEqual(len(records), 1)
            d = records[0].to_prompt_dict()
            for field in ("signal_id", "lane", "platform", "source", "published_at", "title", "body_excerpt", "url", "discovery_method"):
                self.assertIn(field, d)

    def test_exact_canonical_url_dedup_collapses_repeat_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            # Same canonical_url via two different query strings -- scanner.canonical_url()
            # strips tracking params, so these collapse to one raw_signals row already at
            # upsert time; simulate two distinct raw rows with the same canonical_url directly
            # to prove the evidence packet's own dedup pass (not just upsert_signal's).
            self.add_signal(conn, url="https://example.com/story?utm_source=a", title="Story A")
            conn.execute(
                """UPDATE raw_signals SET canonical_url = 'https://example.com/story'
                   WHERE url = 'https://example.com/story?utm_source=a'"""
            )
            self.add_signal(conn, url="https://example.com/story?utm_source=b", title="Story A repeated")
            conn.execute(
                """UPDATE raw_signals SET canonical_url = 'https://example.com/story'
                   WHERE url = 'https://example.com/story?utm_source=b'"""
            )
            conn.commit()
            records = build_evidence_packet(conn, lookback_days=30, max_signals=100)
            self.assertEqual(len(records), 1, "two rows sharing one canonical_url must collapse to one evidence record")

    def test_max_signals_cap_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            for i in range(10):
                self.add_signal(conn, url=f"https://example.com/{i}", title=f"Story {i}")
            records = build_evidence_packet(conn, lookback_days=30, max_signals=3)
            self.assertEqual(len(records), 3)


# --- 5. Structured discovery parsing + 6. Invented signal IDs rejected --

class DiscoveryValidationTests(unittest.TestCase):
    def test_valid_discovery_output_passes(self):
        output = DiscoveryOutput(
            waves=[
                DiscoveryWave(
                    wave_id="wave-1", working_label="A story", signal_ids=["sig-1", "sig-2"],
                    cross_source_reasoning="Two independent publishers covered it.",
                    expansion_queries=[ExpansionQueryProposal(query="example query", platform="google-news", rationale="confirm spread")],
                )
            ]
        )
        validate_discovery_output(output, known_signal_ids={"sig-1", "sig-2", "sig-3"})  # must not raise

    def test_invented_signal_id_is_rejected(self):
        output = DiscoveryOutput(
            waves=[DiscoveryWave(wave_id="wave-1", working_label="A story", signal_ids=["sig-1", "made-up-id"], cross_source_reasoning="x")]
        )
        with self.assertRaises(DiscoveryValidationError):
            validate_discovery_output(output, known_signal_ids={"sig-1"})

    def test_too_many_waves_rejected(self):
        waves = [DiscoveryWave(wave_id=f"wave-{i}", working_label="x", signal_ids=["sig-1"], cross_source_reasoning="x") for i in range(13)]
        with self.assertRaises(DiscoveryValidationError):
            validate_discovery_output(DiscoveryOutput(waves=waves), known_signal_ids={"sig-1"})

    def test_too_many_expansion_queries_per_wave_rejected(self):
        wave = DiscoveryWave(
            wave_id="wave-1", working_label="x", signal_ids=["sig-1"], cross_source_reasoning="x",
            expansion_queries=[
                ExpansionQueryProposal(query="a", platform="google-news", rationale="r"),
                ExpansionQueryProposal(query="b", platform="youtube", rationale="r"),
                ExpansionQueryProposal(query="c", platform="google-news", rationale="r"),
            ],
        )
        with self.assertRaises(DiscoveryValidationError):
            validate_discovery_output(DiscoveryOutput(waves=[wave]), known_signal_ids={"sig-1"})


# --- 7. Mismatched URLs rejected + 10. Final JSON validation ------------

class EditorialValidationTests(unittest.TestCase):
    def _valid_candidate(self, **overrides):
        defaults = dict(
            rank=1, headline="Something is moving", topic_summary="summary", why_moving_now="because",
            mfo_angle="angle", confidence="medium", evidence_strength="moderate", lanes=["publisher"],
            independent_sources=2,
            evidence=[EvidenceItem(signal_id="sig-1", title="A story", source="Fixture Publisher", lane="publisher", published_at=None, url="https://example.com/a", relevance="core evidence")],
        )
        defaults.update(overrides)
        return Candidate(**defaults)

    def _known(self):
        from trend_radar.claude_evidence import EvidenceRecord

        return {"sig-1": EvidenceRecord(signal_id="sig-1", lane="publisher", platform="rss", source="Fixture Publisher", published_at=None, timestamp_precision=None, title="A story", body_excerpt="", url="https://example.com/a", discovery_method="collect:rss")}

    def test_valid_output_passes(self):
        candidates = [self._valid_candidate(rank=i) for i in (1, 2, 3)]
        output = ClaudeScanOutput(corpus_summary="s", candidates=candidates)
        validate_final_output(output, known_signal_ids={"sig-1"}, evidence_by_id=self._known())

    def test_mismatched_url_rejected(self):
        bad = self._valid_candidate(evidence=[EvidenceItem(signal_id="sig-1", title="A story", source="Fixture Publisher", lane="publisher", url="https://example.com/WRONG", relevance="x")])
        output = ClaudeScanOutput(corpus_summary="s", candidates=[bad, self._valid_candidate(rank=2), self._valid_candidate(rank=3)])
        with self.assertRaises(EditorialValidationError):
            validate_final_output(output, known_signal_ids={"sig-1"}, evidence_by_id=self._known())

    def test_mismatched_source_rejected(self):
        bad = self._valid_candidate(evidence=[EvidenceItem(signal_id="sig-1", title="A story", source="Some Other Outlet", lane="publisher", url="https://example.com/a", relevance="x")])
        output = ClaudeScanOutput(corpus_summary="s", candidates=[bad, self._valid_candidate(rank=2), self._valid_candidate(rank=3)])
        with self.assertRaises(EditorialValidationError):
            validate_final_output(output, known_signal_ids={"sig-1"}, evidence_by_id=self._known())

    def test_mismatched_lane_rejected(self):
        bad = self._valid_candidate(evidence=[EvidenceItem(signal_id="sig-1", title="A story", source="Fixture Publisher", lane="creator", url="https://example.com/a", relevance="x")])
        output = ClaudeScanOutput(corpus_summary="s", candidates=[bad, self._valid_candidate(rank=2), self._valid_candidate(rank=3)])
        with self.assertRaises(EditorialValidationError):
            validate_final_output(output, known_signal_ids={"sig-1"}, evidence_by_id=self._known())

    def test_invented_signal_id_in_final_pass_rejected(self):
        bad = self._valid_candidate(evidence=[EvidenceItem(signal_id="never-given", title="x", source="x", lane="publisher", url=None, relevance="x")])
        output = ClaudeScanOutput(corpus_summary="s", candidates=[bad, self._valid_candidate(rank=2), self._valid_candidate(rank=3)])
        with self.assertRaises(EditorialValidationError):
            validate_final_output(output, known_signal_ids={"sig-1"}, evidence_by_id=self._known())

    def test_candidate_count_out_of_range_rejected(self):
        output = ClaudeScanOutput(corpus_summary="s", candidates=[self._valid_candidate(rank=1)])
        with self.assertRaises(EditorialValidationError):
            validate_final_output(output, known_signal_ids={"sig-1"}, evidence_by_id=self._known())

    def test_duplicate_rank_rejected(self):
        output = ClaudeScanOutput(corpus_summary="s", candidates=[self._valid_candidate(rank=1), self._valid_candidate(rank=1), self._valid_candidate(rank=1)])
        with self.assertRaises(EditorialValidationError):
            validate_final_output(output, known_signal_ids={"sig-1"}, evidence_by_id=self._known())


# --- 8. Search-budget enforcement + 9. Expansion failure isolation ------

class ExpansionTests(ClaudeTestCase):
    def test_search_budget_capped_at_twelve(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            waves = [
                DiscoveryWave(
                    wave_id=f"wave-{i}", working_label="x", signal_ids=["sig-1"], cross_source_reasoning="x",
                    expansion_queries=[
                        ExpansionQueryProposal(query=f"q{i}a", platform="google-news", rationale="r"),
                        ExpansionQueryProposal(query=f"q{i}b", platform="youtube", rationale="r"),
                    ],
                )
                for i in range(10)  # 20 proposed queries total, well over the cap
            ]
            with mock.patch("trend_radar.claude_expansion._google_news_search", return_value=[]), \
                 mock.patch("trend_radar.claude_expansion._youtube_search", return_value=[]):
                _, log = run_expansion(conn, waves, budget=MAX_TOTAL_SEARCHES)
            self.assertEqual(len(log), MAX_TOTAL_SEARCHES)

    def test_one_failing_query_does_not_abort_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            waves = [
                DiscoveryWave(wave_id="wave-1", working_label="x", signal_ids=["sig-1"], cross_source_reasoning="x",
                               expansion_queries=[ExpansionQueryProposal(query="bad", platform="google-news", rationale="r")]),
                DiscoveryWave(wave_id="wave-2", working_label="x", signal_ids=["sig-1"], cross_source_reasoning="x",
                               expansion_queries=[ExpansionQueryProposal(query="good", platform="youtube", rationale="r")]),
            ]

            def failing(_query):
                raise RuntimeError("network exploded")

            with mock.patch("trend_radar.claude_expansion._google_news_search", side_effect=failing), \
                 mock.patch("trend_radar.claude_expansion._youtube_search", return_value=[]):
                results, log = run_expansion(conn, waves, budget=MAX_TOTAL_SEARCHES)

            statuses = {entry["wave_id"]: entry["status"] for entry in log}
            self.assertEqual(statuses["wave-1"], "failed")
            self.assertEqual(statuses["wave-2"], "success")

    def test_successful_expansion_upserts_signals_without_fk_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            waves = [
                DiscoveryWave(wave_id="wave-1", working_label="x", signal_ids=["sig-1"], cross_source_reasoning="x",
                               expansion_queries=[ExpansionQueryProposal(query="q", platform="google-news", rationale="r")])
            ]
            fake_item = mock.Mock(url="https://example.com/found", title="Found story", summary="body", source="Some Outlet", published="2026-08-17T00:00:00Z")
            with mock.patch("trend_radar.claude_expansion.scanner") as fake_scanner:
                fake_scanner.http_get_text.return_value = "<rss></rss>"
                fake_scanner.rss_items_from_xml.return_value = [fake_item]
                results, log = run_expansion(conn, waves, budget=MAX_TOTAL_SEARCHES)

            self.assertEqual(log[0]["status"], "success")
            self.assertEqual(log[0]["new_signal_count"], 1)
            self.assertIn("wave-1", results)
            self.assertEqual(len(results["wave-1"]), 1)
            row_count = conn.execute("SELECT COUNT(*) FROM raw_signals WHERE url = 'https://example.com/found'").fetchone()[0]
            self.assertEqual(row_count, 1)


# --- 11. Usage recording --------------------------------------------------

class UsageTests(unittest.TestCase):
    def test_usage_accumulates_across_calls(self):
        usage = UsageTotals(model="claude-sonnet-5-5")
        fake_usage = mock.Mock(input_tokens=100, output_tokens=50)
        usage.record(fake_usage, elapsed=1.5)
        usage.record(fake_usage, elapsed=2.5)
        d = usage.as_dict()
        self.assertEqual(d["api_calls"], 2)
        self.assertEqual(d["input_tokens"], 200)
        self.assertEqual(d["output_tokens"], 100)
        self.assertAlmostEqual(d["elapsed_seconds"], 4.0)
        self.assertEqual(d["model"], "claude-sonnet-5-5")

    def test_usage_dict_never_contains_api_key_field(self):
        usage = UsageTotals(model="claude-sonnet-5-5")
        d = usage.as_dict()
        self.assertNotIn("api_key", d)
        self.assertNotIn("ANTHROPIC_API_KEY", str(d))


# --- 12. No acceptance-target terms in production prompts ----------------

class NoForbiddenTermsTests(unittest.TestCase):
    FORBIDDEN = ("Tom Holland", "tom holland", "Cindy", "cindy", "Spider-Man", "spider-man", "spider man", "27 rounds")

    def test_prompts_contain_no_forbidden_terms(self):
        from trend_radar import claude_prompts

        source = Path(claude_prompts.__file__).read_text(encoding="utf-8")
        for term in self.FORBIDDEN:
            self.assertNotIn(term, source, f"forbidden term {term!r} found in claude_prompts.py")

    def test_schema_field_descriptions_contain_no_forbidden_terms(self):
        from trend_radar import claude_schemas

        source = Path(claude_schemas.__file__).read_text(encoding="utf-8")
        for term in self.FORBIDDEN:
            self.assertNotIn(term, source, f"forbidden term {term!r} found in claude_schemas.py")


# --- End-to-end orchestration with a fully mocked client -----------------

class EndToEndMockedScanTests(ClaudeTestCase):
    def _fake_parsed_response(self, parsed_output, input_tokens=10, output_tokens=10, stop_reason="end_turn", text=None):
        """A streamed final message: JSON text block + usage + stop_reason."""
        response = mock.Mock()
        block = mock.Mock(type="text")
        block.text = text if text is not None else parsed_output.model_dump_json()
        response.content = [mock.Mock(type="thinking"), block]
        response.stop_reason = stop_reason
        response.usage = mock.Mock(input_tokens=input_tokens, output_tokens=output_tokens,
                                   cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return response

    def _client_returning(self, *responses):
        fake_client = mock.Mock()
        stream_cm = mock.MagicMock()
        stream_cm.__enter__.return_value.get_final_message.side_effect = list(responses)
        fake_client.messages.stream.return_value = stream_cm
        return fake_client

    def _run(self, conn, fake_client, **kwargs):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             mock.patch("trend_radar.claude_scan.build_client", return_value=fake_client):
            from trend_radar.claude_scan import run_claude_scan

            return run_claude_scan(conn, skip_expansion=True, **kwargs)

    def _cand(self, rank, ids):
        return Candidate(rank=rank, headline=f"h{rank}", topic_summary="s", why_moving_now="w", mfo_angle="a",
                         confidence="low", evidence_strength="weak", lanes=["publisher"], independent_sources=1,
                         evidence=[EvidenceItem(signal_id=i, relevance="core") for i in ids])

    def test_short_refs_are_sent_and_mapped_back_with_repairs_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            sig1 = self.add_signal(conn, url="https://example.com/one", title="Story one", author_or_account="Fixture Publisher")
            discovery = DiscoveryOutput(waves=[
                DiscoveryWave(wave_id="wave-1", working_label="A", signal_ids=["S001", "S999"], cross_source_reasoning="x"),
                DiscoveryWave(wave_id="wave-2", working_label="B", signal_ids=["S777"], cross_source_reasoning="x"),
            ])
            editorial = ClaudeScanOutput(corpus_summary="c", candidates=[
                self._cand(1, ["S001"]), self._cand(2, ["S001", "S555"]), self._cand(3, ["S001"]), self._cand(4, ["S404"]),
            ])
            client = self._client_returning(self._fake_parsed_response(discovery), self._fake_parsed_response(editorial))
            result = self._run(conn, client)

            sent = client.messages.stream.call_args_list[0].kwargs["messages"][0]["content"]
            self.assertIn('"signal_id": "S001"', sent)
            self.assertNotIn(sig1, sent)
            self.assertNotIn("https://example.com/one", sent)

            self.assertEqual([c["rank"] for c in result["candidates"]], [1, 2, 3])
            first = result["candidates"][0]["evidence"][0]
            self.assertEqual(first["signal_id"], sig1)
            self.assertEqual(first["url"], "https://example.com/one")
            self.assertEqual(first["source"], "Fixture Publisher")
            self.assertEqual([w["wave_id"] for w in result["_discovery_waves"]], ["wave-1"])
            joined = " ".join(result["_validation_repairs"])
            for marker in ("S999", "wave-2", "S555", "S404"):
                self.assertIn(marker, joined)

    def test_editor_feedback_reaches_both_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            self.add_signal(conn, url="https://example.com/one", title="Story one")
            discovery = DiscoveryOutput(waves=[
                DiscoveryWave(wave_id="wave-1", working_label="A", signal_ids=["S001"], cross_source_reasoning="x"),
            ])
            editorial = ClaudeScanOutput(corpus_summary="c", candidates=[self._cand(r, ["S001"]) for r in (1, 2, 3)])
            client = self._client_returning(self._fake_parsed_response(discovery), self._fake_parsed_response(editorial))
            result = self._run(conn, client, editor_feedback="EDITOR FEEDBACK — spanish squat published")
            calls = client.messages.stream.call_args_list
            for call in calls:
                self.assertIn("EDITOR FEEDBACK", call.kwargs["messages"][0]["content"])
            self.assertTrue(result["_editor_feedback_used"])

    def test_truncated_response_reports_stop_reason(self):
        from trend_radar.claude_client import ClaudeOutputError

        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            self.add_signal(conn, url="https://example.com/one", title="Story one")
            dummy = DiscoveryOutput(waves=[])
            client = self._client_returning(self._fake_parsed_response(dummy, stop_reason="max_tokens", text='{"waves":[{"wave'))
            with self.assertRaisesRegex(ClaudeOutputError, "truncated"):
                self._run(conn, client)

    def test_spend_cap_blocks_call_before_it_is_made(self):
        from trend_radar.claude_client import SpendCapReached, SpendLedger

        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            self.add_signal(conn, url="https://example.com/one", title="Story one")
            ledger = SpendLedger(Path(tmp) / "spend.json", daily_cap_usd=0.01)
            client = self._client_returning()
            with self.assertRaises(SpendCapReached):
                self._run(conn, client, ledger=ledger)
            client.messages.stream.assert_not_called()

    def test_ledger_records_actual_cost(self):
        from trend_radar.claude_client import SpendLedger

        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            self.add_signal(conn, url="https://example.com/one", title="Story one")
            discovery = DiscoveryOutput(waves=[DiscoveryWave(wave_id="w", working_label="A", signal_ids=["S001"], cross_source_reasoning="x")])
            editorial = ClaudeScanOutput(corpus_summary="c", candidates=[self._cand(i, ["S001"]) for i in (1, 2, 3)])
            ledger = SpendLedger(Path(tmp) / "spend.json", daily_cap_usd=50)
            client = self._client_returning(
                self._fake_parsed_response(discovery, input_tokens=1_000_000, output_tokens=0),
                self._fake_parsed_response(editorial, input_tokens=0, output_tokens=100_000),
            )
            result = self._run(conn, client, ledger=ledger)
            self.assertAlmostEqual(ledger.spent_today(), 3.0, places=4)  # $2 in + $1 out
            self.assertAlmostEqual(result["usage"]["cost_usd"], 3.0, places=4)

    def test_editorial_retries_once_when_too_few_candidates_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            self.add_signal(conn, url="https://example.com/one", title="Story one")
            discovery = DiscoveryOutput(waves=[DiscoveryWave(wave_id="w", working_label="A", signal_ids=["S001"], cross_source_reasoning="x")])
            thin = ClaudeScanOutput(corpus_summary="c", candidates=[self._cand(1, ["S001"])])
            good = ClaudeScanOutput(corpus_summary="c", candidates=[self._cand(i, ["S001"]) for i in (1, 2, 3)])
            client = self._client_returning(*(self._fake_parsed_response(o) for o in (discovery, thin, good)))
            result = self._run(conn, client)
            self.assertEqual(len(result["candidates"]), 3)
            self.assertEqual(result["usage"]["api_calls"], 3)
            retry_prompt = client.messages.stream.call_args_list[2].kwargs["messages"][0]["content"]
            self.assertIn("could not be used", retry_prompt)

    def test_full_two_pass_run_with_mocked_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.connect(Path(tmp) / "t.db")
            self.seed_source_and_run(conn)
            sig1 = self.add_signal(conn, url="https://example.com/one", title="Story one", author_or_account="Fixture Publisher")

            discovery_output = DiscoveryOutput(
                waves=[DiscoveryWave(wave_id="wave-1", working_label="A story", signal_ids=[sig1], cross_source_reasoning="one source only")]
            )
            editorial_output = ClaudeScanOutput(
                corpus_summary="one story found",
                candidates=[
                    Candidate(
                        rank=1, headline="A story is moving", topic_summary="summary", why_moving_now="fresh",
                        mfo_angle="angle", confidence="low", evidence_strength="weak", lanes=["publisher"],
                        independent_sources=1,
                        evidence=[EvidenceItem(signal_id=sig1, title="Story one", source="Fixture Publisher", lane="publisher", url="https://example.com/one", relevance="core")],
                    ),
                    Candidate(rank=2, headline="h2", topic_summary="s", why_moving_now="w", mfo_angle="a", confidence="low", evidence_strength="weak", lanes=["publisher"], independent_sources=1, evidence=[EvidenceItem(signal_id=sig1, title="Story one", source="Fixture Publisher", lane="publisher", url="https://example.com/one", relevance="core")]),
                    Candidate(rank=3, headline="h3", topic_summary="s", why_moving_now="w", mfo_angle="a", confidence="low", evidence_strength="weak", lanes=["publisher"], independent_sources=1, evidence=[EvidenceItem(signal_id=sig1, title="Story one", source="Fixture Publisher", lane="publisher", url="https://example.com/one", relevance="core")]),
                ],
            )

            fake_client = self._client_returning(
                self._fake_parsed_response(discovery_output),
                self._fake_parsed_response(editorial_output),
            )

            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
                 mock.patch("trend_radar.claude_scan.build_client", return_value=fake_client):
                from trend_radar.claude_scan import run_claude_scan

                result = run_claude_scan(conn, skip_expansion=True)

            self.assertEqual(len(result["candidates"]), 3)
            self.assertEqual(result["usage"]["api_calls"], 2)
            self.assertEqual(result["usage"]["input_tokens"], 20)
            self.assertEqual(result["model"], "claude-sonnet-5-5")
            self.assertIn("generated_at", result)


if __name__ == "__main__":
    unittest.main()
