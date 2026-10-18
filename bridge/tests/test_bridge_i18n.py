"""The bridge in two languages: Telegram/webhook replies, errors, notifications, CLI output, the local UI and the
OpenClaw plugin's texts, in English AND Turkish.

Policy under test (plugins/orkestra/lib/orkestra_core/i18n.py): what a person reads follows ORKESTRA_LANG, then "lang"
in ORKESTRA_HOME/config.json, then the OS locale, then English; API JSON keys, status enums and the `error` codes of
the HTTP API never change with the language; the command vocabulary works in either language; the Turkish letters live
in the catalog and the UI table only. Other bridge suites pin ORKESTRA_LANG=tr (their assertions predate the English
default), so this file is where English is exercised.

Most checks run in-process against the real module with every path patched into a temp dir; a few start a real bridge
(fake claude, no network, no ~/.orkestra) to see the language end to end. Node-based checks (the plugin tables, the UI
table, language-resolution parity) are skipped when `node` is not installed.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
os.environ["ORKESTRA_NO_DAEMON"] = "1"      # no test starts the detached fast watcher (fastwatch.ensure_running)
BRIDGE_DIR = HERE.parent
ROOT = BRIDGE_DIR.parent
BRIDGE = BRIDGE_DIR / "orkestra_bridge.py"
CLIENT = BRIDGE_DIR / "orkestra_client.py"
N8N_IMPORT = BRIDGE_DIR / "n8n_import.py"
UI = BRIDGE_DIR / "ui"
PLUGIN_JS = ROOT / "integrations" / "openclaw" / "plugin"
FAKE_CLAUDE = HERE / "fake_claude.py"
NODE = shutil.which("node")

TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
EM_DASH = "—"
LANG_VARS = ("ORKESTRA_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE")

# `orkestra_bridge` computes HOME/CONFIG_PATH/JOBS_DIR/LOG_PATH at import time: import it once with a throw-away home,
# then every test patches those four names into its own temp dir.
sys.path.insert(0, str(BRIDGE_DIR))
_import_home = tempfile.mkdtemp(prefix="bridge-i18n-import-")
with mock.patch.dict(os.environ, {"ORKESTRA_HOME": _import_home}):
    import orkestra_bridge as B
    from orkestra_core import i18n
shutil.rmtree(_import_home, ignore_errors=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)


class Sandbox(unittest.TestCase):
    """A temp ORKESTRA_HOME + workspace, the bridge module's paths pointing into it, and no language variables."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge-i18n-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.ws = self.tmp / "Projects"
        self.home.mkdir()
        for name in ("demo", "demo2"):
            (self.ws / name).mkdir(parents=True)
        (self.ws / "secret-proj").mkdir()
        env = mock.patch.dict(os.environ, {"ORKESTRA_HOME": str(self.home), "ORKESTRA_WORKSPACE": str(self.ws)})
        env.start()
        self.addCleanup(env.stop)
        for var in LANG_VARS:
            os.environ.pop(var, None)
        for name, value in (("HOME", self.home), ("CONFIG_PATH", self.home / "bridge.secret.json"),
                            ("JOBS_DIR", self.home / "bridge" / "jobs"), ("LOG_PATH", self.home / "logs" / "bridge.log")):
            patcher = mock.patch.object(B, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = json.loads(json.dumps(B.DEFAULTS))
        self.cfg.update(workspace=str(self.ws), allowed_projects=["demo", "demo2"], watchdog_interval=0)

    def speak(self, code):
        os.environ["ORKESTRA_LANG"] = code

    def job(self, **fields):
        job = {"id": "aaaa11112222", "project": "demo", "project_path": str(self.ws / "demo"), "chat_id": "42",
               "status": "awaiting_user", "turns": [], "created": "2026-01-01T00:00:00+0000", "engine": "claude",
               "session_id": "0a0a0a0a-1111-4222-8333-444455556666"}
        job.update(fields)
        B.save_job(job)
        return job


def run_node(code, stdin_obj=None, esm=False):
    args = [NODE] + (["--input-type=module"] if esm else []) + ["-e", code]
    cp = subprocess.run(args, input=json.dumps(stdin_obj) if stdin_obj is not None else "", capture_output=True,
                        text=True, encoding="utf-8", timeout=60)
    if cp.returncode != 0:
        raise AssertionError(f"node failed: {cp.stderr}")
    return json.loads(cp.stdout)


# ====================================================================================================== catalog

class TestCatalog(unittest.TestCase):
    def sources(self):
        """The bridge's Python modules that talk to a person."""
        return [BRIDGE, CLIENT, N8N_IMPORT]

    def test_every_key_in_the_code_exists_and_every_bridge_key_is_used(self):
        used = set()
        for path in self.sources():
            used |= set(re.findall(r"""\bt\(["'](bridge\.[\w.]+)["']""", path.read_text(encoding="utf-8")))
        used |= {f"bridge.label.{status}" for status in B.NOTIFY_LABELS}      # built by B.status_label()
        for code in ("en", "tr"):
            catalog = {k: v for k, v in i18n.catalog(code).items() if k.startswith("bridge.")}
            self.assertEqual(sorted(used - set(catalog)), [], f"keys used but not in the {code} catalog")
            self.assertEqual(sorted(set(catalog) - used), [], f"{code} catalog keys nothing uses")

    def test_catalog_text_has_no_em_dash(self):
        for code in ("en", "tr"):
            for key, text in i18n.catalog(code).items():
                if key.startswith("bridge."):
                    self.assertNotIn(EM_DASH, text, f"{code} {key}")

    def test_english_catalog_has_no_turkish_letters_and_turkish_is_translated(self):
        en = {k: v for k, v in i18n.catalog("en").items() if k.startswith("bridge.")}
        tr = {k: v for k, v in i18n.catalog("tr").items() if k.startswith("bridge.")}
        self.assertTrue(en, "bridge catalog is empty")
        self.assertEqual(sorted(en), sorted(tr))
        for key, text in en.items():
            self.assertIsNone(TR_LETTERS.search(text), key)
            self.assertEqual(i18n.fields(text), i18n.fields(tr[key]), key)
        untranslated = [k for k in en if en[k] == tr[k] and len(re.sub(r"\{\w+\}|[^A-Za-z]", "", en[k])) > 12]
        self.assertEqual(untranslated, [])

    def test_python_sources_and_ui_html_hold_no_turkish_letters(self):
        """Turkish text lives in the catalog (messages/bridge.py) and in ui/i18n.js: nowhere else of the bridge."""
        for path in self.sources() + [UI / "index.html", UI / "app.js", UI / "app.css"]:
            self.assertIsNone(TR_LETTERS.search(path.read_text(encoding="utf-8")), path.name)
        for path in sorted((BRIDGE_DIR / "n8n").glob("*.json")):
            self.assertIsNone(TR_LETTERS.search(path.read_text(encoding="utf-8")), path.name)
        self.assertEqual(sorted(p.name for p in BRIDGE_DIR.glob("*.py")), ["n8n_import.py", "orkestra_bridge.py", "orkestra_client.py"],
                         "a new bridge module must be added to TestCatalog.sources")

    def test_the_openclaw_contract_is_english_with_a_linked_turkish_twin(self):
        en = (ROOT / "integrations" / "openclaw" / "CONTRACT.md").read_text(encoding="utf-8")
        tr = (ROOT / "integrations" / "openclaw" / "CONTRACT.tr.md").read_text(encoding="utf-8")
        self.assertIn("(./CONTRACT.tr.md)", en)
        self.assertIn("(./CONTRACT.md)", tr)
        # the only Turkish in the English text: the language name, the Turkish protocol words and the Turkish example
        stripped = re.sub(r"Türkçe|\biş\b|Başladım|`/canli`", "", en)
        self.assertIsNone(TR_LETTERS.search(stripped), TR_LETTERS.search(stripped))
        self.assertTrue(TR_LETTERS.search(tr))
        for doc in (en, tr):
            for token in ("/run", "/yap", "/runs", "/durum", "/cancel", "/iptal", "/live", "/canli", "/telegram",
                          "ORKESTRA_LANG", "config.json", '"lang"', "parseJobRef", "approve", "onayla", "allowed_projects"):
                self.assertIn(token, doc, token)
        self.assertNotIn(EM_DASH, en)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_the_plugin_repeats_the_bridge_labels_and_job_word(self):
        """The OpenClaw plugin builds the same notification header the bridge sends."""
        tables = run_node(
            f'import {{ MESSAGES }} from {json.dumps((PLUGIN_JS / "messages.js").as_uri())};'
            "console.log(JSON.stringify(MESSAGES));", esm=True)
        for code in ("en", "tr"):
            catalog = i18n.catalog(code)
            self.assertEqual(tables[code]["job_ref"], catalog["bridge.job_ref"], code)
            for status in B.NOTIFY_LABELS:
                self.assertEqual(tables[code][f"label.{status}"], catalog[f"bridge.label.{status}"], f"{code} {status}")


# =============================================================================================== language resolution

LANG_CASES = [
    ({}, None),
    ({"ORKESTRA_LANG": "tr"}, None),
    ({"ORKESTRA_LANG": "en", "LANG": "tr_TR.UTF-8"}, {"lang": "tr"}),
    ({"ORKESTRA_LANG": "TR"}, None),
    ({"ORKESTRA_LANG": "de", "LANG": "tr_TR.UTF-8"}, None),
    ({"ORKESTRA_LANG": "auto", "LC_ALL": "tr_TR.UTF-8"}, None),
    ({"ORKESTRA_LANG": " Turkish "}, None),
    ({"LANG": "tr_TR.UTF-8"}, None),
    ({"LANG": "en_US.UTF-8"}, {"lang": "tr"}),
    ({"LANG": "tr_TR.UTF-8"}, {"lang": "en"}),
    ({"LANG": "tr_TR.UTF-8"}, {"lang": "auto"}),
    ({"LANG": "de_DE.UTF-8"}, {"lang": "auto"}),
    ({"LC_ALL": "C", "LANG": "tr_TR.UTF-8"}, None),
    ({"LC_ALL": "en_GB.UTF-8", "LANG": "tr_TR.UTF-8"}, None),
    ({"LC_MESSAGES": "tr_TR", "LANG": "en_US"}, None),
    ({"LANGUAGE": "de:tr:en"}, None),
    ({"LANGUAGE": "de:fr"}, None),
    ({"LANG": "tr_TR.UTF-8"}, "{not json"),
    ({"LANG": "tr_TR.UTF-8"}, "[]"),
    ({"LANG": "tr_TR.UTF-8"}, {"lang": 5}),
    ({"LANG": "tr_TR.UTF-8"}, {"lang": "klingon"}),
    ({}, {"lang": "turkce"}),
    ({}, {"lang": "English"}),
]


class TestLanguageResolution(Sandbox):
    def test_bridge_text_follows_i18n_lang_in_every_case(self):
        """The bridge's language IS i18n.lang(): same ORKESTRA_LANG > config.json > OS locale > English order."""
        for index, (env, config) in enumerate(LANG_CASES):
            with self.subTest(env=env, config=config):
                home = self.tmp / f"case{index}"
                home.mkdir()
                if config is not None:
                    (home / "config.json").write_text(config if isinstance(config, str) else json.dumps(config),
                                                      encoding="utf-8")
                with mock.patch.dict(os.environ, dict(env, ORKESTRA_HOME=str(home))):
                    want = i18n.lang(dict(env, ORKESTRA_HOME=str(home)))
                    self.assertEqual(B.usage(), i18n.catalog(want)["bridge.usage"])
                    self.assertEqual(B.status_label("done"), i18n.catalog(want)["bridge.label.done"])
                    self.assertEqual(want, i18n.lang(), "the bridge reads the live environment")

    def test_default_is_english(self):
        self.assertEqual(B.usage(), i18n.catalog("en")["bridge.usage"])
        self.assertIn("/run claude:|cursor:|copilot:|pi: <project> <request>", B.usage())

    def test_a_config_change_reaches_a_running_bridge_without_a_restart(self):
        self.assertIn("/run <project>", B.usage())
        (self.home / "config.json").write_text(json.dumps({"lang": "tr"}), encoding="utf-8")
        self.assertIn("/yap <proje> <istek>", B.usage())
        (self.home / "config.json").write_text(json.dumps({"lang": "en", "padding": "x" * 8}), encoding="utf-8")
        self.assertIn("/run <project>", B.usage())

    @unittest.skipUnless(NODE, "node is not installed")
    def test_the_plugin_resolves_the_language_exactly_like_the_bridge(self):
        """integrations/openclaw/plugin/lang.js and orkestra_core/i18n.py agree on every case."""
        cases = []
        for index, (env, config) in enumerate(LANG_CASES):
            home = self.tmp / f"parity{index}"
            home.mkdir()
            if config is not None:
                (home / "config.json").write_text(config if isinstance(config, str) else json.dumps(config),
                                                  encoding="utf-8")
            cases.append(dict(env, ORKESTRA_HOME=str(home)))
        got = run_node(
            f'import {{ resolveLang }} from {json.dumps((PLUGIN_JS / "lang.js").as_uri())};'
            "const cases = JSON.parse(await new Promise((r) => { let d = ''; process.stdin.on('data', (c) => d += c);"
            " process.stdin.on('end', () => r(d)); }));"
            "console.log(JSON.stringify(cases.map((c) => resolveLang(c))));", cases, esm=True)
        want = []
        for env in cases:
            with mock.patch.dict(os.environ, {"ORKESTRA_HOME": env["ORKESTRA_HOME"]}):
                want.append(i18n.lang(env))
        self.assertEqual(got, want)


# ================================================================================================= chat commands

class TestChatCommands(Sandbox):
    def setUp(self):
        super().setUp()
        live = mock.patch.object(B, "live_runs", return_value=[])
        live.start()
        self.addCleanup(live.stop)

    def say(self, code, text, chat="42"):
        self.speak(code)
        return B.handle_command(self.cfg, chat, text)

    def test_usage_in_both_languages(self):
        en, tr = self.say("en", "/help"), self.say("tr", "/help")
        self.assertIn("/run claude:|cursor:|copilot:|pi: <project> <request>", en)
        self.assertIn("/status", en)
        self.assertIn("/cancel", en)
        self.assertIn("The Turkish names work too: /yap, /durum, /iptal.", en)
        self.assertIsNone(TR_LETTERS.search(en))
        self.assertIn("/yap claude:|cursor:|copilot:|pi: <proje> <istek>", tr)
        self.assertIn("/durum", tr)
        self.assertIn("/iptal", tr)
        self.assertIn("İngilizce karşılıkları da çalışır: /run, /status, /cancel.", tr)

    def test_help_is_the_same_command_in_every_spelling_and_language(self):
        for code in ("en", "tr"):
            usage = i18n.catalog(code)["bridge.usage"]
            for text in ("", "   ", "/start", "/help", "/yardim", "/yardım", "/HELP", "/Yardim", "/help@orkestra_bot"):
                self.assertEqual(self.say(code, text), usage, (code, text))

    def test_run_without_a_request_shows_the_usage(self):
        for code in ("en", "tr"):
            usage = i18n.catalog(code)["bridge.usage"]
            for text in ("/run", "/yap", "/run demo", "/yap demo", "/run cursor: demo", "/yap claude: demo"):
                self.assertEqual(self.say(code, text), usage, (code, text))

    def test_started_and_start_failed(self):
        fake = {"id": "abcdef012345", "project": "demo", "engine": "claude"}
        with mock.patch.object(B, "start_job", return_value=fake) as start:
            self.assertEqual(self.say("en", "/run demo add a field"),
                             "Started: [demo] job abcdef012345. I will write here when a question or an approval is needed.")
            self.assertEqual(self.say("tr", "/yap demo alan ekle"),
                             "Başladım: [demo] iş abcdef012345. Soru ya da onay gerektiğinde buraya yazacağım.")
            self.assertEqual(self.say("en", "/yap demo add a field"), self.say("en", "/run demo add a field"))
            self.assertEqual(start.call_args[0][1:3], ("demo", "add a field"))
            fake["engine"] = "cursor"
            self.assertIn("job abcdef012345 · cursor.", self.say("en", "/run cursor demo add a field"))
            self.assertIn("iş abcdef012345 · cursor.", self.say("tr", "/yap cursor: demo alan ekle"))
        self.assertEqual(self.say("en", "/run ../etc bad"), "Could not start: invalid project name: '../etc'")
        self.assertEqual(self.say("tr", "/yap ../etc kötü"), "Başlatılamadı: geçersiz proje adı: '../etc'")

    def test_status_lists_this_chats_jobs_and_the_live_view_in_both_languages(self):
        self.job(chat_id="42", status="awaiting_user", phase="plan")
        runs = [{"project_name": "demo", "run_id": "001-x", "phase": "plan", "tasks_done": 1, "tasks_total": 3,
                 "live": {"text": "WAITING"}, "activity": []}]
        with mock.patch.object(B, "live_runs", return_value=runs):
            for word in ("/status", "/durum", "/STATUS"):
                en, tr = self.say("en", word), self.say("tr", word)
                self.assertEqual(en.splitlines()[0], "Jobs in this chat:")
                self.assertIn("aaaa11112222 · demo · awaiting_user · plan", en)
                self.assertIn("Live runs:\n• demo · 001-x · phase plan · task 1/3\n  WAITING", en)
                self.assertEqual(tr.splitlines()[0], "Bu sohbetin işleri:")
                self.assertIn("Canlı çalışmalar:\n• demo · 001-x · faz plan · görev 1/3\n  WAITING", tr)
        self.assertTrue(self.say("en", "/status", chat="7").startswith("No job has been started from this chat."))
        self.assertTrue(self.say("tr", "/durum", chat="7").startswith("Bu sohbetten başlatılmış iş yok."))
        with mock.patch.object(B, "live_runs", return_value=[]):
            self.assertIn("Live runs:\nNo open Orkestra runs.", self.say("en", "/status", chat="7"))
            self.assertIn("Canlı çalışmalar:\nAçık orkestra çalışması yok.", self.say("tr", "/durum", chat="7"))

    def test_cancel_in_both_languages(self):
        for word in ("/cancel", "/iptal"):
            self.assertEqual(self.say("en", word), "No open job.")
            self.assertEqual(self.say("tr", word), "Açık iş yok.")
        self.job()
        with mock.patch.object(B, "cancel_job") as cancel:
            self.assertEqual(self.say("en", "/cancel"),
                             "Cancelled: aaaa11112222 (files stay as they are; to undo, run /orkestra:rollback on the desktop).")
            self.assertEqual(self.say("tr", "/iptal"),
                             "İptal edildi: aaaa11112222 (dosyalar yerinde; geri almak için masaüstünde /orkestra:rollback).")
            self.assertEqual(cancel.call_count, 2)

    def test_free_text_goes_to_the_waiting_job_and_the_replies_are_localised(self):
        self.assertEqual(self.say("en", "approve"), "No job is waiting for you.\n\n" + i18n.catalog("en")["bridge.usage"])
        self.assertEqual(self.say("tr", "onay"), "Seni bekleyen bir iş yok.\n\n" + i18n.catalog("tr")["bridge.usage"])
        self.job(status="running")
        self.assertEqual(self.say("en", "approve"), "The job is still running; I will write when it finishes.")
        self.assertEqual(self.say("tr", "onay"), "İş hâlâ çalışıyor; bitince yazacağım.")
        self.job(status="awaiting_user")
        with mock.patch.object(B, "reply_job") as reply:
            self.assertEqual(self.say("en", "approve"), "Got it, continuing: [demo] job aaaa11112222.")
            self.assertEqual(self.say("tr", "onay"), "Aldım, devam ediyorum: [demo] iş aaaa11112222.")
            self.assertEqual(reply.call_args[0][2], "onay")
        with mock.patch.object(B, "reply_job", side_effect=ValueError("boom")):
            self.assertEqual(self.say("en", "approve"), "boom")

    def test_command_vocabulary_never_shrinks(self):
        """Every spelling stays a command in both languages: none of them is mistaken for an answer to a job."""
        self.job(status="awaiting_user")
        with mock.patch.object(B, "reply_job") as reply:
            for code in ("en", "tr"):
                for word in ("/yap", "/run", "/durum", "/status", "/iptal", "/cancel", "/yardim", "/help", "/start"):
                    with mock.patch.object(B, "cancel_job"):
                        self.say(code, word)
            self.assertEqual(reply.call_count, 0)
            self.say("en", "/not-a-command")                        # anything else is still an answer
            self.assertEqual(reply.call_count, 1)


# ==================================================================================================== messages

class TestMessages(Sandbox):
    def assertBoth(self, produce, en, tr):
        for code, want in (("en", en), ("tr", tr)):
            self.speak(code)
            self.assertEqual(produce(), want, code)

    def test_project_and_engine_errors(self):
        for text, en, tr in (
            ("../etc", "invalid project name: '../etc'", "geçersiz proje adı: '../etc'"),
            ("nope", f"project not found: nope (workspace: {self.ws})", f"proje bulunamadı: nope (workspace: {self.ws})"),
            ("secret-proj", "secret-proj is not in the bridge allowed_projects list",
             "secret-proj bridge allowed_projects listesinde değil"),
        ):
            def produce(text=text):
                with self.assertRaises(ValueError) as raised:
                    B.resolve_project(self.cfg, text)
                return str(raised.exception)
            self.assertBoth(produce, en, tr)

        def unknown_engine():
            with self.assertRaises(ValueError) as raised:
                B.pick_engine(self.cfg, "gemini")
            return str(raised.exception)
        self.assertBoth(unknown_engine, "unknown engine: 'gemini' (choices: claude, cursor, copilot, pi)",
                        "bilinmeyen motor: 'gemini' (seçenekler: claude, cursor, copilot, pi)")

    def test_project_list_errors_are_written_in_the_current_language(self):
        """The local UI shows these verbatim: they came out English on a Turkish page."""
        def raised(cfg):
            def produce():
                with self.assertRaises(ValueError) as caught:
                    B.ui_projects(cfg)
                return str(caught.exception)
            return produce
        self.assertBoth(raised(dict(self.cfg, workspace=str(self.tmp / "no-such-workspace"))),
                        "the bridge workspace is not available", "bridge workspace'i kullanılamıyor")
        self.assertBoth(raised(dict(self.cfg, allowed_projects="demo")),
                        "bridge allowed_projects must be a list of project names",
                        "bridge allowed_projects proje adlarından oluşan bir liste olmalı")
        self.assertBoth(raised(dict(self.cfg, allowed_projects=["demo", "ghost"])),
                        "configured project is unavailable: ghost", "yapılandırılmış proje kullanılamıyor: ghost")

    def test_job_errors(self):
        def start(request="do it"):
            with self.assertRaises(ValueError) as raised:
                B.start_job(self.cfg, "demo", request, "42")
            return str(raised.exception)
        self.assertBoth(lambda: start("x"), "the request is empty", "istek boş")
        self.job(status="awaiting_user")
        self.assertBoth(start, "demo already has an open job (aaaa11112222, awaiting_user). Finish it first or send /cancel.",
                        "demo için açık bir iş var (aaaa11112222, awaiting_user). Önce onu bitir veya /iptal.")

        def reply(status):
            def produce():
                with self.assertRaises(ValueError) as raised:
                    B.reply_job(self.cfg, {"id": "x", "status": status}, "hi")
                return str(raised.exception)
            return produce
        self.assertBoth(reply("running"), "the job is still running; I will write to you when it finishes",
                        "iş hâlâ çalışıyor; bitince sana yazacağım")
        self.assertBoth(reply("done"), "the job is closed (done); use /run to start a new one",
                        "iş kapandı (done); yeni iş için /yap kullan")

    def test_engine_readiness_reasons(self):
        with mock.patch.object(B, "cursor_bin", return_value=None):
            self.assertBoth(lambda: B.cursor_ready(self.cfg),
                            "Cursor CLI (agent) not found: https://cursor.com/cli (or set cursor_bin in bridge.secret.json)",
                            "Cursor CLI (agent) bulunamadı: https://cursor.com/cli (ya da bridge.secret.json → cursor_bin)")
        with mock.patch.object(B, "cursor_bin", return_value="agent"), \
                mock.patch("orkestra_core.cursorkit.hooks_active", return_value=False):
            self.speak("en")
            self.assertIn("orkestra cursor install", B.cursor_ready(self.cfg))
            self.assertTrue(B.cursor_ready(self.cfg).startswith("Orkestra hooks are not installed in Cursor"))
            self.speak("tr")
            self.assertTrue(B.cursor_ready(self.cfg).startswith("Cursor'da orkestra hook'ları kurulu değil"))
        with mock.patch.object(B, "copilot_bin", return_value=None):
            self.assertBoth(lambda: B.copilot_ready(self.cfg), "Copilot CLI not found (check copilot_bin or PATH)",
                            "Copilot CLI bulunamadı (copilot_bin veya PATH'i denetle)")
        with mock.patch.object(B, "copilot_bin", return_value="copilot"), \
                mock.patch.object(B.EN, "copilot_hooks_active", return_value=False), \
                mock.patch.object(B, "copilot_options", return_value={"plugin_dir": ""}):
            self.speak("en")
            self.assertIn("UserPromptSubmit, PreToolUse and PostToolUse hooks are required", B.copilot_ready(self.cfg))
            self.speak("tr")
            self.assertIn("hook'ları gerekir", B.copilot_ready(self.cfg))

    def test_engines_endpoint_reason_follows_the_language(self):
        with mock.patch.object(B.EN, "binary", return_value=None), mock.patch.object(B, "cursor_ready", return_value=""), \
                mock.patch.object(B, "copilot_ready", return_value=""):
            self.speak("en")
            self.assertEqual(B.ui_engines(self.cfg)["engines"][0]["reason"], "Claude Code CLI not found")
            self.speak("tr")
            self.assertEqual(B.ui_engines(self.cfg)["engines"][0]["reason"], "Claude Code CLI bulunamadı")

    def test_live_text(self):
        runs = [{"project_name": "demo", "run_id": "001", "phase": "plan", "tasks_total": 0, "live": {"text": "ok"},
                 "activity": [{"time": "10:11:12", "agent": "main", "tool": "Read", "what": "x.py"}]}]
        self.assertBoth(lambda: B.live_text([]), "No open Orkestra runs.", "Açık orkestra çalışması yok.")
        self.assertBoth(lambda: B.live_text(runs), "• demo · 001 · phase plan\n  ok\n  10:11 main Read x.py",
                        "• demo · 001 · faz plan\n  ok\n  10:11 main Read x.py")
        long = [dict(runs[0], live={"text": "y" * 5000})]
        self.assertTrue(B.live_text(long).endswith("…"))
        self.assertLessEqual(len(B.live_text(long)), 3500)

    def test_notification_text_and_labels(self):
        job = {"id": "aaaa11112222", "project": "demo", "status": "awaiting_user", "phase": "plan", "engine": "claude",
               "last_message": "Which one?"}
        self.assertBoth(lambda: B.notification_text(job),
                        "[demo] ⏸ Waiting for your reply · plan · job aaaa11112222\n\nWhich one?",
                        "[demo] ⏸ Yanıtını bekliyor · plan · iş aaaa11112222\n\nWhich one?")
        self.assertBoth(lambda: B.notification_text(dict(job, status="done", phase=None, engine="cursor", last_message="")),
                        "[demo] ✅ Done · job aaaa11112222 · cursor\n\n", "[demo] ✅ Tamamlandı · iş aaaa11112222 · cursor\n\n")
        want = {"en": {"awaiting_user": "⏸ Waiting for your reply", "done": "✅ Done", "blocked": "⛔ Blocked",
                       "error": "❌ Error", "cancelled": "🛑 Cancelled", "paused": "⏸ Paused"},
                "tr": {"awaiting_user": "⏸ Yanıtını bekliyor", "done": "✅ Tamamlandı", "blocked": "⛔ Engellendi",
                       "error": "❌ Hata", "cancelled": "🛑 İptal", "paused": "⏸ Duraklatıldı"}}
        for code in ("en", "tr"):
            self.speak(code)
            self.assertEqual({s: B.status_label(s) for s in B.NOTIFY_LABELS}, want[code])
            self.assertEqual(B.status_label("weird"), "weird")

    def test_the_notification_header_is_what_the_openclaw_plugin_parses(self):
        """OpenClaw routes a Telegram reply by `[project] ... <job|iş> <12 hex>`: both spellings, in any language."""
        route = re.compile(r"\[([A-Za-z0-9._-]{1,80})\][^\n]*?\b(?:iş|job)\s+([a-f0-9]{12})\b")
        job = {"id": "0123456789ab", "project": "my-app", "status": "awaiting_user", "phase": "clarify", "engine": "cursor"}
        for code in ("en", "tr"):
            self.speak(code)
            self.assertEqual(route.search(B.notification_text(job)).groups(), ("my-app", "0123456789ab"))
            with mock.patch.object(B, "start_job", return_value=dict(job, project="my-app", engine="claude")):
                self.assertEqual(route.search(B.handle_command(self.cfg, "42", "/run demo hi")).groups(),
                                 ("my-app", "0123456789ab"))

    def test_notify_sends_the_same_text_to_the_callback_and_to_telegram_with_stable_keys(self):
        job = self.job(status="awaiting_user", phase="plan", last_message="Which one?")
        cfg = dict(self.cfg, callback_url="http://127.0.0.1:1/cb", telegram={"bot_token": "T", "allowed_chat_ids": ["42"]})
        for code in ("en", "tr"):
            self.speak(code)
            with mock.patch.object(B, "post_json") as post, mock.patch.object(B, "send_telegram") as send:
                B.notify(cfg, job)
            payload = post.call_args[0][1]
            self.assertEqual(sorted(payload), sorted(["id", "project", "status", "phase", "run_id", "chat_id", "created",
                                                      "updated", "last_message", "engine", "message"]))
            self.assertEqual(payload["status"], "awaiting_user")                        # the enum never changes
            self.assertEqual(payload["message"], B.notification_text(job))
            self.assertEqual(send.call_args[0][2], payload["message"])

    def test_the_text_sent_to_the_model_stays_english_in_every_language(self):
        """The note on a request from an external trigger is model-facing: English, whatever the UI language."""
        sent = []

        def run_turn(cfg, job, prompt, new, said=None):
            sent.append((job["source"], prompt))

        def close_jobs():
            for existing in B.list_jobs():
                B.save_job(dict(existing, status="done"))
            demo = Path(self.cfg["workspace"]) / "demo"
            from orkestra_core.state import project_active_run
            if project_active_run(demo):
                subprocess.run(
                    [sys.executable, str(B.ORKESTRA_BIN), "abort", "i18n-test-cleanup", "-p", str(demo)],
                    capture_output=True, text=True, env=os.environ.copy(),
                )

        with mock.patch.object(B, "run_turn", side_effect=run_turn):
            for code in ("en", "tr"):
                self.speak(code)
                for source in ("n8n", "web-ui", "telegram", "api", "openclaw", "cli"):
                    close_jobs()
                    B.start_job(self.cfg, "demo", "add a field", "42", source)
        self.assertEqual(len(sent), 12)
        for source, prompt in sent:
            self.assertIsNone(TR_LETTERS.search(prompt), prompt)
            self.assertIn("add a field", prompt)
            if source in ("n8n", "web-ui"):
                self.assertIn(f"[This request came from an external trigger (source={source}).", prompt)
            else:
                self.assertNotIn("external trigger", prompt, source)
        self.assertEqual(sum("external trigger" in prompt for _, prompt in sent), 4)


class TestTurnMessages(Sandbox):
    def setUp(self):
        super().setUp()
        notify = mock.patch.object(B, "notify")
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def last_message(self, job):
        return B.load_job(job["id"])["last_message"]

    def wait_for(self, job_id, status, timeout=30):
        deadline = time.time() + timeout
        job = {}
        while time.time() < deadline:
            job = B.load_job(job_id) or {}
            if job.get("status") == status:
                return job
            time.sleep(0.1)
        self.fail(f"job {job_id} never reached {status}: {job}")

    def test_a_missing_engine_binary(self):
        cfg = dict(self.cfg, claude_bin=str(self.tmp / "no-such-claude"))
        for code, want in (("en", "claude not found: "), ("tr", "claude bulunamadı: ")):
            self.speak(code)
            job = self.job(status="awaiting_user")
            B.run_turn(cfg, job, "hello", new=False)
            saved = B.load_job(job["id"])
            self.assertEqual(saved["status"], "error")
            self.assertEqual(saved["last_message"], want + str(self.tmp / "no-such-claude"))

    def test_an_engine_that_exits_with_an_error(self):
        # `python -p ...` is refused by the interpreter (exit 2, a usage error on stderr): a portable failing "claude"
        cfg = dict(self.cfg, claude_bin=sys.executable)
        for code, head in (("en", "claude exited with code 2: "), ("tr", "claude çıkış kodu 2: ")):
            self.speak(code)
            job = self.job(status="awaiting_user")
            B.run_turn(cfg, job, "hello", new=False)
            saved = self.wait_for(job["id"], "error")
            self.assertTrue(saved["last_message"].startswith(head), saved["last_message"])
            self.assertIn("Unknown option", saved["last_message"])                       # the interpreter's own words

    def test_a_turn_that_cannot_start_for_cursor_and_copilot(self):
        for engine, patch_ready, en, tr in (
                ("cursor", "cursor_ready", "Cursor turn not started: why not", "Cursor turu başlatılmadı: why not"),
                ("copilot", "copilot_ready", "Copilot turn not started: why not", "Copilot turu başlatılmadı: why not")):
            for code, want in (("en", en), ("tr", tr)):
                self.speak(code)
                job = self.job(engine=engine, status="awaiting_user")
                with mock.patch.object(B, patch_ready, return_value="why not"):
                    B.run_turn(self.cfg, job, "hello", new=False)
                self.assertEqual(self.last_message(job), want)

    def test_the_cursor_driver_refusing_us(self):
        job = self.job(engine="cursor", status="awaiting_user")
        with mock.patch.object(B, "cursor_ready", return_value=""), mock.patch.object(B.EN, "driver_prompt", return_value=None):
            self.speak("en")
            B.run_turn(self.cfg, job, "hello", new=False)
            en = self.last_message(job)
            self.speak("tr")
            B.run_turn(self.cfg, job, "hello", new=False)
            tr = self.last_message(job)
        self.assertTrue(en.startswith("Cursor turn not started: the bridge seems to have been started from inside an agent shell"))
        self.assertIsNone(TR_LETTERS.search(en))
        self.assertTrue(tr.startswith("Cursor turu başlatılmadı: bridge bir ajan kabuğunun içinden başlatılmış görünüyor"))
        self.assertEqual(B.load_job(job["id"])["status"], "awaiting_user")

    def test_serve_restart_notice(self):
        for code, want in (("en", "(The bridge restarted; send a message to continue the job.)"),
                           ("tr", "(Bridge yeniden başladı; işi sürdürmek için bir mesaj yaz.)")):
            self.speak(code)
            self.job(status="running")
            with mock.patch.object(B, "ThreadingHTTPServer", side_effect=RuntimeError("stop here")):
                with self.assertRaises(RuntimeError):
                    B.serve(dict(self.cfg, token="t"))
            saved = B.load_job("aaaa11112222")
            self.assertEqual((saved["status"], saved["last_message"]), ("awaiting_user", want))


# ===================================================================================== the CLIs of the bridge

class TestCliOutput(Sandbox):
    def capture(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = fn(*args)
        return result, out.getvalue()

    def test_help_in_both_languages(self):
        for code, head, snippet in (("en", "Orkestra bridge: lets n8n, Telegram or any webhook", "Local UI: http://127.0.0.1:47615/"),
                                    ("tr", "Orkestra bridge: n8n, Telegram ya da herhangi bir webhook", "Yerel arayüz: http://127.0.0.1:47615/")):
            self.speak(code)
            for argv in (["orkestra_bridge.py", "--help"], ["orkestra_bridge.py", "help"], ["orkestra_bridge.py", "nonsense"]):
                with mock.patch.object(sys, "argv", argv):
                    rc, out = self.capture(B.main)
                self.assertEqual(rc, 0)
                self.assertTrue(out.startswith(head), out)
                self.assertIn(snippet, out)
        self.speak("en")
        with mock.patch.object(sys, "argv", ["orkestra_bridge.py", "--help"]):
            self.assertIsNone(TR_LETTERS.search(self.capture(B.main)[1]))

    def test_init_prints_the_config_in_both_languages(self):
        for code, want in (("en", "config:   "), ("tr", "yapılandırma: ")):
            self.speak(code)
            _, out = self.capture(B.init)
            self.assertTrue(out.startswith(want + str(B.CONFIG_PATH)), out)
            self.assertIn("token:", out)
            self.assertIn("telegram-setup", out)
        cfg = json.loads(B.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertTrue(cfg["token"])
        if os.name != "nt":
            self.assertEqual(B.CONFIG_PATH.stat().st_mode & 0o777, 0o600)
            self.assertIn("(chmod 600)", out)

    def test_serve_refuses_in_both_languages(self):
        for code, no_token, loopback in (
                ("en", "no token configured: run: python3 orkestra_bridge.py init",
                 "refusing to bind a non-loopback host (expose it through an authenticated tunnel instead)"),
                ("tr", "token yapılandırılmamış, önce çalıştır: python3 orkestra_bridge.py init",
                 "loopback olmayan bir adrese bağlanmayı reddediyorum (bunun yerine kimlik doğrulamalı bir tünel kullan)")):
            self.speak(code)
            with self.assertRaises(SystemExit) as raised:
                B.serve({"token": ""})
            self.assertEqual(raised.exception.code, no_token)
            with self.assertRaises(SystemExit) as raised:
                B.serve({"token": "t", "host": "0.0.0.0"})
            self.assertEqual(raised.exception.code, loopback)

    def test_launchd_is_macos_only_and_says_what_to_do_elsewhere(self):
        fake_home = self.tmp / "fake-home"
        fake_home.mkdir()
        for code in ("en", "tr"):
            self.speak(code)
            with mock.patch.dict(os.environ, {"HOME": str(fake_home), "USERPROFILE": str(fake_home)}), \
                    mock.patch.object(B.sys, "platform", "linux"):
                rc, out = self.capture(B.launchd)
            self.assertEqual(rc, 1)
            self.assertIn(f"{sys.executable} {BRIDGE} serve", out)
            self.assertIn("launchd", out)
        self.assertEqual(list(fake_home.iterdir()), [], "nothing is written where there is no launchd")

    @unittest.skipIf(os.name == "nt", "the LaunchAgent path is a macOS path")
    def test_launchd_writes_the_agent_on_macos(self):
        fake_home = self.tmp / "fake-home"
        fake_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(fake_home), "USERPROFILE": str(fake_home)}), \
                mock.patch.object(B.sys, "platform", "darwin"):
            self.speak("en")
            rc, out = self.capture(B.launchd)
        plist = fake_home / "Library" / "LaunchAgents" / "com.orkestra.bridge.plist"
        self.assertEqual(rc, 0)
        self.assertIn(f"wrote {plist}", out)
        self.assertIn("launchctl load -w", out)
        self.assertIn(str(BRIDGE), plist.read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "the LaunchAgent path is a macOS path")
    def test_launchd_agent_carries_the_language_launchd_would_drop(self):
        for code, locale in (("tr", "tr_TR.UTF-8"), ("en", "en_US.UTF-8")):
            fake_home = self.tmp / f"fake-home-{code}"
            fake_home.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(fake_home), "USERPROFILE": str(fake_home)}), \
                    mock.patch.object(B.sys, "platform", "darwin"):
                self.speak(code)
                rc, _ = self.capture(B.launchd)
            self.assertEqual(rc, 0)
            text = (fake_home / "Library" / "LaunchAgents" / "com.orkestra.bridge.plist").read_text(encoding="utf-8")
            self.assertIn(f"<key>LANG</key><string>{locale}</string>", text)
            self.assertIn("<key>PATH</key>", text)

    def test_cancel_interrupts_with_a_signal_where_there_is_one_and_terminates_on_windows(self):
        proc = mock.Mock()
        with mock.patch.object(B.sys, "platform", "win32"):
            B._interrupt(proc)
        proc.terminate.assert_called_once_with()
        proc.send_signal.assert_not_called()
        proc = mock.Mock()
        with mock.patch.object(B.sys, "platform", "linux"):
            B._interrupt(proc)
        proc.send_signal.assert_called_once_with(B.signal.SIGINT)
        proc.terminate.assert_not_called()

    def test_stdio_never_dies_on_a_character_the_console_cannot_encode(self):
        for name in ("stdout", "stderr"):
            stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
            with mock.patch.object(sys, name, stream):
                B.safe_stdio()
                print("Turkish İ ş ğ and an emoji ✅", file=stream)
                stream.flush()
                self.assertIn(b"Turkish ? ? ? and an emoji ?", stream.buffer.getvalue())

    def test_telegram_setup_messages_and_the_yes_words_of_both_languages(self):
        def setup(code, answer, argv=("--token", "T")):
            self.speak(code)
            with mock.patch.object(B, "openclaw_uses_telegram", return_value=True), \
                    mock.patch("builtins.input", return_value=answer), \
                    mock.patch.object(B, "_tg", side_effect=RuntimeError("boom")):
                return self.capture(B.telegram_setup, list(argv))

        rc, out = setup("en", "n")
        self.assertEqual(rc, 1)
        self.assertIn("WARNING: OpenClaw has a Telegram configuration.", out)
        self.assertNotIn("Could not verify", out)
        rc, out = setup("tr", "")
        self.assertEqual(rc, 1)
        self.assertIn("UYARI: OpenClaw'da bir Telegram yapılandırması var.", out)
        for answer in ("y", "yes", "e", "evet", "Y", " Evet "):                      # either language says yes
            rc, out = setup("en", answer)
            self.assertIn("Could not verify the token: boom", out, answer)
        rc, out = setup("tr", "e")
        self.assertIn("Token doğrulanamadı: boom", out)
        prompts = []
        for code in ("en", "tr"):
            self.speak(code)
            with mock.patch.object(B, "openclaw_uses_telegram", return_value=True), \
                    mock.patch("builtins.input", side_effect=lambda prompt: prompts.append(prompt) or "n"):
                self.capture(B.telegram_setup, ["--token", "T"])
        self.assertEqual(prompts, ["Continue with a separate bot? [y/N] ", "Ayrı bir bot ile devam edilsin mi? [e/H] "])

    def test_telegram_setup_refuses_a_bot_that_has_a_webhook(self):
        def tg(token, method, params=None, timeout=40):
            return {"result": {"username": "bot"}} if method == "getMe" else {"result": {"url": "https://hooks.example/x"}}
        for code, want in (("en", "This bot has a webhook (hooks.example): another program (for example OpenClaw) is using it."),
                           ("tr", "Bu botun bir webhook'u var (hooks.example): başka bir program (ör. OpenClaw) onu kullanıyor.")):
            self.speak(code)
            with mock.patch.object(B, "openclaw_uses_telegram", return_value=False), mock.patch.object(B, "_tg", side_effect=tg):
                rc, out = self.capture(B.telegram_setup, ["--token", "T"])
            self.assertEqual(rc, 1)
            self.assertIn(want, out)
            self.assertIn("--force", out)


