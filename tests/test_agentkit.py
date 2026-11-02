import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
SCRIPT = KIT / "agentkit.py"
spec = importlib.util.spec_from_file_location("agentkit", SCRIPT)
ak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ak)


def config(**overrides):
    cfg = {"rules_always": [], "rules_review": [], "checks_always": [], "checks_by_path": [], "timeout": 60,
           "protected": [], "high_risk": list(ak.DEFAULT_HIGH_RISK), "reviewers": {"low": 0, "medium": 1, "high": 2},
           "requirements": "pr_body", "check_on_stop": True, "trusted_authors": ["github-actions[bot]"],
           "tasks_dir": "docs/tasks", "tasks_required": True, "block_before_confirm": True,
           "instruction_files": ["CLAUDE.md"], "review_local": ["claude"], "review_ci": ["claude-action"],
           "review_agree": 1, "review_ci_setup": [], "engines": dict(ak.DEFAULT_ENGINES)}
    cfg.update(overrides)
    return cfg


def review(risk="low", issues=()):
    return {"scores": {"requirement": 5, "evidence": 4, "risk": 4, "readability": 5},
            "risk_level": risk, "issues": list(issues)}


def issue(severity="important", confirmed=True):
    return {"file": "a.py", "line": 1, "severity": severity, "confirmed": confirmed, "summary": "问题"}


def as_comment(result):
    return "## AI 审查\n\n正文\n\n<!-- ai-review-result\n" + json.dumps(result, ensure_ascii=False) + "\n-->\n"


def env_without_hook_dir():
    env = os.environ.copy()
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


class TempRepo:
    """A throwaway git repository with an initial commit on main."""

    def __init__(self, files):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name).resolve()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True).stdout

    def write(self, files):
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def run(self, *args, stdin="", env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.root, input=stdin,
                              capture_output=True, text=True, env=env or env_without_hook_dir())

    def close(self):
        self._dir.cleanup()


def toml_config(**sections):
    lines = []
    for section, values in sections.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


class TomlReaderTest(unittest.TestCase):
    SAMPLE = ('version = 1\n[rules]\nalways = ["AGENTS.md"] # c\n[checks]\nalways = [\n  "make check",\n]\n'
              '[[checks.by_path]]\npaths = ["web/**"]\nrun = ["npm ci --prefix web && npm test"]\n'
              '[risk]\nreviewers = { low = 0, medium = 1, high = 2 }\n[review.engines.x]\n'
              "run = \'\'\'\"/a b/claude\" -p --allowedTools \"Read\"\'\'\'\n"
              'key = "a\\"b\\u4e2d"\nok = true\nratio = 1.5\n')

    def test_reader_matches_tomllib(self):
        try:
            import tomllib
        except ModuleNotFoundError:
            self.skipTest("Python < 3.11")
        self.assertEqual(ak.parse_toml(self.SAMPLE), tomllib.loads(self.SAMPLE))

    def test_reader_basics(self):
        data = ak.parse_toml(self.SAMPLE)
        self.assertEqual(data["checks"]["by_path"][0]["paths"], ["web/**"])
        self.assertEqual(data["review"]["engines"]["x"]["run"], '"/a b/claude" -p --allowedTools "Read"')
        self.assertEqual(data["review"]["engines"]["x"]["key"], 'a"b中')
        with self.assertRaises(ValueError):
            ak.parse_toml("[a\nb = 1\n")


