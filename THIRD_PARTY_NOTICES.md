# Third-party notices

Orkestra's own code and documents are released under the MIT License (see `LICENSE`).

The folder `plugins/orkestra/vendor/` contains files copied from other open-source projects. They are
**read-only references**: nothing in that folder is loaded or executed at runtime; Orkestra's playbooks and
agent prompts were written with them as a source of ideas. Each folder keeps its upstream licence file, and
those files stay under their own licences, listed here. Pinned commits and dates: `plugins/orkestra/vendor/PINNED.md`.

| Folder (`plugins/orkestra/vendor/…`) | Upstream | Commit | Licence | Copyright notice | Licence files in the folder |
|---|---|---|---|---|---|
| `superpowers` | https://github.com/obra/superpowers | `8ca22db` | MIT | Copyright (c) 2025 Jesse Vincent | LICENSE |
| `ecc` | https://github.com/affaan-m/ECC | `ef648e0` | MIT | Copyright (c) 2026 Affaan Mustafa | LICENSE |

Notes:

- **ecc.** `agents/database-reviewer.ref.md` states that it includes patterns adapted from Supabase Agent
  Skills (MIT); that credit line is kept in the file.
- Files in every vendor folder that upstream names `SKILL.md` or `<agent>.md` are stored as `*.ref.md`
  for the same reason as above. Orkestra is an independent project and is not affiliated with, sponsored by
  or endorsed by any of the projects listed here, nor by Anthropic (Claude Code) or Anysphere (Cursor).

To refresh the references from upstream: `bash install.sh --refresh-vendor` (then update `PINNED.md` and
this file if a licence or copyright line changed).
