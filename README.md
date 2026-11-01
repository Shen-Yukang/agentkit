# agentkit（v0.2）

一套通用的 kit，加上每个仓库一份配置 `agentkit.toml`。它把“人说清什么算做对 → AI 写 → 另一方验证 → 按风险找人”这条流程，落成仓库里的门禁和证据。

**它不做的两件事**：不做审查引擎（审查由 Claude Code 完成，也可以换成现成的审查产品）；不做任务管理（那是任务平台的事）。它负责的是中间这层：配置、门禁、证据。

## 一条原则：能检查的用工具，要判断的用提示词

| 规则 | 放在哪 | 强制程度 |
|---|---|---|
| 改动对应的检查都要跑、要通过 | CI 的 `agentkit / checks`，按改动路径挑检查 | 必须通过 |
| AI 审查要独立、要有证据 | CI 的 `agentkit / review`：新开的 AI 审查，结果由工具定稿、发评论 | 有已确认的 Important 就不通过 |
| 风险由路径决定，AI 只能往高调 | `agentkit.toml` 的高风险路径 → `risk:*` 标签和建议人审人数 | 工具判定 |
| 改门禁本身算高风险 | kit 默认：`.github/workflows/**`、`agentkit.toml`、`.agentkit/**`、`.claude/settings.json` | 工具判定 |
| 第 3 轮还有阻塞问题就找人 | 工具数轮数，加 `needs-human` 标签 | 工具判定 |
| 受保护路径 AI 不能改 | Claude Code 钩子（编辑前拦截） | 本地拦截 |
| 结束一轮前检查要过 | Claude Code 钩子（结束前跑必跑的检查，没过就让它接着修） | 本地拦截 |
| 证据用工具跑出来，不手写 | `agentkit checks` 生成证据表；`/verify`、`/pr-desc` 直接贴它 | 工具生成 |
| 先复述、先重现、一次一步、3 轮上限 | `/restate`、`/repro` 和 `.agentkit/team-workflow.md` | 提示词（需要判断） |

## 装到一个仓库

```bash
python3 agentkit.py init /path/to/repo
```

需要 Python 3.11 或以上，不依赖第三方包。会写入这些文件：

| 文件 | 归谁 | 升级时 |
|---|---|---|
| `agentkit.toml` | 项目 | 不动。只写事实：检查命令、规则文件、受保护和高风险路径 |
| `CLAUDE.md` | 项目 | 只补引入行。“被纠正过的规则”写在这里 |
| `.claude/settings.json` | 项目 | 只补 kit 的钩子和读取限制，不动其他设置 |
| `.agentkit/agentkit.py`、`review-format.md`、`team-workflow.md` | kit | 覆盖 |
| `.claude/commands/` 下的 5 个命令 | kit | 覆盖（同名但不是 kit 生成的文件会跳过） |
| `.github/workflows/agentkit.yml` | kit | 覆盖 |

升级就是再运行一次 `init`。init 会从 Makefile、package.json、tests 目录猜出检查命令，从 AGENTS.md、CONTRIBUTING.md、REVIEW.md 猜出规则文件；生成的 `agentkit.toml` 请人看一遍。

## agentkit.toml

```toml
version = 1

[rules]
always = ["AGENTS.md"]                    # 写代码时每次都读（引入 CLAUDE.md）
review = ["REVIEW.md", "CONTRIBUTING.md"] # 审查时额外读；和 kit 的格式冲突时以它们为准

[checks]
always = ["make check"]                   # 每次都跑；也是结束前钩子跑的检查
timeout_seconds = 900

[[checks.by_path]]                        # 改到这些路径才加跑
paths = ["web/**"]
run = ["npm ci --prefix web && npm test --prefix web"]

[paths]
protected = ["vendor/shared-contracts/**"]  # AI 不能改；PR 改到就是高风险
high_risk = ["REVIEW.md", "deployment/"]    # 改到就至少高风险（kit 默认的门禁路径会自动加上）

[risk]
reviewers = { low = 0, medium = 1, high = 2 }  # 各风险等级建议几个人审

[requirements]
source = "pr_body"                        # 需求写在哪：pr_body；pmm（后续）

[hooks]
check_on_stop = true                      # 结束前自动跑 checks.always

[review]
trusted_authors = ["github-actions[bot]"] # 只认这些账号发的审查结果（用来数轮数）
```

路径写法：`dir/**` 匹配目录下所有文件；以 `/` 结尾表示目录；不带通配符的写法匹配同名文件或同名目录。

## 命令

| 命令 | 做什么 |
|---|---|
| `agentkit.py context [--for review]` | 打印本仓库的配置，给 AI 读 |
| `agentkit.py checks [--base REF] [--evidence FILE] [--committed-only]` | 按改动路径跑检查，输出证据表；有失败就返回非零。`--committed-only` 只看已提交的改动（CI 用） |
| `agentkit.py risk [--base REF]` | 按路径给出风险下限 |
| `agentkit.py validate FILE` | 检查审查结果的格式 |
| `agentkit.py gate --review FILE --pr N --base SHA --head SHA` | 定稿审查结果（轮次、head、最终风险、建议人数），发评论、加标签，按阻塞问题给出通过或不通过。加 `--dry-run` 只在本地预览 |
| `agentkit.py hook pre-edit / stop` | Claude Code 钩子 |

Claude Code 里的命令：`/restate`、`/repro`、`/verify`、`/selfcheck`（请在新对话里运行）、`/pr-desc`。

## 在 GitHub 上启用

1. 安装 Claude GitHub App，在仓库密钥里添加 `ANTHROPIC_API_KEY`。
2. 开一个小 PR，确认 `agentkit / checks` 和 `agentkit / review` 都正常运行、评论和标签都出现。
3. 分支保护里把这两项设为必须通过。

不需要换掉现有的 CI：agentkit 的工作流和你们原有的工作流并存。

**PR 不能改自己的门禁**：CI 里两个任务都会先把 `.agentkit/` 和 `agentkit.toml` 换成 base 分支上的版本，再运行检查和门禁。一个 PR 改了配置或脚本，只有合入之后才对后面的 PR 生效；而改动本身按高风险处理。第一次安装 kit 的那个 PR 例外（base 上还没有 kit），工作流会给出警告。

## 审查结果的格式

AI 按 `.agentkit/review-format.md` 输出，问题分级和 Claude Code Review 一致：Important、Nit、Pre-existing；没验证的问题 `confirmed: false`，不会阻断。评论末尾有一段机器可读的 `ai-review-result`，任务平台可以用它更新任务状态。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 已知限制

- GitHub 上的工作流还没在真实仓库里跑过。“数轮数只认 `github-actions[bot]`”依赖评论由工作流里的步骤发出，这一点要在第一次试跑时确认。
- 钩子用模拟输入测过，还没在真正的 Claude Code 里触发过。
- 从 fork 来的 PR 拿不到密钥，审查不会运行。
- `npm ci` 这类安装步骤需要写在检查命令里；工作流本身只准备了 Python。

## 下一步

- v0.3：“先红”检查（PR 新增的测试放到旧代码上必须失败）；根据高风险路径自动生成 CODEOWNERS；打包成 Claude Code 插件和 GitHub 可复用工作流，仓库里只剩配置文件。
- v0.4：对接任务平台（MCP 服务和 webhook）；每周看指标：有用意见比例、误报、撤回、平均轮数。
