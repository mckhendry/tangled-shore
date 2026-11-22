"""Spider: the floor, dedupe, the ledger, the window, the slot rules, verify, render, the judge's
argv and output check, the run's flow, and install.sh.

Offline: every fetch, the claude -p session, Slack and the Keychain are fakes, the state folder is a temp
folder, and install.sh runs against a temp HOME with stubbed launchctl, plutil and security.
Run:  python3 -m unittest discover -s plugins/spider/tests -p 'test_*.py' -t plugins/spider/tests
"""
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

SPIDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SPIDER)

import spider  # noqa: E402
import spider_floor as floor  # noqa: E402
import spider_judge as judge  # noqa: E402
import spider_post as post  # noqa: E402
import spider_state as state  # noqa: E402
import spider_verify as verify  # noqa: E402


def wall(y, mo, d, h, mi=0):
    """Epoch seconds for a wall-clock time in config.json's zone."""
    return datetime(y, mo, d, h, mi, tzinfo=state.TZ).timestamp()


# Wed Nov 4 2026 and the days around it.
WED_AM = wall(2026, 11, 4, 7, 30)
WED_PM = wall(2026, 11, 4, 13, 30)


def item(url="https://example.com/a", **kw):
    base = {"topic": "models", "headline": "Model X ships", "why": "It is cheaper.", "status": "shipped",
            "url": url, "discussion_url": "", "event_key": "model-x-ships", "moved": False}
    base.update(kw)
    return base


class NormalizeUrlTests(unittest.TestCase):
    def test_strips_tracking_params_fragment_and_trailing_slash(self):
        self.assertEqual(
            floor.normalize_url("HTTPS://Example.COM/news/post/?utm_source=x&id=3&fbclid=abc&utm_medium=y#top"),
            "https://example.com/news/post?id=3")

    def test_keeps_a_bare_host_and_other_params(self):
        self.assertEqual(floor.normalize_url("https://example.com/"), "https://example.com")
        self.assertEqual(floor.normalize_url("https://example.com/item?id=1&b=2"), "https://example.com/item?id=1&b=2")

    def test_two_spellings_of_one_page_are_one_url(self):
        self.assertEqual(floor.normalize_url("https://a.com/x/#frag"), floor.normalize_url("https://a.com/x?utm_campaign=z"))


class ParseFeedTests(unittest.TestCase):
    RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>New model</title><link>https://openai.com/index/new-model/?utm_source=rss</link>
        <pubDate>Wed, 04 Nov 2026 16:00:00 GMT</pubDate></item>
      <item><title>Old post</title><link>https://openai.com/index/old/</link>
        <pubDate>Mon, 02 Nov 2026 16:00:00 GMT</pubDate></item>
    </channel></rss>"""
    ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <entry><title>Video one</title><link rel="alternate" href="https://www.youtube.com/watch?v=abc"/>
        <published>2026-11-04T15:00:00+00:00</published></entry>
    </feed>"""

    def test_rss_items_carry_title_normalized_link_and_date(self):
        got = floor.parse_feed(self.RSS)
        self.assertEqual([g["title"] for g in got], ["New model", "Old post"])
        self.assertEqual(got[0]["url"], "https://openai.com/index/new-model")
        self.assertEqual(got[0]["published"], datetime(2026, 11, 4, 16, 0, tzinfo=state.UTC).timestamp())

    def test_atom_entries_parse(self):
        got = floor.parse_feed(self.ATOM)
        self.assertEqual(got[0]["url"], "https://www.youtube.com/watch?v=abc")
        self.assertEqual(got[0]["published"], datetime(2026, 11, 4, 15, 0, tzinfo=state.UTC).timestamp())


class HttpGetTests(unittest.TestCase):
    """deepmind.google answers gzip even unasked, which urllib does not decode."""

    def response(self, body, encoding):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = body
        resp.headers = {"Content-Encoding": encoding} if encoding else {}
        return resp

    def test_a_gzip_body_is_decoded(self):
        import gzip
        with mock.patch.object(floor.urllib.request, "urlopen", return_value=self.response(gzip.compress(b"<rss/>"), "gzip")):
            self.assertEqual(floor.http_get("https://x"), b"<rss/>")

    def test_a_plain_body_is_returned_as_is(self):
        with mock.patch.object(floor.urllib.request, "urlopen", return_value=self.response(b"<rss/>", None)):
            self.assertEqual(floor.http_get("https://x"), b"<rss/>")