class MatchingTest(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(ak.matches("src/a/b.py", "src/**"))
        self.assertFalse(ak.matches("src.py", "src/**"))
        self.assertTrue(ak.matches("REVIEW.md", "REVIEW.md"))
        self.assertFalse(ak.matches("docs/REVIEW.md", "REVIEW.md"))
        self.assertTrue(ak.matches("deployment/app.yml", "deployment/"))
        self.assertTrue(ak.matches("contracts/v1.json", "contracts"))
        self.assertTrue(ak.matches("./agentkit.toml", "agentkit.toml"))

    def test_plan_checks_adds_path_checks_once(self):
        cfg = config(checks_always=["make check"],
                     checks_by_path=[{"paths": ["web/**"], "run": ["npm test", "make check"]},
                                     {"paths": ["api/**"], "run": ["make api-test"]}])
        plan = ak.plan_checks(cfg, ["web/a.ts", "web/b.ts", "docs/x.md"])
        self.assertEqual([cmd for cmd, _ in plan], ["make check", "npm test"])
        self.assertIn("web/a.ts 等 2 个文件", plan[1][1])


class ResultFormatTest(unittest.TestCase):
    def test_valid_result(self):
        result, errors = ak.parse_result(as_comment(review(issues=[issue()])))
        self.assertEqual(errors, [])
        self.assertEqual(result["risk_level"], "low")

    def test_requirement_may_be_unknown(self):
        r = review()
        r["scores"]["requirement"] = None
        self.assertEqual(ak.parse_result(as_comment(r))[1], [])

    def test_missing_block(self):
        self.assertTrue(ak.parse_result("## AI 审查\n没有结果段")[1])

    def test_rejects_bad_fields(self):
        r = review(risk="urgent", issues=[{"file": "a.py", "line": "1", "severity": "major", "summary": ""}])
        r["scores"]["evidence"] = 6
        r["scores"]["risk"] = True
        errors = "\n".join(ak.parse_result(as_comment(r))[1])
        for expected in ("scores.evidence", "scores.risk", "risk_level", "line", "severity", "confirmed", "summary"):
            self.assertIn(expected, errors)


class DecisionTest(unittest.TestCase):
    def test_path_floor_raises_risk_but_never_lowers_it(self):
        floor = ak.risk_floor(config(), [".github/workflows/ci.yml"])
        decision = ak.decide(config(), review("low"), floor, 0)
        self.assertEqual(decision["risk_level"], "high")
        self.assertEqual(decision["reviewers_needed"], 2)
        decision = ak.decide(config(), review("medium"), ak.risk_floor(config(), ["src/a.py"]), 0)
        self.assertEqual(decision["risk_level"], "medium")

    def test_gate_config_is_high_risk_by_default(self):
        self.assertEqual(ak.risk_floor(config(), ["agentkit.toml"])["floor"], "high")
        self.assertEqual(ak.risk_floor(config(), [".agentkit/agentkit.py"])["floor"], "high")

    def test_only_confirmed_important_blocks(self):
        floor = ak.risk_floor(config(), [])
        self.assertFalse(ak.decide(config(), review(issues=[issue()]), floor, 0)["passed"])
        unconfirmed = ak.decide(config(), review(issues=[issue(confirmed=False)]), floor, 0)
        self.assertTrue(unconfirmed["passed"])
        self.assertEqual(len(unconfirmed["unconfirmed"]), 1)
        others = review(issues=[issue("nit"), issue("pre-existing")])
        self.assertTrue(ak.decide(config(), others, floor, 0)["passed"])

    def test_third_round_with_blockers_needs_a_human(self):
        floor = ak.risk_floor(config(), [])
        self.assertIn("needs-human", ak.decide(config(), review(issues=[issue()]), floor, 2)["labels"])
        self.assertNotIn("needs-human", ak.decide(config(), review(), floor, 2)["labels"])
        self.assertNotIn("needs-human", ak.decide(config(), review(issues=[issue()]), floor, 1)["labels"])

    def test_rounds_only_count_trusted_authors(self):
        posted = as_comment(review())
        comments = [{"author": "github-actions[bot]", "body": posted},
                     {"author": "someone", "body": posted},
                     {"author": "github-actions[bot]", "body": "普通评论"}]
        self.assertEqual(ak.count_prior_rounds(config(), comments), 1)

    def test_finalize_replaces_the_result_block(self):
        text = as_comment(review("low"))
        floor = ak.risk_floor(config(), ["agentkit.toml"])
        decision = ak.decide(config(), review("low"), floor, 1)
        comment = ak.finalize_comment(text, review("low"), decision, "abcdef1234567")
        self.assertEqual(comment.count("ai-review-result"), 1)
        result, errors = ak.parse_result(comment)
        self.assertEqual(errors, [])
        self.assertEqual((result["head"], result["round"], result["risk_level"], result["ai_risk_level"]),
                         ("abcdef1234567", 2, "high", "low"))
        self.assertIn("上调", comment)
        self.assertIn("`agentkit.toml`", comment)


class ExplainFailureTest(unittest.TestCase):
    BILLING = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Credit balance is too low"}]},
               "error": "billing_error", "is_api_error_message": True, "api_error_status": 400}

    def test_finds_api_errors_in_a_json_array_or_json_lines(self):
        as_array = json.dumps([{"type": "system", "subtype": "init"}, self.BILLING, {"type": "result", "is_error": True}])
        self.assertEqual(ak.api_errors(as_array), [("billing_error", "Credit balance is too low")])
        as_lines = "\n".join(json.dumps(r) for r in ({"type": "system"}, self.BILLING))
        self.assertEqual(ak.api_errors(as_lines), [("billing_error", "Credit balance is too low")])
        self.assertEqual(ak.api_errors(json.dumps([{"type": "result", "is_error": False}])), [])

    def test_hints_match_on_message_text(self):
        self.assertIn("工作区", ak.hint_for("unknown", "API Error: 400 This API key is not scoped to a workspace, so ..."))
        self.assertIn("另一个组织", ak.hint_for("unknown", "Credit balance is too low"))
        self.assertIsNone(ak.hint_for("unknown", "something else"))

    def test_dry_run_explains_the_fix(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            path.write_text(json.dumps([self.BILLING]))
            result = subprocess.run([sys.executable, str(SCRIPT), "explain-failure", "--file", str(path), "--dry-run"],
                                    capture_output=True, text=True, env=env_without_hook_dir())
        self.assertEqual(result.returncode, 0)
        self.assertIn("Credit balance is too low", result.stdout)
        self.assertIn("另一个组织", result.stdout)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo({
            "agentkit.toml": toml_config(paths={"protected": ["vendor/**"]},
                                         checks={"always": ["python3 -c \"import sys; sys.exit(1)\""]}),
            "src/a.py": "x = 1\n",
        })
        self.env = {**env_without_hook_dir(), "CLAUDE_PROJECT_DIR": str(self.repo.root)}

    def tearDown(self):
        self.repo.close()

    def hook(self, event, payload):
        return self.repo.run("hook", event, stdin=json.dumps(payload), env=self.env)

    def test_pre_edit_blocks_protected_paths(self):
        blocked = self.hook("pre-edit", {"tool_name": "Edit", "tool_input": {"file_path": str(self.repo.root / "vendor/lib.py")}})
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("受保护", blocked.stderr)
        allowed = self.hook("pre-edit", {"tool_name": "Write", "tool_input": {"file_path": str(self.repo.root / "src/a.py")}})
        self.assertEqual(allowed.returncode, 0)

    def test_stop_runs_checks_only_when_needed(self):
        self.assertEqual(self.hook("stop", {}).returncode, 0)  # clean tree
        self.repo.write({"src/a.py": "x = 2\n"})
        failed = self.hook("stop", {})
        self.assertEqual(failed.returncode, 2)
        self.assertIn("没通过", failed.stderr)
        self.assertEqual(self.hook("stop", {"stop_hook_active": True}).returncode, 0)


class ChecksCommandTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo({
            "agentkit.toml": toml_config(checks={"always": ["python3 -c \"print(1)\""]})
            + '[[checks.by_path]]\npaths = ["src/**"]\nrun = ["python3 -c \\"import sys; sys.exit(3)\\""]\n',
            "src/a.py": "x = 1\n",
            "docs/a.md": "doc\n",
        })
        self.repo.git("checkout", "-q", "-b", "feature")

    def tearDown(self):
        self.repo.close()

    def test_path_checks_run_only_when_their_paths_change(self):
        self.repo.write({"docs/a.md": "doc 2\n"})
        ok = self.repo.run("checks", "--base", "main")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("没有专门检查覆盖的改动", ok.stdout)
        self.repo.write({"src/a.py": "x = 2\n"})
        failed = self.repo.run("checks", "--base", "main", "--evidence", "ev.md")
        self.assertEqual(failed.returncode, 1)
        evidence = (self.repo.root / "ev.md").read_text()
        self.assertIn("改到 src/a.py", evidence)
        self.assertIn("退出码 3", evidence)

    def test_committed_only_ignores_uncommitted_changes(self):
        self.repo.write({"src/a.py": "x = 2\n"})
        committed = self.repo.run("checks", "--base", "main", "--committed-only")
        self.assertEqual(committed.returncode, 0, committed.stdout + committed.stderr)
        self.assertIn("改动文件 0 个", committed.stdout)


class GateDryRunTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo({"agentkit.toml": "", "src/a.py": "x = 1\n"})
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.write({".github/workflows/ci.yml": "name: ci\n"})
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "change ci")

    def tearDown(self):
        self.repo.close()

    def test_dry_run_raises_risk_and_fails_on_confirmed_important(self):
        self.repo.write({"review.md": as_comment(review("low"))})
        passed = self.repo.run("gate", "--review", "review.md", "--base", "main", "--dry-run")
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn("风险 **高**", passed.stdout)
        self.repo.write({"review.md": as_comment(review("low", [issue()]))})
        failed = self.repo.run("gate", "--review", "review.md", "--base", "main", "--dry-run")
        self.assertEqual(failed.returncode, 1)

    def test_missing_or_malformed_review_fails(self):
        self.assertEqual(self.repo.run("gate", "--review", "nope.md", "--base", "main", "--dry-run").returncode, 1)
        self.repo.write({"bad.md": "## AI 审查\n没有结果段\n"})
        self.assertEqual(self.repo.run("gate", "--review", "bad.md", "--pr", "1", "--base", "main", "--dry-run").returncode, 1)

    def test_skipped_review_when_the_pr_changes_its_own_workflow(self):
        self.repo.write({".github/workflows/agentkit.yml": "name: agentkit\n"})
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "change the review workflow")
        skipped = self.repo.run("gate", "--review", "nope.md", "--pr", "1", "--base", "main", "--dry-run")
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertIn("needs-human", skipped.stdout)
        self.assertIn("被跳过", skipped.stdout)


class InitTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo({
            "Makefile": ".PHONY: check\ncheck:\n\ttrue\nunit-test:\n\ttrue\n",
            "AGENTS.md": "rules\n",
            "REVIEW.md": "review rules\n",
            "vendor/x.txt": "v\n",
            ".claude/commands/restate.md": "my own command\n",
        })

    def tearDown(self):
        self.repo.close()

    def init(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "init", str(self.repo.root)],
                                capture_output=True, text=True, env=env_without_hook_dir())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_install_and_upgrade(self):
        self.init()
        root = self.repo.root
        cfg = ak.load_config(root)
        self.assertEqual(cfg["rules_always"], ["AGENTS.md"])
        self.assertEqual(cfg["rules_review"], ["REVIEW.md"])
        self.assertEqual(cfg["checks_always"], ["make check"])
        self.assertIn("make unit-test", (root / "agentkit.toml").read_text())
        self.assertIn("vendor/**", (root / "agentkit.toml").read_text())
        for path in (".agentkit/agentkit.py", ".agentkit/review-format.md", ".agentkit/team-workflow.md",
                     ".agentkit/task-card.md", ".agentkit/prompts/restate.md", ".agentkit/prompts/reviewer.md",
                     ".claude/commands/repro.md", ".claude/commands/selfcheck.md", ".claude/commands/handoff.md",
                     ".github/workflows/agentkit.yml", ".git/hooks/pre-commit"):
            self.assertTrue((root / path).exists(), path)
        self.assertEqual((root / ".claude/commands/restate.md").read_text(), "my own command\n")
        self.assertFalse((root / ".claude/commands/reviewer.md").exists())
        agents = (root / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith("rules\n"))
        self.assertEqual(agents.count(ak.BLOCK_START), 1)
        self.assertIn("task show", agents)
        self.assertIn("[tasks]", (root / "agentkit.toml").read_text())
        claude_md = (root / "CLAUDE.md").read_text()
        self.assertIn("@AGENTS.md", claude_md)
        self.assertIn("@.agentkit/team-workflow.md", claude_md)
        self.assertIn(".agentkit/*.local.md", (root / ".gitignore").read_text())
        version = (root / ".agentkit/VERSION").read_text()
        self.assertTrue(version.startswith(ak.VERSION + "\n"))
        import hashlib
        self.assertIn("sha256: " + hashlib.sha256((root / ".agentkit/agentkit.py").read_bytes()).hexdigest(), version)

        (root / "agentkit.toml").write_text((root / "agentkit.toml").read_text() + "# 手工改动\n")
        settings_before = (root / ".claude/settings.json").read_text()
        self.init()
        self.assertIn("# 手工改动", (root / "agentkit.toml").read_text())
        self.assertEqual((root / ".claude/settings.json").read_text(), settings_before)
        self.assertEqual((root / "CLAUDE.md").read_text(), claude_md)
        self.assertEqual((root / "AGENTS.md").read_text(), agents)
        settings = json.loads(settings_before)
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1)
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 1)
        self.assertIn("Bash(python3 .agentkit/agentkit.py task confirm:*)", settings["permissions"]["deny"])

    def test_a_repo_that_ships_its_own_hooks_gets_one_line_there(self):
        self.repo.write({".githooks/pre-commit": "#!/bin/sh\nset -eu\nmake check\n"})
        self.init()
        hook = (self.repo.root / ".githooks/pre-commit").read_text()
        self.assertTrue(hook.endswith(ak.GIT_HOOK_GUARDED + "\n"))
        self.assertFalse((self.repo.root / ".git/hooks/pre-commit").exists())
        self.init()
        self.assertEqual((self.repo.root / ".githooks/pre-commit").read_text(), hook)
        exec_hook = self.repo.root / "x-pre-commit"
        exec_hook.write_text("#!/bin/sh\nexec lint-staged\n")
        ak.add_to_hook(exec_hook)
        self.assertEqual(exec_hook.read_text().splitlines()[-1], "exec lint-staged")

    def test_other_agents_get_a_pointer_and_a_repo_without_agents_md_gets_one(self):
        (self.repo.root / "AGENTS.md").unlink()
        (self.repo.root / "agentkit.toml").write_text('[agents]\ninstruction_files = ["CLAUDE.md", "GEMINI.md"]\n'
                                                    '[tasks]\nnote = "最终记录写在 changes/"\n')
        self.init()
        root = self.repo.root
        self.assertIn("被纠正过的规则", (root / "AGENTS.md").read_text())
        self.assertIn("7. **本仓库**：最终记录写在 changes/", (root / "AGENTS.md").read_text())
        self.assertIn("AGENTS.md", (root / "GEMINI.md").read_text())
        self.assertIn("@AGENTS.md", (root / "CLAUDE.md").read_text())


CARD = """# WIP-1 下单时库存不足要提示

## 需求

- 满足谁：下单的用户
- 解决什么：库存不足时现在返回 500
- 怎么算做完：
  - 返回 409 和“库存不足”
- 确认：

## 要人拍板的事

- [x] 用 409 还是 400 → 409（产品，对话，11-01）
- [ ] 多件商品时整单失败还是只去掉缺货的？（建议：整单失败）

## 拆分

1. [done] 下单接口检查库存
2. [doing] 前端显示提示
3. [todo] 多件商品

## 交接

- 做到：接口已合入
- 下一步：结账页处理 409
- 试过不行的：

## 记录
"""


def signed(card=CARD, who="张三，2026-11-01"):
    fp = ak.requirement_fingerprint(ak.card_sections(card)["需求"])
    return ak.set_confirmation(card, f"{who}（需求指纹 {fp}）")


class TaskCardTest(unittest.TestCase):
    def test_status_reads_every_section(self):
        st = ak.card_status(CARD)
        self.assertEqual(st["title"], "WIP-1 下单时库存不足要提示")
        self.assertEqual(st["missing"], [])
        self.assertEqual(st["confirmed"], "no")
        self.assertEqual(len(st["open"]), 1)
        self.assertEqual(st["decided"], 1)
        self.assertEqual([state for state, _ in st["steps"]], ["done", "doing", "todo"])
        self.assertEqual(st["handoff"]["下一步"], "结账页处理 409")

    def test_field_names_may_carry_a_note_or_bold(self):
        section = "- 怎么算做完（可检查）：返回 409\n- **满足谁**：用户\n"
        self.assertEqual(ak.field_value(section, "怎么算做完"), "返回 409")
        self.assertEqual(ak.field_value(section, "满足谁"), "用户")

    def test_a_bold_sign_off_line_is_one_line_everywhere(self):
        card = CARD.replace("- 确认：", "- **确认**（人填）：")
        out = signed(card)
        self.assertEqual(out.count("确认"), card.count("确认"))  # replaced in place, not appended
        self.assertEqual(ak.card_status(out)["confirmed"], "yes")

    def test_missing_fields_and_indented_values(self):
        st = ak.card_status(CARD.replace("- 满足谁：下单的用户", "- 满足谁："))
        self.assertEqual(st["missing"], ["满足谁"])
        self.assertIn("409", ak.field_value(ak.card_sections(CARD)["需求"], "怎么算做完"))

    def test_sign_off_goes_stale_when_the_requirement_changes(self):
        card = signed()
        st = ak.card_status(card)
        self.assertEqual(st["confirmed"], "yes")
        self.assertEqual(st["confirmed_by"], "张三，2026-11-01")
        self.assertEqual(ak.card_status(card.replace("返回 409", "返回 400"))["confirmed"], "stale")
        self.assertEqual(ak.card_status(card.replace("- 做到：接口已合入", "- 做到：都做完了"))["confirmed"], "yes")

    def test_comments_do_not_count(self):
        card = CARD.replace("## 拆分\n", "## 拆分\n\n<!-- 1. [todo] 例子 -->\n")
        self.assertEqual(len(ak.card_status(card)["steps"]), 3)

    def test_append_to_section(self):
        out = ak.append_to_section(CARD, "记录", "- 一行")
        self.assertTrue(out.rstrip().endswith("- 一行"))
        out = ak.append_to_section(CARD, "要人拍板的事", "- [ ] 新问题")
        self.assertEqual(len(ak.card_status(out)["open"]), 2)


class TaskCommandTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo({"agentkit.toml": toml_config(tasks={"dir": "docs/tasks", "required": True}),
                              "shop/orders.py": "x = 1\n"})

    def tearDown(self):
        self.repo.close()

    def test_new_show_list_and_use(self):
        created = self.repo.run("task", "new", "WIP-1", "下单库存不足")
        self.assertEqual(created.returncode, 0, created.stderr)
        card = self.repo.root / "docs/tasks/WIP-1.md"
        self.assertTrue(card.exists())
        self.assertNotIn(ak.OWNED, card.read_text())
        self.assertIn("# WIP-1 下单库存不足", card.read_text())
        shown = self.repo.run("task", "show")
        self.assertIn("还缺", shown.stdout)
        self.assertIn("还没有", shown.stdout)
        self.assertIn("除了这张任务卡", shown.stdout)
        self.assertEqual(self.repo.run("task", "new", "WIP-1").returncode, 1)
        self.assertEqual(self.repo.run("task", "new", "bad id").returncode, 1)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "card")
        self.repo.git("checkout", "-q", "-b", "other")
        self.assertIn("没有找到当前任务", self.repo.run("task", "show").stdout)  # pointer was set on main
        self.repo.git("checkout", "-q", "main")
        self.assertIn("task new / task use", self.repo.run("task", "show").stdout)
        self.repo.git("reset", "-q", "--soft", "HEAD~1")
        self.repo.run("task", "use", "--clear")
        self.assertIn("这个分支改过的任务卡", self.repo.run("task", "show").stdout)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "card")
        self.assertIn("没有找到当前任务", self.repo.run("task", "show").stdout)
        self.repo.git("checkout", "-q", "-b", "yukang/WIP-1-stock")
        self.assertIn("分支名", self.repo.run("task", "show", "--brief").stdout)
        self.assertIn("WIP-1", self.repo.run("task", "list").stdout)

    def test_confirm_refuses_agents(self):
        self.repo.write({"docs/tasks/WIP-1.md": CARD})
        refused = self.repo.run("task", "confirm", "WIP-1")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("由人", refused.stderr)

    def test_handoff_needs_the_handoff_written(self):
        self.repo.write({"docs/tasks/WIP-1.md": CARD.replace("- 下一步：结账页处理 409", "- 下一步：")})
        self.assertEqual(self.repo.run("task", "handoff", "WIP-1").returncode, 1)
        self.repo.write({"docs/tasks/WIP-1.md": CARD})
        done = self.repo.run("task", "handoff", "WIP-1", "--agent", "Kimi")
        self.assertEqual(done.returncode, 0, done.stderr)
        text = (self.repo.root / "docs/tasks/WIP-1.md").read_text()
        self.assertIn("· Kimi ·", text)
        self.assertIn("拆分完成 1/3", text)

    def test_check_for_a_pr(self):
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.write({"shop/orders.py": "x = 2\n"})
        self.repo.git("commit", "-qam", "code only")
        no_card = self.repo.run("task", "check", "--base", "main", "--committed-only")
        self.assertEqual(no_card.returncode, 1)
        self.assertIn("找不到任务卡", no_card.stdout)

        self.repo.write({"docs/tasks/WIP-1.md": CARD})
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "card")
        unsigned = self.repo.run("task", "check", "--base", "main", "--committed-only")
        self.assertEqual(unsigned.returncode, 1)
        self.assertIn("还没有人确认", unsigned.stdout)

        self.repo.write({"docs/tasks/WIP-1.md": signed()})
        self.repo.git("commit", "-qam", "sign off")
        ok = self.repo.run("task", "check", "--base", "main", "--committed-only")
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertIn("还有 1 件事没拍板", ok.stdout)

    def test_check_finds_the_card_from_the_pr_body_and_warns_on_stale_handoff(self):
        self.repo.write({"docs/tasks/WIP-1.md": signed()})
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "card on main")
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.write({"shop/orders.py": "x = 2\n", "body.md": "任务：WIP-1\n"})
        self.repo.git("add", "shop")
        self.repo.git("commit", "-qm", "code")
        result = self.repo.run("task", "check", "--base", "main", "--committed-only", "--pr-body-file", "body.md")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("任务卡没有更新", result.stdout)

    def test_not_required_only_warns(self):
        self.repo.write({"agentkit.toml": toml_config(tasks={"required": False})})
        self.repo.git("commit", "-qam", "relax")
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.write({"shop/orders.py": "x = 2\n"})
        self.repo.git("commit", "-qam", "code")
        result = self.repo.run("task", "check", "--base", "main", "--committed-only")
        self.assertEqual(result.returncode, 0)
        self.assertIn("提醒", result.stdout)


class TaskHookTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo({"agentkit.toml": toml_config(tasks={"dir": "docs/tasks"}),
                              "shop/orders.py": "x = 1\n", "docs/tasks/WIP-1.md": CARD})
        self.repo.run("task", "use", "WIP-1")
        self.env = {**env_without_hook_dir(), "CLAUDE_PROJECT_DIR": str(self.repo.root)}

    def tearDown(self):
        self.repo.close()

    def hook(self, event, payload=None):
        return self.repo.run("hook", event, stdin=json.dumps(payload or {}), env=self.env)

    def edit(self, rel, **tool_input):
        return self.hook("pre-edit", {"tool_name": "Edit", "tool_input": {"file_path": str(self.repo.root / rel), **tool_input}})

    def test_agents_cannot_sign_off(self):
        blocked = self.edit("docs/tasks/WIP-1.md", old_string="- 确认：", new_string="- 确认：我自己")
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("只能由人", blocked.stderr)
        allowed = self.edit("docs/tasks/WIP-1.md", old_string="- 做到：接口已合入", new_string="- 做到：前端也好了")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_no_code_edits_before_sign_off(self):
        blocked = self.hook("pre-edit", {"tool_name": "Write", "tool_input": {"file_path": str(self.repo.root / "shop/orders.py"), "content": "x = 2\n"}})
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("还没有人确认", blocked.stderr)
        self.repo.write({"docs/tasks/WIP-1.md": signed()})
        self.assertEqual(self.edit("shop/orders.py", old_string="x = 1", new_string="x = 2").returncode, 0)

    def test_stop_asks_for_a_handoff_and_session_start_shows_the_card(self):
        self.repo.write({"docs/tasks/WIP-1.md": signed()})
        self.repo.git("commit", "-qam", "sign off")
        self.repo.write({"shop/orders.py": "x = 2\n"})
        stop = self.hook("stop")
        self.assertEqual(stop.returncode, 2)
        self.assertIn("交接", stop.stderr)
        self.repo.write({"docs/tasks/WIP-1.md": signed() + "\n- 更新\n"})
        self.assertEqual(self.hook("stop").returncode, 0)
        start = self.hook("session-start")
        self.assertEqual(start.returncode, 0)
        self.assertIn("当前任务：WIP-1", start.stdout)

    def test_guards_fail_closed_when_the_kit_breaks(self):
        self.repo.write({"agentkit.toml": "[tasks\n"})
        broken = self.hook("pre-edit", {"tool_name": "Write", "tool_input": {"file_path": str(self.repo.root / "x.py"), "content": ""}})
        self.assertEqual(broken.returncode, 2)
        self.assertIn("自己出错了", broken.stderr)
        self.assertEqual(self.hook("stop").returncode, 0)

    def test_git_pre_commit_blocks_unsigned_code(self):
        self.repo.write({"shop/orders.py": "x = 2\n"})
        self.repo.git("add", "shop/orders.py")
        blocked = self.repo.run("hook", "pre-commit")
        self.assertEqual(blocked.returncode, 1)
        self.repo.write({"docs/tasks/WIP-1.md": signed()})
        not_staged = self.repo.run("hook", "pre-commit")  # signed in the working tree only: the commit would carry the unsigned card
        self.assertEqual(not_staged.returncode, 1)
        self.assertIn("暂存区", not_staged.stderr)
        self.repo.git("add", "docs/tasks/WIP-1.md")
        self.assertEqual(self.repo.run("hook", "pre-commit").returncode, 0)
        self.assertIn("已安装", self.repo.run("hook", "install-git").stdout)
        self.assertTrue((self.repo.root / ".git/hooks/pre-commit").exists())


