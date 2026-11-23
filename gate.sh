#!/usr/bin/env bash
# Locked. Runs the checks; the agent never decides by itself that tests passed.
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
LOCKED="AGENTS.md GEMINI.md gate.sh bench"
MAX_SRC_LINES=1800
fail=0

db_stat() {
  local p="$HOME/.local/share/mesa-lore/lore.db"
  if [ -e "$p" ]; then
    stat -c '%y' "$p"
  else
    echo "MISSING"
  fi
}

echo "== locked paths"
if git rev-parse -q --verify refs/tags/lore-baseline-10a >/dev/null; then
  if ! git diff --quiet lore-baseline-10a -- $LOCKED; then
    echo "FAIL: locked paths changed"; git diff --stat lore-baseline-10a -- $LOCKED; fail=1
  fi
  if [ -n "$(git ls-files --others --exclude-standard -- $LOCKED)" ]; then
    echo "FAIL: new files under locked paths"; fail=1
  fi
else
  echo "FAIL: tag lore-baseline-10a missing"; fail=1
fi

db_before=$(db_stat)

echo "== tests"
if ls tests/test_*.py >/dev/null 2>&1; then
  "$PY" -m pytest -q || fail=1
else
  echo "no tests yet"
fi

echo "== bench (every category must pass)"
"$PY" bench/run.py --require all || fail=1

db_after=$(db_stat)
if [ "$db_before" != "$db_after" ]; then
  echo "FAIL: default DB was modified during gate ($db_before -> $db_after)"; fail=1
fi

echo "== size"
if [ -d src/mesa_lore ]; then
  lines=$(find src/mesa_lore -name '*.py' -type f -exec wc -l {} + | tail -n 1 | awk '{print $1}')
  echo "src/mesa_lore: ${lines} total (limit ${MAX_SRC_LINES})"
  if [ "${lines:-0}" -gt "$MAX_SRC_LINES" ]; then
    echo "FAIL: src/mesa_lore exceeds ${MAX_SRC_LINES} lines"; fail=1
  fi
fi

if [ "$fail" = 0 ]; then echo "GATE: PASS"; else echo "GATE: FAIL"; fi
exit "$fail"
