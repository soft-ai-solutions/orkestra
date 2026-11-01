"""The dashboard never says more than its evidence.

User complaint (2026-10-12): a run that was NOT paused showed as "Paused" (other sessions' pause / resume commands had
flipped the recorded status while the agent kept working), and a run in the clarify phase that was merely quiet for a
minute showed "PROBABLY WAITING FOR YOU". Every claim the page makes about a run now has deterministic evidence (the truth
table above `monitor._attention`); this file pins that table state by state, in both languages:

  * table-driven fixtures: real run files (state.json, activity rows, session heartbeats, subagent registry) for every
    state x evidence combination, asserting group, code, wording (EN and TR) and the resume hint;
  * the exact scenarios of the screenshots;
  * no guess about the human ("probably", "muhtemelen", "waiting for you") unless an evidence row exists;
  * the node-rendered page: chip numbers equal the rows under the sections, no duplicate row/card, ages from the server's time;
  * hostile data (non-dict rows, junk timestamps, 1 MB strings) cannot make the verdict raise or lie.

The suite is pinned to Turkish (helpers.py): each check picks its language.
"""
from __future__ import annotations

import json
import re
import time
import unittest
from unittest import mock

import helpers  # noqa: F401  (pins the language and the import path)
from dash_support import HAVE_NODE
from orkestra_core import activity as A
from orkestra_core import monitor as MON
from test_dashboard_ux import NOW, PageCase, attn, ev, feed_row, view
from test_i18n_monitor import MonitorBase, lang

# a sentence that guesses at the human; allowed ONLY where the table has evidence (see USER_OK below)
GUESS = re.compile(r"probably|muhtemelen|may be waiting for you|might be waiting|bekliyor olabilir|bekliyor gibi", re.I)
# any sentence that says the run waits for the human
USER_WORDS = re.compile(r"waiting for you|waiting for your|needs you|seni bekliyor|cevabını bekliyor|yanıtını bekliyor|"
                        r"girdini bekliyor", re.I)


def r(ago, kind, agent="main", tool="", what="", **extra):
    return dict({"ago": ago, "kind": kind, "agent": agent, "tool": tool, "what": what}, **extra)


class Truth(MonitorBase):
    def verdict(self, code="en", rid=None):
        with lang(code):
            views = self.views()
        if rid:
            views = [v for v in views if v["run_id"] == rid]
        self.assertEqual(len(views), 1, [v["run_id"] for v in views])
        return views[0]

    def texts(self, v):
        a = v["attention"]
        return " | ".join([a["head"], a["what"], a["do"], v["live"]["text"]])


