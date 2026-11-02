---
description: 收工前更新任务卡的拆分状态和交接，让下一个人或 agent 能接着做
argument-hint: [你是哪个 agent]
---
收工前做交接。

1. 运行 `python3 .agentkit/agentkit.py task show`。
2. 更新任务卡：
   - 「拆分」：这次做完的改成 `[done]`，正在做的改成 `[doing]`。
   - 「交接」：做到（做到哪一步，证据在哪）、下一步（下一个人或 agent 打开就能动手的那一步）、试过不行的（试过什么、为什么放弃，免得别人重走）。
   - 这次和人确定下来的事，记进「要人拍板的事」：决定、谁定的、日期。
3. 运行 `python3 .agentkit/agentkit.py task handoff --agent "$ARGUMENTS"`（没写就填你自己是哪个 agent）。
4. 告诉我交接写了什么，以及还有哪些事等我拍板。

不要提交或推送，除非我说可以。

<!-- 由 agentkit 生成：升级时会被覆盖 -->
