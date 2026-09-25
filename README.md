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
- `GET /api/batches/{id}/disagreements`、`GET /api/batches/{id}/consistency`、`GET /api/batches/{id}/summary`
- `POST /api/batches/{id}/revisions/submit`、`POST /api/batches/{id}/revisions/{rid}/activate`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。

## 指南换版

每个批次最多挂一份“待启用指南”：

- `POST /api/batches/{id}/revisions/submit`：管理员登记一份待启用指南（可用已存在的 `guideline_id`，或直接给 `version`+`rules` 新建），必须填 `note` 换版说明，记录登记时刻。待启用期间，标注提交与争议仲裁继续按批次当前（旧）版本判定，各自带 `guideline_id`。
- `POST /api/batches/{id}/revisions/{rid}/activate`：记录启用时刻与操作人，并把批次切到新版：已有旧版标注的分配回到 `review`（待复核，需按新版重新提交）；旧版仲裁记录保留但不再计入分歧与冻结判定；尚未提交的条目不受影响，提交时直接按新版。
- 冻结批次拒绝登记与启用换版，保持原结论；冻结金标准的导出在批次级和逐条记录上都写明 `guideline_id` / `guideline_version` 与 `source`（`consensus` 或 `adjudication`）。
- `GET /api/batches/{id}/summary` 返回待处理数量（待提交/待复核、其中待复核、待仲裁、已提交）以及完整的换版前后记录（版本号、说明、登记/启用时刻与操作人）；`/api/state` 中每个批次也带这些计数与待启用指南。

复核者重新打开条目时，页面展示新版指南，并把其上一版答案作为 `prior_annotation`（含旧版版本号）供参考；在按新版提交自己的标注前，讨论区含答案内容仍不可见。
