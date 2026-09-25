import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CorpusDB, DomainError


class CorpusFlowTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g = self.db.add_guideline("v1", "独立标注")
        self.batch = self.db.create_batch("测试批次", self.g)
        self.item1 = self.db.add_item(self.batch, 1, "这个版本很快。")
        self.item2 = self.db.add_item(self.batch, 2, "没有明显变化。")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_full_annotation_disagreement_adjudication_freeze_flow(self):
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        with self.assertRaisesRegex(DomainError, "分歧"):
            self.db.freeze_batch(self.batch, self.mgr)
        self.db.adjudicate(self.item1, "正向", "速度描述构成明确正向倾向", self.arb)
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertIsNotNone(result["metrics"]["pairwise_agreement"])
        exported = self.db.export_gold(self.batch)
        self.assertEqual(2, len(exported["records"]))
        self.assertEqual("adjudication", exported["records"][0]["source"])

    def test_answer_isolation_and_role_validation(self):
        self.db.assign(self.item1, self.a1)
        self.db.assign(self.item1, self.a2)
        self.db.add_discussion(self.item1, self.a2, "我认为是正向", True)
        secret = self.db.get_item_for_user(self.item1, self.a1)
        self.assertTrue(secret["discussions"][0]["hidden"])
        self.db.submit_annotation(self.item1, self.a1, "负向")
        visible = self.db.get_item_for_user(self.item1, self.a1)
        self.assertFalse(visible["discussions"][0].get("hidden", False))
        with self.assertRaisesRegex(DomainError, "标注员"):
            self.db.assign(self.item2, self.arb)

    def _annotate_both_items(self):
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")

    def _pending_v2_revision(self):
        self._annotate_both_items()
        v2 = self.db.add_guideline("v2", "增加反讽标签")
        rid = self.db.submit_revision(self.batch, self.mgr, "补充反讽判定", v2)
        # 待启用期间：提交与仲裁仍按旧版 v1
        self.db.submit_annotation(self.item1, self.a1, "负向", "改判")
        self.assertEqual(
            self.db.get_item_for_user(self.item1, self.a1)["guideline_version"], "v1")
        self.db.adjudicate(self.item1, "负向", "旧版仲裁理由足够长", self.arb)
        self.assertEqual(1, self.db.conn.execute(
            "SELECT COUNT(*) FROM adjudications WHERE guideline_id=?", (self.g,)).fetchone()[0])
        # 每个批次只能有一份待启用指南
        v3 = self.db.add_guideline("v3", "再细化")
        with self.assertRaisesRegex(DomainError, "待启用"):
            self.db.submit_revision(self.batch, self.mgr, "再次换版", v3)
        # 非管理员不能登记
        with self.assertRaisesRegex(DomainError, "管理员"):
            self.db.submit_revision(self.batch, self.arb, "x", v3)
        return rid, v2

    def test_pending_revision_keeps_old_standard(self):
        rid, v2 = self._pending_v2_revision()
        self.assertTrue(rid and v2)

    def test_activation_resets_submitted_and_allows_unsubmitted_on_new(self):
        rid, v2 = self._pending_v2_revision()
        # 再加一条尚未提交的条目：启用后应直接按新版
        item3 = self.db.add_item(self.batch, 3, "可真“快”啊。")
        self.db.assign(item3, self.a1)
        result = self.db.activate_revision(self.batch, rid, self.mgr)
        self.assertEqual(v2, result["next_guideline_id"])
        self.assertEqual(self.g, result["previous_guideline_id"])
        self.assertTrue(result["activated_at"])
        # 已有标注回到待复核
        statuses = {r["annotator_id"]: r["status"] for r in self.db.conn.execute(
            "SELECT annotator_id,status FROM assignments WHERE item_id=?", (self.item1,))}
        self.assertEqual("review", statuses[self.a1])
        self.assertEqual("review", statuses[self.a2])
        # 未提交条目仍为 assigned，直接用新版
        self.assertEqual("assigned", self.db.conn.execute(
            "SELECT status FROM assignments WHERE item_id=? AND annotator_id=?", (item3, self.a1)
        ).fetchone()["status"])
        view3 = self.db.get_item_for_user(item3, self.a1)
        self.assertEqual("v2", view3["guideline_version"])
        # 复核者看到新版指南，并保留旧版答案作参考，但讨论答案仍未揭晓
        view1 = self.db.get_item_for_user(self.item1, self.a1)
        self.assertEqual("v2", view1["guideline_version"])
        self.assertEqual("review", view1["assignment_status"])
        self.assertIsNone(view1["own_annotation"])
        self.assertEqual("负向", view1["prior_annotation"]["label"])
        self.assertEqual("v1", view1["prior_annotation"]["guideline_version"])

    def test_old_adjudication_not_counted_after_activation(self):
        rid, v2 = self._pending_v2_revision()
        self.db.activate_revision(self.batch, rid, self.mgr)
        # 旧仲裁行仍保留
        self.assertEqual(1, self.db.conn.execute(
            "SELECT COUNT(*) FROM adjudications WHERE item_id=? AND guideline_id=?",
            (self.item1, self.g)).fetchone()[0])
        # 复核者按新版重新提交，制造新版分歧：旧版仲裁不能压住它
        self.db.submit_annotation(self.item1, self.a1, "中性", "v2复核")
        self.db.submit_annotation(self.item1, self.a2, "正向", "v2复核")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        self.db.adjudicate(self.item1, "中性", "按v2反讽口径复核后取中性", self.arb)
        self.db.freeze_batch(self.batch, self.mgr)
        gold = self.db.export_gold(self.batch)
        self.assertEqual("v2", gold["guideline_version"])
        self.assertTrue(all(r["guideline_version"] == "v2" for r in gold["records"]))
        self.assertEqual("adjudication", gold["records"][0]["source"])
        self.assertEqual("consensus", gold["records"][1]["source"])

    def test_frozen_batch_keeps_original_conclusion(self):
        self._annotate_both_items()
        self.db.adjudicate(self.item1, "正向", "旧版下的最终判定", self.arb)
        self.db.freeze_batch(self.batch, self.mgr)
        v2 = self.db.add_guideline("v2", "增加反讽标签")
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.db.submit_revision(self.batch, self.mgr, "想改", v2)
        rid = self.db.submit_revision(self._new_fresh_batch(), self.mgr, "另一批次", v2)
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.db.activate_revision(self.batch, rid, self.mgr)
        # 冻结金标准仍写明旧版与来源
        gold = self.db.export_gold(self.batch)
        self.assertEqual("v1", gold["guideline_version"])
        self.assertEqual("adjudication", gold["records"][0]["source"])

    def _new_fresh_batch(self):
        b = self.db.create_batch("另一批", self.g)
        self.db.add_item(b, 1, "另一条。")
        return b

    def test_summary_shows_pending_counts_and_before_after(self):
        rid, v2 = self._pending_v2_revision()
        summary = self.db.batch_summary(self.batch)
        self.assertEqual("v1", summary["guideline_version"])
        self.assertEqual(0, summary["pending"]["annotations"])
        self.assertEqual("v1", summary["pending_revision"]["previous_version"])
        self.assertEqual("v2", summary["pending_revision"]["next_version"])
        self.db.activate_revision(self.batch, rid, self.mgr)
        summary = self.db.batch_summary(self.batch)
        self.assertEqual("v2", summary["guideline_version"])
        self.assertEqual(4, summary["pending"]["reviews"])
        self.assertEqual(4, summary["pending"]["annotations"])
        self.assertIsNone(summary["pending_revision"])
        self.assertEqual("activated", summary["revisions"][0]["status"])


if __name__ == "__main__":
    unittest.main()
