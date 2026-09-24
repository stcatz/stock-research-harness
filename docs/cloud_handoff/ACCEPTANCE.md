# 接管验收与切换决策卡

验收日期：2026-09-10（Asia/Shanghai）。分支：`codex/cn-cloud-handoff`。代码与文档未提交、未 push、未部署。已有未提交 HiThink / engine / reporting 改动原样保留。

## 四个独立状态

| 状态 | 结论 | 证据与限制 |
|---|---|---|
| 工程完成 | 本轮最小旁路交接完成；完整接管规格未验收 | 原 CLI + 原 SQLite 加入冻结/回读/追加/索引；未取得全部原附件，语义评估和完整交付仍缺 |
| 真实接入 | 仅验证已有真实快照的历史重建 | 本次没有联网、消耗模型或数据额度；不能证明当前采集权限、额度与数据新鲜度 |
| 无人值守验证 | 未执行 | 无交易日历、主机连续可用性与真实私人交付回执记录；对应本地 LaunchAgent 当前未加载 |
| 生产切换 | 未执行 | 新流程固定 shadow / not_published；旧云端任务未改，状态未核实 |

## 复用与修改

复用 `run_research`、snapshot loader/validator、report renderer、artifact 哈希校验、文件锁和 CN SQLite。

本轮改动：根 AGENTS 仅增加读取入口；原 CLI 增加四个 shadow 命令；pipeline 增加宿主内部完整 packet 校验读取；新增 `core/handoff.py`、`tests/test_handoff.py` 和本目录规范。没有改采集源、原方法权重、DSH 工具、调度脚本或美股实现。

CN 原库新增 `cn_handoff_records`（日期索引、父 ID、JSON 内容哈希、禁止 UPDATE/DELETE 的触发器）。完整 JSON 是权威交接记录；本地导出的 JSON 是已校验副本。原预测与报告不反写。

## 旁路实际运行

运行使用真实业务日 **2026-08-31** 的已有快照，实际执行发生在 **2026-09-10**；始终 `reconstructed`、`formal_sample=false`。

- 输入：`data/normalized/cn-20260830-224928-hithink-v8/snapshot.json`；as_of 为 8 月 28 日收盘，采集于 8 月 30 日，早于目标日 07:00。
- 收盘输入：`data/normalized/cn-20260831-2332-hithink-v1/snapshot.json`；as_of 为 8 月 31 日收盘，采集于当日夜间。
- run_id：`cn-2026-08-31-be49ebec9486`。
- artifact_id：`cn-artifact-be49ebec948605d3`。
- forecast_id：`cn-handoff-f-f701671cd890b887076f15aa`。
- review_id：`cn-handoff-r-b686e5fc19f9da98fb70687d`。
- 完整 canonical 产物：`artifacts/runs/cn-2026-08-31-be49ebec9486/`（request、snapshot、packet、report、summary、manifest）。
- 本轮回执与完整已校验导出：`artifacts/handoff-validation/20260909T162349Z/`（forecast.json、close_review.json、validation.json、各 CLI 请求/回读结果）。UTC 目录日期与北京时间跨日是正常现象。

这些 runtime 路径均被 Git 忽略，不提交报告、输入数据或绝对 artifact 路径。

实际入口（用对应虚拟环境中的 Python 或已安装 CLI）：

```sh
a-share-research --workspace <repo-root> premarket --request-json <validation-dir>/premarket.request.json
a-share-research --workspace <repo-root> close-review --request-json <validation-dir>/close.request.json
a-share-research --workspace <repo-root> journal-read --request-json <validation-dir>/forecast-read.request.json
a-share-research --workspace <repo-root> journal-list --request-json <business-date-request.json>
```

盘前、收盘和回读由不同 CLI 进程执行，不依赖本段聊天。9 个原候选保留；父预测 hash 前后一致；重复盘前触发复用同一 ID。报告的 bounded preview 超过 20000 字符而截断；完整原文件存在且通过哈希校验，未将预览当作完整送达。

