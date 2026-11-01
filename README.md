# Agent 协作工具包（v0）

把分享里“说清需求 → 先复述 → 先重现 → 一步一证据 → 另一个 AI 审查打分 → 按风险找人”这套做法，变成仓库里可以直接复制的配置。

不需要新服务。一个人加 Claude Code 就能用本地部分；GitHub 上的自动审查需要一个 API key。

## 里面有什么

| 文件 | 做什么 | 对应分享第几页 |
|---|---|---|
| `.github/pull_request_template.md` | PR 描述里必填需求三问和“怎么验证的” | 第 4 页 |
| `.claude/commands/restate.md` | `/restate`：先复述、确认问题存在、列出要拍板的事 | 第 5 页 |
| `.claude/commands/repro.md` | `/repro`：先写会失败的测试，不改实现 | 第 6 页 |
| `.claude/team-workflow.md` | 给 AI 的工作约定：一次一步、每步证据、3 轮上限、什么事要先问 | 第 6 页 |
| `REVIEW.md` | 审查规则：4 项打分、报问题的规矩、风险等级、输出模板 | 第 7 页 |
| `.claude/commands/selfcheck.md` | `/selfcheck`：提交前在新对话里按 REVIEW.md 自查 | 第 7、8 页 |
| `.claude/commands/pr-desc.md` | `/pr-desc`：按模板写 PR 描述，附真实证据 | 第 6 页 |
| `.github/workflows/ai-review.yml` | 每个 PR 自动审查，发一条带分数和风险的评论，加 `risk:*` 标签 | 第 7、8 页 |
| `.github/CODEOWNERS.example` | 高风险目录必须有人审 | 第 8 页 |
| `.claude/settings.json` | 禁止 AI 读 `.env` 和密钥目录 | — |
| `scripts/parse-ai-review.py` | 从审查评论里取出分数和风险（JSON），给任务平台用 | — |

## 30 分钟接入

**1. 复制到仓库**

```bash
./install.sh /path/to/your-repo
```

已经存在的文件不会被覆盖，脚本会列出来，需要的话手动合并。`CLAUDE.md` 只会在末尾加一行 `@.claude/team-workflow.md`。

**2. 填两处项目信息**

- `.claude/team-workflow.md` 的“项目信息”：跑测试和启动的命令。
- `REVIEW.md` 的“高风险清单”：你们的计费、权限、数据库等目录。

**3. 打开 GitHub 上的自动审查**

- 安装 Claude GitHub App（在 Claude Code 里运行 `/install-github-app`，或按官方文档手动安装）。
- 在仓库 Settings → Secrets 里添加 `ANTHROPIC_API_KEY`。用 Bedrock 或 Vertex 的，按 `ai-review.yml` 里的注释改。
- 创建三个风险标签：

```bash
gh label create risk:low    --color 0E8A16 --description "AI 审查：低风险"
gh label create risk:medium --color FBCA04 --description "AI 审查：中风险"
gh label create risk:high   --color D93F0B --description "AI 审查：高风险"
```

**4. 分支保护（Settings → Branches）**

- 合入前必须经过 PR。
- 合入前必须通过你们现有的 CI 检查。不必换成 GitHub Actions，Jenkins 等外部 CI 报回的状态一样可以设为必过。
- 先不要把 AI 审查设成必过检查：它只提意见。等跑了两周、误报够低再考虑。
- 想强制高风险目录有人审：把 `CODEOWNERS.example` 改名为 `CODEOWNERS`，并打开“Require review from Code Owners”。

## 日常怎么用

1. 拿到任务：`/restate <需求>`，确认它的理解和要拍板的事。
2. 修 bug：`/repro`，看到测试失败、确认抓对了问题，再让它改。
3. 一次一步，每一步看证据。同一个问题 3 轮还不对，换思路。
4. 提交前：**新开一个对话**，运行 `/selfcheck`。
5. 写 PR 描述：`/pr-desc <任务号>`。
6. PR 打开后，AI 审查会自动发一条评论。人只看低分项、它列出的证据和“给人审的人”那一节，再按风险标签决定几个人审。
7. 每次纠正 AI，就把规则加到 `.claude/team-workflow.md` 最后一节。

## 成本

- 同一个 PR 连续推送时，只保留最新一次审查；草稿 PR 不审。
- `--max-turns 30` 限制每次审查的步数。可以在 `claude_args` 里加 `--model` 换成更便宜的模型。
- 第一周先看 Anthropic 控制台里的实际花费，算出平均每个 PR 多少钱，再决定推广到哪些仓库。

## 已知限制

- 从 fork 来的 PR 拿不到仓库密钥，不会触发审查。
- 机器人（例如 dependabot）开的 PR 默认不审；需要的话，在 action 里加 `allowed_bots`。
- 没有仓库写权限的人开的 PR 默认不审。
- 这个 v0 的工作流还没有在真实的 GitHub 仓库里跑过。第一次接入时，请开一个小 PR 试一下，确认评论和标签都正常。

## 和任务平台对接（下一步）

- PR 描述里有任务号，任务平台可以通过 GitHub webhook 知道“哪个任务开了 PR”。
- 每条 AI 审查评论末尾都有一段机器可读的 `ai-review-result`。任务平台在收到 `issue_comment` 事件时，用 `scripts/parse-ai-review.py` 取出分数、风险和问题列表，更新任务状态，并按 `reviewers_needed` 分派审查人。
- 再下一步：把任务平台做成 MCP 服务，让 agent 自己领任务、回写复述和证据。
