# agentkit (v0.4.0)

[中文](README.md)

One generic kit, plus one config file per repository, `agentkit.toml`. It turns the workflow "a person says what done means → a person confirms → AI writes → someone else verifies → people are brought in by risk" into task cards, commands and gates, **without tying you to any one agent**: Claude Code, Codex, Kimi, Grok, DeepSeek, or a bot that can only chat all follow the same process.

**Two things it does not do**: it is not an agent (writing and reviewing code is left to the agents or models you choose), and it is not a task tracker (task cards are a place for the work to live until a tracker is connected; later they can move into one).

## Two layers: one standard, each repository's own config

**One standard**: the same for every project and every agent, and checked by the tool.

- **Task card format**: the three questions about the requirement (who needs it / what it solves / done when), the sign-off (only a person fills it in, with a requirement fingerprint), decisions for people, steps, handoff, log.
- **Workflow**: restate first → a person confirms → reproduce first → one step at a time → evidence from the tools → hand off before you stop; 3 rounds at most on the same problem.
- **Commands**: `agentkit task / prompt / checks / review / gate`. Every agent goes through these commands to follow the workflow.
- **Review result format**: `ai-review-result`; issues are Important / Nit / Pre-existing and marked `confirmed`.
- **Gate rules**: path rules only ever raise the risk; only confirmed Important issues block merging; still blocked in round 3 means a person steps in; a PR cannot change its own gate.
- **Three CI checks**: `agentkit / task`, `agentkit / checks`, `agentkit / review`.

**Each repository's own config**: in `agentkit.toml`, decided by each project and team.