收盘 9 项日涨幅均为 UNKNOWN：输入日线的 `trade_status` 为 UNKNOWN，未核验有效交易状态。相关同日证据仍保留，原确认/证伪条件不被改写。不会以有 close/preclose 数值为理由越过新增状态校验。自然语言条件、分钟路径和基准超额收益也保持 UNKNOWN。因此本次证明持久交接与降级可用，不证明完整效果评估或研究方法有效。

## 测试结果

| 检查 | 结果 |
|---|---|
| CN Python 全套 | 137 项通过，含 25 项新交接测试 |
| CN adapter（现有构建） | `node --test test/bridge.test.mjs`：10 项通过 |
| wrapper / 安装器沙箱测试 | `bash scripts/tests/test_daily_wrappers.sh`：15 项通过，没有加载真实任务 |
| 本轮修改的 Python 文件 Ruff lint / format | Ruff 0.16.4 本地缓存二进制检查通过 |
| compileall / diff whitespace | 通过 |
| 整个 CN 目录 Ruff | 未全绿：5 项既有未提交改动诊断，位于 HiThink enrichment/provider 及其测试（I001 / FURB157）；未顺手改动 |
| US Python | 本轮未重跑；美股实现未改，沿用此前 90 项通过记录 |
| 最新 Ruff 自动解析 | 离线缓存缺 0.16.5，未下载；改用已有 0.16.4 |

新增回归覆盖：完整候选/备选/排除保留、重复/并发触发、独立进程收盘、未来截止/盘中提前复盘、禁止生产与模型参数、显式 snapshot、周末非正式样本、时间越界标签、缺行情/前收/交易状态、错误标的/日期/频率与未来证据、未知语义/分钟结果、hash 损坏、artifact 篡改、UPDATE/DELETE、保存失败、回读失败、缺失父 ID、父类型错误、fixture/真实混用、日期索引、有界读取、外部内容不触发网络或进程。

未执行：缺失原附件全部验收种子、光通信等历史事实语义复核、同行实时覆盖、真实交易日历、实时新采集、真实模型返回/额度、私人交付、旧云端重复发布联测、未来连续无人值守运行。没有用 mock 结果替代这些验收。

## 唯一正式发布者方案与回退

1. 当前 Codex 旁路无发布能力，不构成第二发布者。旧任务存在与否、实际名称和回执均待用户/可用云端管理接口核验。
2. 生产实现前先固定唯一 publisher 标识、业务日幂等键、完整报告交付渠道和成功回执定义；数据或保存失败必须有故障回执。日历、截断处理和这些机制尚未实现，不能仅改 shadow 标志上线。
3. 新流程先完成不发布的真实连续盘前—收盘验证；记录实际时间与主机状态。核验旧任务状态后，由用户批准具体切换窗口。
4. 切换时先停用旧流程的正式发布资格并核验，再启用新发布者；不能仅靠两个任务约定“自己不要重复”。切换操作另行授权。
5. 若新流程失败，先撤销新发布资格并核验，再恢复旧任务；核对当日已发回执，避免补发重复。保留所有冻结记录和结果，不删除、不重写。

## 汇总决策卡（尚不请求上线批准）

| 生产切换所缺 | 需要的具体资料 / 决定 |
|---|---|
| 完整交接规格与批准方法 | 原接管包 01/02/03、README；确认确认条件/基准及已核验 seed，复核 10 项流程经验与原包差异 |
| 数据授权与预算 | 已许可供应商、可调用范围/额度、当日有效交易状态和前收数据；海外同行与事件覆盖要求 |
| 时间和交易日历 | 已核验交易日历；07:00 触发还是准点送达（后者须提前冻结并披露时效损失） |
| 主机和认证 | 运行主机、开机/唤醒保障、秘密注入机制及无人值守验收窗口 |
| 交付与告警 | 私人渠道、收件目标、完整报告传送方案、失败告警与回执格式；本轮未授权外发 |
| 旧任务与切换授权 | 旧云端任务 ID/状态、唯一发布者、具体切换/回退窗口；验收后再批准启停 |

不自动 commit、push、部署、安装或启停任务。日常任务不继承本次工程权限。
