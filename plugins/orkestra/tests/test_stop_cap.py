"""Stop nudge cap must not rewrite state.json (FR-002, E3, SC-002).

A clean return inside StateTxn saves. The cap has to leave the transaction before any mutation and record idle after,
so state mtime stays at or behind that idle row and tempo.classify can return turn_ended.
"""
import ast
import importlib
import io
import json
import sys
import time
from contextlib import redirect_stdout
from unittest import mock

import helpers
from helpers import DISPATCH
from orkestra_core import activity, state as ST, tempo
from orkestra_core.i18n import t


def _load_dispatch():
    hooks = str(DISPATCH.parent)
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    return importlib.import_module("dispatch")


D = _load_dispatch()

HOOKMAIN = helpers.PLUGIN / "lib" / "orkestra_core" / "hookmain.py"
SENTINEL = "2020-01-01T00:00:00"
# Module-level imports of hookmain.py. A new one fails this list (FR-002).
EXPECTED_IMPORTS = [
    ("from", "__future__", ("annotations",)),
    ("import", "json"),
    ("import", "os"),
    ("import", "re"),
    ("import", "sys"),
    ("import", "time"),
    ("from", "pathlib", ("Path",)),
    ("from", "typing", ("Optional",)),
    ("from", "orkestra_core", ("paths",)),
    ("from", "orkestra_core", ("state",)),
    ("from", "orkestra_core.hookboot", ("utf8_stdio",)),
    ("from", "orkestra_core.i18n", ("t",)),
    ("from", "orkestra_core.lazy", ("LazyModule",)),
    ("from", "orkestra_core.util", (
        "file_lock", "flock_held_elsewhere", "now_iso", "read_json", "resolve_path", "write_json_atomic")),
]


