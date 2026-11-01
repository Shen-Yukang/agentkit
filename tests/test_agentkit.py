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
           "requirements": "pr_body", "check_on_stop": True, "trusted_authors": ["github-actions[bot]"]}
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
                     ".claude/commands/repro.md", ".claude/commands/selfcheck.md", ".github/workflows/agentkit.yml"):
            self.assertTrue((root / path).exists(), path)
        self.assertEqual((root / ".claude/commands/restate.md").read_text(), "my own command\n")
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
        settings = json.loads(settings_before)
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1)
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)


if __name__ == "__main__":
    unittest.main()
