#!/usr/bin/env python3
"""agentkit: one generic kit, one config file (agentkit.toml) per repository.

Works with any coding agent that can read AGENTS.md and run shell commands; Claude Code
additionally gets hooks and slash commands. Agents without repo access get prompts to paste.

Commands:
  init <repo>                  install or upgrade the kit in a repository
  context [--for review]       print this repository's facts for an agent
  task new|show|list|use|confirm|handoff|check
                               task cards: requirement, human sign-off, split, handoff
  prompt [NAME] [TEXT...]      print a workflow prompt (restate, repro, ...) for any agent
  checks [--base REF] [--evidence FILE]
                               run the checks that apply to the changed files
  risk [--base REF]            path-based risk floor, as JSON
  review [--engines A,B] [--ci] [--print-prompt]
                               independent AI reviews (local CLIs or any API), merged
  validate FILE                check the review result block in FILE
  gate --review FILE [--pr N] [--base REF] [--head SHA] [--dry-run] [--comments-file F]
                               finalize an AI review, post it, label the PR, pass or fail
  hook pre-edit|stop|session-start|pre-commit|install-git
                               Claude Code hooks (hook JSON on stdin) and the git pre-commit hook

Requires Python 3.9+ (uses tomllib on 3.11+, a small built-in TOML reader before that). No third-party packages.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = "0.3.0"
OWNED = "由 agentkit 生成"  # files carrying this text belong to the kit and are replaced on upgrade
RESULT_RE = re.compile(r"<!--\s*ai-review-result\s*(.*?)\s*-->", re.S)
RISK_ORDER = ["low", "medium", "high"]
RISK_ZH = {"low": "低", "medium": "中", "high": "高"}
SEVERITIES = ("important", "nit", "pre-existing")
SCORE_KEYS = ("requirement", "evidence", "risk", "readability")
# Changing the gates themselves is always high risk.
DEFAULT_HIGH_RISK = [".github/workflows/**", "agentkit.toml", ".agentkit/**", ".claude/settings.json"]
HOOK_PRE = 'python3 "$CLAUDE_PROJECT_DIR/.agentkit/agentkit.py" hook pre-edit'
HOOK_STOP = 'python3 "$CLAUDE_PROJECT_DIR/.agentkit/agentkit.py" hook stop'
HOOK_START = 'python3 "$CLAUDE_PROJECT_DIR/.agentkit/agentkit.py" hook session-start'
CLI = "python3 .agentkit/agentkit.py"  # how agents and people call the kit inside a repository
POINTER = ".agentkit/current-task.local.md"  # this clone's current task id and its branch; ignored by git
TASK_FIELDS = ("满足谁", "解决什么", "怎么算做完")
HANDOFF_FIELDS = ("做到", "下一步", "试过不行的")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BLOCK_START = "<!-- agentkit:start"
BLOCK_END = "<!-- agentkit:end -->"
# Review engines anyone can use without configuring them; agentkit.toml can override or add more.
DEFAULT_ENGINES = {
    "claude": {"type": "command",
               "run": 'claude -p --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git show:*),Bash(git log:*)"'},
    "codex": {"type": "command", "run": "codex exec --sandbox read-only --output-last-message {output} -"},
    "claude-action": {"type": "github-action"},
}
MAX_DIFF_CHARS = 120_000
MAX_RULE_CHARS = 20_000
LABEL_COLORS = {"risk:low": "0E8A16", "risk:medium": "FBCA04", "risk:high": "D93F0B", "needs-human": "5319E7"}
KIT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------- config

def repo_root(start: Path | None = None) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and start is None:
        return Path(env)
    cwd = start or Path.cwd()
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return cwd


def load_config(root: Path) -> dict:
    data: dict = {}
    path = root / "agentkit.toml"
    if path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:  # Python < 3.11, e.g. the python3 that ships with macOS
            data = parse_toml(path.read_text(encoding="utf-8"))
        else:
            with open(path, "rb") as f:
                data = tomllib.load(f)
    rules, checks, paths = data.get("rules", {}), data.get("checks", {}), data.get("paths", {})
    high_risk = list(DEFAULT_HIGH_RISK)
    high_risk += [p for p in paths.get("high_risk", []) if p not in high_risk]
    return {
        "rules_always": list(rules.get("always", [])),
        "rules_review": list(rules.get("review", [])),
        "checks_always": list(checks.get("always", [])),
        "checks_by_path": [{"paths": list(e.get("paths", [])), "run": list(e.get("run", []))}
                           for e in checks.get("by_path", [])],
        "timeout": int(checks.get("timeout_seconds", 900)),
        "protected": list(paths.get("protected", [])),
        "high_risk": high_risk,
        "reviewers": {"low": 0, "medium": 1, "high": 2, **data.get("risk", {}).get("reviewers", {})},
        "requirements": data.get("requirements", {}).get("source", "pr_body"),
        "check_on_stop": bool(data.get("hooks", {}).get("check_on_stop", True)),
        "trusted_authors": list(data.get("review", {}).get("trusted_authors", ["github-actions[bot]"])),
        "tasks_dir": str(data.get("tasks", {}).get("dir", "docs/tasks")).strip("/"),
        "tasks_required": bool(data.get("tasks", {}).get("required", False)),
        "tasks_note": str(data.get("tasks", {}).get("note", "")).strip(),
        "block_before_confirm": bool(data.get("hooks", {}).get("block_edits_before_confirm", True)),
        "instruction_files": list(data.get("agents", {}).get("instruction_files", ["CLAUDE.md"])),
        "review_local": list(data.get("review", {}).get("local", ["claude"])),
        "review_ci": list(data.get("review", {}).get("ci", ["claude-action"])),
        "review_agree": max(1, int(data.get("review", {}).get("agree", 1))),
        "review_ci_setup": list(data.get("review", {}).get("ci_setup", [])),
        "engines": {**DEFAULT_ENGINES, **data.get("review", {}).get("engines", {})},
    }


class _TomlReader:
    """Enough TOML for agentkit.toml: tables, arrays of tables, strings, numbers, booleans, arrays, inline tables."""

    KEY = re.compile(r"[A-Za-z0-9_-]+")
    NUMBER = re.compile(r"[+-]?[0-9][0-9_]*(\.[0-9_]+)?([eE][+-]?[0-9]+)?")
    ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\"}

    def __init__(self, text: str):
        self.s, self.i = text, 0

    def fail(self, message: str):
        raise ValueError(f"agentkit.toml 第 {self.s.count(chr(10), 0, self.i) + 1} 行：{message}")

    def peek(self, n: int = 1) -> str:
        return self.s[self.i:self.i + n]

    def skip(self, newlines: bool = False) -> None:
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t\r" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def expect(self, token: str) -> None:
        if not self.s.startswith(token, self.i):
            self.fail(f"这里应该是 {token}")
        self.i += len(token)

    def parse(self) -> dict:
        root: dict = {}
        current = root
        while True:
            self.skip(newlines=True)
            if self.i >= len(self.s):
                return root
            if self.peek(2) == "[[":
                self.i += 2
                keys = self.keys()
                self.expect("]]")
                items = self.descend(root, keys[:-1]).setdefault(keys[-1], [])
                if not isinstance(items, list):
                    self.fail(f"{keys[-1]} 已经定义成了别的类型")
                current = {}
                items.append(current)
            elif self.peek() == "[":
                self.i += 1
                keys = self.keys()
                self.expect("]")
                current = self.descend(root, keys)
            else:
                keys = self.keys()
                self.expect("=")
                self.skip()
                self.descend(current, keys[:-1])[keys[-1]] = self.value()
            self.skip()
            if self.i < len(self.s) and self.s[self.i] != "\n":
                self.fail("这一行后面有多余的内容")

    def descend(self, table: dict, keys: list[str]) -> dict:
        for key in keys:
            nxt = table.setdefault(key, {})
            if isinstance(nxt, list) and nxt:
                nxt = nxt[-1]
            if not isinstance(nxt, dict):
                self.fail(f"{key} 不是表")
            table = nxt
        return table

    def keys(self) -> list[str]:
        out = []
        while True:
            self.skip()
            if self.peek() == '"':
                out.append(self.basic())
            elif self.peek() == "'":
                out.append(self.literal())
            else:
                m = self.KEY.match(self.s, self.i)
                if not m:
                    self.fail("缺少键名")
                out.append(m.group())
                self.i = m.end()
            self.skip()
            if self.peek() != ".":
                return out
            self.i += 1

    def value(self):
        if self.peek(3) in ('"""', "'''"):
            return self.multiline()
        c = self.peek()
        if c == '"':
            return self.basic()
        if c == "'":
            return self.literal()
        if c == "[":
            return self.array()
        if c == "{":
            return self.table()
        for word, val in (("true", True), ("false", False)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        m = self.NUMBER.match(self.s, self.i)
        if not m:
            self.fail("看不懂的值")
        self.i = m.end()
        text = m.group().replace("_", "")
        return float(text) if m.group(1) or m.group(2) else int(text)

    def unescape(self, raw: str) -> str:
        def one(m: re.Match) -> str:
            body = m.group(1)
            if body[0] in "uU":
                return chr(int(body[1:], 16))
            if body in self.ESCAPES:
                return self.ESCAPES[body]
            self.fail(f"不认识的转义 \\{body}")
        return re.sub(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", one, raw)

    def basic(self) -> str:
        m = re.compile(r'"((?:[^"\\\n]|\\.)*)"').match(self.s, self.i)
        if not m:
            self.fail("字符串没有结束")
        self.i = m.end()
        return self.unescape(m.group(1))

    def literal(self) -> str:
        end = self.s.find("'", self.i + 1)
        if end == -1 or "\n" in self.s[self.i + 1:end]:
            self.fail("字符串没有结束")
        value, self.i = self.s[self.i + 1:end], end + 1
        return value

    def multiline(self) -> str:
        quote = self.peek(3)
        self.i += 3
        if self.peek() == "\n":
            self.i += 1
        end = self.s.find(quote, self.i)
        if end == -1:
            self.fail("多行字符串没有结束")
        raw, self.i = self.s[self.i:end], end + 3
        return raw if quote == "'''" else self.unescape(re.sub(r"\\[ \t]*\n\s*", "", raw))

    def array(self) -> list:
        self.i += 1
        items = []
        while True:
            self.skip(newlines=True)
            if self.peek() == "]":
                self.i += 1
                return items
            items.append(self.value())
            self.skip(newlines=True)
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "]":
                self.fail("数组里缺少逗号")

    def table(self) -> dict:
        self.i += 1
        out: dict = {}
        self.skip()
        if self.peek() == "}":
            self.i += 1
            return out
        while True:
            keys = self.keys()
            self.expect("=")
            self.skip()
            self.descend(out, keys[:-1])[keys[-1]] = self.value()
            self.skip()
            if self.peek() == "}":
                self.i += 1
                return out
            self.expect(",")


def parse_toml(text: str) -> dict:
    return _TomlReader(text).parse()


def matches(path: str, pattern: str) -> bool:
    path = path.removeprefix("./")
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return path == pattern or path.startswith(pattern + "/")


def matching(files: list[str], patterns: list[str]) -> list[str]:
    return [f for f in files if any(matches(f, p) for p in patterns)]


# ---------------------------------------------------------------- git

def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def ref_exists(root: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", ref],
                          capture_output=True).returncode == 0


def default_base(root: Path) -> str:
    for ref in ("origin/main", "main", "origin/master", "master"):
        if ref_exists(root, ref):
            return ref
    return "HEAD"


def changed_files(root: Path, base: str, worktree: bool = True) -> list[str]:
    files: set[str] = set()
    if base != "HEAD":
        files |= set(git(root, "diff", "--name-only", f"{base}...HEAD").split())
    if worktree:
        files |= set(git(root, "diff", "--name-only", "HEAD").split())
        files |= set(git(root, "ls-files", "--others", "--exclude-standard").split())
    return sorted(files)


# ---------------------------------------------------------------- checks

def plan_checks(cfg: dict, files: list[str]) -> list[tuple[str, str]]:
    plan = [(cmd, "每次都跑") for cmd in cfg["checks_always"]]
    seen = {cmd for cmd, _ in plan}
    for entry in cfg["checks_by_path"]:
        hit = matching(files, entry["paths"])
        if not hit:
            continue
        reason = f"改到 {hit[0]}" + (f" 等 {len(hit)} 个文件" if len(hit) > 1 else "")
        for cmd in entry["run"]:
            if cmd not in seen:
                plan.append((cmd, reason))
                seen.add(cmd)
    return plan


def run_checks(root: Path, plan: list[tuple[str, str]], timeout: int) -> list[dict]:
    results = []
    for cmd, reason in plan:
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout)
            code, output = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            code, output = 124, f"超时（超过 {timeout} 秒）"
        results.append({"cmd": cmd, "reason": reason, "code": code,
                        "seconds": round(time.monotonic() - start, 1),
                        "tail": "\n".join(output.strip().splitlines()[-30:])})
    return results


def evidence_md(base: str, head: str, files: list[str], results: list[dict], uncovered: list[str]) -> str:
    lines = ["### 自动检查（agentkit checks）", "",
             f"对比：`{base}` → `{head}`；改动文件 {len(files)} 个。", ""]
    if not results:
        lines.append("agentkit.toml 里没有配置任何检查。")
    else:
        lines += ["| 命令 | 为什么跑 | 结果 | 用时 |", "|---|---|---|---|"]
        for r in results:
            verdict = "通过" if r["code"] == 0 else f"**失败**（退出码 {r['code']}）"
            lines.append(f"| `{r['cmd']}` | {r['reason']} | {verdict} | {r['seconds']}s |")
        for r in results:
            if r["code"] != 0:
                lines += ["", f"<details><summary><code>{r['cmd']}</code> 的最后 30 行输出</summary>", "",
                          "```text", r["tail"], "```", "", "</details>"]
    if uncovered:
        shown = "、".join(f"`{f}`" for f in uncovered[:5])
        more = f" 等 {len(uncovered)} 个" if len(uncovered) > 5 else ""
        lines += ["", f"没有专门检查覆盖的改动：{shown}{more}（只跑了每次都跑的检查）。"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- review results

def risk_floor(cfg: dict, files: list[str]) -> dict:
    protected = matching(files, cfg["protected"])
    high = matching(files, cfg["high_risk"])
    return {"floor": "high" if protected or high else "low", "protected": protected, "high_risk": high}


def parse_result(text: str) -> tuple[dict | None, list[str]]:
    blocks = RESULT_RE.findall(text)
    if not blocks:
        return None, ["没有找到 ai-review-result 段"]
    try:
        result = json.loads(blocks[-1])
    except json.JSONDecodeError as e:
        return None, [f"ai-review-result 不是合法的 JSON：{e}"]
    if not isinstance(result, dict):
        return None, ["ai-review-result 必须是一个 JSON 对象"]
    errors = []
    scores = result.get("scores")
    if not isinstance(scores, dict):
        errors.append("scores 必须是对象")
    else:
        for key in SCORE_KEYS:
            value = scores.get(key)
            if value is None and key == "requirement":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
                errors.append(f"scores.{key} 必须是 1–5 的整数")
    if result.get("risk_level") not in RISK_ORDER:
        errors.append("risk_level 只能是 low、medium、high")
    issues = result.get("issues")
    if not isinstance(issues, list):
        errors.append("issues 必须是列表")
    else:
        for i, issue in enumerate(issues):
            if not isinstance(issue, dict):
                errors.append(f"issues[{i}] 必须是对象")
                continue
            if not isinstance(issue.get("file"), str) or not issue["file"]:
                errors.append(f"issues[{i}].file 必须是文件路径")
            line = issue.get("line")
            if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
                errors.append(f"issues[{i}].line 必须是整数或 null")
            if issue.get("severity") not in SEVERITIES:
                errors.append(f"issues[{i}].severity 只能是 important、nit、pre-existing")
            if not isinstance(issue.get("confirmed"), bool):
                errors.append(f"issues[{i}].confirmed 必须是 true 或 false")
            if not isinstance(issue.get("summary"), str) or not issue["summary"].strip():
                errors.append(f"issues[{i}].summary 不能为空")
    return result, errors


def decide(cfg: dict, review: dict, floor: dict, prior_rounds: int) -> dict:
    final = max(review["risk_level"], floor["floor"], key=RISK_ORDER.index)
    important = [i for i in review["issues"] if i["severity"] == "important"]
    blockers = [i for i in important if i["confirmed"]]
    round_ = prior_rounds + 1
    labels = [f"risk:{final}"] + (["needs-human"] if round_ >= 3 and blockers else [])
    return {"round": round_, "risk_level": final, "ai_risk_level": review["risk_level"], "floor": floor,
            "reviewers_needed": int(cfg["reviewers"].get(final, 0)), "blockers": blockers,
            "unconfirmed": [i for i in important if not i["confirmed"]], "labels": labels,
            "passed": not blockers}


def finalize_comment(text: str, review: dict, decision: dict, head: str) -> str:
    result = {**review, "head": head, "round": decision["round"], "risk_level": decision["risk_level"],
              "ai_risk_level": decision["ai_risk_level"], "reviewers_needed": decision["reviewers_needed"]}
    body = RESULT_RE.sub("", text).rstrip()
    floor = decision["floor"]
    why = ""
    if decision["risk_level"] != decision["ai_risk_level"]:
        hits = (floor["protected"] + floor["high_risk"])[:3]
        why = f"（AI 初判为{RISK_ZH[decision['ai_risk_level']]}；按 agentkit.toml 的路径规则上调：改到 " + "、".join(f"`{h}`" for h in hits) + "）"
    if decision["passed"]:
        verdict = "没有已确认的 Important 问题，检查通过"
    else:
        verdict = f"有 {len(decision['blockers'])} 个已确认的 Important 问题，修复前不通过"
    if decision["unconfirmed"]:
        verdict += f"；另有 {len(decision['unconfirmed'])} 个待确认的 Important，请人判断"
    footer = (f"\n\n---\n**agentkit 判定**（第 {decision['round']} 轮，head `{head[:7]}`）："
              f"风险 **{RISK_ZH[decision['risk_level']]}**{why}；建议 {decision['reviewers_needed']} 人审；{verdict}。")
    if "needs-human" in decision["labels"]:
        footer += "\n已经是第 3 轮以上仍有阻塞问题：加上 `needs-human`，建议换个思路或找人当面讨论。"
    block = "<!-- ai-review-result\n" + json.dumps(result, ensure_ascii=False) + "\n-->"
    return body + footer + "\n\n" + block + "\n"


# ---------------------------------------------------------------- task cards
#
# One Markdown file per task, shared by people and every agent: the requirement (three questions),
# the human sign-off, decisions only people make, the split, and the handoff. Agents read it when
# they start and update it before they stop, so work survives a new chat, a new agent or a new person.

def kit_file(*parts: str) -> Path:
    """A kit file: next to an installed agentkit.py (.agentkit/...), or under templates/ in the kit repo."""
    installed = KIT_DIR.joinpath(*parts)
    return installed if installed.exists() else KIT_DIR.joinpath("templates", *parts)


def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def card_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    for line in strip_comments(text).splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1)
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


def field_line(name: str) -> re.Pattern:
    """A "- name：value" line. The name may be bold or carry a note: "- **怎么算做完**（可检查）：…"."""
    gap = r"[ \t]*"  # never \s: a match must not run into the next line
    return re.compile(rf"^({gap}[-*]{gap})\**{re.escape(name)}\**{gap}(?:[（(][^）)\n]*[）)])?{gap}\**{gap}[：:]{gap}\**{gap}([^\n]*)$", re.M)


def field_value(section: str, name: str) -> str:
    """The text after "- name：", plus any indented lines under it."""
    lines = section.splitlines()
    pattern = field_line(name)
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        parts = [m.group(2).strip()]
        for nxt in lines[i + 1:]:
            if not nxt.strip() or not nxt[0].isspace():
                break
            parts.append(nxt.strip())
        return " ".join(p for p in parts if p).strip()
    return ""


def requirement_fingerprint(section: str) -> str:
    """Hash of the requirement without the sign-off line: any later edit makes the sign-off stale."""
    kept = [line for line in section.splitlines() if not field_line("确认").match(line)]
    normalized = re.sub(r"\s+", " ", "\n".join(kept)).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def card_status(text: str) -> dict:
    secs = card_sections(text)
    req = secs.get("需求", "")
    confirm = field_value(req, "确认")
    fp = re.search(r"需求指纹\s*([0-9a-f]{8})", confirm)
    if not confirm:
        confirmed = "no"
    elif fp and fp.group(1) == requirement_fingerprint(req):
        confirmed = "yes"
    else:
        confirmed = "stale"
    decisions = secs.get("要人拍板的事", "")
    steps = re.findall(r"(?m)^\s*(?:[-*]|\d+[.)])\s*\[(todo|doing|done)\]\s*(.*)$", secs.get("拆分", ""))
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
    return {
        "title": title,
        "missing": [f for f in TASK_FIELDS if not field_value(req, f)],
        "confirmed": confirmed,
        "confirmed_by": re.sub(r"[（(]需求指纹.*?[）)]", "", confirm).strip(),
        "open": [t.strip() for t in re.findall(r"(?m)^\s*[-*]\s*\[ \]\s*(.+)$", decisions)],
        "decided": len(re.findall(r"(?m)^\s*[-*]\s*\[[xX]\]", decisions)),
        "steps": [(state, name.strip()) for state, name in steps],
        "handoff": {k: field_value(secs.get("交接", ""), k) for k in HANDOFF_FIELDS},
        "handoff_text": re.sub(r"\s+", " ", secs.get("交接", "")).strip(),
    }


def status_lines(task_id: str, st: dict) -> list[str]:
    lines = ["- 需求：" + ("三问都写了" if not st["missing"] else "还缺 " + "、".join(f"「{f}」" for f in st["missing"]))]
    if st["confirmed"] == "yes":
        lines.append(f"- 确认：{st['confirmed_by']}")
    elif st["confirmed"] == "stale":
        lines.append(f"- 确认：**已失效**，确认之后需求又改过，请人重新运行 `{CLI} task confirm {task_id}`")
    else:
        lines.append(f"- 确认：**还没有**。先把复述写进任务卡，再请人运行 `{CLI} task confirm {task_id}`")
    if st["open"]:
        shown = "；".join(st["open"][:3]) + (" 等" if len(st["open"]) > 3 else "")
        lines.append(f"- 要人拍板：还有 {len(st['open'])} 件没定：{shown}")
    else:
        lines.append("- 要人拍板：没有待定的事" + (f"（已定 {st['decided']} 件）" if st["decided"] else ""))
    if st["steps"]:
        done = sum(1 for state, _ in st["steps"] if state == "done")
        doing = [name for state, name in st["steps"] if state == "doing"]
        lines.append(f"- 拆分：完成 {done}/{len(st['steps'])}" + (f"；正在做：{doing[0]}" if doing else ""))
    else:
        lines.append("- 拆分：还没拆")
    nxt = st["handoff"]["下一步"]
    lines.append(f"- 下一步：{nxt}" if nxt else "- 下一步：（「交接」里还没写）")
    return lines


def rule_reminder(st: dict) -> str:
    if st["confirmed"] != "yes":
        return "按约定：需求被人确认之前，除了这张任务卡，不要改别的文件。"
    return (f"按约定：一次只做「拆分」里的一块，做完运行 `{CLI} checks` 拿证据；"
            f"收工前更新「交接」，再运行 `{CLI} task handoff --agent <你是哪个 agent>`。")


def tasks_prefix(cfg: dict) -> str:
    return cfg["tasks_dir"] + "/"


def card_path(root: Path, cfg: dict, task_id: str) -> Path:
    return root / cfg["tasks_dir"] / f"{task_id}.md"


def card_ids(root: Path, cfg: dict) -> list[str]:
    folder = root / cfg["tasks_dir"]
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.md") if p.name.lower() != "readme.md")


def card_files(cfg: dict, files: list[str]) -> list[str]:
    prefix = tasks_prefix(cfg)
    return [f for f in files if f.startswith(prefix) and f.endswith(".md")
            and "/" not in f[len(prefix):] and f.lower() != (prefix + "readme.md").lower()]


def ids_in(text: str, ids: list[str]) -> list[str]:
    return [i for i in ids if re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![A-Za-z0-9])", text)]


def current_task(root: Path, cfg: dict, explicit: str | None = None, base: str | None = None) -> tuple[str | None, str]:
    """Find this session's task card: given id, then this clone's pointer, the branch name, the cards changed on the branch."""
    ids = card_ids(root, cfg)
    if explicit:
        task_id = Path(explicit).stem if explicit.endswith(".md") else explicit
        return (task_id, "命令行指定") if task_id in ids else (None, f"找不到任务卡 {cfg['tasks_dir']}/{task_id}.md")
    try:
        branch = current_branch(root)
        pointed = read_pointer(root)
        if pointed and pointed[0] in ids and pointed[1] in ("", branch):
            return pointed[0], "当前任务（task new / task use 设置的）"
        hit = ids_in(branch, ids)
        if len(hit) == 1:
            return hit[0], f"分支名 {branch}"
        changed = [Path(f).stem for f in card_files(cfg, changed_files(root, base or default_base(root)))]
    except subprocess.CalledProcessError:
        return None, ""
    changed = [c for c in changed if c in ids]
    if len(changed) == 1:
        return changed[0], "这个分支改过的任务卡"
    return None, ""


def no_task_text(root: Path, cfg: dict) -> str:
    ids = card_ids(root, cfg)
    return "\n".join([
        "没有找到当前任务的任务卡。",
        f"- 新任务：运行 `{CLI} task new <任务号> <一句话标题>`，把复述写进任务卡，请人确认。",
        f"- 接着做已有的任务：运行 `{CLI} task use <任务号>`。"
        + ("已有的任务卡：" + "、".join(ids[:10]) + (" 等" if len(ids) > 10 else "") if ids else "现在还没有任务卡。"),
    ])


def section_span(text: str, heading: str) -> tuple[int, int] | None:
    m = re.search(rf"(?m)^##\s+{re.escape(heading)}\s*$", text)
    if not m:
        return None
    nxt = re.search(r"(?m)^##\s+", text[m.end():])
    return m.end(), (m.end() + nxt.start() if nxt else len(text))


def append_to_section(text: str, heading: str, line: str) -> str:
    span = section_span(text, heading)
    if span is None:
        return text.rstrip("\n") + f"\n\n## {heading}\n\n{line}\n"
    start, end = span
    body = text[start:end].rstrip("\n")
    rest = text[end:]
    return text[:start] + body + "\n" + line + "\n" + ("\n" + rest if rest else "")


def set_confirmation(text: str, value: str) -> str:
    span = section_span(text, "需求")
    if span is None:
        raise ValueError("任务卡里没有「## 需求」一节")
    start, end = span
    section = text[start:end]
    line_re = field_line("确认")
    if line_re.search(section):
        section = line_re.sub(lambda m: f"{m.group(1)}确认：{value}", section, count=1)
    else:
        section = section.rstrip("\n") + f"\n- 确认：{value}\n\n"
    return text[:start] + section + text[end:]


def git_user(root: Path) -> str:
    try:
        name = git(root, "config", "user.name").strip()
    except subprocess.CalledProcessError:
        name = ""
    return name or os.environ.get("USER", "未知")


def detect_agent() -> str:
    if os.environ.get("AGENTKIT_AGENT"):
        return os.environ["AGENTKIT_AGENT"]
    if os.environ.get("CLAUDECODE"):
        return "Claude Code"
    return "未注明 agent"


def current_branch(root: Path) -> str:
    try:
        return git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except subprocess.CalledProcessError:
        return ""


def read_pointer(root: Path) -> tuple[str, str] | None:
    """(task id, branch it was set on). The pointer only counts on that branch, so a switch of branch drops it."""
    pointer = root / POINTER
    if not pointer.exists():
        return None
    lines = pointer.read_text().split()
    return (lines[0], lines[1] if len(lines) > 1 else "") if lines else None


def set_pointer(root: Path, task_id: str | None) -> None:
    pointer = root / POINTER
    if task_id is None:
        pointer.unlink(missing_ok=True)
        return
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(f"{task_id}\n{current_branch(root)}\n")


def task_new(root: Path, cfg: dict, args: argparse.Namespace) -> int:
    task_id = args.id
    if not TASK_ID_RE.match(task_id):
        print("任务号只能用字母、数字、点、横线和下划线，比如 WIP-123", file=sys.stderr)
        return 1
    path = card_path(root, cfg, task_id)
    rel = os.path.relpath(path, root)
    if path.exists():
        set_pointer(root, task_id)
        print(f"任务卡已经存在：{rel}，已设为当前任务。运行 `{CLI} task show` 查看。")
        return 1
    title = " ".join(args.title).strip() or "（标题）"
    template = kit_file("task-card.md").read_text()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(drop_owned_marker(template).replace("{{id}}", task_id).replace("{{title}}", title))
    set_pointer(root, task_id)
    print("\n".join([
        f"已新建任务卡 {rel}，并设为当前任务。",
        "下一步：",
        f"1. 让 agent 复述需求、写进这张卡：Claude Code 用 /restate；其他 agent 运行 `{CLI} prompt restate \"<需求>\"`。",
        f"2. 人看过之后，在自己的终端里运行 `{CLI} task confirm {task_id}`。",
    ]))
    return 0


def task_show(root: Path, cfg: dict, args: argparse.Namespace) -> int:
    task_id, how = current_task(root, cfg, args.id, args.base)
    if not task_id:
        print((how + "\n\n" if args.id else "") + no_task_text(root, cfg))
        return 1 if args.id else 0
    path = card_path(root, cfg, task_id)
    text = path.read_text()
    st = card_status(text)
    rel = os.path.relpath(path, root)
    out = [f"当前任务：{st['title'] or task_id}（`{rel}`，{how}）"]
    if not args.brief:
        out = [f"任务卡 `{rel}`（{how}）：", "", text.rstrip(), "", "---"]
    out += ["状态（agentkit 读出来的）："] + status_lines(task_id, st) + ["", rule_reminder(st)]
    if cfg["tasks_note"]:
        out.append(f"本仓库：{cfg['tasks_note']}")
    print("\n".join(out))
    return 0


def task_list(root: Path, cfg: dict, args: argparse.Namespace) -> int:
    ids = card_ids(root, cfg)
    if not ids:
        print(no_task_text(root, cfg))
        return 0
    current, _ = current_task(root, cfg)
    mark = {"yes": "已确认", "no": "未确认", "stale": "确认已失效"}
    for task_id in ids:
        st = card_status(card_path(root, cfg, task_id).read_text())
        done = sum(1 for state, _ in st["steps"] if state == "done")
        parts = [mark[st["confirmed"]], f"拆分 {done}/{len(st['steps'])}"]
        if st["open"]:
            parts.append(f"待拍板 {len(st['open'])}")
        print(f"{'*' if task_id == current else ' '} {st['title'] or task_id}：{'，'.join(parts)}")
    return 0


def task_use(root: Path, cfg: dict, args: argparse.Namespace) -> int:
    if args.clear:
        set_pointer(root, None)
        print("已清除当前任务。")
        return 0
    if not args.id or args.id not in card_ids(root, cfg):
        print(no_task_text(root, cfg), file=sys.stderr)
        return 1
    set_pointer(root, args.id)
    print(f"当前任务：{args.id}。运行 `{CLI} task show` 查看任务卡。")
    return 0


def task_confirm(root: Path, cfg: dict, args: argparse.Namespace) -> int:
    if os.environ.get("CLAUDECODE") or not sys.stdin.isatty():
        print("确认需求要由人在自己的终端里运行这条命令；agent 不能替人确认。", file=sys.stderr)
        return 2
    task_id, how = current_task(root, cfg, args.id)
    if not task_id:
        print(how or no_task_text(root, cfg), file=sys.stderr)
        return 1
    path = card_path(root, cfg, task_id)
    text = path.read_text()
    st = card_status(text)
    if st["missing"]:
        print("需求还缺 " + "、".join(f"「{f}」" for f in st["missing"]) + "，先补上再确认。", file=sys.stderr)
        return 1
    print(f"任务 {st['title'] or task_id} 的需求：\n")
    print(strip_comments(card_sections(text)["需求"]).strip())
    if st["open"]:
        print(f"\n注意：还有 {len(st['open'])} 件事没拍板（确认需求不代表这些也定了）：")
        for item in st["open"]:
            print(f"  - {item}")
    try:
        answer = input("\n这就是你要的吗？输入 y 确认：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes", "是", "确认"):
        print("没有确认。可以让 agent 按你的意见改任务卡，再运行一次。")
        return 1
    value = f"{git_user(root)}，{time.strftime('%Y-%m-%d')}（需求指纹 {requirement_fingerprint(card_sections(text)['需求'])}）"
    path.write_text(set_confirmation(text, value))
    print(f"已确认。记得把 {os.path.relpath(path, root)} 提交；agent 现在可以按「拆分」动手了。")
    return 0


def task_handoff(root: Path, cfg: dict, args: argparse.Namespace) -> int:
    task_id, how = current_task(root, cfg, args.id)
    if not task_id:
        print(how or no_task_text(root, cfg), file=sys.stderr)
        return 1
    path = card_path(root, cfg, task_id)
    text = path.read_text()
    st = card_status(text)
    missing = [k for k in ("做到", "下一步") if not st["handoff"][k]]
    if missing:
        print("先在任务卡的「交接」里写清" + "、".join(f"「{k}」" for k in missing) + "，再运行这条命令。", file=sys.stderr)
        return 1
    try:
        where = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() + "@" + git(root, "rev-parse", "--short", "HEAD").strip()
    except subprocess.CalledProcessError:
        where = "（还没有提交）"
    done = sum(1 for state, _ in st["steps"] if state == "done")
    line = (f"- {time.strftime('%Y-%m-%d %H:%M')} · {git_user(root)} · {args.agent or detect_agent()} · {where}"
            f" · 拆分完成 {done}/{len(st['steps'])} · 下一步：{st['handoff']['下一步']}")
    path.write_text(append_to_section(text, "记录", line))
    print("已记下交接：\n" + line + "\n\n" + "\n".join(status_lines(task_id, st)))
    return 0


def task_check(root: Path, cfg: dict, args: argparse.Namespace) -> int:
    """For a PR (or the current branch): is there a task card, is the requirement signed off, was the handoff updated?"""
    base = args.base or default_base(root)
    files = changed_files(root, base, worktree=not args.committed_only)
    prefix = tasks_prefix(cfg)
    code = [f for f in files if not f.startswith(prefix)]
    cards = [Path(f).stem for f in card_files(cfg, files) if (root / f).exists()]
    if not cards:
        mentioned = args.branch or ""
        if args.pr_body_file and Path(args.pr_body_file).exists():
            mentioned += "\n" + Path(args.pr_body_file).read_text()
        cards = ids_in(mentioned, card_ids(root, cfg))
    errors: list[str] = []
    warnings: list[str] = []
    report = ["### 任务卡检查（agentkit task check）", ""]
    if not cards:
        if code:
            errors.append(f"这次改了代码，但找不到任务卡（`{prefix}<任务号>.md`）。新任务运行 `{CLI} task new <任务号> <标题>`；"
                          "已有任务，在 PR 描述里写上任务号。")
        else:
            report.append("这次没有改代码，也没有任务卡，跳过。")
    for task_id in cards:
        rel = f"{prefix}{task_id}.md"
        text = (root / rel).read_text()
        st = card_status(text)
        report += [f"#### {st['title'] or task_id}（`{rel}`）", ""] + status_lines(task_id, st) + [""]
        if st["missing"]:
            errors.append(f"{task_id}：需求还缺 " + "、".join(f"「{f}」" for f in st["missing"]))
        if st["confirmed"] == "no":
            errors.append(f"{task_id}：需求还没有人确认（请人运行 `{CLI} task confirm {task_id}`）")
        elif st["confirmed"] == "stale":
            errors.append(f"{task_id}：确认之后需求又改过，请人重新确认")
        if st["open"]:
            warnings.append(f"{task_id}：还有 {len(st['open'])} 件事没拍板")
        if code:
            if rel not in files:
                warnings.append(f"{task_id}：这次改了代码，但任务卡没有更新（拆分状态、交接）")
            else:
                try:
                    before = git(root, "show", f"{base}:{rel}")
                except subprocess.CalledProcessError:
                    before = ""
                if card_status(before)["handoff_text"] == st["handoff_text"]:
                    warnings.append(f"{task_id}：任务卡的「交接」没有更新")
    if not cfg["tasks_required"]:
        warnings, errors = errors + warnings, []
    if errors:
        report += ["**不通过：**"] + [f"- {e}" for e in errors] + [""]
    if warnings:
        report += ["**提醒：**"] + [f"- {w}" for w in warnings] + [""]
    if not errors and not warnings and cards:
        report.append("任务卡齐全，需求已确认，交接已更新。")
    text = "\n".join(report).rstrip() + "\n"
    print(text)
    if os.environ.get("GITHUB_ACTIONS"):
        for e in errors:
            print(f"::error title=任务卡::{e}")
        for w in warnings:
            print(f"::warning title=任务卡::{w}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text)
    return 1 if errors else 0


TASK_ACTIONS = {"new": task_new, "show": task_show, "list": task_list, "use": task_use,
                "confirm": task_confirm, "handoff": task_handoff, "check": task_check}


def cmd_task(args: argparse.Namespace) -> int:
    root = repo_root()
    return TASK_ACTIONS[args.action](root, load_config(root), args)


# ---------------------------------------------------------------- GitHub (gh CLI)

def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def prior_comments(pr: str) -> list[dict]:
    repo = os.environ.get("GITHUB_REPOSITORY", "{owner}/{repo}")
    out = gh("api", "--paginate", f"repos/{repo}/issues/{pr}/comments",
             "--jq", ".[] | {author: .user.login, body: .body}")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def count_prior_rounds(cfg: dict, comments: list[dict]) -> int:
    return sum(1 for c in comments
               if c.get("author") in cfg["trusted_authors"] and RESULT_RE.search(c.get("body") or ""))


def apply_labels(pr: str, labels: list[str]) -> None:
    for label in labels:
        gh("label", "create", label, "--color", LABEL_COLORS.get(label, "BFD4F2"), "--force")
    current = gh("pr", "view", pr, "--json", "labels", "--jq", ".labels[].name").split()
    stale = [l for l in current if l.startswith("risk:") and l not in labels]
    args = ["pr", "edit", pr]
    for label in labels:
        args += ["--add-label", label]
    for label in stale:
        args += ["--remove-label", label]
    gh(*args)


# ---------------------------------------------------------------- hooks

def proposed_content(path: Path, tool_input: dict) -> str:
    """What the file would contain after a Claude Code Write, Edit or MultiEdit."""
    if "content" in tool_input:
        return tool_input["content"] or ""
    current = path.read_text() if path.exists() else ""
    edits = tool_input.get("edits") or ([tool_input] if "old_string" in tool_input else [])
    for e in edits:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        current = current.replace(old, new) if e.get("replace_all") else current.replace(old, new, 1)
    return current


def unconfirmed_task(root: Path, cfg: dict) -> tuple[str, str] | None:
    """(task id, card path) when the current task's requirement is not signed off (or went stale)."""
    task_id, _ = current_task(root, cfg)
    if not task_id:
        return None
    path = card_path(root, cfg, task_id)
    if card_status(path.read_text())["confirmed"] == "yes":
        return None
    return task_id, os.path.relpath(path, root)


def hook_pre_edit(root: Path, cfg: dict, payload: dict) -> int:
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return 0
    if os.path.isabs(path):
        rel = os.path.relpath(Path(path).resolve(), root.resolve())
    else:
        rel = path
    rel = rel.replace(os.sep, "/")
    if rel.startswith(".."):
        return 0
    hit = [p for p in cfg["protected"] if matches(rel, p)]
    if hit:
        print(f"agentkit：{rel} 是受保护路径（{hit[0]}，见 agentkit.toml），不能直接修改。"
              "如果确实需要改，停下来说明原因，请人决定。", file=sys.stderr)
        return 2
    if rel.startswith(tasks_prefix(cfg)):
        card = root / rel
        before = field_value(card_sections(card.read_text() if card.exists() else "").get("需求", ""), "确认")
        after = field_value(card_sections(proposed_content(card, tool_input)).get("需求", ""), "确认")
        if after and after != before:
            print(f"agentkit：任务卡里的「确认」只能由人填写（人在自己的终端运行 `{CLI} task confirm`）。"
                  "你可以写复述、列出要拍板的事，然后停下请人确认。", file=sys.stderr)
            return 2
        return 0
    if cfg["block_before_confirm"] and rel != POINTER:
        pending = unconfirmed_task(root, cfg)
        if pending:
            task_id, card = pending
            print(f"agentkit：当前任务 {task_id} 的需求还没有人确认（或确认后又改过），先别改代码。"
                  f"把复述写进任务卡 {card}，停下来请人运行 `{CLI} task confirm {task_id}`。"
                  f"如果这次做的不是这个任务，请人运行 `{CLI} task use --clear` 或 `task use <任务号>`。", file=sys.stderr)
            return 2
    return 0


def handoff_missing(root: Path, cfg: dict) -> str | None:
    """Card path when code changed in the working tree but the current task card did not."""
    task_id, _ = current_task(root, cfg)
    if not task_id:
        return None
    card = os.path.relpath(card_path(root, cfg, task_id), root).replace(os.sep, "/")
    dirty = changed_files(root, "HEAD")
    code = [f for f in dirty if not f.startswith(tasks_prefix(cfg)) and not f.startswith(".agentkit/")]
    return card if code and card not in dirty else None


def hook_stop(root: Path, cfg: dict, payload: dict) -> int:
    if payload.get("stop_hook_active") or not git(root, "status", "--porcelain").strip():
        return 0
    parts: list[str] = []
    if cfg["check_on_stop"] and cfg["checks_always"]:
        results = run_checks(root, [(c, "每次都跑") for c in cfg["checks_always"]], min(cfg["timeout"], 300))
        failed = [r for r in results if r["code"] != 0]
        if failed:
            parts.append("agentkit：结束前的自动检查没通过：")
            for r in failed:
                parts += [f"- `{r['cmd']}` 退出码 {r['code']}", r["tail"][-1500:]]
            parts.append("请修好后再结束；如果这次不该修，说明原因。")
    card = handoff_missing(root, cfg)
    if card:
        parts.append(f"agentkit：这次改了代码，但任务卡 {card} 还没更新。收工前更新「拆分」的状态和「交接」"
                     f"（做到、下一步、试过不行的），再运行 `{CLI} task handoff --agent \"Claude Code\"`。")
    if not parts:
        return 0
    print("\n".join(parts), file=sys.stderr)
    return 2


def hook_session_start(root: Path, cfg: dict) -> int:
    lines = ["agentkit：本仓库按任务卡协作，流程见 AGENTS.md 和 .agentkit/team-workflow.md。"]
    task_id, how = current_task(root, cfg)
    if task_id:
        st = card_status(card_path(root, cfg, task_id).read_text())
        lines += [f"当前任务：{st['title'] or task_id}（{how}）"] + status_lines(task_id, st)
        lines += [f"开工前先运行 `{CLI} task show` 读完整任务卡。", rule_reminder(st)]
    else:
        lines.append(no_task_text(root, cfg))
    print("\n".join(lines))
    return 0


def hook_pre_commit(root: Path, cfg: dict) -> int:
    """git pre-commit: works for every agent and every person, not just Claude Code."""
    staged = git(root, "diff", "--cached", "--name-only").split()
    code = [f for f in staged if not f.startswith(tasks_prefix(cfg))]
    if not code:
        return 0
    task_id, _ = current_task(root, cfg)
    if not task_id:
        print(f"agentkit 提醒：这次提交改了代码，但没有当前任务卡（`{CLI} task show`）。", file=sys.stderr)
        return 0
    if card_status(card_path(root, cfg, task_id).read_text())["confirmed"] != "yes":
        print(f"agentkit：任务 {task_id} 的需求还没有人确认（或确认后又改过），先不提交代码。"
              f"请人运行 `{CLI} task confirm {task_id}`；如果这次提交不属于这个任务，运行 `{CLI} task use --clear`。",
              file=sys.stderr)
        return 1
    return 0


GIT_HOOK_LINE = "python3 .agentkit/agentkit.py hook pre-commit"
GIT_HOOK_GUARDED = f"[ ! -f .agentkit/agentkit.py ] || {GIT_HOOK_LINE}  # agentkit：需求没确认前不提交代码"
SHIPPED_HOOK_DIRS = (".githooks", ".husky")


def add_to_hook(path: Path) -> bool:
    """Add the kit's line to a hook the repository ships; before a final exec, which would never return."""
    text = path.read_text()
    if GIT_HOOK_LINE in text:
        return False
    lines = text.rstrip("\n").split("\n")
    at = len(lines) - 1 if lines and lines[-1].lstrip().startswith("exec ") else len(lines)
    lines.insert(at, GIT_HOOK_GUARDED)
    path.write_text("\n".join(lines) + "\n")
    return True


def install_git_hook(root: Path) -> str:
    """Hook the kit into git commits. Returns what happened, for the install report.

    A repository that ships its own hooks (core.hooksPath, .githooks/, .husky/) gets one line in its pre-commit,
    so everyone who enables the repository's hooks gets the check; .git/hooks is left alone so the repository's
    own installer is not blocked. Otherwise the hook goes into this clone's .git/hooks.
    """
    custom = subprocess.run(["git", "-C", str(root), "config", "core.hooksPath"], capture_output=True, text=True).stdout.strip()
    shipped = None
    if custom:
        candidate = (Path(custom) if os.path.isabs(custom) else root / custom).resolve() / "pre-commit"
        if candidate.is_file() and candidate.is_relative_to(root.resolve()):
            shipped = candidate
        else:
            return f"git 钩子目录是 {custom}：请在其中的 pre-commit 里加一行 `{GIT_HOOK_GUARDED}`"
    else:
        shipped = next((root / d / "pre-commit" for d in SHIPPED_HOOK_DIRS if (root / d / "pre-commit").is_file()), None)
    if shipped:
        rel = os.path.relpath(shipped, root)
        if not add_to_hook(shipped):
            return ""
        return f"在仓库自带的 {rel} 里加了一行 agentkit 检查；按仓库自己的方式启用 git 钩子后生效"
    hooks = Path(git(root, "rev-parse", "--git-path", "hooks").strip())
    hook = (hooks if hooks.is_absolute() else root / hooks) / "pre-commit"
    if hook.exists():
        if GIT_HOOK_LINE in hook.read_text(errors="ignore"):
            return ""
        return f"已有 {os.path.relpath(hook, root)}：请在里面加一行 `{GIT_HOOK_GUARDED}`"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n# {OWNED}：需求没确认前不提交代码\nexec {GIT_HOOK_LINE}\n")
    hook.chmod(0o755)
    return "已安装 git pre-commit 钩子（只对这个克隆生效；其他人运行 `" + CLI + " hook install-git`）"


# ---------------------------------------------------------------- init

def detect(root: Path) -> dict:
    found = {"rules_always": [], "rules_review": [], "always": [], "candidates": [], "high_risk": [], "protected_hint": []}
    if (root / "AGENTS.md").exists():
        found["rules_always"].append("AGENTS.md")
    found["rules_review"] = [n for n in ("CONTRIBUTING.md", "REVIEW.md") if (root / n).exists()]
    makefile = root / "Makefile"
    if makefile.exists():
        targets = [m.group(1) for m in re.finditer(r"(?m)^([A-Za-z0-9][\w.-]*)\s*:(?!=)", makefile.read_text(errors="ignore"))]
        if "check" in targets:
            found["always"].append("make check")
        elif "test" in targets:
            found["always"].append("make test")
        found["candidates"] = [f"make {t}" for t in targets
                               if re.search(r"test|lint|check|typecheck", t) and f"make {t}" not in found["always"]]
    package = root / "package.json"
    if package.exists() and not found["always"]:
        try:
            scripts = json.loads(package.read_text()).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        if "test" in scripts and "no test specified" not in scripts["test"]:
            found["always"].append("npm test")
        found["candidates"] += [f"npm run {s}" for s in ("lint", "typecheck") if s in scripts]
    if not found["always"]:
        pyproject = root / "pyproject.toml"
        if (root / "pytest.ini").exists() or (pyproject.exists() and "[tool.pytest" in pyproject.read_text(errors="ignore")):
            found["always"].append("python3 -m pytest -q")
        elif (root / "tests").is_dir():
            found["always"].append("python3 -m unittest -v")
    found["high_risk"] = [d for d in ("migrations/", "db/migrations/", "deployment/", "deploy/", "infra/", "contracts/")
                          if (root / d).is_dir()]
    if (root / "vendor").is_dir():
        found["protected_hint"].append("vendor/**")
    return found


def config_text(found: dict) -> str:
    def arr(items: list[str]) -> str:
        return json.dumps(items, ensure_ascii=False)
    lines = [
        "# agentkit.toml：只写这个仓库的事实。流程和规则在 kit 里统一维护，升级 kit 不会改动这个文件。",
        "version = 1",
        "",
        "[rules]",
        "# 写代码时每次都读（会引入 CLAUDE.md）",
        f"always = {arr(found['rules_always'])}",
        "# 审查时额外读",
        f"review = {arr(found['rules_review'])}",
        "",
        "[checks]",
        "# 每次都跑；也是 Claude 结束一轮前自动跑的检查",
        f"always = {arr(found['always'])}",
        "timeout_seconds = 900",
        "",
        "# 按改动路径加跑的检查。",
    ]
    if found["candidates"]:
        lines.append("# 发现的候选：" + "、".join(found["candidates"]))
    lines += ["# [[checks.by_path]]", '# paths = ["src/**"]', '# run = ["make test"]', "",
              "[paths]",
              "# 受保护：AI 改不了（钩子拦截），PR 改到就按高风险处理"]
    if found["protected_hint"]:
        lines.append("# 发现的候选：" + "、".join(found["protected_hint"]))
    lines += ["protected = []",
              "# 高风险：改到就至少是高风险。kit 默认已包含 " + "、".join(DEFAULT_HIGH_RISK),
              f"high_risk = {arr(found['high_risk'])}",
              "",
              "[risk]",
              "# 各风险等级建议几个人审",
              "reviewers = { low = 0, medium = 1, high = 2 }",
              "",
              "[hooks]",
              "# Claude Code 结束一轮前自动跑 checks.always，没通过就让它接着修",
              "check_on_stop = true",
              "# 当前任务的需求还没人确认时，拦住 Claude Code 改代码（git 提交前的钩子对所有 agent 都拦）",
              "block_edits_before_confirm = true",
              "",
              "[tasks]",
              "# 任务卡放在哪：每个任务一个文件，记着需求、人的确认和拍板、拆分和交接；所有 agent 和人共用",
              'dir = "docs/tasks"',
              "# PR 改了代码却没有任务卡，或需求没人确认：true 判为不通过；false 只提醒",
              "required = true",
              "# 本仓库对任务卡的补充说明（比如和已有任务记录的分工），会写进 AGENTS.md 和 task show",
              '# note = ""',
              "",
              "[agents]",
              "# 所有 agent 都从 AGENTS.md 读流程。这里列出还要写入口的文件（某些 agent 只读自己的文件）：",
              "# CLAUDE.md 用 @ 引入 AGENTS.md；其他文件写一段指向 AGENTS.md 的说明。比如再加 \"GEMINI.md\"",
              'instruction_files = ["CLAUDE.md"]',
              "",
              "[review]",
              "# 本地 `agentkit review` 和 CI 各用哪些审查者。每位都是新开的独立会话；同一个写两次就开两个",
              'local = ["claude"]',
              'ci = ["claude-action"]',
              "# 同一处 Important 要几位审查者都提出才阻断合入（1 = 任何一位提出并确认就阻断）",
              "agree = 1",
              "",
              "# 内置审查者：claude（本地 claude -p）、codex（本地 codex exec）、claude-action（GitHub 上的 Claude）。",
              "# 兼容 OpenAI 接口的模型都能用 type = \"api\"：DeepSeek、Kimi、Grok、OpenRouter、本地 vLLM / Ollama 等。",
              "# [review.engines.deepseek]",
              '# type = "api"',
              '# format = "openai"                 # openai 或 anthropic',
              '# base_url = "https://api.deepseek.com"',
              '# model = "填模型名"',
              '# key_env = "DEEPSEEK_API_KEY"      # 从这个环境变量读 key；CI 里用同名的仓库密钥',
              "#",
              "# [review.engines.my-cli]",
              '# type = "command"                  # 任何能从标准输入读提示词、把结果输出到标准输出的命令',
              '# run = "填命令"                     # 可以用 {output}（结果文件）和 {prompt_file}（提示词文件）',
              "#",
              "# [review.engines.coderabbit]",
              '# type = "github-comments"          # 第三方审查机器人：意见附在评论里，仅供参考，不进门禁',
              '# author = "coderabbitai[bot]"',
              ""]
    return "\n".join(lines)


AGENTS_HEADER = ("# 给 AI agent 的说明\n\n所有 agent（Claude Code、Codex、Kimi、Grok……）都会读这个文件。\n\n"
                 "## 被纠正过的规则\n\n每被人纠正一次，就在这里加一条。\n\n- （暂无）\n\n")
CLAUDE_HEADER = "# 给 Claude Code 的说明\n\n规则写在 AGENTS.md（所有 agent 共用），这里只负责引入。\n\n"


def agents_block(cfg: dict) -> str:
    others = [r for r in cfg["rules_always"] if r != "AGENTS.md"]
    lines = [
        f"{BLOCK_START} {OWNED}：这一段升级时会被替换，文件其余内容归项目 -->",
        "## 和人协作的流程（agentkit）",
        "",
        "不管你是哪个 agent，都按下面做。完整约定见 `.agentkit/team-workflow.md`，开工前读一遍。",
        "",
        f"1. **开工先读任务卡**：运行 `{CLI} task show`。任务卡记着需求、人拍板过的事、做到哪一步、试过什么不行。",
        f"2. **需求没确认，先复述**：没有任务卡就运行 `{CLI} task new <任务号> <标题>`；按 `{CLI} prompt restate` "
        "给出的步骤把复述写进任务卡，然后停下，请人确认。**「确认」只能由人运行 `"
        f"{CLI} task confirm` 填写，不要自己填，也不要替人运行。**需求确认前，除了任务卡不改别的文件。",
        "3. **拿不准就问**：会影响做法的选择写进任务卡「要人拍板的事」，附上你的建议；人在对话里定了，"
        "就把决定、谁定的、日期记进去。",
        f"4. **一次一步**：一次只做「拆分」里的一块，做完运行 `{CLI} checks` 拿证据，更新这一块的状态。",
        f"5. **收工前交接**：更新任务卡的「交接」（做到、下一步、试过不行的），再运行 `{CLI} task handoff --agent <你是哪个 agent>`。",
        f"6. **仓库的事实**：检查命令、受保护路径、高风险路径，运行 `{CLI} context` 查看。",
    ]
    extra = []
    if others:
        extra.append("**项目规则**：还要遵守 " + "、".join(f"`{r}`" for r in others) + "。")
    if cfg["tasks_note"]:
        extra.append(f"**本仓库**：{cfg['tasks_note']}")
    lines += [f"{n}. {text}" for n, text in enumerate(extra, start=7)]
    lines += ["", f"没有仓库权限的聊天机器人：由人运行 `{CLI} prompt <步骤>`，把输出整段贴给它，再把回答整理进任务卡。",
              BLOCK_END]
    return "\n".join(lines)


def pointer_block() -> str:
    return "\n".join([f"{BLOCK_START} {OWNED} -->",
                      "本仓库的协作流程写在 `AGENTS.md`。开工前先读它，并按其中「和人协作的流程（agentkit）」一节做。",
                      BLOCK_END])


def upsert_block(path: Path, block: str, header: str) -> str:
    """Write or replace the kit's marked block in a project-owned file; the rest of the file is left alone."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header + block + "\n")
        return "created"
    text = path.read_text()
    start, end = text.find(BLOCK_START), text.find(BLOCK_END)
    if start != -1 and end > start:
        new = text[:start] + block + text[end + len(BLOCK_END):]
    else:
        new = text.rstrip("\n") + "\n\n" + block + "\n"
    if new == text:
        return ""
    path.write_text(new)
    return "updated"


def write_owned(src: Path, dest: Path, report: dict) -> None:
    if dest.exists() and OWNED not in dest.read_text(errors="ignore"):
        report["skipped"].append((dest, "（不是 agentkit 生成的文件，没有覆盖）"))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    existed = dest.exists()
    shutil.copyfile(src, dest)
    report["updated" if existed else "created"].append((dest, ""))


def merge_settings(path: Path) -> bool:
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})

    def ensure(event: str, matcher: str | None, command: str) -> bool:
        entries = hooks.setdefault(event, [])
        if any(h.get("command") == command for e in entries for h in e.get("hooks", [])):
            return False
        entry: dict = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        entries.append(entry)
        return True

    changed = ensure("PreToolUse", "Edit|MultiEdit|Write", HOOK_PRE) | ensure("Stop", None, HOOK_STOP)
    changed |= ensure("SessionStart", None, HOOK_START)
    deny = data.setdefault("permissions", {}).setdefault("deny", [])
    for rule in ("Read(./.env)", "Read(./.env.*)", f"Bash({CLI} task confirm:*)"):
        if rule not in deny:
            deny.append(rule)
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return changed


def ensure_lines(path: Path, needed: list[str], header: str) -> list[str]:
    if not path.exists():
        path.write_text(header + "\n".join(needed) + "\n")
        return needed
    existing = path.read_text().splitlines()
    missing = [line for line in needed if line not in existing]
    if missing:
        with open(path, "a") as f:
            f.write(("\n" if existing and existing[-1].strip() else "") + "\n".join(missing) + "\n")
    return missing


def cmd_init(args: argparse.Namespace) -> int:
    templates = KIT_DIR / "templates"
    if not templates.is_dir():
        sys.exit("请从 kit 仓库运行 init（找不到 templates/）")
    root = Path(args.repo).resolve()
    if not (root / ".git").exists():
        sys.exit(f"{root} 不是 git 仓库的根目录")
    report: dict = {"created": [], "updated": [], "skipped": []}

    config = root / "agentkit.toml"
    if config.exists():
        report["skipped"].append((config, "（项目自己的配置，不覆盖）"))
    else:
        config.write_text(config_text(detect(root)))
        report["created"].append((config, ""))

    kit = root / ".agentkit"
    kit.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).resolve(), kit / "agentkit.py")
    (kit / "VERSION").write_text(VERSION + "\n")
    for name in ("review-format.md", "team-workflow.md", "task-card.md"):
        write_owned(templates / name, kit / name, report)
    for src in sorted((templates / "prompts").glob("*.md")):
        write_owned(src, kit / "prompts" / src.name, report)
        if split_frontmatter(src.read_text())[0].get("claude-command") != "false":
            write_owned(src, root / ".claude" / "commands" / src.name, report)
    write_owned(templates / "workflow.yml", root / ".github" / "workflows" / "agentkit.yml", report)

    if merge_settings(root / ".claude" / "settings.json"):
        report["updated"].append((root / ".claude" / "settings.json", "（加入钩子和权限限制）"))
    cfg = load_config(root)
    agents = root / "AGENTS.md"
    state = upsert_block(agents, agents_block(cfg), AGENTS_HEADER)
    if state:
        report["created" if state == "created" else "updated"].append((agents, "（写入「和人协作的流程」一节，其余内容不动）"))
    for name in cfg["instruction_files"]:
        if name == "CLAUDE.md":
            needed = ["@AGENTS.md"] + [f"@{r}" for r in cfg["rules_always"] if r != "AGENTS.md"] + ["@.agentkit/team-workflow.md"]
            if ensure_lines(root / name, needed, CLAUDE_HEADER):
                report["updated"].append((root / name, "（引入 AGENTS.md）"))
        elif upsert_block(root / name, pointer_block(), ""):
            report["updated"].append((root / name, "（指向 AGENTS.md）"))
    ensure_lines(root / ".gitignore", [".agentkit/*.local.md"], "")
    git_hook = install_git_hook(root)

    print(f"agentkit {VERSION} 已安装到 {root}")
    for key, title in (("created", "新建"), ("updated", "更新"), ("skipped", "跳过")):
        for path, note in report[key]:
            print(f"  {title}：{os.path.relpath(path, root)}{note}")
    if git_hook:
        print(f"  git 钩子：{git_hook}")
    if "[tasks]" not in config.read_text():
        print("\n注意：agentkit.toml 里还没有 [tasks]，任务卡检查目前只提醒、不拦 PR。要强制，加上：\n"
              '  [tasks]\n  dir = "docs/tasks"\n  required = true')
    print("\n下一步：检查 agentkit.toml；新任务运行 `" + CLI + " task new <任务号> <标题>`；"
          "在 GitHub 添加审查要用的密钥，并把 agentkit / task、agentkit / checks、agentkit / review 设为必须通过的检查。")
    return 0


# ---------------------------------------------------------------- other commands

def cmd_context(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    def listing(items: list[str]) -> str:
        return "、".join(f"`{i}`" for i in items) if items else "（无）"
    out = ["# 本仓库的 agentkit 配置（来自 agentkit.toml）", ""]
    out.append(f"- 写代码时遵守：{listing(cfg['rules_always'])}；流程见 AGENTS.md 的 agentkit 一节和 `.agentkit/team-workflow.md`")
    if args.purpose == "review":
        out.append(f"- 审查时按顺序读：{listing(cfg['rules_always'] + cfg['rules_review'] + ['.agentkit/review-format.md'])}")
        out.append("- 项目自己的审查规则和 review-format.md 冲突时，以项目规则为准；输出格式始终按 review-format.md")
    out.append(f"- 需求在哪：任务卡 `{tasks_prefix(cfg)}<任务号>.md`（当前任务运行 `{CLI} task show`）；没有任务卡时看 PR 描述")
    out.append(f"- 每次都跑的检查：{listing(cfg['checks_always'])}")
    for entry in cfg["checks_by_path"]:
        out.append(f"- 改到 {listing(entry['paths'])} 时加跑：{listing(entry['run'])}")
    out.append(f"- 受保护路径（不能改）：{listing(cfg['protected'])}")
    out.append(f"- 高风险路径（改到就至少高风险）：{listing(cfg['high_risk'])}")
    r = cfg["reviewers"]
    out.append(f"- 建议人审人数：低 {r['low']}、中 {r['medium']}、高 {r['high']}")
    out.append(f"- 运行检查并拿到证据：`{CLI} checks`")
    out.append(f"- AI 审查者：本地 {listing(cfg['review_local'])}（`{CLI} review`）；CI {listing(cfg['review_ci'])}")
    print("\n".join(out))
    return 0


def cmd_checks(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    base = args.base or default_base(root)
    files = changed_files(root, base, worktree=not args.committed_only)
    plan = plan_checks(cfg, files)
    patterns = [p for e in cfg["checks_by_path"] for p in e["paths"]]
    uncovered = [f for f in files if not any(matches(f, p) for p in patterns)] if cfg["checks_by_path"] else []
    results = run_checks(root, plan, cfg["timeout"])
    head = git(root, "rev-parse", "--short", "HEAD").strip() + ("（含未提交的改动）" if git(root, "status", "--porcelain").strip() else "")
    base_label = git(root, "rev-parse", "--short", base).strip() if base != "HEAD" else "HEAD"
    text = evidence_md(base_label, head, files, results, uncovered)
    print(text)
    if args.evidence:
        Path(args.evidence).write_text(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text)
    return 1 if any(r["code"] != 0 for r in results) else 0


def cmd_risk(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    files = changed_files(root, args.base or default_base(root))
    print(json.dumps(risk_floor(cfg, files), ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    result, errors = parse_result(Path(args.file).read_text())
    if errors:
        print("审查结果格式不对：\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    print(f"格式正确：{len(result['issues'])} 个问题，风险初判 {RISK_ZH[result['risk_level']]}。")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    text = Path(args.review).read_text() if Path(args.review).exists() else ""
    review, errors = parse_result(text)
    base = args.base or default_base(root)
    if errors and not text and args.pr and workflow_changed(root, base, args.head):
        return skipped_review(args.pr, args.dry_run)
    if errors:
        print("AI 审查没有产出合格的结果，不能放行：\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    files = changed_files(root, base, worktree=args.dry_run)
    floor = risk_floor(cfg, files)
    if args.comments_file:
        comments = json.loads(Path(args.comments_file).read_text())
    elif args.dry_run or not args.pr:
        comments = []
    else:
        comments = prior_comments(args.pr)
    decision = decide(cfg, review, floor, count_prior_rounds(cfg, comments))
    head = args.head or git(root, "rev-parse", "HEAD").strip()
    comment = finalize_comment(text, review, decision, head)
    if args.dry_run or not args.pr:
        print(comment)
        print(json.dumps({k: decision[k] for k in ("round", "risk_level", "ai_risk_level", "reviewers_needed", "labels", "passed")},
                         ensure_ascii=False))
    else:
        out = Path(args.review).with_suffix(".final.md")
        out.write_text(comment)
        gh("pr", "comment", args.pr, "--body-file", str(out))
        apply_labels(args.pr, decision["labels"])
        print(f"已发出审查评论并加标签：{'、'.join(decision['labels'])}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(comment.split("<!--")[0])
    if not decision["passed"]:
        print(f"不通过：{len(decision['blockers'])} 个已确认的 Important 问题。", file=sys.stderr)
        return 1
    return 0


WORKFLOW_FILE = ".github/workflows/agentkit.yml"


def workflow_changed(root: Path, base: str, head: str | None) -> bool:
    """claude-code-action skips the review when a PR changes its own workflow file."""
    diff = subprocess.run(["git", "-C", str(root), "diff", "--quiet", base, head or "HEAD", "--", WORKFLOW_FILE])
    return diff.returncode == 1


def skipped_review(pr: str, dry_run: bool) -> int:
    body = ("**agentkit**：这个 PR 改了审查工作流本身（`" + WORKFLOW_FILE + "`）。为了防止 PR 改写自己的审查，"
            "Claude 的审查 action 只在工作流和默认分支一致时运行，所以这次 AI 审查被跳过。\n\n"
            "按规则，改门禁属于高风险：请至少 1 位负责人审过再合入。合入之后，后面的 PR 会恢复自动审查。")
    labels = ["risk:high", "needs-human"]
    if dry_run:
        print(body)
        print(json.dumps({"labels": labels, "passed": True, "skipped": True}, ensure_ascii=False))
        return 0
    out = Path("review.skipped.md")
    out.write_text(body + "\n")
    gh("pr", "comment", pr, "--body-file", str(out))
    apply_labels(pr, labels)
    print("AI 审查因工作流改动被跳过：已加 risk:high 和 needs-human，交给人审。")
    return 0


ERROR_HINTS = {
    "billing_error": ("这个 key 所属的组织额度不足。如果 Console 里明明有额度，多半是 key 建在了另一个组织下："
                      "在 Console 左上角切到有额度的组织，在那里新建 key，更新仓库密钥 ANTHROPIC_API_KEY。"
                      "也可以改用 Claude 订阅的 OAuth token（claude_code_oauth_token）。"),
    "authentication_error": "API key 无效：检查仓库密钥 ANTHROPIC_API_KEY 是否填对。",
    "permission_error": "这个 API key 没有调用该模型的权限：检查账户的模型权限。",
    "rate_limit_error": "触发了限流：稍后在 Actions 页面重跑这个任务。",
    "overloaded_error": "服务暂时过载：稍后在 Actions 页面重跑这个任务。",
}


# Some API errors arrive with an unhelpful kind; match on the message text too.
MESSAGE_HINTS = {
    "not scoped to a workspace": ("这个 key 是组织级的，没有归属到工作区。在 Console 里进入一个工作区（Workspaces → 选中工作区 → API keys）"
                                  "新建 key，用它更新仓库密钥 ANTHROPIC_API_KEY。"),
    "credit balance is too low": "billing_error",
    "invalid x-api-key": "authentication_error",
}


def hint_for(kind: str, message: str) -> str | None:
    for needle, hint in MESSAGE_HINTS.items():
        if needle in message.lower():
            return ERROR_HINTS.get(hint, hint)
    return ERROR_HINTS.get(kind)


def api_errors(text: str) -> list[tuple[str, str]]:
    """Find API errors in claude-code-action's execution output (JSON array or JSON lines)."""
    try:
        records = [json.loads(text)]
    except json.JSONDecodeError:
        records = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    found: list[tuple[str, str]] = []

    def visit(obj) -> None:
        if isinstance(obj, list):
            for item in obj:
                visit(item)
        elif isinstance(obj, dict):
            if obj.get("is_api_error_message") or (isinstance(obj.get("error"), str) and obj["error"].endswith("_error")):
                content = (obj.get("message") or {}).get("content") or obj.get("content") or []
                texts = [c.get("text", "") for c in content if isinstance(c, dict)] if isinstance(content, list) else []
                found.append((str(obj.get("error") or "api_error"), " ".join(t for t in texts if t).strip()))
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    visit(value)

    visit(records)
    return list(dict.fromkeys(found))


def cmd_explain_failure(args: argparse.Namespace) -> int:
    path = Path(args.file)
    errors = api_errors(path.read_text()) if path.exists() else []
    lines = ["**agentkit**：AI 审查没有运行完，这次没有审查结果，`agentkit / review` 判为不通过。", ""]
    if errors:
        for kind, message in errors[:3]:
            lines.append(f"- 接口返回 `{kind}`" + (f"：{message}" if message else ""))
            hint = hint_for(kind, message)
            if hint:
                lines.append(f"  - 处理办法：{hint}")
    else:
        lines.append("- 没有在输出里找到明确的接口错误，请到 Actions 日志里看 Claude 那一步。")
    lines += ["", "处理好之后，在 Actions 页面重跑这个任务，或者推一个新提交。"]
    body = "\n".join(lines) + "\n"
    if args.dry_run or not args.pr:
        print(body)
        return 0
    out = Path("review.failure.md")
    out.write_text(body)
    gh("pr", "comment", args.pr, "--body-file", str(out))
    print("已在 PR 上说明 AI 审查失败的原因。")
    return 0


# ---------------------------------------------------------------- prompts

def split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            meta = {}
            for line in text[4:end].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            return meta, text[end + 5:]
    return {}, text


def drop_owned_marker(text: str) -> str:
    return re.sub(rf"<!--\s*{OWNED}.*?-->\s*", "", text, flags=re.S)


def cmd_prompt(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    folder = kit_file("prompts")
    if not args.name:
        print("工作流程的提示词，任何 agent 都能用。没有仓库权限的聊天机器人：由人把输出整段贴给它。\n")
        for path in sorted(folder.glob("*.md")):
            print(f"- {path.stem}：{split_frontmatter(path.read_text())[0].get('description', '')}")
        print(f"\n用法：{CLI} prompt <名字> [补充说明]")
        return 0
    path = folder / f"{args.name}.md"
    if not path.exists():
        print(f"没有叫 {args.name} 的提示词。运行 `{CLI} prompt` 查看全部。", file=sys.stderr)
        return 1
    if args.name == "reviewer":
        print(build_review_prompt(root, cfg, args.base or default_base(root)))
        return 0
    body = split_frontmatter(path.read_text())[1].replace("$ARGUMENTS", " ".join(args.text).strip() or "（没有给出）")
    out = drop_owned_marker(body).strip()
    task_id, _ = current_task(root, cfg)
    if task_id and not args.no_card:
        card = card_path(root, cfg, task_id)
        out += f"\n\n---\n当前任务卡（`{os.path.relpath(card, root)}`）：\n\n" + card.read_text().rstrip()
    print(out)
    return 0


# ---------------------------------------------------------------- review engines
#
# Every reviewer gets the same standard prompt (rules, format, task card, checks, diff) in a fresh
# session and must answer in the standard result format. Which reviewers run is configuration:
# a local CLI ("command"), any OpenAI- or Anthropic-compatible API ("api"), the GitHub action
# ("github-action", run by the workflow), or a third-party bot's PR comments ("github-comments", advisory).

def read_limited(path: Path, limit: int) -> str:
    text = path.read_text(errors="ignore")
    return text if len(text) <= limit else text[:limit] + f"\n\n（后面还有 {len(text) - limit} 个字符，已截断）"


def review_diff(root: Path, base: str, committed_only: bool) -> str:
    if base == "HEAD":
        return git(root, "diff", "HEAD")
    merge_base = git(root, "merge-base", base, "HEAD").strip()
    diff = git(root, "diff", merge_base, "HEAD") if committed_only else git(root, "diff", merge_base)
    if not committed_only:
        for name in git(root, "ls-files", "--others", "--exclude-standard").split()[:10]:
            path = root / name
            if path.is_file() and path.stat().st_size < 20_000:
                diff += f"\n新文件（还没加入 git）：{name}\n```\n{path.read_text(errors='ignore')}\n```\n"
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n\n（diff 太长，后面 {len(diff) - MAX_DIFF_CHARS} 个字符已截断；能运行命令的话自己看完整改动）"
    return diff


def build_review_prompt(root: Path, cfg: dict, base: str, evidence: str | None = None, pr_body: str = "",
                        output_file: str | None = None, committed_only: bool = False) -> str:
    if output_file:
        finish = (f"最后把完整结果写进文件 `{output_file}`，再运行 `{CLI} validate {output_file}`，"
                  "不通过就改到通过。不要修改其他文件。")
    else:
        finish = "不要修改任何文件。把完整结果作为你的最终回复直接输出，前后不要加别的话。"
    if committed_only or base == "HEAD":
        scope, diff_cmd = f"`{base}...HEAD`（已提交的改动）", f"git diff {base}...HEAD"
    else:
        merge_base = git(root, "merge-base", base, "HEAD").strip()
        scope = f"`{base}` 之后的全部改动，**包括还没提交的**（工作区里的修改和新文件）"
        diff_cmd = f"git diff {merge_base[:12]}`（看工作区）和 `git status"
    template = split_frontmatter(kit_file("prompts", "reviewer.md").read_text())[1]
    filled = template.replace("$SCOPE", scope).replace("$DIFF_CMD", diff_cmd).replace("$OUTPUT", finish)
    parts = [drop_owned_marker(filled).strip()]
    for rule in dict.fromkeys(cfg["rules_always"] + cfg["rules_review"]):
        if (root / rule).is_file():
            parts.append(f"## 项目规则：{rule}\n\n" + read_limited(root / rule, MAX_RULE_CHARS))
    parts.append("## 审查格式\n\n" + strip_comments(kit_file("review-format.md").read_text()).strip())
    task_id, _ = current_task(root, cfg, base=base)
    if task_id:
        parts.append(f"## 任务卡（{cfg['tasks_dir']}/{task_id}.md）\n\n" + card_path(root, cfg, task_id).read_text().strip())
    else:
        parts.append("## 任务卡\n\n没有找到任务卡。需求以 PR 描述为准；需求或完成标准缺失，本身就是一个 Important 问题。")
    if pr_body.strip():
        parts.append("## PR 描述\n\n" + pr_body.strip())
    if evidence and Path(evidence).exists():
        parts.append("## 自动检查结果\n\n" + Path(evidence).read_text().strip())
    files = changed_files(root, base, worktree=not committed_only)
    parts.append(f"## 改动的文件（{len(files)} 个）\n\n" + "\n".join(f"- {f}" for f in files))
    parts.append("## 完整改动（diff）\n\n```diff\n" + review_diff(root, base, committed_only) + "\n```")
    return "\n\n---\n\n".join(parts) + "\n"


def run_command_engine(root: Path, engine: dict, prompt: str, timeout: int) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        output, prompt_file = Path(tmp) / "review.md", Path(tmp) / "prompt.md"
        prompt_file.write_text(prompt)
        cmd = engine["run"].replace("{output}", shlex.quote(str(output))).replace("{prompt_file}", shlex.quote(str(prompt_file)))
        env = {**os.environ, "AGENTKIT_PROMPT_FILE": str(prompt_file), "AGENTKIT_OUTPUT_FILE": str(output)}
        proc = subprocess.run(cmd, shell=True, cwd=root, input=prompt, capture_output=True, text=True,
                              timeout=timeout, env=env)
        text = output.read_text() if output.exists() and output.read_text().strip() else proc.stdout
    if proc.returncode != 0 and not RESULT_RE.search(text):
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-5:])
        return "", f"命令退出码 {proc.returncode}：{tail}"
    return text, ""


def api_call(engine: dict, messages: list[dict], timeout: int) -> str:
    key_env = engine.get("key_env", "")
    key = os.environ.get(key_env, "")
    if not key:
        raise RuntimeError(f"没有设置环境变量 {key_env or '（agentkit.toml 里没写 key_env）'}")
    if not engine.get("model"):
        raise RuntimeError("agentkit.toml 里没写 model")
    if engine.get("format", "openai") == "anthropic":
        url = engine.get("base_url", "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        body = {"model": engine["model"], "max_tokens": int(engine.get("max_tokens", 8000)), "messages": messages}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        if not engine.get("base_url"):
            raise RuntimeError("agentkit.toml 里没写 base_url")
        url = engine["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": engine["model"], "messages": messages}
        if engine.get("max_tokens"):
            body["max_tokens"] = int(engine["max_tokens"])
        headers = {"Authorization": f"Bearer {key}"}
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={**headers, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} 返回 HTTP {e.code}：{e.read().decode(errors='ignore')[:300]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"连不上 {url}：{e}") from None
    if engine.get("format", "openai") == "anthropic":
        return "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def run_api_engine(engine: dict, prompt: str, timeout: int) -> tuple[str, str]:
    messages = [{"role": "user", "content": prompt}]
    text = api_call(engine, messages, timeout)
    _, errors = parse_result(text)
    if errors:  # one chance to fix the format, in the same session
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "结果格式不对：" + "；".join(errors) + "。请按审查格式重新输出完整结果。"}]
        text = api_call(engine, messages, timeout)
    return text, ""


