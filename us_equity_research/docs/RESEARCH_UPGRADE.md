# 美股研究升级契约：us-research-plan-v1.0

2026-09-12。独立 US 引擎，保留既有研究状态、数据目录和唯一报告写入者。没有接入交易能力。

## 已实现

- 财务事实同时检查自身与来源的 `available_at`。市场背景、多空风险段落、主题维度在缺少完整截止时间内支持时隐藏原文并标 UNKNOWN。
- `engine_version` 纳入运行标识；旧版无版本产物使用 `core/legacy_v01/` 冻结实现核验。该目录仅供历史核验，不用于新研究。摘要、报告、数据文件及清单的完整性校验继续执行；未知引擎版本拒绝读取。
- `research_diagnostics` 将公司原始证据、有效常规收盘、完整计算、预期版本覆盖分开。READY 表示所列字段覆盖，不代表业务观点正确或数据足够新；公司披露时效仍需结合报告头时间人工判断。
- 每个候选均有条件式计划卡。`exclude` 只生成 `EVIDENCE_REPAIR` 补证任务。卡片包括现有计算基线、原命题证伪条件、来源、披露前记录、披露后核验以及支持/反对/未知分支。
- 同主题成员不是经验证的同行；种子中的未来日期不是已经确认的财报日。尚无一致预期、同行回报或市场情绪自动评分。
- 计划卡为人工检查清单；没有调用语义模型、创建日程或持续监控。

## 行情资格

`close_price` 必须属于该股票，单位 `USD/share`、数值为正，来源为 `structured_market/market_price`，事实和来源均已在研究时点可得。报价只存在于其他股票或仅有行业指数不能通过。

真实数据超过 96 小时先标 STALE。这是保守上限，并不是通过新鲜度的充分条件；即使未超过 96 小时，也必须通过下述显式日历检验。长时间特殊休市可能保守降级，需要人工复核。

有效日历存在时，价格必须对应截至研究时点最近一个已完成常规时段：`period_end` 等于该日，来源 `as_of` 精确等于该日 `close_at`。供应商若把 `as_of` 写成采集时刻，应在规范化时另行保留采集时间，不能将其冒充收盘时刻。盘前盘后报价不能替代常规收盘。

缺少可验证日历为 UNKNOWN_CALENDAR；日期/时点不符为 SESSION_MISMATCH；缺有效报价为 UNKNOWN_NO_PRICE；合成夹具为 FIXTURE，不计入真实市场覆盖。不能通过行情资格的候选不能升级 observe。

## 可选 session_calendar 输入

这是人工核验后注入新冻结快照的可选字段，**没有自动日历采集器**。旧快照不可覆写。覆盖期间内应列出所有适用交易日，休市日不列行。应用范围限共同常规股票时段，特殊证券和市场安排需要重新核对。

```json
{
  "session_calendar": {
    "scope": "US_CORE_EQUITIES",
    "source_evidence_ref": "OFFICIAL-CALENDAR-EVIDENCE-ID",
    "coverage_start": "2026-11-25",
    "coverage_end": "2026-11-30",
    "complete": true,
    "sessions": [
      {"date":"2026-11-25","open_at":"2026-11-25T09:30:00-05:00","close_at":"2026-11-25T16:00:00-05:00"},
      {"date":"2026-11-27","open_at":"2026-11-27T09:30:00-05:00","close_at":"2026-11-27T13:00:00-05:00"},
      {"date":"2026-11-30","open_at":"2026-11-30T09:30:00-05:00","close_at":"2026-11-30T16:00:00-05:00"}
    ]
  }
}
```

以上为结构示例，不是可直接投入研究的完整事实包。`source_evidence_ref` 必须引用快照内官方来源证据，具有全套时间字段并在截止时点可得。校验会检查覆盖范围、行顺序、重复日期、时区和开收盘关系；**不能凭 complete=true 证明人工没有漏录交易日**，完整性需核源站。

有可用日历且存在未来开盘行时，给出下一开盘前 30 分钟、开盘后 30 分钟、收盘后 15 分钟的人工核验点，并同时显示纽约和上海时间。研究时点已过去的检查点不再生成。日历缺失或覆盖结束时显示未知，不按“下一个工作日”猜测。

## 完整报告读取

```sh
us-equity-research --workspace WORKSPACE artifact-read --complete --request-json REQUEST.json
```

请求中 `section=report`；`max_chars` 为每页大小；`offset` 默认 0，以 Unicode 字符计数。响应含 `offset/next_offset/total_chars`，末页 `next_offset=null`。`--complete` 从 0 顺序读取全部页面，逐页进行不可变产物核验，返回 `full_report_verified=true`。`scripts/run_us_validation.sh` 已采用此方式。

DSH `us_artifact_read` 同样支持 offset。调用方必须跟随服务器 next_offset，不能用可见文本长度计算下一页；适配器会脱敏，可见长度可能变化。Python CLI 的完整文本与 canonical 文件可作精确比较；适配器只承诺保留页面顺序和脱敏后内容，不承诺带凭据内容逐字相同。

## 尚未完成的外部数据链

没有安装或调用 EdgarTools/OpenBB/Qlib/Dexter/FinRobot/TradingAgents，没有新增收费行情授权、财报正文解析、一致预期版本库、完整同行库、市场宽度或波动率数据源。没有将 SEC companyfacts 自动等同于已读完公司报告。现有计算仍只覆盖原先指标集合；历史代码保留原有行为，仅用于旧产物读取。

后续语义模块必须消费冻结证据，逐条引用事实和计算；其输出只进入新候选快照并经规则门槛验收，不能越过缺数据的 UNKNOWN。事前/事后记录和历史评估需另做 append-only 证据账本，目前计划卡结果为 PENDING_MANUAL_REVIEW。
