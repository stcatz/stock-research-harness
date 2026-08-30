# A 股题材研究引擎（方法 v1.1）

这个项目把题材研究流程变成可重复的 `Snapshot -> Evidence Gates -> ResearchPacket -> Report` 管线。Python 引擎是正式 artifact 的唯一作者；DeepSeek Harness 只负责自然语言入口、触发工具和读取已生成的 artifact。

## 已实现

- `decision_at`、`available_at`、`retrieved_at`、`as_of` 时间校验；
- 官方事件证据和同期结构化市场证据双门槛；
- “大、新、多、久、准”分别记录，不合成上涨概率；
- “排除 / 继续研究 / 观察”三种研究状态；
- 反方解释、证伪条件、下一催化、数据缺口和人工复核项；
- SQLite 运行索引与包含排除样本的决策记录；
- 不可变 run 目录、文件哈希和稳定 manifest hash；
- 硬门槛与软优先级分离的 `research_priority`，以及结构化 `research_gaps`；
- 不含正反 thesis 和最终裁决的 `fact_packet.json`，供真正独立的反方审查；
- 横截面估值分位；行业与历史分位在缺少 PIT 可比集时明确为 `UNKNOWN`；
- T+5/T+20 相对中证 800 的不可变 outcome sidecar 和按可用时间过滤的历史校准；
- 跨冻结快照的供应商历史事实漂移审计；
- 可无损拼接并校验整段哈希的 artifact 分页读取；
- Markdown/JSON 报告；
- DeepSeek Harness 原生 Cordis 工具适配器和显式联网的每日研究 Harness；
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
  "max_chars": 12000,
  "cursor": 0
}' | uv run a-share-research artifact-read --request-json -
```

可读区段为 `summary`、`report`、`manifest`、`packet` 和 `facts`。`facts` 是独立反方专用的
thesis-blind 事实包。返回值包含 `next_cursor`、`total_chars` 和 `content_sha256`；只要
`next_cursor` 非空，就把它作为下一次 `cursor`，最终按顺序拼接并确认所有页的总长度与哈希一致。
单页结果不能冒充完整报告。

结果回填不会改写历史 artifact。下面示例记录 T+5 相对中证 800 的观察，并在一个明确的评估时点
汇总；收益率使用小数，例如 `0.031` 表示 3.1%：

```bash
printf '%s' '{
  "schema_version":"0.1", "market":"CN", "run_id":"cn-...",
  "candidate_id":"theme-id:CN.SH.600000", "symbol":"600000.SH",
  "horizon_trading_days":5,
  "observed_at":"2026-08-24T15:00:00+08:00",
  "available_at":"2026-08-24T15:05:00+08:00",
  "candidate_return":0.031, "benchmark_return":0.012,
  "benchmark_symbol":"000906.SH",
  "source_url":"https://licensed-provider.example/receipt/123",
  "source_document_id":"provider-receipt-123"
}' | uv run a-share-research outcome-record --request-json -

