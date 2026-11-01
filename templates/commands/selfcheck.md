---
description: 按项目规则和 agentkit 格式审查当前改动（请在新开的对话里运行）
argument-hint: [对比的分支，默认 main]
---
你是审查者，没有参与写这段代码。如果这些代码是你在这个对话里写的，先提醒我新开一个对话再运行这个命令，然后停下。

1. 运行 `python3 .agentkit/agentkit.py context --for review`，按它列出的顺序读完项目规则和 `.agentkit/review-format.md`。
2. 对比的分支是：$ARGUMENTS（没写就用 main）。用 `git diff <对比分支>...HEAD` 和 `git status` 查看全部改动，包括还没提交的。
3. 运行 `python3 .agentkit/agentkit.py checks --base <对比分支>`，把结果当作证据。
4. 需求以 PR 描述、任务记录，或我在对话里给出的三问为准；都没有的话，先问我要。
5. 按 review-format.md 的输出模板，把结果写进 `.agentkit/review.local.md`（覆盖旧的），并原样回复给我。写完运行 `python3 .agentkit/agentkit.py validate .agentkit/review.local.md`，不通过就改到通过。除了这个文件，不要修改任何文件。

<!-- 由 agentkit 生成：升级时会被覆盖 -->
