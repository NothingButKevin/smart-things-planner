from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


things = load_module("things_server_for_test", ROOT / "mcp" / "things_server.py")
gmail = load_module("gmail_server_for_test", ROOT / "mcp" / "gmail_server.py")


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def raw_message(
    message_id: str,
    *,
    subject: str = "Subject",
    body: str = "Body",
    mime_type: str = "text/plain",
) -> dict:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["INBOX", "CATEGORY_UPDATES"],
        "snippet": "A deliberately vague snippet",
        "payload": {
            "mimeType": mime_type,
            "headers": [
                {"name": "From", "value": "Course Staff <staff@example.edu>"},
                {"name": "To", "value": "student@example.edu"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 21 Sep 2026 12:00:00 -0700"},
                {"name": "Message-ID", "value": f"<{message_id}@example.edu>"},
            ],
            "body": {"data": encoded(body)},
        },
    }


def requested_id(args: tuple[str, ...]) -> str:
    params_index = args.index("--params") + 1
    return str(json.loads(args[params_index])["id"])


class ThingsServerTests(unittest.TestCase):
    def test_workload_weights_and_missing_tag_default(self):
        self.assertEqual(things._workload_summary(["🍅🍅"])["workload_weight"], 2)
        missing = things._workload_summary(["ordinary"])
        self.assertEqual(missing["workload_weight"], 1)
        self.assertEqual(missing["effective_workload_tag"], "🍅")

    def test_multiple_workload_tags_use_largest(self):
        summary = things._workload_summary(["🍅", "☀️", "ordinary"])
        self.assertEqual(summary["workload_weight"], 5)
        self.assertIn("multiple", summary["workload_issue"])

    def test_scheduled_workload_is_filtered_and_aggregated(self):
        rows = [
            {
                "id": "a",
                "title": "first",
                "status": "open",
                "activation_date": "2026-09-22T07:00:00.000Z",
                "due_date": None,
                "area": "A",
                "project": "",
                "tag_names": "🍅🍅",
            },
            {
                "id": "b",
                "title": "second",
                "status": "open",
                "activation_date": "2026-09-22T18:00:00.000Z",
                "due_date": None,
                "area": "A",
                "project": "",
                "tag_names": "",
            },
            {
                "id": "c",
                "title": "outside",
                "status": "open",
                "activation_date": "2026-10-01T07:00:00.000Z",
                "due_date": None,
                "area": "A",
                "project": "",
                "tag_names": "☀️",
            },
        ]
        with patch.object(things, "_read_open_todos", return_value=rows):
            result = things._list_open_todos("2026-09-22", "2026-09-28", False)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["daily_workload"], {"2026-09-22": 3})

    def test_rejects_reversed_date_window(self):
        with self.assertRaises(ValueError):
            things._list_open_todos("2026-09-28", "2026-09-22", False)


