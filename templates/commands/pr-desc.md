---
description: 按仓库的 PR 模板写 PR 描述，附上真实的检查结果
argument-hint: <任务号或任务链接>
---
根据这次的改动和我们的对话，写一份 PR 描述。任务：$ARGUMENTS

- 仓库有 `.github/pull_request_template.md` 的，按模板逐项填写。没有的话，用这几节：任务、需求（满足谁 / 解决什么 / 怎么算做完）、改了什么、怎么验证的、风险自评、需要人重点看的地方。
- “怎么验证的”：运行 `python3 .agentkit/agentkit.py checks`，把输出的表格原样贴进去；其他手动验证写真实的命令和结果，没做的写原因。
- 风险自评按 `.agentkit/review-format.md` 的“风险初判”来判断。
- 勾选框只勾真正做到的。
- 只输出 Markdown，不要直接创建 PR，除非我说可以。

<!-- 由 agentkit 生成：升级时会被覆盖 -->
