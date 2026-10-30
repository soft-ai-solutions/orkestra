"""CLI UX: `--lang`, `orkestra lang`, `--reason`, localized `--help`, `--version`, the doctor language row.

The suite is pinned to Turkish (helpers.py); every test here that cares about the language sets it explicitly, and
checks English output for Turkish letters. Nothing here writes outside the Sandbox directories."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import __version__, cli as CLI, cliparse as CP, i18n
from orkestra_core.messages import help as H

TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
EM_DASH = chr(0x2014)
# an environment with no language hint at all (empty values are "unset" for i18n on every platform)
NEUTRAL = {"ORKESTRA_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "", "LANGUAGE": "", "NO_COLOR": "1",
           "PYTHON_COLORS": "0"}


def children(parser):
    """name -> subcommand parser (argparse keeps them on the _SubParsersAction)."""
    for act in parser._actions:
        if act.__class__.__name__ == "_SubParsersAction":
            return dict(act.choices)
    return {}


def squash(text):
    return " ".join(text.split())


class Base(helpers.Sandbox):
    def run_cli(self, *args, **env):
        """The CLI in a language-neutral environment; `env` adds to it."""
        merged = dict(NEUTRAL)
        merged.update(env)
        return self.cli(*args, check=False, env=merged)

    def write_config(self, text):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")

    def config(self):
        return json.loads((self.home / "config.json").read_text(encoding="utf-8"))

    def fields(self, stdout):
        """`key: value` lines of `orkestra lang show` as a dict."""
        out = {}
        for line in stdout.splitlines():
            key, sep, value = line.partition(": ")
            if sep and key in ("language", "source"):
                out[key] = value
        return out


# ====================================================================== --lang (the global flag)

class TestLangFlag(Base):
    def shown(self, *args, **env):
        cp = self.run_cli(*args, **env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return self.fields(cp.stdout)

    def test_flag_works_before_the_subcommand(self):
        got = self.shown("--lang", "tr", "lang", "show")
        self.assertEqual(got, {"language": "tr", "source": "env (--lang)"})
        self.assertEqual(self.shown("--lang", "en", "lang", "show")["language"], "en")

    def test_flag_works_after_the_subcommand_and_with_equals(self):
        for argv in (["lang", "show", "--lang", "en"], ["lang", "--lang", "en"], ["lang", "show", "--lang=en"],
                     ["--lang=en", "lang", "show"]):
            self.assertEqual(self.shown(*argv, ORKESTRA_LANG="tr"), {"language": "en", "source": "env (--lang)"}, argv)

    def test_flag_beats_the_environment_and_the_config_file(self):
        self.write_config({"lang": "tr"})
        self.assertEqual(self.shown("--lang", "en", "lang", "show", ORKESTRA_LANG="tr")["language"], "en")
        self.assertEqual(self.shown("--lang", "tr", "lang", "show", ORKESTRA_LANG="en")["language"], "tr")

    def test_flag_auto_hands_the_decision_back_to_config_and_locale(self):
        self.write_config({"lang": "tr"})
        got = self.shown("--lang", "auto", "lang", "show", ORKESTRA_LANG="en")
        self.assertEqual(got["language"], "tr")
        self.assertTrue(got["source"].startswith("config ("), got)
        self.assertEqual(self.shown("--lang", "auto", "lang", "show", ORKESTRA_LANG="en", LANG="tr_TR.UTF-8")["source"],
                         "config (%s)" % (self.home / "config.json"))
        (self.home / "config.json").unlink()
        self.assertEqual(self.shown("--lang", "auto", "lang", "show", ORKESTRA_LANG="en", LANG="tr_TR.UTF-8"),
                         {"language": "tr", "source": "locale"})

    def test_spellings_of_a_language_are_accepted(self):
        for raw in ("TR", "tr_TR.UTF-8", "tr-TR", "Turkish", " tr ", "EN", "en_US.UTF-8", "English"):
            want = "tr" if raw.strip().lower().startswith(("tr", "turk")) else "en"
            self.assertEqual(self.shown("--lang", raw, "lang", "show")["language"], want, raw)

    def test_invalid_values_are_a_usage_error(self):
        for argv in (["--lang", "de", "lang", "show"], ["lang", "show", "--lang", "klingon"], ["--lang", "", "lang"],
                     ["--lang=fr", "doctor"], ["--lang"], ["lang", "--lang"], ["--lang", "C", "lang"]):
            cp = self.run_cli(*argv)
            self.assertEqual(cp.returncode, 2, (argv, cp.stdout, cp.stderr))
            self.assertEqual(cp.stdout, "", argv)                                                # errors go to stderr
            self.assertNotIn("Traceback", cp.stderr, argv)
            self.assertRegex(cp.stderr, r"--lang", argv)
        cp = self.run_cli("--lang", "de", "lang")
        self.assertIn("unsupported language 'de'", cp.stderr)
        self.assertIn("en, tr or auto", cp.stderr)

    def test_flag_is_the_environment_of_children_and_is_undone_afterwards(self):
        seen = {}

        def fake(a):
            seen["env"] = os.environ.get("ORKESTRA_LANG")
            cp = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get('ORKESTRA_LANG'))"],
                                capture_output=True, text=True)
            seen["child"] = cp.stdout.strip()
            return 0

        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "tr"}), mock.patch.dict(CLI.HANDLERS, {"lang": fake}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(CLI.main(["--lang", "en", "lang"]), 0)
            self.assertEqual(os.environ["ORKESTRA_LANG"], "tr")                 # the caller's value is back
        self.assertEqual(seen, {"env": "en", "child": "en"})
        with mock.patch.dict(os.environ), mock.patch.dict(CLI.HANDLERS, {"lang": fake}):
            os.environ.pop("ORKESTRA_LANG", None)
            with contextlib.redirect_stdout(io.StringIO()):
                CLI.main(["lang", "--lang", "tr"])
            self.assertNotIn("ORKESTRA_LANG", os.environ)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                CLI.main(["--lang", "tr", "nope"])                               # an error path restores it too
            self.assertNotIn("ORKESTRA_LANG", os.environ)
        self.assertIsNone(CLI._LANG_FLAG)

    def test_flag_localizes_the_help_it_prints(self):
        en = self.run_cli("--lang", "en", "--help", ORKESTRA_LANG="tr")
        tr = self.run_cli("--help", "--lang", "tr", ORKESTRA_LANG="en")
        self.assertEqual((en.returncode, tr.returncode), (0, 0))
        self.assertIn("usage:", en.stdout)
        self.assertNotRegex(en.stdout, TR_LETTERS)
        self.assertIn("kullanım:", tr.stdout)
        self.assertIn("örnekler:", tr.stdout)

    def test_hoisting_and_guard_view_agree(self):
        self.assertEqual(CP.hoist_globals(["watch", "--lang", "tr", "--once"]), ["--lang", "tr", "watch", "--once"])
        self.assertEqual(CP.hoist_globals(["watch", "--lang=tr", "-p", "x"]), ["--lang=tr", "-p", "x", "watch"])
        self.assertEqual(CP.requested_lang(["--lang", "TR", "watch"]), "tr")
        self.assertEqual(CP.requested_lang(["watch", "--lang=en", "--lang", "tr"]), "tr")      # the last one wins
        self.assertEqual(CP.requested_lang(["lang", "tr"]), None)
        self.assertEqual(CP.requested_lang(["--lang", "de", "lang"]), None)
        self.assertEqual(CP.requested_lang(["--lang"]), None)
        # a --lang inside the command of tdd/evidence belongs to that command
        self.assertEqual(CP.requested_lang(["tdd", "red", "T001", "npm", "test", "--lang", "tr"]), None)
        self.assertEqual(CP.requested_lang(["evidence", "x", "cmd", "--lang", "tr"]), None)
        # ... and the guard sees the subcommand the CLI will run, wherever the flag stands
        self.assertEqual(CP.inspect(["--lang", "tr", "rollback", "--yes"])[:2], ("rollback", []))
        self.assertEqual(CP.inspect(["rollback", "--yes", "--lang", "tr"])[:2], ("rollback", []))
        self.assertEqual(CP.inspect(["tdd", "--lang", "tr", "red", "T001", "--", "npm", "test"])[:2],
                         ("tdd", ["npm", "test"]))
        self.assertEqual(CP.inspect(["tdd", "red", "T001", "--lang", "tr", "--", "npm"])[:2],
                         ("tdd", ["--lang", "tr", "npm"]))               # past the task id it is the command's, for both
        key, cmd, err = CP.inspect(["--lang", "tr", "--", "task", "start", "T001"])
        self.assertEqual((key, cmd), ("", []))
        self.assertTrue(err)
        self.assertTrue(CP.inspect(["--lang", "de", "rollback", "--yes"])[2])                   # refused, nothing runs


# ====================================================================== orkestra lang

class TestLangShow(Base):
    def shown(self, *args, **env):
        cp = self.run_cli(*args, **env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return self.fields(cp.stdout), cp.stdout

    def test_default_when_nothing_is_set(self):
        got, out = self.shown("lang")
        self.assertEqual(got, {"language": "en", "source": "default"})
        self.assertIn("orkestra lang en | tr | auto", out)
        self.assertEqual(self.shown("lang", "show")[0], got)                                      # `show` is the default
        self.assertFalse(self.home.exists(), "showing the language must not create the home directory")

    def test_every_source_is_named(self):
        self.assertEqual(self.shown("lang", ORKESTRA_LANG="tr")[0], {"language": "tr", "source": "env (ORKESTRA_LANG)"})
        self.assertEqual(self.shown("lang", LANG="tr_TR.UTF-8")[0], {"language": "tr", "source": "locale"})
        self.assertEqual(self.shown("lang", LANGUAGE="de:tr:en")[0], {"language": "tr", "source": "locale"})
        self.write_config({"lang": "tr"})
        got = self.shown("lang", LANG="en_US.UTF-8")[0]
        self.assertEqual(got, {"language": "tr", "source": "config (%s)" % (self.home / "config.json")})

    def test_precedence_env_then_config_then_locale(self):
        self.write_config({"lang": "tr"})
        self.assertEqual(self.shown("lang", ORKESTRA_LANG="en")[0]["language"], "en")
        self.assertEqual(self.shown("lang", LANG="en_US.UTF-8")[0]["language"], "tr")
        self.write_config({"lang": "auto"})
        self.assertEqual(self.shown("lang", LANG="tr_TR.UTF-8")[0], {"language": "tr", "source": "locale"})

    def test_unsupported_env_value_means_english_and_says_env(self):
        self.assertEqual(self.shown("lang", ORKESTRA_LANG="de", LANG="tr_TR.UTF-8")[0],
                         {"language": "en", "source": "env (ORKESTRA_LANG)"})

    def test_a_broken_config_is_ignored_not_fatal(self):
        for bad in ("{nope", "[]", '"tr"', json.dumps({"lang": 5}), ""):
            self.write_config(bad)
            self.assertEqual(self.shown("lang", LANG="tr_TR.UTF-8")[0], {"language": "tr", "source": "locale"}, bad)

    def test_the_hint_line_follows_the_language(self):
        self.assertNotRegex(self.shown("lang", ORKESTRA_LANG="en")[1], TR_LETTERS)
        self.assertIn("değiştirmek için", self.shown("lang", ORKESTRA_LANG="tr")[1])

    def test_lang_source_agrees_with_lang(self):
        cases = [({}, None), ({"LANG": "tr_TR.UTF-8"}, None), ({"ORKESTRA_LANG": "en", "LANG": "tr_TR"}, None),
                 ({"ORKESTRA_LANG": "de", "LANG": "tr_TR"}, None), ({"ORKESTRA_LANG": "auto", "LC_ALL": "tr"}, None),
                 ({"LANGUAGE": "fr:tr"}, None), ({"LANG": "tr_TR.UTF-8"}, {"lang": "en"}), ({}, {"lang": "tr"}),
                 ({}, {"lang": "auto"}), ({"LANG": "en_US"}, {"lang": "klingon"}), ({}, "{broken")]
        want_source = ["default", "locale", "env", "env", "locale", "locale", "config", "config", "default", "locale",
                       "default"]
        with mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(self.home)}):
            for (env, config), source in zip(cases, want_source):
                cfg = self.home / "config.json"
                if config is None:
                    if cfg.exists():
                        cfg.unlink()
                else:
                    self.write_config(config)
                self.assertEqual(i18n.lang_source(env), (i18n.lang(env), source), (env, config))


class TestLangSave(Base):
    def test_saves_atomically_into_a_new_home(self):
        cp = self.run_cli("lang", "tr")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(self.config(), {"lang": "tr"})
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.json"], "no temp files left behind")
        self.assertIn("dil kaydedildi: tr", cp.stdout)                                          # said in the new language
        cp = self.run_cli("lang", "en")
        self.assertEqual(self.config(), {"lang": "en"})
        self.assertIn("language saved: en", cp.stdout)
        self.assertNotRegex(cp.stdout, TR_LETTERS)

    def test_the_saved_language_is_what_the_next_command_uses(self):
        self.run_cli("lang", "tr")
        self.assertEqual(self.fields(self.run_cli("lang", "show").stdout),
                         {"language": "tr", "source": "config (%s)" % (self.home / "config.json")})
        self.run_cli("lang", "en")
        self.assertEqual(self.fields(self.run_cli("lang", "show", LANG="tr_TR.UTF-8").stdout)["language"], "en")
        out = self.run_cli("lang", "auto", LANG="tr_TR.UTF-8").stdout
        self.assertEqual(self.config(), {"lang": "auto"})
        self.assertIn("auto", out)
        self.assertEqual(self.fields(self.run_cli("lang", "show", LANG="tr_TR.UTF-8").stdout),
                         {"language": "tr", "source": "locale"})

    def test_every_other_key_survives_in_order(self):
        original = {"workspace_root": "~/Projects", "guards": {"git": False, "secrets": True}, "note": "Türkçe değer",
                    "lang": "auto", "engine": {"default": "cursor"}, "list": [1, "ş", None], "n": 3}
        self.write_config(original)
        self.assertEqual(self.run_cli("lang", "tr").returncode, 0)
        got = self.config()
        self.assertEqual(got, dict(original, lang="tr"))
        self.assertEqual(list(got), list(original), "key order is the user's")
        self.assertIn("Türkçe değer", (self.home / "config.json").read_text(encoding="utf-8"))   # not \u-escaped
        original = {"a": 1}
        self.write_config(original)
        self.run_cli("lang", "en")
        self.assertEqual(self.config(), {"a": 1, "lang": "en"})

    def test_spellings_are_saved_as_the_code(self):
        for raw, want in (("TR", "tr"), ("Turkish", "tr"), ("tr_TR.UTF-8", "tr"), ("English", "en"), ("AUTO", "auto")):
            self.assertEqual(self.run_cli("lang", raw).returncode, 0, raw)
            self.assertEqual(self.config()["lang"], want, raw)

    def test_invalid_values_change_nothing(self):
        self.write_config({"lang": "tr", "keep": True})
        before = (self.home / "config.json").read_bytes()
        for bad in ("de", "klingon", "", "none", "tr en", "show me"):
            # the saved language here is Turkish, so the refusal is Turkish too (it used to be English whatever the
            # language): this loop asks for English explicitly, the next one checks the saved Turkish
            cp = self.run_cli("lang", bad, ORKESTRA_LANG="en")
            self.assertEqual(cp.returncode, 2, (bad, cp.stdout, cp.stderr))
            self.assertIn("unsupported value", cp.stderr)
            self.assertIn("show, en, tr or auto", cp.stderr)
            self.assertEqual((self.home / "config.json").read_bytes(), before, bad)
            cp = self.run_cli("lang", bad)
            self.assertEqual(cp.returncode, 2, (bad, cp.stdout, cp.stderr))
            self.assertIn("desteklenmeyen değer", cp.stderr)
            self.assertIn("show, en, tr veya auto kullanın", cp.stderr)
            self.assertEqual((self.home / "config.json").read_bytes(), before, bad)
        cp = self.run_cli("lang", "tr", "en")                                                    # two values
        self.assertEqual(cp.returncode, 2)

    def test_a_corrupt_config_is_kept_aside_then_replaced(self):
        for raw in (b"{broken", b"[1, 2]", b'"tr"', b"\xff\xfe\x00junk", b"null"):
            shutil.rmtree(self.home, ignore_errors=True)
            self.home.mkdir()
            (self.home / "config.json").write_bytes(raw)
            cp = self.run_cli("lang", "en")
            self.assertEqual(cp.returncode, 0, (raw, cp.stderr))
            self.assertEqual(self.config(), {"lang": "en"}, raw)
            backups = sorted(self.home.glob("config.json.corrupt-*"))
            self.assertEqual([b.read_bytes() for b in backups], [raw], raw)
            self.assertIn(str(backups[0]), cp.stdout)
            self.assertNotRegex(cp.stdout, TR_LETTERS)

    def test_an_empty_config_needs_no_backup_and_a_bom_is_not_corruption(self):
        self.write_config("  \n")
        self.assertEqual(self.run_cli("lang", "tr").returncode, 0)
        self.assertEqual(self.config(), {"lang": "tr"})
        self.assertEqual(list(self.home.glob("config.json.corrupt-*")), [])
        (self.home / "config.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"keep": 1}).encode("utf-8"))
        self.assertEqual(self.run_cli("lang", "en").returncode, 0)
        self.assertEqual(self.config(), {"keep": 1, "lang": "en"})
        self.assertEqual(list(self.home.glob("config.json.corrupt-*")), [])
        self.assertEqual((self.home / "config.json").read_bytes()[:1], b"{")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_backup_of_a_private_config_is_private_too(self):
        """config.json holds notify tokens and webhook URLs and is written 0600; its copy used to be 0644."""
        self.home.mkdir(parents=True, exist_ok=True)
        cfg = self.home / "config.json"
        cfg.write_text('{"notify": {"token": "s3cr3t-token"},}', encoding="utf-8")            # a trailing comma: corrupt
        os.chmod(cfg, 0o600)
        self.assertEqual(self.run_cli("lang", "tr").returncode, 0)
        backup, = self.home.glob("config.json.corrupt-*")
        self.assertIn("s3cr3t-token", backup.read_text(encoding="utf-8"))
        self.assertEqual(oct(backup.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(cfg.stat().st_mode & 0o777), oct(0o600))

    def test_two_corrupt_files_never_overwrite_each_others_backup(self):
        self.write_config("{one")
        self.run_cli("lang", "en")
        self.write_config("{two")
        self.run_cli("lang", "tr")
        self.assertEqual(sorted(b.read_text() for b in self.home.glob("config.json.corrupt-*")), ["{one", "{two"])

    def test_the_environment_overriding_the_saved_value_is_said(self):
        cp = self.run_cli("lang", "tr", ORKESTRA_LANG="en")
        self.assertEqual(self.config(), {"lang": "tr"})
        self.assertIn("ORKESTRA_LANG", cp.stdout)
        self.assertIn("does not apply", cp.stdout)
        cp = self.run_cli("--lang", "en", "lang", "tr")
        self.assertIn("--lang", cp.stdout)
        cp = self.run_cli("lang", "en", ORKESTRA_LANG="en")
        self.assertNotIn("note:", cp.stdout)                                                     # nothing to warn about

    def test_unreadable_or_unwritable_homes_are_a_clean_error(self):
        self.home.mkdir()
        (self.home / "config.json").mkdir()                                                      # a directory, not a file
        cp = self.run_cli("lang", "tr")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("cannot read", cp.stderr)
        self.assertNotIn("Traceback", cp.stderr)
        blocker = self.tmp / "afile"
        blocker.write_text("x")
        cp = self.run_cli("lang", "tr", ORKESTRA_HOME=str(blocker))                              # home is not a directory
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertRegex(cp.stderr, r"cannot (read|write)")
        self.assertNotIn("Traceback", cp.stderr)

    def test_a_failed_write_leaves_the_old_file_and_no_temp_files(self):
        self.write_config({"lang": "en", "keep": 1})
        before = (self.home / "config.json").read_bytes()
        with mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(self.home), "ORKESTRA_LANG": "en"}), \
                mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(CLI.OrkestraError) as ctx:
                CLI._save_lang("tr")
        self.assertIn("cannot write", str(ctx.exception))
        self.assertEqual((self.home / "config.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.json"])

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks on this platform")
    def test_a_symlinked_config_stays_a_symlink(self):
        real = self.tmp / "dotfiles" / "orkestra.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"keep": 1}), encoding="utf-8")
        self.home.mkdir()
        try:
            os.symlink(str(real), str(self.home / "config.json"))
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create symlinks")
        self.assertEqual(self.run_cli("lang", "tr").returncode, 0)
        self.assertTrue((self.home / "config.json").is_symlink())
        self.assertEqual(json.loads(real.read_text(encoding="utf-8")), {"keep": 1, "lang": "tr"})


class TestSetupLang(Base):
    """`orkestra setup --lang X`: the flag is global (parsed anywhere) and setup keeps the choice once it succeeded.
    The setup handler itself is stubbed: the real one installs into Claude/Cursor/Copilot."""

    def run_main(self, argv, rc=0):
        out = io.StringIO()
        with mock.patch.dict(CLI.HANDLERS, {"setup": lambda a: rc}), contextlib.redirect_stdout(out):
            code = CLI.main(argv)
        return code, out.getvalue()

    def test_setup_parses_the_flag_in_any_position(self):
        parser = CP.build_parser()
        for argv in (["setup", "--yes", "--lang", "tr"], ["setup", "--lang", "tr", "--yes"], ["--lang", "tr", "setup", "-y"],
                     ["setup", "--lang=tr", "--yes"]):
            ns = CP.parse(parser, argv)
            self.assertEqual((ns.cmd, ns.lang, ns.yes), ("setup", "tr", True), argv)
        self.assertIsNone(CP.parse(parser, ["setup", "--yes"]).lang)

    def test_a_successful_setup_keeps_the_language(self):
        code, out = self.run_main(["setup", "--yes", "--lang", "tr"])
        self.assertEqual(code, 0)
        self.assertEqual(self.config(), {"lang": "tr"})
        self.assertIn("dil kaydedildi: tr", out)

    def test_a_failed_or_flagless_setup_changes_nothing(self):
        code, _ = self.run_main(["setup", "--yes", "--lang", "tr"], rc=1)
        self.assertEqual(code, 1)
        code, _ = self.run_main(["setup", "--yes"])
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "config.json").exists())

    def test_other_commands_never_save_the_flag(self):
        code, _ = self.run_main(["lang", "show", "--lang", "tr"])
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "config.json").exists())


# ====================================================================== --reason

class TestReasonParsing(Base):
    """abort, block, pause (and await, unblock): the reason as words OR with --reason, never both."""

    NAMES = ("await", "block", "unblock", "pause", "abort")

    def call(self, cmd, words=None, flag=None):
        ns = argparse.Namespace(project="p", run=None, cmd=cmd, reason=list(words or []), reason_opt=flag)
        with mock.patch.object(CLI, "_resolve_project", return_value=self.tmp), \
                mock.patch.object(CLI, "_run_id", return_value="001-x"), \
                mock.patch.object(CLI.R, "set_status") as set_status, \
                mock.patch.object(CLI, "_unbind_here"), mock.patch.object(CLI, "unbind_marker", return_value="[marker]"), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = CLI.cmd_status_change(ns)
        return rc, set_status

    def test_both_forms_parse(self):
        parser = CP.build_parser()
        for name in self.NAMES:
            words = CP.parse(parser, [name, "waiting", "for", "keys"])
            self.assertEqual((words.reason, words.reason_opt), (["waiting", "for", "keys"], None), name)
            flagged = CP.parse(parser, [name, "--reason", "waiting for keys"])
            self.assertEqual((flagged.reason, flagged.reason_opt), ([], "waiting for keys"), name)
            self.assertEqual(CP.parse(parser, ["-p", "demo", name, "--reason=x y"]).reason_opt, "x y", name)
            self.assertEqual(CP.parse(parser, [name, "--reason", "x", "-p", "demo"]).project, "demo", name)
            bare = CP.parse(parser, [name])
            self.assertEqual((bare.reason, bare.reason_opt), ([], None), name)

    def test_guards_see_the_same_subcommand_for_both_forms(self):
        for name in self.NAMES:
            self.assertEqual(CP.inspect([name, "--reason", "x"])[:2], (name, []))
            self.assertEqual(CP.inspect([name, "x", "y"])[:2], (name, []))

    def test_option_abbreviations_stay_refused(self):
        self.assertTrue(CP.inspect(["block", "--rea", "x"])[2])
        self.assertTrue(CP.inspect(["abort", "--reas", "x"])[2])

    def test_flag_and_words_reach_the_run_as_the_same_reason(self):
        for name in ("block", "abort", "await"):
            _, via_words = self.call(name, words=["dns", "is", "down"])
            _, via_flag = self.call(name, flag="dns is down")
            self.assertEqual(via_words.call_args, via_flag.call_args, name)
            self.assertEqual(via_flag.call_args[0][3], "dns is down", name)
        _, ss = self.call("pause", flag="lunch")
        self.assertEqual(ss.call_args[0][2:4], ("paused", "lunch"))
        _, ss = self.call("pause")                                                               # pause needs no reason
        self.assertEqual(ss.call_args[0][2:4], ("paused", ""))
        _, ss = self.call("unblock")
        self.assertEqual(ss.call_args[0][2:4], ("active", ""))

    def test_both_at_once_is_an_error_and_changes_nothing(self):
        for name in self.NAMES:
            with self.assertRaises(CLI.OrkestraError) as ctx:
                self.call(name, words=["a"], flag="b")
            self.assertIn("not both", str(ctx.exception))
            self.assertIn("--reason", str(ctx.exception))
        with mock.patch.object(CLI.R, "set_status") as set_status:
            with self.assertRaises(CLI.OrkestraError):
                self.call("abort", words=["a"], flag="b")
        set_status.assert_not_called()

    def test_the_reason_is_still_required_where_it_was(self):
        for name in ("await", "block", "abort"):
            for words, flag in (([], None), ([], ""), ([], "   "), (["  "], None)):
                with self.assertRaises(CLI.OrkestraError) as ctx:
                    self.call(name, words=words, flag=flag)
                self.assertIn("needs a reason", str(ctx.exception))
                self.assertIn("--reason", str(ctx.exception))                                  # the error teaches the flag
        for name in ("pause", "unblock"):
            self.assertEqual(self.call(name)[0], 0)

    def test_an_old_style_namespace_without_the_flag_still_works(self):
        ns = argparse.Namespace(project="p", run=None, cmd="block", reason=["old", "style"])
        with mock.patch.object(CLI, "_resolve_project", return_value=self.tmp), \
                mock.patch.object(CLI, "_run_id", return_value="001-x"), mock.patch.object(CLI.R, "set_status") as ss, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CLI.cmd_status_change(ns), 0)
        self.assertEqual(ss.call_args[0][3], "old style")


@unittest.skipUnless(os.name != "nt", "point_current symlink / POSIX flow fixtures (as in test_flow)")
class TestReasonEndToEnd(Base):
    """Real runs: the same state whether the reason is words or --reason, and the exit codes the epilog promises."""

    def setUp(self):
        super().setUp()
        self.make_project("demo")
        self.cli("init", "-p", "demo")

    def start(self, mode="autopilot"):
        out = self.cli("start", "-p", "demo", "--title", "T", "--request", "do the thing", "--size", "S",
                       "--approval-mode", mode).stdout
        m = re.search(r"\[orkestra:bind\] project=(.+?) run=(\S+)", out)
        self.assertIsNotNone(m, out)
        self.rid = m.group(2)
        return self.rid

    def state(self):
        return json.loads(self.cli("status", "--json", "-p", "demo", "--run", self.rid).stdout)

    def ledger_has(self, token):
        root = Path(self.ws) / "demo" / ".orkestra"
        return any(token in p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("ledger.md"))

    def test_block_words_and_flag_are_the_same(self):
        self.start()
        self.cli("block", "-p", "demo", "needs", "the", "api", "key")
        self.assertEqual(self.state()["blocked"]["reason"], "needs the api key")
        self.cli("unblock", "-p", "demo")
        self.assertIsNone(self.state()["blocked"])
        self.cli("block", "-p", "demo", "--reason", "needs the api key")
        self.assertEqual((self.state()["status"], self.state()["blocked"]["reason"]), ("blocked", "needs the api key"))
        self.cli("unblock", "--reason", "key arrived", "-p", "demo")
        self.assertEqual(self.state()["status"], "active")

    def test_await_flag_on_a_manual_run(self):
        self.start("manual")
        self.cli("await", "--reason", "which colour?", "-p", "demo")
        st = self.state()
        self.assertEqual((st["status"], st["awaiting"]["question"]), ("awaiting_user", "which colour?"))

    def test_pause_and_abort_take_the_flag(self):
        self.start()
        self.cli("pause", "-p", "demo", "--reason", "pause-token-4471")
        self.assertEqual(self.state()["status"], "paused")
        self.assertTrue(self.ledger_has("pause-token-4471"))
        self.cli("resume", "-p", "demo", "--run", self.rid)
        self.cli("abort", "--reason", "abort-token-9183", "-p", "demo", "--run", self.rid)
        self.assertEqual(self.state()["status"], "aborted")
        self.assertTrue(self.ledger_has("abort-token-9183"))

    def test_both_forms_at_once_exit_2_and_leave_the_run_alone(self):
        self.start()
        cp = self.cli("block", "-p", "demo", "words", "--reason", "flag", check=False)
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("not both", cp.stderr)
        self.assertEqual(self.state()["status"], "active")
        cp = self.cli("abort", "-p", "demo", "words", "--reason", "flag", check=False)
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(self.state()["status"], "active")

    def test_exit_codes_match_the_epilog(self):
        """0 ok · 1 a check failed · 2 usage error or refused request."""
        self.start()
        self.assertEqual(self.cli("status", "-p", "demo", check=False).returncode, 0)
        self.assertEqual(self.cli("plan", "check", "-p", "demo", check=False).returncode, 1)       # no plan yet: FAIL
        self.assertEqual(self.cli("phase", "next", "-p", "demo", check=False).returncode, 0)       # intake -> discover
        self.assertEqual(self.cli("phase", "check", "-p", "demo", check=False).returncode, 1)      # no context.md yet
        self.assertEqual(self.cli("phase", "next", "-p", "demo", check=False).returncode, 2)       # refused, not "failed"
        self.assertEqual(self.cli("block", "-p", "demo", check=False).returncode, 2)               # reason missing
        self.assertEqual(self.cli("no-such-command", check=False).returncode, 2)
        self.assertEqual(self.cli("phase", "sideways", "-p", "demo", check=False).returncode, 2)


# ====================================================================== --help

class TestHelp(unittest.TestCase):
    """`--help` in both languages. Built in-process (argparse reads COLUMNS, the language comes from the env)."""

    ENV = {"COLUMNS": "200", "NO_COLOR": "1", "PYTHON_COLORS": "0"}

    def parser_in(self, code):
        with mock.patch.dict(os.environ, dict(self.ENV, ORKESTRA_LANG=code)):
            return CP.build_parser()

    def render(self, code, name=None):
        with mock.patch.dict(os.environ, dict(self.ENV, ORKESTRA_LANG=code)):
            parser = CP.build_parser()
            target = parser if name is None else children(parser)[name]
            return target.format_help()

    def test_every_subcommand_has_a_help_line_in_both_languages(self):
        names = sorted(children(CP.build_parser()))
        self.assertGreaterEqual(len(names), 40)
        for code in ("en", "tr"):
            cat = i18n.catalog(code)
            for name in names:
                key = "help.cmd." + name
                self.assertIn(key, cat, f"{key} missing in {code}")
                self.assertTrue(cat[key].strip(), key)
        self.assertIn("lang", names)
        self.assertIn("version", names)

    def test_no_stale_help_keys(self):
        names = set(children(CP.build_parser()))
        for code in ("en", "tr"):
            keys = {k[len("help.cmd."):] for k in i18n.catalog(code) if k.startswith("help.cmd.")}
            self.assertEqual(sorted(keys - names), [], f"help.cmd.* keys without a command ({code})")

    def test_help_texts_are_safe_for_argparse_and_for_people(self):
        for name, table in (("EN", H.EN), ("TR", H.TR)):
            for key, text in table.items():
                self.assertNotIn("%", text, f"{name} {key}: argparse expands %")
                self.assertNotIn(EM_DASH, text, f"{name} {key}: no em dash in new text")
                self.assertEqual(text, text.strip(), key)
        for key, text in H.EN.items():
            self.assertNotRegex(text, TR_LETTERS, key)

    def test_top_level_help_in_english(self):
        out = self.render("en")
        flat = squash(out)
        self.assertNotRegex(out, TR_LETTERS)
        for token in ("usage:", "commands", "options", "examples:", "exit codes:", "--lang", "--version"):
            self.assertIn(token, out)
        self.assertIn(squash(i18n.catalog("en")["help.description"]), flat)
        for name in children(self.parser_in("en")):
            self.assertIn(squash(f"{name} {i18n.catalog('en')['help.cmd.' + name]}"), flat, name)

    def test_top_level_help_in_turkish(self):
        out = self.render("tr")
        flat = squash(out)
        for token in ("kullanım:", "komutlar", "seçenekler", "örnekler:", "çıkış kodları:", "--lang", "--version"):
            self.assertIn(token, out)
        self.assertNotIn("usage:", out)
        self.assertNotIn("examples:", out)
        self.assertRegex(out, TR_LETTERS)
        for name in children(self.parser_in("tr")):
            self.assertIn(squash(f"{name} {i18n.catalog('tr')['help.cmd.' + name]}"), flat, name)

    def test_every_subcommand_help_renders_in_both_languages(self):
        names = sorted(children(CP.build_parser()))
        for code in ("en", "tr"):
            line_of = i18n.catalog(code)
            for name in names:
                out = self.render(code, name)
                self.assertIn(("usage:" if code == "en" else "kullanım:") + " orkestra " + name, out, f"{code} {name}")
                self.assertIn(squash(line_of["help.cmd." + name]), squash(out), f"{code} {name}")
                self.assertIn(line_of["help.opt.help"], out, f"{code} {name}")
                if code == "en":
                    self.assertNotRegex(out, TR_LETTERS, f"en {name}")

    def test_epilog_has_five_examples_that_really_parse(self):
        for code in ("en", "tr"):
            epilog = i18n.catalog(code)["help.epilog"]
            lines = [ln for ln in epilog.splitlines() if ln.startswith("  orkestra ")]
            self.assertEqual(len(lines), 5, code)
            keys = []
            for ln in lines:
                argv = shlex.split(ln.strip())[1:]
                key, _, err = CP.inspect(argv)
                self.assertEqual(err, "", f"{code}: `{ln.strip()}` does not parse")
                self.assertTrue(key, ln)
                keys.append(key)
            self.assertEqual(keys, ["doctor", "run", "watch", "status", "lang"], code)
            # no explanation glued to a command line: what is on the line can be pasted as it is
            self.assertFalse([ln for ln in lines if "#" in ln], code)
            self.assertEqual(len([ln for ln in epilog.splitlines() if ln.startswith("  # ")]), 5, code)

    def test_epilog_examples_are_the_same_commands_in_both_languages(self):
        def examples(code):
            return [shlex.split(ln.strip()) for ln in i18n.catalog(code)["help.epilog"].splitlines()
                    if ln.startswith("  orkestra ")]
        for en, tr in zip(examples("en"), examples("tr")):
            self.assertEqual(en[:2], tr[:2])
            if en[1] != "run":                                      # the request of `run` is prose: it is translated
                self.assertEqual(en, tr)

    def test_epilog_documents_the_exit_codes(self):
        for code, label in (("en", "exit codes:"), ("tr", "çıkış kodları:")):
            epilog = i18n.catalog(code)["help.epilog"]
            self.assertIn(label, epilog)
            section = epilog.split(label, 1)[1].split("\n\n", 1)[0]
            codes = re.findall(r"^  (\d)  \S", section, re.M)
            self.assertEqual(codes, ["0", "1", "2"], code)

    def test_building_and_rendering_help_writes_nothing(self):
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="orkestra-help-"))
        self.addCleanup(shutil.rmtree, str(root), True)
        home = root / "does-not-exist"
        with mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(home), "ORKESTRA_LANG": ""}):
            parser = CP.build_parser()
            parser.format_help()
            for child in children(parser).values():
                child.format_help()
            CP.inspect(["task", "list"])
        self.assertEqual(list(root.iterdir()), [])

    def test_the_guard_path_never_loads_a_catalog(self):
        """Hooks import cliparse and call inspect() on every Bash command: no catalogs, no config.json reads."""
        code = (
            "import sys, os\n"
            f"sys.path.insert(0, {str(helpers.LIB)!r})\n"
            "os.environ['ORKESTRA_HOME'] = os.path.join(os.environ['TMPDIR_FOR_TEST'], 'nohome')\n"
            "from orkestra_core import cliparse as C\n"
            "assert not [m for m in sys.modules if m.startswith('orkestra_core.messages')], 'import loads catalogs'\n"
            "assert C.inspect(['task', 'list'])[0] == 'task:list'\n"
            "assert C.inspect(['--lang', 'tr', 'rollback', '--yes'])[0] == 'rollback'\n"
            "assert C.inspect(['--lang', 'xx', 'doctor'])[2]\n"
            "assert C.inspect(['lang', 'tr'])[0] == 'lang'\n"
            "assert not [m for m in sys.modules if m.startswith('orkestra_core.messages')], 'inspect loads catalogs'\n"
            "assert not os.path.exists(os.environ['ORKESTRA_HOME'])\n"
            "print('ok')\n"
        )
        import tempfile
        root = tempfile.mkdtemp(prefix="orkestra-guard-")
        self.addCleanup(shutil.rmtree, root, True)
        env = dict(os.environ, TMPDIR_FOR_TEST=root)
        cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "ok"), cp.stderr)

    def test_the_guard_parser_does_not_call_the_catalog(self):
        with mock.patch.object(i18n, "t", side_effect=AssertionError("catalog used by the guard parser")):
            parser = CP.build_parser(CP.QuietParser, localize=False)
            ns = CP.parse(parser, ["--lang", "tr", "block", "--reason", "x"])
        self.assertEqual((ns.cmd, ns.lang, ns.reason_opt), ("block", "tr", "x"))

    def test_the_quiet_parser_still_never_prints(self):
        for argv in (["--help"], ["lang", "-h"], ["--version"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(CP.inspect(argv)[:2], ("help", []), argv)
            self.assertEqual((out.getvalue(), err.getvalue()), ("", ""), argv)


class TestHelpThroughTheCli(Base):
    def test_both_languages_end_to_end(self):
        for argv in (["--help"], ["-h"], ["doctor", "--help"], ["lang", "-h"]):
            cp = self.cli(*argv, check=False, env=dict(NEUTRAL, ORKESTRA_LANG="en"))
            self.assertEqual(cp.returncode, 0, (argv, cp.stderr))
            self.assertIn("usage:", cp.stdout)
            self.assertNotRegex(cp.stdout, TR_LETTERS, argv)
        for argv in (["--help"], ["doctor", "--help"], ["lang", "-h"], ["phase", "show", "-h"]):
            cp = self.cli(*argv, check=False, env=dict(NEUTRAL, ORKESTRA_LANG="tr"))
            self.assertEqual(cp.returncode, 0, (argv, cp.stderr))
            self.assertIn("kullanım:", cp.stdout)
        cp = self.cli(check=False, env=dict(NEUTRAL, ORKESTRA_LANG="tr"))                      # no arguments: the help
        self.assertEqual(cp.returncode, 0)
        self.assertIn("örnekler:", cp.stdout)

    def test_the_language_comes_from_the_config_file_and_the_locale_too(self):
        self.write_config({"lang": "tr"})
        self.assertIn("kullanım:", self.run_cli("--help").stdout)
        self.write_config({"lang": "auto"})
        self.assertIn("kullanım:", self.run_cli("--help", LANG="tr_TR.UTF-8").stdout)
        self.assertIn("usage:", self.run_cli("--help", LANG="de_DE.UTF-8").stdout)


# ====================================================================== --version / version

class TestVersion(Base):
    def test_flag_and_command_print_the_same_line(self):
        a = self.run_cli("--version")
        b = self.run_cli("version")
        self.assertEqual((a.returncode, b.returncode), (0, 0), a.stderr + b.stderr)
        self.assertEqual(a.stdout.strip(), f"orkestra {__version__}")
        self.assertEqual(a.stdout, b.stdout)

    def test_version_does_not_depend_on_the_language(self):
        for code in ("en", "tr", "auto"):
            self.assertEqual(self.run_cli("--lang", code, "version").stdout.strip(), f"orkestra {__version__}", code)
            self.assertEqual(self.run_cli("--version", ORKESTRA_LANG=code).stdout.strip(), f"orkestra {__version__}", code)

    def test_version_reads_no_state(self):
        self.run_cli("version")
        self.run_cli("--version")
        self.assertFalse(self.home.exists())

    def test_the_guard_treats_it_as_help_not_as_a_command(self):
        self.assertEqual(CP.inspect(["--version"])[:2], ("help", []))
        self.assertEqual(CP.inspect(["version"])[:2], ("version", []))

    def test_unknown_commands_are_usage_errors(self):
        cp = self.run_cli("verison")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("Unknown command verison. Did you mean version?", cp.stderr)


# ====================================================================== doctor

def _path_without_claude():
    parts = []
    for d in ("/usr/bin", "/bin", str(Path(sys.executable).parent)):
        if Path(d).is_dir() and d not in parts:
            parts.append(d)
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if d and d not in parts and not (Path(d) / "claude").exists():
            parts.append(d)
    return os.pathsep.join(parts)


class TestDoctorLanguageRow(Base):
    ROW = re.compile(r"^\[OK\] language: (\w+) \((.+)\)$", re.M)

    def doctor(self, **env):
        base = {"PATH": _path_without_claude(), "COPILOT_HOME": str(self.tmp / "nocopilot")}
        base.update(env)
        return self.run_cli("doctor", **base)

    def test_row_names_language_and_source(self):
        cp = self.doctor(ORKESTRA_LANG="tr")
        m = self.ROW.search(cp.stdout)
        self.assertIsNotNone(m, cp.stdout)
        self.assertEqual((m.group(1), m.group(2)), ("tr", "env (ORKESTRA_LANG)"))
        cp = self.doctor(LANG="tr_TR.UTF-8")
        self.assertEqual(self.ROW.search(cp.stdout).groups(), ("tr", "locale"))

    def test_row_follows_the_config_file(self):
        self.write_config({"lang": "en"})
        m = self.ROW.search(self.doctor(LANG="tr_TR.UTF-8").stdout)
        self.assertEqual(m.group(1), "en")
        self.assertTrue(m.group(2).startswith("config ("), m.group(2))

    def test_row_is_soft_and_has_no_em_dash(self):
        self.write_config("{broken")                                    # a broken config must not turn it into a failure
        cp = self.doctor()
        self.assertNotIn("[!!] language", cp.stdout)
        row = [ln for ln in cp.stdout.splitlines() if "language:" in ln]
        self.assertEqual(len(row), 1, cp.stdout)
        self.assertNotIn(EM_DASH, row[0])
        self.assertNotRegex(row[0], TR_LETTERS)

    def test_row_does_not_change_the_exit_code_rules(self):
        """No engine is ready on this fixture machine: exit 1 with or without a language row (test_doctor owns the
        rest of the matrix)."""
        cp = self.doctor()
        self.assertIsNotNone(self.ROW.search(cp.stdout), cp.stdout)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)


def _all_parsers(parser):
    """The parser and every subcommand parser below it."""
    yield parser
    for child in children(parser).values():
        yield from _all_parsers(child)


class TestEveryHelpTextIsLocalized(unittest.TestCase):
    """No option of any command prints a hard-coded English help line in a Turkish `--help` (they were plain literals)."""

    def helps(self, code):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": code, "COLUMNS": "200"}):
            parser = CP.build_parser()
            return [(p.prog, a.dest, a.help) for p in _all_parsers(parser) for a in p._actions
                    if a.help and a.help != argparse.SUPPRESS]

    def test_every_help_line_is_a_catalog_text_in_both_languages(self):
        for code in ("en", "tr"):
            values = set(i18n.catalog(code).values())
            rows = self.helps(code)
            self.assertGreater(len(rows), 60, "the walk really visits every subcommand")
            for prog, dest, text in rows:
                self.assertIn(text, values, f"{code}: `{prog}` {dest} has a help text that is not in the {code} catalog")

    def test_turkish_help_of_the_commands_that_used_to_print_english(self):
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "tr", "COLUMNS": "200", "NO_COLOR": "1"}):
            parser = CP.build_parser()
            shown = {name: squash(children(parser)[name].format_help()) for name in
                     ("start", "watchdog", "rollback", "watch", "dashboard", "run", "cursor", "copilot", "setup", "lang",
                      "tdd", "task", "playbook", "await")}
        for name, english in (("start", "run size; auto"), ("start", "which agent drives"), ("watchdog", "classify and print"),
                              ("watchdog", "print nothing when every run"), ("rollback", "also revert differences"),
                              ("watch", "also show finished runs"), ("dashboard", "open the browser"),
                              ("run", "in your own words"), ("cursor", "copy files instead"),
                              ("copilot", "show what would change"), ("setup", "without asking"),
                              ("lang", "where it comes from"), ("tdd", "reason when deviating"),
                              ("task", "start despite shared files"), ("playbook", "the open run is in"),
                              ("await", "give one of the two")):
            self.assertNotIn(english, shown[name], f"{name}: English help in a Turkish --help")
        self.assertIn("run boyutu", shown["start"])
        self.assertIn("tarayıcıyı aç", shown["dashboard"])


class TestParserErrorsFollowTheLanguage(unittest.TestCase):
    """argparse words its usage errors in English; in Turkish the `error:` line is translated, in English it is
    argparse's own sentence, character for character."""

    BAD = (["phase", "bogus"], ["brief"], ["status", "--bogus"], ["start", "--title"], ["tdd", "red"],
           ["watch", "--interval", "fast"], ["lang", "de"], ["--lang", "de", "status"], ["status", "extra"])

    def stderr_of(self, parser_class, argv, code):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": code, "COLUMNS": "200"}), contextlib.redirect_stderr(err):
            parser = CP.build_parser(parser_class)
            with self.assertRaises(SystemExit) as caught:
                CP.parse(parser, argv)
        self.assertEqual(caught.exception.code, 2, argv)
        return err.getvalue()

    def test_in_english_the_message_is_argparses_own(self):
        for argv in self.BAD:
            plain = self.stderr_of(argparse.ArgumentParser, argv, "en")
            ours = self.stderr_of(CP.LocalizedParser, argv, "en")
            self.assertEqual(ours, plain, argv)
            self.assertIn("error:", ours)

    def test_an_unknown_command_gets_the_unknown_command_error_in_both_languages(self):
        en = self.stderr_of(CP.LocalizedParser, ["nosuchcmd"], "en")
        self.assertIn("error: Unknown command nosuchcmd.", en)
        self.assertNotIn("invalid choice", en)
        tr = self.stderr_of(CP.LocalizedParser, ["nosuchcmd"], "tr")
        self.assertIn("hata:", tr)
        self.assertIn("nosuchcmd", tr)
        self.assertNotIn("invalid choice", tr)

    def test_in_turkish_the_error_line_is_turkish_and_the_exit_code_is_the_same(self):
        for argv in self.BAD:
            text = self.stderr_of(CP.LocalizedParser, argv, "tr")
            self.assertIn("hata:", text, argv)
            self.assertNotIn("error:", text, argv)
            for english in ("invalid choice", "are required", "unrecognized arguments", "expected one argument",
                            "invalid float value", "unsupported"):
                self.assertNotIn(english, text, argv)
        self.assertIn("geçersiz seçenek: 'bogus'", self.stderr_of(CP.LocalizedParser, ["phase", "bogus"], "tr"))
        self.assertIn("şu argümanlar gerekli: task", self.stderr_of(CP.LocalizedParser, ["brief"], "tr"))
        self.assertIn("tanınmayan argümanlar: extra", self.stderr_of(CP.LocalizedParser, ["status", "extra"], "tr"))
        self.assertIn("bir değer bekliyor", self.stderr_of(CP.LocalizedParser, ["start", "--title"], "tr"))
        self.assertIn("geçersiz float değeri: 'fast'", self.stderr_of(CP.LocalizedParser, ["watch", "--interval", "fast"], "tr"))

    def test_a_message_no_pattern_knows_is_printed_as_argparse_made_it(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ORKESTRA_LANG": "tr"}), contextlib.redirect_stderr(err):
            parser = CP.LocalizedParser(prog="x")
            with self.assertRaises(SystemExit):
                parser.error("something argparse never said")
        self.assertIn("x: hata: something argparse never said", err.getvalue())

    def test_the_real_cli_prints_it_end_to_end(self):
        proc = subprocess.run([sys.executable, str(helpers.BIN), "phase", "bogus"], capture_output=True, text=True,
                              encoding="utf-8", env=dict(os.environ, ORKESTRA_LANG="tr", ORKESTRA_HOME=str(helpers.PLUGIN)),
                              timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("kullanım:", proc.stderr)
        self.assertIn("orkestra phase: hata: ", proc.stderr)
        self.assertIn("geçersiz seçenek", proc.stderr)


class TestRunUsageError(Base):
    """`orkestra run` without a request prints its usage in the user's language and names all three engines."""

    def test_the_usage_line_follows_the_language_and_lists_copilot(self):
        self.make_project("demo")
        en = self.run_cli("run", "ab", "-p", "demo", ORKESTRA_LANG="en")
        tr = self.run_cli("run", "ab", "-p", "demo", ORKESTRA_LANG="tr")
        self.assertEqual((en.returncode, tr.returncode), (2, 2), en.stdout + tr.stdout)
        self.assertIn('usage: orkestra run [--engine claude|cursor|copilot|pi] [-p <project>] "<request>"', en.stderr)
        self.assertNotRegex(en.stderr, TR_LETTERS)
        self.assertNotIn("<proje>", en.stderr)
        self.assertIn('kullanım: orkestra run [--engine claude|cursor|copilot|pi] [-p <proje>] "<istek>"', tr.stderr)


class TestDocumentLookups(Base):
    """`orkestra template <name>` and `orkestra playbook <name>` look a name up in the plugin's own document folders. A
    name is a key, not a path: `../` or an absolute path must never print a file from anywhere else."""

    def setUp(self):
        super().setUp()
        self.secret = self.tmp / "outside.md"
        self.secret.write_text("TOP-SECRET-OUTSIDE-THE-DOCUMENT-FOLDERS\n", encoding="utf-8")

    def lookups(self, command, folder):
        base = helpers.PLUGIN / folder
        names = [os.path.relpath(self.secret, base), os.path.relpath(self.secret, base)[:-3], str(self.secret),
                 str(self.secret)[:-3], "../" * 12 + str(self.secret).lstrip("/\\"), "..", ".", "/", "memory/../../../../x"]
        for name in names:
            cp = self.run_cli(command, name, ORKESTRA_LANG="en")
            self.assertNotIn("TOP-SECRET", cp.stdout + cp.stderr, name)
            self.assertEqual(cp.returncode, 2, (name, cp.stdout, cp.stderr))
            self.assertIn("available:", cp.stderr, name)

    def test_a_template_name_cannot_leave_the_template_folder(self):
        self.lookups("template", Path("templates") / "en")

    def test_a_playbook_name_cannot_leave_the_playbook_folder(self):
        self.lookups("playbook", Path("skills") / "run" / "references")

    def test_real_names_still_resolve(self):
        for lang in ("en", "tr"):
            for name in ("plan", "plan.md", "memory/PROJECT", "memory/PROJECT.md", "spec"):
                cp = self.run_cli("template", name, ORKESTRA_LANG=lang)
                self.assertEqual(cp.returncode, 0, (lang, name, cp.stderr))
                self.assertTrue(cp.stdout.strip(), (lang, name))
        for name in ("implement", "phase-implement", "intake", "run"):
            cp = self.run_cli("playbook", name, ORKESTRA_LANG="en")
            self.assertEqual(cp.returncode, 0, (name, cp.stderr))
            self.assertTrue(cp.stdout.strip(), name)

    @unittest.skipIf(os.name == "nt", "needs symlinks")
    def test_a_symlink_that_points_out_is_not_followed(self):
        """The folders are the plugin's own, so the link is made in a private copy of the library."""
        copy = self.tmp / "plugin-copy"
        shutil.copytree(helpers.PLUGIN, copy, ignore=shutil.ignore_patterns("tests", "cursor", "__pycache__", "vendor"))
        os.symlink(self.secret, copy / "templates" / "en" / "evil.md")
        env = dict(os.environ, ORKESTRA_LANG="en", ORKESTRA_HOME=str(self.home), ORKESTRA_WORKSPACE=str(self.ws))
        cp = subprocess.run([sys.executable, str(copy / "bin" / "orkestra"), "template", "evil"], capture_output=True,
                            text=True, encoding="utf-8", env=env, cwd=str(self.tmp), timeout=60)
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertNotIn("TOP-SECRET", cp.stdout + cp.stderr)


class TestDashboardReplaceFlag(helpers.Sandbox):
    def test_replace_is_parsed_and_reaches_the_dashboard(self):
        from unittest import mock
        from orkestra_core import cli as CLI
        from orkestra_core import cliparse as CP
        ns = CP.build_parser().parse_args(["dashboard", "--open", "--replace", "--port", "4000"])
        self.assertTrue(ns.replace and ns.open and ns.port == 4000)
        self.assertFalse(CP.build_parser().parse_args(["dashboard"]).replace)
        with mock.patch("orkestra_core.monitor.dashboard", return_value=0) as dash:
            self.assertEqual(CLI.cmd_dashboard(ns), 0)
        dash.assert_called_once_with(None, 4000, True, replace=True, read_only=False)
        ro = CP.build_parser().parse_args(["dashboard", "--read-only"])
        self.assertTrue(ro.read_only)
        with mock.patch("orkestra_core.monitor.dashboard", return_value=0) as dash:
            self.assertEqual(CLI.cmd_dashboard(ro), 0)
        dash.assert_called_once_with(None, 47616, False, replace=False, read_only=True)

    def test_the_flag_is_documented_in_both_languages(self):
        for code in ("en", "tr"):
            out = self.cli("dashboard", "--help", env={"ORKESTRA_LANG": code}).stdout
            self.assertIn("--replace", out, code)


if __name__ == "__main__":
    unittest.main()
