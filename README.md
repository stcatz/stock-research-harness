# Stock Research Harness

一个把离线市场快照转换为可审计研究报告的双市场投研工作区。

它由两个彼此隔离的项目组成：

- [A 股题材研究引擎](a_share_research/README.md)：强调政策与事件催化、题材强度、受益传导、市场确认和证伪条件。
- [美股研究引擎](us_equity_research/README.md)：强调官方披露、财务事实、确定性计算、正反论证和风险审查。

两个项目都可以直接通过 CLI 使用，也可以安装为 DeepSeek Harness 的薄插件。无论入口是什么，正式报告都只由 Python 研究引擎生成；DeepSeek Harness 不接触数据源、不改写报告，也不承担持久化调度。

> [!IMPORTANT]
> 当前版本是研究基础设施，不是股票预测器。仓库开箱即用的是合成 <code>demo</code>，仅用于安装和端到端验证。使用 <code>latest</code> 或 <code>id</code> 运行真实研究前，必须先准备符合 schema 的真实 snapshot。

> [!WARNING]
> 本项目不连接券商、不自动下单，也不输出买入、卖出、目标价、仓位或收益承诺。研究状态只有 <code>exclude</code>、<code>continue_research</code> 和 <code>observe</code>，它们表示研究优先级，不是交易信号。

## 目录