class FloorTests(unittest.TestCase):
    """The floor's gates, its per-source status, and its dedupe against itself and the ledger."""

    START = wall(2026, 11, 3, 13, 30)

    def fetcher(self, table):
        def fetch(url, timeout=None):
            for key, value in table.items():
                if key in url:
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise OSError("no fake for %s" % url)
        return fetch

    def sources(self):
        return {"sources": [
            {"name": "openai", "kind": "rss", "url": "https://openai.com/news/rss.xml"},
            {"name": "claude-code", "kind": "github_releases", "repo": "anthropics/claude-code"},
            {"name": "hn", "kind": "hn", "min_points": 100},
            {"name": "hf", "kind": "hf_daily_papers", "min_upvotes": 20},
        ]}

    def table(self):
        return {
            "openai.com/news/rss.xml": ParseFeedTests.RSS,
            "api.github.com/repos/anthropics/claude-code/releases": json.dumps([
                {"name": "v2.2.0", "tag_name": "v2.2.0", "html_url": "https://github.com/anthropics/claude-code/releases/tag/v2.2.0",
                 "published_at": "2026-11-04T01:00:00Z", "body": "Adds a thing"},
                {"name": "v2.1.0", "tag_name": "v2.1.0", "html_url": "https://github.com/anthropics/claude-code/releases/tag/v2.1.0",
                 "published_at": "2026-10-01T01:00:00Z", "body": "old"},
                {"name": "v2.3.0-draft", "tag_name": "v2.3.0", "draft": True,
                 "html_url": "https://github.com/anthropics/claude-code/releases/tag/v2.3.0",
                 "published_at": "2026-11-04T02:00:00Z", "body": "draft"}]).encode(),
            "hn.algolia.com": json.dumps({"hits": [
                {"title": "New model is out", "url": "https://openai.com/index/new-model/", "points": 400,
                 "objectID": "1", "created_at_i": int(WED_AM), "num_comments": 200},
                {"title": "Ask HN", "url": None, "points": 150, "objectID": "2", "created_at_i": int(WED_AM),
                 "num_comments": 10},
                {"title": "Under the bar", "url": "https://low.example/x", "points": 40, "objectID": "3",
                 "created_at_i": int(WED_AM), "num_comments": 2}]}).encode(),
            "huggingface.co/api/daily_papers": json.dumps([
                {"paper": {"id": "2611.1", "title": "Big paper", "upvotes": 80,
                           "submittedOnDailyAt": "2026-11-04T00:00:00.000Z"}},
                {"paper": {"id": "2611.2", "title": "Small paper", "upvotes": 3,
                           "submittedOnDailyAt": "2026-11-04T00:00:00.000Z"}}]).encode(),
        }

    def test_gates_and_window_keep_only_recent_items_over_the_bar(self):
        cands, statuses = floor.run_floor(self.sources(), {"creators": []}, self.START, set(), fetch=self.fetcher(self.table()))
        titles = sorted(c["title"] for c in cands)
        self.assertEqual(titles, ["Ask HN", "Big paper", "New model", "anthropics/claude-code v2.2.0"])
        self.assertEqual({s["name"]: s["status"] for s in statuses},
                         {"openai": "ok", "claude-code": "ok", "hn": "ok", "hf": "ok"})

    def test_one_url_from_two_sources_is_one_candidate_with_both_sources_and_the_discussion_link(self):
        cands, _ = floor.run_floor(self.sources(), {"creators": []}, self.START, set(), fetch=self.fetcher(self.table()))
        model = [c for c in cands if c["url"] == "https://openai.com/index/new-model"]
        self.assertEqual(len(model), 1)
        self.assertEqual(sorted(model[0]["sources"]), ["hn", "openai"])
        self.assertEqual(model[0]["discussion_url"], "https://news.ycombinator.com/item?id=1")

    def test_an_hn_story_with_no_link_points_at_its_discussion(self):
        cands, _ = floor.run_floor(self.sources(), {"creators": []}, self.START, set(), fetch=self.fetcher(self.table()))
        ask = [c for c in cands if c["title"] == "Ask HN"][0]
        self.assertEqual(ask["url"], "https://news.ycombinator.com/item?id=2")

    def test_a_url_already_in_the_ledger_never_reaches_the_session(self):
        cands, _ = floor.run_floor(self.sources(), {"creators": []}, self.START,
                                   {"https://openai.com/index/new-model"}, fetch=self.fetcher(self.table()))
        self.assertNotIn("https://openai.com/index/new-model", [c["url"] for c in cands])

    def test_a_failed_source_is_recorded_and_the_rest_continue(self):
        table = self.table()
        table["openai.com/news/rss.xml"] = OSError("boom")
        cands, statuses = floor.run_floor(self.sources(), {"creators": []}, self.START, set(), fetch=self.fetcher(table))
        self.assertEqual({s["name"]: s["status"] for s in statuses}["openai"], "failed")
        self.assertTrue(cands)
        self.assertFalse(floor.all_failed(statuses))

    def test_a_creator_missing_channel_id_fails_alone_by_name_or_index(self):
        creators = {"creators": [{"name": "no-id"}, {}]}
        cands, statuses = floor.run_floor(self.sources(), creators, self.START, set(), fetch=self.fetcher(self.table()))
        by_name = {s["name"]: s["status"] for s in statuses}
        self.assertEqual(by_name["youtube:no-id"], "failed")
        self.assertEqual(by_name["youtube:#1"], "failed")
        self.assertEqual(by_name["openai"], "ok")
        self.assertTrue(cands)

    def test_a_source_with_nothing_in_the_window_is_empty_not_failed(self):
        cands, statuses = floor.run_floor(self.sources(), {"creators": []}, wall(2026, 11, 5, 0), set(),
                                          fetch=self.fetcher(self.table()))
        self.assertEqual({s["name"]: s["status"] for s in statuses}["openai"], "empty")

    def test_hn_asks_for_stories_created_up_to_two_days_before_the_window(self):
        # A story created before the last post can cross the points bar after it.
        seen = []

        def fetch(url, timeout=None):
            seen.append(url)
            return json.dumps({"hits": []}).encode()
        floor.run_floor({"sources": [{"name": "hn", "kind": "hn", "min_points": 150}]}, {"creators": []},
                        self.START, set(), fetch=fetch)
        query = floor.urllib.parse.parse_qs(floor.urllib.parse.urlsplit(seen[0]).query)
        self.assertEqual(query["numericFilters"],
                         ["created_at_i>%d,points>=150" % int(self.START - floor.GATED_LOOKBACK_S)])

    def test_a_feed_item_dated_a_little_before_the_window_is_still_a_candidate(self):
        rss = b"""<rss><channel><item><title>Late</title><link>https://openai.com/index/late</link>
          <pubDate>%s</pubDate></item></channel></rss>"""
        dated = datetime.fromtimestamp(self.START - 3600, state.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT").encode()
        cands, _ = floor.run_floor({"sources": [{"name": "openai", "kind": "rss", "url": "https://openai.com/rss"}]},
                                   {"creators": []}, self.START, set(), fetch=lambda u, timeout=None: rss % dated)
        self.assertEqual([c["title"] for c in cands], ["Late"])
        self.assertTrue(cands[0]["from_feed"])

    def test_an_unknown_source_kind_fails_only_that_source(self):
        sources = self.sources()
        sources["sources"].append({"name": "typo", "kind": "rsss", "url": "https://x"})
        cands, statuses = floor.run_floor(sources, {"creators": []}, self.START, set(), fetch=self.fetcher(self.table()))
        by_name = {s["name"]: s for s in statuses}
        self.assertEqual(by_name["typo"]["status"], "failed")
        self.assertIn("unknown source kind", by_name["typo"]["error"])
        self.assertTrue(cands)

    def test_sources_left_when_the_floor_budget_runs_out_are_failed_not_fetched(self):
        ticks = iter([0, 0, floor.FLOOR_BUDGET_S + 1, floor.FLOOR_BUDGET_S + 2, floor.FLOOR_BUDGET_S + 3])
        cands, statuses = floor.run_floor(self.sources(), {"creators": []}, self.START, set(),
                                          fetch=self.fetcher(self.table()), clock=lambda: next(ticks))
        self.assertEqual([s["status"] for s in statuses], ["ok", "failed", "failed", "failed"])
        self.assertIn("budget", statuses[1]["error"])

    def test_a_link_hn_reaches_first_is_still_vouched_by_its_publisher_that_lists_it_second(self):
        sources = self.sources()
        sources["sources"] = [sources["sources"][2], sources["sources"][0]]  # hn, then openai
        cands, _ = floor.run_floor(sources, {"creators": []}, self.START, set(), fetch=self.fetcher(self.table()))
        self.assertIn("https://openai.com/index/new-model", floor.feed_urls(cands))

    def test_one_malformed_link_skips_its_entry_not_the_source(self):
        rss = b"""<rss><channel>
          <item><title>Bad</title><link>https://[x</link><pubDate>Wed, 04 Nov 2026 16:00:00 GMT</pubDate></item>
          <item><title>Good</title><link>https://openai.com/index/g</link><pubDate>Wed, 04 Nov 2026 16:00:00 GMT</pubDate></item>
        </channel></rss>"""
        cands, statuses = floor.run_floor({"sources": [{"name": "o", "kind": "rss", "url": "https://openai.com/rss"}]},
                                          {"creators": []}, self.START, set(), fetch=lambda u, timeout=None: rss)
        self.assertEqual([c["title"] for c in cands], ["Good"])
        self.assertEqual(statuses[0]["status"], "ok")

    def test_a_feed_that_declares_an_entity_is_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel></channel></rss>'
        with self.assertRaises(ValueError):
            floor.parse_feed(bomb)

    def test_an_entity_declared_after_a_long_prolog_is_still_refused(self):
        # XML allows comments before the DOCTYPE, so a guard on only the first bytes can be padded past.
        padded = (b'<?xml version="1.0"?><!--' + b"x" * 70000 + b'--><!DOCTYPE r [<!ENTITY a "aaaa">]>'
                  b"<rss><channel></channel></rss>")
        with self.assertRaises(ValueError):
            floor.parse_feed(padded)

    def test_a_utf16_or_utf32_feed_is_refused_since_a_byte_scan_cant_read_it(self):
        body = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss/>'
        for encoded in (body.encode("utf-16"), body.encode("utf-16-be"), body.encode("utf-32"),
                        body.encode("utf-32-le")):
            with self.assertRaises(ValueError):
                floor.parse_feed(encoded)

    def test_an_ordinary_utf8_feed_still_parses(self):
        self.assertEqual(len(floor.parse_feed(ParseFeedTests.RSS)), 2)

    def test_hn_links_are_not_feed_listed_but_a_lab_feed_link_is(self):
        cands, _ = floor.run_floor(self.sources(), {"creators": []}, self.START, set(), fetch=self.fetcher(self.table()))
        listed = floor.feed_urls(cands)
        self.assertIn("https://openai.com/index/new-model", listed)  # in OpenAI's feed and on HN
        self.assertNotIn("https://news.ycombinator.com/item?id=2", listed)

    def test_a_feed_vouches_only_for_links_on_its_own_publishers_host(self):
        rss = b"""<rss><channel>
          <item><title>Own</title><link>https://www.deepmind.google/blog/own</link><pubDate>Wed, 04 Nov 2026 16:00:00 GMT</pubDate></item>
          <item><title>Elsewhere</title><link>https://evil.example/post</link><pubDate>Wed, 04 Nov 2026 16:00:00 GMT</pubDate></item>
        </channel></rss>"""
        sources = {"sources": [{"name": "dm", "kind": "rss", "url": "https://deepmind.google/blog/rss.xml"}]}
        cands, _ = floor.run_floor(sources, {"creators": []}, self.START, set(), fetch=lambda u, timeout=None: rss)
        self.assertEqual({c["title"]: c["from_feed"] for c in cands}, {"Own": True, "Elsewhere": False})

    def test_each_api_vouches_for_its_own_host(self):
        self.assertTrue(floor.vouched("https://github.com/a/b/releases/tag/v1", "github_releases", {}))
        self.assertTrue(floor.vouched("https://huggingface.co/papers/1", "hf_daily_papers", {}))
        self.assertTrue(floor.vouched("https://www.youtube.com/watch?v=x", "youtube", {}))
        self.assertFalse(floor.vouched("https://openai.com/index/x", "hn", {}))
        self.assertFalse(floor.vouched("https://gist.github.com/x", "github_releases", {}))

    def test_every_source_failing_aborts(self):
        _, statuses = floor.run_floor(self.sources(), {"creators": []}, self.START, set(), fetch=self.fetcher({}))
        self.assertTrue(floor.all_failed(statuses))

    def test_each_listed_creator_is_a_youtube_source(self):
        creators = {"creators": [{"name": "Two Minute Papers", "channel_id": "UCbfYPyITQ-7l4upoX8nvctg"}]}
        table = {"youtube.com/feeds/videos.xml?channel_id=UCbfYPyITQ-7l4upoX8nvctg": ParseFeedTests.ATOM}
        cands, statuses = floor.run_floor({"sources": []}, creators, self.START, set(), fetch=self.fetcher(table))
        self.assertEqual([(c["title"], c["topic_hint"]) for c in cands], [("Video one", "video")])
        self.assertEqual(statuses[0]["name"], "youtube:Two Minute Papers")

    def test_a_feed_404_falls_back_to_the_videos_page_and_the_source_stays_ok(self):
        creators = {"creators": [{"name": "Matthew Berman", "channel_id": "UCawZsQWqfGSbCI5yjkdVkTA"}]}
        page = [{"title": "New", "url": "https://www.youtube.com/watch?v=n", "published": WED_AM},
                {"title": "Old", "url": "https://www.youtube.com/watch?v=o", "published": self.START - 86400},
                {"title": "Undated", "url": "https://www.youtube.com/watch?v=u", "published": None}]
        with mock.patch.object(floor, "list_videos_page", return_value=page) as videos:
            cands, statuses = floor.run_floor({"sources": []}, creators, self.START, set(), fetch=self.fetcher(
                {"youtube.com/feeds": OSError("HTTP Error 404: Not Found")}))
        videos.assert_called_once_with("UCawZsQWqfGSbCI5yjkdVkTA")
        self.assertEqual([(c["title"], c["via"]) for c in cands], [("New", "videos-page")])
        self.assertEqual(statuses[0]["status"], "ok")

    def test_feed_and_videos_page_both_failing_fails_only_that_creator(self):
        creators = {"creators": [{"name": "A", "channel_id": "UCa"}]}
        with mock.patch.object(floor, "list_videos_page", side_effect=OSError("yt-dlp exit 1")):
            _, statuses = floor.run_floor({"sources": []}, creators, self.START, set(),
                                          fetch=self.fetcher({"youtube.com/feeds": OSError("404")}))
        self.assertEqual(statuses[0]["status"], "failed")

    def test_the_videos_page_reader_asks_ytdlp_for_flat_metadata_with_approximate_dates(self):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps({"entries": [
            {"id": "abc", "title": "T", "timestamp": 1793725200}, {"id": "def", "title": "U"}]}).encode(), stderr=b"")
        with mock.patch.object(floor.subprocess, "run", return_value=done) as run, \
                mock.patch.dict(os.environ, {"SPIDER_YTDLP": "/x/yt-dlp"}):
            got = floor.list_videos_page("UCa")
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "/x/yt-dlp")
        for flag in ("--flat-playlist", "-J", "youtubetab:approximate_date", "https://www.youtube.com/channel/UCa/videos"):
            self.assertIn(flag, argv)
        self.assertNotIn("--download", argv)
        self.assertEqual(got, [{"title": "T", "url": "https://www.youtube.com/watch?v=abc", "published": 1793725200.0},
                               {"title": "U", "url": "https://www.youtube.com/watch?v=def", "published": None}])

    def test_every_shipped_creator_has_a_name_and_a_unique_channel_id(self):
        # No count: the reader adds and drops creators by editing the file.
        with open(os.path.join(SPIDER, "creators.json")) as f:
            creators = json.load(f)["creators"]
        self.assertTrue(creators)
        self.assertEqual(len({c["channel_id"] for c in creators}), len(creators))
        self.assertTrue(all(c["name"] and c["channel_id"].startswith("UC") and len(c["channel_id"]) == 24
                            for c in creators))

    def test_the_shipped_sources_file_parses_and_every_kind_is_known(self):
        with open(os.path.join(SPIDER, "sources.json")) as f:
            for s in json.load(f)["sources"]:
                self.assertIn(s["kind"], floor.PULL, s)