- **Which agent writes code**: any. Entry-point files go in `[agents]`.
- **Project facts**: check commands, protected paths, high-risk paths, how many people review at each risk level.
- **Task cards**: where they live and whether PRs must have them (`[tasks]`).
- **Who reviews**: which reviewers run locally and in CI, how many, and how many must agree (`[review]`). A reviewer can be a local CLI, any compatible API, Claude on GitHub, or a third-party review bot.
- **Where it runs**: locally, GitHub Actions or another CI, with the same commands.
- **Which language**: Chinese (the default) or English (`[kit] language`); see [Language](#language) below.

## How each kind of agent keeps to the workflow

| What the agent can do | How the workflow holds |
|---|---|
| Has hooks (Claude Code) | Blocked locally: no code changes before the requirement is confirmed; it cannot fill in the sign-off; before it stops it must run the checks and update the handoff |
| Reads AGENTS.md and runs commands | Follows the workflow in AGENTS.md; the git pre-commit hook stops unconfirmed commits; the PR checks catch the rest |
| Can only chat (a web chat, a bot) | A person runs `prompt start` and the like and pastes the output to it; the PR checks catch the rest |

Most coding agents (Codex, Cursor and others) read `AGENTS.md` on their own. For one that does not, add the file it reads to `[agents] instruction_files` and the kit writes a short note there pointing to AGENTS.md. Models like DeepSeek, Kimi and Grok follow the same rules in any of these tools.

## Task cards

Chat history gets lost: a new session, a new agent or a new person, and the requirement and decisions that were settled are gone. Task cards keep them in the repository, committed with the code.

```markdown
# WIP-123 Tell people when an item is out of stock

## Requirement
- Who needs it: people placing orders
- What it solves: an order for an out-of-stock item returns 500 today
- Done when: it returns 409 with an "out of stock" message; covered by a test
- Confirmed by: Alice, 2026-11-01 (requirement fingerprint 1a2b3c4d)

## Decisions for people
- [x] 409 or 400 → 409 (product, decided in chat, 11-01)
- [ ] With several items, fail the whole order or drop the missing ones? (suggestion: fail the whole order)

## Steps
1. [done] Check stock in the order API, with tests
2. [doing] Show the message in the web app
3. [todo] Several items (waiting on the decision above)

## Handoff
- Progress: step 2; the API change is merged
- Next step: handle 409 on the checkout page
- Tried and failed: a database constraint locks the table, dropped

## Log
- 2026-11-01 18:20 · Alice · Codex · feat/WIP-123@a1b2c3d · steps done 1/3 · next: handle 409 on the checkout page
```

| Command | Who uses it | What it does |
|---|---|---|
| `task new <task-id> <title>` | a person or an agent | creates a task card and makes it the current task |
| `task show [--brief]` | an agent, first thing | shows the card and its status: what is missing, who confirmed it, what is still undecided, the next step |
| `task confirm` | **a person only** | after reading the requirement, confirms it and records who, when and the requirement fingerprint. If the requirement changes afterwards, the sign-off is void |
| `task handoff --agent <name>` | an agent, before it stops | checks the Handoff is written and appends a line to the Log |
| `task list` / `task use <task-id>` | a person or an agent | lists the tasks, switches the current task |
| `task check` | CI | is there a task card, is the requirement confirmed, was the handoff updated |

The current task is found in this order: set by `task use` or `task new`, the task id in the branch name, the task card changed on this branch; in CI also the task id in the PR description.

`task confirm` does not run in an agent's tools: it wants a y typed into a real terminal, and Claude Code's permission rules deny it as well.

## Reviews: who reviews is up to you

`agentkit review` has the configured reviewers each open a fresh session and review the same change at the same time. Every reviewer gets the same material (project rules, review format, task card, check results, the full diff) and none sees the others; the results are merged by fixed rules: the highest risk, the lowest scores, and every issue kept with who raised it.

```toml
[review]
local = ["claude", "claude", "codex", "deepseek"]   # listing one twice = two independent sessions
ci = ["claude-action", "deepseek"]
agree = 2          # an Important issue blocks merging only when 2 reviewers raise it at the same spot and confirm it

[review.engines.deepseek]
type = "api"
format = "openai"                  # openai or anthropic
base_url = "https://api.deepseek.com"
model = "model name"
key_env = "DEEPSEEK_API_KEY"
```

| Type | What it is | Examples |
|---|---|---|
| `command` | any local CLI that reads the prompt on stdin and prints the result on stdout (or writes it to `{output}`) | built-in `claude`, `codex`; CLIs for Kimi, Grok and others can be configured |
| `api` | any model with an OpenAI- or Anthropic-compatible API, called by the kit directly; no agent to install | DeepSeek, Kimi, Grok, OpenRouter, a local vLLM / Ollama |
| `github-action` | Claude Code on GitHub (a step in the workflow) | built-in `claude-action` |
| `github-comments` | a third-party review bot's comments on the PR, attached **for reference only, not part of the gate** | `author = "coderabbitai[bot]"` |

A local review is written to `.agentkit/review.local.md`, with a local estimate of the verdict. CI merges the same way, then `gate` finalizes it, posts the comment and sets the labels.

## Install in a repository

```bash
git clone https://github.com/Shen-Yukang/agentkit.git ~/tools/agentkit
python3 ~/tools/agentkit/agentkit.py init /path/to/repo --language en
```

Needs Python 3.9 or later (the python3 that comes with macOS will do) and no third-party packages. Have a person read the generated `agentkit.toml`: check commands, protected and high-risk paths, `[tasks] required` (enforce or only remind), and the reviewers in `[review]`.

Upgrade: update the kit, then run `init` again. It only overwrites the kit's own files.

```bash
git -C ~/tools/agentkit pull && python3 ~/tools/agentkit/agentkit.py init /path/to/repo
```

`.agentkit/VERSION` records the kit version, the commit it came from and the script's sha256, so you can check which version a repository has.

| File | Belongs to | On upgrade |
|---|---|---|
| `agentkit.toml` | the project | untouched; it holds only facts and choices |
| `AGENTS.md` | the project | only the marked "Working with people (agentkit)" section is replaced; created if missing |
| `CLAUDE.md` and the files listed in `[agents]` | the project | only the import lines or a short note pointing to AGENTS.md are added |
| `.claude/settings.json` | the project | only the kit's hooks and permission rules are added |
| `docs/tasks/*.md` (task cards) | the project | untouched |
| `.agentkit/`: script, prompts, task card template, review format, conventions (in the `[kit] language`) | the kit | overwritten |
| the 6 commands in `.claude/commands/` | the kit | overwritten (files with the same name that the kit did not generate are skipped) |
| `.github/workflows/agentkit.yml` | the kit | overwritten |
| the git pre-commit check | the repository or each clone | if the repository ships its own hooks (`.githooks/`, `.husky/` or `core.hooksPath`), one line is added to its pre-commit, enabled the repository's way; otherwise it goes into this clone's `.git/hooks/pre-commit`, and everyone else runs `hook install-git` |

## Language

Everything the kit says and writes into a repository (command messages and errors, hook messages, the review comment and verdict on the PR, the `agentkit.toml` that `init` generates, its section of AGENTS.md, the prompts, the task card template, the conventions, the review format, the workflow) comes in Chinese and in English. Each repository picks one in `agentkit.toml`:

```toml
[kit]
language = "en"   # or "zh"; Chinese when this is not set
```

- **A new repository in English**: `python3 ~/tools/agentkit/agentkit.py init /path/to/repo --language en`. The generated `agentkit.toml` carries `[kit] language = "en"`. `--language` only applies when `agentkit.toml` is created.
- **Switching a repository that already has the kit**: change `[kit] language` in `agentkit.toml`, then run `init` again. The kit's own files (templates and prompts under `.agentkit/`, `.claude/commands/`, the workflow) and the kit's section of AGENTS.md switch to the new language; the project's own content is left alone. `agentkit.toml` belongs to the project, so `init` never changes it for you: when it disagrees with `--language`, `init` does nothing and tells you which line to change.
- **Task cards are read in both languages**: headings and fields (`需求` / `Requirement`, `满足谁` / `Who needs it`, `解决什么` / `What it solves`, `怎么算做完` / `Done when`, `确认` / `Confirmed by`, `要人拍板的事` / `Decisions for people`, `拆分` / `Steps`, `交接` / `Handoff`, `做到` / `Progress`, `下一步` / `Next step`, `试过不行的` / `Tried and failed`, `记录` / `Log`) are recognized whichever language the repository uses, the English names in any case. After a switch, existing Chinese cards can still be read, confirmed and handed off; the requirement fingerprint is computed exactly as before, so cards that were already confirmed stay confirmed.
- **What agentkit writes into a card follows the card's language**: the sign-off line from `task confirm` and the log line from `task handoff` are written in the card's own language (`Confirmed by: Alice, 2026-11-01 (requirement fingerprint …)` in an English card, `确认：张三，2026-11-01（需求指纹 …）` in a Chinese one); new cards come from the template in the repository's current language.
- **The review result's JSON is the same in both languages**: the keys and values of `ai-review-result` (`requirement`, `evidence`, `risk` and `readability` under `scores`; `low` / `medium` / `high` for `risk_level`; `important` / `nit` / `pre-existing` for each issue's `severity`, and so on) are a machine contract and never translated.

