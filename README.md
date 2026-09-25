# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、仲裁、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

## 启动

```bash
python app.py
```

默认地址 <http://127.0.0.1:8112>，默认数据库为 `corpus.db`。首次启动会写入两位标注员、一位仲裁员和一个含分歧的示例批次。

```bash
PORT=9002 CORPUS_DB=/tmp/corpus.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：分配、标注、发现分歧、阻止提前冻结、仲裁、计算一致性、冻结和导出；另一条测试验证提交答案前后讨论可见性变化，以及错误角色不能领取标注任务。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/annotations`、`POST /api/adjudications`
- `GET /api/items/{id}?user_id=`
- `GET /api/batches/{id}/disagreements`
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`
- `POST /api/batches/{id}/guideline-changes`、`POST /api/guideline-changes/{id}/activate`
- `GET /api/batches/{id}/guideline`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧；冻结后不能修改标注，导出结果来自不可变的 `gold_records`，并写明每条记录判定所用的指南版本与来源（一致通过或仲裁）。

## 指南换版

批次转入标注后仍可换版：管理员用 `POST /api/batches/{id}/guideline-changes` 提交一份待启用指南（每批次同时最多一份，登记说明），启用前提交与仲裁继续按旧版判定。`POST /api/guideline-changes/{id}/activate` 启用后：旧版标注回到待复核（任务重新打开）、旧仲裁不再计入、未提交条目直接按新版标注；冻结批次保持原结论，不能换版。`GET /api/batches/{id}/guideline` 返回当前指南、待启用申请、换版历史与待处理数量（待复核/未提交），页面“指南换版”区可直接操作。