class ScheduleTests(unittest.TestCase):
    def test_the_slots_are_730_and_1330_every_day(self):
        self.assertEqual(state.latest_slot(WED_AM + 60)[:2], ("2026-11-04-am", "am"))
        self.assertEqual(state.latest_slot(WED_PM + 60)[:2], ("2026-11-04-pm", "pm"))
        self.assertEqual(state.latest_slot(WED_AM - 3600)[:2], ("2026-11-03-pm", "pm"))

    def test_a_firing_a_moment_early_belongs_to_its_own_slot(self):
        self.assertEqual(state.latest_slot(WED_AM - 1)[0], "2026-11-04-am")
        self.assertEqual(state.latest_slot(WED_AM - state.EARLY_FIRE_S - 1)[0], "2026-11-03-pm")

    def test_a_weekend_run_belongs_to_its_own_weekend_slot(self):
        self.assertEqual(state.latest_slot(wall(2026, 11, 21, 10))[:2], ("2026-11-21-am", "am"))
        self.assertEqual(state.latest_slot(wall(2026, 11, 22, 14))[:2], ("2026-11-22-pm", "pm"))
        self.assertEqual(state.latest_slot(wall(2026, 11, 23, 7, 28))[:2], ("2026-11-22-pm", "pm"))

    def test_expected_slots_between_two_times(self):
        got = [s[0] for s in state.slots_between(wall(2026, 11, 20, 12), wall(2026, 11, 23, 14))]
        self.assertEqual(got, ["2026-11-20-pm", "2026-11-21-am", "2026-11-21-pm", "2026-11-22-am", "2026-11-22-pm",
                               "2026-11-23-am", "2026-11-23-pm"])


class WindowAndLedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_first_run_looks_back_a_day(self):
        self.assertEqual(state.window_start(self.dir, WED_AM), WED_AM - state.FIRST_WINDOW_S)

    def test_the_window_starts_at_the_last_successful_post(self):
        state.record_success(self.dir, wall(2026, 11, 3, 13, 30))
        self.assertEqual(state.window_start(self.dir, WED_AM), wall(2026, 11, 3, 13, 30))

    def test_a_post_covers_everything_since_the_last_one_across_a_weekend(self):
        fri = wall(2026, 11, 6, 13, 31)
        state.record_success(self.dir, fri)
        self.assertEqual(state.window_start(self.dir, wall(2026, 11, 9, 7, 30)), fri)

    def test_status_keeps_a_month_of_slots_and_trims_beyond_it(self):
        self.assertGreaterEqual(state.STATUS_KEEP, 62)  # 31 days at two slots a day
        for day in range(1, 32):
            for name in ("am", "pm"):
                state.write_status(self.dir, "2026-10-%02d-%s" % (day, name), "OK", "1 item", WED_AM, "/r")
        self.assertEqual(len(state.load_status(self.dir)["slots"]), 62)
        for day in range(1, 11):
            state.write_status(self.dir, "2026-11-%02d-am" % day, "OK", "1 item", WED_AM, "/r")
        slots = sorted(state.load_status(self.dir)["slots"])
        self.assertEqual(len(slots), state.STATUS_KEEP)
        self.assertEqual(slots[-1], "2026-11-10-am")

    def test_ledger_round_trip_and_normalized_urls(self):
        state.append_ledger(self.dir, [item("https://a.com/x/?utm_source=s", discussion_url="https://news.ycombinator.com/item?id=9")],
                            WED_AM, "1700000000.000100")
        self.assertEqual(state.ledger_urls(self.dir), {"https://a.com/x", "https://news.ycombinator.com/item?id=9"})
        entry = state.load_ledger(self.dir)[0]
        for key in ("url", "event_key", "topic", "date", "post_ts", "headline"):
            self.assertIn(key, entry)

    def test_ledger_keeps_thirty_days(self):
        state.append_ledger(self.dir, [item("https://old.com/a")], WED_AM - state.LEDGER_KEEP_S - 86400, "1")
        state.append_ledger(self.dir, [item("https://new.com/a")], WED_AM, "2")
        self.assertEqual(state.ledger_urls(self.dir), {"https://new.com/a"})

    def test_status_keeps_one_entry_per_slot_and_the_latest_wins(self):
        state.write_status(self.dir, "2026-11-04-am", "FAIL", "post-failed", WED_AM, "/r")
        state.write_status(self.dir, "2026-11-04-am", "OK", "7 items", WED_AM + 600, "/r2")
        state.write_status(self.dir, "2026-11-04-pm", "SKIP", "no-new-items", WED_PM, "/r3")
        slots = state.load_status(self.dir)["slots"]
        self.assertEqual(slots["2026-11-04-am"]["result"], "OK")
        self.assertEqual(sorted(slots), ["2026-11-04-am", "2026-11-04-pm"])


class VerifyTests(unittest.TestCase):
    def check(self, good):
        return lambda url: url in good

    def test_an_item_whose_link_fails_is_dropped(self):
        kept, dropped = verify.verify([item("https://a.com/1"), item("https://b.com/2")], set(),
                                      check=self.check({"https://a.com/1"}))
        self.assertEqual([k["url"] for k in kept], ["https://a.com/1"])
        self.assertIn("does not resolve", dropped[0]["reason"])

    def test_a_link_in_the_ledger_is_never_posted_again_even_for_a_moved_story(self):
        kept, dropped = verify.verify([item("https://a.com/1/?utm_source=x", moved=True)], {"https://a.com/1"},
                                      check=self.check({"https://a.com/1/?utm_source=x"}))
        self.assertEqual(kept, [])
        self.assertIn("already posted", dropped[0]["reason"])

    def test_a_duplicate_link_inside_one_post_is_dropped(self):
        kept, _ = verify.verify([item("https://a.com/1"), item("https://a.com/1#x")], set(),
                                check=lambda u: True)
        self.assertEqual(len(kept), 1)

    def test_a_failing_discussion_link_is_removed_and_the_item_kept(self):
        kept, dropped = verify.verify([item("https://a.com/1", discussion_url="https://hn.com/bad")], set(),
                                      check=self.check({"https://a.com/1"}))
        self.assertEqual(kept[0]["discussion_url"], "")
        self.assertEqual(dropped[0]["what"], "discussion link")

    def test_a_discussion_link_in_the_ledger_is_removed(self):
        kept, _ = verify.verify([item("https://a.com/1", discussion_url="https://news.ycombinator.com/item?id=1")],
                                {"https://news.ycombinator.com/item?id=1"}, check=lambda u: True)
        self.assertEqual(kept[0]["discussion_url"], "")

    def test_a_discussion_link_that_is_the_items_own_link_is_removed(self):
        kept, dropped = verify.verify([item("https://news.ycombinator.com/item?id=1",
                                            discussion_url="https://news.ycombinator.com/item?id=1")],
                                      set(), check=lambda u: True)
        self.assertEqual(kept[0]["discussion_url"], "")
        self.assertEqual(dropped[0]["reason"], "repeats a link in this post")

    def test_an_unparseable_link_drops_its_item_not_the_run(self):
        kept, dropped = verify.verify([item("https://[x"), item("https://a.com/1", discussion_url="https://[y")],
                                      set(), check=lambda u: True)
        self.assertEqual([k["url"] for k in kept], ["https://a.com/1"])
        self.assertEqual(kept[0]["discussion_url"], "")
        self.assertEqual([d["reason"] for d in dropped], ["not a parseable link"] * 2)

    def test_a_feed_listed_link_skips_the_fetch(self):
        kept, _ = verify.verify([item("https://openai.com/index/x/")], set(), check=lambda u: False,
                                listed={"https://openai.com/index/x"})
        self.assertEqual(len(kept), 1)

    def test_a_listed_link_still_cannot_repeat_a_posted_one(self):
        kept, _ = verify.verify([item("https://openai.com/index/x")], {"https://openai.com/index/x"},
                                check=lambda u: True, listed={"https://openai.com/index/x"})
        self.assertEqual(kept, [])


