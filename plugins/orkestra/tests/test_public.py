"""What the public-release audit (2026-10-06) found that a weak orchestrator could get wrong: closing a task with
no review, loading the wrong playbook, ending a headless turn under its subagents, a fix round without the review.
Plus the files a public repository must ship (licences, notices, no private data)."""
import json
import os
import re
import subprocess
import time
import unittest
from pathlib import Path

import helpers
from test_tempo import TempoBase

ROOT = helpers.PLUGIN.parent.parent


class TestTaskReviewIsEnforced(TempoBase):
    def ready(self):
        self.to_implement()
        self.cli("task", "start", "T001", "-p", "demo")
        self.tdd_cycle("T001", "sub", "a - b", 5, 3, 2)

    def done(self):
        return self.cli("task", "done", "T001", "-p", "demo", "review: PASS", check=False)

    def test_m_run_needs_a_pass_review_of_this_round(self):
        self.ready()
        cp = self.done()
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no review of this round", cp.stderr + cp.stdout)
        self.assertIn("scope: task T001", cp.stderr + cp.stdout)
        (self.fdir / "reviews" / "T001.review.md").write_text("# review\nlooks fine\n")
        self.assertIn("has no `VERDICT:", self.done().stderr)
        self.review("T001", "FAIL")
        cp = self.done()
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("the review says FAIL", cp.stderr)
        self.assertIn("orkestra task reopen T001", cp.stderr)
        # the FAIL is applied by a reopen: the same file (even rewritten to PASS in place, older than now) is stale
        self.cli("task", "reopen", "T001", "review round 1", "-p", "demo")
        brief = self.cli("brief", "T001", "-p", "demo").stdout
        self.assertIn("## Review to fix", brief)
        self.assertIn("T001.review.md", brief)
        self.cli("task", "start", "T001", "-p", "demo")
        self.tdd_cycle("T001", "sub2", "a - b", 9, 3, 6)
        self.assertIn("no review of this round", self.done().stderr)
        time.sleep(0.05)
        self.review("T001", "PASS_WITH_NOTES", minors=1)
        self.assertEqual(self.done().returncode, 0)
        self.assertNotIn("## Review to fix", self.cli("brief", "T001", "-p", "demo").stdout)

    def test_adhoc_tasks_and_the_switch(self):
        self.ready()
        self.cli("task", "add", "FIX-R001", "extra fix", "--no-tdd", "-p", "demo")
        self.cli("task", "start", "FIX-R001", "-p", "demo", "--force", "test", check=False)
        self.assertEqual(self.cli("task", "done", "FIX-R001", "-p", "demo", check=False).returncode, 0)
        self.assertNotEqual(self.done().returncode, 0)
        self.home.mkdir(parents=True, exist_ok=True)
        cfgp = self.home / "config.json"
        cfg = json.loads(cfgp.read_text()) if cfgp.exists() else {}
        cfg.setdefault("tempo", {})["task_review_required"] = False
        cfgp.write_text(json.dumps(cfg))
        self.assertEqual(self.done().returncode, 0)


class TestSmallRunsAreNotReviewedPerTask(TempoBase):
    size = "S"

    def test_s_run(self):
        self.to_implement()
        self.cli("task", "start", "T001", "-p", "demo")
        self.tdd_cycle("T001", "sub", "a - b", 5, 3, 2)
        self.assertEqual(self.cli("task", "done", "T001", "-p", "demo", check=False).returncode, 0)


