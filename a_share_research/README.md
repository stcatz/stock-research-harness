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
- 行情新鲜度与候选自身交易日校验、完整报告分页回读；
- 固定强弱组/题材成员、T+1/T+5/T+20 研究反馈和追加式预期登记；
- 保留全市场现价横截面与完整特色池，区分当前观察、历史累计反馈和过期催化；
- 日报顶部自动生成下一交易日计划卡：具体名单、检查点、支持/反方条件及研究动作；盘中观察仍由人工完成；
- 单条件 R0/R1 影子排序对照（效果尚未验证，正式排序保持 R0）；
- DeepSeek Harness 原生 Cordis 工具适配器；
- 无券商、账户、订单或自动交易能力。

## 安装与测试

```bash
cd ~/ai/stock/a_share_research
uv sync --extra market
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

读取长报告时，按返回的 `next_offset` 继续请求，直至 `truncated=false`，保留页面首尾空白。本机导出可添加 `artifact-read --complete`；每日 wrapper 已采用完整导出。研究反馈、日历输入、预期登记及实验边界见 [研究反馈、市场观察与计划卡](docs/RESEARCH_FEEDBACK.md)。

## 采集真实 A 股快照

研究引擎运行时仍然只读取冻结 snapshot；联网只发生在显式的 `collect-snapshot` 或 `provider-probe` 命令中。`collect-snapshot` 把研究员维护的政策、公告、题材和候选 seed，与选定 provider 的结构化市场数据合并，验证后原子发布到 `data/normalized/<snapshot_id>/snapshot.json`。默认 provider 仍是 BaoStock；显式选择 `hithink` 时会使用同花顺 Financial API。

先复制 [research_seed.example.json](config/research_seed.example.json)，删除 `example_notice`，并把所有合成名称、URL、结论和人工复核项换成你已核验的真实内容。示例文件和仍含 `example.invalid`、`EXAMPLE_ONLY`、`[合成示例]` 等标记的副本会被采集器拒绝；每条 evidence 必须包含独立的 `published_at`、`effective_at`、`available_at`、`retrieved_at` 与 `as_of`，seed 也不能预填行情或 `market_evidence_refs`。候选身份必须满足例如 `symbol=600000` 对应 `security_id=CN.SH.600000`。

```bash
cd ~/ai/stock/a_share_research
uv sync --extra market

uv run a-share-research \
  --workspace ~/ai/stock \
  collect-snapshot \
  --seed-json ~/ai/stock/data/seeds/cn-research-seed.json \
  --provider baostock \
  --snapshot-id "cn-$(date +%Y%m%d)-manual-v1"
```

BaoStock 没有不可变的 first-seen/vintage 语义，因此这些快照固定标记为 `RECONSTRUCTED_NON_PIT`。生产 CLI 始终使用采集时的系统时钟，不提供人为覆盖 `retrieved_at` 的参数。命令不会自动抓取巨潮或政策正文；官方证据仍由研究员在 seed 中提供并负责授权与准确性。

### 使用 HiThink Financial API

HiThink 接入位于采集层，不改变研究引擎和 DSH 工具。API Key 唯一允许的入口是环境变量 `HITHINK_FINANCE_API_KEY`；不要把它放进 seed、命令行、plist、仓库或报告。先做一次显式联网探针，再采集：

```bash
cd ~/ai/stock/a_share_research
uv sync
read -s HITHINK_FINANCE_API_KEY
export HITHINK_FINANCE_API_KEY

uv run a-share-research provider-probe \
  --provider hithink \
  --symbol 600519.SH

uv run a-share-research \
  --workspace ~/ai/stock \
  collect-snapshot \
  --seed-json ~/ai/stock/data/seeds/cn-research-seed.json \
  --provider hithink \
  --snapshot-id "cn-$(date +%Y%m%d)-hithink-v1"
