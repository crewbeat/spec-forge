#!/usr/bin/env bash
# Install or update Forge in a git repository.
#
#   ./install.sh /path/to/your/repo
#
# Copies the skill, the agents and the factory scripts. On an update it keeps
# your forge/config.json and forge/checks.sh.
set -euo pipefail

src="$(cd "$(dirname "$0")" && pwd)"
target="${1:?usage: install.sh /path/to/your/repo}"
target="$(cd "$target" && pwd)"

git -C "$target" rev-parse --show-toplevel >/dev/null 2>&1 || { echo "$target is not a git repository" >&2; exit 1; }
[ "$(git -C "$target" rev-parse --show-toplevel)" = "$target" ] || { echo "give the repository's root folder" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "Forge needs python3 3.9 or later" >&2; exit 1; }

mkdir -p "$target/.claude/skills/forge" "$target/.claude/agents" "$target/forge/bin" "$target/forge/lib" "$target/forge/inbox"
cp "$src/.claude/skills/forge/SKILL.md" "$target/.claude/skills/forge/SKILL.md"
cp "$src"/.claude/agents/forge-*.md "$target/.claude/agents/"
cp "$src/forge/bin/forge" "$target/forge/bin/forge"
chmod +x "$target/forge/bin/forge"
rm -rf "$target/forge/lib/forgelib"
mkdir "$target/forge/lib/forgelib"
cp "$src"/forge/lib/forgelib/*.py "$target/forge/lib/forgelib/"
cp "$src/forge/dashboard.html" "$target/forge/dashboard.html"
touch "$target/forge/inbox/.gitkeep"

for keep in config.json checks.sh; do
  if [ -e "$target/forge/$keep" ]; then
    echo "kept your forge/$keep"
  else
    cp "$src/forge/$keep" "$target/forge/$keep"
  fi
done
chmod +x "$target/forge/checks.sh"

touch "$target/.gitignore"
for line in "forge/jobs/" "forge/inbox/*.md" "forge/inbox/imported/" ".worktrees/" "__pycache__/"; do
  grep -qxF "$line" "$target/.gitignore" || echo "$line" >> "$target/.gitignore"
done

echo
echo "Forge is installed in $target"
echo "Next:"
echo "  1. Put this repo's test, lint and build commands in forge/checks.sh"
echo "  2. Set base_branch, models and the human gates in forge/config.json"
echo "  3. Commit the new files, then open Claude Code there and type: /forge run"