# (name, make_run kwargs, expected (group, code), user_ok, EN fragments, TR fragments, has_resume_command)
HEAD_TASKS = {"T001": {"status": "in_progress", "title": "Wave", "red": {"ok": True}}}
SCENARIOS = [
    # ---- the recorded status contradicts fresh work: never "paused", never a resume hint
    ("paused-but-working", dict(status="paused", updated_ago=150, rows=[r(40, "tool", tool="Read", what="a.ts"),
                                                                     r(6, "tool", tool="Edit", what="b.ts")]),
     ("running", "recorded"), False,
     ["Working, recorded as paused", "The record says paused, but work is still happening: last activity 6s ago."],
     ["Çalışıyor, kayıtta duraklatılmış", "Kayıtta duraklatıldı görünüyor ama iş sürüyor: son hareket 6 sn önce."], False),
    ("paused-subagent-row-only", dict(status="paused", updated_ago=150,
                                      rows=[r(8, "tool", agent="orkestra:implementer", tool="Bash", what="npm test")]),
     ("running", "recorded"), False, ["still happening"], ["iş sürüyor"], False),
    ("paused-session-heartbeat", dict(status="paused", updated_ago=300, session=True), ("paused", "paused"), False,
     ["Paused, with no sign of work", "Its session contacted Orkestra", "which is not work"],
     ["Duraklatılmış, çalışma belirtisi yok", "Oturumu", "çalışma sayılmaz"], True),
    ("blocked-but-working", dict(status="blocked", updated_ago=400,
                                 extra={"blocked": {"reason": "needs credentials", "at": "AGO:300"}},
                                 rows=[r(4, "tool", tool="Bash", what="ls")]),
     ("running", "recorded"), False, ["Working, recorded as blocked", "work is still happening"],
     ["Çalışıyor, kayıtta bloke", "iş sürüyor"], False),
    ("awaiting-but-main-continued", dict(status="awaiting_user", phase="clarify", updated_ago=400,
                                        extra={"awaiting": {"question": "Which?", "at": "AGO:300"}},
                                        rows=[r(300, "tool", tool="AskUserQuestion", what="question: x"),
                                              r(5, "tool", tool="Edit", what="a.ts")]),
     ("running", "recorded"), True, ["recorded as waiting for you", "work continued after the question"],
     ["kayıtta seni bekliyor", "sorudan sonra iş sürdü"], False),
    # ---- genuinely paused / blocked / waiting
    ("paused-no-activity", dict(status="paused", updated_ago=900, rows=[r(2000, "tool", tool="Read", what="a.ts")]),
     ("paused", "paused"), False, ["Paused", "Paused, with no sign of work. Last activity 33m ago."],
     ["Duraklatıldı", "Duraklatılmış, çalışma belirtisi yok. Son hareket 33 dk önce."], True),
    ("paused-work-older-than-the-pause", dict(status="paused", updated_ago=10, rows=[r(40, "tool", tool="Edit", what="a.ts")]),
     ("paused", "paused"), False, ["Paused"], ["Duraklatıldı"], True),
    ("paused-never-any-row", dict(status="paused", updated_ago=900), ("paused", "paused"), False,
     ["No activity has been recorded."], ["Hiç hareket kaydı yok."], True),
    ("blocked", dict(status="blocked", updated_ago=400, extra={"blocked": {"reason": "needs credentials", "at": "AGO:300"}}),
     ("you", "blocked"), True, ["Blocked", "needs credentials"], ["Bloke", "needs credentials"], True),
    ("awaiting-question-open", dict(status="awaiting_user", phase="clarify", updated_ago=300,
                                    extra={"awaiting": {"question": "Which colour?", "at": "AGO:300"}},
                                    rows=[r(300, "tool", tool="AskUserQuestion", what="question: Colour")]),
     ("you", "question"), True, ["Waiting for your answer", "Which colour?"], ["Sorunun yanıtını bekliyor", "Which colour?"], False),
    ("awaiting-with-subagents-working", dict(status="awaiting_user", phase="clarify", updated_ago=300,
                                            extra={"awaiting": {"question": "Which colour?", "at": "AGO:300"}},
                                            rows=[r(300, "tool", tool="AskUserQuestion", what="question: Colour"),
                                                  r(3, "tool", agent="orkestra:explorer", tool="Read", what="x.ts")]),
     ("you", "question"), True, ["Which colour?"], ["Which colour?"], False),
    ("active-open-question-row", dict(phase="clarify", updated_ago=300,
                                      rows=[r(200, "tool", tool="Read", what="a"), r(150, "tool", tool="AskUserQuestion",
                                                                                      what="question: Size")]),
     ("you", "question"), True, ["Waiting for your answer"], ["Sorunun yanıtını bekliyor"], False),
    ("turn-ended", dict(phase="clarify", updated_ago=900, rows=[r(500, "tool", tool="Read", what="a"),
                                                              r(420, "idle", what="turn ended")]),
     ("you", "turn_ended"), True, ["Waiting for your reply", "Claude finished its turn 7m ago."],
     ["Yanıtını bekliyor", "Claude turu 7 dk önce bitirdi."], False),
    ("turn-ended-in-turkish-hook-text", dict(phase="clarify", updated_ago=900, rows=[r(420, "idle", what="tur bitti")]),
     ("you", "turn_ended"), True, ["Claude finished its turn 7m ago."], ["Claude turu 7 dk önce bitirdi."], False),
    ("permission-prompt-row", dict(phase="implement", updated_ago=900, rows=[r(500, "tool", tool="Edit", what="a"),
                                                                           r(240, "permission_prompt", what="Allow Bash?")]),
     ("you", "prompt"), True, ["Waiting for your input", "recorded 4m ago"], ["İstem yanıtını bekliyor", "4 dk önce"], False),
    # ---- evidence that is absent or contradicted: only measured facts, no call to action
    ("turn-ended-but-work-followed", dict(phase="clarify", updated_ago=900, rows=[r(420, "idle", what="turn ended"),
                                                                                 r(100, "tool", tool="Read", what="a")]),
     ("running", "quiet"), False, ["no activity for 1m"], ["1 dk hareket yok"], False),
    ("turn-ended-then-state-changed", dict(phase="clarify", updated_ago=100, rows=[r(420, "idle", what="turn ended")]),
     ("running", "quiet"), False, ["no activity for 7m", "The run record changed 1m ago."],
     ["7 dk hareket yok", "Çalışma kaydı 1 dk önce değişti."], False),
    ("session-left-not-waiting", dict(phase="clarify", updated_ago=900,
                                      rows=[r(200, "idle", what="turn ended · this turn did not drive the bound run → session unbound")]),
     ("stalled", "detached"), False, ["No session visible yet", "not proof the run stopped"],
     ["Bağlı oturum henüz görünmüyor", "çalışmanın durduğu anlamına gelmez"], True),
    ("unknown-idle-text-no-claim", dict(phase="clarify", updated_ago=900, rows=[r(200, "idle", what="something else")]),
     ("running", "quiet"), False, ["Phase clarification"], ["Bu fazda (netleştirme)"], False),
    ("clarify-quiet-one-minute-no-rows", dict(phase="clarify", updated_ago=100), ("running", "quiet"), False,
     ["Phase clarification: no activity for 1m. The agent may be thinking or running a long step."],
     ["Bu fazda (netleştirme) 1 dk hareket yok. Ajan düşünüyor ya da uzun bir adım çalıştırıyor olabilir."], False),
    ("approve-quiet-ten-minutes", dict(phase="approve", updated_ago=360, rows=[r(350, "tool", tool="Read", what="a")]),
     ("running", "quiet"), False, ["Phase approval: no activity for 5m"], ["Bu fazda (onay) 5 dk hareket yok"], False),
    ("working-now", dict(rows=[r(5, "tool", tool="Edit", what="a.ts")], updated_ago=5), ("running", "working"), False,
     ["Working", "Last activity 5s ago."], ["Çalışıyor", "Son hareket 5 sn önce."], False),
    # ---- measured stalls: facts first, hedged
    ("long-command", dict(phase="verify", updated_ago=1200, rows=[r(1100, "tool", tool="Bash", what="docker compose build")]),
     ("stalled", "hung"), False,
     ["Long-running command", "started `docker compose build` 18m ago, and no end has been recorded. It may still be running, "
      "or the call may have been interrupted."],
     ["Uzun süren komut", "Ana oturum `docker compose build` komutunu 18 dk önce başlattı ve bitiş kaydı yok. Hâlâ çalışıyor "
      "olabilir ya da çağrı kesilmiş olabilir."], False),
    ("silent", dict(updated_ago=2000, rows=[r(3000, "tool", tool="Read", what="x.py"), r(2990, "end", tool="Bash", what="ok")]),
     ("stalled", "silent"), False, ["No tool activity for 49m and no open command call", "may have closed, or the agent may be thinking"],
     ["49 dk araç hareketi yok, açık komut çağrısı da yok", "ajan düşünüyor olabilir"], True),
    ("dead-wait", dict(updated_ago=800, history_ago=100,
                       rows=[r(900, "tool", tool="Agent", what="-> orkestra:reviewer: T016"),
                             r(899, "result", tool="Agent", what="<- orkestra:reviewer: async_launched"),
                             r(300, "agent_done", agent="orkestra:reviewer", what="reviewer done")]),
     ("stalled", "dead_wait"), False, ["Report not in yet", "its report has not reached the main session"],
     ["Rapor henüz yok", "raporu henüz ana oturuma ulaşmadı"], True),
    # ---- closed: finished says finished, with when
    ("done", dict(status="done", phase="done", updated_ago=3600, rows=[r(3700, "tool", tool="Bash", what="ls")]),
     ("done", "done"), False, ["Finished", "Finished 1h ago."], ["Bitti", "1 sa önce bitti."], False),
    ("aborted", dict(status="aborted", phase="plan", updated_ago=120), ("done", "aborted"), False,
     ["Aborted", "This run was aborted 2m ago."], ["İptal edildi", "Bu çalışma 2 dk önce iptal edildi."], False),
]