class UrlResolvesTests(unittest.TestCase):
    def addr(self, ip):
        return mock.patch.object(verify.socket, "getaddrinfo", return_value=[(2, 1, 6, "", (ip, 0))])

    def test_a_non_http_link_fails_without_a_request(self):
        with mock.patch.object(verify._OPENER, "open") as opened:
            self.assertFalse(verify.url_resolves("ftp://example.com/x"))
        opened.assert_not_called()

    def test_loopback_private_and_link_local_hosts_are_never_fetched(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "100.64.0.1", "0.0.0.0"):
            with self.addr(ip), mock.patch.object(verify._OPENER, "open") as opened:
                self.assertFalse(verify.url_resolves("http://internal.example/x"), ip)
            opened.assert_not_called()

    def test_a_redirect_to_a_public_host_is_followed(self):
        handler = verify._PublicRedirects()
        req = verify.urllib.request.Request("http://example.com/x")
        with self.addr("93.184.216.34"):
            new = handler.redirect_request(req, None, 301, "Moved", {}, "https://www.example.com/x")
        self.assertEqual(new.full_url, "https://www.example.com/x")

    def test_a_redirect_to_a_private_address_is_refused(self):
        handler = verify._PublicRedirects()
        with self.addr("10.0.0.5"):
            with self.assertRaises(verify.urllib.error.URLError):
                handler.redirect_request(mock.Mock(), None, 302, "Found", {}, "http://10.0.0.5/admin")

    def test_an_odd_reply_drops_the_link_not_the_run(self):
        import http.client
        with self.addr("93.184.216.34"), \
                mock.patch.object(verify._OPENER, "open", side_effect=http.client.InvalidURL("control characters")):
            self.assertFalse(verify.url_resolves("https://example.com/x"))

    def test_a_non_200_success_does_not_resolve(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 204
        with self.addr("93.184.216.34"), mock.patch.object(verify._OPENER, "open", return_value=resp):
            self.assertFalse(verify.url_resolves("https://example.com/x"))

    def test_a_200_from_a_public_host_resolves(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 200
        with self.addr("93.184.216.34"), mock.patch.object(verify._OPENER, "open", return_value=resp):
            self.assertTrue(verify.url_resolves("https://example.com/x"))


class JudgeTests(unittest.TestCase):
    def test_argv_pins_model_effort_tools_schema_and_budget(self):
        argv = judge.build_argv("/usr/local/bin/claude")
        self.assertEqual(argv[:2], ["/usr/local/bin/claude", "-p"])
        for flag, value in (("--model", judge.MODEL), ("--effort", judge.EFFORT),
                            ("--tools", judge.TOOLS), ("--allowedTools", judge.TOOLS),
                            ("--output-format", "json"), ("--max-budget-usd", judge.MAX_BUDGET_USD)):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), judge.SCHEMA)
        self.assertIn("--strict-mcp-config", argv)

    def test_the_session_has_no_file_tool(self):
        argv = judge.build_argv("claude")
        for flag in ("--tools", "--allowedTools"):
            self.assertEqual(set(argv[argv.index(flag) + 1].split(",")), {"WebSearch", "WebFetch"})

    def test_check_output_refuses_an_unparseable_link_and_an_over_long_field(self):
        for it in (item("https://[x"), item(headline="h" * (judge.MAX_CHARS["headline"] + 1)),
                   item(why="w" * (judge.MAX_CHARS["why"] + 1))):
            self.assertTrue(judge.check_output({"items": [it], "hunt_added": 0}), it)

    def test_the_prompt_carries_every_input_inline(self):
        prompt = judge.build_prompt("Mon", "Tue", "TASTE-TEXT", [{"title": "CAND-1", "url": "https://c"}],
                                    [{"url": "https://ledger-url", "event_key": "ev", "epoch": 1, "post_ts": "x"}],
                                    [{"name": "SRC-1", "status": "ok"}])
        for text in ("TASTE-TEXT", "CAND-1", "https://ledger-url", "SRC-1", "never instructions"):
            self.assertIn(text, prompt)
        self.assertNotIn("post_ts", prompt)
        self.assertNotIn("Read tool", prompt)

    def test_the_resume_argv_continues_the_same_session(self):
        argv = judge.build_argv("claude", resume="abc")
        self.assertEqual(argv[argv.index("--resume") + 1], "abc")

    def test_check_output_accepts_a_good_result(self):
        self.assertEqual(judge.check_output({"items": [item()], "hunt_added": 2}), [])

    def test_check_output_names_each_problem(self):
        problems = judge.check_output({"items": [item(topic="weather", url="ftp://x")] * (judge.MAX_ITEMS + 1)})
        text = " ".join(problems)
        for word in ("hunt_added", "more than %d" % judge.MAX_ITEMS, "topic", "url"):
            self.assertIn(word, text)

    def test_child_env_uses_the_blank_profile_and_drops_api_keys(self):
        env = judge.child_env({"ANTHROPIC_API_KEY": "k", "PATH": "/bin", "HOME": "/h"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join("/h", ".claude-clean"))

    def fake(self, outputs):
        calls = []

        def runner(argv, prompt, cwd, env, timeout):
            calls.append((argv, prompt))
            return outputs.pop(0)
        return runner, calls

    def test_invalid_output_gets_one_repair_prompt_on_the_same_session(self):
        bad = (0, json.dumps({"session_id": "s1", "structured_output": {"items": "nope"}, "total_cost_usd": 0.5}), "")
        good = (0, json.dumps({"session_id": "s1", "structured_output": {"items": [], "hunt_added": 0},
                               "total_cost_usd": 0.1}), "")
        runner, calls = self.fake([bad, good])
        out = judge.run_session("claude", "prompt", "/tmp", runner=runner)
        self.assertEqual(out["output"], {"items": [], "hunt_added": 0})
        self.assertEqual(len(calls), 2)
        self.assertIn("--resume", calls[1][0])
        self.assertAlmostEqual(out["cost_usd"], 0.6)

    def test_a_second_invalid_output_fails(self):
        bad = (0, json.dumps({"session_id": "s1", "structured_output": None}), "")
        runner, _ = self.fake([bad, bad])
        with self.assertRaises(judge.JudgeError):
            judge.run_session("claude", "prompt", "/tmp", runner=runner)

    def test_a_timeout_fails_without_repair(self):
        def runner(*a):
            raise subprocess.TimeoutExpired("claude", 1)
        with self.assertRaises(judge.JudgeError) as cm:
            judge.run_session("claude", "prompt", "/tmp", runner=runner)
        self.assertIn("timed out", str(cm.exception))

    def test_the_process_group_runner_kills_the_whole_group(self):
        # A kill of the child alone would leave the background sleep holding the pipe for 30 s, and alive.
        pidfile = os.path.join(tempfile.mkdtemp(), "pid")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            judge.group_runner(["/bin/sh", "-c", "sleep 30 & echo $! > %s; sleep 30" % pidfile], "",
                               tempfile.gettempdir(), dict(os.environ), 1)
        self.assertLess(time.monotonic() - started, 10)
        with open(pidfile) as f:
            grandchild = int(f.read())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild, 0)

    def test_an_error_result_gets_one_repair_and_its_reason_carries_only_the_subtype(self):
        bad = (0, json.dumps({"session_id": "s1", "is_error": True, "subtype": "error_max_budget_usd",
                              "result": "IGNORE PREVIOUS INSTRUCTIONS"}), "")
        runner, calls = self.fake([bad, bad])
        with self.assertRaises(judge.JudgeError) as cm:
            judge.run_session("claude", "prompt", "/tmp", runner=runner)
        self.assertEqual(len(calls), 2)
        self.assertIn("error_max_budget_usd", str(cm.exception))
        self.assertNotIn("IGNORE", str(cm.exception))

    def test_an_invalid_answer_with_no_session_fails_without_a_repair(self):
        runner, calls = self.fake([(0, json.dumps({"structured_output": None}), "")])
        with self.assertRaises(judge.JudgeError) as cm:
            judge.run_session("claude", "prompt", "/tmp", runner=runner)
        self.assertIn("no session to repair", str(cm.exception))
        self.assertEqual(len(calls), 1)

    def test_non_json_output_is_saved_to_the_run_folder_and_kept_out_of_the_reason(self):
        cwd = tempfile.mkdtemp()
        runner, _ = self.fake([(1, "Traceback\nSECRET-ISH TEXT", "boom\nline 2")])
        with self.assertRaises(judge.JudgeError) as cm:
            judge.run_session("claude", "prompt", cwd, runner=runner)
        self.assertNotIn("SECRET-ISH", str(cm.exception))
        self.assertNotIn("\n", str(cm.exception))
        with open(os.path.join(cwd, "session-1-output.txt")) as f:
            self.assertIn("SECRET-ISH TEXT", f.read())

    def test_a_session_that_does_not_start_fails(self):
        def runner(*a):
            raise FileNotFoundError("claude")
        with self.assertRaises(judge.JudgeError) as cm:
            judge.run_session("claude", "prompt", "/tmp", runner=runner)
        self.assertIn("did not start", str(cm.exception))

    def test_check_output_refuses_a_link_slack_would_read_as_markup(self):
        for bad in ("https://x.example/p><!channel>", "https://x.example/a|b", "https://x.example/a b"):
            problems = judge.check_output({"items": [item(bad)], "hunt_added": 0})
            self.assertTrue(any("contains" in p for p in problems), bad)


