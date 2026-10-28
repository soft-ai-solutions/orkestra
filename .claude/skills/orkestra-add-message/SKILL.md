---
name: orkestra-add-message
description: Add or change human-facing Orkestra text (dashboards, watch, notifications, artifacts, --help, bridge) in both English and Turkish. Use for any new user-visible string.
---

# Add a message

1. Decide the reader: model text stays in place in English; human text goes to `plugins/orkestra/lib/orkestra_core/messages/<area>.py`; machine tokens are never localized.
2. Add `"<area>.<name>"` to both `EN` and `TR` with identical `{fields}`. EN has no Turkish letters, TR is a real translation, no em dash (use `:`, `.`, ` · `).
3. Use `i18n.t("<area>.<name>", field=value)`. No string concatenation of translated pieces.
4. Test both languages with `ORKESTRA_LANG=en` and `tr` set explicitly; for EN assert no `[çğıöşüÇĞİÖŞÜ]` unless user text is echoed.
5. Protocol tokens (section headings, AskUserQuestion labels): only add spellings in `messages/protocol.py` and keep `templates/en` and `templates/tr` in sync.
6. Gate: `cd plugins/orkestra/tests && python3 -m unittest test_i18n test_docs_drift -q`, then full suites. Mirror any doc change in the `.tr.md` file.
