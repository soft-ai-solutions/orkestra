"""Copilot model / effort / context / allow-all: one resolver, argv for both builders, VS Code merge, CLI surface."""
import json
import os
import stat
import sys
import unittest
from pathlib import Path

import helpers
from orkestra_core import copilotkit as CK
from orkestra_core import engines as EN
from orkestra_core import i18n
from orkestra_core import paths

FULL = ["--allow-all-tools", "--allow-all-paths", "--allow-all-urls"]


def fake_cli(dirpath: Path, help_text: str) -> str:
    p = dirpath / "copilot"
    p.write_text("#!/bin/sh\ncat <<'EOF'\n" + help_text + "\nEOF\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


class TestResolver(unittest.TestCase):
    def test_defaults_are_the_documented_ones(self):
        o = EN.copilot_resolve({})
        self.assertEqual(EN.copilot_profile_of(o), {"model": "gpt-6-luna", "reasoning_effort": "max",
                                                    "context": "long_context", "allow_all": True})
        self.assertEqual(set(o["sources"].values()), {"default"})
        self.assertEqual(EN.COPILOT_DEFAULTS, {k: paths.DEFAULT_GLOBAL_CONFIG["engine"]["copilot"][k]
                                               for k in EN.COPILOT_DEFAULTS})

    def test_precedence_flag_request_run_config_default(self):
        cfg = {"engine": {"copilot": {"model": "m-config", "reasoning_effort": "low", "context": "default"}}}
        o = EN.copilot_resolve(cfg)
        self.assertEqual((o["model"], o["sources"]["model"]), ("m-config", "config"))
        sel = {"model": "m-run", "reasoning_effort": "medium"}
        o = EN.copilot_resolve(cfg, selected=sel)
        self.assertEqual((o["model"], o["sources"]["model"]), ("m-run", "run"))
        o = EN.copilot_resolve(cfg, "use gpt-6-astra, effort high, use long context, no allow all", selected=sel)
        self.assertEqual((o["model"], o["reasoning_effort"], o["context"], o["allow_all"]),
                         ("gpt-6-astra", "high", "long_context", False))
        self.assertEqual(set(o["sources"].values()), {"request"})
        o = EN.copilot_resolve(cfg, "use gpt-6-astra", {"model": "brand-new-9", "reasoning_effort": "xhigh"}, sel)
        self.assertEqual((o["model"], o["reasoning_effort"]), ("brand-new-9", "xhigh"))
        self.assertEqual((o["sources"]["model"], o["sources"]["context"]), ("flag", "config"))

    def test_request_text_never_raises_privileges_or_flips_context_by_accident(self):
        off = {"engine": {"copilot": {"allow_all": False, "context": "long_context"}}}
        for text in ("add CORS allow all origins", "with allow all origins enabled", "use allow all", "hepsine izin ver"):
            o = EN.copilot_resolve(off, text)
            self.assertIs(o["allow_all"], False, text)                     # words can only lower, never raise
            self.assertEqual(o["sources"]["allow_all"], "config")
        for text in ("change the default context menu", "use the default context menu", "update default context provider",
                     "varsayılan context menüsünü değiştir"):
            self.assertEqual(EN.copilot_resolve(off, text)["context"], "long_context", text)
        self.assertEqual(EN.copilot_resolve(off, "use default context")["context"], "default")
        self.assertEqual(EN.copilot_resolve(off, "varsayılan context kullan")["context"], "default")
        self.assertEqual(EN.copilot_resolve({}, "please use long context")["context"], "long_context")
        self.assertIs(EN.copilot_resolve({}, "run with no allow all")["allow_all"], False)
        self.assertEqual(EN.copilot_resolve({}, "use gpt-6-sol, effort high")["model"], "gpt-6-sol")

    def test_extra_overrides_config_and_false_is_kept(self):
        o = EN.copilot_resolve({"engine": {"copilot": {"allow_all": True}}}, extra={"allow_all": False})
        self.assertIs(o["allow_all"], False)
        self.assertEqual(o["sources"]["allow_all"], "config")

    def test_validation_rejects_garbage_in_both_languages(self):
        for lang, word in (("en", "Choose one of"), ("tr", "Şunlardan birini seç")):
            with _env_lang(lang):
                with self.assertRaises(EN.EngineError) as cm:
                    EN.copilot_resolve({}, flags={"reasoning_effort": "bogus"})
                self.assertIn(word, str(cm.exception))
                self.assertIn("xhigh", str(cm.exception))
                self.assertNotIn("—", str(cm.exception))
        for bad in ({"context": "huge"}, {"model": "a b;rm -rf"}, {"allow_all": "maybe"}):
            with self.assertRaises(EN.EngineError):
                EN.copilot_resolve({}, flags=bad)
        with self.assertRaises(EN.EngineError):
            EN.copilot_resolve({"engine": {"copilot": {"reasoning_effort": "ultra"}}})

    def test_model_is_free_text_and_auto_is_allowed(self):
        for m in ("gpt-9-quasar", "claude-opus-5.5", "auto", "AUTO"):
            self.assertEqual(EN.copilot_resolve({}, flags={"model": m})["model"], "auto" if m.lower() == "auto" else m)

    def test_request_text_picks_are_single_hit_wins(self):
        self.assertEqual(EN.pick_copilot_context("lütfen uzun context kullan"), "long_context")
        self.assertEqual(EN.pick_copilot_context("default context please"), "default")
        self.assertIsNone(EN.pick_copilot_context("use long context but use default context"))
        self.assertIsNone(EN.pick_copilot_context("fix the context menu"))
        self.assertIs(EN.pick_copilot_allow_all("with allow all"), True)
        self.assertIs(EN.pick_copilot_allow_all("hepsine izin ver"), True)
        self.assertIs(EN.pick_copilot_allow_all("no allow all"), False)
        self.assertIs(EN.pick_copilot_allow_all("hepsine izin verme"), False)
        self.assertIsNone(EN.pick_copilot_allow_all("with allow all, no allow all"))
        self.assertEqual(EN.pick_copilot_effort("reasoning effort xhigh"), "xhigh")
        self.assertEqual(EN.pick_copilot_model("use gpt-6-sol"), "gpt-6-sol")
        self.assertEqual(EN.pick_copilot_model("use gpt-6.1-sol"), "gpt-6.1-sol")
        self.assertIsNone(EN.pick_copilot_model("gpt-6-sol and gpt-6-luna"))


_env_lang = i18n.use_lang


class TestArgv(helpers.Sandbox):
    OPTS = {"model": "gpt-6-luna", "reasoning_effort": "max", "context": "long_context", "plugin_dir": "/p",
            "approval_mode": "autopilot"}

    def test_headless_and_interactive_carry_context_and_full_permission(self):
        for build in (lambda o: EN.headless_command("copilot", "/bin/copilot", "copilot-S", "x", True, o)["argv"],
                      lambda o: EN.interactive_command("copilot", "/bin/copilot", "x", o)):
            argv = build(self.OPTS)
            for flag in FULL:
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--context") + 1], "long_context")
            self.assertIn(EN.COPILOT_EXCLUDED_TOOLS, argv)                 # no second chat / subagent stays
            narrow = build(dict(self.OPTS, allow_all=False, context="default"))
            self.assertIn("--allow-all-tools", narrow)
            self.assertNotIn("--allow-all-paths", narrow)
            self.assertNotIn("--allow-all-urls", narrow)
            self.assertEqual(narrow[narrow.index("--context") + 1], "default")
            self.assertNotIn("--context", build({k: v for k, v in self.OPTS.items() if k != "context"}))

    def test_context_is_omitted_when_the_installed_cli_does_not_list_it(self):
        old = fake_cli(self.tmp, "Usage: copilot [options]\n  --model <m>\n  --reasoning-effort <l>")
        EN._cli_help.clear()
        argv = EN.headless_command("copilot", old, "copilot-S", "x", True, self.OPTS)["argv"]
        self.assertNotIn("--context", argv)
        self.assertIs(EN.copilot_cli_has(old, "--context"), False)
        (self.tmp / "copilot").write_text("#!/bin/sh\necho '  --context <c>'\n")
        EN._cli_help.clear()
        argv = EN.headless_command("copilot", old, "copilot-S", "x", True, self.OPTS)["argv"]
        self.assertIn("--context", argv)
        self.assertIsNone(EN.copilot_cli_has("/nonexistent/copilot", "--context"))     # unknown: assume yes
        EN._cli_help.clear()


class TestVscode(helpers.Sandbox):
    def test_merge_is_idempotent_keeps_foreign_keys_backs_up_and_removes(self):
        f = self.tmp / "ws" / ".vscode" / "settings.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"editor.tabSize": 2, "chat.pluginLocations": {"/other": True}}))
        plan = CK.vscode_settings(f, "/plug", "autopilot", False, True)
        self.assertTrue(plan)
        self.assertEqual(json.loads(f.read_text())["editor.tabSize"], 2)
        self.assertNotIn("chat.permissions.default", json.loads(f.read_text()))          # dry run wrote nothing
        done = CK.vscode_settings(f, "/plug", "autopilot", False, False)
        doc = json.loads(f.read_text())
        self.assertEqual(doc["chat.pluginLocations"], {"/other": True, "/plug": True})
        self.assertTrue(doc["chat.plugins.enabled"])
        self.assertEqual(doc["chat.permissions.default"], "autopilot")
        self.assertTrue(list(f.parent.glob("settings.json.orkestra-backup-*")))
        self.assertTrue(done)
        self.assertEqual(CK.vscode_settings(f, "/plug", "autopilot", False, False), [])    # idempotent
        CK.vscode_settings(f, "/plug", "autopilot", True, False)
        doc = json.loads(f.read_text())
        self.assertEqual(doc["chat.pluginLocations"], {"/other": True})
        self.assertNotIn("chat.permissions.default", doc)

    def test_default_model_and_configuration_are_written_and_removed_only_while_ours(self):
        f = self.tmp / "u" / "settings.json"
        cfg = CK.DEFAULT_CONFIGURATION
        CK.vscode_settings(f, "/plug", None, False, False, model="gpt-6-luna", default_config=cfg)
        doc = json.loads(f.read_text())
        self.assertEqual(doc["chat.defaultModel"], "gpt-6-luna")
        self.assertEqual(doc["chat.defaultConfiguration"], {"mode": "autopilot", "approvals": "allowAll"})
        self.assertNotIn("chat.permissions.default", doc)
        doc["chat.permissions.default"] = "autoApprove"
        f.write_text(json.dumps(doc))
        CK.vscode_settings(f, "/plug", None, True, False, model="gpt-6-luna", default_config=cfg)   # flag not repeated
        for k in ("chat.defaultModel", "chat.defaultConfiguration", "chat.permissions.default", "chat.pluginLocations"):
            self.assertNotIn(k, json.loads(f.read_text()), k)
        f.write_text(json.dumps({"chat.defaultModel": "mine", "chat.permissions.default": "default",
                                 "chat.defaultConfiguration": {"mode": "plan", "approvals": "manual"}}))
        CK.vscode_settings(f, "/plug", None, True, False, model="gpt-6-luna", default_config=cfg)   # the user's own stay
        self.assertEqual(json.loads(f.read_text())["chat.defaultModel"], "mine")
        self.assertEqual(json.loads(f.read_text())["chat.permissions.default"], "default")
        self.assertEqual(json.loads(f.read_text())["chat.defaultConfiguration"]["mode"], "plan")

    def test_workspace_merge_skips_the_machine_scoped_plugin_key(self):
        f = self.tmp / "w" / "settings.json"
        CK.vscode_settings(f, "/plug", None, False, False, model="gpt-6-luna", plugin_locations=False)
        doc = json.loads(f.read_text())
        self.assertEqual(doc, {"chat.defaultModel": "gpt-6-luna"})

    def test_no_permissions_key_unless_asked_and_missing_file_is_created(self):
        f = self.tmp / "new" / "settings.json"
        CK.vscode_settings(f, "/plug", None, False, False)
        self.assertNotIn("chat.permissions.default", json.loads(f.read_text()))
        self.assertNotIn("chat.tools.global.autoApprove", json.loads(f.read_text()))

    def test_commented_or_broken_settings_are_refused_untouched(self):
        f = self.tmp / "settings.json"
        text = '{\n  // my comment\n  "a": 1,\n}\n'
        f.write_text(text)
        for lang in ("en", "tr"):
            with _env_lang(lang):
                with self.assertRaises(EN.EngineError):
                    CK.vscode_settings(f, "/plug", None, False, False)
        self.assertEqual(f.read_text(), text)

    def test_agent_file_pins_model_and_effort_and_never_overwrites_a_foreign_file(self):
        ws = self.tmp / "ws"
        self.assertTrue(CK.vscode_agent(str(ws), "gpt-6-luna", "max", False, False))
        agent = ws / ".github" / "agents" / "orkestra.agent.md"
        text = agent.read_text()
        self.assertIn('model: ["gpt-6-luna"]', text)
        self.assertIn("reasoning-effort: max", text)
        self.assertEqual(CK.vscode_agent(str(ws), "gpt-6-luna", "max", False, False), [])
        with self.assertRaises(EN.EngineError):
            CK.vscode_agent(str(ws), "gpt-6-luna", "ultra", False, False)
        CK.vscode_agent(str(ws), "", None, True, False)
        self.assertFalse(agent.exists())
        agent.write_text("mine\n")
        with self.assertRaises(EN.EngineError):
            CK.vscode_agent(str(ws), "gpt-6-luna", None, False, False)
        self.assertEqual(agent.read_text(), "mine\n")


class TestCli(helpers.Sandbox):
    def setUp(self):
        super().setUp()
        self.cophome = self.tmp / "copilot-home"
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        fake_cli(self.bindir, "  --context <c>\n  --allow-all")
        self.env = {"COPILOT_HOME": str(self.cophome), "HOME": str(self.tmp / "h"),
                    "PATH": str(self.bindir) + os.pathsep + os.environ.get("PATH", "")}
        self.proj = self.make_project("demo")
        self.cli("init", "-p", "demo", env=self.env)
        self.assertEqual(self.cli("copilot", "install", env=self.env).returncode, 0)

    def run_print(self, *extra, lang="en"):
        e = dict(self.env, ORKESTRA_LANG=lang)
        return self.cli("run", "--engine", "copilot", "--print", "-p", "demo", *extra, "add a feature", env=e, check=False)

    def test_run_print_defaults_flags_and_validation(self):
        cp = self.run_print()
        self.assertEqual(cp.returncode, 0, cp.stderr)
        for token in ("--model gpt-6-luna", "--reasoning-effort max", "--context long_context", "--allow-all-paths",
                      "--allow-all-urls"):
            self.assertIn(token, cp.stdout)
        cp = self.run_print("--model", "gpt-9-x", "--effort", "xhigh", "--context", "default", "--no-allow-all")
        self.assertIn("--model gpt-9-x", cp.stdout)
        self.assertIn("--reasoning-effort xhigh", cp.stdout)
        self.assertIn("--context default", cp.stdout)
        self.assertNotIn("--allow-all-paths", cp.stdout)
        bad = self.run_print("--effort", "bogus")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("xhigh", bad.stderr + bad.stdout)
        tr = self.run_print("--context", "huge", lang="tr")
        self.assertIn("geçerli değil", tr.stderr + tr.stdout)

    def test_run_stores_the_profile_so_a_resume_reuses_it(self):
        cp = self.cli("run", "--engine", "copilot", "-p", "demo", "--model", "gpt-9-x", "--effort", "xhigh",
                      "--no-allow-all", "add a feature", env=dict(self.env, ORKESTRA_LANG="en"), check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        states = list((self.proj / ".orkestra").rglob("state.json"))
        self.assertEqual(len(states), 1)
        stored = json.loads(states[0].read_text())["copilot_profile"]
        self.assertEqual(stored, {"model": "gpt-9-x", "reasoning_effort": "xhigh", "context": "long_context",
                                  "allow_all": False})
        o = EN.copilot_resolve({}, selected=stored)                  # what watchdog.py does on a resume
        self.assertEqual(EN.copilot_profile_of(o), stored)

    def test_flags_on_another_engine_are_refused(self):
        cp = self.cli("run", "--engine", "claude", "--print", "-p", "demo", "--model", "x", "add a feature",
                      env=dict(self.env, ORKESTRA_LANG="en"), check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("--engine copilot", cp.stderr + cp.stdout)

    def test_status_shows_profile_with_sources(self):
        cp = self.cli("copilot", "status", env=dict(self.env, ORKESTRA_LANG="en"), check=False)
        self.assertIn("gpt-6-luna (default)", cp.stdout)
        self.assertIn("allow-all on (default)", cp.stdout)
        cfgp = self.home / "config.json"
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        cfgp.write_text(json.dumps({"engine": {"copilot": {"model": "gpt-6-sol"}}}))
        cp = self.cli("copilot", "status", env=dict(self.env, ORKESTRA_LANG="en"), check=False)
        self.assertIn("gpt-6-sol (config)", cp.stdout)
        self.assertIn("effort max (default)", cp.stdout)        # a partial user config keeps the other defaults

    def test_vscode_command_is_plan_only_until_apply_and_names_its_limits(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        e = dict(self.env, ORKESTRA_LANG="en")
        cp = self.cli("copilot", "vscode", "--workspace", str(ws), env=e, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse((ws / ".vscode").exists())
        self.assertIn("picker", cp.stdout)
        self.assertNotIn("—", cp.stdout)
        cp = self.cli("copilot", "vscode", "--apply", env=e, check=False)
        self.assertNotEqual(cp.returncode, 0)                    # neither --workspace nor --user: refuse
        cp = self.cli("copilot", "vscode", "--apply", "--workspace", str(ws), "--permissions", "autopilot",
                      "--model", "gpt-6-luna", "--effort", "max", env=e, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(json.loads((ws / ".vscode" / "settings.json").read_text())["chat.permissions.default"], "autopilot")
        self.assertTrue((ws / ".github" / "agents" / "orkestra.agent.md").exists())
        self.assertIn("Warning", cp.stdout)
        doc = json.loads((ws / ".vscode" / "settings.json").read_text())
        self.assertEqual(doc["chat.defaultModel"], "gpt-6-luna")
        self.assertEqual(doc["chat.defaultConfiguration"], {"mode": "autopilot", "approvals": "allowAll"})
        self.assertNotIn("chat.pluginLocations", doc)                  # machine-scoped: ignored in a workspace file
        self.assertIn("--user", cp.stdout)
        self.assertIn("NOT registered", cp.stdout)
        tr = self.cli("copilot", "vscode", "--workspace", str(ws), env=dict(self.env, ORKESTRA_LANG="tr"), check=False)
        self.assertIn("seçici", tr.stdout)


class TestVscodeUser(helpers.Sandbox):
    def setUp(self):
        super().setUp()
        self.uhome = self.tmp / "uhome"
        self.uhome.mkdir()
        self.env_en = dict(ORKESTRA_LANG="en", HOME=str(self.uhome), USERPROFILE=str(self.uhome),
                           APPDATA=str(self.uhome), XDG_CONFIG_HOME=str(self.uhome / "xdg"))

    def settings(self) -> Path:
        return next(self.uhome.rglob("settings.json"))

    def test_user_scope_registers_plugin_and_remove_works_without_repeating_flags(self):
        cp = self.cli("copilot", "vscode", "--apply", "--user", "--permissions", "autoApprove", env=self.env_en, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        doc = json.loads(self.settings().read_text())
        self.assertTrue(doc["chat.pluginLocations"])
        self.assertEqual(doc["chat.defaultModel"], "gpt-6-luna")
        self.assertEqual(doc["chat.permissions.default"], "autoApprove")
        self.assertNotIn("NOT registered", cp.stdout)
        cp = self.cli("copilot", "vscode", "--apply", "--user", "--remove", env=self.env_en, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(json.loads(self.settings().read_text()), {"chat.plugins.enabled": True})

    def test_effort_without_model_is_refused_in_both_languages(self):
        for lang, word in (("en", "--effort only pins"), ("tr", "--effort yalnız")):
            cp = self.cli("copilot", "vscode", "--effort", "max", env=dict(self.env_en, ORKESTRA_LANG=lang), check=False)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn(word, cp.stdout + cp.stderr)

    def test_no_allow_all_in_config_skips_the_default_configuration(self):
        cfgp = self.home / "config.json"
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        cfgp.write_text(json.dumps({"engine": {"copilot": {"allow_all": False, "model": "gpt-6-sol"}}}))
        cp = self.cli("copilot", "vscode", "--apply", "--user", env=self.env_en, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        doc = json.loads(self.settings().read_text())
        self.assertEqual(doc["chat.defaultModel"], "gpt-6-sol")
        self.assertNotIn("chat.defaultConfiguration", doc)


class TestWatchdogReuse(unittest.TestCase):
    def test_resume_keeps_what_the_run_started_with(self):
        cfg = {"engine": {"copilot": {"model": "gpt-6-luna"}}}
        stored = {"model": "gpt-6-sol", "reasoning_effort": "xhigh", "context": "default", "allow_all": False}
        o = EN.copilot_resolve(cfg, selected=stored)
        self.assertEqual(EN.copilot_profile_of(o), stored)
        argv = EN.headless_command("copilot", "/nonexistent/copilot", "copilot-S", "x", False, o)["argv"]
        self.assertIn("gpt-6-sol", argv)
        self.assertNotIn("--allow-all-paths", argv)


if __name__ == "__main__":
    unittest.main()