printf '%s' '{
  "schema_version":"0.1", "market":"CN", "run_id":"cn-run-...",
  "evaluation_at":"2026-08-24T16:00:00+08:00"
}' | uv run a-share-research outcome-summary --request-json -
```

`candidate_id` 在 symbol 于本次运行中唯一时可以省略；同一股票跨多个题材出现时必须显式提供，
否则命令拒绝回填，避免把结果记到错误的题材判断上。

日常 Harness 不再要求人工逐条计算：新快照固定采集中证 800，并保留 31 个活跃交易日的
不复权收盘窗口。`outcome-settle-snapshot` 只用冻结快照寻找研究基准日后的精确第 5/20 个交易日，
自动写入同一不可变 sidecar；基准缺失、候选停牌或窗口不足时保持 pending。最近 90 天仍未完成
T+20 的旧候选会进入只读行情 watchlist，即使已从当天 seed 删除，也会继续采集到可结算为止；超过
90 天仍缺行情的槽位记为 expired，不会让每日 pending 队列无限增长。

`outcome-history` 只返回 `available_at <= evaluation_at` 的旧观察。`research-history` 返回候选连续
`continue_research` 次数和距上次状态/缺口进展的天数；14 天触发降级复核，30 天触发关闭复核，
但不会改写 canonical 状态。`audit-drift` 接受显式的
`before_snapshot_id` 和 `after_snapshot_id`，比较相同实体、指标和观察期的历史值，并把不可变 receipt
写入 `data/audit/cn/provider-drift/`。重建型数据仅重采时间变化且未跨越 `decision_at` 时为 info；
只有改变 PIT 资格才是 critical。

CLI 运行根目录可通过 `--workspace` 或 `STOCK_RESEARCH_WORKSPACE` 指定。DSH 工具结果只返回相对路径、短摘要和 opaque ID。

## 采集真实 A 股快照

研究引擎运行时仍然只读取冻结 snapshot；联网只发生在显式的 `collect-snapshot` 或 `provider-probe` 命令中。`collect-snapshot` 把研究员维护的政策、公告、题材和候选 seed，与选定 provider 的结构化市场数据合并，验证后原子发布到 `data/normalized/<snapshot_id>/snapshot.json`。默认 provider 仍是 BaoStock；显式选择 `hithink` 时会使用同花顺 Financial API。

先复制 [research_seed.example.json](config/research_seed.example.json)，删除 `example_notice`，并把所有合成名称、URL、结论和人工复核项换成你已核验的真实内容。示例文件和仍含 `example.invalid`、`EXAMPLE_ONLY`、`[合成示例]` 等标记的副本会被采集器拒绝；每条 evidence 必须包含独立的 `published_at`、`effective_at`、`available_at`、`retrieved_at` 与 `as_of`，seed 也不能预填行情或 `market_evidence_refs`。候选身份必须满足例如 `symbol=600000` 对应 `security_id=CN.SH.600000`。

V2 候选可提供完整 `opportunity_profile`（新信息、经济影响、预期差、市场定价、正式催化）和至少
三环的候选级 `impact_chain`。只有引用冻结且在 `decision_at` 前可用的 Evidence ID 才计分；普通
`data_gaps` 会进入调查队列，但不再自动把候选压到 `continue_research`。官方公告全文可用
`evidence.document` 冻结，字段为 `source_document_id`、`media_type`、`extraction_method`、`text`、
`text_sha256`；正文进入分页 facts/packet，不在报告正文整段展开。经济影响只有在正式正文或冻结的
结构化财务事实支持下才得分，并须写明金额分子、收入/利润/现金流等分母、计算公式和可校验 ratio；
摘要或媒体口径即使填满字段也不能获得经济量级分。当前仓库仍不自动下载巨潮 PDF，正文的授权、
获取和抽取准确性由接入方负责。

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

- 候选股和上证综指、深证成指、创业板指、中证 800 的不复权日线；
- 全市场上涨、下跌、平盘、无成交家数和成交额；
- 涨停、跌停、炸板池及候选命中；
- 候选最新估值快照；
- 候选最近两个可对齐年度的利润表、资产负债表和现金流量表字段。

所有 provider 缺失值都保留为 `UNKNOWN`，财务三表只在相同 `period_end` 上连接；供应商给出的涨停原因只是 `provider_derived_unverified`，不能替代公司公告。HiThink 的结构化数据始终标为 `structured_market`，不会满足“至少一条官方证据”的门槛。

成功响应会以不可覆盖方式保存到仓库外的私有审计目录。默认位置为：

- macOS：`~/Library/Application Support/stock-research-harness/raw/cn/hithink`
- Linux：`${XDG_DATA_HOME:-~/.local/share}/stock-research-harness/raw/cn/hithink`

可用 `--raw-store-root /absolute/private/path` 或 `STOCK_RESEARCH_RAW_STORE` 覆盖；解析后的目录如果位于源码仓库或运行 workspace 内会被拒绝。raw store 保存响应正文只为本地审计，不会进入 snapshot、报告、DSH session 或 Git。

HiThink 没有公开不可变 first-seen/vintage 契约，最新估值也不能冒充历史估值。因此即使启用原始响应审计，快照仍是 `RECONSTRUCTED_NON_PIT`，不能用于宣称严格历史 PIT 回放。

## 每日 canonical 报告与 macOS 调度

仓库根目录的 wrapper 会严格按“采集真实快照 → 使用显式 snapshot ID 运行 → 读取第一份可校验的
report 分页”执行，不依赖 DSH，也不会退回 demo：

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

### 显式联网的机会发现 Harness

联网模型不放进离线 `run`。`run_cn_harness_daily.sh` 先执行 canonical 流程和冻结快照自动结算，再做
两次隔离调用：

1. 反方只可分页读取 `facts`，看不到 bull thesis、原反方或最终决策；
2. 裁判分页读取报告，加载 outcome history 与候选老化历史，把第一步 JSON 当作不可信输入，并可
   联网查询 `decision_at` 之前的一手来源；模型只输出结构化 JSON，经过强校验后由确定性渲染器生成
   最终 Top 3 简报。

```bash
bash ~/ai/stock/scripts/run_cn_harness_daily.sh \
  --root ~/ai/stock \
  --seed-json ~/ai/stock/data/seeds/cn-research-seed.json \
  --provider hithink \
  --dsh-bin "$(command -v dsh)" \
  --bear-profile skeptic \
  --judge-profile headless \
  --bear-model-id your-bear-model-version \
  --judge-model-id your-judge-model-version