def resolve(kwargs, now):
    """`AGO:<s>` inside the extra state becomes an ISO time `<s>` seconds ago."""
    import datetime as dt
    out = json.loads(json.dumps(kwargs, default=str))

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, str) and o.startswith("AGO:"):
            return dt.datetime.fromtimestamp(now - float(o[4:])).isoformat()
        return o
    return walk(out)


class TestEveryStateAndEvidence(Truth):
    def test_the_table(self):
        for name, kw, want, user_ok, en, tr, resume in SCENARIOS:
            with self.subTest(name):
                rid = "r-" + name
                self.make_run("p-" + name, rid, **resolve(kw, time.time()))
                for code, parts in (("en", en), ("tr", tr)):
                    v = self.verdict(code, rid)
                    a = v["attention"]
                    self.assertEqual((a["group"], a["code"]), want, (code, a))
                    text = self.texts(v)
                    for part in parts:
                        self.assertIn(part, a["head"] + " | " + a["what"] + " | " + a["do"], (code, name))
                    self.assertFalse(GUESS.search(text), (code, name, text))
                    if not user_ok:
                        self.assertFalse(USER_WORDS.search(text), (code, name, text))
                    if not resume:                      # no resume hint where nothing needs resuming
                        self.assertNotIn("resume", a["command"], (code, name))
                        self.assertNotIn("orkestra-resume", a["command"], (code, name))
                    if resume:
                        self.assertTrue(a["command"], (code, name))
                    self.assertTrue(a["head"] and a["what"], (code, name))

    def test_the_status_line_of_watch_and_the_json_live_never_contradict_the_verdict(self):
        rid = "r-pw"
        self.make_run("pw", rid, status="paused", updated_ago=150, rows=[r(6, "tool", tool="Edit", what="b.ts")])
        for code in ("en", "tr"):
            v = self.verdict(code, rid)
            self.assertEqual(v["live"]["level"], "run")
            self.assertEqual(v["live"]["text"], v["attention"]["what"])
            with lang(code):
                text = MON.render_text([v], width=140)
            self.assertNotIn("Ⅱ", text)
            self.assertIn(v["attention"]["what"][:40], text)
            self.assertEqual(v["status"], "paused")                       # the JSON still carries the RECORDED status
            self.assertEqual(v["stall"]["key"], "paused")

    def test_the_talk_phase_guess_is_gone_from_json_and_terminal(self):
        self.make_run("kl", "r-kl", phase="clarify", updated_ago=100)
        for code in ("en", "tr"):
            v = self.verdict(code, "r-kl")
            self.assertEqual(v["live"]["level"], "quiet")                 # was "wait" with "probably WAITING FOR YOU"
            with lang(code):
                text = MON.render_text([v], width=140)
            self.assertFalse(GUESS.search(text), text)
            self.assertFalse(GUESS.search(json.dumps(v, ensure_ascii=False)), code)

    def test_a_registered_subagent_that_shows_life_contradicts_a_recorded_pause(self):
        proj = self.make_run("ag", "r-ag", status="paused", updated_ago=300)
        A.agent_launched(proj, "r-ag", {"id": "ag1", "type": "orkestra:implementer"}, now=time.time() - 20)
        for code in ("en", "tr"):
            v = self.verdict(code, "r-ag")
            self.assertEqual((v["attention"]["group"], v["attention"]["code"]), ("running", "recorded"))
        self.assertIn("implementer still showing life", self.verdict("en", "r-ag")["attention"]["what"])

    def test_a_registered_subagent_does_not_cancel_a_real_question(self):
        proj = self.make_run("ag2", "r-ag2", status="awaiting_user", phase="clarify", updated_ago=300,
                             extra={"awaiting": {"question": "Q?", "at": resolve({"x": "AGO:300"}, time.time())["x"]}},
                             rows=[r(300, "tool", tool="AskUserQuestion", what="question: x")])
        A.agent_launched(proj, "r-ag2", {"id": "ag1", "type": "orkestra:explorer"}, now=time.time() - 20)
        self.assertEqual(self.verdict("en", "r-ag2")["attention"]["code"], "question")

    def test_a_registered_subagent_blocks_the_turn_ended_claim(self):
        proj = self.make_run("ag3", "r-ag3", phase="clarify", updated_ago=900, rows=[r(420, "idle", what="turn ended")])
        A.agent_launched(proj, "r-ag3", {"id": "ag1", "type": "orkestra:explorer"}, now=time.time() - 20)
        a = self.verdict("en", "r-ag3")["attention"]
        self.assertNotEqual(a["code"], "turn_ended")
        self.assertNotEqual(a["group"], "you")

    def test_rows_of_a_session_that_no_longer_drives_the_run_are_not_proof_of_work(self):
        # a bound session exists, but the fresh main row carries another session id: not this run's heartbeat
        self.make_run("sid", "r-sid", status="paused", updated_ago=300, session=True,
                      rows=[r(3, "tool", tool="Edit", what="a.ts", sid="someone-else")])
        v = self.verdict("en", "r-sid")
        # the bound session's own heartbeat (the file just written) is still proof; the foreign row is not what we cite
        self.assertNotIn("last activity 3s ago", v["attention"]["what"])

    def test_json_keys_stay_stable_and_additive(self):
        self.make_run("k", "r-k", status="paused", updated_ago=150, rows=[r(6, "tool", tool="Edit", what="b.ts")])
        v = self.verdict("en", "r-k")
        self.assertEqual(set(v["attention"]), {"group", "code", "rank", "level", "since", "since_ts", "head", "what", "do",
                                               "command", "where", "activity_ts"})
        self.assertEqual(v["attention"]["rank"], MON.GROUP_RANK[v["attention"]["group"]])
        self.assertEqual(v["attention"]["where"], "none")