- [项目解决什么问题](#项目解决什么问题)
- [当前能力与边界](#当前能力与边界)
- [A 股与美股有什么不同](#a-股与美股有什么不同)
- [系统架构](#系统架构)
- [五分钟快速开始](#五分钟快速开始)
- [如何运行每日主题和个股研究](#如何运行每日主题和个股研究)
- [如何准备真实快照](#如何准备真实快照)
- [如何理解报告和审计产物](#如何理解报告和审计产物)
- [如何接入-deepseek-harness](#如何接入-deepseek-harness)
- [如何在远程-mac-运行](#如何在远程-mac-运行)
- [数据源和许可证策略](#数据源和许可证策略)
- [测试与质量检查](#测试与质量检查)
- [常见问题](#常见问题)
- [项目结构](#项目结构)
- [路线图](#路线图)
- [贡献与许可证](#贡献与许可证)

## 项目解决什么问题

AI 投研系统最容易出现的并不是文案不够漂亮，而是下面这些更基础的问题：

1. 使用了研究截止时点之后才出现的数据，形成时间穿越。
2. 把原始事实、确定性计算和模型观点混在一起，无法复核。
3. 数据缺失时擅自补数，最后给出看似完整但没有证据的结论。
4. 同一份研究无法重放，不知道当时用了哪个快照、哪些参数和哪版方法。
5. Agent 在对话里生成了结论，却没有留下可验证的报告、manifest 和输入快照。
6. A 股和美股共用一套模糊的数据模型，导致标的、时区、披露规则和方法论串线。

Stock Research Harness 把这些问题变成明确的工程约束：

- 研究只能从一个已固定的 snapshot 开始。
- 每次运行都必须给出带时区的 <code>decision_at</code>。
- <code>available_at</code> 晚于 <code>decision_at</code> 的信息不能进入当次判断。
- 事实、计算、观点、风险和缺口分层保存。
- 数值由确定性 Python 代码计算，模型只负责研究解释。
- 缺失值保留为 <code>UNKNOWN</code> 或 data gap，不自动脑补。
- 每次运行生成不可变 artifact、内容哈希和 SQLite 审计记录。
- A 股与美股使用不同的运行目录、数据库、报告、schema 和 DSH 工具。

## 当前能力与边界

### 已实现

- 两个独立的 Python 3.11+ 研究引擎。
- 版本化 JSON 请求契约。
- <code>daily_report</code>、<code>theme_research</code>、<code>stock_research</code> 三种工作流。
- <code>demo</code>、<code>latest</code>、<code>id</code> 三种 snapshot 选择方式。
- 带时区的研究截止时点和 PIT 时间门槛。
- 研究包、Markdown 报告、摘要、输入快照和 manifest 落盘。
- 基于哈希的完整性检查、复用和防篡改读取。
- 独立 SQLite 运行账本。
- 有大小上限的 artifact 读取接口。
- A 股和美股各自独立的 DeepSeek Harness 薄插件。
- 远程 Mac 同步脚本与 SSH 端口转发脚本。
- A 股“研究 seed + BaoStock 日线”的显式单次快照采集器。
- 美股 SEC submissions/companyfacts 的显式单次快照采集器，并可选导入经过许可声明的行情 JSON。
- 不依赖 DSH 的 CN 日报、US 验证 wrapper，以及 A 股 macOS <code>launchd</code> 示例。

### 尚未内置

- 可直接商用的数据供应商账号或 API key。
- 历史 point-in-time 数据仓库。
- A 股公告/政策全文自动发现与结构化、全市场行情宽度和严格 PIT 回放。
- 美股发行人 IR、官方宏观、transcript 和一致预期采集。
- 多市场常驻任务服务。当前只提供可审计的单次 wrapper 与 A 股 LaunchAgent 示例。
- 自动发送邮件、微信、Slack 等发布渠道。
- 券商连接、订单管理和实盘执行。

换句话说，当前仓库已经实现了“受控单次采集 → 可信快照 → 可审计报告”的纵向链路，但不会替你取得商业授权，也不等于完整生产数据平台。

## 核心设计原则

### 1. Evidence → Claim → Thesis

研究结论不是一段不透明的模型文本，而是一条可追溯链路：

1. <strong>Evidence</strong>：官方公告、结构化行情、财务事实或其他带来源和时间的证据。
2. <strong>Claim</strong>：可以被证据支持或反驳的具体判断。
3. <strong>Thesis</strong>：由多条 claim 组成的研究观点，同时保留反方证据、失效条件和数据缺口。

### 2. LLM 不负责算数

研究引擎中的财务比率、增长率、市值和估值派生项由 Python 计算。输出同时保存公式、输入 fact ID 和计算状态。模型可以解释这些结果，但不应在自然语言里重新计算并覆盖它们。

### 3. 显式 UNKNOWN

如果缺少财务事实、可靠时间戳或一手来源，系统会保留未知状态并降低研究可信度。完整但虚构的数字，比不完整但诚实的报告更危险。

### 4. Focus 不是交易建议

日报中的重点标的只是“下一步值得花研究时间的对象”。系统不会把研究优先级包装成买入建议，也不会给出目标仓位。

### 5. DeepSeek Harness 可拔掉

停掉或卸载 DeepSeek Harness 后，CLI、snapshot、报告、审计账本和外部定时任务仍应正常工作。这样可以避免把项目核心绑定在仍快速变化的 Harness API 上。

## A 股与美股有什么不同

| 维度 | A 股项目 | 美股项目 |
|---|---|---|
| 目录 | <code>a_share_research/</code> | <code>us_equity_research/</code> |
| 固定市场 | <code>CN</code> | <code>US</code> |
| 主要研究问题 | 题材是否够大、够新、够广、够久、时点够准 | 催化是否可验证、财务影响是否可计算、估值与风险是否匹配 |
| 主要证据 | 政策、公司公告、交易所披露、行业供需、市场确认 | SEC filings、公司 IR、官方宏观、结构化行情、财务事实 |
| 方法核心 | 大、新、多、久、准；受益传导；下一催化；证伪条件 | Evidence → Claim → Thesis；bull/bear/risk；确定性财务计算 |
| snapshot 路径 | <code>data/normalized/&lt;snapshot_id&gt;/snapshot.json</code> | <code>data/normalized/us/&lt;snapshot_id&gt;/snapshot.json</code> |
| SQLite | <code>data/stock_research.sqlite3</code> | <code>data/us_stock_research.sqlite3</code> |
| artifacts | <code>artifacts/runs/&lt;run_id&gt;/</code> | <code>artifacts/us/runs/&lt;run_id&gt;/</code> |
| 日报 | <code>reports/daily/</code> | <code>reports/us/daily/</code> |
| DSH 工具 | <code>cn_research_run</code> / <code>cn_artifact_read</code> | <code>us_research_run</code> / <code>us_artifact_read</code> |

两套引擎只共享仓库级规范，不共享运行时数据。不要把 A 股 snapshot 复制进美股目录，也不要在一个市场的请求里传入另一个市场的标的。

## 系统架构

~~~mermaid
flowchart LR
    User["研究员 / 外部调度器"] --> CLI["市场专属 CLI"]
    User --> DSH["DeepSeek Harness 会话"]

    DSH --> CNA["CN 薄适配器"]
    DSH --> USA["US 薄适配器"]
    CNA --> CNCLI["A 股 Python CLI"]
    USA --> USCLI["美股 Python CLI"]

    CLI --> CNCLI
    CLI --> USCLI

    CNS["CN snapshot store"] --> CNCLI
    USS["US snapshot store"] --> USCLI

    CNCLI --> CNG["CN 时间门槛与研究引擎"]
    USCLI --> USG["US 时间门槛、计算与研究引擎"]

    CNG --> CNAF["CN artifacts / reports / SQLite"]
    USG --> USAF["US artifacts / reports / SQLite"]

    CNAF --> Read["有上限的 artifact read"]
    USAF --> Read
    Read --> DSH
~~~

一次研究运行的实际顺序是：

1. CLI 校验 JSON 请求和市场。
2. snapshot loader 解析指定快照。
3. 时间门槛排除在 <code>decision_at</code> 之后才可见的事实。
4. 研究引擎生成结构化 research packet。
5. 确定性报告器生成 canonical Markdown 报告。
6. 系统写入 request、snapshot、packet、report、summary 和 manifest。
7. SQLite 记录运行、排除项和 artifact 索引。
8. DSH 或 CLI 只能通过 <code>artifact-read</code> 有界读取已生成内容。

### 为什么不把所有工具直接暴露给 Agent

如果让模型自由调用几十个数据源再临场拼报告，就很难保证：

- 相同输入能够重放。
- <code>decision_at</code> 不会被绕过。
- 数据源失败时不会静默换成不可靠来源。
- 最终数字确实来自同一版快照。

因此 DSH 只看到业务级工具，而不是 SEC、CNInfo、行情接口和 SQLite 等底层工具。

## 五分钟快速开始

### 前置条件

- macOS 或 Linux。
- Python 3.11 或更高版本。
- [uv](https://docs.astral.sh/uv/)。
- 如需 DSH 插件：Node.js 22+、npm，以及已安装的 DeepSeek Harness。
- 当前 DSH 适配器针对 <code>0.1.0-rc.6</code> 验证；升级 DSH 前请重新运行 adapter 测试。

### 克隆仓库

~~~bash
git clone https://github.com/stcatz/stock-research-harness.git
cd stock-research-harness
~~~

### 运行 A 股 demo

~~~bash
cd a_share_research
uv sync --frozen
uv run a-share-research doctor
uv run a-share-research demo
~~~

### 运行美股 demo

~~~bash
cd us_equity_research
uv sync --frozen
uv run us-equity-research doctor
uv run us-equity-research demo
~~~

成功时 CLI 会返回 JSON，其中应包含：

- <code>market</code>
- <code>workflow</code>
- <code>artifact_id</code>
- <code>snapshot_id</code>
- <code>data_mode</code>
- <code>pit_quality</code>
- <code>counts</code>
- <code>focus</code>
- <code>warnings</code> 和 <code>gaps</code>

demo 的 <code>data_mode</code> 必须是 <code>fixture</code>，<code>pit_quality</code> 必须是 <code>FIXTURE</code>。这说明安装成功，不说明任何股票值得关注。

## 如何运行每日主题和个股研究

两个 CLI 共享下面五个离线子命令：

| 命令 | 用途 | 是否访问网络 |
|---|---|---|
| <code>init</code> | 初始化运行目录和 SQLite | 否 |
| <code>doctor</code> | 检查工作区、fixture 和 snapshot 状态 | 否 |
| <code>demo</code> | 使用仓库内合成 fixture 跑通链路 | 否 |
| <code>run</code> | 运行一份版本化 JSON 研究请求 | 否 |
| <code>artifact-read</code> | 按 artifact ID 读取一个受限区段 | 否 |

此外，A 股提供联网命令 <code>collect-snapshot</code>，美股提供联网命令 <code>collect-sec-snapshot</code>。它们只负责构建并发布验证通过的不可变 snapshot；<code>run</code> 本身仍然不联网。

全局 <code>--workspace</code> 要放在子命令之前：

~~~bash
uv run a-share-research --workspace /absolute/runtime/path doctor
uv run us-equity-research --workspace /absolute/runtime/path doctor
~~~

也可以设置：

~~~bash
export STOCK_RESEARCH_WORKSPACE=/absolute/runtime/path
~~~

workspace 的默认值取决于入口：

- 直接运行 CLI 时，默认 workspace 是对应子项目目录。
- 通过 DSH adapter 运行时，默认 workspace 是两个子项目的父目录，也就是仓库根目录。

共享一个仓库根 workspace 是受支持的：CN 使用 <code>data/normalized/</code>、<code>data/stock_research.sqlite3</code> 和 <code>artifacts/runs/</code>；US 使用 <code>data/normalized/us/</code>、<code>data/us_stock_research.sqlite3</code> 和 <code>artifacts/us/runs/</code>，路径不会重叠。

如果你希望 CLI 和 DSH 看到同一批 snapshot 与 artifact，推荐把仓库根目录作为统一 workspace：

~~~bash
export STOCK_RESEARCH_WORKSPACE=/absolute/path/to/stock-research-harness

cd /absolute/path/to/stock-research-harness/a_share_research
uv run a-share-research doctor

cd /absolute/path/to/stock-research-harness/us_equity_research
uv run us-equity-research doctor
~~~

如果不设置该变量，前面的五分钟 demo 仍可正常运行，只是产物会分别写在各自子项目目录。

### 三种工作流

| workflow | 必填附加字段 | 禁止字段 | 适合场景 |
|---|---|---|---|
| <code>daily_report</code> | 无 | <code>subject</code>、<code>symbol</code> | 对整个快照做每日筛选 |
| <code>theme_research</code> | <code>subject</code> | <code>symbol</code> | 深入研究一个主题或事件 |
| <code>stock_research</code> | <code>symbol</code> | <code>subject</code> | 深入研究单一标的 |

### 通用请求字段

| 字段 | 规则 |
|---|---|
| <code>schema_version</code> | 当前必须为 <code>0.1</code> |
| <code>market</code> | 美股必须为 <code>US</code>；A 股 CLI 可省略，若提供只能为 <code>CN</code> |
| <code>workflow</code> | 三种工作流之一 |
| <code>decision_at</code> | 必须是带时区的 ISO-8601 时间 |
| <code>snapshot.selector</code> | <code>demo</code>、<code>latest</code> 或 <code>id</code> |
| <code>snapshot.snapshot_id</code> | CLI 在 <code>selector=id</code> 时必填，其他 selector 不得出现 |
| <code>top_n</code> | 1–20，默认 5 |

### A 股日报：显式 fixture

~~~bash
cd a_share_research
printf '%s' '{
  "schema_version": "0.1",
  "market": "CN",
  "workflow": "daily_report",
  "decision_at": "2026-08-16T08:30:00+08:00",
  "snapshot": {"selector": "demo"},
  "top_n": 5
}' | uv run a-share-research run --request-json -
~~~

### 美股日报：显式 fixture

~~~bash
cd us_equity_research
printf '%s' '{
  "schema_version": "0.1",
  "market": "US",
  "workflow": "daily_report",
  "decision_at": "2026-08-16T08:30:00-04:00",
  "snapshot": {"selector": "demo"},
  "top_n": 5
}' | uv run us-equity-research run --request-json -
~~~

### A 股主题研究

下面的请求形式有效，但 demo 中是否有与主题匹配的合成记录取决于 fixture。真实使用时应把 selector 改成 <code>latest</code> 或 <code>id</code>。

~~~bash
cd a_share_research
printf '%s' '{
  "schema_version": "0.1",
  "market": "CN",
  "workflow": "theme_research",
  "subject": "示例算力基础设施",
  "decision_at": "2026-08-16T08:30:00+08:00",
  "snapshot": {"selector": "demo"},
  "top_n": 10
}' | uv run a-share-research run --request-json -
~~~

### 美股个股研究

~~~bash
cd us_equity_research
printf '%s' '{
  "schema_version": "0.1",
  "market": "US",
  "workflow": "stock_research",
  "symbol": "DEMOA",
  "decision_at": "2026-08-16T08:30:00-04:00",
  "snapshot": {"selector": "demo"},
  "top_n": 5
}' | uv run us-equity-research run --request-json -
~~~

<code>DEMOA</code> 是 fixture 中的合成 ticker。真实 snapshot 中的美股 symbol 必须符合大写 ticker 规则，例如 <code>AAPL</code>、<code>BRK.B</code> 或 <code>RDS-A</code>。

### 使用 JSON 文件

生产脚本里推荐把请求保存成文件，避免 shell 引号问题：

~~~bash
uv run us-equity-research run --request-json /absolute/path/to/request.json
~~~

stdin 只能包含一个 JSON 对象；日志应写 stderr，stdout 保留给 canonical JSON。

### 读取报告

运行成功后，从结果中取得 <code>artifact_id</code>：

~~~bash
cd us_equity_research
printf '%s' '{
  "artifact_id": "YOUR_ARTIFACT_ID",
  "section": "report",
  "max_chars": 12000
}' | uv run us-equity-research artifact-read --request-json -
~~~

可读 section：

| section | 内容 |
|---|---|
| <code>summary</code> | 适合 UI 和 Agent 的短摘要 |
| <code>report</code> | canonical Markdown 报告 |
| <code>manifest</code> | 文件哈希、运行身份和完整性信息 |
| <code>packet</code> | 结构化研究包 |

如果不传 <code>section</code>，默认读取 <code>summary</code>。<code>max_chars</code> 范围是 500–20000，默认 12000。接口刻意限制返回大小，完整原始数据不会被塞进模型上下文。

## 如何准备真实快照

### 放置路径

A 股：

~~~text
a_share_research/
└── data/
    └── normalized/
        └── <snapshot_id>/
            └── snapshot.json
~~~

美股：

~~~text
us_equity_research/
└── data/
    └── normalized/
        └── us/
            └── <snapshot_id>/
                └── snapshot.json
~~~

如果使用自定义 <code>--workspace</code> 或 <code>STOCK_RESEARCH_WORKSPACE</code>，以上路径相对于该 workspace。直接运行 CLI 时默认是对应子项目目录；DSH adapter 默认是仓库根目录。请先用 <code>doctor</code> 确认当前进程实际看到的 snapshot 数量。

### snapshot 选择规则

| selector | 行为 | 默认仓库能否直接用 |
|---|---|---|
| <code>demo</code> | 只加载内置合成 fixture | 可以 |
| <code>latest</code> | 从已验证的真实快照中选择最新一份 | 不可以，需先写入快照 |
| <code>id</code> | 精确加载 <code>snapshot.snapshot_id</code> | 不可以，需先写入对应快照 |

仓库默认不附带真实 snapshot。因此在初始 clone 上直接使用 <code>latest</code> 会得到“no normalized snapshot found”错误，这是正确行为。

### 真实请求示例

~~~json
{
  "schema_version": "0.1",
  "market": "US",
  "workflow": "daily_report",
  "decision_at": "2026-08-17T08:30:00-04:00",
  "snapshot": {
    "selector": "id",
    "snapshot_id": "us-2026-08-17T120000Z"
  },
  "top_n": 10
}
~~~

### snapshot 最低语义

两边的 snapshot 都至少包含：

- <code>schema_version</code>
- <code>market</code>
- <code>snapshot_id</code>
- <code>data_mode</code>
- <code>pit_quality</code>
- <code>as_of</code>
- <code>retrieved_at</code>
- <code>market_context</code>
- <code>evidence</code>
- <code>themes</code>

美股还要求 <code>financial_facts</code>。完整定义请看：

- [A 股 snapshot schema](a_share_research/schemas/snapshot.schema.json)
- [美股 snapshot schema](us_equity_research/schemas/snapshot.schema.json)
- [A 股 run request schema](a_share_research/schemas/run-request.schema.json)
- [美股 run request schema](us_equity_research/schemas/run-request.schema.json)

### 时间字段怎么理解

| 字段 | 含义 |
|---|---|
| <code>published_at</code> | 来源声称的发布时间 |
| <code>effective_at</code> | 规则、政策或事实开始生效的时间 |
| <code>available_at</code> | 研究员最早能够获得该信息的时间 |
| <code>retrieved_at</code> | 本系统实际抓取或接收的时间 |
| <code>as_of</code> | 数据值对应的观察时点 |
| <code>decision_at</code> | 本次研究允许看到信息的截止时点 |

其中最重要的是 <code>available_at</code>。只有 <code>available_at &lt;= decision_at</code> 的证据才能进入当次研究。不要用 <code>published_at</code> 代替所有时间字段，也不要把财报期末日误当成市场首次可见时间。

### PIT 质量

当前契约接受：

- <code>P1</code>
- <code>P2</code>
- <code>P3</code>
- <code>RECONSTRUCTED_NON_PIT</code>
- <code>FIXTURE</code>

只有具有可靠 first-available 时间、原始载荷和本地快照证据的数据，才应该标成最高质量。今天补抓的旧数据应明确标为 <code>RECONSTRUCTED_NON_PIT</code>，不能冒充当时已保存的历史快照。<code>FIXTURE</code> 永远只用于测试。

## 如何理解报告和审计产物

### 每次运行会生成什么

A 股 artifact 目录：

~~~text
a_share_research/artifacts/runs/<run_id>/
~~~

美股 artifact 目录：

~~~text
us_equity_research/artifacts/us/runs/<run_id>/
~~~

一个完整运行通常包含：

| 文件 | 用途 |
|---|---|
| <code>request.json</code> | 规范化后的研究请求 |
| <code>snapshot.json</code> | 本次运行实际使用的冻结输入 |
| <code>research_packet.json</code> | 结构化事实、计算、论点、风险和缺口 |
| <code>report.md</code> | canonical 人类可读报告 |
| <code>summary.json</code> | 有界摘要与重点研究对象 |
| <code>manifest.json</code> | 文件哈希、运行身份和完整性元数据 |

日报另写入：

- A 股：<code>a_share_research/reports/daily/</code>
- 美股：<code>us_equity_research/reports/us/daily/</code>

### 三种研究状态

| 状态 | 含义 |
|---|---|
| <code>exclude</code> | 当前证据、时点或风险不支持继续占用研究资源 |
| <code>continue_research</code> | 有一定价值，但仍需要补证据或等待催化确认 |
| <code>observe</code> | 当前最值得持续跟踪和人工复核 |

它们不映射到卖出、持有或买入。

### A 股报告重点

A 股引擎关注：

- 题材的“大、新、多、久、准”。
- 政策或事件如何传导到行业和公司。
- 市场是否已经充分定价。
- 候选在产业链中的角色与受益纯度。
- 下一次可验证催化。
- 反方证据、失效条件和人工复核项。

方法卡见 [A 股题材方法](a_share_research/methods/a_share_theme_v1.toml)，日常流程见 [A 股工作流](a_share_research/WORKFLOW.md)。

### 美股报告重点

美股引擎关注：

- SEC 或发行人一手证据。
- 收入增长、营业利润率、自由现金流率。
- 市值、净现金、企业价值、EV/Revenue 和 FCF yield。
- 每个派生数字的公式、输入 fact ID 和状态。
- bull case、bear case、risk verdict。
- 催化、失效条件、数据缺口和人工复核项。

方法卡见 [美股催化方法](us_equity_research/methods/us_equity_catalyst_v1.toml)，日常流程见 [美股工作流](us_equity_research/WORKFLOW.md)。

### 为什么 artifact 不允许手工修改

artifact 是审计证据，不是工作草稿。读取时会校验 manifest 和内容哈希；发现缺失、路径逃逸、符号链接或内容篡改时，系统会拒绝把它当成可信结果。

如果需要改变研究内容，请创建新的 snapshot 或请求并生成一个新 run，不要编辑旧 run 目录。

## 如何接入 DeepSeek Harness

### DSH 在系统中的角色

DeepSeek Harness 适合做：

- 自然语言入口。
- 选择日报、主题或个股工作流。
- 触发 Python CLI。
- 展示短摘要、warnings 和 data gaps。
- 根据 artifact ID 读取报告并继续追问。

它不负责：

- 直接访问数据商。
- 直接读写 SQLite。
- 修改 canonical 报告。
- 把 session log 当研究数据库。
- 使用会话 job 代替每日持久调度。
- 连接券商或发送订单。

### 适配器暴露的工具

| 市场 | 运行工具 | 读取工具 |
|---|---|---|
| A 股 | <code>cn_research_run</code> | <code>cn_artifact_read</code> |
| 美股 | <code>us_research_run</code> | <code>us_artifact_read</code> |

每个适配器只注册两个业务级工具。它通过显式 argv 和单个 JSON stdin 调用对应 Python CLI，并把取消信号传给整个 Unix 子进程组。

### 构建 A 股适配器

~~~bash
cd a_share_research/adapter-pkg
npm ci
npm test
./node_modules/.bin/tsc --noEmit
~~~

### 构建美股适配器

~~~bash
cd us_equity_research/adapter-pkg
npm ci
npm test
./node_modules/.bin/tsc --noEmit
~~~

### 安装前备份 DSH profile

DSH 目前仍是快速迭代版本。修改 profile 前，至少备份对应的 <code>package.json</code> 和 lockfile。详细回滚方式见两个 adapter README：

- [A 股 DSH adapter](a_share_research/adapter-pkg/README.md)
- [美股 DSH adapter](us_equity_research/adapter-pkg/README.md)

### 安装到 Web 和 headless profile

必须使用绝对路径：

~~~bash
dsh plugin --profile web add /absolute/path/to/stock-research-harness/a_share_research/adapter-pkg
dsh plugin --profile headless add /absolute/path/to/stock-research-harness/a_share_research/adapter-pkg

dsh plugin --profile web add /absolute/path/to/stock-research-harness/us_equity_research/adapter-pkg
dsh plugin --profile headless add /absolute/path/to/stock-research-harness/us_equity_research/adapter-pkg
~~~

检查插件是否进入 profile：

~~~bash
dsh --profile web --dump-config | grep -E 'cn-a-share-research-tools|us-equity-research-tools'
dsh --profile headless --dump-config | grep -E 'cn-a-share-research-tools|us-equity-research-tools'
~~~

### A 股 DSH 参数名注意

Python CLI 的 ID 字段是：

~~~json
{"snapshot": {"selector": "id", "snapshot_id": "cn-example"}}
~~~

当前 A 股 DSH 工具为了保持已发布 tool contract，使用：

~~~json
{"snapshot": {"selector": "id", "id": "cn-example"}}
~~~

适配器会把 <code>id</code> 映射成 CLI 的 <code>snapshot_id</code>。美股 CLI 和美股 DSH 工具都使用 <code>snapshot_id</code>。调用时不要混用。

A 股 DSH 的 <code>stock_research</code> symbol 使用六位股票代码；美股 symbol 使用大写 ticker。

### 在 DSH 中怎么说

A 股日报：

~~~text
请调用 cn_research_run 生成 A 股日报。
workflow=daily_report
decision_at=2026-08-17T08:30:00+08:00
snapshot.selector=demo
top_n=5
先说明这是 FIXTURE，再返回 artifact_id、重点研究对象、证据缺口和风险提示。
~~~

美股个股研究：

~~~text
请调用 us_research_run 深入研究 fixture 中的 DEMOA。
workflow=stock_research
symbol=DEMOA
decision_at=2026-08-17T08:30:00-04:00
snapshot.selector=demo
top_n=5
不要给交易建议；返回 artifact_id、研究状态、bull/bear/risk 和 UNKNOWN 项。
~~~

读取完整报告：

~~~text
请调用 us_artifact_read，读取 artifact_id=YOUR_ARTIFACT_ID 的 report，
max_chars=12000。只总结报告中已有的事实和结论，不要重写 canonical 报告。
~~~

切换到真实数据时，把 <code>demo</code> 改成 <code>latest</code> 或指定 <code>id</code>，前提是相应目录已经有验证通过的真实 snapshot。

### DSH 输出为什么比报告短

模型可见的工具结果会进入 session log，因此 adapter 会：

- 限制文本和列表长度。
- 隐藏绝对路径。
- 脱敏疑似密钥。
- 对 warnings 和 gaps 只给有界预览。
- 用 opaque <code>artifact_id</code> 指向完整报告。

这不是信息丢失。完整内容仍在 canonical artifact 中，通过读取工具按需获取。

## 如何在远程 Mac 运行

下面假设：

- 当前机器保存代码或作为访问端。
- 远程 Mac 已配置 SSH、uv、Node 22+ 和 DeepSeek Harness。
- 远程目标目录是 <code>/Users/name/ai/stock</code>。

### 1. 同步 A 股项目

在仓库根目录执行：

~~~bash
bash scripts/deploy_remote.sh user@remote-mac /Users/name/ai/stock
~~~

A 股部署脚本会：

- 在远端创建显式目标目录。
- 备份已有代码。
- 使用 rsync 同步源码。
- 不使用 <code>--delete</code>。
- 排除远端的 data、materials、artifacts、reports、prompts 和 templates。
- 不自动修改 DSH profile。

它不会自动安装依赖；按脚本最后的提示在远端执行：

~~~bash
ssh user@remote-mac
cd /Users/name/ai/stock/a_share_research
uv sync --frozen
uv run a-share-research doctor
uv run a-share-research demo

cd adapter-pkg
npm ci
npm test
./node_modules/.bin/tsc --noEmit
~~~

### 2. 同步美股项目

先做本地脚本自检：

~~~bash
bash scripts/deploy_us_remote.sh --self-test
~~~

再部署：

~~~bash
bash scripts/deploy_us_remote.sh user@remote-mac /Users/name/ai/stock
~~~

美股部署脚本会：

- 备份远端已有代码。
- 无删除地同步源码。
- 保留远端用户自己的 prompts、templates、数据和历史报告。
- 只在默认 prompt/template 缺失时补齐版本库中的默认文件。
- 在远端运行 <code>uv sync --frozen</code>、Python 测试、doctor、adapter 测试和 TypeScript 检查。
- 不自动修改 DSH profile。

### 3. 在远端安装插件

~~~bash
ssh user@remote-mac

dsh plugin --profile web add /Users/name/ai/stock/a_share_research/adapter-pkg
dsh plugin --profile headless add /Users/name/ai/stock/a_share_research/adapter-pkg

dsh plugin --profile web add /Users/name/ai/stock/us_equity_research/adapter-pkg
dsh plugin --profile headless add /Users/name/ai/stock/us_equity_research/adapter-pkg
~~~

如果远端项目位置不是 adapter 可以自动推导的标准位置，可显式设置：

~~~bash
export A_SHARE_RESEARCH_ROOT=/Users/name/ai/stock/a_share_research
export US_EQUITY_RESEARCH_ROOT=/Users/name/ai/stock/us_equity_research
export STOCK_RESEARCH_WORKSPACE=/Users/name/ai/stock
~~~

两个 adapter 默认就会把共同父目录 <code>/Users/name/ai/stock</code> 作为 workspace。CN 和 US 的 normalized、SQLite、artifacts 与 reports 都有互不重叠的固定路径，因此可以安全共用这个父目录。远端手工运行 CLI 时也传入同一个 workspace，才能读取到 DSH 已生成的 artifact：

~~~bash
cd /Users/name/ai/stock/a_share_research
uv run a-share-research --workspace /Users/name/ai/stock doctor

cd /Users/name/ai/stock/us_equity_research
uv run us-equity-research --workspace /Users/name/ai/stock doctor
~~~

### 4. headless 运行

~~~bash
dsh --profile headless '请调用 us_research_run，使用 demo snapshot 生成一份美股日报，并返回 artifact_id。'
~~~

### 5. 从当前机器访问远程 Web profile

先在远程 Mac 启动 Web profile，只监听 loopback：

~~~bash
dsh --profile web --host 127.0.0.1 --port 3080
~~~

再在当前机器建立 SSH 隧道：

~~~bash
bash scripts/open_ssh_tunnel.sh user@remote-mac 3080 3080
~~~

然后在当前机器打开：

~~~text
http://127.0.0.1:3080
~~~

这里的 <code>127.0.0.1</code> 有两个不同上下文：

- DSH 在远程 Mac 上监听远端自己的 loopback。
- SSH 把远端 loopback 的 3080 映射到当前机器自己的 loopback 3080。

因此浏览器访问本机 <code>127.0.0.1</code>，并不表示 DSH 安装在当前机器上。它只是 SSH 隧道的本地入口。

### 6. 真实采集与无 DSH 工作流

A 股的研究 seed 保存已经人工核验的政策、公告、题材和候选；采集器只补 BaoStock 不复权日线及三只宽基指数。先复制示例并替换全部合成内容，示例标记未清理时采集器会在联网前拒绝：

~~~bash
cd ~/ai/stock/a_share_research
uv sync --extra market
cp config/research_seed.example.json ~/ai/stock/data/seeds/cn-research-seed.json
# 编辑副本：删除 example_notice，替换 example.invalid、合成名称和研究结论

uv run a-share-research --workspace ~/ai/stock \
  collect-snapshot \
  --seed-json ~/ai/stock/data/seeds/cn-research-seed.json \
  --snapshot-id cn-20260818-manual-v1
~~~

CN 快照固定标记为 <code>RECONSTRUCTED_NON_PIT</code>，不能伪装成严格历史回放。生产 CLI 使用真实系统抓取时间，不允许覆盖 <code>retrieved_at</code>；候选代码和身份也必须一致，例如 <code>600000 ↔ CN.SH.600000</code>。

美股采集器从 SEC submissions 与 companyfacts 获取 filing 元数据和可确定性解析的财务事实。它要求 SEC 合规的 <code>SEC_USER_AGENT</code>，不需要 API key；没有经过许可声明的行情 JSON 时，价格、估值和市场门槛保持 <code>UNKNOWN</code>，候选不会被冒充为可观察结论：

~~~bash
cd ~/ai/stock/us_equity_research
uv sync
cp config/sec-seed.example.json ~/ai/stock/data/seeds/us-sec-seed.json
# 编辑副本：删除 example 标记，更新数据观察时点，并人工核验所有叙事

export SEC_USER_AGENT='stock-research-harness your-real-contact@example.com'
uv run us-equity-research --workspace ~/ai/stock \
  collect-sec-snapshot \
  --seed-json ~/ai/stock/data/seeds/us-sec-seed.json \
  --snapshot-id us-20260818-sec-v1
~~~

两个 wrapper 都按显式 snapshot ID 执行，并最终返回完整 report artifact；它们不依赖 DSH，也不会退回 demo。A 股日报只接受本次新建的 snapshot，且 <code>decision_at</code> 不能早于实际抓取时间：

~~~bash
bash ~/ai/stock/scripts/run_cn_daily.sh \
  --root ~/ai/stock \
  --seed-json ~/ai/stock/data/seeds/cn-research-seed.json

SEC_USER_AGENT='stock-research-harness your-real-contact@example.com' \
bash ~/ai/stock/scripts/run_us_validation.sh \
  --root ~/ai/stock \
  --seed-json ~/ai/stock/data/seeds/us-sec-seed.json \
  --snapshot-id us-20260818-validation-v1
~~~

### 7. 每日调度

本项目不把 DSH session job 当成日报调度器。A 股已提供工作日 20:30 的 macOS LaunchAgent 安装器：

~~~bash
bash ~/ai/stock/scripts/install_cn_launchd.sh \
  --root /Users/yourname/ai/stock \
  --seed-json /Users/yourname/ai/stock/data/seeds/cn-research-seed.json \
  --load
~~~

安装器默认只渲染 plist；只有 <code>--load</code> 才会加载。执行顺序是“采集 → 显式 ID 研究 → canonical artifact → 完整报告”，所以即使 DSH 暂停或升级失败，日报链路也不会停止。美股目前提供单次验证 wrapper，不默认安装定时任务。

## 数据源和许可证策略

### A 股建议

证据主源优先级：

1. 国务院及部委原始政策页面。
2. 巨潮资讯及上交所、深交所、北交所正式披露。
3. 上市公司公告与官网。
4. 有明确授权的结构化行情和财务数据。
5. 行业资料。
6. 新闻、论坛和社交媒体只作为线索，不作为最终事实。

个人研究可以用 Tushare、BaoStock 或 AKShare 做内部适配和交叉检查，但代码许可证不等于数据许可证。收费 newsletter、公开数据服务或商业产品必须重新核对数据商和原始网站的抓取、缓存、展示与再分发条款。

### 美股建议

证据主源优先级：

1. SEC EDGAR 原始 filings 与 XBRL facts。
2. 发行人 IR 的 earnings release、presentation 和 guidance。
3. BLS、BEA 等原始宏观发布机构。
4. 用户自带 key 且授权范围明确的结构化行情提供商。
5. 二级新闻和社交信息只作发现线索。

未经单独条款审核，不要默认把 FRED API、第三方 transcript、卖方一致预期或个人用途行情许可接入 AI 管道或公开报告。

### 开源仓库不包含什么

不要提交：

- API key、Cookie、SSH key 或任何凭据。
- 原始行情响应或付费数据。
- 批量公告、filing、PDF 或 transcript。
- 真实 SQLite。
- artifacts、reports 或用户研究历史。
- 绝对本地路径和远程主机信息。

根目录 [.gitignore](.gitignore) 已排除主要运行时目录，但提交前仍应检查 <code>git status</code> 和 staged diff。

## 测试与质量检查

### A 股 Python

~~~bash
cd a_share_research
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run a-share-research doctor
uv run a-share-research demo
~~~

### 美股 Python

~~~bash
cd us_equity_research
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run us-equity-research doctor
uv run us-equity-research demo
~~~

### A 股 adapter

~~~bash
cd a_share_research/adapter-pkg
npm ci
npm test
./node_modules/.bin/tsc --noEmit
npm pack --dry-run
~~~

### 美股 adapter

~~~bash
cd us_equity_research/adapter-pkg
npm ci
npm test
./node_modules/.bin/tsc --noEmit
npm pack --dry-run
~~~

### 部署脚本

~~~bash
bash -n scripts/deploy_remote.sh
bash -n scripts/deploy_us_remote.sh
bash -n scripts/open_ssh_tunnel.sh
bash scripts/deploy_us_remote.sh --self-test
~~~

## 常见问题

### no normalized snapshot found

原因：你使用了 <code>latest</code>，但对应市场的 normalized 目录里还没有真实 snapshot。

处理：

1. 首次安装验证先用 <code>demo</code>。
2. 根据对应 snapshot schema 生成真实快照。
3. 放到正确市场目录。
4. 再用 <code>latest</code> 或 <code>id</code>。

不要把错误处理成自动回退 demo；那会把合成数据混入真实研究。

### decision_at 必须带时区

错误示例：

~~~text
2026-08-17T08:30:00
~~~

正确示例：

~~~text
2026-08-17T08:30:00+08:00
2026-08-17T08:30:00-04:00
2026-08-17T12:30:00Z
~~~

### market handshake 失败

- A 股引擎只能接收 <code>CN</code>。
- 美股引擎必须接收 <code>US</code>。
- 检查是否把 CN 工具指向了美股 root，或把 US snapshot 放进了 A 股目录。

适配器启动时也会检查市场和 schema 版本；不匹配会直接拒绝运行。

### DSH 里看不到工具

依次检查：

1. adapter 目录是否已执行 <code>npm ci</code> 和 <code>npm test</code>。
2. <code>dsh plugin ... add</code> 是否使用绝对路径。
3. 是否装到了正在运行的 profile。
4. <code>dsh --profile &lt;name&gt; --dump-config</code> 是否包含插件。
5. 修改后是否重启了相应 DSH 进程。

### value is not lossless JSON

当前 main 已处理可选字段中的 <code>undefined</code>，并有 JSON round-trip 回归测试。如果旧远端仍报这个错误：

1. 在远端拉取或重新同步最新 main。
2. 进入对应 <code>adapter-pkg</code>。
3. 重新执行 <code>npm ci</code> 和 <code>npm test</code>。
4. 从 DSH profile 移除旧插件，再按绝对路径重新安装。

### CLI demo 正常，但 DSH 报 DeepSeek API request failed

这通常说明研究引擎本身可用，问题位于 DSH 的模型 provider、API key、网络、代理或 endpoint 配置。先分别验证：

1. CLI demo 是否成功。
2. DSH 能否完成一个不调用研究工具的最小模型请求。
3. DSH profile 是否读取到了正确的 provider 配置。
4. 远程 Mac 是否能访问相应 API endpoint。

不要为了修 provider 连接而改研究 artifact 或 snapshot。

### 为什么报告读取只返回相对路径和有限文本

这是安全与上下文控制。DSH session 不应包含绝对本地路径、完整受限数据或超长报告。使用 artifact ID 和分 section 读取即可。

### integrity check failed 或 tamper detected

旧 artifact 已经不再与 manifest 一致。不要修补旧文件后继续使用；应保留现场用于审计，并用新的输入重新生成一个 run。

### 为什么远程地址也是 127.0.0.1

见[远程 Web profile](#5-从当前机器访问远程-web-profile)。远端和本地各自有一个 loopback，SSH 隧道把二者连接起来。

## 项目结构

~~~text
stock-research-harness/
├── AGENTS.md
├── README.md
├── LICENSE
├── a_share_research/
│   ├── README.md
│   ├── WORKFLOW.md
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── schemas/
│   ├── methods/
│   ├── prompts/
│   ├── templates/
│   ├── src/a_share_research/
│   ├── tests/
│   └── adapter-pkg/
├── us_equity_research/
│   ├── README.md
│   ├── WORKFLOW.md
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── schemas/
│   ├── methods/
│   ├── prompts/
│   ├── templates/
│   ├── src/us_equity_research/
│   ├── tests/
│   └── adapter-pkg/
└── scripts/
    ├── deploy_remote.sh
    ├── deploy_us_remote.sh
    ├── run_cn_daily.sh
    ├── run_us_validation.sh
    ├── install_cn_launchd.sh
    ├── open_ssh_tunnel.sh
    └── sync_course_materials.sh
~~~

### 文档入口

- [A 股项目说明](a_share_research/README.md)
- [A 股研究工作流](a_share_research/WORKFLOW.md)
- [A 股 DSH adapter](a_share_research/adapter-pkg/README.md)
- [美股项目说明](us_equity_research/README.md)
- [美股研究工作流](us_equity_research/WORKFLOW.md)
- [美股 DSH adapter](us_equity_research/adapter-pkg/README.md)

## 路线图

短期优先级：

1. 在现有 A 股 seed + BaoStock 采集器上增加官方公告/政策的增量发现、哈希和首次抓取时间。
2. 在现有 SEC 采集器上增加发行人 IR、官方宏观与经过授权的行情 adapter。
3. 用真实 forward snapshot 连续生成并盲评日报。
4. 记录主题和标的后续验证结果，评估研究质量而不是只评估格式正确率。
5. 加固现有 LaunchAgent 的监控、失败告警和交易日历判断，同时保持调度与 DSH session 解耦。
6. 在数据授权明确后增加 estimate vintage、transcript 或更多财务字段。

明确不在路线图内：

- 自动跟单。
- 实盘下单。
- 券商账户管理。
- 用回测收益包装未经验证的 LLM 预测能力。

## 灵感来源

本项目吸收了以下开源项目的设计思想，但没有把它们拼装成运行时依赖：

- [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot)：确定性估值、研究流水线和数字溯源。
- [Dexter](https://github.com/virattt/dexter)：研究循环、工具边界、scratchpad 和可评测性。
- [TradingAgents](https://github.com/TauricResearch/TradingAgents)：多角色分析、正反辩论、风险审查和 checkpoint。
- [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)：mandate、组合级风险和研究周期记录。
- [StockBench](https://github.com/ChenYXxxx/stockbench) 与 [FINSABER-2](https://github.com/waylonli/FINSABER)：point-in-time 评测和失败基准。
- [Anthropic Financial Services](https://github.com/anthropics/financial-services)：金融研究 skill、命令和输出结构。

外部项目、论文、数据和模型仍受各自许可证与使用条款约束。本仓库只借鉴公开思想，不复制受限数据、prompt、品牌或交易结论。

## 贡献与许可证

提交修改前请遵守：

1. 市场边界不能弱化；CN 与 US 必须继续物理隔离。
2. 不得新增买卖、目标价、仓位、订单或券商接口。
3. 新证据类型必须保留来源和时间字段。
4. 新计算必须由确定性代码完成，并保存输入和公式。
5. 数据缺失必须显式暴露，不能静默填充。
6. 先更新对应 schema、测试和子项目文档，再更新根 README。
7. 提交前检查 staged diff，确保没有 credentials、数据、artifacts 或报告。

仓库代码使用 [MIT License](LICENSE)。

本项目仅用于研究和教育，不构成投资建议。任何事实、估值、风险判断和实际交易决定都必须由使用者独立复核。