def third_party_comment(pr: str, author: str) -> str:
    bodies = [c["body"] for c in prior_comments(pr) if c.get("author") == author and c.get("body")]
    repo = os.environ.get("GITHUB_REPOSITORY", "{owner}/{repo}")
    out = gh("api", "--paginate", f"repos/{repo}/pulls/{pr}/reviews", "--jq", ".[] | {author: .user.login, body: .body}")
    for line in out.splitlines():
        if line.strip():
            item = json.loads(line)
            if item.get("author") == author and item.get("body"):
                bodies.append(item["body"])
    return bodies[-1] if bodies else ""


def same_spot(a: dict, b: dict) -> bool:
    if a["file"] != b["file"]:
        return False
    if a.get("line") is None or b.get("line") is None:
        return a.get("line") is None and b.get("line") is None
    return abs(a["line"] - b["line"]) <= 3


SEVERITY_RANK = {"important": 0, "pre-existing": 1, "nit": 2}
SEVERITY_LABEL = {"important": "Important", "pre-existing": "Pre-existing", "nit": "Nit"}
SCORE_LABEL = {"requirement": "符合需求", "evidence": "测试与证据", "risk": "边界与风险", "readability": "好读好改"}


def merge_reviews(valid: list[tuple[str, str, dict]], advisory: list[tuple[str, str]],
                  failed: list[tuple[str, str]], agree: int) -> tuple[str, dict]:
    """Merge independent reviews into one standard result. An Important blocks only if `agree` reviewers raised it."""
    names = [name for name, _, _ in valid]
    issues = [{**issue, "reviewer": name} for name, _, result in valid for issue in result["issues"]]
    groups: list[list[dict]] = []
    for issue in issues:
        group = next((g for g in groups if any(same_spot(issue, other) for other in g)), None)
        if group is None:
            groups.append([issue])
        else:
            group.append(issue)
    needed = min(agree, len(valid))
    for group in groups:
        raised = {i["reviewer"] for i in group if i["severity"] == "important"}
        for issue in group:
            if issue["severity"] == "important" and issue["confirmed"] and len(raised) < needed:
                issue["confirmed"] = False
                issue["note"] = f"只有 {len(raised)} 位审查者提出，没达到 agree = {agree}"
    scores = {}
    for key in SCORE_KEYS:
        values = [r["scores"].get(key) for _, _, r in valid if isinstance(r["scores"].get(key), int)]
        scores[key] = min(values) if values else None
    risk = max((r["risk_level"] for _, _, r in valid), key=RISK_ORDER.index)
    merged = {"scores": scores, "risk_level": risk, "issues": [i for g in groups for i in g], "reviewers": names}
    if failed:
        merged["failed_reviewers"] = [name for name, _ in failed]
    if len(valid) == 1 and not advisory and not failed:
        return RESULT_RE.sub("", valid[0][1]).rstrip() + "\n", merged

    lines = [f"## AI 审查（{len(valid)} 位独立审查者：{'、'.join(names)}）", ""]
    if groups:
        # one row per spot; every reviewer's own words and severity, so nobody is credited with another's claim
        lines += ["| 位置 | 级别 | 谁说了什么 |", "|---|---|---|"]
        for group in sorted(groups, key=lambda g: min(SEVERITY_RANK[i["severity"]] for i in g)):
            top = min(group, key=lambda i: SEVERITY_RANK[i["severity"]])
            where = f"`{top['file']}" + (f":{top['line']}`" if top.get("line") is not None else "`")
            level = SEVERITY_LABEL[top["severity"]]
            if top["severity"] == "important":
                level += " · " + ("已确认" if any(i["severity"] == "important" and i["confirmed"] for i in group) else "待确认")
            said = []
            for i in sorted(group, key=lambda i: SEVERITY_RANK[i["severity"]]):
                summary = i["summary"].replace("|", "\\|").replace("\n", " ")[:80]
                said.append(f"**{i['reviewer']}**（{SEVERITY_LABEL[i['severity']]}）：{summary}")
            lines.append(f"| {where} | {level} | {'<br>'.join(said)} |")
    else:
        lines.append("没有审查者发现需要改的问题。")
    lines += ["", f"**风险初判：{RISK_ZH[risk]}**（取各审查者里最高的）", "",
              "分数（取各审查者里最低的）：" + " · ".join(
                  f"{SCORE_LABEL[k]} {scores[k]}/5" if scores[k] is not None else f"{SCORE_LABEL[k]} 无法判断" for k in SCORE_KEYS)]
    if agree > 1:
        lines += ["", f"规则：同一处 Important 要有 {needed} 位审查者都提出，并且已确认，才阻断合入。"]
    if failed:
        lines += ["", "没跑成的审查者：" + "；".join(f"{name}（{error[:120]}）" for name, error in failed)]
    budget = 50_000 // max(1, len(valid) + len(advisory))
    for name, text, _ in valid:
        body = RESULT_RE.sub("", text).strip()
        lines += ["", f"<details><summary>{name} 的完整审查</summary>", "", body[:budget], "", "</details>"]
    for name, text in advisory:
        lines += ["", f"<details><summary>{name} 的意见（第三方，仅供参考，不进门禁）</summary>", "",
                  text.strip()[:budget], "", "</details>"]
    return "\n".join(lines) + "\n", merged