class RoundTwoFixesTest(unittest.TestCase):
    """Findings from the second independent review of annotation#17."""

    def test_names_with_spaces_or_chinese_are_kept_whole(self):
        repo = TempRepo({"agentkit.toml": toml_config(paths={"protected": ["vendor/**"]}), "a.txt": "a\n"})
        try:
            repo.git("checkout", "-q", "-b", "feature")
            repo.write({"vendor/中文 文件.txt": "x\n"})
            repo.git("add", "-A")
            repo.git("commit", "-qm", "add")
            files = ak.changed_files(repo.root, "main", worktree=False)
            self.assertEqual(files, ["vendor/中文 文件.txt"])
            self.assertEqual(ak.risk_floor(ak.load_config(repo.root), files)["floor"], "high")
        finally:
            repo.close()

    def test_relative_paths_are_normalized_before_the_protected_check(self):
        repo = TempRepo({"agentkit.toml": toml_config(paths={"protected": ["vendor/**"]}), "src/a.py": "x\n"})
        try:
            env = {**env_without_hook_dir(), "CLAUDE_PROJECT_DIR": str(repo.root)}
            sneaky = repo.run("hook", "pre-edit", stdin=json.dumps({"tool_input": {"file_path": "src/../vendor/x.py"}}), env=env)
            self.assertEqual(sneaky.returncode, 2)
            self.assertIn("vendor/x.py", sneaky.stderr)
        finally:
            repo.close()

    def test_stop_only_holds_a_session_to_what_it_changed(self):
        repo = TempRepo({"agentkit.toml": toml_config(checks={"always": ["python3 -c \"import sys; sys.exit(1)\""]}),
                         "src/a.py": "x = 1\n"})
        try:
            env = {**env_without_hook_dir(), "CLAUDE_PROJECT_DIR": str(repo.root)}
            repo.write({"src/a.py": "x = 2\n"})  # someone's uncommitted work, failing checks
            repo.run("hook", "session-start", stdin=json.dumps({"session_id": "reviewer"}), env=env)
            self.assertEqual(repo.run("hook", "stop", stdin=json.dumps({"session_id": "reviewer"}), env=env).returncode, 0)
            repo.run("hook", "session-start", stdin=json.dumps({"session_id": "author"}), env=env)
            repo.write({"src/a.py": "x = 3\n"})
            self.assertEqual(repo.run("hook", "stop", stdin=json.dumps({"session_id": "author"}), env=env).returncode, 2)
            self.assertEqual(repo.run("hook", "stop", stdin="{}", env={**env, "AGENTKIT_REVIEW": "1"}).returncode, 0)
            self.assertEqual(repo.run("hook", "stop", stdin="{}", env=env).returncode, 2)  # unknown session: as before
        finally:
            repo.close()

    def test_required_task_cards_block_edits_and_commits_without_a_card(self):
        repo = TempRepo({"agentkit.toml": toml_config(tasks={"dir": "docs/tasks", "required": True}), "src/a.py": "x\n"})
        try:
            env = {**env_without_hook_dir(), "CLAUDE_PROJECT_DIR": str(repo.root)}
            edit = {"tool_input": {"file_path": str(repo.root / "src/a.py"), "content": "y\n"}}
            self.assertEqual(repo.run("hook", "pre-edit", stdin=json.dumps(edit), env=env).returncode, 2)
            local = {"tool_input": {"file_path": str(repo.root / ".agentkit/review.local.md"), "content": "r\n"}}
            self.assertEqual(repo.run("hook", "pre-edit", stdin=json.dumps(local), env=env).returncode, 0)
            repo.write({"src/a.py": "y\n"})
            repo.git("add", "src/a.py")
            self.assertEqual(repo.run("hook", "pre-commit").returncode, 1)
            repo.write({"agentkit.toml": toml_config(tasks={"dir": "docs/tasks", "required": False})})
            self.assertEqual(repo.run("hook", "pre-commit").returncode, 0)
        finally:
            repo.close()

    def test_a_new_tasks_branch_never_reuses_another_tasks_card(self):
        # Round 3: a sibling Task's branch must not pick up WIP-7-2's sign-off.
        repo = TempRepo({"agentkit.toml": "", "docs/tasks/WIP-7-2.md": CARD.replace("WIP-1", "WIP-7-2"),
                         "docs/tasks/WIP-7.md": CARD.replace("WIP-1", "WIP-7")})
        try:
            repo.git("checkout", "-q", "-b", "me/feat/WIP-7-3-search")
            self.assertIsNone(ak.current_task(repo.root, ak.load_config(repo.root), base="HEAD")[0])
            self.assertEqual(ak.ids_in("me/feat/WIP-7-3-search", ["WIP-7", "WIP-7-3", "WIP-7-2"]), ["WIP-7-3"])
            self.assertEqual(ak.ids_in("me/feat/WIP-7-2-search", ["WIP-7-2"]), ["WIP-7-2"])
            self.assertEqual(ak.ids_in("任务：WIP-7。", ["WIP-7"]), ["WIP-7"])
        finally:
            repo.close()

    def test_review_material_names_cut_files_and_reads_committed_cards(self):
        repo = TempRepo({"agentkit.toml": "", "docs/tasks/WIP-1.md": CARD, "src/keep.py": "x\n"})
        old_limit = ak.MAX_DIFF_CHARS
        try:
            repo.git("checkout", "-q", "-b", "feature")
            repo.write({f"src/big{i}.py": "line\n" * 400 for i in range(3)})
            repo.git("add", "-A")
            repo.git("commit", "-qm", "big")
            ak.MAX_DIFF_CHARS = 3000
            diff = ak.review_diff(repo.root, "main", committed_only=True)
            self.assertIn("没有附上", diff)
            self.assertIn("- src/big2.py", diff)
            repo.write({"docs/tasks/WIP-1.md": CARD.replace("下单的用户", "工作区里的改动")})
            prompt = ak.build_review_prompt(repo.root, ak.load_config(repo.root), "main", pr_body="任务：WIP-1",
                                            committed_only=True)
            self.assertIn("任务卡（docs/tasks/WIP-1.md）", prompt)  # named in the PR, unchanged on the branch
            self.assertIn("下单的用户", prompt)
            self.assertNotIn("工作区里的改动", prompt)
        finally:
            ak.MAX_DIFF_CHARS = old_limit
            repo.close()


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo({"agentkit.toml": "", "docs/tasks/WIP-1.md": CARD})

    def tearDown(self):
        self.repo.close()

    def test_prompts_work_for_any_agent(self):
        listing = self.repo.run("prompt")
        for name in ("restate", "repro", "handoff", "start"):
            self.assertIn(name, listing.stdout)
        out = self.repo.run("prompt", "restate", "下单时库存不足要提示").stdout
        self.assertIn("下单时库存不足要提示", out)
        self.assertNotIn("$ARGUMENTS", out)
        self.assertNotIn(ak.OWNED, out)
        self.repo.run("task", "use", "WIP-1")
        self.assertIn("当前任务卡", self.repo.run("prompt", "start", "接着做").stdout)
        self.assertNotIn("当前任务卡", self.repo.run("prompt", "start", "--no-card").stdout)


def fake_review(risk, issues, name="x"):
    return as_comment(review(risk, issues)).replace("## AI 审查", f"## AI 审查（{name}）")


class ReviewEngineTest(unittest.TestCase):
    def test_merge_takes_max_risk_min_scores_and_keeps_every_issue(self):
        a = review("low", [issue()])
        b = review("high", [issue("nit")])
        b["scores"]["evidence"] = 2
        text, merged = ak.merge_reviews([("claude", as_comment(a), a), ("deepseek", as_comment(b), b)], [], [], 1)
        self.assertEqual(merged["risk_level"], "high")
        self.assertEqual(merged["scores"]["evidence"], 2)
        self.assertEqual(merged["reviewers"], ["claude", "deepseek"])
        self.assertEqual(len(merged["issues"]), 2)
        self.assertIn("2 位独立审查者", text)
        row = next(line for line in text.splitlines() if line.startswith("| `a.py"))
        self.assertIn("**claude**（Important）", row)  # same spot, one row, each reviewer's own words
        self.assertIn("**deepseek**（Nit）", row)

    def test_agree_needs_several_reviewers_to_block(self):
        a = review("low", [issue()])
        b = review("low", [])
        _, merged = ak.merge_reviews([("a", "", a), ("b", "", b)], [], [], 2)
        self.assertFalse(ak.decide(config(), merged, ak.risk_floor(config(), []), 0)["blockers"])
        b = review("low", [{**issue(), "line": 3}])
        _, merged = ak.merge_reviews([("a", "", review("low", [issue()])), ("b", "", b)], [], [], 2)
        self.assertEqual(len(ak.decide(config(), merged, ak.risk_floor(config(), []), 0)["blockers"]), 2)

    def test_instances_and_plan(self):
        self.assertEqual(ak.review_instances(["claude", "claude", "codex"]),
                         [("claude#1", "claude"), ("claude#2", "claude"), ("codex", "codex")])

    def test_command_engines_run_independently_and_merge(self):
        result = json.dumps(review("medium", [issue()]))
        script = f"import sys; sys.stdin.read(); print('## AI 审查\\n\\n正文\\n\\n<!-' + '- ai-review-result\\n' + {result!r} + '\\n-' + '->')"
        bad = "import sys; sys.stdin.read(); print('没有结果')"
        engines = ('[review]\nlocal = ["good", "good", "bad"]\n'
                   f'[review.engines.good]\ntype = "command"\nrun = {json.dumps(sys.executable + " -c " + json.dumps(script))}\n'
                   f'[review.engines.bad]\ntype = "command"\nrun = {json.dumps(sys.executable + " -c " + json.dumps(bad))}\n')
        repo = TempRepo({"agentkit.toml": engines, "src/a.py": "x = 1\n"})
        try:
            repo.git("checkout", "-q", "-b", "feature")
            repo.write({"src/a.py": "x = 2\n"})
            out = repo.run("review", "--base", "main")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn("good#1：完成", out.stdout)
            self.assertIn("bad：结果格式不对", out.stdout)
            self.assertIn("没有结果", (repo.root / ".agentkit/review.bad.local.md").read_text())
            merged, errors = ak.parse_result((repo.root / ".agentkit/review.local.md").read_text())
            self.assertEqual(errors, [])
            self.assertEqual(merged["reviewers"], ["good#1", "good#2"])
            self.assertEqual(merged["failed_reviewers"], ["bad"])
            prompt = repo.run("review", "--print-prompt", "--base", "main").stdout
            self.assertIn("x = 2", prompt)
            self.assertIn("包括还没提交的", prompt)
            self.assertIn("...HEAD`（已提交的改动）", repo.run("review", "--print-prompt", "--ci", "--base", "main").stdout)
            self.assertIn("审查格式", prompt)
            plan = repo.run("review", "--ci", "--plan").stdout
            self.assertIn("action=true", plan)
        finally:
            repo.close()

    def test_review_prompt_covers_every_card_new_files_and_head_scope(self):
        repo = TempRepo({"agentkit.toml": "", "docs/tasks/WIP-1.md": CARD, "docs/tasks/WIP-2.md": CARD.replace("WIP-1", "WIP-2")})
        try:
            repo.git("checkout", "-q", "-b", "feature")
            repo.write({"docs/tasks/WIP-1.md": CARD + "\n", "docs/tasks/WIP-2.md": CARD.replace("WIP-1", "WIP-2") + "\n"})
            repo.write({f"new/f{i:02}.txt": "x\n" for i in range(12)})
            prompt = repo.run("review", "--print-prompt", "--base", "main").stdout
            self.assertIn("任务卡（docs/tasks/WIP-1.md）", prompt)
            self.assertIn("任务卡（docs/tasks/WIP-2.md）", prompt)
            self.assertIn("没有附上内容", prompt)
            self.assertIn("- new/f11.txt", prompt)
            head = repo.run("review", "--print-prompt", "--base", "HEAD").stdout
            self.assertIn("还没提交的", head)
            self.assertNotIn("HEAD...HEAD", head)
        finally:
            repo.close()

    def test_api_engine_reports_a_missing_key(self):
        text, error = "", ""
        try:
            ak.run_api_engine({"type": "api", "base_url": "http://127.0.0.1:9", "model": "m", "key_env": "AGENTKIT_TEST_NO_KEY"}, "p", 5)
        except RuntimeError as e:
            error = str(e)
        self.assertIn("AGENTKIT_TEST_NO_KEY", error)