class TestHostileData(Truth):
    def test_junk_rows_and_state_never_raise_or_lie(self):
        proj = self.make_run("h", "r-h", status="paused", updated_ago=150,
                             rows=[r(5, "tool", tool="Edit", what="x" * 10 ** 6)])
        path = proj / ".orkestra" / "runs" / "r-h" / "activity.jsonl"
        junk = ['[]', '"s"', '{"ts": "NaN", "kind": "tool"}', '{"ts": null, "kind": ["a"]}', '{"ts": 1e400, "kind": "idle"}',
                '{"kind": "idle", "agent": 5, "what": 7}', '{"ts": -5, "kind": "tool", "agent": null}']
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(junk) + "\n")
        for code in ("en", "tr"):
            v = self.verdict(code, "r-h")
            self.assertIn(v["attention"]["group"], ("running", "paused"))
            self.assertLessEqual(len(v["attention"]["what"]), 600)
            self.assertEqual(json.loads(json.dumps(v)), v)

    def test_state_with_wrong_types(self):
        self.make_run("h2", "r-h2", status="blocked", updated_ago=100, extra={"blocked": "oops", "awaiting": [1]})
        v = self.verdict("en", "r-h2")
        self.assertIn(v["attention"]["code"], ("blocked", "unreadable"))     # unreadable is the honest answer, never a crash
        self.make_run("h3", "r-h3", status="awaiting_user", extra={"awaiting": "oops"})
        self.assertIn(self.verdict("en", "r-h3")["attention"]["code"], ("question", "unreadable"))


class TestFactsUnit(unittest.TestCase):
    def test_fresh_work_must_come_after_the_recorded_change(self):
        rows = [{"ts": NOW - 20, "kind": "tool", "agent": "main"}]
        facts = MON._facts({}, rows, NOW, NOW - 18)
        self.assertIsNone(MON._alive_after(facts, NOW - 18, NOW))           # 2 s after the change: a straggler, not proof
        facts = MON._facts({}, rows, NOW, NOW - 40)
        self.assertEqual(MON._alive_after(facts, NOW - 40, NOW), ("row", NOW - 20))
        old = [{"ts": NOW - 100, "kind": "tool", "agent": "main"}]
        self.assertIsNone(MON._alive_after(MON._facts({}, old, NOW, NOW - 300), NOW - 300, NOW))   # older than 60 s

    def test_idle_meaning(self):
        self.assertEqual(MON._idle_meaning(""), "ended")
        self.assertEqual(MON._idle_meaning("turn ended"), "ended")
        self.assertEqual(MON._idle_meaning("tur bitti"), "ended")
        self.assertEqual(MON._idle_meaning("turn ended · status paused"), "left")
        self.assertEqual(MON._idle_meaning("turn ended · status done"), "left")
        self.assertEqual(MON._idle_meaning("???"), "unknown")