Upgrading from 0.3 to 0.4: a repository without `[kit] language` stays in Chinese, and every output and generated file is exactly what 0.3.1 produced (a test compares them word for word); task cards that were already confirmed stay confirmed.

## Commands

Every command is `python3 .agentkit/agentkit.py <command>`.

| Command | What it does |
|---|---|
| `task …` | see Task cards above |
| `prompt [name] [notes]` | prints a workflow prompt: start, restate, repro, verify, selfcheck, pr-desc, handoff. The current task card is attached, so it can be pasted straight to any agent or chatbot |
| `context [--for review]` | prints this repository's configuration for an agent |
| `checks [--base REF] [--evidence FILE] [--committed-only]` | runs the checks for the changed paths and prints the evidence table |
| `review [--engines a,b] [--print-prompt]` | independent reviews by several reviewers, merged |
| `risk [--base REF]` | the risk floor from the changed paths |
| `validate FILE` | checks the format of a review result |
| `gate --review FILE --pr N --base SHA --head SHA` | finalizes a review result, posts it, sets labels, passes or fails |
| `hook pre-edit / stop / session-start` | Claude Code hooks |
| `hook pre-commit / install-git` | the git pre-commit hook, for every agent and person |

In Claude Code there are also `/restate`, `/repro`, `/verify`, `/selfcheck`, `/pr-desc` and `/handoff`, with the same text as `prompt`.

