#!/bin/bash
# 重新生成 README 用的截图。数据来自 tests/fixtures.py 的「normal」样本，
# 不读你自己的 ~/.codex —— 截图可复现，也不会把个人用量印进仓库。
#
#   ./build.sh && docs/render.sh            # 写到 docs/
#   docs/render.sh /some/other/dir          # 先写到别处看看
set -e
cd "$(dirname "$0")/.."
OUT="${1:-docs}"
[ -x ./codex-cost ] || { echo "找不到 ./codex-cost，先跑 ./build.sh"; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
python3 tests/fixtures.py "$TMP" >/dev/null
mkdir -p "$OUT"
export CODEX_HOME="$TMP/normal"
./codex-cost --render "$OUT/panel-en.png" --expanded --lang en
./codex-cost --render "$OUT/panel-zh.png" --expanded --lang zh
./codex-cost --social "$OUT/social-preview.png" "$OUT/panel-en.png" --lang en

# 「历史用量」页：tests/usage_fixtures.py 生成的一周演示数据（Codex、Claude Code、opencode）
U="$TMP/usage"
python3 tests/usage_fixtures.py "$U" >/dev/null
(
  export CODEX_HOME="$U/codex" CLAUDE_CONFIG_DIR="$U/claude" XDG_DATA_HOME="$U/xdg" CODEX_COST_DATA_DIR="$U/data"
  ./codex-cost --render-history "$OUT/history-en.png" week --lang en
  ./codex-cost --render-history "$OUT/history-zh.png" week --lang zh
)
