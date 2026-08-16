# 投研助手工作区

这是一个不连接券商、不自动下单的投研工作区。A 股与美股按两个独立项目演进；本轮只交付 A 股 v0.1，美股项目不进入本次发布，避免把未验证的演示骨架伪装成可用系统。

## 项目

- `a_share_research/`：已实现的 A 股题材研究 CLI、SQLite、不可变 artifacts 与 DeepSeek Harness 薄插件。

正式事实只能来自带时间戳的官方披露和结构化市场快照。仓库内置的 `demo` 全部是合成数据，只用于安装和端到端测试。

## A 股快速验证

```bash
cd ~/ai/stock/a_share_research
uv sync
uv run a-share-research demo
uv run python -m unittest discover -s tests -v
```

详细契约、真实 snapshot 放置方式与 DSH 安装说明见 `a_share_research/README.md`。

## 数据与开源边界

代码、schema、方法卡、合成 fixture 和适配器可以进入 Git。课程原文、原始公告、行情缓存、数据商响应、报告、SQLite 和任何凭据均被排除在仓库之外。

本项目仅用于研究，不构成投资建议。
