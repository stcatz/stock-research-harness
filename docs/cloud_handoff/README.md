# A 股最小接管接入

这是当前 harness 的增量交接目录，不是新的研究平台。

- [AUDIT.md](AUDIT.md)：实施前能力矩阵、证据位置与最小变更清单。
- [CONTRACT.md](CONTRACT.md)：由实际可见需求提炼的本地候选合同。
- [LESSONS.json](LESSONS.json)：10 项可见流程经验；包含适用条件、反例和测试意图。
- [DAILY.md](DAILY.md)：现有 CLI 的旁路盘前、收盘、回读入口与日常权限边界。
- [ACCEPTANCE.md](ACCEPTANCE.md)：本次实测结果、运行 ID、四项独立状态和切换决策卡。
- `00_CODEX_TAKEOVER_PROMPT.md`：原主指令的只读来源归档，不是每日任务提示词。
- `import_manifest.json`：导入内容哈希与缺失文件列表。

未取得原接管包的 README、01、02、03 及原 manifest。此目录不宣称完整导入云端历史或原有 13 项经验 / 20 项种子。新的 25 项工程测试来自当前可见需求，不冒充原附件测试。

现有 pipeline 仍是报告的唯一作者。新记录保存在原 CN SQLite，不创建第二数据库，也不改 US。所有新入口固定 shadow，不能触发生产发布、定时任务或交易。