def _module_imports(src: str):
    found = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(("import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            found.append(("from", node.module or "", tuple(alias.name for alias in node.names)))
    return found


def _raise_name(node):
    exc = node.exc
    if isinstance(exc, ast.Name):
        return exc.id
    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
        return exc.func.id
    return None


def _mutates_state(node):
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
        return True
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Subscript):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault"


def _state_txn(node):
    return isinstance(node, ast.With) and any(
        isinstance(item.context_expr, ast.Call) and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == "StateTxn" for item in node.items)


class TestStopCap(helpers.Sandbox):
    def setUp(self):
        super().setUp()
        self.proj = self.make_project("demo", git=False)
        self.rid = "001-stopcap"
        self.sid = "sess-stop"
        st = ST.new_state(self.proj, self.rid, "Stop cap", "S", "feature")
        st["phase"] = "implement"
        ST.save_state(self.proj, st)
        self.assertTrue(ST.bind_session(self.sid, self.proj, self.rid))

    def ev(self, **kw):
        base = {"session_id": self.sid, "stop_hook_active": False, "cwd": str(self.proj)}
        base.update(kw)
        return base

    def put_state(self, **kw):
        path = ST.state_path(self.proj, self.rid)
        st = json.loads(path.read_text())
        st.update(kw)
        st["updated"] = SENTINEL
        path.write_text(json.dumps(st))
        return st

    def stamp(self):
        path = ST.state_path(self.proj, self.rid)
        return path.stat().st_mtime_ns, json.loads(path.read_text())["updated"], path.read_bytes()

    def drive(self):
        activity.record(self.proj, self.rid, {"session_id": self.sid, "tool_name": "Read"}, "tool", "read calc.py")

    def call(self, ev):
        D._ANSWER.update(closed=False, emitted=False)
        entered, saves = [], []
        real_enter, real_save = ST.StateTxn.__enter__, ST.save_state

        def enter(txn):
            entered.append(txn.run_id)
            return real_enter(txn)

        def save(project, st):
            saves.append(st.get("run_id"))
            return real_save(project, st)

        buf = io.StringIO()
        with mock.patch.object(ST.StateTxn, "__enter__", enter), mock.patch.object(ST, "save_state", save), \
                redirect_stdout(buf):
            D.on_stop(ev)
        return buf.getvalue(), entered, saves

    def test_no_new_module_level_import(self):
        self.assertEqual(_module_imports(HOOKMAIN.read_text()), EXPECTED_IMPORTS)

    def test_cap_signal_is_exception_not_baseexception(self):
        src = HOOKMAIN.read_text()
        classes = {}
        for node in ast.parse(src).body:
            if isinstance(node, ast.ClassDef):
                classes[node.name] = [b.id if isinstance(b, ast.Name) else ast.dump(b) for b in node.bases]
        fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "on_stop")
        caught = [n.type.id for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name)]
        self.assertEqual(caught, [c for c in caught if c.startswith("_") and classes.get(c) == ["Exception"]])
        self.assertEqual(len(caught), 1, caught)
        name = caught[0]
        self.assertTrue(name.startswith("_"))
        self.assertEqual(classes[name], ["Exception"])
        tries = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                if isinstance(handler.type, ast.Name) and handler.type.id == name:
                    tries.append((node, handler))
        self.assertEqual(len(tries), 1)
        try_node, handler = tries[0]
        txn = [n for stmt in try_node.body for n in ast.walk(stmt) if _state_txn(n)]
        self.assertEqual(len(txn), 1)
        self.assertFalse(any(isinstance(n, ast.With) for stmt in handler.body for n in ast.walk(stmt)))
        self.assertTrue(handler.body)
        raises = [n for n in ast.walk(txn[0]) if isinstance(n, ast.Raise) and _raise_name(n) == name]
        self.assertEqual(len(raises), 1)
        mutations = [n for n in ast.walk(txn[0]) if _mutates_state(n)]
        self.assertTrue(mutations, "the increment path still writes stop_blocks inside the transaction")
        self.assertTrue(all(n.lineno > raises[0].lineno for n in mutations))
        cls = getattr(D, name)
        self.assertTrue(issubclass(cls, Exception))
        self.assertNotIn(BaseException, cls.__bases__)
        self.assertIn(Exception, cls.__bases__)

    def _assert_cap(self, blocks):
        self.drive()
        self.put_state(stop_blocks=blocks)
        before_ns, before_updated, before_bytes = self.stamp()
        _out, entered, saves = self.call(self.ev())
        path = ST.state_path(self.proj, self.rid)
        rows = activity.read_rows(self.proj, self.rid)
        self.assertEqual(rows[-1]["kind"], "idle")
        self.assertEqual(rows[-1]["what"], t("dispatch.turn_nudge_limit"))
        with self.subTest("transaction entered"):
            self.assertEqual(entered, [self.rid])
        with self.subTest("save_state"):
            self.assertEqual(saves, [])
        with self.subTest("bytes"):
            self.assertEqual(path.read_bytes(), before_bytes)
        with self.subTest("mtime"):
            self.assertEqual(path.stat().st_mtime_ns, before_ns)
        with self.subTest("updated"):
            self.assertEqual(json.loads(path.read_text())["updated"], before_updated)
        with self.subTest("stop_blocks"):
            self.assertEqual(ST.load_state(self.proj, self.rid)["stop_blocks"], blocks)
        with self.subTest("turn_ended"):
            got = tempo.classify(
                ST.load_state(self.proj, self.rid), rows, state_mtime=path.stat().st_mtime, now=time.time())
            self.assertEqual(got["key"], "turn_ended", (got, path.stat().st_mtime, rows[-1].get("ts")))

    def test_count_cap_records_idle_without_saving_state(self):
        st = self.put_state()
        key = D.progress_key(self.ev(), st)
        self._assert_cap({key: D.MAX_STOP_BLOCKS_PER_PROMPT})

    def test_rolling_ceiling_records_idle_without_saving_state(self):
        self._assert_cap({"_nudges": [time.time()] * D.STOP_NUDGE_CEILING})

    def test_a_closed_answer_from_an_earlier_event_does_not_hide_the_nudge(self):
        D._ANSWER.update(closed=True, emitted=True)
        self.drive()
        st = self.put_state(stop_blocks={})
        key = D.progress_key(self.ev(), st)
        out, _entered, _saves = self.call(self.ev())
        saved = ST.load_state(self.proj, self.rid)
        self.assertEqual(saved["stop_blocks"][key], 1)
        self.assertIn('"decision": "block"', out)

    def test_increment_writes_stop_blocks(self):
        self.drive()
        st = self.put_state(stop_blocks={})
        key = D.progress_key(self.ev(), st)
        out, entered, saves = self.call(self.ev())
        saved = ST.load_state(self.proj, self.rid)
        self.assertEqual(entered, [self.rid])
        self.assertEqual(saves, [self.rid])
        self.assertEqual(saved["stop_blocks"][key], 1)
        self.assertEqual(len(saved["stop_blocks"]["_nudges"]), 1)
        self.assertNotEqual(saved["updated"], SENTINEL)
        self.assertIn('"decision": "block"', out)

    def test_early_returns_stay_outside_the_transaction(self):
        # phase done, status still active: a terminal status is hidden by session_binding, so the done return
        # inside on_stop is the phase check.
        cases = (
            ("done", {"phase": "done"}, {}, "dispatch.turn_done", None),
            ("awaiting", {"status": "awaiting_user"}, {}, "dispatch.turn_awaiting", None),
            ("paused", {"status": "paused"}, {}, "dispatch.turn_status", {"status": "paused"}),
            ("off_topic", {}, {}, "dispatch.turn_off_topic", None),
            ("stop_hook_active", {}, {"stop_hook_active": True}, "dispatch.turn_plain", None),
            ("not_driving", {}, {}, "dispatch.turn_not_driving", None),
        )
        for label, state_kw, ev_kw, key, fields in cases:
            with self.subTest(label):
                fields_state = {"phase": "implement", "status": "active"}
                fields_state.update(state_kw)
                self.put_state(**fields_state)
                self.assertTrue(ST.bind_session(self.sid, self.proj, self.rid), label)
                log = ST.state_path(self.proj, self.rid).parent / "activity.jsonl"
                if log.exists():
                    log.unlink()
                if label == "off_topic":
                    activity.record(self.proj, self.rid, {"session_id": self.sid}, "user", "another topic")
                before = self.stamp()
                why = t(key, **fields) if fields else t(key)
                out, entered, saves = self.call(self.ev(**ev_kw))
                self.assertEqual(self.stamp(), before)
                self.assertEqual(entered, [])
                self.assertEqual(saves, [])
                self.assertEqual(out, "")
                rows = activity.read_rows(self.proj, self.rid)
                self.assertEqual(rows[-1]["kind"], "idle")
                self.assertEqual(rows[-1]["what"], why)
