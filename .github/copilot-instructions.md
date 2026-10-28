# Copilot instructions: Orkestra repo

Read `AGENTS.md` and `CONTRIBUTING.md` first. Key traps: stdlib-only Python; `plugins/orkestra/cursor/` is generated
(`python3 plugins/orkestra/bin/orkestra cursor build`); do not edit hooks/guards/agents while `orkestra watch` shows an
open run; every change needs a failing-first test; human-facing text goes through `i18n.t` in EN and TR; no commit or
push unless asked. Gates: plugin and bridge `unittest discover` suites.