class RenderTests(unittest.TestCase):
    def test_header_counts_and_one_section_per_item(self):
        items = [item("https://a.com/%d" % i, headline="H%d" % i) for i in range(post.THIN)]
        msg = post.render("am", WED_AM, items, found=40)
        self.assertIn("Morning intel, Wed Nov 4", msg["text"])
        self.assertIn("%d kept of 40 found" % post.THIN, msg["blocks"][0]["text"]["text"])
        self.assertNotIn("Quiet", msg["text"])
        self.assertEqual(len([b for b in msg["blocks"] if b["type"] == "section"]), post.THIN + 1)

    def test_an_over_long_field_is_cut_so_the_section_stays_under_slacks_limit(self):
        msg = post.render("am", WED_AM, [item(headline="h" * 5000, why="w" * 5000)], 1)
        self.assertLess(len(msg["blocks"][1]["text"]["text"]), 3000)

    def test_a_thin_afternoon_says_so(self):
        msg = post.render("pm", WED_PM, [item()], found=5)
        self.assertIn("Quiet afternoon: 1 item", json.dumps(msg))

    def test_the_heading_follows_the_slots_hour_not_its_name(self):
        with mock.patch.dict(post.SLOT_HOURS, {"early": 6, "late": 18}):
            self.assertIn("Morning intel", post.render("early", WED_AM, [item()] * 5, 5)["text"])
            self.assertIn("Afternoon intel", post.render("late", WED_PM, [item()] * 5, 5)["text"])

    def test_item_text_escapes_slack_control_characters_and_links_both_urls(self):
        msg = post.render("am", WED_AM, [item(headline="A<b>&c", discussion_url="https://news.ycombinator.com/item?id=1")], 1)
        body = msg["blocks"][1]["text"]["text"]
        self.assertIn("A&lt;b&gt;&amp;c", body)
        self.assertIn("<https://example.com/a|", body)
        self.assertIn("<https://news.ycombinator.com/item?id=1|discussion>", body)
        self.assertIn("shipped", body)


class RenderLinkTests(unittest.TestCase):
    def test_a_link_cannot_close_its_markup_or_mention_the_channel(self):
        msg = post.render("am", WED_AM, [item("https://x.example/p><!channel>|label")], 1)
        body = msg["blocks"][1]["text"]["text"]
        self.assertNotIn("<!channel>", body)
        self.assertIn("<https://x.example/p%3E%3C!channel%3E%7Clabel|x.example>", body)


class PostMessageTests(unittest.TestCase):
    def reply(self, body):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps(body).encode()
        return resp

    def post(self, **kw):
        with mock.patch.object(post.urllib.request, "urlopen", **kw) as opened:
            result = post.post_message("xoxb-t", {"text": "t", "blocks": []})
        return result, opened

    def test_ok_returns_the_ts_and_sends_the_token_only_in_the_header(self):
        ts, opened = self.post(return_value=self.reply({"ok": True, "ts": "1.2"}))
        self.assertEqual(ts, "1.2")
        req = opened.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer xoxb-t")
        self.assertNotIn(b"xoxb-t", req.data)
        self.assertEqual(json.loads(req.data)["channel"], post.CHANNEL)

    def test_ok_false_is_a_retryable_error(self):
        with self.assertRaises(post.PostError) as cm:
            self.post(return_value=self.reply({"ok": False, "error": "channel_not_found"}))
        self.assertEqual((str(cm.exception), cm.exception.retryable), ("channel_not_found", True))

    def test_a_4xx_retries_but_a_5xx_does_not_since_it_may_come_after_slack_stored_the_post(self):
        for code, retry in ((429, True), (403, True), (500, False), (502, False), (504, False)):
            err = post.urllib.error.HTTPError("u", code, "x", {}, None)
            with self.assertRaises(post.PostError) as cm:
                self.post(side_effect=err)
            self.assertEqual(cm.exception.retryable, retry, code)

    def test_a_refused_connection_retries_but_a_timeout_after_sending_does_not(self):
        for before in (ConnectionRefusedError(), post.socket.gaierror(8, "nodename nor servname")):
            with self.assertRaises(post.PostError) as cm:
                self.post(side_effect=post.urllib.error.URLError(before))
            self.assertTrue(cm.exception.retryable, before)
        for err in (post.socket.timeout("read"), ConnectionResetError(), post.urllib.error.URLError(post.socket.timeout()),
                    post.urllib.error.URLError(BrokenPipeError()), post.urllib.error.URLError(ConnectionAbortedError())):
            with self.assertRaises(post.PostError) as cm:
                self.post(side_effect=err)
            self.assertFalse(cm.exception.retryable, err)

    def test_an_unreadable_reply_is_unconfirmed(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b"<html>"
        with self.assertRaises(post.PostError) as cm:
            self.post(return_value=resp)
        self.assertFalse(cm.exception.retryable)


class PublishTests(unittest.TestCase):
    """publish(), the one way a digest leaves Spider."""

    def test_publish_returns_the_ts_and_retries_only_a_certain_non_post(self):
        tries, naps = [], []

        def poster(tok, msg):
            tries.append(msg)
            if len(tries) < 3:
                raise post.PostError("ratelimited")
            return "1.2"

        self.assertEqual(post.publish("xoxb-t", {"text": "t"}, post=poster, sleep=naps.append), "1.2")
        self.assertEqual((len(tries), naps), (3, list(post.POST_WAITS_S)))

    def test_publish_raises_at_once_when_a_try_may_have_posted(self):
        tries = []

        def poster(tok, msg):
            tries.append(msg)
            raise post.PostError("unconfirmed: timeout", retryable=False)

        with self.assertRaises(post.PostError):
            post.publish("xoxb-t", {"text": "t"}, post=poster, sleep=lambda s: None)
        self.assertEqual(len(tries), 1)


class ConfigTests(unittest.TestCase):
    """config.json: the channel, the Keychain item and the zone, read once at import."""

    GOOD = {"slack_channel": "C1", "keychain_service": "svc", "keychain_account": "acct",
            "time_zone": "America/New_York"}

    def load(self, value):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "config.json")
        with open(path, "w") as f:
            json.dump(value, f)
        return state.load_config(path)

    def test_the_post_and_the_keychain_read_use_the_shipped_config(self):
        with open(os.path.join(SPIDER, "config.json")) as f:
            self.assertEqual(state.CONFIG, json.load(f))
        self.assertEqual((post.CHANNEL, post.KEYCHAIN_SERVICE, post.KEYCHAIN_ACCOUNT),
                         tuple(state.CONFIG[k] for k in ("slack_channel", "keychain_service", "keychain_account")))
        self.assertEqual(state.TZ, state.ZoneInfo(state.CONFIG["time_zone"]))

    def test_a_good_file_loads(self):
        self.assertEqual(self.load(self.GOOD), self.GOOD)

    def test_a_bad_file_is_refused(self):
        # reader was a key until the prompt stopped naming the reader; a file still carrying it is an extra key.
        for bad in (dict(self.GOOD, extra="x"), dict(self.GOOD, reader="Sam"),
                    {k: v for k, v in self.GOOD.items() if k != "keychain_account"},
                    dict(self.GOOD, keychain_account=""), dict(self.GOOD, slack_channel=7),
                    dict(self.GOOD, time_zone="Mars/Olympus"), dict(self.GOOD, time_zone="../etc"), []):
            with self.assertRaises(ValueError, msg=bad):
                self.load(bad)

    def test_a_zone_with_daylight_time_has_its_offset_by_date(self):
        ny = state.ZoneInfo("America/New_York")
        self.assertEqual(state.zone_offset_s(wall(2026, 7, 1, 12), ny), -4 * 3600)
        self.assertEqual(state.zone_offset_s(wall(2026, 12, 1, 12), ny), -5 * 3600)

    def test_the_prompt_writes_to_the_reader_as_you_in_the_configured_zone(self):
        prompt = judge.build_prompt("A", "B", "taste", [], [], [])
        self.assertIn("what changed in AI for the reader.", prompt)
        self.assertIn("(%s time)" % state.CONFIG["time_zone"], prompt)
        self.assertIn('never by name or as "the reader"', prompt)
        self.assertNotIn("{reader}", prompt)


class KeychainTokenTests(unittest.TestCase):
    def security(self, code, out):
        done = subprocess.CompletedProcess([], code, stdout=out, stderr=b"")
        return mock.patch.object(post.subprocess, "run", return_value=done)

    def test_a_bot_token_is_returned_and_read_from_stdout_not_argv(self):
        with self.security(0, b"xoxb-123-abc\n") as run:
            self.assertEqual(post.keychain_token(), "xoxb-123-abc")
        argv = run.call_args[0][0]
        self.assertEqual(argv[-3:], ["-s", post.KEYCHAIN_SERVICE, "-w"])

    def test_no_item_a_bare_prefix_or_another_kind_of_token_is_none(self):
        for code, out in ((44, b""), (0, b"xoxb-"), (0, b"xapp-1-2"), (0, b"")):
            with self.security(code, out):
                self.assertIsNone(post.keychain_token(), out)

    def test_security_not_running_is_none(self):
        with mock.patch.object(post.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 30)):
            self.assertIsNone(post.keychain_token())


