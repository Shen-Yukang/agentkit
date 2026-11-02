# agentkit（v0.3）

一套通用的 kit，加上每个仓库一份配置 `agentkit.toml`。它把“人说清什么算做对 → 人确认 → AI 写 → 另一方验证 → 按风险找人”这条流程落成任务卡、命令和门禁，**不绑定哪一家 agent**：Claude Code、Codex、Kimi、Grok、DeepSeek，或者只能聊天的机器人，都走同一套。

**它不做的两件事**：不做 agent（写代码、审代码都交给你选的 agent 或模型）；不做任务管理平台（任务卡是平台接入前的落脚点，以后可以换成存在平台里）。

## 两层：统一的标准，和各自的配置

**统一的标准**：所有项目、所有 agent 都一样，由工具检查。

- **任务卡格式**：需求三问（满足谁 / 解决什么 / 怎么算做完）、确认（只能由人填，带需求指纹）、要人拍板的事、拆分、交接、记录。
- **流程**：先复述 → 人确认 → 先重现 → 一次一步 → 用工具跑出证据 → 收工交接；同一个问题 3 轮上限。
- **命令**：`agentkit task / prompt / checks / review / gate`。任何 agent 都通过这几个命令和流程打交道。
- **审查结果格式**：`ai-review-result`；问题分 Important / Nit / Pre-existing，标 `confirmed`。
- **门禁规则**：风险按路径只升不降；只有已确认的 Important 阻断合入；第 3 轮还有阻塞问题就找人；PR 不能改自己的门禁。
- **CI 的三项检查**：`agentkit / task`、`agentkit / checks`、`agentkit / review`。

**各自的配置**：写在 `agentkit.toml`，每个项目、每个团队自己定。

- **用哪个 agent 写代码**：任意。入口文件写在 `[agents]`。
- **项目事实**：检查命令、受保护路径、高风险路径、各风险等级要几个人审。
- **任务卡**：放在哪、PR 上是否强制（`[tasks]`）。
- **谁来审查**：本地和 CI 各用哪些审查者、几位、几位都提出才算数（`[review]`）。审查者可以是本地的 CLI、任何兼容的 API、GitHub 上的 Claude，或第三方审查机器人。
- **在哪跑**：本地、GitHub Actions、别的 CI 都行，命令一样。

## 不同的 agent 怎么守流程

| agent 能做什么 | 怎么守流程 |
|---|---|
| 有钩子（Claude Code） | 本地直接拦：需求没确认不能改代码；不能自己填「确认」；收工前要跑检查、更新交接 |
| 能读 AGENTS.md、能运行命令 | 读 AGENTS.md 里的流程；git 提交前的钩子拦住未确认的提交；PR 上的检查兜底 |
| 只能聊天（网页版、机器人） | 人运行 `prompt start` 等，把输出贴给它；PR 上的检查兜底 |

多数 coding agent（Codex、Cursor 等）会自动读 `AGENTS.md`。不读的，把它自己读的文件名加进 `[agents] instruction_files`，kit 会在那个文件里写一段指向 AGENTS.md 的说明。DeepSeek、Kimi、Grok 这类模型接在上面任何一种工具里用，规则都一样。

## 任务卡

聊天记录会丢：换个会话、换个 agent、换个人，前面说清的需求和拍板就没了。任务卡把这些记在仓库里，和代码一起提交。

```markdown
# WIP-123 下单时库存不足要提示

## 需求
- 满足谁：下单的用户
- 解决什么：库存不足时现在返回 500
- 怎么算做完：返回 409 和“库存不足”提示；有测试覆盖
- 确认：张三，2026-11-01（需求指纹 1a2b3c4d）

## 要人拍板的事
- [x] 用 409 还是 400 → 409（产品，对话里定的，11-01）
- [ ] 多件商品时整单失败，还是只去掉缺货的？（建议：整单失败）

## 拆分
1. [done] 下单接口检查库存，加测试
2. [doing] 前端显示提示
3. [todo] 多件商品的情况（等上面拍板）

## 交接
- 做到：第 2 步，接口已合入
- 下一步：在结账页处理 409
- 试过不行的：在数据库层加约束，会锁表，放弃

## 记录
- 2026-11-01 18:20 · 张三 · Codex · feat/WIP-123@a1b2c3d · 拆分完成 1/3 · 下一步：在结账页处理 409
```

