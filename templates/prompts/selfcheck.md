---
description: 以审查者的身份审当前改动（请在新开的会话里运行）
argument-hint: [对比的分支，默认 main]
---
你是审查者，没有参与写这段代码。如果这些代码是你在这个会话里写的，先提醒我新开一个会话再运行，然后停下。

1. 运行 `python3 .agentkit/agentkit.py review --print-prompt --output-file .agentkit/review.local.md --base <对比分支>`。对比的分支是：$ARGUMENTS（没写就省略 `--base`）。
2. 完全按它输出的要求审查。除了 `.agentkit/review.local.md`，不要修改任何文件。
3. 把结果原样回复给我。

想让别的 agent 或模型各开一个新会话来审，运行 `python3 .agentkit/agentkit.py review`，审查者在 agentkit.toml 的 `[review]` 里配置。

<!-- 由 agentkit 生成：升级时会被覆盖 -->
