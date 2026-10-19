#!/bin/sh
# Prepare an Orkestra release locally: see scripts/release.py (version bump everywhere, dated CHANGELOG, tar.gz +
# SHA256SUMS, prints the `gh release create` command and never runs it). Dry run unless --apply.
#
#   scripts/release.sh 1.7.0              # plan only
#   scripts/release.sh 1.7.0 --apply      # write the files and the archive
#   scripts/release.sh --check            # version drift check
here="$(cd "$(dirname "$0")" && pwd)"
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    exec "$py" "$here/release.py" "$@"
  fi
done
echo "release.sh: Python 3.9+ not found" >&2
exit 127
