"""Installer localization: `install.sh` (bash, no Python at hand) resolves its language exactly like `orkestra` does and
prints every message in English or Turkish; `install.py` takes its text from the `install` catalog."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from orkestra_core import i18n

ROOT = helpers.PLUGIN.parent.parent
INSTALL_SH = ROOT / "install.sh"
INSTALL_PY = ROOT / "install.py"
TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
BASH = [b for b in dict.fromkeys(filter(None, ("/bin/bash", shutil.which("bash")))) if os.path.exists(b)]
NEEDS_BASH = unittest.skipUnless(os.name != "nt" and BASH, "install.sh is a bash script (Windows uses install.py)")


def sh_text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def language_block() -> str:
    """The functions that resolve the language, cut out of install.sh so they can run on their own."""
    text = sh_text()
    return text[text.index("lang_of() {"):text.index('LANG_CODE="$(pick_lang)"')]


def l_function() -> str:
    return re.search(r"^L\(\) \{.*$", sh_text(), re.M).group(0)


@NEEDS_BASH
class TestInstallShLanguage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orkestra-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def bash(self, bash: str, script: str, env: dict) -> str:
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp), "ORKESTRA_HOME": str(self.tmp)}
        cp = subprocess.run([bash, "-c", script], env={**base, **env}, capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip()

    def pick(self, bash: str, env: dict) -> str:
        return self.bash(bash, "set -euo pipefail\n" + language_block() + "\npick_lang", env)

    def config(self, value) -> None:
        (self.tmp / "config.json").write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def python_says(self, env: dict) -> str:
        with mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(self.tmp)}):
            return i18n.lang(dict(env))

    CASES = [
        {},
        {"ORKESTRA_LANG": "tr"}, {"ORKESTRA_LANG": "TR"}, {"ORKESTRA_LANG": "tr_TR.UTF-8"}, {"ORKESTRA_LANG": "Turkish"},
        {"ORKESTRA_LANG": "en", "LANG": "tr_TR.UTF-8"}, {"ORKESTRA_LANG": "EN_us"}, {"ORKESTRA_LANG": "English"},
        {"ORKESTRA_LANG": "de", "LANG": "tr_TR.UTF-8"},                     # explicit but unsupported: English, not the locale
        {"ORKESTRA_LANG": "auto", "LANG": "tr_TR.UTF-8"}, {"ORKESTRA_LANG": "", "LANG": "tr_TR.UTF-8"},
        {"ORKESTRA_LANG": "auto", "LANG": "de_DE.UTF-8"},
        {"LANG": "tr_TR.UTF-8"}, {"LANG": "en_US.UTF-8"}, {"LANG": "de_DE.UTF-8"}, {"LANG": "C"}, {"LANG": "POSIX"},
        {"LC_ALL": "tr_TR.UTF-8", "LANG": "en_US.UTF-8"}, {"LC_ALL": "C", "LANG": "tr_TR.UTF-8"},
        {"LC_MESSAGES": "tr_TR.UTF-8"}, {"LANGUAGE": "de:tr:en"}, {"LANGUAGE": "de:fr"}, {"LANG": "tr_TR@euro"},
    ]

    def test_resolution_matches_orkestra_for_environment_and_locale(self):
        for bash in BASH:
            for env in self.CASES:
                self.assertEqual(self.pick(bash, env), self.python_says(env), f"{bash} {env}")

    def test_resolution_matches_orkestra_for_the_config_file(self):
        configs = [{"lang": "tr"}, {"lang": "en"}, {"lang": "auto"}, {"lang": "de"}, {"lang": "TR"},
                   {"other": 1, "lang": "tr", "x": [1]}, {"nothing": True}, "{not json", "", "[]"]
        for bash in BASH:
            for cfg in configs:
                self.config(cfg)
                for env in ({}, {"LANG": "tr_TR.UTF-8"}, {"LANG": "en_US.UTF-8"}, {"ORKESTRA_LANG": "en"}):
                    self.assertEqual(self.pick(bash, env), self.python_says(env), f"{bash} {cfg!r} {env}")

    def test_a_config_with_spaces_around_the_colon_is_read(self):
        (self.tmp / "config.json").write_text('{\n  "lang" :   "tr"\n}\n', encoding="utf-8")
        for bash in BASH:
            self.assertEqual(self.pick(bash, {"LANG": "en_US.UTF-8"}), "tr")

    def test_l_prints_the_requested_language_and_keeps_text_verbatim(self):
        for bash in BASH:
            for code, want in (("tr", "Ön koşullar"), ("en", "Prerequisites")):
                out = self.bash(bash, f'LANG_CODE={code}\n{l_function()}\nL "Prerequisites" "Ön koşullar"', {})
                self.assertEqual(out, want)
            weird = self.bash(bash, f"LANG_CODE=tr\n{l_function()}\nL 'a %s \\n b' 'c %d \\\\ d'", {})
            self.assertEqual(weird, "c %d \\\\ d")                      # printf '%s': no format or escape processing

    def test_the_script_parses_in_every_bash(self):
        for bash in BASH:
            cp = subprocess.run([bash, "-n", str(INSTALL_SH)], capture_output=True, text=True)
            self.assertEqual((cp.returncode, cp.stderr), (0, ""), bash)


class TestInstallShMessages(unittest.TestCase):
    L_CALL = re.compile(r"""\$\(L (?:'([^']*)'|"((?:[^"\\]|\\.|\$\([^)]*\))*)") (?:'([^']*)'|"((?:[^"\\]|\\.|\$\([^)]*\))*)")\)""")

    def pairs(self):
        out = []
        for m in self.L_CALL.finditer(sh_text()):
            out.append((m.group(1) if m.group(1) is not None else m.group(2),
                        m.group(3) if m.group(3) is not None else m.group(4)))
        return out

    def test_every_message_has_both_languages(self):
        pairs = self.pairs()
        self.assertGreaterEqual(len(pairs), 40, "the L-call pattern no longer matches install.sh: fix the test")
        for en, tr in pairs:
            self.assertTrue(en.strip() and tr.strip(), (en, tr))
            self.assertFalse(TR_LETTERS.search(en), f"English text with Turkish letters: {en!r}")
            self.assertNotIn("—", en + tr, "no em dash in new installer text")
            self.assertEqual(re.findall(r"\$\w+|\$\{\w+\}", en), re.findall(r"\$\w+|\$\{\w+\}", tr),
                             f"the two texts must reference the same variables: {en!r}")

    def test_turkish_texts_are_translations_not_copies(self):
        same = [en for en, tr in self.pairs() if en == tr and len(en) > 12 and not en.startswith("Install Claude")]
        self.assertEqual(same, [])

    def test_no_turkish_outside_the_messages_and_the_vocabulary(self):
        text = sh_text()
        text = self.L_CALL.sub("", text)
        text = re.sub(r"if \[ \"\$LANG_CODE\" = tr \]; then\ncat <<EOF.*?\nEOF\nelse", "else", text, flags=re.S)
        text = re.sub(r"^\s*tr\|tr\[_\.@-\]\*\|turkish\|turkce\|türkçe\).*$", "", text, flags=re.M)
        text = re.sub(r"^L\(\) \{.*$", "", text, flags=re.M)
        hits = [ln for ln in text.splitlines() if TR_LETTERS.search(ln)]
        self.assertEqual(hits, [], "Turkish text outside L 'en' 'tr' pairs")

    def test_epilogue_exists_in_both_languages_with_the_same_commands(self):
        text = sh_text()
        blocks = re.findall(r"cat <<EOF\n(.*?)\nEOF", text, re.S)
        self.assertEqual(len(blocks), 2)
        tr_block, en_block = blocks
        self.assertTrue(TR_LETTERS.search(tr_block))
        self.assertFalse(TR_LETTERS.search(en_block))
        cmds = lambda b: sorted(set(re.findall(r"(?:python3 \$PLUGIN/bin/orkestra [\w |-]+|/orkestra:\w+|N8N_URL[^\n]*)", b)))
        self.assertEqual(cmds(tr_block), cmds(en_block))
        for must in ("orkestra lang en | tr | auto", "/orkestra:run", "orkestra doctor"):
            self.assertIn(must, tr_block)
            self.assertIn(must, en_block)

    def test_the_prompt_hint_follows_the_language(self):
        text = sh_text()
        self.assertIn('echo "$(L Y/n E/h)"', text)
        self.assertIn('echo "$(L y/N e/H)"', text)
        # Intentional change: this used to pin the prefix test `[YyEe]`, under which "exit" and "enable" answered yes.
        # A yes is now the whole word y/yes, and e/evet only in a Turkish session (test_installer.TestInstallSh.answer).
        self.assertNotIn("[YyEe]", text)
        self.assertIn("y|yes) return 0", text)
        self.assertIn("e|evet) [ \"$LANG_CODE\" = tr ]", text)