# ---------------------------------------------------------------- languages

CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
GOLDEN = KIT / "tests" / "golden" / "default-zh.txt"

CARD_EN = """# WIP-1 Tell people when an item is out of stock

## Requirement

- Who needs it: people placing orders
- What it solves: an order for an out-of-stock item returns 500 today
- Done when:
  - it returns 409 and "out of stock"
- Confirmed by:

## Decisions for people

- [x] 409 or 400 → 409 (product, in chat, 11-01)
- [ ] With several items, fail the whole order or drop the missing ones? (suggestion: fail the whole order)

## Steps

1. [done] Check stock in the order API
2. [doing] Show the message in the web app
3. [todo] Several items

## Handoff

- Progress: the API change is merged
- Next step: handle 409 on the checkout page
- Tried and failed:

## Log
"""

SCENARIO = {
    "zh": {"title": "下单库存不足", "need": "下单时库存不足要提示", "card": CARD, "confirm": "- 确认：",
           "progress": ("- 做到：接口已合入", "- 做到：前端提示也做完了"), "note": "最终记录写在 changes/",
           "review": "## AI 审查\n\n正文\n", "summary": "库存检查漏了并发"},
    "en": {"title": "Out-of-stock message", "need": "Tell people when an item is out of stock", "card": CARD_EN,
           "confirm": "- Confirmed by:", "progress": ("- Progress: the API change is merged", "- Progress: the web app message is done too"),
           "note": "Final notes go in changes/", "review": "## AI review\n\nBody\n", "summary": "the stock check misses concurrent orders"},
}