def review_instances(names: list[str]) -> list[tuple[str, str]]:
    """Listing the same reviewer twice runs two independent sessions: claude#1, claude#2."""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    seen: dict[str, int] = {}
    out = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        out.append((f"{name}#{seen[name]}" if counts[name] > 1 else name, name))
    return out


def cmd_review(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    base = args.base or default_base(root)
    names = [n.strip() for n in args.engines.split(",") if n.strip()] if args.engines else \
        (cfg["review_ci"] if args.ci else cfg["review_local"])
    if args.plan:
        uses_action = any(cfg["engines"].get(n, {}).get("type") == "github-action" for n in names)
        print(f"action={'true' if uses_action else 'false'}")
        print("engines=" + ",".join(names))
        return 0
    pr_body = Path(args.pr_body_file).read_text() if args.pr_body_file and Path(args.pr_body_file).exists() else ""
    if args.print_prompt:
        print(build_review_prompt(root, cfg, base, args.evidence, pr_body, args.output_file, committed_only=args.ci), end="")
        return 0
    if args.ci:
        for cmd in cfg["review_ci_setup"]:
            subprocess.run(cmd, shell=True, cwd=root, check=False)
    prompt = build_review_prompt(root, cfg, base, args.evidence, pr_body, committed_only=args.ci)
    included = dict(spec.split("=", 1) for spec in (args.include or []) if "=" in spec)

    def work(label: str, name: str) -> tuple[str, str, str, str]:
        engine = cfg["engines"].get(name)
        if engine is None:
            return label, "", "", f"agentkit.toml 里没有定义这个审查者（[review.engines.{name}]）"
        kind = engine.get("type", "")
        try:
            if kind == "github-action":
                path = Path(included.get(label) or included.get(name) or "")
                if path.is_file() and path.read_text().strip():
                    return label, kind, path.read_text(), ""
                return label, kind, "", "工作流里的这一步没有产出结果，看 Actions 日志"
            if kind == "github-comments":
                if not args.pr:
                    return label, kind, "", "需要 --pr"
                text = third_party_comment(args.pr, engine.get("author", ""))
                return label, kind, text, "" if text else f"{engine.get('author')} 还没有在这个 PR 上留下意见"
            if kind == "command":
                return (label, kind, *run_command_engine(root, engine, prompt, args.timeout))
            if kind == "api":
                return (label, kind, *run_api_engine(engine, prompt, args.timeout))
            return label, kind, "", f"不认识的类型 {kind!r}：只支持 command、api、github-action、github-comments"
        except subprocess.TimeoutExpired:
            return label, kind, "", f"超时（{args.timeout} 秒）"
        except (RuntimeError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as e:
            return label, kind, "", str(e)

    instances = review_instances(names)
    print(f"并行运行 {len(instances)} 位审查者，每位都是新开的独立会话：{'、'.join(l for l, _ in instances)}")
    with ThreadPoolExecutor(max_workers=max(1, len(instances))) as pool:
        outcomes = list(pool.map(lambda pair: work(*pair), instances))
    valid, advisory, failed = [], [], []
    raw_dir = Path(args.out or ".agentkit/review.local.md").parent
    for label, kind, text, error in outcomes:
        if text:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"review.{label.replace('#', '-')}.local.md").write_text(text)
        if error:
            failed.append((label, error))
            print(f"- {label}：没跑成：{error}")
        elif kind == "github-comments":
            advisory.append((label, text))
            print(f"- {label}：拿到第三方意见（仅供参考）")
        else:
            result, errors = parse_result(text)
            if errors:
                failed.append((label, "格式不对：" + "；".join(errors[:3])))
                raw = raw_dir / f"review.{label.replace('#', '-')}.local.md"
                print(f"- {label}：结果格式不对：{'；'.join(errors[:3])}（原始输出在 {raw}）")
            else:
                valid.append((label, text, result))
                print(f"- {label}：完成，{len(result['issues'])} 个问题，风险初判{RISK_ZH[result['risk_level']]}")
    if not valid:
        print("没有审查者产出合格的结果。", file=sys.stderr)
        return 3
    text, merged = merge_reviews(valid, advisory, failed, cfg["review_agree"])
    out = Path(args.out or ".agentkit/review.local.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n<!-- ai-review-result\n" + json.dumps(merged, ensure_ascii=False) + "\n-->\n")
    print(f"合并结果已写入 {out}")
    if not args.ci:
        floor = risk_floor(cfg, changed_files(root, base))
        decision = decide(cfg, merged, floor, 0)
        verdict = "没有已确认的 Important" if decision["passed"] else f"{len(decision['blockers'])} 个已确认的 Important 要先修"
        print(f"本地预判：风险{RISK_ZH[decision['risk_level']]}，建议 {decision['reviewers_needed']} 人审，{verdict}。")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    if args.event == "install-git":
        print(install_git_hook(root) or "git pre-commit 钩子已经装好了。")
        return 0
    if args.event == "pre-commit":
        return hook_pre_commit(root, cfg)
    if args.event == "session-start":
        return hook_session_start(root, cfg)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if args.event == "pre-edit":
        return hook_pre_edit(root, cfg, payload)
    return hook_stop(root, cfg, payload)


GUARDS = ("pre-edit", "pre-commit")


def run_hook(args: argparse.Namespace) -> int:
    """Guards fail closed: if the kit itself breaks, block and say so instead of silently letting edits through."""
    try:
        return cmd_hook(args)
    except Exception as e:  # noqa: BLE001 - any failure inside a guard must be visible
        if args.event not in GUARDS:
            print(f"agentkit 钩子出错（{args.event}）：{type(e).__name__}: {e}", file=sys.stderr)
            return 0
        print(f"agentkit 的检查自己出错了，为安全起见先拦下：{type(e).__name__}: {e}。"
              "请停下来告诉人，由人检查 agentkit.toml 和 Python 环境。", file=sys.stderr)
        return 2 if args.event == "pre-edit" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentkit", description=f"agentkit {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="安装或升级到某个仓库")
    p.add_argument("repo")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("context", help="打印本仓库的配置，给 agent 读")
    p.add_argument("--for", dest="purpose", choices=["coding", "review"], default="coding")
    p.set_defaults(func=cmd_context)
    p = sub.add_parser("task", help="任务卡：需求、人的确认、拆分、交接")
    tasks = p.add_subparsers(dest="action", required=True)
    t = tasks.add_parser("new", help="新建任务卡，并设为当前任务")
    t.add_argument("id")
    t.add_argument("title", nargs="*")
    t = tasks.add_parser("show", help="显示当前任务卡和状态（开工先运行）")
    t.add_argument("id", nargs="?")
    t.add_argument("--base")
    t.add_argument("--brief", action="store_true", help="只显示状态")
    tasks.add_parser("list", help="列出所有任务卡")
    t = tasks.add_parser("use", help="切换当前任务")
    t.add_argument("id", nargs="?")
    t.add_argument("--clear", action="store_true", help="清除当前任务")
    t = tasks.add_parser("confirm", help="人确认需求（只能由人在自己的终端运行）")
    t.add_argument("id", nargs="?")
    t = tasks.add_parser("handoff", help="记下交接（先在任务卡里写好「交接」）")
    t.add_argument("id", nargs="?")
    t.add_argument("--agent", help="你是哪个 agent，比如 Codex、Kimi")
    t = tasks.add_parser("check", help="检查这次改动的任务卡（CI 用）")
    t.add_argument("--base")
    t.add_argument("--committed-only", action="store_true")
    t.add_argument("--branch")
    t.add_argument("--pr-body-file")
    p.set_defaults(func=cmd_task)
    p = sub.add_parser("prompt", help="打印流程提示词，任何 agent 都能用")
    p.add_argument("name", nargs="?")
    p.add_argument("text", nargs="*")
    p.add_argument("--base")
    p.add_argument("--no-card", action="store_true", help="不附上当前任务卡")
    p.set_defaults(func=cmd_prompt)
    p = sub.add_parser("review", help="让配置好的审查者各开新会话审查，合并结果")
    p.add_argument("--engines", help="用哪些审查者，逗号分隔；默认用 agentkit.toml 的 review.local")
    p.add_argument("--ci", action="store_true", help="CI 用：review.ci 的审查者，只看已提交的改动")
    p.add_argument("--plan", action="store_true", help="CI 用：输出要不要跑 GitHub action")
    p.add_argument("--base")
    p.add_argument("--evidence", help="agentkit checks 的结果文件")
    p.add_argument("--pr")
    p.add_argument("--pr-body-file")
    p.add_argument("--include", action="append", help="已有的审查结果，比如 claude-action=review-action.md")
    p.add_argument("--out", help="合并结果写到哪，默认 .agentkit/review.local.md")
    p.add_argument("--print-prompt", action="store_true", help="只打印审查提示词")
    p.add_argument("--output-file", help="和 --print-prompt 一起用：让审查者把结果写进这个文件")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_review)
    p = sub.add_parser("checks", help="按改动运行检查，输出证据")
    p.add_argument("--base")
    p.add_argument("--evidence")
    p.add_argument("--committed-only", action="store_true", help="只看已提交的改动（CI 用）")
    p.set_defaults(func=cmd_checks)
    p = sub.add_parser("risk", help="按路径给出风险下限")
    p.add_argument("--base")
    p.set_defaults(func=cmd_risk)
    p = sub.add_parser("validate", help="检查审查结果的格式")
    p.add_argument("file")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("gate", help="定稿审查结果、发评论、加标签、给出通过或不通过")
    p.add_argument("--review", required=True)
    p.add_argument("--pr")
    p.add_argument("--base")
    p.add_argument("--head")
    p.add_argument("--comments-file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_gate)
    p = sub.add_parser("explain-failure", help="AI 审查没跑起来时，在 PR 上说明原因")
    p.add_argument("--file", required=True, help="claude-code-action 的执行输出")
    p.add_argument("--pr")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_explain_failure)
    p = sub.add_parser("hook", help="Claude Code 钩子和 git 钩子")
    p.add_argument("event", choices=["pre-edit", "stop", "session-start", "pre-commit", "install-git"])
    p.set_defaults(func=run_hook)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