class GmailServerTests(unittest.TestCase):
    def setUp(self):
        gmail._get_raw_message_cached.cache_clear()
        gmail._account_email.cache_clear()

    def test_account_aware_message_url(self):
        url = gmail._message_web_url("thread-id", "person+test@example.com")
        self.assertEqual(
            url,
            "https://mail.google.com/mail/u/?authuser=person%2Btest%40example.com#all/thread-id",
        )

    def test_html_cleanup_removes_scripts_and_keeps_action_link(self):
        payload = raw_message(
            "html",
            body=(
                "<html><head><style>.x{display:none}</style></head>"
                "<body><h1>Welcome</h1><script>steal()</script>"
                "<p>Read the syllabus <a href='https://example.edu/syllabus'>here</a>.</p>"
                "</body></html>"
            ),
            mime_type="text/html",
        )["payload"]
        text = gmail._message_body(payload)
        self.assertIn("Welcome", text)
        self.assertIn("https://example.edu/syllabus", text)
        self.assertNotIn("display:none", text)
        self.assertNotIn("steal()", text)

    def test_cached_message_is_fetched_only_once(self):
        with patch.object(gmail, "_run_gws", return_value=raw_message("one")) as run:
            first = gmail._get_raw_message_cached("one")
            second = gmail._get_raw_message_cached("one")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(gmail._get_raw_message_cached.cache_info().hits, 1)

    def test_cache_is_bounded_to_512_messages(self):
        def fake_run(*args):
            return raw_message(requested_id(args))

        with patch.object(gmail, "_run_gws", side_effect=fake_run):
            for index in range(513):
                gmail._get_raw_message_cached(f"message-{index}")
        info = gmail._get_raw_message_cached.cache_info()
        self.assertEqual(info.currsize, 512)
        self.assertEqual(info.maxsize, 512)

    def test_search_prefetches_concurrently_and_preserves_order(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_run(*args):
            nonlocal active, max_active
            if "list" in args:
                return {
                    "messages": [{"id": f"m{index}"} for index in range(8)],
                    "nextPageToken": "next-page",
                }
            if "getProfile" in args:
                return {"emailAddress": "student@example.edu"}
            message_id = requested_id(args)
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return raw_message(message_id)

        with patch.object(gmail, "_run_gws", side_effect=fake_run):
            result = gmail._search_messages("after:2026/9/1", 8)
        self.assertTrue(result["complete"])
        self.assertEqual([item["id"] for item in result["messages"]], [f"m{i}" for i in range(8)])
        self.assertEqual(result["next_page_token"], "next-page")
        self.assertGreater(max_active, 1)

    def test_search_prefetch_makes_batch_body_read_all_cache_hits(self):
        message_gets = 0

        def fake_run(*args):
            nonlocal message_gets
            if "list" in args:
                return {"messages": [{"id": "a"}, {"id": "b"}]}
            if "getProfile" in args:
                return {"emailAddress": "student@example.edu"}
            message_gets += 1
            return raw_message(
                requested_id(args),
                subject="Welcome to the class!: CMPSC 16 - PROBLEM SOLVING I",
                body=(
                    "Before the first lecture, read the Syllabus and FAQ. "
                    "Post an introduction Note on Piazza for 30% of HW1. "
                    "The introduction is due October 11 EOD."
                ),
            )

        with patch.object(gmail, "_run_gws", side_effect=fake_run):
            search = gmail._search_messages("after:2026/9/1", 2)
            body_result = gmail._get_messages(["a", "b"])
        self.assertTrue(search["prefetched_full_messages"])
        self.assertEqual(message_gets, 2)
        self.assertEqual(body_result["cache"]["hits"], 2)
        self.assertEqual(body_result["cache"]["misses"], 0)
        self.assertIn("Syllabus and FAQ", body_result["messages"][0]["body"])
        self.assertIn("October 11 EOD", body_result["messages"][0]["body"])

    def test_bulk_read_isolates_one_message_failure(self):
        rows = [raw_message("good"), None]
        failures = [{"id": "bad", "error": "temporary failure"}]
        cache = {"hits": 1, "misses": 1, "current_size": 1, "max_size": 512}
        with (
            patch.object(gmail, "_prefetch_messages", return_value=(rows, failures, cache)),
            patch.object(gmail, "_account_email", return_value="student@example.edu"),
        ):
            result = gmail._get_messages(["good", "bad"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["fetch_failures"][0]["id"], "bad")

    def test_bulk_read_rejects_more_than_twenty_messages(self):
        with self.assertRaises(ValueError):
            gmail._get_messages([f"m{i}" for i in range(21)])

    def test_body_reports_truncation_and_total_length(self):
        raw = raw_message("long", body="x" * 1500)
        result = gmail._message_with_body(raw, "student@example.edu", 1000)
        self.assertTrue(result["body_truncated"])
        self.assertEqual(result["body_chars"], 1500)
        self.assertEqual(len(result["body"]), 1000)

    def test_fresh_server_module_starts_with_empty_message_cache(self):
        with patch.object(gmail, "_run_gws", return_value=raw_message("one")):
            gmail._get_raw_message_cached("one")
        fresh = load_module(
            "gmail_server_fresh_process_test", ROOT / "mcp" / "gmail_server.py"
        )
        self.assertEqual(fresh._get_raw_message_cached.cache_info().currsize, 0)


if __name__ == "__main__":
    unittest.main()
