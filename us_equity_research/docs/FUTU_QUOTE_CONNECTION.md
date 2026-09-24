# 富途只读行情接入（2026-09-13）

## 当前完成情况

已经实现不依赖富途 SDK 的只读 REST 采集器、规范化 OHLCV 待验包、行情观察附页，以及 SEC 行情入口的独立日历合并。2026-09-13 已完成官方 quote:read 授权及 MCP 实际读取：MSFT、SPY、QQQ 各 135 日的未复权和复权序列，最新 2026-09-11；交易日集合与 NYSE/Nasdaq 日历核对一致。REST 通道尚未实测，不能把 MCP 成功等同于 REST 成功。

官方 MCP 适合交互验证，REST 采集器用于固定文件流水线，两种认证不能未经验证直接互换。MCP 登录凭据由 Codex 管理；REST 采集器读取进程环境的 `FUTU_QUOTE_ACCESS_TOKEN`。不从 Codex 凭据库复制令牌，不输出或保存令牌。若 OAuth 的 audience 不支持 REST，须使用 REST 官方认证，不能把 MCP 令牌直接当 REST 令牌。

MCP 配置名为 futu-quotes，已核实授权范围只有 quote:read。实际工具目录包含交易及账户工具，故不能按名称前缀整体放开；当前白名单仅 quote_history_kline 与 quote_trading_days。研究引擎的 FutuMCPQuoteClient 接收宿主提供的 RPC 回调，不读取 Codex 凭据文件。此次联调在临时宿主进程内使用既有授权，仅向原 MCP 域名发送令牌，没有转发到 REST、输出或持久化令牌。

## 数据处理边界

采集器仅允许 GET 美国股票历史 K 线和 US 交易日历两个端点，拒绝其他路径。禁止重定向转发凭据；服务端错误仅输出固定说明。没有安装券商 SDK，也没有调用资金、持仓、自选修改或交易接口。

历史行情分别显式请求未复权和前复权不含股息日线。原始供应商响应只在内存中解析，规范化后仅保留字段白名单；未复权价保留供未来估值证据核验，观察附页使用拆股复权版本。

纽约日期、时区偏移、OHLC 范围、有限正价格、非负成交量、重复日期冲突和分页游标均会检查。分页失败拒绝生成产物。数据包独占创建，不覆写以前文件。

供应商没有提供每根 K 线的精确发布时间时，published_at 保留 UNKNOWN；available_at 记本次实际取得时间并注明 first_observed_at_collection，不倒填历史收盘。该数据包状态固定 STAGED_NOT_FORMAL_EVIDENCE，不会把行情缺口自动变成正式证据通过。

## 使用顺序

1. 从官方流程取得适用的行情读取授权，确认允许本次本地研究保存用途。
2. 复制 examples/futu-quote-plan.json 到本地运行目录，核对冻结标的、基准和起止日期。示例的许可字段刻意为 UNKNOWN；确认实际授权后填写 authorized_for_local_research_snapshot。这个值只是调用者声明，不是程序替用户取得授权。
3. 安全提供 REST 专用只读访问令牌到进程环境，不把令牌放入命令参数、仓库、报告或聊天。
4. 运行 collect-futu-quotes，生成新待验包。
5. 提供经核对、覆盖分析窗口和研究时点的 calendar bundle，再生成行情观察附页。

```sh
us-equity-research collect-futu-quotes --plan-json PLAN.json --output NEW-STAGING.json
us-equity-research futu-observation-report --bundle-json NEW-STAGING.json --calendar-json CALENDAR.json --decision-at ISO_TIME --output NEW-OBSERVATIONS.md
```

calendar bundle 结构为 `{ "session_calendar": {...}, "evidence": {...} }`，其中日历契约见 RESEARCH_UPGRADE.md。供应商 trade_second 不自动变成交易所已核实的开收盘时间。测试中使用的日历仅为合成数据，不可复制为真实证据。

## 主报告衔接

SEC collector 的 market JSON 新增可选 calendar_bundle；市场证据和日历官方证据分别验证，再写入同一新快照。原有 market JSON 继续可用，主报告及历史核验版本未变化。

严格主报告仍要求来源时间齐全。当前 Futu 待验包不能直接作为 market JSON：需要补足可验证证据时间、适用行情权益和正式日历。不得用 bar 日期代替 published_at，也不得为了通过门槛把 UNKNOWN 改成猜测时间。

行情观察附页提供：

- 1、5、20、60 日当前拆股复权价格回报，不含股息；不是严格历史当时版本。
- 20 日回报减固定基准回报，不标为风险调整 alpha。
- 当前量除以前 20 个完整日的均量，均量不含本日；公司行为及供应商量覆盖仍待人工核对。
- 必须按日历满足连续完整窗口，缺一个预期交易日则该窗口 UNKNOWN；缺最近收盘不使用较早日期冒充。

附页不生成 observe/exclude 判断，不冒充已经完成盘中监控，也不替代 SEC、原始 IR 和事前经营预期。

## 后续真实验收

登录后先用 MSFT 和固定基准验证少量已完成日线：核收盘、时区、复权、成交量口径及分页，再扩展样本。还需核供应商权限和速率限制。不要用今日取得的数据重写旧快照，也不要把合成测试标为真实验证。

## 本次真实联调发现

- MCP 日线 time_zone 实际为小时（-4/-5），适配器明确转为分钟后再与纽约时区核验；不改动 REST 的独立口径。
- MCP continuation 位于顶层 pagination。适配器每次限定最多 300 个日历日；仅接受明确 has_more=false 的完整响应，其余拒绝生成部分产物。不声称已验证多页游标。
- 实测小范围请求 num=2 仍返回 8 根日线，不能假定 num 是可靠截断。完整性以交易日集合核对，不以请求条数认定。
- 扩大请求时出现 ret_code=-11（频率限制）；减少重复请求并间隔请求后完成本次采集。固定配额、实时权益和费用仍 UNKNOWN。
- 市场数据与日历原始发布时间仍 UNKNOWN。新输出是待验行情附页，不会升级正式候选、替换历史快照或证明经营超预期。
- Python 全套 114 项检查通过；真实联调验证与合成测试分别记录。