| 命令 | 谁用 | 做什么 |
|---|---|---|
| `task new <任务号> <标题>` | 人或 agent | 新建任务卡，设为当前任务 |
| `task show [--brief]` | agent 开工先运行 | 显示任务卡和状态：缺什么、谁确认的、还有什么没拍板、下一步 |
| `task confirm` | **只能由人** | 看过需求后确认，记下是谁、哪天、需求指纹。之后需求再改，确认自动失效 |
| `task handoff --agent <名字>` | agent 收工前 | 检查「交接」写了，并在「记录」里追加一行 |
| `task list` / `task use <任务号>` | 人或 agent | 看所有任务、切换当前任务 |
| `task check` | CI | 有没有任务卡、需求有没有人确认、交接有没有更新 |

当前任务按这个顺序找：`task use` 或 `task new` 设置的、分支名里的任务号、这个分支改过的任务卡；在 CI 里还会看 PR 描述里的任务号。

`task confirm` 不会在 agent 的工具里运行：它要求在真正的终端里输入 y，在 Claude Code 里还被权限规则禁止。

## 审查：谁来审可以换

`agentkit review` 让配置好的审查者各开一个新会话，同时审同一次改动。每位拿到的材料一样（项目规则、审查格式、任务卡、检查结果、完整 diff），互相看不到，最后按统一规则合并：风险取最高，分数取最低，问题全部保留并标上是谁提的。

```toml
[review]
local = ["claude", "claude", "codex", "deepseek"]   # 同一个写两次 = 开两个独立会话
ci = ["claude-action", "deepseek"]
agree = 2          # 同一处 Important 要 2 位审查者都提出并确认，才阻断合入

[review.engines.deepseek]
type = "api"
format = "openai"                  # openai 或 anthropic
base_url = "https://api.deepseek.com"
model = "填模型名"
key_env = "DEEPSEEK_API_KEY"
```

| 类型 | 是什么 | 例子 |
|---|---|---|
| `command` | 本地任何 CLI：从标准输入读提示词，结果输出到标准输出（或写进 `{output}`） | 内置 `claude`、`codex`；也可以配 Kimi、Grok 等的 CLI |
| `api` | 任何兼容 OpenAI 或 Anthropic 接口的模型，kit 直接调用，不需要装 agent | DeepSeek、Kimi、Grok、OpenRouter、本地 vLLM / Ollama |
| `github-action` | GitHub 上的 Claude Code（工作流里的一步） | 内置 `claude-action` |
| `github-comments` | 第三方审查机器人在 PR 上的意见，附在评论里，**仅供参考，不进门禁** | `author = "coderabbitai[bot]"` |

本地审查结果写在 `.agentkit/review.local.md`，并给出本地预判。CI 里同样合并，再由 `gate` 定稿、发评论、加标签。

## 装到一个仓库

```bash
python3 agentkit.py init /path/to/repo
```

需要 Python 3.9 或以上（macOS 自带的 python3 就行），不依赖第三方包。升级就是再运行一次 `init`。

| 文件 | 归谁 | 升级时 |
|---|---|---|
| `agentkit.toml` | 项目 | 不动。只写事实和选择 |
| `AGENTS.md` | 项目 | 只替换带标记的「和人协作的流程（agentkit）」一节，其余不动；没有就新建 |
| `CLAUDE.md` 和 `[agents]` 里列出的文件 | 项目 | 只补引入行或一段指向 AGENTS.md 的说明 |
| `.claude/settings.json` | 项目 | 只补 kit 的钩子和权限限制 |
| `docs/tasks/*.md`（任务卡） | 项目 | 不动 |
| `.agentkit/`：脚本、提示词、任务卡模板、审查格式、协作约定 | kit | 覆盖 |
| `.claude/commands/` 下的 6 个命令 | kit | 覆盖（同名但不是 kit 生成的会跳过） |
| `.github/workflows/agentkit.yml` | kit | 覆盖 |
| `.git/hooks/pre-commit` | 每个克隆 | 没有才装；仓库用了自己的钩子目录时，提示加一行。其他人运行 `hook install-git` |