# =============================================================================== subprocess: client, doctor, n8n

class TestSubprocessCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-i18n-cli-")).resolve()
        cls.home = cls.tmp / "home"
        cls.fake_home = cls.tmp / "user-home"
        cls.home.mkdir()
        cls.fake_home.mkdir()
        cls.port = free_port()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def env(self, code, **extra):
        env = {k: v for k, v in os.environ.items() if k not in LANG_VARS}
        env.update(ORKESTRA_HOME=str(self.home), HOME=str(self.fake_home), USERPROFILE=str(self.fake_home),
                   ORKESTRA_WORKSPACE=str(self.tmp / "Projects"))
        if code:
            env["ORKESTRA_LANG"] = code
        env.update(extra)
        return env

    def run_py(self, script, args, code, **extra):
        return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, encoding="utf-8",
                              env=self.env(code, **extra), stdin=subprocess.DEVNULL, timeout=120)

    def test_client_usage_and_errors_in_both_languages(self):
        cp = self.run_py(CLIENT, [], "en")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("Tiny client for the Orkestra bridge", cp.stdout)
        self.assertIn("--engine claude|cursor|copilot|pi", cp.stdout)
        self.assertIsNone(TR_LETTERS.search(cp.stdout))
        cp = self.run_py(CLIENT, ["--help"], "tr")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("Orkestra bridge için küçük istemci", cp.stdout)
        for code, want in (("en", "bridge not configured: run: python3 "), ("tr", "bridge yapılandırılmamış, önce çalıştır: python3 ")):
            cp = self.run_py(CLIENT, ["status"], code)
            self.assertEqual(cp.returncode, 1)
            self.assertIn(want, cp.stderr)
            self.assertIn("orkestra_bridge.py init", cp.stderr)
        (self.home / "bridge.secret.json").write_text(json.dumps({"port": free_port(), "token": "t"}), encoding="utf-8")
        for code, want in (("en", "bridge not reachable ("), ("tr", "bridge'e ulaşılamıyor (")):
            cp = self.run_py(CLIENT, ["status"], code)
            self.assertEqual(cp.returncode, 1)
            self.assertIn(want, json.loads(cp.stdout)["error"])
        (self.home / "bridge.secret.json").unlink()

    def test_client_survives_a_console_that_cannot_encode_turkish(self):
        """A piped stdout in a legacy code page (PYTHONIOENCODING) must not turn the usage into a traceback."""
        cp = subprocess.run([sys.executable, str(CLIENT), "--help"], capture_output=True, env=self.env("tr", PYTHONIOENCODING="ascii"),
                            stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(cp.returncode, 2, cp.stderr)
        self.assertIn(b"Orkestra bridge i", cp.stdout)
        self.assertNotIn(b"Traceback", cp.stderr)

    def test_doctor_in_both_languages(self):
        (self.home / "bridge.secret.json").write_text(json.dumps({
            "port": self.port, "token": "t", "workspace": str(self.tmp / "Projects"), "claude_bin": sys.executable,
            "telegram": {"bot_token": "", "allowed_chat_ids": []}}), encoding="utf-8")
        en = self.run_py(BRIDGE, ["doctor"], "en")
        self.assertEqual(en.returncode, 1, en.stdout + en.stderr)
        for line in (f"[OK] config: {self.home / 'bridge.secret.json'}", "[OK] bridge token", f"[OK] claude CLI: {sys.executable}",
                     "[!!] Orkestra plugin installed: run install.sh", "[!!] bridge not running: ",
                     "[--] Telegram is not configured (python3 orkestra_bridge.py telegram-setup)", "doctor: gaps found (see above)"):
            self.assertIn(line, en.stdout)
        self.assertRegex(en.stdout, r"\[(OK|--)\] (Cursor engine|no Cursor engine)")
        self.assertIsNone(TR_LETTERS.search(en.stdout), en.stdout)
        tr = self.run_py(BRIDGE, ["doctor"], "tr")
        self.assertEqual(tr.returncode, 1, tr.stdout + tr.stderr)
        for line in ("[!!] orkestra plugin kurulu: install.sh çalıştır", "[!!] bridge çalışmıyor: ",
                     "[--] telegram yapılandırılmadı (python3 orkestra_bridge.py telegram-setup)", "doctor: eksikler var (yukarı bak)"):
            self.assertIn(line, tr.stdout)
        self.assertRegex(tr.stdout, r"\[(OK|--)\] (cursor motoru|cursor motoru yok)")

    def test_the_n8n_workflows_are_english_and_consistent(self):
        spec = importlib.util.spec_from_file_location("n8n_import_under_test", N8N_IMPORT)
        n8n = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(n8n)
        names = set()
        for path in sorted((BRIDGE_DIR / "n8n").glob("*.json")):
            workflow = json.loads(path.read_text(encoding="utf-8"))
            names.add(workflow["name"])
            nodes = {node["name"] for node in workflow["nodes"]}
            self.assertEqual(len(nodes), len(workflow["nodes"]), "node names must be unique")
            for source, outputs in workflow["connections"].items():
                self.assertIn(source, nodes, path.name)
                for branch in outputs["main"]:
                    for link in branch:
                        self.assertIn(link["node"], nodes, path.name)
            self.assertNotIn(EM_DASH, path.read_text(encoding="utf-8"))
        self.assertEqual(names, set(n8n.LEGACY_NAMES), "every workflow has its legacy (Turkish) name registered")

    def test_n8n_import_updates_old_workflows_in_place_and_speaks_the_language(self):
        spec = importlib.util.spec_from_file_location("n8n_import_flow", N8N_IMPORT)
        n8n = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(n8n)
        legacy = {name: old for name, olds in n8n.LEGACY_NAMES.items() for old in olds}
        cfg_path = self.tmp / "n8n-bridge.secret.json"
        cfg_path.write_text(json.dumps({"token": "bridge-token", "n8n_webhook_key": "k"}), encoding="utf-8")

        def run(existing, code):
            calls = []

            def api(path, method="GET", body=None, key=""):
                calls.append((method, path, (body or {}).get("name")))
                if path.startswith("/api/v1/workflows?"):
                    return {"data": [{"name": n, "id": i} for n, i in existing.items()]}
                if path == "/api/v1/credentials":
                    return {"id": "cred1"}
                if method == "POST" and path == "/api/v1/workflows":
                    return {"id": f"new{len([c for c in calls if c[0] == 'POST' and c[1] == '/api/v1/workflows'])}"}
                return {}
            out = io.StringIO()
            with mock.patch.object(n8n, "api", api), mock.patch.object(n8n, "BRIDGE_CFG", cfg_path), \
                    mock.patch.dict(os.environ, {"N8N_API_KEY": "key", "ORKESTRA_LANG": code}), contextlib.redirect_stdout(out):
                self.assertEqual(n8n.main(), 0)
            return calls, out.getvalue()

        existing = {old: f"old{i}" for i, old in enumerate(legacy.values())}
        for code, updated in (("en", "updated: Orkestra · Start a job via webhook (old"), ("tr", "güncellendi: Orkestra · Start a job via webhook (old")):
            calls, out = run(existing, code)
            puts = [c for c in calls if c[0] == "PUT"]
            self.assertEqual(len(puts), 2, calls)
            self.assertEqual(sorted(c[2] for c in puts), sorted(n8n.LEGACY_NAMES))          # renamed to the English names
            self.assertEqual([c for c in calls if c[0] == "POST" and c[1] == "/api/v1/workflows"], [], "no duplicate workflow")
            self.assertIn(updated, out)
            self.assertIn("/webhook/orkestra-callback", out)
        calls, out = run({}, "en")
        self.assertEqual(len([c for c in calls if c[0] == "POST" and c[1] == "/api/v1/workflows"]), 2)
        self.assertIn("created: Orkestra · Start a job via webhook (new1)", out)
        self.assertIn(f"active → POST {n8n.N8N_URL}/webhook/orkestra-job", out)
        calls, out = run({name: "same" for name in n8n.LEGACY_NAMES}, "tr")                  # already English: found by name
        self.assertEqual(len([c for c in calls if c[0] == "PUT"]), 2)
        self.assertIn("güncellendi:", out)

    def test_n8n_import_errors_in_both_languages(self):
        for code, want in (("en", "Set N8N_API_KEY, or N8N_STATE to a JSON file with an apiKey field"),
                           ("tr", "N8N_API_KEY ayarla ya da apiKey alanı olan bir JSON dosyasını N8N_STATE ile göster")):
            cp = self.run_py(N8N_IMPORT, [], code, N8N_API_KEY="", N8N_STATE="")
            self.assertEqual(cp.returncode, 1)
            self.assertIn(want, cp.stderr)
        cp = self.run_py(N8N_IMPORT, [], "en", N8N_API_KEY="", N8N_STATE=str(self.tmp / "missing.json"))
        self.assertIn("missing file: ", cp.stderr)


# ===================================================================================== a real bridge, end to end

class Receiver(BaseHTTPRequestHandler):
    got = None

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        type(self).got.append(json.loads(self.rfile.read(n)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class LiveBridge:
    """One bridge subprocess (fake claude) with its own home, workspace and callback receiver."""

    def __init__(self, tmp, name, language, extra_env=None, config_lang=None):
        self.home = tmp / f"{name}-home"
        self.ws = tmp / f"{name}-Projects"
        self.home.mkdir()
        for project in ("demo", "demo2", "demo3", "secret-proj"):
            (self.ws / project).mkdir(parents=True)
            (self.ws / project / "AGENTS.md").write_text(f"# {project}\n", encoding="utf-8")
            git_init(self.ws / project)
        self.token = f"token-{name}"
        self.port = free_port()
        self.callbacks = []
        handler = type("Receiver", (Receiver,), {"got": self.callbacks})
        self.receiver = HTTPServer(("127.0.0.1", free_port()), handler)
        threading.Thread(target=self.receiver.serve_forever, daemon=True).start()
        (self.home / "bridge.secret.json").write_text(json.dumps({
            "port": self.port, "token": self.token, "workspace": str(self.ws), "claude_bin": str(FAKE_CLAUDE),
            "callback_url": f"http://127.0.0.1:{self.receiver.server_address[1]}/cb", "allowed_projects": ["demo", "demo2", "demo3"],
            "watchdog_interval": 0, "auto_resume_delays": [0, 0, 0], "telegram": {"bot_token": "", "allowed_chat_ids": ["42", "43", "77"]}}),
            encoding="utf-8")
        if config_lang:
            (self.home / "config.json").write_text(json.dumps({"lang": config_lang}), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in LANG_VARS + ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID",
                                                                              "ORKESTRA_HEADLESS")}
        env.update(ORKESTRA_HOME=str(self.home), ORKESTRA_WORKSPACE=str(self.ws))
        # hermetic: what the engines report must not depend on a Cursor / Copilot installed on the developer's machine
        env.update(ORKESTRA_CURSOR_HOME=str(tmp / f"{name}-cursor-home"), ORKESTRA_SHIM_DIR=str(tmp / f"{name}-shim"),
                   ORKESTRA_CURSOR_BIN=str(tmp / "no-such-cursor-agent"), COPILOT_HOME=str(tmp / f"{name}-copilot-home"))
        if language:
            env["ORKESTRA_LANG"] = language
        env.update(extra_env or {})
        self.proc = subprocess.Popen([sys.executable, str(BRIDGE), "serve"], env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1).close()
                break
            except Exception:
                time.sleep(0.1)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            stream.close()
        self.receiver.shutdown()
        self.receiver.server_close()

    def call(self, method, path, body=None, token=True, raw=False):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                payload = r.read()
                return r.status, (payload.decode("utf-8") if raw else json.loads(payload or b"{}"))
        except urllib.error.HTTPError as e:
            payload = e.read()
            e.close()
            return e.code, (payload.decode("utf-8") if raw else json.loads(payload or b"{}"))

    def say(self, text, chat="42"):
        code, body = self.call("POST", "/telegram", {"chat_id": chat, "text": text})
        assert code == 200, (code, body)
        return body["message"]

    def wait_status(self, job_id, status, timeout=40):
        deadline = time.time() + timeout
        job = {}
        while time.time() < deadline:
            job = self.call("GET", f"/jobs/{job_id}")[1]
            if job.get("status") == status:
                return job
            time.sleep(0.2)
        raise AssertionError(f"job {job_id} never reached {status}; last: {job}")

    def job_file(self, job_id):
        return json.loads((self.home / "bridge" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))


JOB_KEYS = ["chat_id", "created", "engine", "id", "last_message", "phase", "project", "run_id", "status", "updated"]


class TestEnglishBridgeEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-i18n-live-")).resolve()
        cls.en = LiveBridge(cls.tmp, "en", "en")
        cls.tr = LiveBridge(cls.tmp, "tr", "tr")

    @classmethod
    def tearDownClass(cls):
        cls.en.stop()
        cls.tr.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_health_reports_the_language_and_the_api_codes_do_not_change(self):
        for bridge, code in ((self.en, "en"), (self.tr, "tr")):
            self.assertEqual(bridge.call("GET", "/health", token=False), (200, {"ok": True, "lang": code}))
            self.assertEqual(bridge.call("GET", "/jobs", token=False), (401, {"error": "unauthorized"}))
            self.assertEqual(bridge.call("GET", "/jobs/ffffffffffff"), (404, {"error": "not found"}))
            self.assertEqual(bridge.call("POST", "/telegram", {"chat_id": "999", "text": "/help"}), (403, {"error": "chat not allowed"}))
            self.assertEqual(bridge.call("POST", "/reply", {"chat_id": "nobody", "text": "x"})[0], 409)

    def test_human_sentences_of_the_api_follow_the_language_the_request_asks_for(self):
        """Stopping a card that just finished, or replying with nothing to reply to, showed an English sentence on a
        Turkish page: the sentence is written in the language of the request (the code-like errors stay as they are)."""
        for bridge in (self.en, self.tr):
            (bridge.home / "bridge" / "jobs").mkdir(parents=True, exist_ok=True)
            (bridge.home / "bridge" / "jobs" / "abcdef000009.json").write_text(json.dumps({
                "id": "abcdef000009", "project": "demo", "status": "done", "chat_id": "42", "created": "2026-10-12T20:00:00+0000"}),
                encoding="utf-8")
            for lang in ("en", "tr"):
                code, body = bridge.call("POST", f"/jobs/abcdef000009/cancel?lang={lang}", {})
                self.assertEqual(code, 409, body)
                self.assertIn("done", body["error"])
                self.assertEqual(bool(TR_LETTERS.search(body["error"])), lang == "tr", body)
                code, body = bridge.call("POST", f"/reply?lang={lang}", {"chat_id": "nobody", "text": "x"})
                self.assertEqual(code, 409, body)
                self.assertEqual(bool(TR_LETTERS.search(body["error"])), lang == "tr", body)
        self.assertEqual(self.en.call("POST", "/jobs/abcdef000009/cancel", {})[1]["error"],
                         "the job is closed (done); use /run to start a new one")

    def test_the_local_ui_ships_its_language_table(self):
        for bridge in (self.en, self.tr):
            code, html = bridge.call("GET", "/", token=False, raw=True)
            self.assertEqual(code, 200)
            self.assertLess(html.index('src="/i18n.js"'), html.index('src="/app.js"'), "the table loads before the app")
            code, js = bridge.call("GET", "/i18n.js", token=False, raw=True)
            self.assertEqual(code, 200)
            self.assertIn("OrkestraI18n", js)

    def test_a_config_change_switches_a_running_bridge(self):
        """No ORKESTRA_LANG in the environment: the language comes from config.json and follows it live."""
        live = LiveBridge(self.tmp, "cfg", None, extra_env={"LANG": "en_US.UTF-8"})
        self.addCleanup(live.stop)
        self.assertEqual(live.call("GET", "/health", token=False)[1]["lang"], "en")
        self.assertIn("/run <project> <request>", live.say("/help"))
        (live.home / "config.json").write_text(json.dumps({"lang": "tr"}), encoding="utf-8")
        self.assertEqual(live.call("GET", "/health", token=False)[1]["lang"], "tr")
        self.assertIn("/yap <proje> <istek>", live.say("/help"))
        (live.home / "config.json").write_text(json.dumps({"lang": "auto"}), encoding="utf-8")
        self.assertEqual(live.call("GET", "/health", token=False)[1]["lang"], "en")          # the locale (en_US) takes over

    def test_a_request_can_ask_for_the_language_of_the_texts_the_server_writes(self):
        """The UI's language button used to switch only its own labels: the live status, the engine reasons and the
        errors stayed in the bridge's language. `?lang=` makes the server answer that one request in the other one."""
        for bridge, own, other in ((self.tr, "tr", "en"), (self.en, "en", "tr")):
            code, engines = bridge.call("GET", f"/engines?project=demo&lang={other}")
            self.assertEqual(code, 200)
            cursor = next(e for e in engines["engines"] if e["id"] == "cursor")["reason"]
            plain = next(e for e in bridge.call("GET", "/engines?project=demo")[1]["engines"] if e["id"] == "cursor")["reason"]
            self.assertEqual(bool(TR_LETTERS.search(cursor)), other == "tr", cursor)
            self.assertEqual(bool(TR_LETTERS.search(plain)), own == "tr", plain)
            code, body = bridge.call("POST", f"/jobs?lang={other}", {"project": "nope", "request": "do it"})
            self.assertEqual(code, 400)
            self.assertTrue(body["error"].startswith("proje bulunamadı" if other == "tr" else "project not found"), body)
            self.assertEqual(bridge.call("GET", "/health?lang=" + other, token=False)[1]["lang"], own,
                             "/health says what the bridge speaks, not what one request asked")
        # a value nobody supports changes nothing, and so does a missing one
        self.assertTrue(self.tr.call("POST", "/jobs?lang=de", {"project": "nope", "request": "x"})[1]["error"]
                        .startswith("proje bulunamadı"))
        self.assertTrue(self.en.call("POST", "/jobs?lang=", {"project": "nope", "request": "x"})[1]["error"]
                        .startswith("project not found"))

    def test_the_live_view_is_written_in_the_language_the_request_asks_for(self):
        live = LiveBridge(self.tmp, "perreq", "tr")
        self.addCleanup(live.stop)
        code, job = live.call("POST", "/jobs", {"project": "demo", "request": "add a priority field; let me decide", "chat_id": "42"})
        self.assertEqual(code, 202, job)
        live.wait_status(job["id"], "awaiting_user")
        texts = {}
        for query in ("", "?lang=en", "?lang=tr", "?lang=de"):
            code, body = live.call("GET", "/runs" + query)
            self.assertEqual(code, 200)
            runs = [r for r in body["runs"] if r.get("project_name") == "demo"]
            self.assertEqual(len(runs), 1, body)
            texts[query] = (runs[0]["live"]["text"], runs[0].get("status_label"))
        self.assertTrue(texts["?lang=en"][0].startswith("WAITING FOR YOU"), texts)
        self.assertEqual(texts["?lang=en"][1], "waiting for you")
        self.assertTrue(texts["?lang=tr"][0].startswith("SENİ BEKLİYOR"), texts)
        self.assertEqual(texts[""], texts["?lang=tr"], "no `lang` and an unsupported one: the bridge's own language")
        self.assertEqual(texts["?lang=de"], texts["?lang=tr"])

    def test_error_replies_follow_the_language_and_keep_their_status_codes(self):
        for bridge, project_missing, not_allowed, engine in (
                (self.en, "project not found: nope", "secret-proj is not in the bridge allowed_projects list", "unknown engine: 'gemini'"),
                (self.tr, "proje bulunamadı: nope", "secret-proj bridge allowed_projects listesinde değil", "bilinmeyen motor: 'gemini'")):
            code, body = bridge.call("POST", "/jobs", {"project": "nope", "request": "do something"})
            self.assertEqual(code, 400)
            self.assertTrue(body["error"].startswith(project_missing), body)
            code, body = bridge.call("POST", "/jobs", {"project": "secret-proj", "request": "do something"})
            self.assertEqual((code, body["error"]), (400, not_allowed))
            code, body = bridge.call("POST", "/jobs", {"project": "demo", "request": "do something", "engine": "gemini"})
            self.assertEqual(code, 400)
            self.assertTrue(body["error"].startswith(engine), body)
        self.assertIsNone(TR_LETTERS.search(self.en.call("POST", "/jobs", {"project": "nope", "request": "do it"})[1]["error"]))

    def test_job_lifecycle_in_english(self):
        bridge = self.en
        code, job = bridge.call("POST", "/jobs", {"project": "demo", "request": "add a priority field; let me decide", "chat_id": "42"})
        self.assertEqual(code, 202, job)
        self.assertEqual(sorted(job), JOB_KEYS)
        job = bridge.wait_status(job["id"], "awaiting_user")                                    # the enum is not translated
        deadline = time.time() + 10
        while time.time() < deadline and not any(p.get("id") == job["id"] for p in bridge.callbacks):
            time.sleep(0.1)
        payload = next(p for p in bridge.callbacks if p.get("id") == job["id"])
        self.assertTrue(payload["message"].startswith(f"[demo] ⏸ Waiting for your reply · intake · job {job['id']}\n\n"), payload["message"])
        self.assertEqual(payload["status"], "awaiting_user")
        self.assertIsNone(TR_LETTERS.search(payload["message"].split("\n\n", 1)[0]))
        # a second job for the same project is refused, in English, with the English cancel command
        code, body = bridge.call("POST", "/jobs", {"project": "demo", "request": "another job"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], f"demo already has an open job ({job['id']}, awaiting_user). Finish it first or send /cancel.")
        # the chat command language, English spellings
        self.assertIn(f"{job['id']} · demo · awaiting_user", bridge.say("/status"))
        self.assertIn("Live runs:", bridge.say("/status"))
        self.assertEqual(bridge.say("/durum").splitlines()[0], "Jobs in this chat:")               # the Turkish word, same bridge
        self.assertEqual(bridge.say("/run ../etc bad"), "Could not start: invalid project name: '../etc'")
        self.assertIn("Could not start: demo already has an open job", bridge.say("/run demo something else"))
        self.assertEqual(bridge.say("hello there"), f"Got it, continuing: [demo] job {job['id']}.")
        self.assertEqual(bridge.say("/iptal", chat="43"), "No open job.")
        self.assertTrue(bridge.say("/cancel").startswith(f"Cancelled: {job['id']} (files stay as they are;"))
        self.assertEqual(bridge.call("GET", f"/jobs/{job['id']}")[1]["status"], "cancelled")
        self.assertEqual(bridge.say("/cancel"), "No open job.")
        self.assertEqual(bridge.say("/status", chat="77").splitlines()[0], "No job has been started from this chat.")

    def test_turkish_bridge_keeps_speaking_turkish(self):
        bridge = self.tr
        code, job = bridge.call("POST", "/jobs", {"project": "demo3", "request": "öncelik ekle; ben karar verici olayım", "chat_id": "42"})
        self.assertEqual(code, 202, job)
        job = bridge.wait_status(job["id"], "awaiting_user")
        deadline = time.time() + 10
        while time.time() < deadline and not any(p.get("id") == job["id"] for p in bridge.callbacks):
            time.sleep(0.1)
        payload = next(p for p in bridge.callbacks if p.get("id") == job["id"])
        self.assertTrue(payload["message"].startswith(f"[demo3] ⏸ Yanıtını bekliyor · intake · iş {job['id']}\n\n"))
        self.assertIn("açık bir iş var", bridge.call("POST", "/jobs", {"project": "demo3", "request": "başka"})[1]["error"])
        self.assertIn("Bu sohbetin işleri:", bridge.say("/durum"))
        self.assertIn("Bu sohbetin işleri:", bridge.say("/status"))                              # the English word works too
        self.assertTrue(bridge.say("/cancel").startswith(f"İptal edildi: {job['id']}"))

    def test_auto_resume_cap_message_in_english(self):
        bridge = self.en
        code, job = bridge.call("POST", "/jobs", {"project": "demo2", "request": "add a priority field; let me decide", "chat_id": "44"})
        self.assertEqual(code, 202, job)
        bridge.wait_status(job["id"], "awaiting_user")
        self.assertEqual(bridge.call("POST", f"/jobs/{job['id']}/reply", {"text": "hep takıl"})[0], 202)     # the fake model stalls
        deadline = time.time() + 60
        saved = {}
        while time.time() < deadline:
            saved = bridge.job_file(job["id"])
            if saved.get("status") == "awaiting_user" and "limit reached" in (saved.get("last_message") or ""):
                break
            time.sleep(0.2)
        self.assertIn("(Automatic continuation limit reached: the run was continued 3 times and stopped again. "
                      "Send a message to continue.)", saved.get("last_message") or "", saved)


# ================================================================================================ the local UI

@unittest.skipUnless(NODE, "node is not installed")
class TestUiTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = run_node(
            "const vm = require('node:vm'); const fs = require('node:fs');"
            f"const sandbox = {{ module: {{ exports: {{}} }}, self: {{}}, URLSearchParams }};"
            f"vm.runInNewContext(fs.readFileSync({json.dumps(str(UI / 'i18n.js'))}, 'utf8'), sandbox);"
            "console.log(JSON.stringify({ messages: sandbox.module.exports.MESSAGES, names: sandbox.module.exports.LANGUAGE_NAMES,"
            " supported: sandbox.module.exports.SUPPORTED }));")
        cls.en, cls.tr = cls.tables["messages"]["en"], cls.tables["messages"]["tr"]
        cls.html = (UI / "index.html").read_text(encoding="utf-8")
        cls.app = (UI / "app.js").read_text(encoding="utf-8")

    def test_both_languages_have_the_same_keys_and_fields(self):
        self.assertEqual(sorted(self.en), sorted(self.tr))
        self.assertEqual(self.tables["supported"], ["en", "tr"])
        for key in self.en:
            self.assertEqual(i18n.fields(self.en[key]), i18n.fields(self.tr[key]), key)
            self.assertTrue(self.en[key].strip() and self.tr[key].strip(), key)
            self.assertIsNone(TR_LETTERS.search(self.en[key]), f"en {key}")
            self.assertNotIn(EM_DASH, self.en[key] + self.tr[key], key)
        untranslated = [k for k in self.en if self.en[k] == self.tr[k] and len(re.sub(r"\{\w+\}|[^A-Za-z]", "", self.en[k])) > 12
                        and not k.startswith("engine.")]
        self.assertEqual(untranslated, [])

    def test_every_key_the_page_and_the_app_use_exists_and_nothing_is_dead(self):
        used = set(re.findall(r'data-i18n(?:-rich)?="([\w.-]+)"', self.html))
        for pair in re.findall(r'data-i18n-attr="([^"]+)"', self.html):
            used |= {p.split(":", 1)[1] for p in pair.split(";") if ":" in p}
        called = set(re.findall(r"""\bt\(["']([\w.-]+)["']""", self.app)) | set(re.findall(r"""setStatus\(["']([\w.-]+)["']""", self.app))
        self.assertEqual(sorted(k for k in called | used if k not in self.en), [], "a key the page or the app asks for is missing")
        used |= {literal for literal in re.findall(r"""["']([a-z0-9_-]+(?:\.[a-z0-9_-]+)+)["']""", self.app) if literal in self.en}
        dynamic = ("phase.", "jobstatus.", "flowstatus.", "agent.", "activity.", "engine.")        # app.js builds these keys
        base = {k[:-4] if k.endswith("_one") else k for k in self.en}
        dead = [k for k in base if k not in used and not k.startswith(dynamic) and f"{k}_one" not in used]
        self.assertEqual(sorted(dead), [], "UI messages nothing uses")
        for phase in re.findall(r'"(\w+)"', re.search(r"flowPhaseIds = \[(.*?)\]", self.app, re.S).group(1)):
            self.assertIn(f"phase.{phase}", self.en)
        for status in ("queued", "running", "awaiting_user", "blocked", "paused", "stalled", "done", "error", "cancelled"):
            self.assertIn(f"jobstatus.{status}", self.en)
        for status in ("active", "awaiting_user", "blocked", "paused", "done", "error", "aborted"):
            self.assertIn(f"flowstatus.{status}", self.en)
        for agent in ("adversary", "architect", "implementer", "main", "plan-critic", "researcher", "reviewer", "scout",
                      "verifier", "ana"):
            self.assertIn(f"agent.{agent}", self.en)
            self.assertIn(f"agent.{agent}.desc", self.en)
        for engine in ("claude", "cursor", "copilot", "pi"):
            self.assertIn(f"engine.{engine}", self.en)
            self.assertEqual(self.en[f"engine.{engine}"], self.tr[f"engine.{engine}"])

    def test_the_page_default_is_the_english_table(self):
        """index.html is the no-script fallback: its text equals the English table, so the two cannot drift apart."""
        for match in re.finditer(r'<(\w+)([^>]*?)data-i18n="([\w.-]+)"([^>]*)>([^<]*)<', self.html):
            key, text = match.group(3), match.group(5).strip()
            if text and "{" not in self.en[key]:
                self.assertEqual(text.replace("&uuml;", "ü"), self.en[key].strip(), key)

    def test_every_call_to_the_bridge_carries_the_language_of_the_page(self):
        """The server writes some of the texts the page shows: the page tells it which language to write them in, and
        asks again after the language button was pressed."""
        api = re.search(r"async function api\(.*?\n\}", self.app, re.S).group(0)
        self.assertIn("fetch(withLang(path)", api)
        with_lang = re.search(r"function withLang\(.*?\n\}", self.app, re.S).group(0)
        self.assertIn("lang=${encodeURIComponent(state.lang)}", with_lang)
        self.assertIn('path.includes("?") ? "&" : "?"', with_lang)
        toggle = re.search(r"function toggleLanguage\(.*?\n\}", self.app, re.S).group(0)
        for call in ("refreshFlows()", "loadEngines()"):
            self.assertIn(call, toggle)
        self.assertNotRegex(self.app.replace(api, ""), r"fetch\(\s*[\"'`]/(?!health)", "no other call skips api()")

    def test_every_eyebrow_and_the_local_tag_are_translated(self):
        """Two bits of visible text had no binding and stayed English in the Turkish page: the hero eyebrow and the tag."""
        tags = re.findall(r'<(\w+)([^>]*class="[^"]*\b(?:eyebrow|local-tag)\b[^"]*"[^>]*)>', self.html)
        self.assertGreaterEqual(len(tags), 3)
        for name, attrs in tags:
            self.assertIn("data-i18n=", attrs, f"<{name}{attrs}> is not bound to a message")
        for key in ("hero.eyebrow", "brand.local"):
            self.assertIn(f'data-i18n="{key}"', self.html)
            self.assertNotEqual(self.en[key], self.tr[key], key)

    def test_the_language_button_names_each_language_in_itself(self):
        self.assertEqual(self.tables["names"], {"en": "English", "tr": "Türkçe"})

    def test_which_language_the_page_picks(self):
        cases = [
            ({}, "en"), ({"browser": ["tr-TR"]}, "tr"), ({"browser": ["de-DE", "tr", "en"]}, "tr"), ({"browser": ["de-DE"]}, "en"),
            ({"server": "tr"}, "tr"), ({"server": "tr", "browser": ["en-US"]}, "tr"), ({"stored": "en", "server": "tr"}, "en"),
            ({"search": "?lang=tr", "stored": "en", "server": "en"}, "tr"), ({"search": "?lang=en", "server": "tr"}, "en"),
            ({"search": "?lang=de", "server": "tr"}, "tr"), ({"search": "?x=1&lang=TR"}, "tr"), ({"stored": "garbage", "browser": "tr"}, "tr"),
            ({"stored": "tr_TR.UTF-8"}, "tr"), ({"server": "Turkish"}, "tr"), ({"browser": []}, "en"),
        ]
        got = run_node(
            "const vm = require('node:vm'); const fs = require('node:fs');"
            "const sandbox = { module: { exports: {} }, self: {}, URLSearchParams };"
            f"vm.runInNewContext(fs.readFileSync({json.dumps(str(UI / 'i18n.js'))}, 'utf8'), sandbox);"
            "const cases = JSON.parse(fs.readFileSync(0, 'utf8'));"
            "console.log(JSON.stringify(cases.map((c) => sandbox.module.exports.pickLanguage(c))));", [c for c, _ in cases])
        self.assertEqual(got, [want for _, want in cases])

    def test_translate_plurals_fallbacks_and_params(self):
        got = run_node(
            "const vm = require('node:vm'); const fs = require('node:fs');"
            "const sandbox = { module: { exports: {} }, self: {}, URLSearchParams };"
            f"vm.runInNewContext(fs.readFileSync({json.dumps(str(UI / 'i18n.js'))}, 'utf8'), sandbox);"
            "const t = sandbox.module.exports.translate;"
            "console.log(JSON.stringify([t('en','project.scope_restricted',{n:1}), t('en','project.scope_restricted',{n:3}),"
            " t('tr','project.scope_restricted',{n:1}), t('de','status.disconnected'), t('tr','no.such.key'),"
            " t('en','flow.updated',{}), t('en','flow.updated',{time:'10:00'}), t('en','job.meta',{engine:'X',id:'1',run:''})]));")
        self.assertEqual(got, ["1 allowed project shown.", "3 allowed projects shown.", "1 izinli proje gösteriliyor.", "Not connected",
                               "no.such.key", "Real run data · {time}", "Real run data · 10:00", "X · job 1"])

    def test_the_scripts_are_valid_javascript(self):
        for script in ("app.js", "i18n.js"):
            cp = subprocess.run([NODE, "--check", str(UI / script)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)


if __name__ == "__main__":
    unittest.main()