@unittest.skipUnless(HAVE_NODE, "node is needed to run the dashboard script")
class TestPageTruth(PageCase):
    def test_a_recorded_contradiction_is_listed_with_the_running_runs_and_counted_there(self):
        views = [view(1, "running", "recorded", status="paused", attention=attn("running", "recorded", None, head="Working, recorded as paused",
                                                                              what="The record says paused, but work is still happening: last activity 6s ago.")),
                 view(2, "paused", "paused", status="paused"), view(3, "running", "working")]
        snap, _ = self.snap(views)
        by_g = {s["g"]: [i["key"] for i in s["items"]] for s in snap["sections"]}
        self.assertEqual(by_g["running"], ["k1", "k3"])
        self.assertEqual(by_g["paused"], ["k2"])
        chips = {c["g"]: c["n"] for c in snap["chips"]}
        self.assertEqual((chips["running"], chips["paused"], chips["all"]), ("2", "1", "3"))
        card = next(i for s in snap["sections"] for i in s["items"] if i["key"] == "k1")
        self.assertIn("Working, recorded as paused", card["text"])
        self.assertNotIn("PAUSED", card["text"].upper().replace("RECORDED AS PAUSED", "").replace("RECORDED AS PAUSED", "")
                         .replace("RECORD SAYS PAUSED", ""))

    def test_the_card_of_a_recorded_contradiction_is_marked_for_styling(self):
        views = [view(1, "running", "recorded", status="paused")]
        out = self.run_page(views, [ev("return H.card('k1').dataset.code", "c")])
        self.assertEqual(out["results"]["c"], "recorded")

    def test_chip_counts_always_equal_the_rows_under_the_sections(self):
        views = [view(i, ["you", "stalled", "running", "paused", "done"][i % 5], "working", i, status="done" if i % 5 == 4 else "active",
                      title="alpha" if i % 2 else "beta") for i in range(1, 16)]
        for query in ("", "alpha", "beta", "nomatch"):
            snap, _ = self.snap(views, [ev(f"S.q={json.dumps(query)};render();return 0", "x")])
            chips = {c["g"]: int(c["n"]) for c in snap["chips"]}
            listed = {s["g"]: int(s["count"]) for s in snap["sections"]}
            for g in ("you", "stalled", "running", "paused", "done"):
                self.assertEqual(chips[g], listed.get(g, 0), (query, g))
            self.assertEqual(chips["all"], sum(chips[g] for g in ("you", "stalled", "running", "paused", "done")), query)

    def test_a_search_does_not_change_the_tab_title_or_the_calm_line(self):
        views = [view(1, "you", "question", 100, title="alpha"), view(2, "running", "working", title="beta")]
        base, _ = self.snap(views)
        searched, _ = self.snap(views, [ev("S.q='beta';render();return 0", "x")])
        self.assertEqual(base["title"], searched["title"])
        self.assertTrue(base["title"].startswith("(1) "))

    def test_an_opened_paused_row_is_one_block_not_a_row_plus_a_card(self):
        views = [view(1, "paused", "paused", status="paused")]
        out = self.run_page(views, [ev("const b=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='expand')[0];b.fire('click');"
                                       "const arts=H.find(H.byId('main'),n=>n.tag==='article');"
                                       "const col=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='collapse');"
                                       "return {rowHidden:b.hidden,cards:arts.length,collapse:col.length,label:col[0]&&col[0].textContent}", "o"),
                                    ev("const c=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='collapse')[0];c.fire('click');"
                                       "const b=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='expand')[0];"
                                       "return {rowHidden:b.hidden,card:H.find(H.byId('main'),n=>n.tag==='article')[0].hidden,exp:b.getAttribute('aria-expanded')}", "c")])
        self.assertEqual(out["results"]["o"], {"rowHidden": True, "cards": 1, "collapse": 1, "label": "Hide details"})
        self.assertEqual(out["results"]["c"], {"rowHidden": False, "card": True, "exp": "false"})

    def test_the_hidden_compact_row_is_really_hidden_by_css(self):
        css = self.page("en")
        self.assertIn(".compact[hidden]{display:none}", css)          # `.compact{display:grid}` would otherwise beat [hidden]

    def test_ages_follow_the_server_clock_even_when_the_browser_clock_is_off(self):
        v = view(1, "you", "question", 120, attention=attn("you", "question", 120, since_ts=NOW - 120))
        for header in ({"X-Orkestra-Time": str(NOW)}, {"X-Orkestra-Time": ""}):
            snap, _ = self.snap([v], [{"op": "set", "key": "headers", "value": header}, {"op": "advance", "ms": 2100}],
                                options={"now": int((NOW + 3600) * 1000)})        # the browser is one hour ahead
            card = snap["sections"][0]["items"][0]["text"]
            self.assertIn("waiting 2m", card, header)
            self.assertNotIn("1h", card, header)

    def test_the_ages_in_forms_are_from_the_servers_timestamps_not_the_pages_load_time(self):
        out = self.run_page([view(1, "paused", "paused", status="paused", state_updated_ts=NOW - 600, last_event_ts=NOW - 3000)],
                            [{"op": "snap", "as": "s"}])
        item = out["results"]["s"]["sections"][0]["items"][0]["text"]
        self.assertIn("Last activity 50m ago", item)                   # the last ACTIVITY row: the record write 10m ago is not activity
        self.assertNotIn("Last activity 10m ago", item)

    def test_every_language_renders_the_recorded_card_without_errors(self):
        views = [view(1, "running", "recorded", status="paused")]
        for code in ("en", "tr"):
            snap, out = self.snap(views, code=code)
            self.assertEqual(out["errors"], [])
            self.assertTrue(snap["sections"][0]["items"][0]["text"])

    def test_no_guess_phrase_reaches_the_page_for_a_quiet_clarify_run(self):
        with lang("en"):
            self.make_run("kq", "r-kq", phase="clarify", updated_ago=100)
            views = self.views()
        snap, _ = self.snap(views)
        text = json.dumps(snap, ensure_ascii=False)
        self.assertFalse(GUESS.search(text), text)
        self.assertEqual([s["g"] for s in snap["sections"]], ["running"])


if __name__ == "__main__":
    unittest.main()


# ================================================================== round 2: findings of the truth review (2026-10-12)