def clean_env(**extra):
    """No Claude Code, CI or personal git settings: the same session prints the same thing anywhere."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "AGENTKIT_", "GITHUB_", "GIT_"))}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}, **extra)
    return env


def in_terminal(cmd, cwd, env, answer):
    """Run cmd with a terminal on stdin (task confirm insists on one) and type answer into it."""
    import pty
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        os.write(master, answer.encode())
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        os.close(slave)
        os.close(master)
    return proc.returncode, stdout, stderr


def review_engine(text):
    """A command reviewer (see [review.engines]) that ignores its prompt and answers text."""
    return shlex.quote(sys.executable) + " -c " + shlex.quote(f"import sys; sys.stdin.read(); print({text!r})")


def transcript(script, lang):
    """One fixed session of agentkit in a fresh repository: every command, what it printed, and the files it wrote.

    script is a kit checkout's agentkit.py. tests/golden/default-zh.txt is this session in Chinese as 0.3.1 ran it
    (transcript(<git archive v0.3.1>/agentkit.py, "zh")); commit hashes, dates, timings and the version are masked.
    """
    data = SCENARIO[lang]
    log = []
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve() / "repo"
        root.mkdir()
        env = clean_env()
        hook_env = clean_env(CLAUDE_PROJECT_DIR=str(root))

        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env=env).stdout

        def commit(*args):  # the kit's own git hook is under test, not in the way of setting up
            git("commit", "-q", "--no-verify", *args)

        def write(files):
            for name, content in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(content)

        def record(shown, code, stdout, stderr):
            log.append(f"$ agentkit {shown}")
            log.append(f"[exit {code}]")
            log.extend(stdout.rstrip("\n").splitlines())
            log.extend("stderr| " + line for line in stderr.rstrip("\n").splitlines())
            log.append("")

        def run(*args, stdin="", kit=None, environ=None):
            cmd = [sys.executable, str(kit or root / ".agentkit/agentkit.py"), *args]
            proc = subprocess.run(cmd, cwd=root, input=stdin, capture_output=True, text=True, env=environ or env)
            record(" ".join(args), proc.returncode, proc.stdout, proc.stderr)

        def hook(event, payload):
            run("hook", event, stdin=json.dumps(payload), environ=hook_env)

        def edit(rel, **tool_input):
            hook("pre-edit", {"tool_name": "Edit", "tool_input": {"file_path": str(root / rel), **tool_input}})

        def confirm(answer):
            cmd = [sys.executable, str(root / ".agentkit/agentkit.py"), "task", "confirm", "WIP-1"]
            record(f"task confirm WIP-1  # a person types {answer!r}", *in_terminal(cmd, root, env, answer))

        def show(*names):
            for name in names:
                log.extend([f"=== {name}", (root / name).read_text().rstrip("\n"), ""])

        def owned():
            for top in (".agentkit", ".claude/commands", ".github/workflows"):
                for path in sorted((root / top).rglob("*")):
                    if path.is_file() and path.name not in ("agentkit.py", "VERSION") and not path.name.endswith(".local.md"):
                        log.append(f"{path.relative_to(root)}  sha256:{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}")
            log.append("")

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        write({"Makefile": ".PHONY: check\ncheck:\n\ttrue\nunit-test:\n\ttrue\n", "AGENTS.md": "rules\n",
               "REVIEW.md": "review rules\n", "CONTRIBUTING.md": "contributing\n", "vendor/x.txt": "v\n",
               "src/a.py": "x = 1\n", ".claude/commands/restate.md": "my own command\n"})
        git("add", "-A")
        commit("-m", "start")

        run("init", str(root), *(["--language", "en"] if lang == "en" else []), kit=script)
        show("agentkit.toml", "AGENTS.md", "CLAUDE.md", ".claude/settings.json", ".gitignore", ".git/hooks/pre-commit")
        owned()
        kit_table = '[kit]\nlanguage = "en"\n' if lang == "en" else ""
        always = "import os, sys; bad = os.path.exists('FAIL'); print('FAIL is here' if bad else 'ok'); sys.exit(1 if bad else 0)"
        result = {"scores": {"requirement": 4, "evidence": 3, "risk": 3, "readability": 4}, "risk_level": "medium",
                  "issues": [{"file": "src/a.py", "line": 1, "severity": "important", "confirmed": True, "summary": data["summary"]}]}
        good = data["review"] + "\n<!-- ai-review-result\n" + json.dumps(result, ensure_ascii=False) + "\n-->"
        write({"agentkit.toml": kit_table + "\n".join([
            "[rules]", 'always = ["AGENTS.md", "CONTRIBUTING.md"]', 'review = ["REVIEW.md"]',
            "[checks]", f"always = {json.dumps(['python3 -c ' + json.dumps(always)])}",
            "[[checks.by_path]]", 'paths = ["src/**"]', 'run = ["python3 -c \\"import sys; print(\'boom\'); sys.exit(3)\\""]',
            "[paths]", 'protected = ["vendor/**"]',
            "[tasks]", 'dir = "docs/tasks"', "required = true", f"note = {json.dumps(data['note'], ensure_ascii=False)}",
            "[agents]", 'instruction_files = ["CLAUDE.md", "GEMINI.md"]',
            "[review]", 'local = ["good", "good", "bad"]', "agree = 2",
            "[review.engines.good]", 'type = "command"', f"run = {json.dumps(review_engine(good))}",
            "[review.engines.bad]", 'type = "command"', f"run = {json.dumps(review_engine('---'))}", ""])})
        run("init", str(root), kit=script)  # upgrade with the project's own config
        show("AGENTS.md", "CLAUDE.md", "GEMINI.md")
        git("add", "-A")
        commit("-m", "kit")

        run("context")
        run("context", "--for", "review")
        run("task", "list")
        run("task", "show")
        hook("session-start", {"session_id": "s1"})
        edit("src/a.py", old_string="x = 1", new_string="x = 2")
        edit("vendor/x.txt", old_string="v", new_string="w")
        run("task", "new", "bad id")
        run("task", "new", "WIP-1", data["title"])
        run("task", "new", "WIP-1")
        run("task", "show")
        run("task", "show", "--brief")
        run("prompt")
        run("prompt", "restate", data["need"])
        run("prompt", "nope")
        edit("src/a.py", old_string="x = 1", new_string="x = 2")
        edit("docs/tasks/WIP-1.md", old_string=data["confirm"], new_string=data["confirm"] + " me")
        run("task", "confirm")
        run("task", "handoff")
        write({"docs/tasks/WIP-1.md": data["card"]})
        run("task", "show", "--brief")
        confirm("n\n")
        git("checkout", "-q", "-b", "feature/WIP-1-stock")
        git("add", "-A")
        commit("-m", "card")
        write({"src/a.py": "x = 2\n"})
        git("add", "src/a.py")
        run("hook", "pre-commit")
        run("task", "check", "--base", "main", "--committed-only")
        confirm("y\n")
        show("docs/tasks/WIP-1.md")
        run("task", "show", "--brief")
        git("add", "docs")
        run("hook", "pre-commit")
        commit("-m", "sign off and code")
        write({"src/a.py": "x = 3\n", ".github/workflows/ci.yml": "name: ci\n"})
        edit("src/a.py", old_string="x = 2", new_string="x = 3")
        run("checks", "--base", "main")
        write({"FAIL": "x\n"})
        hook("stop", {})
        (root / "FAIL").unlink()
        write({"docs/tasks/WIP-1.md": (root / "docs/tasks/WIP-1.md").read_text().replace(*data["progress"])})
        hook("stop", {})
        run("task", "handoff", "--agent", "Kimi")
        show("docs/tasks/WIP-1.md")
        hook("session-start", {"session_id": "s2"})
        git("add", "-A")
        commit("-m", "code")
        run("task", "list")
        run("task", "check", "--base", "main", "--committed-only", "--branch", "feature/WIP-1-stock")
        run("review", "--base", "main")
        show(".agentkit/review.local.md")
        run("review", "--print-prompt", "--base", "main")
        run("review", "--ci", "--plan")
        unconfirmed = json.loads(json.dumps(result))
        unconfirmed["issues"][0]["confirmed"] = False
        write({"review-fail.md": good + "\n",
               "review-pass.md": data["review"] + "\n<!-- ai-review-result\n" + json.dumps(unconfirmed) + "\n-->\n",
               "bad.md": "## AI review\n", "out.json": json.dumps([
                   {"error": "billing_error", "is_api_error_message": True,
                    "message": {"content": [{"type": "text", "text": "Credit balance is too low"}]}},
                   {"error": "invalid_request_error", "is_api_error_message": True,
                    "message": {"content": [{"type": "text", "text": "This API key is not scoped to a workspace"}]}}]),
               "empty.json": "[]"})
        run("gate", "--review", "review-fail.md", "--base", "main", "--head", "abcdef1234567", "--dry-run")
        run("gate", "--review", "review-pass.md", "--base", "main", "--head", "abcdef1234567", "--dry-run")
        run("validate", "review-pass.md")
        run("validate", "bad.md")
        run("explain-failure", "--file", "out.json", "--dry-run")
        run("explain-failure", "--file", "empty.json", "--dry-run")
        run("risk", "--base", "main")
        run("task", "use", "--clear")
        run("task", "show")
        run("task", "use", "nope")
        run("task", "use", "WIP-1")
        run("hook", "install-git")
        write({".github/workflows/agentkit.yml": "name: changed\n"})
        commit("-am", "change the review workflow")
        run("gate", "--review", "nope.md", "--pr", "1", "--base", "main", "--dry-run")

        commits = git("rev-list", "--all").split()
    text = "\n".join(log) + "\n"
    for n, sha in enumerate(reversed(commits)):
        text = re.sub(rf"\b{sha[:7]}[0-9a-f]*\b", f"<commit {n}>", text)
    text = re.sub(r"(?m)^index [0-9a-f]{7,}\.\.[0-9a-f]{7,}", "index <blobs>", text)  # blobs of files that hold a time
    text = text.replace(str(root), "<repo>")
    text = re.sub(r"\b20\d\d-\d\d-\d\d \d\d:\d\d\b", "<time>", text)
    text = text.replace(time.strftime("%Y-%m-%d"), "<today>")
    text = re.sub(r"\| [0-9.]+s \|", "| <seconds> |", text)
    return re.sub(r"agentkit \d+\.\d+\.\d+", "agentkit <version>", text)


class DefaultLanguageTest(unittest.TestCase):
    def test_without_a_language_every_output_is_what_0_3_1_gave(self):
        self.maxDiff = None
        self.assertEqual(transcript(SCRIPT, "zh"), GOLDEN.read_text(encoding="utf-8"))

    def test_the_requirement_fingerprint_is_unchanged(self):
        # computed by 0.3.1 for CARD: cards signed before the upgrade stay signed
        self.assertEqual(ak.requirement_fingerprint(ak.card_sections(CARD)["需求"]), "d4e11033")
        old = ak.set_confirmation(CARD, "张三，2026-11-01（需求指纹 d4e11033）")
        self.assertEqual(ak.card_status(old)["confirmed"], "yes")


class EnglishTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(ak.set_language, ak.UI_LANG)

    def test_the_language_comes_from_agentkit_toml(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(ak.load_config(root)["language"], "zh")
            for value, lang in (("en", "en"), ("EN-us", "en"), ("zh_CN", "zh")):
                (root / "agentkit.toml").write_text(f'[kit]\nlanguage = "{value}"\n')
                self.assertEqual(ak.load_config(root)["language"], lang)
            (root / "agentkit.toml").write_text('[kit]\nlanguage = "fr"\n')
            with self.assertRaises(ValueError):
                ak.load_config(root)
            (root / "agentkit.toml").write_text('[kit]\nlanguage = "en"\n[tasks\n')
            self.assertEqual(ak.repo_language(root), "en")  # a broken config still gets its errors in English

    def test_english_cards_are_read_and_signed(self):
        ak.set_language("en")
        st = ak.card_status(CARD_EN)
        self.assertEqual((st["title"], st["lang"], st["missing"], st["confirmed"]),
                         ("WIP-1 Tell people when an item is out of stock", "en", [], "no"))
        self.assertEqual((len(st["open"]), st["decided"]), (1, 1))
        self.assertEqual([state for state, _ in st["steps"]], ["done", "doing", "todo"])
        self.assertEqual(st["handoff"]["下一步"], "handle 409 on the checkout page")
        self.assertEqual(ak.card_status(CARD_EN.replace("- Done when:", "- done WHEN:"))["missing"], [])
        self.assertEqual(ak.card_status(CARD_EN.replace("- Who needs it: people placing orders", "- Who needs it:"))["missing"],
                         ["Who needs it"])
        fp = ak.requirement_fingerprint(ak.section_of(ak.card_sections(CARD_EN), "需求"))
        signed_en = ak.set_confirmation(CARD_EN, f"Alice, 2026-11-01 (requirement fingerprint {fp})")
        self.assertIn(f"- Confirmed by: Alice, 2026-11-01 (requirement fingerprint {fp})\n", signed_en)
        st = ak.card_status(signed_en)
        self.assertEqual((st["confirmed"], st["confirmed_by"]), ("yes", "Alice, 2026-11-01"))
        self.assertEqual(ak.card_status(signed_en.replace("returns 409", "returns 400"))["confirmed"], "stale")
        self.assertEqual(ak.card_status(signed_en.replace("is merged", "is live"))["confirmed"], "yes")
        logged = ak.append_to_section(signed_en, "记录", "- a line")
        self.assertTrue(logged.endswith("## Log\n\n- a line\n"))
        self.assertIn("## Log\n\n- a line", ak.append_to_section(CARD_EN.replace("## Log\n", ""), "记录", "- a line"))

    def test_chinese_cards_are_read_the_same_in_an_english_repository(self):
        ak.set_language("en")
        st = ak.card_status(signed())
        self.assertEqual((st["lang"], st["confirmed"], st["confirmed_by"]), ("zh", "yes", "张三，2026-11-01"))
        self.assertEqual(ak.card_status(CARD.replace("- 满足谁：下单的用户", "- 满足谁："))["missing"], ["满足谁"])
        self.assertIn("- 确认：", ak.set_confirmation(CARD, "x"))  # written in the card's own language
        lines = "\n".join(ak.status_lines("WIP-1", ak.card_status(CARD)))
        self.assertIn("- Sign-off: **not yet**", lines)
        ak.set_language("zh")
        self.assertEqual(ak.card_status(CARD_EN)["handoff"]["做到"], "the API change is merged")

    def test_messages_and_toml_errors_follow_the_language(self):
        ak.set_language("en")
        self.assertIn("is out of credit", ak.hint_for("unknown", "Credit balance is too low"))
        self.assertEqual(ak.parse_result("## AI review\n")[1], ["no ai-review-result block found"])
        with self.assertRaisesRegex(ValueError, "agentkit.toml line 1: expected ] here"):
            ak.parse_toml("[a\nb = 1\n")

    def test_init_in_english_writes_english_everywhere(self):
        repo = TempRepo({"AGENTS.md": "rules\n", "src/a.py": "x\n"})
        try:
            root = repo.root
            env = env_without_hook_dir()
            init = subprocess.run([sys.executable, str(SCRIPT), "init", str(root), "--language", "en"],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(init.returncode, 0, init.stderr)
            self.assertIn("installed in", init.stdout)
            self.assertFalse(CJK.search(init.stdout))
            self.assertEqual(ak.load_config(root)["language"], "en")
            self.assertIn('[kit]\n# Language of everything agentkit writes and says: "en" (English) or "zh" (Chinese, the default)\n'
                          'language = "en"\n', (root / "agentkit.toml").read_text())
            en = KIT / "templates" / "en"
            pairs = [(en / name, root / ".agentkit" / name) for name in ("task-card.md", "team-workflow.md", "review-format.md")]
            pairs += [(src, root / ".agentkit/prompts" / src.name) for src in (en / "prompts").glob("*.md")]
            pairs += [(src, root / ".claude/commands" / src.name) for src in (en / "prompts").glob("*.md")
                      if src.stem not in ("reviewer", "start")]
            pairs += [(en / "workflow.yml", root / ".github/workflows/agentkit.yml")]
            self.assertEqual(len(pairs), 3 + 8 + 6 + 1)
            for src, dest in pairs:
                self.assertEqual(dest.read_text(), src.read_text(), dest)
            for path in [src for src, _ in pairs] + [root / n for n in ("agentkit.toml", "AGENTS.md", "CLAUDE.md", ".git/hooks/pre-commit")]:
                self.assertEqual([line for line in path.read_text().splitlines() if CJK.search(line)], [], path)
            self.assertIn("## Working with people (agentkit)", (root / "AGENTS.md").read_text())
            self.assertTrue((root / "AGENTS.md").read_text().startswith("rules\n"))

            # switching back is the project's call, made in agentkit.toml; init alone does not do it
            before = (root / "AGENTS.md").read_text()
            refused = subprocess.run([sys.executable, str(SCRIPT), "init", str(root), "--language", "zh"],
                                     capture_output=True, text=True, env=env)
            self.assertEqual(refused.returncode, 1)
            self.assertIn('[kit] language = "zh"', refused.stderr)
            self.assertEqual((root / "AGENTS.md").read_text(), before)
            config = (root / "agentkit.toml").read_text().replace('language = "en"', 'language = "zh"')
            (root / "agentkit.toml").write_text(config)
            switched = subprocess.run([sys.executable, str(SCRIPT), "init", str(root)], capture_output=True, text=True, env=env)
            self.assertEqual(switched.returncode, 0, switched.stderr)
            self.assertIn("已安装到", switched.stdout)
            self.assertEqual((root / ".agentkit/task-card.md").read_text(), (KIT / "templates/task-card.md").read_text())
            agents = (root / "AGENTS.md").read_text()
            self.assertEqual(agents.count(ak.BLOCK_START), 1)
            self.assertIn("## 和人协作的流程（agentkit）", agents)
            self.assertTrue(agents.startswith("rules\n"))
        finally:
            repo.close()

    def test_an_english_session_says_nothing_in_chinese(self):
        text = transcript(SCRIPT, "en")
        self.assertEqual([line for line in text.splitlines() if CJK.search(line)], [])
        for expected in ("Created task card docs/tasks/WIP-1.md",
                         "- Sign-off: **not yet**",
                         "agentkit: nobody has confirmed the requirement of the current task WIP-1 yet",
                         "- Confirmed by: t, <today> (requirement fingerprint ",
                         "- <time> · t · Kimi · feature/WIP-1-stock@<commit",
                         "### Automatic checks (agentkit checks)",
                         "**agentkit verdict** (round 1, head `abcdef1`): risk **high**",
                         "8. **This repository**: Final notes go in changes/",
                         "This repository's workflow is in `AGENTS.md`."):
            self.assertIn(expected, text)
        self.assertNotIn("stale", text)

    def test_a_chinese_card_works_in_an_english_repository(self):
        repo = TempRepo({"agentkit.toml": '[kit]\nlanguage = "en"\n' + toml_config(tasks={"required": True}),
                         "shop/orders.py": "x = 1\n", "docs/tasks/WIP-1.md": CARD})
        try:
            env = clean_env()
            repo.run("task", "use", "WIP-1", env=env)
            shown = repo.run("task", "show", "--brief", env=env).stdout
            self.assertIn("- Requirement: all three answered", shown)
            self.assertIn("- Next step: 结账页处理 409", shown)
            code, out, err = in_terminal([sys.executable, str(SCRIPT), "task", "confirm"], repo.root, env, "y\n")
            self.assertEqual(code, 0, out + err)
            card = (repo.root / "docs/tasks/WIP-1.md").read_text()
            self.assertIn("- 确认：t，", card)
            self.assertIn("（需求指纹 d4e11033）", card)
            self.assertIn("Confirmed. Remember to commit", out)
            repo.git("checkout", "-q", "-b", "feature")
            repo.write({"shop/orders.py": "x = 2\n"})
            repo.git("add", "shop", "docs")
            self.assertEqual(repo.run("hook", "pre-commit", env=env).returncode, 0)
            repo.git("commit", "-qm", "code")
            check = repo.run("task", "check", "--base", "main", "--committed-only", env=env)
            self.assertEqual(check.returncode, 0, check.stdout)
            self.assertIn("1 decision(s) still open", check.stdout)
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
