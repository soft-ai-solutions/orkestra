#!/usr/bin/env bash
# Re-pull the pinned ECC reference files (plugins/orkestra/vendor/ecc/SOURCE.md) from upstream HEAD.
# Standalone on purpose: it touches only vendor/ecc. scripts/refresh-vendor.sh may call it.
# References only: nothing here is auto-loaded.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
V="${ORKESTRA_VENDOR_DIR:-$ROOT/plugins/orkestra/vendor}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
clone() { git clone -q --depth 1 "https://github.com/$1" "$TMP/$2"; }
clone affaan-m/ECC ecc
E="$V/ecc"
mkdir -p "$E/agents" "$E/skills" "$E/rules"
cp "$TMP/ecc/LICENSE" "$E/LICENSE"
for a in security-reviewer silent-failure-hunter typescript-reviewer react-reviewer database-reviewer pr-test-analyzer; do
  cp "$TMP/ecc/agents/$a.md" "$E/agents/$a.ref.md"
done
for s in security-review click-path-audit database-migrations prisma-patterns e2e-testing strategic-compact search-first; do
  rm -rf "$E/skills/$s"; cp -r "$TMP/ecc/skills/$s" "$E/skills/$s"
  [ -f "$E/skills/$s/SKILL.md" ] && mv "$E/skills/$s/SKILL.md" "$E/skills/$s/SKILL.ref.md"
done
for r in common/security.md typescript/security.md react/security.md react/hooks.md web/security.md \
         react-native/security.md react-native/performance.md react-native/production-readiness.md react-native/patterns.md; do
  mkdir -p "$E/rules/$(dirname "$r")"; cp "$TMP/ecc/rules/$r" "$E/rules/$r"
done
echo "ecc refreshed from upstream HEAD — update the commit line in $E/SOURCE.md:"
echo "  ecc $(git -C "$TMP/ecc" log -1 --format='%h %cs')"