class TestTruthReview(Truth):
    """Each test is one finding of the review: a sentence the page printed that its own evidence did not support."""

    def run_(self, name, code="en", **kw):
        rid = "r-" + name
        self.make_run("p-" + name, rid, **kw)
        return self.verdict(code, rid)

    def both(self, name, **kw):
        """The same run read in English and in Turkish: (en verdict, tr verdict)."""
        rid = "r-" + name
        self.make_run("p-" + name, rid, **kw)
        return self.verdict("en", rid), self.verdict("tr", rid)

    def beat(self, sid, rid, name, ago):
        import os
        d = self.home / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.json"
        f.write_text(json.dumps({"run_id": rid, "project": str(self.ws / name), "bound_at": self.iso(time.time() - 6000)}),
                     encoding="utf-8")
        os.utime(f, (time.time() - ago, time.time() - ago))

    # ---- headless: nobody can type into a window
    def test_a_headless_run_never_points_at_a_chat_window(self):
        for v in self.both("hl-ended", mode="headless", phase="implement", updated_ago=900,
                           rows=[r(300, "tool", tool="Edit", what="a.ts"), r(120, "idle", what="turn ended")]):
            a = v["attention"]
            self.assertEqual((a["group"], a["code"], a["where"]), ("you", "turn_ended", "terminal"))
            self.assertIn("orkestra resume", a["command"])
            for text in (a["head"], a["what"], a["do"], v["live"]["text"]):
                self.assertNotRegex(text, r"(?i)claude window|claude pencere|that window|o pencere|window to continue", text)
            self.assertFalse(USER_WORDS.search(a["head"] + a["what"]), a)
        en, tr = self.both("hl-ask", mode="headless", status="awaiting_user", phase="clarify", updated_ago=300,
                           extra={"awaiting": {"question": "Which?", "at": self.iso(time.time() - 300)}})
        for v in (en, tr):
            self.assertEqual((v["attention"]["group"], v["attention"]["code"], v["attention"]["where"]),
                             ("you", "question", "terminal"))
            self.assertNotRegex(v["attention"]["do"], r"(?i)claude window|claude pencere")
        self.assertIn("channel that started the run", en["attention"]["do"])
        en, tr = self.both("hl-prompt", mode="headless", updated_ago=900, rows=[r(240, "permission_prompt", what="Allow?")])
        self.assertNotRegex(en["attention"]["do"] + tr["attention"]["do"], r"(?i)claude window|claude pencere")

    def test_an_interactive_run_still_points_at_its_window(self):
        v = self.run_("ia-ended", phase="implement", updated_ago=900, rows=[r(120, "idle", what="turn ended")])
        self.assertIn("Claude window", v["attention"]["do"])

    # ---- a state.json write is not agent activity
    def test_idle_time_is_measured_from_the_last_activity_row_not_from_a_record_write(self):
        v = self.run_("statebump", "en", rows=[r(1500, "tool", tool="Read", what="a.ts")], updated_ago=20)
        a = v["attention"]
        self.assertNotEqual(a["code"], "working", a)
        self.assertIn("No activity for 25m", a["what"])
        self.assertIn("The run record changed 20s ago.", a["what"])
        self.assertTrue(1495 <= a["since"] <= 1510, a)

    def test_a_fresh_row_is_working_and_names_the_last_activity(self):
        v = self.run_("fresh", "en", rows=[r(12, "tool", tool="Edit", what="a.ts")], updated_ago=500)
        self.assertEqual(v["attention"]["code"], "working")
        self.assertEqual(v["attention"]["what"], "Last activity 12s ago.")

    def test_a_run_with_no_row_says_the_record_changed_not_that_the_agent_worked(self):
        v = self.run_("norow", "en", phase="implement", updated_ago=30)
        self.assertEqual(v["attention"]["code"], "working")
        self.assertIn("The run record changed 30s ago", v["attention"]["what"])
        self.assertIn("No tool activity", v["attention"]["what"])

    def test_the_activity_time_is_in_the_json_and_apart_from_the_record_time(self):
        v = self.run_("ts", "en", status="paused", updated_ago=40, rows=[r(10800, "tool", tool="Edit", what="a.ts")])
        self.assertTrue(abs(v["attention"]["activity_ts"] - (time.time() - 10800)) < 5)
        self.assertTrue(abs(v["state_updated_ts"] - (time.time() - 40)) < 5)
        self.assertIn("Last activity 3h ago", v["attention"]["what"])

    # ---- a session heartbeat is not work
    def test_a_heartbeat_alone_never_turns_a_blocked_run_into_working(self):
        v = self.run_("blk", "en", status="blocked", updated_ago=900, session=True,
                      extra={"blocked": {"reason": "needs credentials", "at": self.iso(time.time() - 800)}},
                      rows=[r(790, "tool", tool="Edit", what="a.ts")])
        self.assertEqual((v["attention"]["group"], v["attention"]["code"]), ("you", "blocked"))
        self.assertNotIn("work is still happening", v["attention"]["what"])

    # ---- a command with no recorded end under a paused run
    def test_a_paused_run_with_an_open_command_does_not_claim_no_sign_of_work(self):
        en, tr = self.both("pcmd", status="paused", updated_ago=2000,
                           rows=[r(900, "tool", tool="Bash", what="npm run build:release")])
        for v, fragment in ((en, "may still be running"), (tr, "çalışıyor olabilir")):
            a = v["attention"]
            self.assertEqual((a["group"], a["code"]), ("paused", "paused"))
            self.assertIn(fragment, a["what"])
            self.assertNotRegex(a["what"], r"(?i)no sign of work|belirtisi yok")
            self.assertRegex(a["do"], r"(?i)terminal")
            self.assertTrue(a["command"])

    # ---- the session left the run
    def test_a_session_that_left_the_run_is_not_called_quiet_thinking(self):
        v = self.run_("left", "en", updated_ago=900,
                      rows=[r(500, "tool", tool="Edit", what="a.ts"),
                            r(300, "idle", what="turn ended · this turn did not drive the bound run → session unbound")])
        a = v["attention"]
        self.assertEqual((a["group"], a["code"]), ("stalled", "detached"))
        self.assertNotIn("thinking", a["what"])
        self.assertIn("orkestra", a["command"])

    def test_a_fresh_unbind_is_not_called_a_left_session(self):
        v = self.run_("left-fresh", "en", updated_ago=900,
                      rows=[r(40, "idle", what="turn ended · this turn did not drive the bound run → session unbound")])
        a = v["attention"]
        self.assertNotEqual(a["code"], "detached", a)
        self.assertNotIn("left the run", a["head"] + a["what"])

    def test_a_live_driver_is_not_called_detached(self):
        self.make_run("p-left-live", "r-left-live", updated_ago=900,
                      rows=[r(400, "idle", what="turn ended · this turn did not drive the bound run → session unbound")])
        with mock.patch.object(MON, "driver_born_at", return_value=time.time() - 30):
            v = self.verdict("en", "r-left-live")
        self.assertNotEqual(v["attention"]["code"], "detached", v["attention"])

    def test_a_fresh_session_bound_after_the_unbind_cancels_the_detached_verdict(self):
        self.make_run("p-left2", "r-left2", updated_ago=900,
                      rows=[r(500, "tool", tool="Edit", what="a.ts"),
                            r(300, "idle", what="turn ended · this turn did not drive the bound run → session unbound")])
        self.beat("sess-new", "r-left2", "p-left2", 5)
        v = self.verdict("en", "r-left2")
        self.assertNotEqual(v["attention"]["code"], "detached", v["attention"])

    # ---- hung commands are measured, not named "stalled" or "quiet"
    def test_an_open_command_is_never_quiet_at_any_age(self):
        for name, ago, cmd in (("c21", 1260, "pytest -x"), ("c2h", 7200, "pytest -x"), ("c2hs", 7200, "sleep 99999"),
                               ("c30h", 108000, "npm run build")):
            v = self.run_(name, "en", updated_ago=ago, rows=[r(ago, "tool", tool="Bash", what=cmd)])
            a = v["attention"]
            self.assertEqual((a["group"], a["code"]), ("stalled", "hung"), (name, a))
            self.assertIn(cmd, a["what"], name)
            self.assertNotIn("thinking", a["what"])

    def test_the_hung_card_is_a_measurement_and_counts_from_the_start(self):
        v = self.run_("h38", "en", updated_ago=2280, rows=[r(2280, "tool", tool="Bash", what="pytest -x")])
        a = v["attention"]
        self.assertTrue(a["what"].startswith("The main session started `pytest -x` 38m ago, and no end has been recorded."), a["what"])
        self.assertTrue(2275 <= a["since"] <= 2290)
        self.assertIn("interrupted", a["what"])

    def test_silent_session_claims_only_what_was_measured(self):
        v = self.run_("bgc", "en", updated_ago=700, rows=[r(600, "tool", tool="Edit", what="a.ts"),
                                                         r(601, "tool", tool="Bash", what="npm run dev &"),
                                                         r(600.5, "end", tool="Bash", what="ok")])
        a = v["attention"]
        self.assertEqual(a["code"], "silent", a)
        self.assertNotIn("no command is running", a["what"])
        self.assertIn("no open command call", a["what"])

    # ---- two sessions on one run
    def test_one_sessions_stop_row_is_not_the_runs_turn_end_while_another_session_works(self):
        self.make_run("p-multi", "r-multi", updated_ago=900,
                      rows=[r(30, "tool", tool="Edit", what="a.ts", sid="A"), r(10, "idle", what="turn ended", sid="B")])
        self.beat("A", "r-multi", "p-multi", 3)
        self.beat("B", "r-multi", "p-multi", 9)
        v = self.verdict("en", "r-multi")
        self.assertNotEqual(v["attention"]["code"], "turn_ended", v["attention"])

    def test_a_lone_stop_row_is_still_a_turn_end(self):
        self.make_run("p-solo", "r-solo", updated_ago=900, rows=[r(30, "tool", tool="Edit", what="a.ts", sid="A"),
                                                              r(10, "idle", what="turn ended", sid="A")])
        self.beat("A", "r-solo", "p-solo", 10)
        self.assertEqual(self.verdict("en", "r-solo")["attention"]["code"], "turn_ended")

    # ---- a run whose state.json cannot be parsed
    def test_an_unparsable_state_json_stays_on_the_page(self):
        self.make_run("p-ok", "r-ok", rows=[r(5, "tool", tool="Edit", what="a.ts")])
        bad = self.ws / "p-bad" / ".orkestra" / "runs" / "r-bad"
        bad.mkdir(parents=True)
        (bad / "state.json").write_text("{bad json", encoding="utf-8")
        with lang("en"):
            views = self.views()
        got = {v["run_id"]: v["attention"] for v in views}
        self.assertEqual(set(got), {"r-ok", "r-bad"})
        self.assertEqual((got["r-bad"]["group"], got["r-bad"]["code"]), ("stalled", "unreadable"))

    # ---- a clock that stepped back
    def test_a_row_from_the_future_is_no_proof_of_work(self):
        v = self.run_("fut", "en", status="paused", updated_ago=900,
                      rows=[{"ts": time.time() + 300, "kind": "tool", "agent": "main", "tool": "Edit", "what": "a.ts"}])
        self.assertEqual((v["attention"]["group"], v["attention"]["code"]), ("paused", "paused"), v["attention"])
        self.assertNotIn("0s ago", v["attention"]["what"])

    # ---- durations
    def test_durations_have_no_trailing_zero_unit_and_use_days(self):
        cases = ((None, "?", "?"), (0, "0s", "0 sn"), (59, "59s", "59 sn"), (60, "1m", "1 dk"), (64, "1m", "1 dk"),
                 (89, "1m", "1 dk"), (3599, "59m", "59 dk"), (3600, "1h", "1 sa"), (7200, "2h", "2 sa"),
                 (7500, "2h 5m", "2 sa 5 dk"), (86399, "23h 59m", "23 sa 59 dk"), (86400, "1d", "1 gün"),
                 (90000, "1d 1h", "1 gün 1 sa"), (172800, "2d", "2 gün"), (176400 + 600, "2d 1h", "2 gün 1 sa"))
        for sec, en, tr in cases:
            with lang("en"):
                self.assertEqual(MON._hum(sec), en, sec)
            with lang("tr"):
                self.assertEqual(MON._hum(sec), tr, sec)

    def test_a_closed_run_text_has_no_zero_unit(self):
        v = self.run_("dn", "en", status="done", phase="done", updated_ago=48 * 3600)
        self.assertIn("Finished 2d ago.", v["attention"]["what"])
        self.assertNotRegex(v["attention"]["what"], r"\b0m\b")

    def test_a_never_started_run_uses_one_number_in_header_and_body(self):
        v = self.run_("nvr", "en", updated_ago=900, history_ago=90000)
        a = v["attention"]
        self.assertEqual(a["code"], "silent", a)
        self.assertTrue(abs(a["since"] - v["timing"]["run"]) <= 5, (a["since"], v["timing"]["run"]))

    # ---- notices
    def test_an_old_guard_loop_is_not_shown_while_the_agent_has_worked_since(self):
        deny = "[plan] Source changes are blocked in phase 'discover': the plan is not approved yet by the person"
        rows = [r(2400, "deny", tool="Edit", what=deny), r(2390, "deny", tool="Edit", what=deny),
                r(2380, "deny", tool="Edit", what=deny), r(9, "tool", tool="Edit", what="a.ts")]
        v = self.run_("gl-old", "en", rows=rows)
        self.assertEqual([n["code"] for n in v["notices"]], [])

    def test_a_current_guard_loop_is_shown_with_a_clean_cut_and_a_readable_subject(self):
        deny = "[plan] Source changes are blocked in phase 'discover': the plan is not approved yet by the person who owns it"
        rows = [r(30, "deny", tool="Edit", what=deny), r(20, "deny", tool="Edit", what=deny), r(10, "deny", tool="Edit", what=deny)]
        en, tr = self.both("gl-new", rows=rows)
        for v, subject in ((en, "The main session was stopped by the same guard 3 times: "),
                           (tr, "Ana oturum aynı korumaya 3 kez takıldı: ")):
            text = next(n["text"] for n in v["notices"] if n["code"] == "guard_loop")
            self.assertTrue(text.startswith(subject), text)
            self.assertTrue(text.endswith("…"), text)
            self.assertNotRegex(text, r"approv…$")

    # ---- Turkish wording
    def test_turkish_uses_one_verb_for_every_waiting_card(self):
        self.make_run("p-q", "r-q", status="awaiting_user", phase="clarify", updated_ago=300,
                      extra={"awaiting": {"question": "Q?", "at": self.iso(time.time() - 300)}})
        self.make_run("p-p", "r-p", updated_ago=900, rows=[r(240, "permission_prompt", what="Allow?")])
        self.make_run("p-t", "r-t", updated_ago=900, rows=[r(240, "idle", what="turn ended")])
        for rid in ("r-q", "r-p", "r-t"):
            a = self.verdict("tr", rid)["attention"]
            blob = a["head"] + " " + a["what"] + " " + a["do"]
            self.assertIn("yanıt", blob.lower(), (rid, blob))
            self.assertNotRegex(blob.lower(), r"cevap|giriş", (rid, blob))

    def test_an_awaiting_record_without_a_question_says_so_and_has_an_age(self):
        v = self.run_("aq", "en", status="awaiting_user", phase="clarify", updated_ago=300)
        a = v["attention"]
        self.assertNotEqual(a["what"], "A question is open.")
        self.assertIn("no question text was saved", a["what"])
        self.assertTrue(a["since"] is not None and 290 <= a["since"] <= 320, a)


