# 投研助手工作区

这是一个不连接券商、不自动下单的双市场投研工作区。A 股与美股是两个物理隔离的研究引擎：各自有独立的 CLI、SQLite、artifacts、reports、fixtures 和 DSH 薄插件；它们共享的是安全边界，而不是运行时目录。

正式事实只能来自带时间戳的官方披露和结构化市场快照。仓库内置的 `demo` 全部是合成数据，只用于安装、验证和端到端测试。

## 项目

- `a_share_research/`：A 股题材研究引擎，侧重题材发现、受益链和同期市场确认。
- `us_equity_research/`：US equity research engine，固定 `market=US`，以离线 snapshot、PIT 时间门槛、Evidence -> Claim -> Thesis 结构和不可变报告为核心。

## 快速验证

A 股：

```bash
cd ~/ai/stock/a_share_research
uv sync
uv run a-share-research demo
uv run python -m unittest discover -s tests -v
```

美股：

```bash
cd ~/ai/stock/us_equity_research
uv sync
uv run us-equity-research doctor
uv run us-equity-research demo
uv run python -m unittest discover -s tests -v
```

详细契约、真实 snapshot 放置方式、DSH 使用方式与研究边界分别见 `a_share_research/README.md` 和 `us_equity_research/README.md`。

## 当前边界

- 两个引擎都只输出研究状态，不输出买卖、目标价、仓位或自动交易动作。
- `us_equity_research/` v0.1 只消费本地 snapshot；当前不包含网络抓取、卖方一致预期、财报电话会转录授权、完整 DCF、组合管理、券商接入或任务调度。
- `demo` / `FIXTURE` 明确表示合成数据；不得把 fixture 输出包装成真实研究结论。
- DSH 是薄客户端，不是正式报告作者。正式 artifact 只能由 Python 引擎写出。

## 数据、来源与开源边界

- 代码、schema、方法卡、合成 fixture 和适配器可以进入 Git。
- 原始 filings、issuer materials、行情响应、数据商响应、SQLite、reports、artifacts、课程原文与任何凭据均不得进入 Git。
- US 项目推荐把 SEC EDGAR、issuer IR 作为一级事实源，把 BLS/BEA 作为官方宏观源，把自带许可证的 BYOK 市场数据供应商作为结构化价格源；未经过单独许可审查，不要接入 FRED API。

## Inspiration And Attribution

`us_equity_research/` 只吸收公开方法，不复制外部项目代码、测试、数据或品牌表达：

- FinRobot：吸收“计算必须可追溯到输入 fact ID 和公式”的做法。
- Dexter：吸收“版本化请求、append-only manifest、bounded artifact read”的接口纪律。
- TradingAgents：吸收“bull case / bear case / risk verdict 分栏”的研究结构。
- ai-hedge-fund：吸收“研究授权范围写成强约束，不接交易执行”的边界管理。
- StockBench / FINSABER：吸收“固定 `decision_at`、PIT 证据排除、可重放评估”的时间方法。
- Anthropic financial-services：吸收“daily/theme/stock 任务外形和专业报告分节”的工作流启发。

本仓库自身代码按 `LICENSE` 中的 MIT 条款发布；外部项目、论文、文章和示例仍各自受其原始许可证或使用条款约束。若未来引入任何上游代码、提示词或文本，必须单独保留原始版权与归属说明，不能借本仓库 MIT 许可证覆盖上游材料。

本项目仅用于研究，不构成投资建议。