class TestPlaybookKnowsThePhase(TempoBase):
    def test_no_argument_is_the_current_phase(self):
        cp = self.cli("playbook", cwd=self.proj, check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no open run here", cp.stderr)
        self.assertIn("headless", self.cli("playbook", "headless", cwd=self.proj).stdout.lower())      # needs no run
        self.start()
        intake = self.cli("playbook", "-p", "demo").stdout
        self.assertEqual(intake, self.cli("playbook", "intake", "-p", "demo").stdout)
        self.assertNotIn("NOTE: run", intake)
        wrong = self.cli("playbook", "implement", "-p", "demo").stdout
        self.assertTrue(wrong.startswith("NOTE: run"), wrong[:80])
        self.assertIn("is in phase `intake`, not `implement`", wrong)
        self.assertNotIn("NOTE: run", self.cli("playbook", "security", "-p", "demo").stdout)           # not a phase
        self.assertNotIn("HEADLESS:", intake)

    def test_a_headless_run_is_told_how_to_wait(self):
        self.start()
        out = self.cli("playbook", "-p", "demo", env={"ORKESTRA_HEADLESS": "1"}).stdout
        self.assertTrue(out.startswith("HEADLESS: "), out[:80])
        self.assertIn("`orkestra wait`", out)                    # one blocking call instead of `sleep 30; orkestra next` polling
        self.assertNotIn("sleep 30;", out.replace("never `sleep` + `orkestra next` polling", ""))
        self.assertIn("do not end your turn", (helpers.PLUGIN / "skills/run/references/headless.md").read_text().lower())


class TestPublicRepository(unittest.TestCase):
    TEXT = (".md", ".py", ".js", ".json", ".html", ".sh", ".yml", ".txt")

    def files(self):
        for d, dirs, names in os.walk(ROOT):
            root = Path(d)
            dirs[:] = [
                name for name in dirs
                if name not in ("__pycache__", ".git", "node_modules", ".orkestra")
                and not (root == ROOT and name in ("dist", "internal"))
            ]
            for n in names:
                if n.endswith(self.TEXT):
                    yield Path(d) / n

    def test_every_vendored_project_ships_its_licence_and_is_listed(self):
        vendor = helpers.PLUGIN / "vendor"
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        pinned = (vendor / "PINNED.md").read_text(encoding="utf-8")
        dirs = sorted(p.name for p in vendor.iterdir() if p.is_dir())
        self.assertGreaterEqual(len(dirs), 2)
        for d in dirs:
            lic = vendor / d / "LICENSE"
            self.assertTrue(lic.is_file() and lic.stat().st_size > 200, f"vendor/{d} has no LICENSE")
            self.assertIn(f"| `{d}` |", notices, f"vendor/{d} missing from THIRD_PARTY_NOTICES.md")
            self.assertIn(f"| {d} |", pinned, f"vendor/{d} missing from PINNED.md")
            row = next(ln for ln in notices.splitlines() if ln.startswith(f"| `{d}` |"))
            self.assertRegex(row, r"https://github\.com/\S+ \| `[0-9a-f]{7}` \| (MIT|Apache-2\.0)")
            if "Apache" in row or "Apache" in lic.read_text(encoding="utf-8")[:200]:
                full = vendor / d / "LICENSE-APACHE-2.0.txt"
                self.assertIn("TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION", full.read_text(encoding="utf-8"))
        for holder in ("Jesse Vincent", "Affaan Mustafa"):
            self.assertIn(holder, notices)
        self.assertEqual((helpers.PLUGIN / "LICENSE").read_text(), (ROOT / "LICENSE").read_text())
        self.assertTrue((helpers.PLUGIN / "THIRD_PARTY_NOTICES.md").is_file())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("THIRD_PARTY_NOTICES.md", readme)
        self.assertIn("https://github.com/obra/superpowers", readme)
        self.assertIn("https://github.com/affaan-m/ECC", readme)

    def test_nothing_private_is_published(self):
        bad = re.compile(r"[\w.+-]+@(gmail|hotmail|outlook|yahoo|icloud)\.\w+|/Users/[a-z][\w.-]+/|/home/[a-z][\w.-]+/"
                         r"|\bchat_id=\d{6,}|\b\d{8,10}:[\w-]{30,}\b")
        allowed = ("/Users/alex/", "/Users/me/", "/Users/you/", "/Users/x/", "/home/user/", "/home/claude/", "/home/runner/")
        hits = []
        for p in self.files():
            if "/vendor/" in str(p) or p.name == "test_public.py":
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for m in bad.finditer(line):
                    if not m.group(0).startswith(allowed):
                        hits.append(f"{p.relative_to(ROOT)}:{i}: {m.group(0)[:40]}")
        self.assertEqual(hits[:15], [])
        for manifest in (ROOT / ".claude-plugin/marketplace.json", helpers.PLUGIN / ".claude-plugin/plugin.json"):
            self.assertNotIn("email", manifest.read_text())
        ignore_file = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/internal/", ignore_file.splitlines())
        self.assertIn("/dist/", ignore_file.splitlines())
        for path in ("dist/release.tar.gz", "internal/notes.md"):
            cp = subprocess.run(["git", "check-ignore", "--no-index", "--quiet", "--", path], cwd=ROOT)
            self.assertEqual(cp.returncode, 0, f"{path} should be ignored")
        for path in ("bridge/dist/package.json", "bridge/internal/guide.md"):
            cp = subprocess.run(["git", "check-ignore", "--no-index", "--quiet", "--", path], cwd=ROOT)
            self.assertEqual(cp.returncode, 1, f"{path} should not be ignored by the root-only rules")
        # FR-005 / E8: sibling project tokens banned in shipped plugin sources only
        # (hooks/lib/agents/skills; vendor + tests/ out of scope — fixtures stay).
        sibling = re.compile(r"other-app|sample-app", re.I)
        sibling_hits = []
        for area in ("hooks", "lib", "agents", "skills"):
            base = helpers.PLUGIN / area
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if not p.is_file() or not str(p).endswith(self.TEXT):
                    continue
                rel = str(p.relative_to(helpers.PLUGIN))
                if "/vendor/" in f"/{rel}/" or rel.startswith("vendor/"):
                    continue
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if sibling.search(line):
                        sibling_hits.append(f"plugins/orkestra/{rel}:{i}: {line.strip()[:80]}")
        self.assertEqual(sibling_hits[:15], [])

    def test_documents_point_at_files_that_exist(self):
        for doc in ("README.md", "README.tr.md", "ORKESTRA.md", "ORKESTRA.tr.md", "CONTRIBUTING.md", "SECURITY.md",
                    "THIRD_PARTY_NOTICES.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            for target in re.findall(r"\]\(((?!https?:|#|mailto:)[^)\s]+)\)", text):
                self.assertTrue((ROOT / target.split("#")[0]).exists(), f"{doc} links to missing {target}")
            for name in re.findall(r"`((?:[\w.-]+/)*[\w.-]+\.(?:md|sh|html))`", text):
                if "/" in name or name.isupper() or name[0].isupper():
                    hit = (ROOT / name).exists() or (helpers.PLUGIN / name).exists() or list(ROOT.glob(f"**/{name}"))
                    self.assertTrue(hit or name.startswith(("<", "~")) or "*" in name, f"{doc} names missing file {name}")


    def test_public_oss_placeholders_and_attribution(self):
        """Canonical clone URL is public; other personal branding / home paths stay banned."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/soft-ai-solutions/orkestra.git", readme)
        self.assertTrue((ROOT / "CODE_OF_CONDUCT.md").is_file())
        self.assertTrue((ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").is_file())
        # the issue forms (.yml) are the templates; the old free-text .md ones were removed because GitHub lists every
        # valid template in the folder, so reporters could still pick the form-less version (no version, OS, doctor output)
        for name in ("bug_report", "feature_request"):
            self.assertTrue((ROOT / ".github" / "ISSUE_TEMPLATE" / f"{name}.yml").is_file(), name)
            self.assertFalse((ROOT / ".github" / "ISSUE_TEMPLATE" / f"{name}.md").exists(), f"{name}.md duplicates the form")
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Orkestra contributors", license_text)
        self.assertEqual(license_text, (helpers.PLUGIN / "LICENSE").read_text(encoding="utf-8"))
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for needle in ("*.p8", "*.pem", "*.mobileprovision", "credentials.json", ".secrets/",
                       "CLAUDE.local.md", "*.key", ".env.*"):
            self.assertIn(needle, ignore)
        # The public clone path is the soft-ai-solutions org. Personal account names and private
        # home paths remain forbidden everywhere else.
        allowed_clone = re.compile(r"github\.com/soft-ai-solutions/orkestra", re.I)
        personal = re.compile(r"[A-Za-z0-9._%+-]+@(gmail|hotmail|outlook|yahoo|icloud)\.", re.I)
        home_orkestra = re.compile(r"~/Projects/orkestra|~/Projects/n8n")
        hits = []
        for path in self.files():
            rel = str(path.relative_to(ROOT))
            if "/vendor/" in rel or rel.startswith(".orkestra/") or path.name == "test_public.py":
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(body.splitlines(), 1):
                if home_orkestra.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:80]}")
                    continue
                scrubbed = allowed_clone.sub("", line)
                if personal.search(scrubbed):
                    hits.append(f"{rel}:{i}: {line.strip()[:80]}")
        self.assertEqual(hits[:20], [])
        n8n = (ROOT / "bridge" / "n8n_import.py").read_text(encoding="utf-8")
        self.assertIn("N8N_API_KEY", n8n)
        self.assertIn("N8N_STATE", n8n)
        self.assertNotIn('parent.parent / "n8n"', n8n)
        self.assertNotIn("../n8n", (ROOT / "install.sh").read_text(encoding="utf-8"))
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("HAS_CLAUDE", install)
        self.assertIn("Cursor-only", install)

    def test_install_sh_mentions_cursor_and_copilot_paths(self):
        """FR-005 / SC-004 / E6 / E9 / E11: install.sh Copilot detect/ask, readiness, epilog, doctor."""
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("HAS_CLAUDE", install)
        self.assertIn("Cursor-only", install)
        self.assertRegex(install, r"HAS_COPILOT|COPILOT_HOME|\.copilot")
        self.assertRegex(install, r'ask\s+"[^"]*[Cc]opilot')
        self.assertIn("copilot install", install)
        self.assertRegex(install, r"\bsetup\b")
        self.assertIn("--yes", install)
        # Three-way readiness: warn only when Claude, Cursor, and Copilot are all unready
        self.assertRegex(install, r"COPILOT_READY|copilot hooks", re.I)
        self.assertIn(
            '[ "$HAS_CLAUDE" = 0 ] && [ "$CURSOR_READY" = 0 ] && [ "$COPILOT_READY" = 0 ]',
            install,
        )
        warn_block = "\n".join(
            ln for ln in install.splitlines()
            if "warn" in ln.lower() and ("Claude" in ln or "Cursor" in ln or "Copilot" in ln or "hazır" in ln)
        )
        self.assertRegex(warn_block, r"[Cc]opilot")
        # Epilog lists setup and copilot install|status
        self.assertRegex(install, r'bin/orkestra"\s+setup|orkestra setup')
        self.assertRegex(install, r"copilot install \| status")
        # Doctor after engine install steps (second invocation beyond the early advisory call)
        doctor_calls = install.count('bin/orkestra" doctor')
        self.assertGreaterEqual(doctor_calls, 2, "doctor should run early and again after engines")
        # TR ask() preserved (E9)
        self.assertIn('ask()  { # ask "question" default(y|n)', install)
        # A1: --yes defaults only for *detected* engines (not “Claude absent ⇒ force y”)
        self.assertNotIn(
            '[ "$HAS_CLAUDE" = 0 ] && COPILOT_DEFAULT=y',
            install,
            "COPILOT_DEFAULT=y must not be forced solely because Claude is absent",
        )
        self.assertNotIn(
            '[ "$HAS_CLAUDE" = 0 ] && CURSOR_DEFAULT=y',
            install,
            "CURSOR_DEFAULT=y must not be forced solely because Claude is absent",
        )
        self.assertRegex(
            install,
            r'\[ "\$HAS_COPILOT" = 1 \] && COPILOT_DEFAULT=y',
            "Copilot --yes default must gate on HAS_COPILOT=1",
        )
        # CURSOR_DEFAULT=y only when Cursor is detected (HAS_CURSOR from agent and/or ~/.cursor)
        self.assertRegex(
            install,
            r"HAS_CURSOR=0",
            "install.sh must track Cursor detection via HAS_CURSOR",
        )
        self.assertRegex(
            install,
            r'\[ "\$HAS_CURSOR" = 1 \] && CURSOR_DEFAULT=y',
            "CURSOR_DEFAULT=y must gate on HAS_CURSOR=1 (detected Cursor)",
        )
        # A2: do not claim ready when the three-way readiness warn fired
        ready_ok = 'ok "$(L \'Orkestra is ready.\' \'Orkestra hazır.\')"'      # English + Turkish pair (install.sh resolves the language)
        self.assertIn(ready_ok, install)
        ready_ok_idx = install.index(ready_ok)
        readiness_warn = (
            '[ "$HAS_CLAUDE" = 0 ] && [ "$CURSOR_READY" = 0 ] && [ "$COPILOT_READY" = 0 ]'
        )
        self.assertIn(readiness_warn, install)
        warn_idx = install.index(readiness_warn)
        # Gate: "Orkestra hazır" must sit in an else/ready branch after the unready check,
        # not run unconditionally after the warn block.
        after_warn = install[warn_idx:ready_ok_idx]
        self.assertRegex(
            after_warn,
            r"\belse\b|READY|hazır değil|not.?ready|kurulum hazır değil",
            "ok Orkestra hazır must be gated (else/ready) when unready warn can fire",
        )
        self.assertRegex(
            install,
            r"hazır değil|not.?ready|kurulum hazır değil|Orkestra hazır değil",
            "unready path must print a clear not-ready line (not only warn)",
        )
        # FR-002 / E4: PATH shim always runs even when Cursor ask is declined
        self.assertRegex(
            install,
            r"ensure_path_shim|ORKESTRA_SHIM",
            "install.sh must invoke ensure_path_shim (or ORKESTRA_SHIM) for PATH",
        )
        self.assertIn(
            "ensure_path_shim",
            install,
            "install.sh must call cursorkit.ensure_path_shim / CLI path",
        )
        cursor_yes = re.search(
            r'if ask "[^"]*[Cc]ursor[^"]*"[^\n]*; then\n(.*?)^fi\s*$',
            install,
            re.M | re.S,
        )
        self.assertIsNotNone(cursor_yes, "Cursor ask if-block must exist")
        self.assertNotIn(
            "ensure_path_shim",
            cursor_yes.group(1),
            "ensure_path_shim must not live only inside the Cursor-yes branch (E4)",
        )

    def test_openclaw_ci_logic_only_and_docs(self):
        """FR-004 / E7 / SC-002: CI npm test is logic-only; e2e optional locally; docs honesty."""
        pkg = json.loads(
            (ROOT / "integrations" / "openclaw" / "plugin" / "package.json").read_text(
                encoding="utf-8"
            )
        )
        scripts = pkg.get("scripts") or {}
        self.assertIn("test", scripts)
        self.assertIn("test:e2e", scripts)
        self.assertIn("logic.test.js", scripts["test"])
        self.assertNotIn(
            "plugin.e2e.test.js",
            scripts["test"],
            "default npm test must not require Homebrew openclaw (logic-only)",
        )
        self.assertIn("plugin.e2e.test.js", scripts["test:e2e"])

        e2e = (
            ROOT / "integrations" / "openclaw" / "plugin" / "plugin.e2e.test.js"
        ).read_text(encoding="utf-8")
        self.assertIn("OPENCLAW_PKG", e2e)
        self.assertRegex(
            e2e,
            r"existsSync\s*\(\s*OPENCLAW_PKG\s*\)|skip:.*OPENCLAW_PKG|missing.*OPENCLAW_PKG",
            "e2e must skip (or exit 0) when OPENCLAW_PKG is absent",
        )

        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("openclaw", workflow)
        self.assertRegex(
            workflow,
            r"integrations/openclaw/plugin && npm test",
            "CI OpenClaw job must run default npm test (logic-only)",
        )
        self.assertNotIn(
            "test:e2e",
            workflow,
            "CI must not run OpenClaw e2e (Homebrew openclaw)",
        )
        # A1: logic job is a hard required check — no continue-on-error
        self.assertNotIn(
            "continue-on-error",
            workflow,
            "OpenClaw logic CI job must be required (no continue-on-error)",
        )

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        # A1: required CI logic job wording — must NOT brand logic npm test as optional/advisory
        self.assertNotRegex(
            readme,
            re.compile(
                r"optional\s*/\s*advisory.{0,80}[Oo]pen[Cc]law\s+logic|"
                r"[Oo]pen[Cc]law\s+logic[^\n]{0,80}optional\s*/\s*advisory|"
                r"advisory\s+[Oo]pen[Cc]law\s+logic|"
                r"[Oo]pen[Cc]law\s+logic-only\s+\(optional",
                re.I | re.S,
            ),
            "README must not call OpenClaw logic CI job optional/advisory (A1)",
        )
        self.assertRegex(
            readme,
            re.compile(
                r"required.{0,60}[Oo]pen[Cc]law.{0,40}logic|"
                r"[Oo]pen[Cc]law.{0,40}logic[^\n]{0,60}required|"
                r"[Oo]pen[Cc]law\s+logic-only\s+job|"
                r"logic-only\s*\(CI\)",
                re.I,
            ),
            "README must describe OpenClaw logic npm test as a required CI job (A1)",
        )
        self.assertRegex(
            readme,
            r"test:e2e|logic\.test|logic-only|logic only",
            "README must distinguish logic npm test from local e2e",
        )
        self.assertRegex(
            readme,
            re.compile(
                r"(e2e|test:e2e).{0,40}(local|optional)|(local|optional).{0,40}(e2e|test:e2e)",
                re.I,
            ),
            "README must call OpenClaw e2e local/optional (not the logic CI job)",
        )

        readme_tr = (ROOT / "README.tr.md").read_text(encoding="utf-8")
        self.assertNotRegex(
            readme_tr,
            re.compile(
                r"(isteğe bağlı|opsiyonel)\s*/\s*advisory.{0,80}[Oo]pen[Cc]law\s+logic|"
                r"[Oo]pen[Cc]law\s+logic[^\n]{0,80}(isteğe bağlı|opsiyonel)\s*/\s*advisory|"
                r"advisory\s+[Oo]pen[Cc]law\s+logic|"
                r"[Oo]pen[Cc]law\s+logic\s+\((isteğe bağlı|opsiyonel)",
                re.I | re.S,
            ),
            "README.tr must not call OpenClaw logic CI job optional/advisory (A1)",
        )
        self.assertRegex(
            readme_tr,
            re.compile(
                r"(zorunlu|required|CI).{0,60}[Oo]pen[Cc]law.{0,40}logic|"
                r"[Oo]pen[Cc]law.{0,40}logic[^\n]{0,60}(zorunlu|required|CI)",
                re.I,
            ),
            "README.tr must describe OpenClaw logic npm test as required CI (A1)",
        )
        self.assertRegex(
            readme_tr,
            re.compile(
                r"(e2e|test:e2e).{0,40}(yerel|isteğe bağlı|opsiyonel)|"
                r"(yerel|isteğe bağlı|opsiyonel).{0,40}(e2e|test:e2e)",
                re.I,
            ),
            "README.tr must call OpenClaw e2e local/optional",
        )

        contrib = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertRegex(
            contrib,
            r"test:e2e|OPENCLAW_PKG|optional.*e2e|e2e.*optional",
            "CONTRIBUTING must document local e2e (test:e2e / OPENCLAW_PKG)",
        )

    def test_doc_honesty_path_skip_fail_bridge_coc(self):
        """FR-006 / FR-007 / FR-008 / FR-009 / E9 / E10 / SC-003: labelled doc honesty needles."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_tr = (ROOT / "README.tr.md").read_text(encoding="utf-8")
        onboarding = (ROOT / "ONBOARDING.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        orkestra_md = (ROOT / "ORKESTRA.md").read_text(encoding="utf-8")
        orkestra_tr_md = (ROOT / "ORKESTRA.tr.md").read_text(encoding="utf-8")
        coc = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")

        # FR-006 / E9: PATH shim + install.sh --skip-tests in README and ONBOARDING
        for label, text in (("README.md", readme), ("ONBOARDING.md", onboarding)):
            self.assertRegex(
                text,
                r"ORKESTRA_SHIM_DIR|~/\.local/bin",
                f"FR-006: {label} must document PATH shim (~/.local/bin or ORKESTRA_SHIM_DIR)",
            )
            self.assertIn(
                "--skip-tests",
                text,
                f"FR-006 / E9: {label} must document install.sh --skip-tests",
            )

        # FR-007: Claude/Cursor fail-open vs Copilot PreToolUse fail-closed (product keeps fail-open)
        self.assertRegex(
            security,
            r"fail-open",
            "FR-007: SECURITY.md must state Claude/Cursor fail-open",
        )
        self.assertRegex(
            security,
            r"fail-closed",
            "FR-007: SECURITY.md must state Copilot PreToolUse fail-closed asymmetry",
        )
        self.assertRegex(
            security,
            r"[Cc]opilot.*[Pp]reToolUse|[Pp]reToolUse.*[Cc]opilot",
            "FR-007: SECURITY.md must name Copilot PreToolUse for fail-closed",
        )
        self.assertRegex(
            security,
            r"[Cc]laude|[Cc]ursor",
            "FR-007: SECURITY.md fail-open side must name Claude and/or Cursor",
        )
        limits = readme.split("## Limits you should know", 1)
        self.assertEqual(len(limits), 2, "FR-007: README must keep a Limits section")
        limits_body = limits[1].split("\n## ", 1)[0]
        self.assertRegex(
            limits_body,
            r"fail-open|fail open",
            "FR-007: README Limits must mention fail-open",
        )
        self.assertRegex(
            limits_body,
            r"fail-closed|fail closed",
            "FR-007: README Limits must mention Copilot fail-closed asymmetry",
        )
        self.assertRegex(
            limits_body,
            r"[Cc]opilot",
            "FR-007: README Limits must name Copilot in the asymmetry note",
        )

        # FR-008: bridge/workbench wording matches code (Copilot is a bridge engine); both language editions
        for doc, heading in ((orkestra_md, "## Scope"), (orkestra_tr_md, "## Kapsam")):
            kaps = doc.split(heading, 1)[1].split("## ", 1)[0]
            self.assertNotRegex(
                kaps,
                r"[Cc]opilot[^\n.]{0,80}(kapsam d[ıi][şs][ıi]|out of scope)",
                f"FR-008: ORKESTRA ({heading}) must not claim Copilot bridge/engine is out of scope",
            )
            self.assertRegex(
                kaps,
                r"[Bb]ridge.*[Cc]opilot|[Cc]opilot.*[Bb]ridge",
                f"FR-008: ORKESTRA ({heading}) must acknowledge Copilot on the bridge",
            )
        self.assertRegex(
            readme,
            r"workbench[^\n]{0,120}[Cc]opilot|[Cc]opilot[^\n]{0,80}workbench|"
            r"Claude Code/[Cc]ursor/[Cc]opilot|Claude Code,?\s*[Cc]ursor,?\s*(and |or )?[Cc]opilot",
            "FR-008: README workbench must include Copilot as a selectable engine",
        )
        self.assertNotRegex(
            readme_tr,
            r"Bridge Copilot'u desteklemez|Copilot engine bridge'de desteklenmiyor|"
            r"Copilot bridge kapsam d[ıi][şs][ıi]",
            "FR-008: README.tr must not deny Copilot on Telegram/bridge",
        )
        self.assertRegex(
            readme_tr,
            r"[Bb]ridge.*[Cc]opilot|[Cc]opilot.*[Bb]ridge|/yap[^\n]*copilot|"
            r'"engine":\s*"copilot"',
            "FR-008: README.tr must document Copilot bridge/Telegram usage",
        )

        # FR-009 / E10: CoC contact is GHSA / maintainer private report — no invented public email
        self.assertRegex(
            coc,
            r"GitHub Security Advisories|GHSA",
            "FR-009 / E10: CODE_OF_CONDUCT must point at GHSA / GitHub Security Advisories",
        )
        self.assertRegex(
            coc,
            r"private report|maintainers|repository maintainers",
            "FR-009: CODE_OF_CONDUCT must allow maintainer private report",
        )
        self.assertNotRegex(
            coc,
            r"mailto:|@[\w.-]+\.(com|org|net|io)\b",
            "FR-009 / E10: CODE_OF_CONDUCT must not invent a public contact email",
        )

    def test_install_windows_entrypoints(self):
        """FR-003 / E6: install.py + install.ps1 exist; Windows skips LaunchAgent."""
        self.assertTrue((ROOT / "install.py").is_file())
        self.assertTrue((ROOT / "install.ps1").is_file())
        py = (ROOT / "install.py").read_text(encoding="utf-8")
        self.assertIn('os.name == "nt"', py)
        self.assertRegex(py, r"LaunchAgent|launchd")
        self.assertIn("skip", py.casefold())
        sh = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertRegex(sh, r"[Ww]indows.*(install\.py|install\.ps1)")
        self.assertIn("Darwin", sh)
        self.assertIn("LaunchAgent", sh)

    def test_posix_only_modules_declare_nt_skip(self):
        """FR-007: listed POSIX fixture modules declare skipUnless(os.name != \"nt\")."""
        modules = [
            ROOT / "plugins" / "orkestra" / "tests" / "test_engines.py",
            ROOT / "plugins" / "orkestra" / "tests" / "test_engines2.py",
            ROOT / "plugins" / "orkestra" / "tests" / "test_flow.py",
            ROOT / "plugins" / "orkestra" / "tests" / "test_hardening.py",
            ROOT / "bridge" / "tests" / "test_bridge_engines.py",
        ]
        for path in modules:
            src = path.read_text(encoding="utf-8")
            self.assertIn("skipUnless", src, path.name)
            self.assertRegex(src, r"os\.name\s*!=\s*[\"']nt[\"']", path.name)

    def test_ci_includes_windows_latest(self):
        """FR-007 / SC-003: unit matrix includes windows-latest for plugin+bridge."""
        yml = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("windows-latest", yml)
        self.assertIn("ubuntu-latest", yml)
        self.assertIn("macos-latest", yml)
        self.assertIn("plugins/orkestra/tests", yml)
        self.assertIn("bridge/tests", yml)

    def test_windows_docs_honesty(self):
        """FR-008 / SC-004: Windows supported with honest limits; not WSL-only; no pip/fail-open flip."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_tr = (ROOT / "README.tr.md").read_text(encoding="utf-8")
        onboarding = (ROOT / "ONBOARDING.md").read_text(encoding="utf-8")
        self.assertNotIn("Windows is untested", readme)
        for label, text in (("README.md", readme), ("README.tr.md", readme_tr), ("ONBOARDING.md", onboarding)):
            self.assertRegex(text, r"[Ww]indows", f"{label} must mention Windows")
            self.assertNotRegex(text, r"WSL[- ]only|only WSL", f"{label} must not claim WSL-only")
            self.assertNotRegex(text, r"(?i)pip install\s+orkestra")
            self.assertRegex(text, r"LaunchAgent|launchd", f"{label}: LaunchAgent N/A on Windows")
            self.assertRegex(text, r"orkestra\.cmd|\.cmd", f"{label}: .cmd PATH shim")
            self.assertRegex(text, r"Developer Mode|Dev Mode|symlink", f"{label}: optional Dev Mode/symlink note")
            self.assertRegex(text, r"%USERPROFILE%|~/\.local/bin|ORKESTRA_SHIM_DIR", f"{label}: shim dir hint")
        self.assertRegex(readme, r"(?i)(standard library|nothing to `pip install`)")

if __name__ == "__main__":
    unittest.main()