@unittest.skipUnless(HAVE_NODE, "node is needed to run the dashboard script")
class TestTruthReviewPage(PageCase):
    def item(self, v, code="en"):
        snap, _ = self.snap([v], code=code)
        return snap["sections"][0]["items"][0]["text"]

    def test_the_collapsed_row_shows_the_last_activity_not_the_record_write(self):
        text = self.item(view(1, "paused", "paused", status="paused", state_updated_ts=NOW - 40, last_event_ts=NOW - 10800))
        self.assertIn("Last activity 3h ago", text)
        self.assertNotIn("1m ago", text)
        self.assertNotIn("Last signal", text)

    def test_a_row_without_any_activity_says_the_record_changed(self):
        text = self.item(view(1, "paused", "paused", status="paused", state_updated_ts=NOW - 120, last_event_ts=None))
        self.assertIn("Record changed 2m ago", text)

    def test_an_aborted_row_says_aborted(self):
        text = self.item(view(1, "done", "aborted", status="aborted", state_updated_ts=NOW - 72 * 3600,
                              timing={"run": 900, "phases": {}, "task_avg": None, "reopens": 0, "tasks_reopened": 0}))
        self.assertIn("aborted 3d ago", text)
        self.assertNotIn("finished", text)

    def test_a_hung_card_counts_from_the_start_and_is_not_announced_as_stalled(self):
        v = view(1, "stalled", "hung", 2280)
        snap, out = self.snap([v])
        card = snap["sections"][0]["items"][0]["text"]
        self.assertIn("started 38m ago", card)
        self.assertNotIn("quiet 38m", card)

    def test_the_stalled_section_is_named_by_what_is_measured(self):
        snap, _ = self.snap([view(1, "stalled", "hung", 100)])
        chips = {c["g"]: c["text"] for c in snap["chips"]}
        self.assertIn("Needs a look", chips["stalled"])
        snap_tr, _ = self.snap([view(1, "stalled", "hung", 100)], code="tr")
        self.assertIn("Bakmak gerek", {c["g"]: c["text"] for c in snap_tr["chips"]}["stalled"])

    def test_a_fresh_question_comes_before_an_old_turn_end(self):
        views = [view(1, "you", "turn_ended", 72 * 3600), view(2, "you", "question", 23)]
        snap, _ = self.snap(views)
        self.assertEqual([i["key"] for i in snap["sections"][0]["items"]], ["k2", "k1"])
