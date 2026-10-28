---
name: orkestra-preflight
description: Pre-PR gate for the Orkestra repo. Use before declaring a change done or opening a pull request.
---

# Preflight

Run, report real output, never hide exit codes:

```bash
cd plugins/orkestra/tests && python3 -m unittest discover -s . -q
cd bridge/tests && python3 -m unittest discover -s . -q
cd integrations/openclaw/plugin && npm test          # only if touched
python3 plugins/orkestra/bin/orkestra cursor build   # only if agents/skills/hooks changed, then re-run tests
scripts/release.sh --check                           # version drift
```

Checklist: test that fails without the change; EN+TR catalogs and mirrored `.tr.md` docs; `## Unreleased` line in CHANGELOG if user-visible; no secrets, personal paths or sibling-project content in the diff; nothing under `dist/`, `internal/`, `~/.orkestra`. No commit or push unless the user asked.
