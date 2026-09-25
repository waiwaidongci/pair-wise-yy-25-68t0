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

    def test_guideline_change_reopens_review_and_keeps_frozen_batch(self):
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.db.adjudicate(self.item1, "正向", "速度描述构成明确正向倾向", self.arb)
        g2 = self.db.add_guideline("v2", "新增 混合 标签")
        change = self.db.propose_guideline_change(self.batch, g2, "引入混合标签", self.mgr)
        # 未启用：提交与仲裁仍按旧版
        self.assertEqual("v1", self.db.get_item_for_user(self.item2, self.a1)["guideline_version"])
        status = self.db.guideline_status(self.batch)
        self.assertEqual("v1", status["current_guideline"]["version"])
        self.assertEqual("v2", status["pending_change"]["guideline_version"])
        self.assertEqual(0, status["pending_review"])
        with self.assertRaisesRegex(DomainError, "待启用"):
            self.db.propose_guideline_change(self.batch, g2, "重复提交", self.mgr)
        result = self.db.activate_guideline_change(change, self.mgr)
        self.assertEqual(4, result["reopened"])
        self.assertEqual(1, result["adjudications_discarded"])
        self.assertEqual("v1", result["from_guideline"]["version"])
        self.assertEqual("v2", result["to_guideline"]["version"])
        status = self.db.guideline_status(self.batch)
        self.assertEqual("v2", status["current_guideline"]["version"])
        self.assertIsNone(status["pending_change"])
        self.assertEqual(4, status["pending_review"])
        self.assertEqual(1, len(status["history"]))
        # 旧标注与旧仲裁不再计入
        self.assertEqual([], self.db.disagreements(self.batch))
        with self.assertRaisesRegex(DomainError, "缺少"):
            self.db.freeze_batch(self.batch, self.mgr)
        # 未提交条目直接用新版
        item3 = self.db.add_item(self.batch, 3, "新增的条目。")
        self.db.assign(item3, self.a1)
        self.db.assign(item3, self.a2)
        self.assertEqual("v2", self.db.get_item_for_user(item3, self.a1)["guideline_version"])
        # 按新版重新标注并冻结
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "混合")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.db.submit_annotation(item3, self.a1, "混合")
        self.db.submit_annotation(item3, self.a2, "混合")
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        self.db.adjudicate(self.item1, "混合", "新版指南下应判为混合", self.arb)
        self.db.freeze_batch(self.batch, self.mgr)
        exported = self.db.export_gold(self.batch)
        self.assertEqual("v2", exported["guideline"]["version"])
        self.assertEqual({"v2"}, {r["guideline_version"] for r in exported["records"]})
        self.assertEqual("adjudication", exported["records"][0]["source"])
        # 冻结批次保持原结论
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.db.propose_guideline_change(self.batch, self.g, "冻结后回退", self.mgr)


if __name__ == "__main__":
    unittest.main()
