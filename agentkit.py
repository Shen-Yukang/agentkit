#!/usr/bin/env python3
"""agentkit: one generic kit, one config file (agentkit.toml) per repository.

Commands:
  init <repo>                  install or upgrade the kit in a repository
  context [--for review]       print this repository's facts for an agent
  checks [--base REF] [--evidence FILE]
                               run the checks that apply to the changed files
  risk [--base REF]            path-based risk floor, as JSON
  validate FILE                check the review result block in FILE
  gate --review FILE [--pr N] [--base REF] [--head SHA] [--dry-run] [--comments-file F]
                               finalize an AI review, post it, label the PR, pass or fail
  hook pre-edit|stop           Claude Code hooks (read the hook JSON on stdin)

Requires Python 3.11+ (tomllib). No third-party packages.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "0.2.0"
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
        except ModuleNotFoundError:
            sys.exit("agentkit 需要 Python 3.11 及以上（用到标准库 tomllib）")
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
    }


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

def hook_pre_edit(root: Path, cfg: dict, payload: dict) -> int:
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path or not cfg["protected"]:
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
    return 0


def hook_stop(root: Path, cfg: dict, payload: dict) -> int:
    if payload.get("stop_hook_active") or not cfg["check_on_stop"] or not cfg["checks_always"]:
        return 0
    if not git(root, "status", "--porcelain").strip():
        return 0
    results = run_checks(root, [(c, "每次都跑") for c in cfg["checks_always"]], min(cfg["timeout"], 300))
    failed = [r for r in results if r["code"] != 0]
    if not failed:
        return 0
    parts = ["agentkit：结束前的自动检查没通过："]
    for r in failed:
        parts += [f"- `{r['cmd']}` 退出码 {r['code']}", r["tail"][-1500:]]
    parts.append("请修好后再结束；如果这次不该修，说明原因。")
    print("\n".join(parts), file=sys.stderr)
    return 2


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
              "[requirements]",
              "# 需求写在哪：pr_body（PR 描述）；pmm（任务平台，后续支持）",
              'source = "pr_body"',
              "",
              "[hooks]",
              "# Claude 结束一轮前自动跑 checks.always，没通过就让它接着修",
              "check_on_stop = true",
              ""]
    return "\n".join(lines)


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
    deny = data.setdefault("permissions", {}).setdefault("deny", [])
    for rule in ("Read(./.env)", "Read(./.env.*)"):
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
    for name in ("review-format.md", "team-workflow.md"):
        write_owned(templates / name, kit / name, report)
    for src in sorted((templates / "commands").glob("*.md")):
        write_owned(src, root / ".claude" / "commands" / src.name, report)
    write_owned(templates / "workflow.yml", root / ".github" / "workflows" / "agentkit.yml", report)

    if merge_settings(root / ".claude" / "settings.json"):
        report["updated"].append((root / ".claude" / "settings.json", "（加入钩子和读取限制）"))
    cfg = load_config(root)
    ensure_lines(root / "CLAUDE.md", [f"@{r}" for r in cfg["rules_always"]] + ["@.agentkit/team-workflow.md"],
                 "# 给 Claude Code 的说明\n\n## 被纠正过的规则\n\n每被人纠正一次，就在这里加一条。\n\n- （暂无）\n\n## 引入的规则\n\n")
    ensure_lines(root / ".gitignore", [".agentkit/*.local.md"], "")

    print(f"agentkit {VERSION} 已安装到 {root}")
    for key, title in (("created", "新建"), ("updated", "更新"), ("skipped", "跳过")):
        for path, note in report[key]:
            print(f"  {title}：{os.path.relpath(path, root)}{note}")
    print("\n下一步：检查 agentkit.toml；在 GitHub 添加 ANTHROPIC_API_KEY，并把 agentkit / checks、agentkit / review 设为必须通过的检查。")
    return 0


# ---------------------------------------------------------------- other commands

def cmd_context(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    def listing(items: list[str]) -> str:
        return "、".join(f"`{i}`" for i in items) if items else "（无）"
    out = ["# 本仓库的 agentkit 配置（来自 agentkit.toml）", ""]
    out.append(f"- 写代码时遵守：{listing(cfg['rules_always'])}（已通过 CLAUDE.md 引入）")
    if args.purpose == "review":
        out.append(f"- 审查时按顺序读：{listing(cfg['rules_always'] + cfg['rules_review'] + ['.agentkit/review-format.md'])}")
        out.append("- 项目自己的审查规则和 review-format.md 冲突时，以项目规则为准；输出格式始终按 review-format.md")
    out.append(f"- 需求在哪：{'PR 描述' if cfg['requirements'] == 'pr_body' else cfg['requirements']}")
    out.append(f"- 每次都跑的检查：{listing(cfg['checks_always'])}")
    for entry in cfg["checks_by_path"]:
        out.append(f"- 改到 {listing(entry['paths'])} 时加跑：{listing(entry['run'])}")
    out.append(f"- 受保护路径（不能改）：{listing(cfg['protected'])}")
    out.append(f"- 高风险路径（改到就至少高风险）：{listing(cfg['high_risk'])}")
    r = cfg["reviewers"]
    out.append(f"- 建议人审人数：低 {r['low']}、中 {r['medium']}、高 {r['high']}")
    out.append("- 运行检查并拿到证据：`python3 .agentkit/agentkit.py checks`")
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
    "billing_error": "API 账户额度不足：到 Anthropic Console 的 Billing 页面充值，或者改用 Claude 订阅的 OAuth token（claude_code_oauth_token）。",
    "authentication_error": "API key 无效：检查仓库密钥 ANTHROPIC_API_KEY 是否填对。",
    "permission_error": "这个 API key 没有调用该模型的权限：检查账户的模型权限。",
    "rate_limit_error": "触发了限流：稍后在 Actions 页面重跑这个任务。",
    "overloaded_error": "服务暂时过载：稍后在 Actions 页面重跑这个任务。",
}


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
            if kind in ERROR_HINTS:
                lines.append(f"  - 处理办法：{ERROR_HINTS[kind]}")
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


def cmd_hook(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if args.event == "pre-edit":
        return hook_pre_edit(root, cfg, payload)
    return hook_stop(root, cfg, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentkit", description=f"agentkit {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="安装或升级到某个仓库")
    p.add_argument("repo")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("context", help="打印本仓库的配置，给 agent 读")
    p.add_argument("--for", dest="purpose", choices=["coding", "review"], default="coding")
    p.set_defaults(func=cmd_context)
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
    p = sub.add_parser("hook", help="Claude Code 钩子")
    p.add_argument("event", choices=["pre-edit", "stop"])
    p.set_defaults(func=cmd_hook)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
