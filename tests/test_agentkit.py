import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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

    def test_other_agents_get_a_pointer_and_a_repo_without_agents_md_gets_one(self):
        (self.repo.root / "AGENTS.md").unlink()
        (self.repo.root / "agentkit.toml").write_text('[agents]\ninstruction_files = ["CLAUDE.md", "GEMINI.md"]\n')
        self.init()
        root = self.repo.root
        self.assertIn("被纠正过的规则", (root / "AGENTS.md").read_text())
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
        self.assertEqual(self.repo.run("hook", "pre-commit").returncode, 0)
        self.assertIn("已安装", self.repo.run("hook", "install-git").stdout)
        self.assertTrue((self.repo.root / ".git/hooks/pre-commit").exists())


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

    def test_api_engine_reports_a_missing_key(self):
        text, error = "", ""
        try:
            ak.run_api_engine({"type": "api", "base_url": "http://127.0.0.1:9", "model": "m", "key_env": "AGENTKIT_TEST_NO_KEY"}, "p", 5)
        except RuntimeError as e:
            error = str(e)
        self.assertIn("AGENTKIT_TEST_NO_KEY", error)


if __name__ == "__main__":
    unittest.main()
