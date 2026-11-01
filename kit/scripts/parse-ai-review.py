#!/usr/bin/env python3
"""Extract and validate the ai-review-result block from an AI review comment.

Usage: parse-ai-review.py [comment.md]   (reads stdin when no file is given)
Prints the last result as JSON; exits 1 if none is found or it is malformed.
Task platforms can call this from a webhook to update task status.
"""
import json
import re
import sys

BLOCK = re.compile(r"<!--\s*ai-review-result\s*(.*?)\s*-->", re.S)
SCORE_KEYS = ("requirement", "evidence", "risk", "readability")


def validate(result):
    errors = []
    scores = result.get("scores")
    if not isinstance(scores, dict):
        errors.append("scores must be an object")
    else:
        for key in SCORE_KEYS:
            value = scores.get(key)
            if value is None and key == "requirement":
                continue
            if not isinstance(value, int) or not 1 <= value <= 5:
                errors.append(f"scores.{key} must be an integer 1-5")
    if result.get("risk_level") not in ("low", "medium", "high"):
        errors.append("risk_level must be low, medium or high")
    if result.get("reviewers_needed") not in (0, 1, 2):
        errors.append("reviewers_needed must be 0, 1 or 2")
    issues = result.get("issues")
    if not isinstance(issues, list):
        errors.append("issues must be a list")
    else:
        for i, issue in enumerate(issues):
            if issue.get("origin") not in ("new", "existing", "unsure"):
                errors.append(f"issues[{i}].origin must be new, existing or unsure")
            if issue.get("severity") not in ("blocker", "major", "minor"):
                errors.append(f"issues[{i}].severity must be blocker, major or minor")
    return errors


def main():
    text = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    blocks = BLOCK.findall(text)
    if not blocks:
        sys.exit("no ai-review-result block found")
    try:
        result = json.loads(blocks[-1])
    except json.JSONDecodeError as e:
        sys.exit(f"ai-review-result is not valid JSON: {e}")
    errors = validate(result)
    if errors:
        sys.exit("invalid ai-review-result:\n  " + "\n  ".join(errors))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