```

反方与裁判 profile 必须不同；默认分别为 `web` 和 `headless`。`--profile` 仅作为旧命令的
`--judge-profile` 别名。模型 ID 是审计标签，不会改变 DSH profile 的实际模型配置；若提供了两个明确
版本，它们也必须不同。请在调度前用 DSH 配置检查确认两个 profile 确实指向预期模型。

联网输出写入 `.runtime/harness/cn/<snapshot_id>/`，不会改写 canonical artifact；其中包含
`canonical-runner-receipt.json`、完整分页校验回执 `artifact-integrity.json`、`settlement.json`、
冻结的 `outcome-history.json` / `research-history.json`、规范化反方 JSON、
`final-judgment.json` 和瘦身机会简报。
`harness-manifest.json` 固定代码版本、DSH 二进制哈希、两个 profile、声明的模型版本、prompt 模板、
模型原始/规范化输出哈希。模型新发现的主题或标的
只能标为“尚未冻结 / `continue_research`”，经官方来源核验并进入下一份 snapshot 后才可参与正式门槛。
工作日 20:45 的模板位于
`scripts/launchd/com.stcatz.stock-research.cn-harness-daily.plist.example`；模板不包含 API Key，安装前必须
替换绝对路径并通过受控的秘密注入机制提供 collector 所需凭据。

## DeepSeek Harness

插件位于 `adapter-pkg/`，只注册四个业务级只读/研究工具：

- `cn_research_run`
- `cn_artifact_read`
- `cn_outcome_history`
- `cn_research_history`

插件不连接数据商、不读取 SQLite、不进行财务计算、不调用券商。开发时可用 `--patch` 临时挂载；验证稳定后再作为独立 bundle 安装。具体命令见 `adapter-pkg/README.md`。

## 当前边界

`run`、`artifact-read` 和四个 DSH 业务工具仍是离线的；联网只发生在显式 collector/provider probe
以及独立的 Harness 模型会话中。BaoStock 模式只补候选和四只基准指数日线；HiThink 模式增加全市场
宽度、特色池、估值和年度三表，但仍不提供官方公告自动冻结、严格 PIT 或数据再分发授权。Harness
能够发现线索，却不会把网页内容自动提升为 canonical 事实。上游根仓库有 MIT LICENSE，但其 Python
子项目元数据和实际数据访问授权需要分别核对；本项目不复制上游 SDK 代码，只调用公开 REST 合同。
任何代码许可证都不自动授予数据的商业使用或再分发权。

本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。
