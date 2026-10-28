---
name: orkestra-guard-change
description: Change or add an Orkestra hook/guard decision safely (latency budget + decision corpus). Use when editing guards.py, hookmain.py, hookboot.py or hooks/, or adding a guard case.
---

# Guard change

1. `python3 plugins/orkestra/bin/orkestra watch`. If a run is open, stop: work on a branch or copy.
2. Clean tree: `cd plugins/orkestra/tests && python3 guard_corpus.py --check` must print `0 differences`.
3. Write the failing test first (`test_guards` / `test_guards2`). For a looser decision, also add a test that the true positive is still denied.
4. Implement with no new top-level import in `hookmain.py`, `hookboot.py`, `guards.py`.
5. Add the case at the END of `BASH_MULTI` (or `FILE_PATHS`, `MCP`, `OTHER_TOOLS`), then `python3 guard_corpus.py --write`.
6. `git diff --stat tests/data/guard_decisions.json`: only added rows. A changed row needs a stated reason in the PR and in a test.
7. `python3 -m unittest test_hook_latency test_guards test_guards2 -q`, then the full plugin and bridge suites.
8. If `agents/`, `skills/` or `hooks/` changed: `python3 plugins/orkestra/bin/orkestra cursor build`.

Report: which decisions changed (should be none unless intended), test output, corpus diff stat.
