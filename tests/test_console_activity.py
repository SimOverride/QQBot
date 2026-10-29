"""活动阅读视图的分页、聚合与历史准确性。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admin.console_activity import activity_detail, activity_page
from plugins.assistant.memory_activity import durable_activity, emit, recent


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "data/memory-activity/99"
        self.folder.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, number, kind="memory_changed", **kwargs):
        value = {
            "id": f"{number:032x}",
            "created": number,
            "bot": 99,
            "kind": kind,
            "notes": ["a" * 32],
            **kwargs,
        }
        (self.folder / (value["id"] + ".json")).write_text(json.dumps(value), encoding="utf-8")
        return value["id"]

    def test_filter_before_paging_and_new_insert_preserves_cursor(self):
        self.event(1)
        self.event(2)
        self.event(3, "processing_completed", result="no_durable_facts")
        self.event(4, "recall")
        first = activity_page(self.root, limit=1)
        self.assertEqual(first["items"][0]["id"], f"{2:032x}")
        self.event(5)
        second = activity_page(self.root, first["next_cursor"], limit=1)
        self.assertEqual(second["items"][0]["id"], f"{1:032x}")
        self.assertFalse(second["has_more"])

    def test_completion_merges_saved_events_and_keeps_exact_snapshot(self):
        self.event(
            1,
            changes=[
                {
                    "id": "a" * 32,
                    "title": "饮品偏好",
                    "subject": 2,
                    "before": "喜欢茶",
                    "after": "喜欢咖啡",
                    "sources": [],
                }
            ],
        )
        self.event(2, category="diary", notes=["b" * 32])
        ident = self.event(
            3, "processing_completed", result="saved", change_event_ids=[f"{1:032x}"]
        )
        with patch(
            "plugins.assistant.vault.Vault.get", side_effect=AssertionError("不得读取当前正文")
        ):
            page = activity_page(self.root)
            detail = activity_detail(self.root, 99, ident)
        self.assertEqual(len(page["items"]), 1)
        self.assertIn("饮品偏好", page["items"][0]["title"])
        self.assertEqual(detail["changes"][0]["before"], "喜欢茶")
        self.assertEqual(detail["changes"][0]["after"], "喜欢咖啡")

    def test_legacy_does_not_present_current_value_as_history(self):
        ident = self.event(1)
        detail = activity_detail(self.root, 99, ident)
        self.assertFalse(detail["changes"][0]["historical"])
        self.assertIsNone(detail["changes"][0]["after"])
        self.assertIn("无法可靠还原", detail["reasons"][0])

    def test_same_note_and_close_time_do_not_imply_same_operation(self):
        self.event(1, changes=[{"id": "a" * 32, "before": None, "after": "喜欢茶"}])
        self.event(2, "processing_completed", result="saved")
        page = activity_page(self.root)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["items"][0]["title"].startswith("旧记录："))

    def test_state_and_fixed_changes_visible_without_body_change(self):
        for number, attributes, title in (
            (1, {"before_state": "active", "after_state": "archived"}, "归档"),
            (2, {"before_state": "archived", "after_state": "active"}, "恢复"),
            (3, {"before_fixed": False, "after_fixed": True}, "固定"),
            (4, {"before_fixed": True, "after_fixed": False}, "解除固定"),
        ):
            ident = self.event(
                number,
                changes=[
                    {
                        "id": "a" * 32,
                        "title": "饮品偏好",
                        "before": "喜欢茶",
                        "after": "喜欢茶",
                        **attributes,
                    }
                ],
            )
            detail = activity_detail(self.root, 99, ident)
            self.assertIn(title, detail["title"])
            self.assertTrue(detail["reasons"])
        self.assertEqual(len(activity_page(self.root)["items"]), 4)

    def test_internal_and_unchanged_hidden_failure_remains(self):
        self.event(1, internal=True)
        self.event(2, changes=[{"before": "茶", "after": "茶"}])
        self.event(3, "processing_failed", reason="invalid_evidence_or_output")
        page = activity_page(self.root)
        self.assertEqual(len(page["items"]), 1)
        self.assertIn("失败", page["items"][0]["title"])

    def test_invalid_cursor_and_cross_account_detail_rejected(self):
        ident = self.event(1)
        with self.assertRaises(ValueError):
            activity_page(self.root, "invalid")
        with self.assertRaises(ValueError):
            activity_detail(self.root, 100, ident)

    def test_activity_cache_isolated_and_invalidates_on_replacement(self):
        ident = self.event(1)
        first = recent(self.root)
        first[0]["notes"].append("b" * 32)
        self.assertEqual(len(recent(self.root)[0]["notes"]), 1)
        target = self.folder / (ident + ".json")
        replacement = self.folder / "replacement.pending"
        item = json.loads(target.read_text(encoding="utf-8"))
        item["kind"] = "processing_failed"
        replacement.write_text(json.dumps(item), encoding="utf-8")
        replacement.replace(target)
        self.assertEqual(recent(self.root)[0]["kind"], "processing_failed")
        target.unlink()
        self.assertEqual(recent(self.root), [])

    def test_changes_and_diagnostics_have_independent_retention_limits(self):
        for number in range(1, 206):
            self.event(number)
        for number in range(206, 411):
            self.event(number, "recall")
        emit(self.root, 99, "processing_completed", result="no_durable_facts")
        items = recent(self.root, limit=None)
        self.assertEqual(sum(durable_activity(item) for item in items), 200)
        self.assertEqual(sum(not durable_activity(item) for item in items), 200)
        emit(
            self.root,
            99,
            "memory_changed",
            notes=["a" * 32],
            changes=[{"before": "茶", "after": "咖啡"}],
        )
        items = recent(self.root, limit=None)
        self.assertEqual(sum(durable_activity(item) for item in items), 200)
        self.assertEqual(sum(not durable_activity(item) for item in items), 200)


if __name__ == "__main__":
    unittest.main()
