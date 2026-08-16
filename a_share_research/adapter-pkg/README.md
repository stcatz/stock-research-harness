# A股 DSH Adapter

面向 DeepSeek Harness `0.1.0-rc.6` 的安装型 bundle。

- 工具名：`cn_research_run`
- 工具名：`cn_artifact_read`
- 运行方式：显式 argv + JSON stdin/stdout 调用项目 `.venv` Python CLI
- CLI 入口：`.venv/bin/python -m a_share_research.cli --workspace <workspace> run|artifact-read --request-json -`
- 市场固定：`CN`
- 输出：canonical CLI JSON，白名单映射，不暴露绝对路径
- 失败策略：`@deepseek-ai/dsh-tools` 缺失时拒绝加载，不静默降级
- 子进程环境：只继承最小系统变量，不继承模型或数据 API 凭据

参数契约：

- `cn_research_run`
  - `workflow`: `daily_report | stock_research | theme_research`
  - `decision_at`: 带时区 ISO-8601 时间
  - `snapshot`: `{ selector: demo | latest | id, id?: string }`
  - `subject?`, `symbol?`, `top_n?`
- `cn_artifact_read`
  - `artifact_id`
  - `section?`: `summary | report | manifest | packet`
  - `max_chars?`

本包包含：

- `dist/index.js`：Cordis 插件入口
- `cordis.patch.yml`：DSH bundle patch
- `package.json#dsh.bundle`：安装型 bundle 清单

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