class RunTests(unittest.TestCase):
    """The run's flow against fakes: the result line, the marker, the ledger, the window and the status."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.posts = []
        self.session_items = [item("https://a.com/1"), item("https://b.com/2", event_key="b")]

    def deps(self, token="xoxb-test", post_ok=True, floor_failed=False):
        def run_floor(sources, creators, start, ledger, fetch=None):
            if floor_failed:
                return [], [{"name": "x", "status": "failed"}]
            return [{"title": "t", "url": "https://a.com/1", "sources": ["x"]}], [{"name": "x", "status": "ok"}]

        def session(prompt, cwd):
            return {"output": {"items": list(self.session_items), "hunt_added": 3}, "cost_usd": 0.2, "seconds": 1}

        def poster(tok, msg):
            self.posts.append(msg)
            if not post_ok:
                raise post.PostError("channel_not_found")
            return "1700000000.000100"

        return spider.Deps(run_floor=run_floor, session=session, check_url=lambda u: True,
                           token=lambda: token, post=poster, network_ready=lambda: True, sleep=lambda s: None,
                           utc_offset=state.zone_offset_s)

    def run_at(self, now, **kw):
        return spider.run(self.dir, now, self.deps(**kw), dry_run=kw.get("dry_run", False), config_dir=SPIDER)

    def test_a_good_run_posts_then_writes_ledger_marker_window_and_status(self):
        line = spider.run(self.dir, WED_AM + 60, self.deps(), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am OK 2 items")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(state.ledger_urls(self.dir), {"https://a.com/1", "https://b.com/2"})
        self.assertTrue(state.has_marker(self.dir, "2026-11-04-am"))
        self.assertEqual(state.window_start(self.dir, WED_PM), WED_AM + 60)
        self.assertEqual(state.load_status(self.dir)["slots"]["2026-11-04-am"]["result"], "OK")

    def test_a_repeat_run_for_the_same_slot_does_nothing(self):
        spider.run(self.dir, WED_AM + 60, self.deps(), config_dir=SPIDER)
        line = spider.run(self.dir, WED_AM + 120, self.deps(), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am SKIP already-done")
        self.assertEqual(len(self.posts), 1)

    def test_a_late_morning_run_after_1330_is_the_afternoon_slot_and_posts_once(self):
        # The Mac slept through 7:30 and woke at 2:00 PM: launchd fires once, and the 1:30 firing finds the marker.
        state.record_success(self.dir, wall(2026, 11, 3, 13, 31))  # Tuesday afternoon's post
        first = spider.run(self.dir, wall(2026, 11, 4, 14), self.deps(), config_dir=SPIDER)
        second = spider.run(self.dir, wall(2026, 11, 4, 14, 1), self.deps(), config_dir=SPIDER)
        self.assertEqual(first, "RESULT 2026-11-04-pm OK 2 items")
        self.assertEqual(second, "RESULT 2026-11-04-pm SKIP already-done")
        self.assertEqual(len(self.posts), 1)
        am = state.load_status(self.dir)["slots"]["2026-11-04-am"]
        self.assertEqual((am["result"], am["reason"]), ("MISSED", "folded into 2026-11-04-pm"))

    def test_folding_stops_at_a_slot_that_has_its_own_result(self):
        state.write_status(self.dir, "2026-11-04-am", "FAIL", "post-failed: x", WED_AM, "/r")
        spider.run(self.dir, WED_PM + 60, self.deps(), config_dir=SPIDER)
        slots = state.load_status(self.dir)["slots"]
        self.assertEqual(slots["2026-11-04-am"]["result"], "FAIL")
        self.assertEqual(slots["2026-11-04-pm"]["result"], "OK")

    def test_the_first_post_folds_nothing_since_earlier_slots_may_predate_the_install(self):
        # Installed Wed 10:00 AM; the first run at 1:31 PM must not call Wed 7:30 AM a slot the Mac slept through.
        spider.run(self.dir, WED_PM + 60, self.deps(), config_dir=SPIDER)
        self.assertEqual(sorted(state.load_status(self.dir)["slots"]), ["2026-11-04-pm"])

    def test_folding_never_reaches_before_the_last_posts_window(self):
        state.record_success(self.dir, wall(2026, 11, 3, 13, 31))
        state.write_status(self.dir, "2026-11-03-pm", "OK", "6 items", wall(2026, 11, 3, 13, 31), "/r")
        spider.run(self.dir, WED_PM + 60, self.deps(), config_dir=SPIDER)
        missed = sorted(s for s, e in state.load_status(self.dir)["slots"].items() if e["result"] == "MISSED")
        self.assertEqual(missed, ["2026-11-04-am"])

    def test_a_monday_post_after_a_slept_through_weekend_folds_every_slot_since_friday(self):
        state.record_success(self.dir, wall(2026, 11, 20, 13, 31))
        state.write_status(self.dir, "2026-11-20-pm", "OK", "6 items", wall(2026, 11, 20, 13, 31), "/r")
        spider.run(self.dir, wall(2026, 11, 23, 14), self.deps(), config_dir=SPIDER)
        slots = state.load_status(self.dir)["slots"]
        self.assertEqual(sorted(slots), ["2026-11-20-pm", "2026-11-21-am", "2026-11-21-pm", "2026-11-22-am",
                                         "2026-11-22-pm", "2026-11-23-am", "2026-11-23-pm"])
        self.assertEqual([s for s, e in sorted(slots.items()) if e["result"] == "MISSED"],
                         ["2026-11-21-am", "2026-11-21-pm", "2026-11-22-am", "2026-11-22-pm", "2026-11-23-am"])

    def test_a_dry_run_folds_nothing(self):
        spider.run(self.dir, wall(2026, 11, 4, 14), self.deps(), dry_run=True, config_dir=SPIDER)
        self.assertEqual(state.load_status(self.dir)["slots"], {})

    def test_an_unconfirmed_post_is_not_retried_and_records_nothing(self):
        def poster(tok, msg):
            self.posts.append(msg)
            raise post.PostError("unconfirmed: timeout", retryable=False)
        line = spider.run(self.dir, WED_AM + 60, self.deps()._replace(post=poster), config_dir=SPIDER)
        self.assertTrue(line.startswith("RESULT 2026-11-04-am FAIL post-unconfirmed"), line)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(state.ledger_urls(self.dir), set())
        self.assertFalse(state.has_marker(self.dir, "2026-11-04-am"))

    def test_a_feed_listed_link_passes_verify_without_a_fetch(self):
        def run_floor(sources, creators, start, ledger, fetch=None):
            return ([{"title": "t", "url": "https://openai.com/index/x", "sources": ["openai"], "from_feed": True}],
                     [{"name": "openai", "status": "ok"}])
        self.session_items = [item("https://openai.com/index/x/")]
        deps = self.deps()._replace(run_floor=run_floor, check_url=lambda u: False)
        self.assertEqual(spider.run(self.dir, WED_AM + 60, deps, config_dir=SPIDER), "RESULT 2026-11-04-am OK 1 item")

    def test_a_failed_post_writes_no_ledger_no_marker_and_keeps_the_window(self):
        line = spider.run(self.dir, WED_AM + 60, self.deps(post_ok=False), config_dir=SPIDER)
        self.assertTrue(line.startswith("RESULT 2026-11-04-am FAIL post-failed"), line)
        self.assertEqual(len(self.posts), 3)  # the try and two retries
        self.assertEqual(state.ledger_urls(self.dir), set())
        self.assertFalse(state.has_marker(self.dir, "2026-11-04-am"))
        self.assertEqual(state.window_start(self.dir, WED_PM), WED_PM - state.FIRST_WINDOW_S)
        self.assertEqual(state.load_status(self.dir)["slots"]["2026-11-04-am"]["result"], "FAIL")

    def test_no_token_writes_the_would_be_post_and_fails_the_slot(self):
        line = spider.run(self.dir, WED_AM + 60, self.deps(token=None), config_dir=SPIDER)
        self.assertTrue(line.startswith("RESULT 2026-11-04-am FAIL no-token"), line)
        self.assertEqual(self.posts, [])
        run_dir = state.load_status(self.dir)["slots"]["2026-11-04-am"]["run_dir"]
        with open(os.path.join(run_dir, "post.json")) as f:
            self.assertIn("blocks", json.load(f))
        self.assertEqual(state.ledger_urls(self.dir), set())

    def test_a_dry_run_posts_nothing_and_writes_no_state(self):
        line = spider.run(self.dir, WED_AM + 60, self.deps(), dry_run=True, config_dir=SPIDER)
        self.assertTrue(line.startswith("RESULT 2026-11-04-am DRYRUN 2 items"), line)
        self.assertEqual(self.posts, [])
        self.assertEqual(state.ledger_urls(self.dir), set())
        self.assertFalse(state.has_marker(self.dir, "2026-11-04-am"))
        self.assertEqual(state.load_status(self.dir), {"slots": {}})

    def test_no_new_items_skips_and_marks_the_slot(self):
        self.session_items = []
        line = spider.run(self.dir, WED_AM + 60, self.deps(), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am SKIP no-new-items")
        self.assertEqual(self.posts, [])
        self.assertTrue(state.has_marker(self.dir, "2026-11-04-am"))

    def test_every_source_failing_fails_the_slot(self):
        line = spider.run(self.dir, WED_AM + 60, self.deps(floor_failed=True), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am FAIL all-sources-failed")

    def test_network_never_ready_fails_the_slot(self):
        deps = self.deps()._replace(network_ready=lambda: False)
        line = spider.run(self.dir, WED_AM + 60, deps, config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am FAIL network-not-ready")

    def test_a_judge_failure_fails_the_slot_and_posts_nothing(self):
        def session(*a):
            raise judge.JudgeError("invalid output after repair")
        deps = self.deps()._replace(session=session)
        line = spider.run(self.dir, WED_AM + 60, deps, config_dir=SPIDER)
        self.assertTrue(line.startswith("RESULT 2026-11-04-am FAIL judge"), line)
        self.assertEqual(self.posts, [])

    def test_a_crash_is_a_failed_slot_with_a_status_entry(self):
        def session(*a):
            raise RuntimeError("boom\nsecond line")
        line = spider.run(self.dir, WED_AM + 60, self.deps()._replace(session=session), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am FAIL crash (RuntimeError)")
        self.assertEqual(state.load_status(self.dir)["slots"]["2026-11-04-am"]["result"], "FAIL")

    def test_every_link_failing_verification_fails_the_slot_and_posts_nothing(self):
        # Not a quiet day: the news was there and the check (or the network) failed, so status.json must show it.
        line = spider.run(self.dir, WED_AM + 60, self.deps()._replace(check_url=lambda u: False), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am FAIL no-verified-items: 2 of 2 links failed verification")
        self.assertEqual(self.posts, [])
        self.assertFalse(state.has_marker(self.dir, "2026-11-04-am"))

    def test_every_item_repeating_a_posted_link_is_a_quiet_skip(self):
        state.append_ledger(self.dir, self.session_items, WED_AM - 86400, "0.1")
        line = spider.run(self.dir, WED_AM + 60, self.deps(), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am SKIP no-new-items: every item repeated a posted link")
        self.assertTrue(state.has_marker(self.dir, "2026-11-04-am"))

    def test_a_reinstall_after_an_uninstall_folds_nothing_from_the_gap(self):
        # Last post Mon 7:30 AM, uninstalled Mon, reinstalled Wed 10:00 AM (a new install record), Wed 1:31 PM run.
        state.record_success(self.dir, wall(2026, 11, 2, 7, 31))
        with open(os.path.join(self.dir, "installed"), "w") as f:
            f.write("%d\n" % wall(2026, 11, 4, 10))
        spider.run(self.dir, WED_PM + 60, self.deps(), config_dir=SPIDER)
        self.assertEqual(sorted(state.load_status(self.dir)["slots"]), ["2026-11-04-pm"])

    def test_a_mac_off_the_configured_zone_fails_the_slot_and_says_why(self):
        want = state.zone_offset_s(WED_AM + 60)
        line = spider.run(self.dir, WED_AM + 60, self.deps()._replace(utc_offset=lambda now: want + 2 * 3600),
                          config_dir=SPIDER)
        self.assertTrue(line.startswith("RESULT 2026-11-04-am FAIL mac-clock-not-time-zone: the Mac is on UTC%+.1f h "
                                        "and config.json's %s is UTC%+.1f h" % (want / 3600.0 + 2,
                                                                               state.CONFIG["time_zone"],
                                                                               want / 3600.0)), line)
        self.assertEqual(self.posts, [])

    def test_an_ok_post_names_the_sources_that_failed(self):
        def run_floor(sources, creators, start, ledger, fetch=None):
            return ([{"title": "t", "url": "https://a.com/1", "sources": ["x"]}],
                    [{"name": "x", "status": "ok"}, {"name": "youtube:A", "status": "failed"}])
        line = spider.run(self.dir, WED_AM + 60, self.deps()._replace(run_floor=run_floor), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am OK 2 items; failed sources: youtube:A")

    def test_an_item_already_in_the_ledger_is_not_posted_and_the_header_counts_the_hunt(self):
        state.append_ledger(self.dir, [item("https://b.com/2")], WED_AM - 86400, "0.1")
        spider.run(self.dir, WED_AM + 60, self.deps(), config_dir=SPIDER)
        body = json.dumps(self.posts[0])
        self.assertIn("https://a.com/1", body)
        self.assertNotIn("https://b.com/2", body)
        self.assertIn("1 kept of 4 found", body)  # 1 floor candidate + hunt_added 3

    def test_a_dry_run_that_finds_nothing_writes_no_marker_status_or_fold(self):
        self.session_items = []
        state.record_success(self.dir, wall(2026, 11, 3, 13, 31))
        line = spider.run(self.dir, WED_PM + 60, self.deps(), dry_run=True, config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-pm SKIP no-new-items")
        self.assertFalse(state.has_marker(self.dir, "2026-11-04-pm"))
        self.assertEqual(state.load_status(self.dir)["slots"], {})

    def test_a_record_failure_after_slacks_ok_is_a_failed_slot_that_says_it_posted(self):
        with mock.patch.object(state, "append_ledger", side_effect=OSError("disk full")):
            line = spider.run(self.dir, WED_AM + 60, self.deps(), config_dir=SPIDER)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("FAIL posted (ts 1700000000.000100) but recording it failed (OSError)", line)

    def test_a_multi_line_reason_becomes_one_line(self):
        def session(*a):
            raise judge.JudgeError("first\nsecond\tthird")
        line = spider.run(self.dir, WED_AM + 60, self.deps()._replace(session=session), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am FAIL judge: first second third")
        self.assertEqual(state.load_status(self.dir)["slots"]["2026-11-04-am"]["reason"], "judge: first second third")

    def test_the_run_alarm_fails_the_slot_with_its_reason(self):
        def session(*a):
            raise spider.RunTimeout()
        line = spider.run(self.dir, WED_AM + 60, self.deps()._replace(session=session), config_dir=SPIDER)
        self.assertEqual(line, "RESULT 2026-11-04-am FAIL run timed out after %d s" % spider.RUN_TIMEOUT_S)

    def test_the_alarm_is_off_while_the_post_and_its_record_run(self):
        remaining = []
        spider.signal.signal(spider.signal.SIGALRM, lambda *a: None)
        self.addCleanup(spider.signal.alarm, 0)

        def poster(tok, msg):
            remaining.append(spider.signal.alarm(0))  # what was left on the alarm when the post began
            return "1.0"
        spider.signal.alarm(600)
        spider.run(self.dir, WED_AM + 60, self.deps()._replace(post=poster), config_dir=SPIDER)
        self.assertEqual(remaining, [0])

    def test_real_deps_wires_the_real_functions(self):
        with mock.patch.dict(os.environ, {"SPIDER_CLAUDE": "/x/claude"}):
            deps = spider.real_deps()
        self.assertIs(deps.run_floor, floor.run_floor)
        self.assertIs(deps.check_url, verify.url_resolves)
        self.assertIs(deps.token, post.keychain_token)
        self.assertIs(deps.post, post.post_message)
        self.assertIs(deps.network_ready, spider.network_ready)
        self.assertIs(deps.sleep, time.sleep)
        with mock.patch.object(judge, "run_session", return_value={"output": {}}) as run_session:
            deps.session("PROMPT", "/run")
        run_session.assert_called_once_with("/x/claude", "PROMPT", "/run")

    def test_the_run_alarm_is_not_an_exception_a_source_can_swallow(self):
        self.assertFalse(issubclass(spider.RunTimeout, Exception))

    def test_the_run_budget_is_the_sum_of_every_stages_own_bound(self):
        self.assertEqual(spider.RUN_TIMEOUT_S, sum(spider.STAGE_BUDGETS_S.values()))
        self.assertEqual(spider.STAGE_BUDGETS_S["session and repair"], 2 * judge.TIMEOUT_S)
        self.assertEqual(spider.STAGE_BUDGETS_S["floor"], floor.FLOOR_BUDGET_S + floor.SLOWEST_SOURCE_S)

    def test_the_result_line_is_appended_to_the_results_log(self):
        spider.run(self.dir, WED_AM + 60, self.deps(), config_dir=SPIDER)
        with open(os.path.join(self.dir, "results.log")) as f:
            self.assertIn("RESULT 2026-11-04-am OK 2 items", f.read())


class MainTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.runs = []
        patch = mock.patch.object(spider, "run", side_effect=self.fake_run)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(spider.signal.alarm, 0)

    def fake_run(self, state_dir, now, deps, dry_run=False):
        self.runs.append(now)
        return self.lines.pop(0)

    def main(self, times, lines, *args):
        self.lines = list(lines)
        clock = iter(times)
        with mock.patch("sys.stdout"):
            return spider.main(["run", "--state-dir", self.dir] + list(args), deps=object(), clock=lambda: next(clock))

    def test_a_run_that_ends_in_the_next_slot_runs_once_more_for_it(self):
        # The 7:30 run fired on wake at 1:10 PM and ended at 1:40 PM, so launchd's 1:30 firing was skipped.
        code = self.main([wall(2026, 11, 4, 13, 10), wall(2026, 11, 4, 13, 40), wall(2026, 11, 4, 13, 40),
                          wall(2026, 11, 4, 13, 45)], ["RESULT a OK 6 items", "RESULT b OK 2 items"])
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(code, 0)

    def test_a_run_inside_its_own_slot_runs_once(self):
        self.main([WED_AM + 60, WED_AM + 600], ["RESULT a OK 6 items"])
        self.assertEqual(len(self.runs), 1)

    def test_a_dry_run_never_runs_twice(self):
        self.main([wall(2026, 11, 4, 13, 10), wall(2026, 11, 4, 13, 40)], ["RESULT a DRYRUN 6 items"], "--dry-run")
        self.assertEqual(len(self.runs), 1)

    def test_a_failed_run_exits_1(self):
        self.assertEqual(self.main([WED_AM + 60, WED_AM + 600], ["RESULT a FAIL post-failed: x"]), 1)

    def test_a_dry_run_proceeds_while_a_scheduled_run_holds_its_lock(self):
        # So the scheduled run's lock is never the one a dry run takes.
        with open(os.path.join(self.dir, "lock"), "w") as held:
            spider.fcntl.flock(held, spider.fcntl.LOCK_EX | spider.fcntl.LOCK_NB)
            self.main([WED_AM + 60, WED_AM + 600], ["RESULT a DRYRUN 6 items"], "--dry-run")
        self.assertEqual(len(self.runs), 1)

    def test_a_second_start_while_one_holds_the_lock_does_nothing(self):
        with open(os.path.join(self.dir, "lock"), "w") as held:
            spider.fcntl.flock(held, spider.fcntl.LOCK_EX | spider.fcntl.LOCK_NB)
            self.assertEqual(self.main([WED_AM + 60], []), 0)
        self.assertEqual(self.runs, [])


class NetworkReadyTests(unittest.TestCase):
    def ok(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 200
        return resp

    def test_ready_on_the_first_answer(self):
        self.assertTrue(spider.network_ready(urlopen=lambda *a, **k: self.ok(), sleep=lambda s: None))

    def test_it_polls_until_the_network_answers(self):
        answers = [OSError("down"), OSError("down"), self.ok()]

        def urlopen(*a, **k):
            value = answers.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        naps = []
        self.assertTrue(spider.network_ready(urlopen=urlopen, sleep=naps.append, clock=lambda: 0))
        self.assertEqual(naps, [spider.NETWORK_POLL_S] * 2)

    def test_it_gives_up_at_the_deadline(self):
        clock = iter([0, 100, spider.NETWORK_WAIT_S + 1])

        def down(*a, **k):
            raise OSError("down")
        self.assertFalse(spider.network_ready(urlopen=down, sleep=lambda s: None, clock=lambda: next(clock)))


class ScheduleFileTests(unittest.TestCase):
    """spider_state's loader for schedule.json, one malformed file per rule."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, schedule):
        path = os.path.join(self.dir, "schedule.json")
        with open(path, "w") as f:
            f.write(schedule if isinstance(schedule, str) else json.dumps(schedule))
        return path

    def test_the_shipped_file_loads_and_state_exposes_it(self):
        days, slots = state.load_schedule()
        self.assertEqual((days, slots), (state.DAYS, state.SLOTS))

    def test_each_malformed_file_is_refused_for_its_own_rule(self):
        good = {"name": "am", "hour": 7, "minute": 30}
        cases = [
            ("days out of range", {"days": [0, 8], "slots": [good]}),
            ("days empty", {"days": [], "slots": [good]}),
            ("days repeated", {"days": [1, 1], "slots": [good]}),
            ("days not integers", {"days": [True], "slots": [good]}),
            ("no slots", {"days": [1], "slots": []}),
            ("slot names repeated", {"days": [1], "slots": [good, dict(good, hour=9)]}),
            ("hour 24", {"days": [1], "slots": [dict(good, hour=24)]}),
            ("minute 60", {"days": [1], "slots": [dict(good, minute=60)]}),
            ("hour is a bool", {"days": [1], "slots": [dict(good, hour=True)]}),
            ("name empty", {"days": [1], "slots": [dict(good, name="")]}),
            ("name not text", {"days": [1], "slots": [dict(good, name=5)]}),
            ("name has a slash", {"days": [1], "slots": [dict(good, name="am/x")]}),
            ("name climbs out of the folder", {"days": [1], "slots": [dict(good, name="..")]}),
            ("name has a capital", {"days": [1], "slots": [dict(good, name="AM")]}),
            ("two slots share a time", {"days": [1], "slots": [good, dict(good, name="pm")]}),
            ("slots key missing", {"days": [1]}),
            ("slot field missing", {"days": [1], "slots": [{"name": "am", "hour": 7}]}),
            ("slots not a list", {"days": [1], "slots": 5}),
        ]
        for label, schedule in cases:
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    state.load_schedule(self.write(schedule))
        with self.assertRaises(ValueError):  # not JSON at all
            state.load_schedule(self.write("{"))

    def test_a_valid_file_with_slots_out_of_time_order_loads_in_time_order(self):
        _days, slots = state.load_schedule(self.write({"days": [1], "slots": [
            {"name": "pm", "hour": 13, "minute": 30}, {"name": "am", "hour": 7, "minute": 30}]}))
        self.assertEqual(slots, (("am", 7, 30), ("pm", 13, 30)))

    def test_the_status_cap_and_the_latest_slot_follow_the_schedule(self):
        # A module loaded from a folder holding a three-slot file listed out of order, as install.sh's copy would be.
        shutil.copy(os.path.join(SPIDER, "spider_state.py"), self.dir)
        shutil.copy(os.path.join(SPIDER, "config.json"), self.dir)
        self.write({"days": [1, 2, 3, 4, 5, 6, 7], "slots": [{"name": "late", "hour": 18, "minute": 0},
                                                              {"name": "noon", "hour": 12, "minute": 0},
                                                              {"name": "early", "hour": 6, "minute": 0}]})
        spec = importlib.util.spec_from_file_location("spider_state_three_slots", os.path.join(self.dir, "spider_state.py"))
        three = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(three)
        self.assertEqual(three.STATUS_KEEP, 31 * 3 + 2)
        self.assertEqual(state.STATUS_KEEP, 31 * len(state.SLOTS) + 2)
        # At 3:00 PM, the latest slot is that day's noon one, not the 6:00 AM one listed last.
        self.assertEqual(three.latest_slot(wall(2026, 11, 24, 15))[0], "2026-11-24-noon")


