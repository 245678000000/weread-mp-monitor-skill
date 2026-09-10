#!/usr/bin/env python3
"""Offline unit tests for weread_monitor. No network, no WeRead account needed.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import weread_monitor as wm  # noqa: E402

CLI = os.path.join(SCRIPTS, "weread_monitor.py")


def make_article(original_id: str, title: str, summary: str = "", ts: int = 1_700_000_000) -> dict:
    return {
        "title": title,
        "summary": summary,
        "mp_name": "测试号",
        "original_id": original_id,
        "url": f"https://mp.weixin.qq.com/s/{original_id}",
        "read_num": 0,
        "like_num": 0,
        "time": ts,
        "published_at": wm.ts_to_iso(ts),
    }


def make_state(**monitors) -> dict:
    state = wm.migrate_state({"version": wm.STATE_VERSION})
    for book_id, item in monitors.items():
        state["monitors"][book_id] = {
            "name": item.get("name", book_id),
            "book_id": book_id,
            "keywords": item.get("keywords", []),
            "match_mode": item.get("match_mode", "any"),
            "match_body": item.get("match_body", False),
            "created_at": wm.now_iso(),
            "last_checked_at": None,
            "seen": item.get("seen", []),
            "pending": item.get("pending"),
        }
    return state


# --------------------------------------------------------------------------


class TestArticleBodyExtraction(unittest.TestCase):
    def test_nested_divs_are_not_truncated(self):
        html = (
            '<div class="rich_media_content js_underline_content" id="js_content">'
            "<p>第一段</p>"
            '<div class="img_wrap"><img src="x"></div>'
            "<p>第二段：真正的重点在这里</p>"
            "</div><div>页脚</div>"
        )
        text = wm.html_to_text(wm.extract_js_content(html))
        self.assertIn("第一段", text)
        self.assertIn("真正的重点在这里", text)
        self.assertNotIn("页脚", text)

    def test_deeply_nested_sections(self):
        html = (
            '<div id="js_content"><section><div><div><p>深层内容</p></div></div></section></div>'
            "<div>不应出现</div>"
        )
        text = wm.html_to_text(wm.extract_js_content(html))
        self.assertEqual(text, "深层内容")

    def test_self_closing_div_does_not_break_depth(self):
        html = '<div id="js_content">A<div/>B</div><div>尾巴</div>'
        text = wm.html_to_text(wm.extract_js_content(html))
        self.assertNotIn("尾巴", text)
        self.assertIn("A", text)
        self.assertIn("B", text)

    def test_missing_container_returns_empty(self):
        self.assertEqual(wm.extract_js_content("<html><body>nope</body></html>"), "")

    def test_unbalanced_markup_falls_back_to_remainder(self):
        html = '<div id="js_content"><p>内容未闭合'
        self.assertIn("内容未闭合", wm.extract_js_content(html))

    def test_scripts_and_styles_are_stripped(self):
        html = '<div id="js_content"><script>var a=1;</script><style>.x{}</style><p>正文</p></div>'
        self.assertEqual(wm.html_to_text(wm.extract_js_content(html)), "正文")


class TestGetArticleBody(unittest.TestCase):
    def _fetch(self, payload: str, **kwargs):
        response = mock.MagicMock()
        response.read.return_value = payload.encode("utf-8")
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: False
        with mock.patch("urllib.request.urlopen", return_value=response):
            return wm.get_article_body("https://mp.weixin.qq.com/s/abc", **kwargs)

    def test_ok_status_and_publish_time(self):
        html = (
            '<em id="publish_time" class="rich_media_meta">2026-09-01 08:00</em>'
            '<div id="js_content"><p>正文内容</p></div>'
        )
        out = self._fetch(html)
        self.assertEqual(out["body_status"], "ok")
        self.assertEqual(out["pub_time"], "2026-09-01 08:00")
        self.assertEqual(out["body"], "正文内容")
        self.assertFalse(out["body_truncated"])

    def test_truncation_is_flagged(self):
        html = '<div id="js_content"><p>' + ("字" * 500) + "</p></div>"
        out = self._fetch(html, max_chars=100)
        self.assertTrue(out["body_truncated"])
        self.assertEqual(len(out["body"]), 100)

    def test_blocked_page(self):
        out = self._fetch("<html>环境异常，请完成验证 去验证</html>")
        self.assertEqual(out["body_status"], "blocked")

    def test_empty_container(self):
        out = self._fetch('<div id="js_content">   </div>')
        self.assertEqual(out["body_status"], "empty")

    def test_fetch_failure_is_not_fatal(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            out = wm.get_article_body("https://mp.weixin.qq.com/s/abc")
        self.assertEqual(out["body_status"], "fetch_failed")
        self.assertEqual(out["body"], "")

    def test_no_url(self):
        self.assertEqual(wm.get_article_body("")["body_status"], "no_url")


class TestMatching(unittest.TestCase):
    def test_no_keywords_matches_everything(self):
        self.assertTrue(wm.article_matches(make_article("a", "任意标题"), []))

    def test_any_mode(self):
        art = make_article("a", "聊聊 Agent 的落地")
        self.assertTrue(wm.article_matches(art, ["Agent", "编程"], mode="any"))

    def test_all_mode_requires_every_keyword(self):
        art = make_article("a", "聊聊 Agent 的落地")
        self.assertFalse(wm.article_matches(art, ["Agent", "编程"], mode="all"))
        art2 = make_article("b", "Agent 与编程实践")
        self.assertTrue(wm.article_matches(art2, ["Agent", "编程"], mode="all"))

    def test_case_insensitive(self):
        art = make_article("a", "AGENT 专题")
        self.assertTrue(wm.article_matches(art, ["agent"]))

    def test_body_matching_is_opt_in(self):
        art = make_article("a", "周报", "本周要闻")
        art["body"] = "文中详细讨论了 Agent"
        self.assertFalse(wm.article_matches(art, ["Agent"], include_body=False))
        self.assertTrue(wm.article_matches(art, ["Agent"], include_body=True))

    def test_matched_keywords_reported(self):
        art = make_article("a", "Agent 与 RAG")
        self.assertEqual(wm.matched_keywords(art, ["Agent", "编程", "RAG"]), ["Agent", "RAG"])


class TestAccountResolution(unittest.TestCase):
    ACCOUNTS = [
        {"name": "豆包", "book_id": "MP_WXS_1"},
        {"name": "豆包科技", "book_id": "MP_WXS_2"},
        {"name": "最高人民法院", "book_id": "MP_WXS_3"},
    ]

    def test_exact_wins_over_partial(self):
        self.assertEqual(wm.resolve_account(self.ACCOUNTS, "豆包")["book_id"], "MP_WXS_1")

    def test_unique_partial(self):
        self.assertEqual(wm.resolve_account(self.ACCOUNTS, "最高人民")["book_id"], "MP_WXS_3")

    def test_ambiguous_partial_raises(self):
        with self.assertRaises(wm.AmbiguousAccount) as ctx:
            wm.resolve_account(self.ACCOUNTS, "豆")
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_not_found(self):
        with self.assertRaises(wm.AccountNotFound):
            wm.resolve_account(self.ACCOUNTS, "不存在的号")

    def test_whitespace_is_ignored(self):
        self.assertEqual(wm.resolve_account(self.ACCOUNTS, " 豆 包 ")["book_id"], "MP_WXS_1")


class TestGetArticles(unittest.TestCase):
    @staticmethod
    def _page(ids, create_time, review_count=10):
        """A full page. Upstream stops paging when a page holds < 10 reviews."""
        reviews = [
            {
                "createTime": create_time,
                "subReviews": [
                    {"review": {"mpInfo": {"title": f"标题{i}", "originalId": i, "content": "", "time": 1}}}
                    for i in ids
                ],
            }
        ]
        while len(reviews) < review_count:
            reviews.append({"createTime": create_time, "subReviews": []})
        return {"reviews": reviews}

    def test_stuck_cursor_does_not_loop_forever(self):
        # Every page returns the same createTime: previously an infinite loop.
        page = self._page(["a", "b"], 1000)
        page["reviews"] = page["reviews"] * 10  # >= 10 reviews so the old code kept paging
        with mock.patch.object(wm, "api_call", return_value=page) as call:
            out = wm.get_articles("cookie", "MP_WXS_1", limit=50)
        self.assertLessEqual(call.call_count, wm.MAX_ARTICLE_PAGES)
        self.assertEqual([a["original_id"] for a in out], ["a", "b"])

    def test_duplicates_across_pages_are_dropped(self):
        pages = [self._page(["a", "b"], 2000), self._page(["b", "c"], 1000), {"reviews": []}]
        with mock.patch.object(wm, "api_call", side_effect=pages):
            out = wm.get_articles("cookie", "MP_WXS_1", limit=10)
        self.assertEqual([a["original_id"] for a in out], ["a", "b", "c"])

    def test_untitled_rows_are_skipped_without_spinning(self):
        page = {"reviews": [{"createTime": 500, "subReviews": [{"review": {"mpInfo": {"originalId": "x"}}}]}]}
        with mock.patch.object(wm, "api_call", return_value=page) as call:
            out = wm.get_articles("cookie", "MP_WXS_1", limit=20)
        self.assertEqual(out, [])
        self.assertLessEqual(call.call_count, wm.MAX_ARTICLE_PAGES)

    def test_limit_is_respected(self):
        page = self._page([f"id{i}" for i in range(50)], 900)
        with mock.patch.object(wm, "api_call", return_value=page):
            out = wm.get_articles("cookie", "MP_WXS_1", limit=5)
        self.assertEqual(len(out), 5)

    def test_published_at_is_derived(self):
        page = {
            "reviews": [
                {
                    "createTime": 900,
                    "subReviews": [
                        {"review": {"mpInfo": {"title": "T", "originalId": "a", "time": 1_700_000_000}}}
                    ],
                }
            ]
        }
        with mock.patch.object(wm, "api_call", return_value=page):
            out = wm.get_articles("cookie", "MP_WXS_1", limit=1)
        self.assertTrue(out[0]["published_at"].startswith("20"))


class TestState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def test_missing_file_yields_empty_registry(self):
        state = wm.load_state(self.path)
        self.assertEqual(state["monitors"], {})
        self.assertEqual(state["version"], wm.STATE_VERSION)

    def test_corrupt_file_raises_state_error(self):
        with open(self.path, "w") as f:
            f.write("not json")
        with self.assertRaises(wm.StateError):
            wm.load_state(self.path)

    def test_non_object_raises_state_error(self):
        with open(self.path, "w") as f:
            json.dump([1, 2, 3], f)
        with self.assertRaises(wm.StateError):
            wm.load_state(self.path)

    def test_v1_state_migrates(self):
        legacy = {
            "version": 1,
            "monitors": {"MP_WXS_1": {"name": "豆包", "book_id": "MP_WXS_1", "keywords": ["A"], "seen": ["x"]}},
        }
        with open(self.path, "w") as f:
            json.dump(legacy, f)
        state = wm.load_state(self.path)
        item = state["monitors"]["MP_WXS_1"]
        self.assertEqual(state["version"], wm.STATE_VERSION)
        self.assertEqual(item["match_mode"], "any")
        self.assertFalse(item["match_body"])
        self.assertIsNone(item["pending"])
        self.assertEqual(item["seen"], ["x"])
        self.assertIn("last_auth_warning_at", state)

    def test_save_is_owner_only(self):
        wm.save_state(self.path, make_state(MP_WXS_1={"name": "豆包"}))
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode & 0o077, 0, f"state file is group/world readable: {oct(mode)}")

    def test_save_leaves_no_temp_file(self):
        wm.save_state(self.path, make_state())
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_lock_is_reentrant_across_sequential_calls(self):
        with wm.state_lock(self.path):
            pass
        with wm.state_lock(self.path):
            pass  # must not deadlock or raise


class TestCheckMonitors(unittest.TestCase):
    def test_one_broken_account_does_not_abort_the_run(self):
        state = make_state(
            MP_WXS_1={"name": "好号"},
            MP_WXS_2={"name": "坏号"},
            MP_WXS_3={"name": "另一个好号"},
        )

        def fake(cookie, book_id, limit=20):
            if book_id == "MP_WXS_2":
                raise wm.MonitorError("WeRead HTTP 500")
            return [make_article(f"{book_id}-a", "新文章")]

        with mock.patch.object(wm, "get_articles", side_effect=fake):
            result = wm.check_monitors("c", state, None, 20, False, 4000)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["new_count"], 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["name"], "坏号")
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], ["MP_WXS_1-a"])
        self.assertEqual(state["monitors"]["MP_WXS_2"]["seen"], [])

    def test_auth_failure_marks_remaining_as_skipped(self):
        state = make_state(MP_WXS_1={"name": "A"}, MP_WXS_2={"name": "B"})
        with mock.patch.object(wm, "get_articles", side_effect=wm.AuthError("expired")):
            result = wm.check_monitors("c", state, None, 20, False, 4000)
        self.assertTrue(result["auth_expired"])
        statuses = sorted(a["status"] for a in result["accounts"])
        self.assertEqual(statuses, ["error", "skipped"])

    def test_empty_account_warns(self):
        state = make_state(MP_WXS_1={"name": "取关了的号"})
        with mock.patch.object(wm, "get_articles", return_value=[]):
            result = wm.check_monitors("c", state, None, 20, False, 4000)
        self.assertEqual([w["code"] for w in result["warnings"]], ["account_empty"])

    def test_all_unseen_at_limit_warns_about_truncation(self):
        state = make_state(MP_WXS_1={"name": "高产号"})
        articles = [make_article(f"a{i}", f"文章{i}") for i in range(5)]
        with mock.patch.object(wm, "get_articles", return_value=articles):
            result = wm.check_monitors("c", state, None, 5, False, 4000)
        self.assertEqual([w["code"] for w in result["warnings"]], ["limit_reached"])

    def test_scanned_non_matching_articles_are_still_marked_seen(self):
        state = make_state(MP_WXS_1={"name": "豆包", "keywords": ["Agent"]})
        articles = [make_article("a", "讲 Agent"), make_article("b", "无关内容")]
        with mock.patch.object(wm, "get_articles", return_value=articles):
            result = wm.check_monitors("c", state, None, 20, False, 4000)
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(sorted(state["monitors"]["MP_WXS_1"]["seen"]), ["a", "b"])

    def test_match_body_fetches_before_filtering(self):
        state = make_state(MP_WXS_1={"name": "豆包", "keywords": ["Agent"], "match_body": True})
        articles = [make_article("a", "周报", "本周要闻")]
        body = {"body": "正文里提到了 Agent", "pub_time": "", "body_status": "ok", "body_truncated": False}
        with mock.patch.object(wm, "get_articles", return_value=articles), mock.patch.object(
            wm, "get_article_body", return_value=body
        ) as fetch:
            result = wm.check_monitors("c", state, None, 20, False, 4000)
        fetch.assert_called_once()
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["new_articles"][0]["matched_keywords"], ["Agent"])

    def test_all_mode_filters_correctly(self):
        state = make_state(MP_WXS_1={"name": "豆包", "keywords": ["Agent", "编程"], "match_mode": "all"})
        articles = [make_article("a", "只讲 Agent"), make_article("b", "Agent 与编程")]
        with mock.patch.object(wm, "get_articles", return_value=articles):
            result = wm.check_monitors("c", state, None, 20, False, 4000)
        self.assertEqual([a["original_id"] for a in result["new_articles"]], ["b"])

    def test_dry_run_writes_nothing(self):
        state = make_state(MP_WXS_1={"name": "豆包"})
        with mock.patch.object(wm, "get_articles", return_value=[make_article("a", "新文章")]):
            result = wm.check_monitors("c", state, None, 20, False, 4000, mark_seen=False)
        self.assertTrue(result["dry_run"])
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], [])
        self.assertIsNone(state["monitors"]["MP_WXS_1"]["last_checked_at"])

    def test_results_are_sorted_newest_first(self):
        state = make_state(MP_WXS_1={"name": "豆包"})
        articles = [make_article("old", "旧", ts=1000), make_article("new", "新", ts=2000)]
        with mock.patch.object(wm, "get_articles", return_value=articles):
            result = wm.check_monitors("c", state, None, 20, False, 4000)
        self.assertEqual([a["original_id"] for a in result["new_articles"]], ["new", "old"])

    def test_seen_list_is_capped(self):
        state = make_state(MP_WXS_1={"name": "豆包", "seen": [f"old{i}" for i in range(wm.MAX_SEEN_PER_ACCOUNT)]})
        with mock.patch.object(wm, "get_articles", return_value=[make_article("brand-new", "新")]):
            wm.check_monitors("c", state, None, 20, False, 4000)
        seen = state["monitors"]["MP_WXS_1"]["seen"]
        self.assertEqual(len(seen), wm.MAX_SEEN_PER_ACCOUNT)
        self.assertIn("brand-new", seen)


class TestDeferredAck(unittest.TestCase):
    def test_deferred_check_does_not_mark_seen_until_acked(self):
        state = make_state(MP_WXS_1={"name": "豆包"})
        articles = [make_article("a", "新文章")]

        with mock.patch.object(wm, "get_articles", return_value=articles):
            first = wm.check_monitors("c", state, None, 20, False, 4000, defer_ack=True)
        self.assertTrue(first["ack_required"])
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], [])

        # Delivery crashed before ack: the article must resurface, not vanish.
        with mock.patch.object(wm, "get_articles", return_value=articles):
            second = wm.check_monitors("c", state, None, 20, False, 4000, defer_ack=True)
        self.assertEqual(second["new_count"], 1)

        wm.ack_batch(state, second["batch_id"])
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], ["a"])
        self.assertIsNone(state["monitors"]["MP_WXS_1"]["pending"])

        with mock.patch.object(wm, "get_articles", return_value=articles):
            third = wm.check_monitors("c", state, None, 20, False, 4000, defer_ack=True)
        self.assertEqual(third["new_count"], 0)

    def test_unknown_batch_id_raises(self):
        state = make_state(MP_WXS_1={"name": "豆包"})
        with self.assertRaises(wm.BatchNotFound) as ctx:
            wm.ack_batch(state, "run-does-not-exist")
        self.assertIn("already acked", str(ctx.exception))

    def test_superseded_batch_error_names_the_current_batch(self):
        state = make_state(MP_WXS_1={"name": "豆包"})
        articles = [make_article("a", "新文章")]
        with mock.patch.object(wm, "get_articles", return_value=articles):
            first = wm.check_monitors("c", state, None, 20, False, 4000, defer_ack=True)
            second = wm.check_monitors("c", state, None, 20, False, 4000, defer_ack=True)
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        with self.assertRaises(wm.BatchNotFound) as ctx:
            wm.ack_batch(state, first["batch_id"])
        message = str(ctx.exception)
        self.assertIn("superseded", message)
        self.assertIn(second["batch_id"], message, "the error must name the batch to ack instead")
        # The stale ack must not have committed anything.
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], [])
        wm.ack_batch(state, second["batch_id"])
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], ["a"])

    def test_pending_batches_lists_outstanding_ids(self):
        state = make_state(
            MP_WXS_1={"name": "A", "pending": {"batch_id": "r1", "ids": ["x"], "created_at": ""}},
            MP_WXS_2={"name": "B", "pending": {"batch_id": "r1", "ids": ["y"], "created_at": ""}},
        )
        self.assertEqual(wm.pending_batches(state), ["r1"])

    def test_ack_all_commits_every_pending_batch(self):
        state = make_state(
            MP_WXS_1={"name": "A", "pending": {"batch_id": "r1", "ids": ["x"], "created_at": wm.now_iso()}},
            MP_WXS_2={"name": "B", "pending": {"batch_id": "r2", "ids": ["y"], "created_at": wm.now_iso()}},
        )
        result = wm.ack_batch(state, None, ack_all=True)
        self.assertEqual(result["acked_total"], 2)
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], ["x"])
        self.assertEqual(state["monitors"]["MP_WXS_2"]["seen"], ["y"])

    def test_ack_all_on_empty_registry_is_a_noop(self):
        state = make_state(MP_WXS_1={"name": "A"})
        result = wm.ack_batch(state, None, ack_all=True)
        self.assertEqual(result["acked_total"], 0)


class TestAuthWarningThrottle(unittest.TestCase):
    def test_first_warning_notifies(self):
        state = make_state()
        payload = wm.auth_warning_payload(state, "expired", 20)
        self.assertTrue(payload["notify_user"])
        self.assertIsNotNone(state["last_auth_warning_at"])

    def test_repeat_within_window_is_suppressed(self):
        state = make_state()
        wm.auth_warning_payload(state, "expired", 20)
        payload = wm.auth_warning_payload(state, "expired", 20)
        self.assertFalse(payload["notify_user"])

    def test_repeat_after_window_notifies_again(self):
        state = make_state()
        stale = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
        state["last_auth_warning_at"] = stale
        payload = wm.auth_warning_payload(state, "expired", 20)
        self.assertTrue(payload["notify_user"])

    def test_successful_run_clears_the_throttle(self):
        state = make_state()
        wm.auth_warning_payload(state, "expired", 20)
        wm.clear_auth_warning(state)
        self.assertIsNone(state["last_auth_warning_at"])
        self.assertTrue(wm.auth_warning_payload(state, "expired", 20)["notify_user"])


class TestRegistryOps(unittest.TestCase):
    ACCOUNTS = [{"name": "豆包", "book_id": "MP_WXS_1"}]

    def test_add_seeds_baseline(self):
        state = make_state()
        articles = [make_article(f"a{i}", f"旧文{i}") for i in range(3)]
        with mock.patch.object(wm, "get_accounts", return_value=self.ACCOUNTS), mock.patch.object(
            wm, "get_articles", return_value=articles
        ):
            result = wm.add_monitor("c", state, "豆包", [], 20)
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["baseline_count"], 3)
        self.assertEqual(len(state["monitors"]["MP_WXS_1"]["seen"]), 3)

    def test_baseline_zero_leaves_everything_new(self):
        state = make_state()
        with mock.patch.object(wm, "get_accounts", return_value=self.ACCOUNTS), mock.patch.object(
            wm, "get_articles"
        ) as fetch:
            wm.add_monitor("c", state, "豆包", [], 0)
        fetch.assert_not_called()
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], [])

    def test_readd_replaces_keywords_and_keeps_seen(self):
        state = make_state(MP_WXS_1={"name": "豆包", "keywords": ["旧"], "seen": ["x"]})
        with mock.patch.object(wm, "get_accounts", return_value=self.ACCOUNTS):
            result = wm.add_monitor("c", state, "豆包", ["Agent", "编程"], 20)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(state["monitors"]["MP_WXS_1"]["keywords"], ["Agent", "编程"])
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], ["x"])

    def test_add_keyword_appends_without_dropping_existing(self):
        state = make_state(MP_WXS_1={"name": "豆包", "keywords": ["Agent"]})
        with mock.patch.object(wm, "get_accounts", return_value=self.ACCOUNTS):
            wm.add_monitor("c", state, "豆包", [], 20, append_keywords=["编程", "Agent"])
        self.assertEqual(state["monitors"]["MP_WXS_1"]["keywords"], ["Agent", "编程"])

    def test_reset_baseline_reseeds_and_clears_pending(self):
        state = make_state(
            MP_WXS_1={"name": "豆包", "seen": ["x"], "pending": {"batch_id": "r", "ids": ["y"], "created_at": ""}}
        )
        with mock.patch.object(wm, "get_accounts", return_value=self.ACCOUNTS), mock.patch.object(
            wm, "get_articles", return_value=[make_article("fresh", "新")]
        ):
            wm.add_monitor("c", state, "豆包", [], 20, reset_baseline=True)
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], ["fresh"])
        self.assertIsNone(state["monitors"]["MP_WXS_1"]["pending"])

    def test_remove_reports_remaining_count(self):
        state = make_state(MP_WXS_1={"name": "豆包"}, MP_WXS_2={"name": "别的号"})
        result = wm.remove_monitor(state, "豆包")
        self.assertEqual(result["remaining"], 1)
        self.assertNotIn("MP_WXS_1", state["monitors"])

    def test_remove_unknown_raises(self):
        with self.assertRaises(wm.AccountNotFound):
            wm.remove_monitor(make_state(), "不存在")

    def test_remove_ambiguous_raises(self):
        state = make_state(MP_WXS_1={"name": "豆包"}, MP_WXS_2={"name": "豆包科技"})
        with self.assertRaises(wm.AmbiguousAccount):
            wm.remove_monitor(state, "豆")


class TestCookieResolution(unittest.TestCase):
    def test_cli_has_no_cookie_argument(self):
        parser = wm.build_parser()
        options = {action.option_strings[0] for action in parser._actions if action.option_strings}
        self.assertNotIn("--cookie", options, "raw --cookie leaks into shell history and ps")
        self.assertIn("--cookie-file", options)

    def test_env_file_beats_env_cookie(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cookie", delete=False) as f:
            f.write("from_file=1")
            path = f.name
        args = wm.build_parser().parse_args(["list"])
        with mock.patch.dict(os.environ, {"WEREAD_COOKIE_FILE": path, "WEREAD_COOKIE": "from_env=1"}):
            self.assertEqual(wm.resolve_cookie(args), "from_file=1")
        os.unlink(path)

    def test_no_browser_cookie_fails_fast(self):
        args = wm.build_parser().parse_args(["--no-browser-cookie", "list"])
        with mock.patch.dict(os.environ, {"WEREAD_COOKIE_FILE": "", "WEREAD_COOKIE": ""}):
            with self.assertRaises(wm.AuthError):
                wm.resolve_cookie(args)


class TestApiErrorMapping(unittest.TestCase):
    def test_non_json_response_is_reported_as_upstream_change(self):
        response = mock.MagicMock()
        response.read.return_value = b"<html>nope</html>"
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: False
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(wm.MonitorError) as ctx:
                wm.api_call("/web/shelf/sync", "cookie")
        self.assertIn("may have changed", str(ctx.exception))

    def test_login_error_message_maps_to_auth_error(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"errCode": -2012, "errMsg": "登录超时"}).encode()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: False
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(wm.AuthError):
                wm.api_call("/web/shelf/sync", "cookie")


class TestCli(unittest.TestCase):
    """End-to-end checks that the CLI always emits structured JSON."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def run_cli(self, *argv):
        proc = subprocess.run(
            [sys.executable, CLI, "--state", self.path, "--json", *argv],
            capture_output=True,
            text=True,
        )
        return proc

    def test_corrupt_state_returns_json_not_traceback(self):
        with open(self.path, "w") as f:
            f.write("not json")
        proc = self.run_cli("list")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(proc.stderr, "", "a corrupt state file must not produce a traceback")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["code"], "state_unreadable")

    def test_list_on_empty_registry(self):
        proc = self.run_cli("list")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), {"status": "ok", "monitors": []})

    def test_remove_missing_monitor_is_structured(self):
        proc = self.run_cli("remove", "不存在")
        self.assertEqual(proc.returncode, 3)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["code"], "account_not_found")
        self.assertIn("next_action", payload)

    def test_ack_unknown_batch_is_structured(self):
        proc = self.run_cli("ack", "run-nope")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(json.loads(proc.stdout)["code"], "batch_not_found")

    def test_auth_failure_is_structured_and_throttled(self):
        env = dict(os.environ, WEREAD_COOKIE="", WEREAD_COOKIE_FILE="")
        cmd = [sys.executable, CLI, "--state", self.path, "--json", "--no-browser-cookie", "doctor"]
        first = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 2)
        self.assertTrue(json.loads(first.stdout)["notify_user"])
        second = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.assertFalse(
            json.loads(second.stdout)["notify_user"],
            "a second auth failure inside the window must not re-alert the user",
        )

    def test_version_flag(self):
        proc = subprocess.run([sys.executable, CLI, "--version"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(wm.__version__, proc.stdout)

    def test_defer_ack_and_no_mark_are_mutually_exclusive(self):
        proc = self.run_cli("check", "--defer-ack", "--no-mark")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not allowed with", proc.stderr)


if __name__ == "__main__":
    unittest.main()


class TestClearKeywords(unittest.TestCase):
    ACCOUNTS = [{"name": "豆包", "book_id": "MP_WXS_1"}]

    def test_bare_readd_preserves_filters(self):
        state = make_state(MP_WXS_1={"name": "豆包", "keywords": ["Agent"]})
        with mock.patch.object(wm, "get_accounts", return_value=self.ACCOUNTS):
            wm.add_monitor("c", state, "豆包", [], 20)
        self.assertEqual(state["monitors"]["MP_WXS_1"]["keywords"], ["Agent"])

    def test_clear_keywords_drops_filters(self):
        state = make_state(MP_WXS_1={"name": "豆包", "keywords": ["Agent", "编程"]})
        with mock.patch.object(wm, "get_accounts", return_value=self.ACCOUNTS):
            wm.add_monitor("c", state, "豆包", [], 20, clear_keywords=True)
        self.assertEqual(state["monitors"]["MP_WXS_1"]["keywords"], [])

    def test_clear_keywords_does_not_touch_seen(self):
        state = make_state(MP_WXS_1={"name": "豆包", "keywords": ["Agent"], "seen": ["x", "y"]})
        with mock.patch.object(wm, "get_accounts", return_value=self.ACCOUNTS):
            wm.add_monitor("c", state, "豆包", [], 20, clear_keywords=True)
        self.assertEqual(state["monitors"]["MP_WXS_1"]["seen"], ["x", "y"])


class TestScanToSignIn(unittest.TestCase):
    """The scan flow must never require the user to touch a cookie value."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_succeeds_once_the_scan_lands(self):
        attempts = {"n": 0}

        def cookie_appears(preferred=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise wm.AuthError("no session yet")
            return "wr_skey=abc"

        with mock.patch.object(wm, "extract_browser_cookie", side_effect=cookie_appears), mock.patch.object(
            wm, "get_accounts", return_value=[{"name": "豆包", "book_id": "MP_WXS_1"}]
        ), mock.patch.object(wm, "webbrowser") as browser, mock.patch.object(wm.time, "sleep"):
            result = wm.wait_for_login(timeout=60, poll_seconds=0)

        browser.open.assert_called_once_with(wm.WEREAD_HOST)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["followed_accounts"], 1)
        self.assertEqual(result["attempts"], 3)

    def test_result_never_contains_the_cookie(self):
        with mock.patch.object(wm, "extract_browser_cookie", return_value="wr_skey=SECRET"), mock.patch.object(
            wm, "get_accounts", return_value=[]
        ), mock.patch.object(wm, "webbrowser"):
            result = wm.wait_for_login(timeout=1, poll_seconds=0)
        self.assertNotIn("SECRET", json.dumps(result), "the session must never reach the transcript")

    def test_times_out_with_actionable_message(self):
        with mock.patch.object(wm, "extract_browser_cookie", side_effect=wm.AuthError("no session")), mock.patch.object(
            wm, "webbrowser"
        ), mock.patch.object(wm.time, "sleep"):
            with self.assertRaises(wm.AuthError) as ctx:
                wm.wait_for_login(timeout=0, poll_seconds=0)
        self.assertIn("scan with WeChat", str(ctx.exception))

    def test_no_open_leaves_the_browser_alone(self):
        with mock.patch.object(wm, "extract_browser_cookie", return_value="c"), mock.patch.object(
            wm, "get_accounts", return_value=[]
        ), mock.patch.object(wm, "webbrowser") as browser:
            result = wm.wait_for_login(timeout=1, poll_seconds=0, open_browser=False)
        browser.open.assert_not_called()
        self.assertFalse(result["browser_opened"])

    def test_saved_cookie_file_is_owner_only(self):
        target = os.path.join(self.dir, "nested", "weread.cookie")
        with mock.patch.object(wm, "extract_browser_cookie", return_value="wr_skey=abc"), mock.patch.object(
            wm, "get_accounts", return_value=[]
        ), mock.patch.object(wm, "webbrowser"):
            result = wm.wait_for_login(timeout=1, poll_seconds=0, save_to=target)
        self.assertEqual(result["cookie_file"], target)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "wr_skey=abc")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode) & 0o077, 0)

    def test_save_cookie_file_overwrites_without_widening_permissions(self):
        target = os.path.join(self.dir, "weread.cookie")
        with open(target, "w") as f:
            f.write("stale")
        os.chmod(target, 0o644)
        wm.save_cookie_file(target, "fresh=1")
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "fresh=1")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode) & 0o077, 0)

    def test_browser_preference_is_honoured(self):
        fake_bc = mock.MagicMock()
        fake_bc.chrome.side_effect = AssertionError("chrome must not be tried when edge is requested")
        fake_bc.edge.return_value = [mock.Mock(name_="x")]
        cookie_item = mock.Mock()
        cookie_item.name, cookie_item.value = "wr_skey", "abc"
        fake_bc.edge.return_value = [cookie_item]
        with mock.patch.dict(sys.modules, {"browser_cookie3": fake_bc}):
            self.assertEqual(wm.extract_browser_cookie("edge"), "wr_skey=abc")

    def test_missing_browser_cookie3_says_how_to_install_it(self):
        with mock.patch.dict(sys.modules, {"browser_cookie3": None}):
            with self.assertRaises(wm.AuthError) as ctx:
                wm.extract_browser_cookie()
        self.assertIn("pip install browser-cookie3", str(ctx.exception))

    def test_auth_failure_next_action_points_at_login_not_devtools(self):
        state = make_state()
        payload = wm.auth_warning_payload(state, "expired", 20)
        self.assertIn("login", payload["next_action"])
        self.assertNotIn("DevTools", payload["next_action"].replace("DevTools.", ""))