从 0.2 升级：`agentkit.toml` 不会被改，所以没有 `[tasks]` 时任务卡检查只提醒、不拦 PR；init 会提示怎么打开。

## 命令

所有命令都是 `python3 .agentkit/agentkit.py <命令>`。

| 命令 | 做什么 |
|---|---|
| `task …` | 见上面的任务卡一节 |
| `prompt [名字] [补充说明]` | 打印流程提示词：start、restate、repro、verify、selfcheck、pr-desc、handoff。会附上当前任务卡，可以直接贴给任何 agent 或聊天机器人 |
| `context [--for review]` | 打印本仓库的配置，给 agent 读 |
| `checks [--base REF] [--evidence FILE] [--committed-only]` | 按改动路径跑检查，输出证据表 |
| `review [--engines a,b] [--print-prompt]` | 多位审查者独立审查并合并 |
| `risk [--base REF]` | 按路径给出风险下限 |
| `validate FILE` | 检查审查结果的格式 |
| `gate --review FILE --pr N --base SHA --head SHA` | 定稿审查结果、发评论、加标签、给出通过或不通过 |
| `hook pre-edit / stop / session-start` | Claude Code 钩子 |
| `hook pre-commit / install-git` | git 提交前的钩子，对所有 agent 和人都生效 |

Claude Code 里还有 `/restate`、`/repro`、`/verify`、`/selfcheck`、`/pr-desc`、`/handoff`，内容和 `prompt` 是同一份。

## 在 GitHub 上启用

1. 按 `[review] ci` 里的审查者添加仓库密钥：默认的 `claude-action` 需要 `ANTHROPIC_API_KEY` 并安装 Claude GitHub App；`api` 类型按 `key_env` 添加，工作流里预留了 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY`、`XAI_API_KEY`、`OPENROUTER_API_KEY`。
2. 开一个小 PR，确认三项检查都运行、评论和标签都出现。
3. 分支保护里把三项设为必须通过。

不需要换掉现有的 CI。不用 GitHub 的话，在你们的 CI 里依次运行 `task check`、`checks`、`review --ci`、`gate` 即可。

**PR 不能改自己的门禁**：CI 先把 `.agentkit/` 和 `agentkit.toml` 换成 base 分支上的版本，再运行检查和门禁。改配置的 PR 只有合入之后才对后面的 PR 生效，改动本身按高风险处理。

## 防得住什么，防不住什么

- **防得住**：PR 没有任务卡、需求没人确认、确认后又偷偷改了需求（指纹对不上），都会在 PR 上不通过；在 Claude Code 里，这些在动手前就被拦住。
- **只能提醒**：交接没更新、还有事没拍板，在 PR 上给出提醒，不拦。
- **防不住**：agent 用 shell 命令绕过钩子、用 `--no-verify` 跳过 git 钩子、在任务卡里冒名填写确认。kit 防的是“顺手替人做了决定”，不防故意作假。真正的确认最终应该落在任务平台的确认按钮或 PR 上的人工审查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 已知限制

- GitHub 上的 AI 审查还没在真实仓库里完整跑通过：试跑卡在 API 额度上。
- `codex` 审查者的命令行参数（`--sandbox read-only`、`--output-last-message`）按文档写成，还没在真机上验证。
- `api` 审查者一次请求完成，不能自己运行命令验证问题，所以它报的问题大多是“待确认”；能运行命令的审查者（本地 CLI、GitHub 上的 Claude）验证得更充分。
- 从 fork 来的 PR 拿不到密钥，审查不会运行。
- `npm ci` 这类安装步骤需要写在检查命令里；工作流本身只准备了 Python。

## 下一步

- 打包成 Claude Code 插件和 GitHub 可复用工作流；团队一个 agentkit 仓库发版本，各项目只留 `agentkit.toml`。
- 对接任务平台：任务卡改成存在平台里，确认变成平台上的按钮。
- “先红”检查：PR 新增的测试放到旧代码上必须失败。
- 每周指标：有用意见比例、误报、撤回、平均轮数。