```

`provider-probe` 只返回 provider、endpoint、request ID、抓取时间、响应哈希和 opaque raw artifact ID，不回显价格、响应正文或 Key。`doctor` 仍完全离线。

HiThink 模式会采集：

- 候选股和上证综指、深证成指、创业板指的不复权日线；
- 全市场上涨、下跌、平盘、无成交家数和成交额；
- 涨停、跌停、炸板池及候选命中；
- 对完整涨停池的 `+` 分隔原因标签做重复聚类；剔除“中报增长/扭亏”等非题材业绩标签后，同一标签至少命中 3 只涨停股时，作为 `continue_research` 市场线索写入快照和日报；
- 候选最新估值快照；
- 候选最近两个可对齐年度的利润表、资产负债表和现金流量表字段。

所有 provider 缺失值都保留为 `UNKNOWN`，财务三表只在相同 `period_end` 上连接；供应商给出的涨停原因及其重复标签聚类只是 `provider_derived_unverified`，不能替代公司公告，也不会自动晋升为正式研究题材。HiThink 的结构化数据始终标为 `structured_market`，不会满足“至少一条官方证据”的门槛。

成功响应会以不可覆盖方式保存到仓库外的私有审计目录。默认位置为：

- macOS：`~/Library/Application Support/stock-research-harness/raw/cn/hithink`
- Linux：`${XDG_DATA_HOME:-~/.local/share}/stock-research-harness/raw/cn/hithink`

可用 `--raw-store-root /absolute/private/path` 或 `STOCK_RESEARCH_RAW_STORE` 覆盖；解析后的目录如果位于源码仓库或运行 workspace 内会被拒绝。raw store 保存响应正文只为本地审计，不会进入 snapshot、报告、DSH session 或 Git。

HiThink 没有公开不可变 first-seen/vintage 契约，最新估值也不能冒充历史估值。因此即使启用原始响应审计，快照仍是 `RECONSTRUCTED_NON_PIT`，不能用于宣称严格历史 PIT 回放。

## 每日完整报告与 macOS 调度

仓库根目录的 wrapper 会严格按“采集真实快照 → 使用显式 snapshot ID 运行 → 读取完整 report artifact”执行，不依赖 DSH，也不会退回 demo：

```bash
bash ~/ai/stock/scripts/run_cn_daily.sh \
  --root ~/ai/stock \
  --seed-json ~/ai/stock/data/seeds/cn-research-seed.json \
  --provider hithink \
  --snapshot-id "cn-$(date +%Y%m%d)-daily-v1" \
  --top-n 9
```

在 macOS 上可安装工作日 20:30 的 LaunchAgent。默认只写 plist；只有显式传入 `--load` 才会加载：

```bash
bash ~/ai/stock/scripts/install_cn_launchd.sh \
  --root /Users/yourname/ai/stock \
  --seed-json /Users/yourname/ai/stock/data/seeds/cn-research-seed.json \
  --provider hithink \
  --load
```

wrapper 只接受本次新建的 snapshot；若显式或自动生成的 `decision_at` 早于实际抓取时间，会在研究运行前失败。`--provider hithink` 时，调用环境必须已经包含 `HITHINK_FINANCE_API_KEY`。macOS LaunchAgent 不读取 `~/.zshrc`，安装器也不会把 Key 写进 plist；应由你自己的 Keychain/登录会话秘密注入机制把该变量提供给 launchd，否则任务会安全失败。日志写入 `<root>/.runtime/logs/`。日报调度属于研究引擎，关闭 DSH 后仍会继续运行。

## DeepSeek Harness

插件位于 `adapter-pkg/`，只注册：

- `cn_research_run`
- `cn_artifact_read`

插件不连接数据商、不读取 SQLite、不进行财务计算、不调用券商。开发时可用 `--patch` 临时挂载；验证稳定后再作为独立 bundle 安装。具体命令见 `adapter-pkg/README.md`。

## 当前边界

`run`、`artifact-read` 和 DSH 适配器仍是离线的；只有显式的 `collect-snapshot` 和 `provider-probe` 会联网。BaoStock 模式只补候选和三只宽基指数日线；HiThink 模式增加全市场宽度、特色池、估值、年度三表和供应商标签聚类线索，但仍不自动抓取官方公告、不把未经核验的聚类线索晋升为正式题材，也不提供严格 PIT 或数据再分发授权。上游根仓库有 MIT LICENSE，但其 Python 子项目元数据和实际数据访问授权需要分别核对；本项目不复制上游 SDK 代码，只调用公开 REST 合同。任何代码许可证都不自动授予数据的商业使用或再分发权。

本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。