class TestInstallPy(unittest.TestCase):
    def help(self, lang: str) -> str:
        env = {**os.environ, "ORKESTRA_LANG": lang, "PYTHONIOENCODING": "utf-8"}
        cp = subprocess.run([sys.executable, str(INSTALL_PY), "--help"], env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_help_is_english_or_turkish(self):
        en, tr = self.help("en"), self.help("tr")
        self.assertIn("Install Orkestra (cross-platform)", en)
        self.assertFalse(TR_LETTERS.search(en))
        self.assertIn("platformlar arası", tr)
        self.assertIn("--skip-tests", tr)                   # flags never change

    def test_catalog_keys_cover_every_text_the_script_uses(self):
        src = INSTALL_PY.read_text(encoding="utf-8")
        used = set(re.findall(r'_t\("(install\.\w+)"', src))
        self.assertGreaterEqual(len(used), 5)
        for code in ("en", "tr"):
            self.assertEqual(sorted(used - set(i18n.catalog(code))), [], code)
        # scripts/release.py owns the install.release_* keys: each one is named (quoted) there, and none is dead
        release_src = (INSTALL_PY.parent / "scripts" / "release.py").read_text(encoding="utf-8")
        used |= {k for k in i18n.catalog("en") if k.startswith("install.release_")
                 and '"%s"' % k[len("install.release_"):] in release_src}
        self.assertEqual(sorted(k for k in i18n.catalog("en") if k.startswith("install.")), sorted(used))

    def test_a_missing_library_falls_back_to_english(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("orkestra_install_probe", INSTALL_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with mock.patch.object(mod, "PLUGIN", Path(tempfile.gettempdir()) / "no-such-orkestra-plugin"), \
                mock.patch.dict(sys.modules, {"orkestra_core": None, "orkestra_core.i18n": None}):
            self.assertEqual(mod._t("install.finished", "English fallback"), "English fallback")

    def test_the_script_has_no_turkish_letters_and_stays_python39_syntax(self):
        import ast
        src = INSTALL_PY.read_text(encoding="utf-8")
        self.assertFalse(TR_LETTERS.search(src))
        ast.parse(src, feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
