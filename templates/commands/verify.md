---
description: 按改动运行本仓库的检查，贴出真实结果
argument-hint: [对比的分支，默认自动找 main]
---
运行 `python3 .agentkit/agentkit.py checks`；如果我给了对比分支（$ARGUMENTS），加上 `--base <对比分支>`。

- 把它输出的表格原样贴出来，不要改写结果。
- 失败的检查，逐个说明原因和你打算怎么修，先别动手改，等我确认。
- 表格下面如果列出了“没有专门检查覆盖的改动”，提醒我是否需要在 agentkit.toml 里补检查。

<!-- 由 agentkit 生成：升级时会被覆盖 -->