class InstallTests(unittest.TestCase):
    """install.sh against a temp HOME with stubbed launchctl, plutil and security."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.bin = os.path.join(self.home, "stubs")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.home, "calls.log")
        for name, body in (("launchctl", 'echo "launchctl $*" >> "%s"' % self.calls),
                           ("plutil", "exit 0"),
                           ("claude", "exit 0"),
                           ("yt-dlp", "exit 0")):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n%s\n" % body)
            os.chmod(path, 0o755)

    def run_install(self, token=True, spider_dir=SPIDER):
        sec = os.path.join(self.bin, "security")
        with open(sec, "w") as f:
            f.write("#!/bin/sh\n%s\n" % ("echo xoxb-fake" if token else "exit 44"))
        os.chmod(sec, 0o755)
        env = dict(os.environ, HOME=self.home, PATH=self.bin + ":/usr/bin:/bin", SPIDER_SECURITY_BIN=sec)
        return subprocess.run(["/bin/bash", os.path.join(spider_dir, "install.sh")], env=env,
                              capture_output=True, text=True)

    def spider_copy_with_schedule(self, schedule):
        """A copy of the Spider folder, minus its tests, with schedule.json replaced by `schedule`."""
        copy = os.path.join(self.home, "spider-copy")
        shutil.copytree(SPIDER, copy, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        with open(os.path.join(copy, "schedule.json"), "w") as f:
            json.dump(schedule, f)
        return copy

    def installed_intervals(self):
        plist_path = os.path.join(self.home, "Library", "LaunchAgents", spider.LABEL + ".plist")
        with open(plist_path, "rb") as f:
            return sorted((d["Weekday"], d["Hour"], d["Minute"]) for d in plistlib.load(f)["StartCalendarInterval"])

    def test_install_copies_the_scripts_and_renders_every_slot_for_every_day(self):
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("xoxb", proc.stdout + proc.stderr)
        share = os.path.join(self.home, ".local", "share", "spider")
        # Every module spider.py imports, read from the folder, and the three data files.
        expected = [n for n in os.listdir(SPIDER) if n.startswith("spider") and n.endswith(".py")]
        self.assertIn("spider_state.py", expected)
        # schedule.json rides along: the installed spider_state.py reads it from its folder.
        for name in expected + ["config.json", "sources.json", "creators.json", "taste.md", "schedule.json"]:
            self.assertTrue(os.path.exists(os.path.join(share, name)), name)
        plist_path = os.path.join(self.home, "Library", "LaunchAgents", spider.LABEL + ".plist")
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        # The plist's times are whatever the schedule file says: the set spider_state builds from it, and no other.
        self.assertEqual(self.installed_intervals(), sorted((w, h, m) for w in state.DAYS for _n, h, m in state.SLOTS))
        # And the block is byte-for-byte the one the template used to hold, one dict per line.
        line = ("    <dict><key>Weekday</key><integer>%d</integer><key>Hour</key><integer>%d</integer>"
                "<key>Minute</key><integer>%d</integer></dict>")
        with open(plist_path) as f:
            text = f.read()
        block = "\n".join(line % (w, h, m) for w in state.DAYS for _n, h, m in state.SLOTS)
        self.assertIn("<key>StartCalendarInterval</key>\n  <array>\n%s\n  </array>\n" % block, text)
        self.assertEqual(plist["EnvironmentVariables"]["SPIDER_CLAUDE"], os.path.join(self.bin, "claude"))
        self.assertEqual(plist["EnvironmentVariables"]["SPIDER_YTDLP"], os.path.join(self.bin, "yt-dlp"))
        with open(self.calls) as f:
            self.assertIn("bootstrap", f.read())

    def test_the_plist_and_the_slots_both_follow_a_change_to_the_schedule_file(self):
        copy = self.spider_copy_with_schedule({"days": [2, 4], "slots": [{"name": "early", "hour": 6, "minute": 15},
                                                                         {"name": "late", "hour": 18, "minute": 45}]})
        proc = self.run_install(spider_dir=copy)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.installed_intervals(), [(2, 6, 15), (2, 18, 45), (4, 6, 15), (4, 18, 45)])
        # The copy's spider_state reads the same file, here and in the folder install.sh copied it to.
        for folder in (copy, os.path.join(self.home, ".local", "share", "spider")):
            spec = importlib.util.spec_from_file_location("spider_state_at_" + os.path.basename(folder),
                                                          os.path.join(folder, "spider_state.py"))
            changed = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(changed)
            self.assertEqual(changed.SLOTS, (("early", 6, 15), ("late", 18, 45)))
            week = changed.slots_between(wall(2026, 11, 23, 0), wall(2026, 11, 30, 0))  # Mon to Sun
            self.assertEqual([s[0] for s in week], ["2026-11-24-early", "2026-11-24-late",
                                                    "2026-11-26-early", "2026-11-26-late"])

    def test_install_stops_on_a_malformed_schedule_before_it_copies_or_loads_anything(self):
        copy = self.spider_copy_with_schedule({"days": [8], "slots": []})
        proc = self.run_install(spider_dir=copy)
        self.assertNotEqual(proc.returncode, 0)
        # The schedule check's own message, not the token check's traceback (which also names the file).
        self.assertIn("schedule.json is unreadable or malformed", proc.stderr)
        self.assertFalse(os.path.exists(self.calls))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "share", "spider")))

    def test_install_records_the_first_install_time_once(self):
        record = os.path.join(self.home, ".local", "state", "spider", "installed")
        self.assertEqual(self.run_install().returncode, 0)
        with open(record) as f:
            first = f.read()
        self.assertTrue(first.strip().isdigit())
        with open(record, "w") as f:
            f.write("12345\n")
        self.assertEqual(self.run_install().returncode, 0)
        with open(record) as f:
            self.assertEqual(f.read(), "12345\n")

    def test_install_refuses_without_a_token_in_the_keychain(self):
        proc = self.run_install(token=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(post.KEYCHAIN_SERVICE, proc.stderr)
        self.assertFalse(os.path.exists(self.calls))

    def test_install_never_runs_spider_itself(self):
        with open(os.path.join(SPIDER, "install.sh")) as f:
            code = [line for line in f if not line.lstrip().startswith("#")]
        self.assertFalse([line for line in code if "spider.py" in line], code)


if __name__ == "__main__":
    unittest.main()
