import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import digest
from trend_radar import sent_log
from trend_radar.claude_client import UsageTotals, call_web_research
from trend_radar.claude_fit_food import repair_picks
from trend_radar.claude_schemas import FitFoodOutput

NOW = datetime(2026, 11, 13, 7, 0, tzinfo=timezone.utc)
B6 = sent_log.item("fit_food", "TGA wants a nerve-damage warning on vitamin B6 supplements",
                   "https://www.nutraingredients.com/Article/2026/11/09/vitamin-b6/")


def conn_with(*items, when=NOW):
    conn = sqlite3.connect(":memory:")
    sent_log.record(conn, list(items), now=when)
    return conn


def test_same_story_new_headline_or_shared_source_is_a_repeat():
    sent = sent_log.recent(conn_with(B6), now=NOW)
    again = sent_log.item("fit_food", "TGA wants a nerve-damage warning on more vitamin B6 supplements")
    assert sent_log.is_repeat(again, sent)
    same_source = sent_log.item("news", "Different words entirely", B6["urls"][0] + "#x")
    assert sent_log.is_repeat(same_source, sent)
    other = sent_log.item("fit_food", "Creatine gummies fail lab test", "https://example.com/gummies")
    assert not sent_log.is_repeat(other, sent)


def test_mfo_page_only_identifies_refreshes_and_rising():
    page = "https://mensfitnessonline.com.au/glp-1-muscle-loss/"
    sent = sent_log.recent(conn_with(sent_log.item("fit_food", "Mounjaro and muscle", "https://nature.com/a", page)), now=NOW)
    # a different story that also suggests updating the same MFO post is not a repeat
    assert not sent_log.is_repeat(sent_log.item("news", "Semaglutide pill trial results", page), sent)
    sent = sent_log.recent(conn_with(sent_log.item("rising", "abdul el sayed bench", page)), now=NOW)
    assert sent_log.is_repeat(sent_log.item("rising", "abdul el sayed weight", page), sent)


def test_old_items_expire():
    conn = conn_with(B6, when=NOW - timedelta(days=8))
    assert sent_log.recent(conn, now=NOW) == []
    assert sent_log.prompt_block(conn, now=NOW) is None


def test_prompt_block_lists_stories_not_refreshes():
    conn = conn_with(B6, sent_log.item("refresh", "Matteo Sartori", "https://mensfitnessonline.com.au/x/"))
    block = sent_log.prompt_block(conn, now=NOW)
    assert "ALREADY SENT TO THE EDITOR" in block and "vitamin B6" in block and "Sartori" not in block


def test_digest_leaves_out_what_was_sent():
    scan = {"_scan_id": 30, "candidates": [
        {"rank": 1, "headline": "TGA wants a nerve-damage warning on vitamin B6", "evidence": [{"url": B6["urls"][0]}]},
        {"rank": 2, "headline": "Fresh story", "evidence": [{"url": "https://example.com/fresh"}]},
    ]}
    msgs = digest.build_messages(scan, {}, None, now=datetime(2026, 11, 13, 7, 5), sent=[B6])
    titles = [m["title"] for m in msgs]
    assert titles == ["Morning 1/1: Fresh story"]
    assert msgs[0]["_item"]["kind"] == "news"


def test_rising_groups_queries_by_page_and_skips_sent_pages():
    page_a, page_b = "https://mensfitnessonline.com.au/a/", "https://mensfitnessonline.com.au/b/"
    rows = [{"query": "a one", "page": page_a, "impressions_7d": 300, "position": 5.0},
            {"query": "a two", "page": page_a, "impressions_7d": 200, "position": 6.0},
            {"query": "b one", "page": page_b, "impressions_7d": 400, "position": 3.0}]
    msgs = digest.rising_messages({"rows": rows}, [])
    assert [m["click"] for m in msgs] == [page_a, page_b]  # 500 beats 400
    assert "'a one' #5, 'a two' #6" in msgs[0]["message"]
    msgs = digest.rising_messages({"rows": rows}, [sent_log.item("rising", "x", page_a)])
    assert [m["click"] for m in msgs] == [page_b]


def _pick(**kw):
    base = dict(headline="Mounjaro and muscle", why_care="x", first_published="2026-11-10",
                original_source_url="https://www.nature.com/articles/s41591", original_source_opened=True,
                other_outlet_url=None, facts="f", search_term="s", demand_note="d", format="add to existing post",
                update_url="https://mensfitnessonline.com.au/lift-or-lose-it/", fit_score=8, risk="r")
    base.update(kw)
    return base


def test_add_to_a_post_that_already_covered_the_story_is_dropped():
    out = FitFoodOutput(summary="s", picks=[_pick(), _pick(headline="Later news", first_published="2026-11-12")],
                        left_out=[])
    recent = [{"date": "2026-11-10", "title": "Lift or lose it", "url": "https://mensfitnessonline.com.au/lift-or-lose-it/"}]
    notes = repair_picks(out, {"https://www.nature.com/articles/s41591"}, {recent[0]["url"]},
                         fetched={"https://www.nature.com/articles/s41591"}, recent=recent)
    assert [p.headline for p in out.picks] == ["Later news"]
    assert any("MFO covered it on 2026-11-10" in n for n in notes)


def test_opened_needs_a_real_fetch():
    out = FitFoodOutput(summary="s", picks=[_pick(format="news post", update_url=None)], left_out=[])
    repair_picks(out, {"https://www.nature.com/articles/s41591"}, set(), fetched=set())
    assert out.picks[0].original_source_opened is False


class _Stream:
    def __init__(self, final):
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.final


def test_web_research_offers_fetch_and_reports_opened_pages():
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, server_tool_use=None)
    final = SimpleNamespace(stop_reason="end_turn", usage=usage, content=[
        SimpleNamespace(type="web_search_tool_result", content=[SimpleNamespace(url="https://a.org/p", title="P", page_age=None)]),
        SimpleNamespace(type="web_fetch_tool_result", content=SimpleNamespace(type="web_fetch_result", url="https://a.org/p")),
        SimpleNamespace(type="text", text="notes"),
    ])
    seen = {}

    def stream(**kwargs):
        seen.update(kwargs)
        return _Stream(final)

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    text, sources = call_web_research(client, model="claude-sonnet-5-5", system="s", user_content="u",
                                      max_tokens=100, max_searches=3, usage=UsageTotals(model="claude-sonnet-5-5"),
                                      max_fetches=4)
    assert {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4,
            "max_content_tokens": 12_000} in seen["tools"]
    assert text == "notes" and sources == [{"url": "https://a.org/p", "title": "P", "page_age": None, "fetched": True}]
