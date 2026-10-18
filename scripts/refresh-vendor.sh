#!/usr/bin/env bash
# Re-pull the pinned reference files (see plugins/orkestra/vendor/PINNED.md) from upstream HEAD.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
V="$ROOT/plugins/orkestra/vendor"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
clone() { git clone -q --depth 1 "https://github.com/$1" "$TMP/$2"; }
clone obra/superpowers sp
for s in systematic-debugging test-driven-development verification-before-completion subagent-driven-development writing-plans brainstorming receiving-code-review; do
  rm -rf "$V/superpowers/skills/$s"; cp -r "$TMP/sp/skills/$s" "$V/superpowers/skills/$s"
  [ -f "$V/superpowers/skills/$s/SKILL.md" ] && mv "$V/superpowers/skills/$s/SKILL.md" "$V/superpowers/skills/$s/SKILL.ref.md"
done
bash "$ROOT/scripts/refresh-vendor-ecc.sh"
echo "vendor refreshed from upstream HEAD — update the commit column in $V/PINNED.md:"
echo "  sp $(git -C "$TMP/sp" log -1 --format='%h %cs')"
