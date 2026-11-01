#!/usr/bin/env bash
# Copy the kit into a repository. Existing files are never overwritten.
# Usage: ./install.sh <path-to-repo>
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")" && pwd)/kit"
TARGET="${1:-}"

if [ -z "$TARGET" ] || [ ! -d "$TARGET" ]; then
  echo "用法：./install.sh <目标仓库路径>" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

copied=()
skipped=()
while IFS= read -r src; do
  rel="${src#"$KIT_DIR"/}"
  dest="$TARGET/$rel"
  if [ -e "$dest" ]; then
    skipped+=("$rel")
    continue
  fi
  mkdir -p "$(dirname "$dest")"
  cp "$src" "$dest"
  copied+=("$rel")
done < <(find "$KIT_DIR" -type f ! -name '.DS_Store' | sort)

chmod +x "$TARGET/scripts/parse-ai-review.py" 2>/dev/null || true

# Pull the team workflow into CLAUDE.md without touching what is already there.
IMPORT_LINE="@.claude/team-workflow.md"
CLAUDE_MD="$TARGET/CLAUDE.md"
if [ ! -f "$CLAUDE_MD" ]; then
  printf '# 项目说明\n\n%s\n' "$IMPORT_LINE" > "$CLAUDE_MD"
  copied+=("CLAUDE.md")
elif ! grep -qxF "$IMPORT_LINE" "$CLAUDE_MD"; then
  printf '\n%s\n' "$IMPORT_LINE" >> "$CLAUDE_MD"
  copied+=("CLAUDE.md（在末尾加了一行引用）")
fi

echo "已写入 ${#copied[@]} 个文件："
for f in ${copied[@]+"${copied[@]}"}; do echo "  + $f"; done
if [ ${#skipped[@]} -gt 0 ]; then
  echo "已存在、没有覆盖的 ${#skipped[@]} 个文件（需要的话手动合并）："
  for f in "${skipped[@]}"; do echo "  = $f"; done
fi

cat <<'EOF'

接下来（详见 kit 的 README）：
  1. 填写 .claude/team-workflow.md 的“项目信息”，和 REVIEW.md 的“高风险清单”
  2. 安装 Claude GitHub App，添加仓库密钥 ANTHROPIC_API_KEY
  3. 创建标签：risk:low、risk:medium、risk:high
  4. 打开分支保护：合入前必须通过你们现有的 CI
EOF
