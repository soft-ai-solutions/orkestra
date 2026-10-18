"""Source claims for bridge flow-card badges, a failed jobs refresh, and empty gate labels.

Reads the real bridge/ui/app.js and bridge/ui/i18n.js. The catalog is parsed in Python.
When node is installed, the same strings are also checked through i18n.js translate.
Without node, this module still asserts the catalog values.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
UI = HERE.parent / "ui"
APP = UI / "app.js"
I18N = UI / "i18n.js"
MONITOR = ROOT / "plugins" / "orkestra" / "lib" / "orkestra_core" / "monitor.py"
HOOK = ROOT / "plugins" / "orkestra" / "lib" / "orkestra_core" / "hookmain.py"
NODE = shutil.which("node")
EM_DASH = "\u2014"
TR_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")
PAIR = re.compile(r'"((?:\\.|[^"\\])*)"\s*:\s*"((?:\\.|[^"\\])*)"')

BADGES = {
    "perm_wait": ("Permission wait", "İzin bekleniyor"),
    "api_error": ("API error", "API hatası"),
    "loop": ("Deny loop", "Ret döngüsü"),
}
REFRESH = ("The runs list could not refresh.", "Çalışma listesi yenilenemedi.")
GATE_NONE = ("none", "yok")

REFRESH_CATCH = """\
    bind("jobs-updated", () => t("jobs.refresh_failed"));
    const list = $("jobs-list");
    if (!list.querySelector(".job-card")) {
      list.replaceChildren();
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = t("jobs.refresh_failed");
      list.append(empty);
    }
    if (error.message.includes("unauthorized")) setStatus("status.bad_token");