## Turning it on in GitHub

1. Add the repository secrets for the reviewers in `[review] ci`: the default `claude-action` needs `ANTHROPIC_API_KEY` and the Claude GitHub App; for `api` reviewers add the secret named in `key_env`. The workflow already passes `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY`, `XAI_API_KEY` and `OPENROUTER_API_KEY`.
2. Open a small PR and check that all three checks run and the comment and labels appear.
3. Make the three checks required in branch protection.

You do not need to replace your existing CI. Without GitHub, run `task check`, `checks`, `review --ci` and `gate` in your CI, in that order.

Free private repositories cannot make checks required, so the CI results only remind; what really stops things is the local hooks and human review. You can also skip review in CI: run `review` locally (with the Claude Code or Codex you are signed in to, no API credit spent), then `gate --pr N` posts the verdict as a PR comment and sets the `risk:*` label.

**A PR cannot change its own gate**: CI first replaces `.agentkit/` and `agentkit.toml` with the versions on the base branch, then runs the checks and the gate. A PR that changes the config only takes effect for later PRs once merged, and the change itself counts as high risk.

## What it stops and what it does not

- **Stops**: a PR with no task card, a requirement nobody confirmed, or a requirement quietly changed after it was confirmed (the fingerprint no longer matches) fails on the PR; in Claude Code these are stopped before any code is touched.
- **Only reminds**: a handoff that was not updated, decisions still open; reminders on the PR, not blocks.
- **Does not stop**: an agent getting around the hooks with shell commands, skipping git hooks with `--no-verify`, or filling in someone else's sign-off in the task card. The kit guards against "deciding for people in passing", not deliberate forgery. In the end the real sign-off belongs on a task tracker's confirm button or in human review on the PR.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

`tests/golden/default-zh.txt` is the output of 0.3.1 running the same session of commands (`transcript()` in `tests/test_agentkit.py`); the tests compare against it word for word to make sure the default Chinese output and generated files have not changed. The same session in English must print and write no Chinese.

## Known limits

- The AI review on GitHub has not yet run end to end in a real repository: trial runs stopped at API credit.
- Reviewers sometimes do not output the result block in the right format (inside a code block, or with score names translated), and the kit counts that as not having run; spelling out the format in the review instructions avoids it, and a more forgiving parser is planned.
- `gate` counts rounds only from comments posted by `github-actions[bot]`; local review rounds posted from a personal account do not count, so give the round by hand.
- `api` reviewers answer in a single request and cannot run commands to check an issue, so most issues they report are "unconfirmed"; reviewers that can run commands (local CLIs, Claude on GitHub) check more thoroughly.
- PRs from forks get no secrets, so the review does not run.
- Install steps such as `npm ci` belong in the check commands; the workflow itself only sets up Python.

## Next

- Package it as a Claude Code plugin and a reusable GitHub workflow: one agentkit repository per team releases versions, and each project keeps only `agentkit.toml`.
- Connect a task tracker: task cards live in the tracker, and the sign-off becomes a button there.
- A "red first" check: tests added in a PR must fail on the old code.
- Weekly metrics: share of useful comments, false positives, reverts, average rounds.
