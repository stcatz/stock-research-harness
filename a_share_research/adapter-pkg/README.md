# A股 DSH Adapter

面向 DeepSeek Harness `0.1.0-rc.6` 的安装型 bundle。

- 工具名：`cn_research_run`
- 工具名：`cn_artifact_read`
- 工具名：`cn_outcome_history`
- 工具名：`cn_research_history`
- 运行方式：显式 argv + JSON stdin/stdout 调用项目 `.venv` Python CLI
- CLI 入口：`.venv/bin/python -m a_share_research.cli --workspace <workspace> run|artifact-read|outcome-history|research-history --request-json -`
- 市场固定：`CN`
- 输出：canonical CLI JSON，白名单映射，不暴露绝对路径
- 失败策略：`@deepseek-ai/dsh-tools` 缺失时拒绝加载，不静默降级
- 子进程环境：只继承最小系统变量，不继承模型或数据 API 凭据

参数契约：

- `cn_research_run`
  - `workflow`: `daily_report | stock_research | theme_research`
  - `decision_at`: 带时区 ISO-8601 时间
  - DSH 模型可见的 `snapshot`: `{ selector: latest | id, id?: string }`。真实研究使用 `latest` 或 `id`；`demo` 仅保留在底层 bridge 中用于安装测试，不向模型暴露。
    - `id` 只能与 `selector: id` 同时使用，且此时必填。
  - `subject?`, `symbol?`, `top_n?`
- `cn_artifact_read`
  - `artifact_id`
  - `section?`: `summary | report | manifest | packet | facts`
    - `facts` 不含 thesis/counter thesis/最终决策，专供独立反方。
    - 用户要求完整报告时必须分页读取 `report`；`summary` 只是紧凑预览，不能当作完整报告返回。
  - `max_chars?`
  - `cursor?`: 从 0 开始；持续读取返回的 `next_cursor`，并验证 `content_sha256` 和 `total_chars`。
- `cn_outcome_history`
  - `evaluation_at`: 带时区 ISO-8601 时间
  - `limit?`: 1–20；只返回当时已可用的旧结果，不写入 outcome。
- `cn_research_history`
  - `evaluation_at`: 带时区 ISO-8601 时间
  - `limit?`: 1–500；返回连续中间态、无进展天数和注意力老化动作，不改写 canonical 结果。

本包包含：

- `dist/index.js`：Cordis 插件入口
- `cordis.patch.yml`：DSH bundle patch
- `package.json#dsh.bundle`：安装型 bundle 清单

canonical 页面内容保持无损，才能与 `content_sha256` 对齐；如果页面出现疑似密钥或绝对本地路径，
adapter 会 fail-closed，而不是静默改写内容后继续声称哈希可验证。错误消息和摘要仍会有界脱敏。

本地构建与测试：

```bash
cd a_share_research/adapter-pkg
npm test
```

`npm test` 会先用 Node 22 的内建 TypeScript transform 生成 `dist/index.js`，再运行 `node:test` bridge 用例。

安装前先保存 profile 配置（示例）：

```bash
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$HOME/.dsh/backups/a-share-$stamp"
cp -a "$HOME/.dsh/profiles/web/package.json" "$HOME/.dsh/backups/a-share-$stamp/web-package.json"
cp -a "$HOME/.dsh/profiles/headless/package.json" "$HOME/.dsh/backups/a-share-$stamp/headless-package.json"
```

使用绝对路径分别安装到 Web 与 headless profile：

```bash
dsh plugin --profile web add /absolute/path/to/a_share_research/adapter-pkg
dsh plugin --profile headless add /absolute/path/to/a_share_research/adapter-pkg
dsh --profile web --dump-config | grep -F 'cn-a-share-research-tools'
dsh --profile headless --dump-config | grep -F 'cn-a-share-research-tools'
```

移除或回滚：

```bash
dsh plugin --profile web remove @user/dsh-a-share-research
dsh plugin --profile headless remove @user/dsh-a-share-research
```

本地路径安装由 profile 的 `package.json` / lockfile 记录；如移除命令失败，再从上述备份恢复 profile 配置并运行 `dsh plugin --profile <name> install`。插件默认从安装源路径推导项目根目录，部署位置特殊时显式设置：

```bash
export A_SHARE_RESEARCH_ROOT=/absolute/path/to/a_share_research
export STOCK_RESEARCH_WORKSPACE=/absolute/path/to/stock
```