"""

FLOW_GATE = """\
function flowGate(label, value) {
  const text = typeof value === "string" ? value : "";
  const shown = text === "" || text === "\\u2014" ? t("flowcard.gate_none") : text;
  const gate = flowElement("div", "flow-gate");
  gate.append(flowElement("span", "", label), flowElement("b", "", shown));
  return gate;
}
"""


def js_function(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\s*\(", source)
    if not match:
        raise AssertionError(f"missing function {name}")
    brace = source.find("{", match.end())
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
    raise AssertionError(f"unclosed function {name}")


def py_function(source: str, name: str) -> str:
    match = re.search(rf"^def {re.escape(name)}\(", source, re.M)
    if not match:
        raise AssertionError(f"missing function {name}")
    lines = source[match.start():].splitlines(True)
    body = [lines[0]]
    for line in lines[1:]:
        if line.strip() and not line.startswith((" ", "\t", "#")):
            break
        body.append(line)
    return "".join(body)


def message_tables(text: str) -> tuple[dict[str, str], dict[str, str]]:
    marker = text.index("const MESSAGES = {")
    rest = text[marker:]
    en_at = rest.index("\n    en: {")
    tr_at = rest.index("\n    tr: {")
    en_block = rest[en_at:tr_at]
    tr_rest = rest[tr_at:]
    tr_block = tr_rest[:tr_rest.index("\n  };")]

    def pairs(block: str) -> dict[str, str]:
        return {json.loads(f'"{key}"'): json.loads(f'"{value}"') for key, value in PAIR.findall(block)}

    return pairs(en_block), pairs(tr_block)


class TestBridgeCardBugs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = APP.read_text(encoding="utf-8")
        cls.i18n = I18N.read_text(encoding="utf-8")
        cls.en, cls.tr = message_tables(cls.i18n)
        cls.monitor = MONITOR.read_text(encoding="utf-8")
        cls.hook = HOOK.read_text(encoding="utf-8")

    def test_activity_badges_are_catalog_labels_not_raw_ids(self):
        activity = js_function(self.app, "renderFlowActivity")
        label = js_function(self.app, "activityLabel")
        self.assertIn("hasMessage(`activity.${kind}`)", label)
        self.assertIn("t(`activity.${kind}`)", label)
        self.assertIn(
            'flowElement("span", "flow-event-kind", activityLabel(kind) || kind || t("activity.other"))',
            activity,
        )
        for kind, (en_label, tr_label) in BADGES.items():
            key = f"activity.{kind}"
            self.assertEqual(self.en.get(key), en_label, key)
            self.assertEqual(self.tr.get(key), tr_label, key)
            self.assertNotEqual(en_label, kind)
            self.assertNotEqual(tr_label, kind)
            shown = self.en[key] or kind or self.en["activity.other"]
            self.assertEqual(shown, en_label)
            self.assertNotEqual(shown, kind)

    def test_stored_kind_strings_and_dashboard_kind_of_stay(self):
        self.assertIn('activity.record(project, rid, ev, "perm_wait"', self.hook)
        self.assertIn(', ev, "api_error"', self.hook)
        self.assertIn('activity.record(project, rid, ev, "loop"', self.hook)
        kind_of = js_function(self.monitor, "kindOf")
        self.assertIn('||"other"', re.sub(r"\s+", "", kind_of))
        for kind in BADGES:
            self.assertNotIn(kind, kind_of)

    def test_refresh_jobs_failure_shows_catalog_error_without_dropping_cards(self):
        refresh = js_function(self.app, "refreshJobs")
        self.assertIn(REFRESH_CATCH, refresh)
        catch = refresh.split("} catch (error) {", 1)[1].split("} finally {", 1)[0]
        self.assertNotIn("jobs.empty_connect", catch)
        self.assertNotIn("jobs.empty", catch)
        self.assertEqual(self.en.get("jobs.refresh_failed"), REFRESH[0])
        self.assertEqual(self.tr.get("jobs.refresh_failed"), REFRESH[1])
        self.assertNotIn(EM_DASH, REFRESH[0] + REFRESH[1])
        self.assertIsNone(TR_LETTERS.search(REFRESH[0]))

    def test_later_success_removes_the_error_empty_state(self):
        refresh = js_function(self.app, "refreshJobs")
        try_body = refresh.split("} catch (error) {", 1)[0]
        self.assertIn("renderJobs(data.jobs)", try_body)
        render = js_function(self.app, "renderJobs")
        self.assertIn('querySelectorAll(":scope > .empty-state")', render)
        empty_branch = render.split("if (recent.length === 0)", 1)[1].split("return;", 1)[0]
        self.assertIn('t("jobs.empty")', empty_branch)
        self.assertNotIn("jobs.refresh_failed", empty_branch)
        self.assertNotIn("jobs.empty_connect", empty_branch)
        self.assertIn("stale.remove()", render)

    def test_flow_gate_maps_empty_and_sentinel_and_passes_real_text(self):
        self.assertIn(FLOW_GATE, self.app)
        self.assertIn('flowGate(t("flowcard.gate_baseline"), gates.baseline)', self.app)
        self.assertIn('flowGate(t("flowcard.gate_regression"), gates.verify)', self.app)
        self.assertIsNone(re.search(r"gates\.baseline\s*\|\|", self.app))
        self.assertIsNone(re.search(r"gates\.verify\s*\|\|", self.app))
        self.assertNotIn(EM_DASH, self.app)
        self.assertEqual(self.en.get("flowcard.gate_none"), GATE_NONE[0])
        self.assertEqual(self.tr.get("flowcard.gate_none"), GATE_NONE[1])
        for value, expect_en, expect_tr in (("", "none", "yok"), (EM_DASH, "none", "yok"), ("3/3 passed", "3/3 passed", "3/3 passed")):
            self.assertEqual(self._gate_shown(value, self.en), expect_en)
            self.assertEqual(self._gate_shown(value, self.tr), expect_tr)

    def test_monitor_json_sentinel_is_unchanged(self):
        summary = py_function(self.monitor, "_gate_summary")
        self.assertIn('out[label] = "\u2014"', summary)

    def test_new_strings_have_no_em_dash_and_jobs_and_gate_keys_are_referenced(self):
        for key in ("jobs.refresh_failed", "flowcard.gate_none", *(f"activity.{kind}" for kind in BADGES)):
            self.assertIn(key, self.en)
            self.assertIn(key, self.tr)
            self.assertNotIn(EM_DASH, self.en[key] + self.tr[key], key)
            self.assertIsNone(TR_LETTERS.search(self.en[key]), key)
        self.assertIn('t("jobs.refresh_failed")', self.app)
        self.assertIn('t("flowcard.gate_none")', self.app)
        self.assertIn("activity.${kind}", self.app)

    def test_node_checks_scripts_and_translates_the_new_keys(self):
        if not NODE:
            for kind, (en_label, tr_label) in BADGES.items():
                self.assertEqual(self.en[f"activity.{kind}"], en_label)
                self.assertEqual(self.tr[f"activity.{kind}"], tr_label)
            self.assertEqual((self.en["jobs.refresh_failed"], self.tr["jobs.refresh_failed"]), REFRESH)
            self.assertEqual((self.en["flowcard.gate_none"], self.tr["flowcard.gate_none"]), GATE_NONE)
            return
        for name in ("app.js", "i18n.js"):
            checked = subprocess.run([NODE, "--check", str(UI / name)], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
        code = (
            "const vm = require('node:vm'); const fs = require('node:fs');"
            "const sandbox = { module: { exports: {} }, self: {}, URLSearchParams };"
            f"vm.runInNewContext(fs.readFileSync({json.dumps(str(I18N))}, 'utf8'), sandbox);"
            "const t = sandbox.module.exports.translate;"
            "const keys = ['activity.perm_wait','activity.api_error','activity.loop','jobs.refresh_failed','flowcard.gate_none'];"
            "console.log(JSON.stringify(Object.fromEntries(keys.map((key) => [key, {en: t('en', key), tr: t('tr', key)}]))));"
        )
        ran = subprocess.run([NODE, "-e", code], capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        got = json.loads(ran.stdout)
        for kind, (en_label, tr_label) in BADGES.items():
            self.assertEqual(got[f"activity.{kind}"], {"en": en_label, "tr": tr_label})
        self.assertEqual(got["jobs.refresh_failed"], {"en": REFRESH[0], "tr": REFRESH[1]})
        self.assertEqual(got["flowcard.gate_none"], {"en": GATE_NONE[0], "tr": GATE_NONE[1]})

    def _gate_shown(self, value: str, table: dict[str, str]) -> str:
        """The flowGate rule: empty or exactly U+2014 becomes flowcard.gate_none; any other string passes through."""
        text = value if isinstance(value, str) else ""
        return table["flowcard.gate_none"] if text == "" or text == EM_DASH else text


if __name__ == "__main__":
    unittest.main()
