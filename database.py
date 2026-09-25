from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


class CorpusDB:
    """A small multi-annotator corpus governance service."""

    def __init__(self, path: str = "corpus.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('annotator','arbitrator','manager'))
            );
            CREATE TABLE IF NOT EXISTS guidelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version TEXT NOT NULL UNIQUE,
              rules TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','annotating','frozen')),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL,
              text TEXT NOT NULL,
              UNIQUE(batch_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'assigned' CHECK(status IN ('assigned','submitted','review')),
              UNIQUE(item_id, annotator_id)
            );
            CREATE TABLE IF NOT EXISTS annotations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(item_id, annotator_id, guideline_id)
            );
            CREATE TABLE IF NOT EXISTS adjudications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              final_label TEXT NOT NULL,
              reason TEXT NOT NULL,
              arbitrator_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(item_id, guideline_id)
            );
            CREATE TABLE IF NOT EXISTS guideline_revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              previous_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              next_guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              note TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','activated')),
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              activated_by INTEGER REFERENCES users(id),
              activated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_revision_pending_per_batch
              ON guideline_revisions(batch_id) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS discussions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              author_id INTEGER NOT NULL REFERENCES users(id),
              body TEXT NOT NULL,
              contains_answer INTEGER NOT NULL DEFAULT 0 CHECK(contains_answer IN (0,1)),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gold_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              source TEXT NOT NULL CHECK(source IN ('consensus','adjudication')),
              adjudication_id INTEGER REFERENCES adjudications(id),
              frozen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_freezes (
              batch_id INTEGER PRIMARY KEY REFERENCES batches(id),
              metrics_json TEXT NOT NULL,
              frozen_by INTEGER NOT NULL REFERENCES users(id),
              frozen_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        a1 = self.add_user("标注员甲", "annotator")
        a2 = self.add_user("标注员乙", "annotator")
        arb = self.add_user("仲裁员", "arbitrator")
        mgr = self.add_user("管理员", "manager")
        guideline = self.add_guideline("v1", "标签仅可为 正向/负向/中性；先独立标注，不得查看他人答案。")
        batch = self.create_batch("情感标注示例", guideline)
        item1 = self.add_item(batch, 1, "这个更新让工作流畅了很多。")
        item2 = self.add_item(batch, 2, "功能没有变化，但也没有明显问题。")
        self.assign(item1, a1)
        self.assign(item1, a2)
        self.assign(item2, a1)
        self.assign(item2, a2)
        self.submit_annotation(item1, a1, "正向", "整体表达积极")
        self.submit_annotation(item1, a2, "中性", "描述较克制")
        self.submit_annotation(item2, a1, "中性")
        self.submit_annotation(item2, a2, "中性")
        self.submit_revision(
            batch, mgr, "v2：反讽句式单独标记为“中性（反讽）”，并在备注中给出被反讽的词。",
            version="v2", rules="标签可为 正向/负向/中性/中性（反讽）；识别到反讽时必须备注被反讽的词。",
        )

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"annotator", "arbitrator", "manager"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def add_guideline(self, version: str, rules: str) -> int:
        if not version.strip() or not rules.strip():
            raise DomainError("指南版本和规则不能为空")
        with self.transaction():
            cur = self.conn.execute("INSERT INTO guidelines(version,rules) VALUES(?,?)", (version.strip(), rules.strip()))
        return int(cur.lastrowid)

    def create_batch(self, name: str, guideline_id: int) -> int:
        if not name.strip() or not self.conn.execute("SELECT 1 FROM guidelines WHERE id=? AND active=1", (guideline_id,)).fetchone():
            raise DomainError("批次名称或指南无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO batches(name,guideline_id,created_at) VALUES(?,?,?)",
                (name.strip(), guideline_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def submit_revision(
        self, batch_id: int, manager_id: int, note: str,
        guideline_id: int | None = None, version: str = "", rules: str = "",
    ) -> int:
        """每个批次登记一份待启用指南；启用前提交与仲裁仍按旧版。"""
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("冻结批次不能登记换版")
        if not note.strip():
            raise DomainError("换版说明不能为空")
        if self.conn.execute(
            "SELECT 1 FROM guideline_revisions WHERE batch_id=? AND status='pending'", (batch_id,)
        ).fetchone():
            raise DomainError("该批次已有待启用指南")
        if guideline_id:
            target = self.conn.execute(
                "SELECT 1 FROM guidelines WHERE id=? AND active=1", (guideline_id,)
            ).fetchone()
            if not target:
                raise DomainError("目标指南不存在或未启用")
        else:
            guideline_id = self.add_guideline(version, rules)
        if guideline_id == batch["guideline_id"]:
            raise DomainError("新指南不能与当前指南相同")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO guideline_revisions(batch_id,previous_guideline_id,next_guideline_id,"
                "note,status,created_by,created_at) VALUES(?,?,?,?, 'pending',?,?)",
                (batch_id, batch["guideline_id"], guideline_id, note.strip(), manager_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def activate_revision(self, batch_id: int, revision_id: int, manager_id: int) -> dict:
        """启用换版：已有标注回到待复核，旧仲裁不再计入，未提交条目直接用新版。"""
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        revision = self.conn.execute(
            "SELECT * FROM guideline_revisions WHERE id=? AND batch_id=?", (revision_id, batch_id)
        ).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("冻结批次保持原结论，不能启用换版")
        if not revision or revision["status"] != "pending":
            raise DomainError("待启用指南不存在或已经启用")
        activated_at = datetime.now().isoformat()
        previous_id = batch["guideline_id"]
        next_id = revision["next_guideline_id"]
        with self.transaction():
            # 已有（旧版）标注回到待复核：需要按新版重新提交
            self.conn.execute(
                "UPDATE assignments SET status='review' WHERE batch_id=? AND status='submitted' "
                "AND EXISTS (SELECT 1 FROM annotations a WHERE a.item_id=assignments.item_id "
                "AND a.annotator_id=assignments.annotator_id AND a.guideline_id=?)",
                (batch_id, previous_id),
            )
            self.conn.execute(
                "UPDATE guideline_revisions SET status='activated',activated_by=?,activated_at=? WHERE id=?",
                (manager_id, activated_at, revision_id),
            )
            self.conn.execute("UPDATE batches SET guideline_id=? WHERE id=?", (next_id, batch_id))
        return {
            "batch_id": batch_id, "revision_id": revision_id,
            "previous_guideline_id": previous_id, "next_guideline_id": next_id,
            "activated_at": activated_at,
        }

    def add_item(self, batch_id: int, ordinal: int, text: str) -> int:
        batch = self.conn.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] == "frozen":
            raise DomainError("批次不存在或已经冻结")
        if ordinal <= 0 or not text.strip():
            raise DomainError("序号必须大于0且文本不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO items(batch_id,ordinal,text) VALUES(?,?,?)", (batch_id, ordinal, text.strip()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("该批次序号已存在") from exc
        return int(cur.lastrowid)

    def assign(self, item_id: int, annotator_id: int) -> int:
        item = self.conn.execute("SELECT batch_id FROM items WHERE id=?", (item_id,)).fetchone()
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (annotator_id,)).fetchone()
        if not item or not user or user["role"] != "annotator":
            raise DomainError("条目不存在或用户不是标注员")
        with self.transaction():
            self.conn.execute("UPDATE batches SET status='annotating' WHERE id=? AND status='draft'", (item["batch_id"],))
            try:
                cur = self.conn.execute(
                    "INSERT INTO assignments(batch_id,item_id,annotator_id) VALUES(?,?,?)",
                    (item["batch_id"], item_id, annotator_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一标注员不能重复领取同一条目") from exc
        return int(cur.lastrowid)

    def submit_annotation(self, item_id: int, annotator_id: int, label: str, comment: str = "") -> int:
        if not label.strip():
            raise DomainError("标签不能为空")
        item = self.conn.execute(
            "SELECT i.*, b.guideline_id, b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        assignment = self.conn.execute(
            "SELECT * FROM assignments WHERE item_id=? AND annotator_id=?", (item_id, annotator_id)
        ).fetchone()
        if not item or not assignment:
            raise DomainError("只能提交已分配条目的标注")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能修改标注")
        with self.transaction():
            self.conn.execute(
                "DELETE FROM adjudications WHERE item_id=? AND guideline_id=?",
                (item_id, item["guideline_id"]),
            )
            try:
                cur = self.conn.execute(
                    "INSERT INTO annotations(item_id,annotator_id,guideline_id,label,comment,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, annotator_id, item["guideline_id"], label.strip(), comment.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                cur = self.conn.execute(
                    "UPDATE annotations SET label=?,comment=?,created_at=? WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (label.strip(), comment.strip(), datetime.now().isoformat(), item_id, annotator_id, item["guideline_id"]),
                )
                annotation_id = self.conn.execute(
                    "SELECT id FROM annotations WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (item_id, annotator_id, item["guideline_id"]),
                ).fetchone()["id"]
            else:
                annotation_id = int(cur.lastrowid)
            self.conn.execute("UPDATE assignments SET status='submitted' WHERE id=?", (assignment["id"],))
        return int(annotation_id)

    def add_discussion(self, item_id: int, author_id: int, body: str, contains_answer: bool = False) -> int:
        if not body.strip():
            raise DomainError("讨论内容不能为空")
        if not self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise DomainError("条目不存在")
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (author_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO discussions(item_id,author_id,body,contains_answer,created_at) VALUES(?,?,?,?,?)",
                (item_id, author_id, body.strip(), int(contains_answer), datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def get_item_for_user(self, item_id: int, user_id: int) -> dict:
        item = self.conn.execute(
            "SELECT i.id,i.batch_id,i.ordinal,i.text,b.guideline_id,g.version AS guideline_version,g.rules "
            "FROM items i JOIN batches b ON b.id=i.batch_id JOIN guidelines g ON g.id=b.guideline_id WHERE i.id=?",
            (item_id,),
        ).fetchone()
        if not item:
            raise DomainError("条目不存在")
        own = self.conn.execute(
            "SELECT id,label,comment,created_at,guideline_id FROM annotations "
            "WHERE item_id=? AND annotator_id=? AND guideline_id=?",
            (item_id, user_id, item["guideline_id"]),
        ).fetchone()
        prior = None
        if own is None:
            prior_row = self.conn.execute(
                "SELECT a.label,a.comment,g.version AS guideline_version FROM annotations a "
                "JOIN guidelines g ON g.id=a.guideline_id "
                "WHERE a.item_id=? AND a.annotator_id=? ORDER BY a.id DESC LIMIT 1",
                (item_id, user_id),
            ).fetchone()
            prior = dict(prior_row) if prior_row else None
        assignment = self.conn.execute(
            "SELECT status FROM assignments WHERE item_id=? AND annotator_id=?", (item_id, user_id)
        ).fetchone()
        revealed = own is not None
        discussions = []
        for row in self.conn.execute(
            "SELECT d.*,u.name FROM discussions d JOIN users u ON u.id=d.author_id WHERE d.item_id=? ORDER BY d.id", (item_id,)
        ).fetchall():
            if row["contains_answer"] and not revealed:
                discussions.append({"id": row["id"], "author": row["name"], "body": "提交自己的标注后才能查看此讨论", "hidden": True})
            else:
                discussions.append(dict(row))
        payload = dict(item)
        payload["own_annotation"] = dict(own) if own else None
        payload["prior_annotation"] = prior
        payload["assignment_status"] = assignment["status"] if assignment else None
        payload["discussions"] = discussions
        return payload

    def disagreements(self, batch_id: int) -> list[dict]:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        result = []
        for item in self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall():
            rows = self.conn.execute(
                "SELECT a.*,u.name FROM annotations a JOIN users u ON u.id=a.annotator_id "
                "WHERE a.item_id=? AND a.guideline_id=? ORDER BY a.id",
                (item["id"], batch["guideline_id"]),
            ).fetchall()
            labels = {row["label"] for row in rows}
            adj = self.conn.execute(
                "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?",
                (item["id"], batch["guideline_id"]),
            ).fetchone()
            if len(rows) >= 2 and len(labels) > 1 and not adj:
                result.append({
                    "item_id": item["id"], "ordinal": item["ordinal"], "text": item["text"],
                    "labels": [dict(row) for row in rows],
                })
        return result

    def adjudicate(self, item_id: int, final_label: str, reason: str, arbitrator_id: int) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (arbitrator_id,)).fetchone()
        item = self.conn.execute(
            "SELECT i.*,b.guideline_id,b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        if not item or not user or user["role"] != "arbitrator":
            raise DomainError("条目或仲裁员无效")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能重新仲裁")
        rows = self.conn.execute(
            "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?",
            (item_id, item["guideline_id"]),
        ).fetchall()
        if len(rows) < 2:
            raise DomainError("至少需要两份标注才能仲裁")
        if not final_label.strip() or len(reason.strip()) < 5:
            raise DomainError("最终标签必填，仲裁理由至少5个字符")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO adjudications(item_id,guideline_id,final_label,reason,arbitrator_id,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, item["guideline_id"], final_label.strip(), reason.strip(), arbitrator_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                cur = self.conn.execute(
                    "UPDATE adjudications SET final_label=?,reason=?,arbitrator_id=?,created_at=? "
                    "WHERE item_id=? AND guideline_id=?",
                    (final_label.strip(), reason.strip(), arbitrator_id, datetime.now().isoformat(), item_id, item["guideline_id"]),
                )
                adjudication_id = self.conn.execute(
                    "SELECT id FROM adjudications WHERE item_id=? AND guideline_id=?",
                    (item_id, item["guideline_id"]),
                ).fetchone()["id"]
            else:
                adjudication_id = int(cur.lastrowid)
        return int(adjudication_id)

    def consistency(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        items = self.conn.execute("SELECT id,ordinal FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        per_item, agreement_pairs, total_pairs = [], 0, 0
        label_totals: Counter[str] = Counter()
        all_annotation_count = 0
        for item in items:
            labels = [r["label"] for r in self.conn.execute(
                "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
            ).fetchall()]
            if len(labels) < 2:
                per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": None, "annotations": len(labels)})
                continue
            pairs = total = 0
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    total += 1
                    pairs += labels[i] == labels[j]
            agreement = pairs / total
            agreement_pairs += pairs
            total_pairs += total
            all_annotation_count += len(labels)
            label_totals.update(labels)
            per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": round(agreement, 4), "annotations": len(labels)})
        pairwise = agreement_pairs / total_pairs if total_pairs else None
        expected = sum((count / all_annotation_count) ** 2 for count in label_totals.values()) if all_annotation_count else None
        kappa = None
        if pairwise is not None and expected is not None and expected < 1:
            kappa = (pairwise - expected) / (1 - expected)
        return {
            "batch_id": batch_id,
            "items_with_multiple_annotations": total_pairs and sum(1 for row in per_item if row["agreement"] is not None),
            "pairwise_agreement": round(pairwise, 4) if pairwise is not None else None,
            "fleiss_kappa": round(kappa, 4) if kappa is not None else None,
            "items": per_item,
        }

    def freeze_batch(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("批次已冻结")
        items = self.conn.execute("SELECT id FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        if not items:
            raise DomainError("空批次不能冻结")
        disagreements = self.disagreements(batch_id)
        if disagreements:
            raise DomainError(f"仍有 {len(disagreements)} 条分歧未仲裁")
        missing = []
        for item in items:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
            ).fetchone()[0]
            if count < 2:
                missing.append(item["id"])
        if missing:
            raise DomainError(f"条目缺少至少两份标注: {missing}")
        metrics = self.consistency(batch_id)
        frozen_at = datetime.now().isoformat()
        with self.transaction():
            for item in items:
                labels = [r["label"] for r in self.conn.execute(
                    "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
                ).fetchall()]
                adj = self.conn.execute(
                    "SELECT * FROM adjudications WHERE item_id=? AND guideline_id=?",
                    (item["id"], batch["guideline_id"]),
                ).fetchone()
                if adj:
                    label, source, adj_id = adj["final_label"], "adjudication", adj["id"]
                else:
                    label, source, adj_id = labels[0], "consensus", None
                self.conn.execute(
                    "INSERT INTO gold_records(batch_id,item_id,guideline_id,label,source,adjudication_id,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, item["id"], batch["guideline_id"], label, source, adj_id, frozen_at),
                )
            self.conn.execute(
                "INSERT INTO batch_freezes(batch_id,metrics_json,frozen_by,frozen_at) VALUES(?,?,?,?)",
                (batch_id, json.dumps(metrics, ensure_ascii=False), manager_id, frozen_at),
            )
            self.conn.execute("UPDATE batches SET status='frozen' WHERE id=?", (batch_id,))
        return {"batch_id": batch_id, "metrics": metrics, "frozen_at": frozen_at}

    def export_gold(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] != "frozen":
            raise DomainError("只有已冻结批次可以导出金标准")
        freeze = self.conn.execute("SELECT * FROM batch_freezes WHERE batch_id=?", (batch_id,)).fetchone()
        rows = self.conn.execute(
            "SELECT g.item_id,i.ordinal,i.text,g.label,g.source,g.guideline_id,gv.version AS guideline_version,g.frozen_at "
            "FROM gold_records g JOIN items i ON i.id=g.item_id JOIN guidelines gv ON gv.id=g.guideline_id "
            "WHERE g.batch_id=? ORDER BY i.ordinal", (batch_id,)
        ).fetchall()
        current_version = self.conn.execute(
            "SELECT version FROM guidelines WHERE id=?", (batch["guideline_id"],)
        ).fetchone()["version"]
        return {
            "batch_id": batch_id, "batch_name": batch["name"], "frozen_at": freeze["frozen_at"],
            "guideline_id": batch["guideline_id"], "guideline_version": current_version,
            "metrics": json.loads(freeze["metrics_json"]), "records": [dict(row) for row in rows],
        }

    def batch_summary(self, batch_id: int) -> dict:
        batch = self.conn.execute(
            "SELECT b.*,g.version AS guideline_version FROM batches b "
            "JOIN guidelines g ON g.id=b.guideline_id WHERE b.id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        item_total = self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        assignment_total = self.conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        submitted_total = self.conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE batch_id=? AND status='submitted'", (batch_id,)
        ).fetchone()[0]
        pending_annotations = self.conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE batch_id=? AND status IN ('assigned','review')", (batch_id,)
        ).fetchone()[0]
        pending_reviews = self.conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE batch_id=? AND status='review'", (batch_id,)
        ).fetchone()[0]
        pending_arbitrations = len(self.disagreements(batch_id))
        revisions = []
        for r in self.conn.execute(
            "SELECT r.*,pg.version AS previous_version,ng.version AS next_version,"
            "uc.name AS created_by_name,ua.name AS activated_by_name "
            "FROM guideline_revisions r "
            "JOIN guidelines pg ON pg.id=r.previous_guideline_id "
            "JOIN guidelines ng ON ng.id=r.next_guideline_id "
            "JOIN users uc ON uc.id=r.created_by "
            "LEFT JOIN users ua ON ua.id=r.activated_by "
            "WHERE r.batch_id=? ORDER BY r.id", (batch_id,)
        ).fetchall():
            revisions.append(dict(r))
        pending_revision = next((r for r in revisions if r["status"] == "pending"), None)
        return {
            "batch_id": batch_id, "name": batch["name"], "status": batch["status"],
            "guideline_id": batch["guideline_id"], "guideline_version": batch["guideline_version"],
            "pending": {
                "items_total": item_total,
                "assignments_total": assignment_total,
                "annotations": pending_annotations,
                "reviews": pending_reviews,
                "arbitrations": pending_arbitrations,
                "submitted": submitted_total,
            },
            "pending_revision": pending_revision,
            "revisions": revisions,
        }

    def snapshot(self) -> dict:
        batches = []
        for row in self.conn.execute("SELECT * FROM batches ORDER BY id").fetchall():
            summary = self.batch_summary(row["id"])
            batches.append({**dict(row), "pending": summary["pending"],
                            "pending_revision": summary["pending_revision"],
                            "revision_count": len(summary["revisions"])})
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "guidelines": [dict(r) for r in self.conn.execute("SELECT * FROM guidelines ORDER BY id")],
            "batches": batches,
            "items": [dict(r) for r in self.conn.execute("SELECT * FROM items ORDER BY batch_id,ordinal")],
        }
