# A 股题材研究引擎 v0.1

这个项目把题材研究流程变成可重复的 `Snapshot -> Evidence Gates -> ResearchPacket -> Report` 管线。Python 引擎是正式 artifact 的唯一作者；DeepSeek Harness 只负责自然语言入口、触发工具和读取已生成的 artifact。

## 已实现

- `decision_at`、`available_at`、`retrieved_at`、`as_of` 时间校验；
- 官方事件证据和同期结构化市场证据双门槛；
- “大、新、多、久、准”分别记录，不合成上涨概率；
- “排除 / 继续研究 / 观察”三种研究状态；
- 反方解释、证伪条件、下一催化、数据缺口和人工复核项；
- SQLite 运行索引与包含排除样本的决策记录；
- 不可变 run 目录、文件哈希和稳定 manifest hash；
- Markdown/JSON 报告；
- DeepSeek Harness 原生 Cordis 工具适配器；
- 无券商、账户、订单或自动交易能力。

## 安装与测试

```bash
cd ~/ai/stock/a_share_research
uv sync
uv run python -m unittest discover -s tests -v
```

运行显式合成 fixture：

```bash
uv run a-share-research demo
```

输出默认写入：

```text
artifacts/runs/<run_id>/
reports/daily/<date>-<run_hash>.md
data/stock_research.sqlite3
```

这些路径均不进入 Git。

## 版本化 CLI 合约

初始化与健康检查：

```bash
uv run a-share-research init
uv run a-share-research doctor
```

通过 stdin 运行研究。`daily_report`、`theme_research` 和 `stock_research` 是 `run`
请求中的 `workflow` 值，不是独立子命令。

使用合成 fixture 生成日报：

```bash
printf '%s' '{
  "schema_version": "0.1",
  "workflow": "daily_report",
  "decision_at": "2026-08-16T08:30:00+08:00",
  "snapshot": {"selector": "demo"},
  "top_n": 5
}' | uv run a-share-research run --request-json -
```

使用最新真实快照生成日报：

```bash
printf '%s' '{
  "schema_version": "0.1",
  "workflow": "daily_report",
  "decision_at": "2026-08-16T08:30:00+08:00",
  "snapshot": {"selector": "latest"},
  "top_n": 5
}' | uv run a-share-research run --request-json -
```

研究特定题材：

```bash
printf '%s' '{
  "schema_version": "0.1",
  "workflow": "theme_research",
  "decision_at": "2026-08-16T08:30:00+08:00",
  "subject": "算力",
  "snapshot": {"selector": "id", "snapshot_id": "cn-example-20260816"},
  "top_n": 5
}' | uv run a-share-research run --request-json -
```

研究特定标的：

```bash
printf '%s' '{
  "schema_version": "0.1",
  "workflow": "stock_research",
  "decision_at": "2026-08-16T08:30:00+08:00",
  "symbol": "600000.SH",
  "snapshot": {"selector": "latest"},
  "top_n": 5
}' | uv run a-share-research run --request-json -
```

真实 snapshot 必须符合 `schemas/snapshot.schema.json`，并放在：

```text
<workspace>/data/normalized/<snapshot_id>/snapshot.json
```

然后使用 `snapshot.selector=latest` 或 `snapshot.selector=id`。选择 `id` 时必须同时提供
`snapshot.snapshot_id`，其他 selector 不允许携带该字段。今天补抓的旧数据必须标记为
`RECONSTRUCTED_NON_PIT`，不能伪装成严格历史快照。

读取 artifact：

```bash
printf '%s' '{
  "artifact_id": "cn-artifact-...",
  "section": "report",
  "max_chars": 12000
}' | uv run a-share-research artifact-read --request-json -
```

CLI 运行根目录可通过 `--workspace` 或 `STOCK_RESEARCH_WORKSPACE` 指定。DSH 工具结果只返回相对路径、短摘要和 opaque ID。

## DeepSeek Harness

插件位于 `adapter-pkg/`，只注册：

- `cn_research_run`
- `cn_artifact_read`

插件不连接数据商、不读取 SQLite、不进行财务计算、不调用券商。开发时可用 `--patch` 临时挂载；验证稳定后再作为独立 bundle 安装。具体命令见 `adapter-pkg/README.md`。

## 当前边界

v0.1 默认不联网，也不抓取全市场。它先验证 schema、证据门槛、报告质量和 DSH 适配是否可靠。接入巨潮、交易所、Tushare 或商业行情前，需要逐一确认授权、时间语义和快照策略。

本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。
