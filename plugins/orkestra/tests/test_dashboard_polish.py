"""The dashboard after the UX review: what a screen reader hears, what the keyboard can reach, what a small or short window
shows, what happens when one run (or the server) is broken, and the words in both languages.

Server side tests need no browser; the page script runs for real in node (dash_harness.js: DOM stub, fake clock, fake
server), geometry (sticky header, cut text) is fed to it by the test. Every test picks its language explicitly."""
from __future__ import annotations

import io
import json
import os
import re
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import helpers  # noqa: F401  (puts the plugin's lib on the path)
from dash_support import HAVE_NODE, run_dashboard
from orkestra_core import monitor as MON
from orkestra_core.messages import monitor as MSG_MONITOR
from test_dashboard_ux import BASELINE_KEYS, NEW_KEYS, NOW, PageCase, attn, ev, feed_row, view
from test_i18n_monitor import MonitorBase, lang


def key(k, **kw):
    return {"op": "fire", "target": "doc", "type": "keydown", "props": dict({"key": k}, **kw)}


ARTICLES = "return H.find(H.byId('main'),n=>n.tag==='article').length"


# ================================================================== markup and styles
class TestMarkupAndStyle(PageCase):
    def css(self, code="en"):
        return re.search(r"<style>(.*?)</style>", self.page(code), re.S).group(1)

    def script(self, code="en"):
        return re.findall(r"<script>(.*?)</script>", self.page(code), re.S)[-1]

    def test_no_live_region_ticks_every_second(self):
        """The connection chip and the offline banner show a countdown and an age that change once a second. As live regions
        a screen reader would read them out every second for the whole outage: only #live (and the one-off alerts) speak."""
        page = self.page("en")
        for ident in ("conn", "offline", "conn-text", "offline-msg"):
            tag = re.search(rf'<[a-z]+ [^>]*id="{ident}"[^>]*>', page).group(0)
            self.assertNotRegex(tag, r'role="(status|alert|log|timer)"|aria-live', ident)
        self.assertRegex(page, r'<div id="live" class="vh" aria-live="polite">')
        self.assertEqual(len(re.findall(r"aria-live=", page)), 1)
        self.assertEqual(sorted(re.findall(r'role="(status|alert|log|timer)"', page)), ["alert", "alert"])   # stale + render error

    def test_a_pressed_filter_keeps_a_visible_focus_ring(self):
        css = self.css()
        pressed = re.search(r'\.fchip\[aria-pressed="true"\]\{([^}]*)\}', css).group(1)
        self.assertNotIn("outline", pressed)                       # an outline here would beat :focus-visible
        self.assertIn("box-shadow:inset", pressed)
        self.assertIn(":focus-visible{outline:2px solid var(--focus);outline-offset:2px", css)
        forced = css.split("@media (forced-colors:active){")[1]
        self.assertRegex(forced, r'\.fchip\[aria-pressed="true"\]\{border:3px solid Highlight')   # box-shadow is dropped there

    def test_the_placeholder_is_as_readable_as_secondary_text(self):
        self.assertIn("#q::placeholder{color:var(--ink2);opacity:1}", self.css())

    def test_the_header_arrangement_for_small_and_short_windows(self):
        """Only the title row sticks on a phone AND on a short window (landscape phone, 200 % zoom): the whole header took
        up to half of a 400 px tall window."""
        css = self.css()
        compact = re.search(r"@media \(max-width:600px\),\(max-height:560px\)\{(.*?)\n@media", css, re.S).group(1)
        self.assertIn(".bar{display:contents}", compact)
        self.assertIn(".r1{position:sticky", compact)
        self.assertIn("html{--bar-h:56px}", compact)
        self.assertIn("scroll-padding-top:calc(var(--bar-h,120px) + 8px)", css)     # follows the real height (script sets it)
        script = self.script()
        self.assertIn('setProperty("--bar-h"', script)
        self.assertIn("ResizeObserver", script)

    def test_touch_sizes_follow_the_pointer_and_not_only_the_width(self):
        css = self.css()
        block = re.search(r"@media \(max-width:600px\),\(pointer:coarse\)\{(.*?)\}\}", css, re.S).group(1)
        for rule in (".fchip{min-height:44px}", ".btn,.tog,.more,.compact,summary{min-height:44px}", "#q{min-height:44px}"):
            self.assertIn(rule, block)

    def test_a_narrow_header_never_breaks_words_or_buttons(self):
        css = self.css()
        for rule in (".r1 h1{flex:none;white-space:nowrap}", ".seg{display:inline-flex;flex:none;"):
            self.assertIn(rule, css)
        self.assertRegex(css, r"\.conn\{[^}]*min-width:0;flex:1 1 0\}")
        self.assertRegex(css, r"\.seg a\{[^}]*white-space:nowrap\}")

    def test_the_disclosure_arrow_is_not_read_out_as_text(self):
        css = self.css()
        self.assertIn('summary::before{content:"\\25B8";content:"\\25B8" / ""', css)      # the first one is for browsers without alt text
        self.assertIn('details[open]>summary::before{content:"\\25BE";content:"\\25BE" / ""}', css)

    def test_semantics_of_the_chrome(self):
        for code in ("en", "tr"):
            page = self.page(code)
            self.assertNotIn("<nav", page)                                          # the filters are buttons, not links
            self.assertIn('<div id="sum" role="group"></div>', page)
            for ident, visible in (("lang-en", "EN"), ("lang-tr", "TR")):           # label in name: the visible text is in it
                m = re.search(rf'id="{ident}"[^>]*aria-label="([^"]*)"[^>]*>{visible}</a>', page)
                self.assertTrue(m and m.group(1).startswith(visible + ","), (code, ident))
            self.assertIn('<input id="q" type="search" dir="auto"', page)
            self.assertRegex(page, r'<p id="help" class="helpline" role="region" hidden>')
        self.assertIn('r.ol.setAttribute("aria-hidden","true")', self.script())        # the sentence under it says the same

    def test_cards_of_one_project_have_distinct_names(self):
        self.assertIn('art.setAttribute("aria-labelledby",r.h3.id+" "+r.rid.id+" "+r.title.id)', self.script())


# ================================================================== the connection: speak the change, once
@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestConnection(PageCase):
    LIVE = "H.mut.log.filter(m=>m.el===H.byId('live')).length"

    def test_an_outage_is_spoken_twice_at_most_and_the_return_once(self):
        steps = [{"op": "advance", "ms": 10000}, ev(f"return {self.LIVE}", "steady"),
                 {"op": "set", "key": "fail", "value": True}, {"op": "advance", "ms": 20000},
                 ev(f"return [{self.LIVE},H.byId('live').textContent.trim()]", "outage"),
                 {"op": "set", "key": "fail", "value": False}, {"op": "advance", "ms": 12000},
                 ev(f"return [{self.LIVE},H.byId('live').textContent.trim()]", "back")]
        res = self.run_page([view(1)], steps)["results"]
        self.assertEqual(res["steady"], 0, "a healthy page says nothing")
        n, last = res["outage"]
        self.assertLessEqual(n, 2)                                                  # connection lost, then offline: once each
        self.assertEqual(last, "Offline. The page shows the last data it received.")
        self.assertEqual(res["back"], [n + 1, "Connection restored."])

    def test_the_visible_countdown_and_age_still_tick_but_in_text_that_is_not_live(self):
        steps = [{"op": "set", "key": "fail", "value": True}, {"op": "advance", "ms": 14000},
                 ev("return H.byId('offline-msg').textContent", "a"), {"op": "advance", "ms": 5000},
                 ev("return H.byId('offline-msg').textContent", "b")]
        res = self.run_page([view(1)], steps)["results"]
        self.assertNotEqual(res["a"], res["b"])                                      # the age moved on
        self.assertIn("Cannot reach the dashboard server", res["a"])

    def test_the_first_failure_without_data_names_the_missing_data(self):
        out = run_dashboard(self.tmp, self.page("en"), [], [{"op": "advance", "ms": 2500}, ev("return H.byId('live').textContent.trim()", "t")], fail=True)
        self.assertEqual(out["results"]["t"], "Could not load the runs")

    def test_retry_now_polls_at_once_from_the_banner_and_the_chip(self):
        steps = [{"op": "set", "key": "fail", "value": True}, {"op": "advance", "ms": 14000},
                 ev("return H.sc.calls.length", "before"), {"op": "set", "key": "fail", "value": False},
                 ev("H.byId('offline-retry').fire('click');return 0", "x"), {"op": "advance", "ms": 5},
                 ev("return [H.sc.calls.length,H.byId('conn').getAttribute('data-state')]", "after")]
        res = self.run_page([view(1)], steps)["results"]
        self.assertEqual(res["after"], [res["before"] + 1, "live"])
        steps = [{"op": "set", "key": "fail", "value": True}, {"op": "advance", "ms": 2100}, ev("return H.sc.calls.length", "before"),
                 ev("H.byId('conn-retry').fire('click');return 0", "x"), {"op": "advance", "ms": 5}, ev("return H.sc.calls.length", "after")]
        res = self.run_page([view(1)], steps)["results"]
        self.assertEqual(res["after"], res["before"] + 1)

    def test_retry_now_does_not_stack_requests(self):
        steps = [{"op": "set", "key": "latency", "value": 3000}, {"op": "advance", "ms": 2100},
                 ev("for(let i=0;i<5;i++)H.byId('offline-retry').fire('click');return 0", "x"), {"op": "advance", "ms": 4000}]
        self.assertEqual(self.run_page([view(1)], steps)["maxInflight"], 1)

    def test_a_server_that_answers_with_an_error_is_told_apart_from_one_that_does_not_answer(self):
        def banner(code, status, fail):
            steps = [{"op": "advance", "ms": 1000}, {"op": "set", "key": "fail", "value": fail}, {"op": "set", "key": "status", "value": status},
                     {"op": "advance", "ms": 14000}, {"op": "snap", "as": "s"}]
            return self.run_page([view(1)], steps, code)["results"]["s"]["banners"]["offlineText"]

        self.assertRegex(banner("en", 500, False), r"^The dashboard server answered with an error \(HTTP 500\)\. Showing data from \d\d:\d\d "
                                                   r"\(.*ago\)\. Look at the terminal where `orkestra dashboard` is running\.$")
        self.assertIn("Cannot reach the dashboard server", banner("en", 200, True))
        self.assertIn("Dashboard sunucusu hata ile yan\u0131t verdi (HTTP 502)", banner("tr", 502, False))

    def test_the_first_load_that_fails_shows_an_error_with_retry_instead_of_empty_cards(self):
        self.assertIn('id="skel"', self.page("en"))                                    # the skeleton exists before the first answer
        steps = [{"op": "advance", "ms": 2500},
                 ev("const m=H.byId('main');return [m.textContent,H.find(m,n=>n.dataset&&n.dataset.act==='retry').length,"
                    "m.children.map(c=>c.getAttribute('role'))]", "err")]
        out = self.run_page([], steps, options={"status": 500})
        text, retry, roles = out["results"]["err"]
        self.assertIn("Could not load the runs", text)
        self.assertIn("(HTTP 500)", text)
        self.assertEqual((retry, roles), (1, ["alert"]))
        # and when the server is back, one click shows the runs and the error is gone
        steps += [{"op": "set", "key": "status", "value": 200}, {"op": "set", "key": "views", "value": [view(1, "you", "question", 10)]},
                  ev("H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='retry')[0].fire('click');return 0", "x"), {"op": "advance", "ms": 50},
                  ev("const m=H.byId('main');return [m.children.map(c=>c.tag+':'+(c.getAttribute('role')||'')),H.find(m,n=>n.tag==='article').length]", "ok")]
        self.assertEqual(self.run_page([], steps, options={"status": 500})["results"]["ok"], [["section:"], 1])

    def test_no_data_and_no_answer_says_the_server_did_not_answer(self):
        out = run_dashboard(self.tmp, self.page("en"), [], [ev("return H.byId('main').textContent", "t")], fail=True)
        self.assertIn("The dashboard server did not answer. Check that `orkestra dashboard` is still running", out["results"]["t"])


# ================================================================== keyboard
@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestKeyboardPolish(PageCase):
    def fleet(self):
        return [view(1, "you", "question", 900), view(2, "stalled", "hung", 700), view(3, "running", "working"), view(4, "you", "turn_ended", 500)]

    def test_single_key_shortcuts_can_be_turned_off_and_stay_off(self):
        steps = [ev("const c=H.byId('keys-on');c.checked=false;c.fire('change');return localStorage._m['orkestra.dash.keys']", "stored"),
                 key("j"), ev("return document.activeElement.dataset?document.activeElement.dataset.key||null:null", "j"),
                 key("n"), key("/"), ev("return document.activeElement.id||null", "slash"),
                 key("?"), {"op": "snap", "as": "s"}]
        res = self.run_page(self.fleet(), steps)["results"]
        self.assertEqual(res["stored"], "0")
        self.assertIsNone(res["j"])
        self.assertIsNone(res["slash"])
        self.assertFalse(res["s"]["help"])
        again = self.run_page(self.fleet(), [key("j"), ev("return document.activeElement.dataset.key||null", "j")], options={"local": {"orkestra.dash.keys": "0"}})
        self.assertIsNone(again["results"]["j"], "the choice is remembered")
        on = self.run_page(self.fleet(), [key("j"), ev("return document.activeElement.dataset.key||null", "j")])
        self.assertEqual(on["results"]["j"], "k1")                                    # on by default

    def test_escape_still_works_with_the_shortcuts_off_because_it_is_no_character_key(self):
        steps = [ev("const q=H.byId('q');q.value='zzz';q.fire('input');" + ARTICLES, "a"),
                 ev("const c=H.byId('keys-on');c.checked=false;c.fire('change');return 0", "x"), key("Escape"),
                 ev("return [H.byId('q').value,H.find(H.byId('main'),n=>n.tag==='article').length]", "b")]
        res = self.run_page(self.fleet(), steps)["results"]
        self.assertEqual((res["a"], res["b"]), (0, ["", 4]))

    def test_the_help_has_a_visible_button_and_is_announced(self):
        steps = [ev("const b=H.byId('keys-btn');return [b.textContent,H.byId('keys-on-text').textContent]", "a"),
                 ev("H.byId('keys-btn').fire('click');return [H.byId('keys-btn').getAttribute('aria-expanded'),H.byId('help').hidden,"
                    "H.byId('help').textContent,H.byId('help').getAttribute('aria-label'),H.byId('live').textContent.trim()]", "b"),
                 ev("H.byId('keys-btn').fire('click');return [H.byId('keys-btn').getAttribute('aria-expanded'),H.byId('help').hidden]", "c")]
        res = self.run_page(self.fleet(), steps)["results"]
        self.assertEqual(res["a"], ["Keyboard shortcuts", "Single-key shortcuts"])
        expanded, hidden, text, label, spoken = res["b"]
        self.assertEqual((expanded, hidden, label), ("true", False, "Keyboard shortcuts"))
        self.assertTrue(text.startswith("Keyboard: j and k move between runs"))
        self.assertEqual(spoken, text)                                                # the '?' no longer opens a silent line
        self.assertEqual(res["c"], ["false", True])
        off = self.run_page(self.fleet(), [ev("const c=H.byId('keys-on');c.checked=false;c.fire('change');H.byId('keys-btn').fire('click');return H.byId('help').hidden", "h")])
        self.assertFalse(off["results"]["h"], "the button works with the shortcuts off")

    def sticky(self, bottom=121, pos="sticky"):
        return ev(f"const b=H.byId('bar');b._pos={json.dumps(pos)};b._rect={{top:0,bottom:{bottom}}};return 0", "x")

    def test_j_scrolls_the_card_out_from_under_the_sticky_header(self):
        steps = [self.sticky(121),
                 ev("const c=H.card('k1');c._rect={top:12,bottom:700};const f=c.focus.bind(c);window.__o=null;c.focus=o=>{window.__o=o;f(o);};"
                    "const d=H.card('k4');d._rect={top:300,bottom:500};H.scrolls.length=0;return 0", "x"),
                 key("j"), ev("return [H.scrolls.slice(),window.__o]", "first"),
                 key("j"), ev("return H.scrolls.slice()", "second")]
        res = self.run_page(self.fleet(), steps, options={"innerHeight": 640})["results"]
        self.assertEqual(res["first"][0], [[0, 12 - 121 - 8]])                       # the top of the card goes just under the header
        self.assertEqual(res["first"][1], {"preventScroll": True})                   # the browser's own scroll is not used
        self.assertEqual(res["second"], [[0, 12 - 121 - 8]], "a card that is fully in view is not scrolled")

    def test_a_card_far_below_is_brought_up_and_a_static_header_does_not_count(self):
        steps = [self.sticky(121, "static"), ev("const c=H.card('k1');c._rect={top:700,bottom:1500};H.scrolls.length=0;return 0", "x"),
                 key("j"), ev("return H.scrolls.slice()", "s")]
        self.assertEqual(self.run_page(self.fleet(), steps, options={"innerHeight": 640})["results"]["s"], [[0, 700 - 8]])

    def test_the_real_header_height_reaches_the_stylesheet(self):
        steps = [self.sticky(89), {"op": "fire", "target": "win", "type": "resize"}, {"op": "advance", "ms": 200},
                 ev("return document.documentElement.style.props['--bar-h']", "h")]
        self.assertEqual(self.run_page(self.fleet(), steps)["results"]["h"], "89px")


# ================================================================== a card or a press the viewer is working in is not taken away
@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestWorkInProgress(PageCase):
    def test_a_focused_card_that_changes_group_stays_in_the_page_with_its_focus(self):
        views = [view(1, "you", "question", 400), view(2, "running", "working")]
        moved = [view(1, "you", "question", 400), view(2, "stalled", "silent", 700)]
        steps = [ev("const c=H.card('k2');c.focus();window.__c=c;return 0", "x"),
                 {"op": "set", "key": "views", "value": moved}, {"op": "advance", "ms": 2100},
                 ev("return {attached:H.byId('main').contains(window.__c),focus:document.activeElement===window.__c,"
                    "sec:window.__c.parentNode.parentNode.dataset.g,cls:window.__c.className.split(' ')[1]}", "during"),
                 {"op": "advance", "ms": 4100},
                 ev("return {attached:H.byId('main').contains(window.__c),focus:document.activeElement===window.__c}", "later"),
                 ev("document.activeElement=document.body;return 0", "y"), {"op": "advance", "ms": 2100},
                 ev("return {attached:H.byId('main').contains(window.__c),sec:window.__c.parentNode.parentNode.dataset.g}", "after")]
        res = self.run_page(views, steps)["results"]
        self.assertEqual(res["during"], {"attached": True, "focus": True, "sec": "running", "cls": "g-stalled"})   # in place, already marked
        self.assertEqual(res["later"], {"attached": True, "focus": True})
        self.assertEqual(res["after"], {"attached": True, "sec": "stalled"}, "it moves on the first update after the focus left")

    def test_the_n_key_follows_the_real_group_of_a_held_card(self):
        views = [view(1, "running", "working"), view(2, "running", "working")]
        moved = [view(1, "running", "working"), view(2, "you", "question", 30)]
        steps = [ev("const c=H.card('k2');c.focus();return 0", "x"), {"op": "set", "key": "views", "value": moved}, {"op": "advance", "ms": 2100},
                 ev("return [H.card('k2').parentNode.parentNode.dataset.g,S.nav.map(x=>x.st.key+':'+x.g)]", "nav"),
                 ev("document.activeElement=document.body;return 0", "y"), key("n"), ev("return document.activeElement.dataset.key", "k")]
        res = self.run_page(views, steps)["results"]
        self.assertEqual(res["nav"], ["running", ["k2:you", "k1:running"]])               # shown under "Running", but it needs the viewer
        self.assertEqual(res["k"], "k2")                                                  # so `n` goes to it

    def test_a_focused_card_that_becomes_finished_is_not_swapped_for_a_row_under_the_viewer(self):
        views = [view(1, "running", "working"), view(2, "running", "working")]
        done = [view(1, "running", "working"), view(2, "done", "done", status="done", state_updated_ts=NOW - 5)]
        steps = [ev("const c=H.card('k2');c.focus();window.__c=c;return 0", "x"), {"op": "set", "key": "views", "value": done}, {"op": "advance", "ms": 2100},
                 ev("return {sameNode:H.card('k2')===window.__c,focus:document.activeElement===window.__c}", "during"),
                 ev("document.activeElement=document.body;return 0", "y"), {"op": "advance", "ms": 2100}, {"op": "snap", "as": "s"}]
        out = self.run_page(views, steps)
        self.assertEqual(out["results"]["during"], {"sameNode": True, "focus": True})
        kinds = {i["key"]: i["kind"] for s in out["results"]["s"]["sections"] for i in s["items"]}
        self.assertEqual(kinds["k2"], "item")                                           # the compact row once nobody is in it

    def test_the_empty_and_no_match_blocks_go_even_when_their_button_has_focus(self):
        """A card the viewer holds stays where it is (the layout keeps it in its old section), but the helper blocks
        ("no match", "empty") are not content: a focused Clear button must not keep the block on screen after it did its job."""
        steps = [ev("S.q='zzz';render();return H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='clear').length", "shown"),
                 ev("const b=H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='clear')[0];b.focus();b.fire('click');"
                    "return H.byId('main').children.map(c=>c.tag+':'+(c.className||''))", "after")]
        res = self.run_page([view(1, "you", "question", 100), view(2, "running", "working")], steps)["results"]
        self.assertEqual(res["shown"], 1)
        self.assertEqual(len([c for c in res["after"] if c.startswith("div:empty")]), 0, res["after"])
        self.assertEqual(len([c for c in res["after"] if c.startswith("section")]), 2, res["after"])

    def test_a_card_with_the_cursor_in_its_text_stays_too(self):
        steps = [ev("const c=H.card('k1');const w=H.find(c,n=>n.className==='now')[0];H.select(w,0,3);window.__c=c;return 0", "x"),
                 {"op": "set", "key": "views", "value": [view(1, "you", "question", 5)]}, {"op": "advance", "ms": 2100},
                 ev("return [H.byId('main').contains(window.__c),H.sel.isCollapsed]", "r")]
        self.assertEqual(self.run_page([view(1, "running", "working")], steps)["results"]["r"], [True, False])

    def test_a_press_that_never_ends_stops_holding_the_page_after_three_seconds(self):
        views = [view(1, "you", "question", 900)]
        more = [view(1, "you", "question", 900), view(2, "you", "question", 100)]
        steps = [{"op": "fire", "target": "doc", "type": "pointerdown", "props": {"button": 0}}, {"op": "set", "key": "views", "value": more},
                 {"op": "advance", "ms": 1100}, ev(ARTICLES, "held"), {"op": "advance", "ms": 4000}, ev(ARTICLES, "free")]
        res = self.run_page(views, steps)["results"]
        self.assertEqual((res["held"], res["free"]), (1, 2))

    def test_the_press_is_forgotten_at_once_when_the_menu_opens_or_a_drag_ends(self):
        views = [view(1, "you", "question", 900)]
        more = [view(1, "you", "question", 900), view(2, "you", "question", 100)]
        for ending in ("contextmenu", "dragend", "pointercancel", "pointerup"):
            steps = [{"op": "fire", "target": "doc", "type": "pointerdown", "props": {"button": 0}}, {"op": "fire", "target": "doc", "type": ending},
                     {"op": "set", "key": "views", "value": more}, {"op": "advance", "ms": 2100}, ev(ARTICLES, "n")]
            self.assertEqual(self.run_page(views, steps)["results"]["n"], 2, ending)
        steps = [{"op": "fire", "target": "doc", "type": "pointerdown", "props": {"button": 0}}, {"op": "fire", "target": "win", "type": "blur"},
                 {"op": "set", "key": "views", "value": more}, {"op": "advance", "ms": 2100}, ev(ARTICLES, "n")]
        self.assertEqual(self.run_page(views, steps)["results"]["n"], 2, "blur")
        steps = [{"op": "fire", "target": "doc", "type": "pointerdown", "props": {"button": 0}}, {"op": "set", "key": "views", "value": more},
                 {"op": "advance", "ms": 2100}, ev(ARTICLES, "n")]
        self.assertEqual(self.run_page(views, steps)["results"]["n"], 1, "a press is still honoured while it lasts")

    def test_a_secondary_button_never_holds_the_page(self):
        views = [view(1, "you", "question", 900)]
        more = [view(1, "you", "question", 900), view(2, "you", "question", 100)]
        for button in (1, 2):
            steps = [{"op": "fire", "target": "doc", "type": "pointerdown", "props": {"button": button}}, {"op": "set", "key": "views", "value": more},
                     {"op": "advance", "ms": 2100}, ev(ARTICLES, "n")]
            self.assertEqual(self.run_page(views, steps)["results"]["n"], 2, button)

    def test_toggles_the_page_makes_itself_are_not_saved_and_the_viewers_are(self):
        views = [view(1, "running", "working", activity=[feed_row(1)])]
        saved = "JSON.parse(localStorage._m['orkestra.dash.open']||'{}').k1||{}"
        steps = [ev("const d=H.find(H.card('k1'),n=>n.tag==='details'&&n.className==='act')[0];d.open=false;await H.settle();return " + saved, "closed"),
                 ev("const d=H.find(H.card('k1'),n=>n.tag==='details'&&n.className==='dtl')[0];d.open=true;await H.settle();return " + saved, "details"),
                 ev("H.winFire('beforeprint');await H.settle();H.winFire('afterprint');await H.settle();return " + saved, "printed"),
                 ev("const d=H.find(H.card('k1'),n=>n.tag==='details'&&n.className==='act')[0];d.open=true;await H.settle();return [d.open," + saved + "]", "reopened")]
        res = self.run_page(views, steps)["results"]
        self.assertEqual(res["closed"], {"act": False})
        self.assertEqual(res["details"], {"act": False, "det": True})
        self.assertEqual(res["printed"], {"act": False, "det": True}, "printing opens everything but stores nothing")
        self.assertEqual(res["reopened"], [True, {"act": True, "det": True}], "the next choice of the viewer is stored")

    def test_one_toggle_event_for_two_programmatic_changes_cannot_desync_the_choice(self):
        """Browsers may merge the toggle events of an open followed by a close: the old counter then stayed one too high and
        swallowed the viewer's next click."""
        views = [view(1, "running", "working", activity=[feed_row(1)])]
        saved = "JSON.parse(localStorage._m['orkestra.dash.open']||'{}').k1||{}"
        steps = [ev("const d=H.find(H.card('k1'),n=>n.tag==='details'&&n.className==='act')[0];d.open=false;await H.settle();window.__d=d;return " + saved, "a"),
                 # the page opens and closes it again in one task; a browser may fire a single toggle event for the pair
                 ev("const d=window.__d;d._want=true;d._open=true;d._want=false;d._open=false;d.fire('toggle',{});await H.settle();return " + saved, "b"),
                 ev("const d=window.__d;d.open=true;await H.settle();return " + saved, "c")]
        res = self.run_page(views, steps)["results"]
        self.assertEqual((res["a"], res["b"], res["c"]), ({"act": False}, {"act": False}, {"act": True}))


# ================================================================== copy gives visible confirmation
@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestCopyFeedback(PageCase):
    def card(self):
        return view(1, "stalled", "silent", 700, attention=attn("stalled", "silent", 700, command="/orkestra:resume", where="claude"))

    BTN = "H.find(H.byId('main'),n=>n.dataset&&n.dataset.act==='copy')[0]"

    def test_the_button_says_copied_for_a_moment_and_keeps_its_accessible_name(self):
        steps = [ev(f"const b={self.BTN};window.__b=b;b.fire('click');await H.settle();"
                    "return [b.textContent,b.getAttribute('aria-label'),b.getAttribute('data-copied'),H.byId('live').textContent.trim()]", "during"),
                 {"op": "advance", "ms": 1000}, ev("return window.__b.textContent", "mid"),
                 {"op": "advance", "ms": 800}, ev("return [window.__b.textContent,window.__b.getAttribute('data-copied')]", "after")]
        res = self.run_page([self.card()], steps)["results"]
        self.assertEqual(res["during"], ["Copied", "Copy: /orkestra:resume", "true", "Copied"])
        self.assertEqual(res["mid"], "Copied")
        self.assertEqual(res["after"], ["Copy", None])

    def test_two_quick_clicks_do_not_leave_the_label_stuck(self):
        steps = [ev(f"const b={self.BTN};window.__b=b;b.fire('click');await H.settle();return 0", "x"),
                 {"op": "advance", "ms": 800}, ev("window.__b.fire('click');await H.settle();return 0", "y"), {"op": "advance", "ms": 2000},
                 ev("return window.__b.textContent", "after")]
        self.assertEqual(self.run_page([self.card()], steps)["results"]["after"], "Copy")

    def test_a_failed_copy_selects_the_text_and_does_not_claim_success(self):
        steps = [ev(f"const b={self.BTN};b.fire('click');await H.settle();return [b.textContent,H.byId('live').textContent.trim()]", "r")]
        res = self.run_page([self.card()], steps, options={"noClipboard": True})["results"]["r"]
        self.assertEqual(res, ["Copy", "Copy failed: select the text and press Ctrl+C"])


# ================================================================== long and foreign text
@unittest.skipUnless(HAVE_NODE, "node is not installed: the page script is checked where it is")
class TestText(PageCase):
    ROWS = "return H.find(H.byId('main'),n=>n.tag==='li'&&n.className==='fr').map(li=>li.textContent)"

    def rows(self, rows, code="en"):
        out = self.run_page([view(1, "running", "working", activity=rows)], [ev(self.ROWS, "r")], code)
        return out["results"]["r"]

    def test_a_row_does_not_repeat_its_own_label(self):
        texts = self.rows([feed_row(4, "tool", "AskUserQuestion", "question: Map set, Licensing", 40, agent="main"),
                           feed_row(3, "wait_agents", "", "waiting for subagent: implementer (1)", 30, agent="main"),
                           feed_row(2, "agent_done", "", "explorer done", 20),
                           feed_row(1, "idle", "", "turn ended", 10, agent="main")])
        self.assertEqual(texts[0], "Turn ended10s ago")                              # nothing after the badge: the badge says it
        self.assertEqual(texts[1], "Agent doneexplorer done20s ago")                  # unknown wording is never hidden
        self.assertEqual(texts[2], "Waiting for agentswaiting for subagent: implementer (1)30s ago")
        self.assertEqual(texts[3], "QuestionMap set, Licensing40s ago")               # "question: " is the badge's word

    def test_the_server_phrase_wins_and_the_raw_hook_text_stays_in_the_tooltip(self):
        raw = "turn ended \u00b7 status awaiting_user (binding kept for the answer)"
        rows = [feed_row(1, "idle", "", raw, 10, agent="main", brief="waiting for your answer")]
        out = self.run_page([view(1, "running", "working", activity=rows)],
                            [ev("const li=H.find(H.byId('main'),n=>n.tag==='li'&&n.className==='fr')[0];const s=H.find(li,n=>n.className==='sum')[0];"
                                "return [s.textContent,s.getAttribute('title')]", "r")])
        self.assertEqual(out["results"]["r"], ["waiting for your answer", raw])
        tr = self.run_page([view(1, "running", "working", activity=[feed_row(1, "idle", "", "tur bitti \u00b7 durum done", 10, agent="main")])],
                           [ev("return H.find(H.byId('main'),n=>n.className==='sum').map(n=>n.textContent)", "r")], code="tr")
        self.assertEqual(tr["results"]["r"], ["durum done"])                      # a row without a server phrase: the label is cut off

    def test_text_that_is_cut_by_the_clamp_has_a_toggle_and_short_text_has_none(self):
        long_path = "/opt/shared/components/library/QuranBookReader/pager/usePageTurnAnimation.test.tsx"
        rows = [feed_row(1, "tool", "Edit", long_path, 20), feed_row(2, "tool", "Edit", "/opt/shared/a.ts", 10)]
        out = self.run_page([view(1, "running", "working", activity=rows, project="/Users/me/Projects/app")],
                            [ev("const lis=H.find(H.byId('main'),n=>n.tag==='li'&&n.className==='fr');"
                                "const tog=li=>H.find(li,n=>n.dataset&&n.dataset.act==='rowfull')[0];"
                                "const by=t=>lis.find(li=>li.textContent.includes(t));"
                                "const t0=tog(by('usePageTurn'));const t1=tog(by('a.ts'));const label=t0&&t0.textContent;if(t0)t0.fire('click');"
                                "return [!!t0,!!t1,label,t0&&t0.textContent,H.find(by('usePageTurn'),n=>n.tag==='pre').map(n=>n.textContent)]", "r")])
        has_long, has_short, label, after, pre = out["results"]["r"]
        self.assertEqual((has_long, has_short), (True, False))
        self.assertEqual((label, after), ("Show more", "Show less"))                # an edited path is not a "command"
        self.assertEqual(pre, [long_path])

    def test_the_toggle_follows_the_layout_when_the_page_has_one(self):
        """With a layout engine the question is "is it cut right now": a short text that wraps to three lines gets the
        toggle, a long one that fits does not."""
        rows = [feed_row(1, "tool", "Edit", "/opt/b.ts", 20), feed_row(2, "tool", "Edit", "/opt/shared/components/very/deep/path/file.test.tsx", 10)]
        steps = [ev("const lis=H.find(H.byId('main'),n=>n.tag==='li'&&n.className==='fr');"
                    "lis.forEach(li=>{const s=H.find(li,n=>n.className==='sum')[0];s.clientWidth=300;s.clientHeight=36;"
                    "s.scrollHeight=li.textContent.includes('b.ts')?60:36;});return 0", "x"),          # the short one is cut, the long one fits
                 {"op": "fire", "target": "win", "type": "resize"}, {"op": "advance", "ms": 300},
                 ev("const lis=H.find(H.byId('main'),n=>n.tag==='li'&&n.className==='fr');"
                    "const has=t=>!!H.find(lis.find(li=>li.textContent.includes(t)),n=>n.dataset&&n.dataset.act==='rowfull')[0];"
                    "return {short:has('b.ts'),long:has('file.test.tsx')}", "r")]
        out = self.run_page([view(1, "running", "working", activity=rows, project="/Users/me/Projects/app")], steps)
        self.assertEqual(out["results"]["r"], {"short": True, "long": False})

    def test_a_cut_notice_can_be_read_in_full(self):
        text = "implementer was stopped by the same guard 4 times: " + "not in the plan, pick another one. " * 4
        v = view(1, "stalled", "silent", 700, notices=[{"code": "guard_loop", "severity": "warn", "text": text}, {"code": "x", "severity": "info", "text": "short one"}])
        steps = [ev("const m=H.byId('main');const ws=H.find(m,n=>n.className==='nwrap');"
                    "const info=ws.map(w=>({shown:!w.children[1].hidden,cls:w.children[0].className}));"
                    "const b=ws[0].children[1];b.fire('click');"
                    "return {info,after:[ws[0].children[0].className,b.textContent,b.getAttribute('aria-expanded')]}", "r")]
        res = self.run_page([v], steps)["results"]["r"]
        self.assertEqual([i["shown"] for i in res["info"]], [True, False])
        self.assertEqual(res["after"], ["notice open", "Show less", "true"])

    def test_text_from_a_run_is_laid_out_by_its_own_direction_and_commands_are_not(self):
        v = view(1, "you", "question", 100, title="\u0627\u0644\u0645\u0635\u062d\u0641",
                 attention=attn("you", "question", 100, command="/orkestra:resume", what="\u05e9\u05dc\u05d5\u05dd"),
                 activity=[feed_row(1, what="echo hi")], tasks=[{"id": "T1", "status": "done", "title": "\u0645\u0647\u0645\u0629", "tests": "none", "evidence": "--"}],
                 tasks_total=1, tasks_done=1, tasks_by_status={"done": 1, "in_progress": 0, "blocked": 0, "pending": 0, "deferred": 0})
        steps = [ev("const m=H.byId('main');H.find(m,n=>n.dataset&&n.dataset.act==='tasks')[0].fire('click');"
                    "const dirs=cls=>H.find(m,n=>n.className===cls).map(n=>n.dir);"
                    "return {h3:H.find(m,n=>n.tag==='h3').map(n=>n.dir),title:dirs('title'),now:dirs('now'),sum:dirs('sum'),"
                    "code:H.find(m,n=>n.tag==='code').map(n=>n.dir||''),td:H.find(m,n=>n.tag==='td'&&!n.className).map(n=>n.dir||'')}", "r")]
        res = self.run_page([v], steps)["results"]["r"]
        self.assertEqual((res["h3"], res["title"], res["now"], res["sum"]), (["auto"], ["auto"], ["auto"], ["auto"]))
        self.assertTrue(res["code"] and all(d == "" for d in res["code"]), res["code"])      # a command stays left to right
        self.assertIn("auto", res["td"])

    def test_search_ignores_case_in_turkish_the_same_way_as_in_english(self):
        views = [view(1, "running", "working", project_name="acme"), view(2, "running", "working", project_name="NORTHWIND"),
                 view(3, "running", "working", project_name="\u0130STANBUL"), view(4, "running", "working", project_name="\u0131\u015f\u0131k")]
        cases = (("ACME", ["k1"]), ("acme", ["k1"]), ("northwind", ["k2"]), ("NORTHWIND", ["k2"]), ("ISTANBUL", ["k3"]),
                 ("istanbul", ["k3"]), ("\u0130STANBUL", ["k3"]), ("I\u015eIK", ["k4"]), ("\u0131\u015f\u0131k", ["k4"]), ("ISIK", []))
        for code in ("tr", "en"):
            steps = [ev(f"const q=H.byId('q');q.value={json.dumps(query)};q.fire('input');"
                        "return H.find(H.byId('main'),n=>n.tag==='article').map(n=>n.dataset.key)", f"r{i}") for i, (query, _) in enumerate(cases)]
            res = self.run_page(views, steps, code)["results"]
            for i, (query, want) in enumerate(cases):
                self.assertEqual(res[f"r{i}"], want, (code, query))                  # the letter s with a cedilla is not an s

    def test_shortening_never_cuts_a_character_in_half(self):
        cases = [("Bash", "echo " + "\U0001f680" * 200), ("Read", "/x/" + "\U0001f4c1\U0001f4c4" * 80), ("Bash", "echo " + "\u05e9" * 200)]
        out = self.run_page([], [ev("return " + json.dumps(cases) + ".map(c=>OrkDash.shortWhat(c[0],c[1],{}).text)", "r")])
        for (tool, what), got in zip(cases, out["results"]["r"]):
            self.assertEqual(got, MON.short_what(tool, what), (tool, what[:12]))      # the two shorteners agree, emoji included
            self.assertFalse(re.search("[\ud800-\udfff]", got), "a lone surrogate")
            self.assertEqual(len(got), 88)                                              # 88 characters, not 88 UTF-16 units


# ================================================================== server: the feed, the broken run, the answer
class TestFeedForPeople(MonitorBase):
    def hook_rows(self):
        t = 1_000.0
        return [{"ts": t, "kind": "tool", "agent": "main", "tool": "Read", "what": "a.py"},
                {"ts": t + 1, "kind": "tool", "agent": "main", "tool": "Read", "what": "a.py"},
                {"ts": t + 2, "kind": "tool", "agent": "main", "tool": "Read", "what": "a.py"},
                {"ts": t + 3, "kind": "deny", "agent": "orkestra:implementer", "tool": "Bash", "what": "[deps] not in the plan"},
                {"ts": t + 4, "kind": "deny", "agent": "orkestra:implementer", "tool": "Bash", "what": "[deps] not in the plan"}]

    def test_the_legacy_activity_list_is_not_folded_and_the_page_list_is(self):
        rows = self.hook_rows()
        legacy = MON._activity_feed(rows, 14, compact=False)
        page = MON._activity_feed(rows, 14)
        self.assertEqual([r["what"] for r in legacy if r["tool"] == "Read"], ["a.py"] * 3)       # what `watch` always printed
        self.assertEqual(len([r for r in page if r["tool"] == "Read"]), 1)
        self.assertEqual(next(r for r in page if r["tool"] == "Read")["count"], 3)
        for feed in (legacy, page):                                                       # a repeated refusal is one row in both
            deny = [r for r in feed if r["kind"] == "deny"]
            self.assertEqual((len(deny), deny[0]["repeat_count"]), (1, 2))

    def test_watch_prints_every_row_and_json_keeps_activity_as_it_was(self):
        self.make_run("proj", "r1", rows=[self.ago_row(30 - i, "tool", tool="Read", what="same.py") for i in range(3)] +
                      [self.ago_row(20, "tool", tool="Bash", what="ls")])
        v = MON.discover()[0]
        self.assertEqual(len(v["activity"]), 4)                                           # nothing was folded
        self.assertEqual(len(v["activity_feed"]), 2)
        self.assertEqual(v["activity_feed"][0]["count"], 3)
        with lang("en"):
            text = MON.render_text([v], width=120)
        self.assertEqual(text.count("same.py"), 3)
        self.assertTrue(BASELINE_KEYS <= set(v) and NEW_KEYS <= set(v))

    def test_the_hook_phrases_become_short_phrases_in_the_language_of_the_request(self):
        def row(kind, text, tool=""):
            return {"ts": 1000.0, "kind": kind, "agent": "main", "tool": tool, "what": text}

        cases = [  # (row, English phrase, Turkish phrase): the hook may have written either language
            (row("idle", "turn ended"), "", ""),
            (row("idle", "tur bitti"), "", ""),
            (row("idle", "turn ended \u00b7 status awaiting_user (binding kept for the answer)"), "waiting for your answer", "cevab\u0131n\u0131 bekliyor"),
            (row("idle", "tur bitti \u00b7 durum awaiting_user (cevap i\u00e7in ba\u011f korundu)"), "waiting for your answer", "cevab\u0131n\u0131 bekliyor"),
            (row("idle", "turn ended \u00b7 status done"), "the run is finished", "\u00e7al\u0131\u015fma bitti"),
            (row("idle", "turn ended \u00b7 status paused"), "run status: paused", "\u00e7al\u0131\u015fma durumu: duraklat\u0131ld\u0131"),
            (row("idle", "turn ended \u00b7 transcript is off-topic \u2192 session unbound"), "this turn did not keep driving the run", "bu tur \u00e7al\u0131\u015fmay\u0131 s\u00fcrd\u00fcrmedi"),
            (row("idle", "turn ended (continue-nudge limit)"), "reminder limit reached", "hat\u0131rlatma s\u0131n\u0131r\u0131na ula\u015f\u0131ld\u0131"),
            (row("wait_agents", "waiting for subagent: implementer, reviewer (2)"), "implementer, reviewer (2)", "implementer, reviewer (2)"),
            (row("wait_agents", "subagent finished, waiting for its report (1)"), "report not delivered yet (1)", "rapor hen\u00fcz gelmedi (1)"),
            (row("agent_done", "orkestra:implementer finished"), "implementer", "implementer"),
            (row("agent_done", "orkestra:implementer bitti"), "implementer", "implementer"),
            (row("tool", "question: Map set, Licensing", "AskUserQuestion"), "Map set, Licensing", "Map set, Licensing"),
            (row("tool", "soru: Kapsam", "AskUserQuestion"), "Kapsam", "Kapsam"),
        ]
        for code, idx in (("en", 1), ("tr", 2)):
            with lang(code):
                for case in cases:
                    self.assertEqual(MON._activity_feed([case[0]], 5)[0].get("brief"), case[idx], (code, case[0]["what"]))
        with lang("en"):
            for text, kind in (("something the hooks never wrote", "idle"), ("", "idle"), ("x finished soon", "agent_done")):
                self.assertNotIn("brief", MON._activity_feed([row(kind, text)], 5)[0])    # unknown text: nothing is hidden
            self.assertNotIn("brief", MON._activity_feed([row("tool", "ls", "Bash")], 5)[0])

    def test_every_phrase_has_both_languages_and_no_em_dash(self):
        seen = 0
        for name in MSG_MONITOR.EN:
            if name.startswith(("monitor.brief_", "monitor.replace_", "monitor.which_", "monitor.attn_unreadable")):
                seen += 1
                self.assertIn(name, MSG_MONITOR.TR)
                self.assertNotIn("\u2014", MSG_MONITOR.EN[name] + MSG_MONITOR.TR[name], name)
        self.assertGreater(seen, 15)


class TestOneBrokenRun(MonitorBase):
    """A run whose files do not have the expected shape (hand edited, half written, from another version) used to make the
    whole list fail: the server dropped the connection and the page said the dashboard was not running."""

    def broken_runs(self):
        self.make_run("ok", "r-ok", rows=[self.ago_row(10, "tool", tool="Read", what="a.py")])
        self.make_run("bad1", "r-blocked-str", status="blocked", extra={"blocked": "waiting for keys"})
        self.make_run("bad2", "r-awaiting-str", status="awaiting_user", extra={"awaiting": "a question"})
        self.make_run("bad3", "r-tasks-list", extra={"tasks": ["T001", "T002"]})
        self.make_run("bad4", "r-hist", extra={"history": ["intake", "plan"]})
        self.make_run("bad5", "r-ts", rows=[{"ts": "abc", "kind": "tool", "agent": "main", "tool": "Read", "what": "x"}])
        self.make_run("bad6", "r-surrogate", extra={"title": "half \ud83d written"})

    def test_the_other_runs_keep_showing_and_the_broken_one_says_so(self):
        self.broken_runs()
        for code in ("en", "tr"):
            with lang(code):
                views = MON.discover(include_all=True)
            by_id = {v["run_id"]: v for v in views}
            self.assertEqual(len(views), 7, code)
            self.assertEqual(by_id["r-ok"]["attention"]["code"], "working")
            unreadable = [v for v in views if v["attention"]["code"] == "unreadable"]
            self.assertGreaterEqual(len(unreadable), 3, [v["run_id"] for v in unreadable])
            for v in unreadable:
                self.assertTrue(BASELINE_KEYS <= set(v) and NEW_KEYS <= set(v), v["run_id"])
                self.assertEqual((v["attention"]["group"], v["attention"]["rank"]), ("stalled", MON.GROUP_RANK["stalled"]))
                self.assertTrue(v["attention"]["head"] and v["attention"]["what"] and v["attention"]["do"])
                self.assertEqual(v["activity"], [])
                self.assertRegex(v["key"], r"^[0-9a-f]{12}$")
                self.assertIn(v["run_id"], v["artifacts"])                               # the folder to look at
                json.dumps(v, ensure_ascii=False)                                        # serialisable
                MON.render_text([v], width=100)                                          # and `watch` can print it
        with lang("en"):
            head = {v["run_id"]: v for v in MON.discover(include_all=True)}["r-blocked-str"]["attention"]["head"]
        self.assertEqual(head, "Cannot read this run")

    def test_the_answer_of_the_server_is_valid_json_even_with_a_lone_surrogate(self):
        self.broken_runs()
        data = json.loads(urllib.request.urlopen(self.serve() + "/api/runs?all=1", timeout=10).read().decode("utf-8"))
        self.assertEqual(len(data), 7)
        self.assertIsInstance(MON._json_bytes([{"t": "a\ud83db"}]), bytes)
        self.assertEqual(json.loads(MON._json_bytes([{"t": "a\ud83db"}])), [{"t": "a\ud83db"}])
        self.assertEqual(json.loads(MON._json_bytes([{"t": "\u00e7"}]).decode("utf-8")), [{"t": "\u00e7"}])

    def test_an_error_inside_the_listing_is_a_500_with_a_body_not_a_dropped_connection(self):
        base = self.serve()
        with mock.patch.object(MON, "discover", side_effect=RuntimeError("boom")):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/runs", timeout=10)
        self.assertEqual(cm.exception.code, 500)
        self.assertEqual(json.loads(cm.exception.read().decode("utf-8")), {"error": "RuntimeError"})
        cm.exception.close()
        self.assertEqual(json.loads(urllib.request.urlopen(base + "/api/runs", timeout=10).read()), [])     # the server lives on

    def serve(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = ThreadingHTTPServer(("127.0.0.1", port), MON.make_handler(None, port))
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), thread.join(5)))
        return f"http://127.0.0.1:{port}"


# ================================================================== `orkestra dashboard --replace`
class TestReplaceIsCareful(MonitorBase):
    def test_only_an_orkestra_dashboard_is_recognised_by_its_arguments(self):
        cases = [("/usr/bin/python3 /x/bin/orkestra dashboard --open", True), ("python3 /opt/tools/orkestra dashboard", True),
                 ("/opt/homebrew/bin/orkestra dashboard --port 4000", True),
                 ("python3 -m http.server 8080 --directory /tmp/orkestra-dashboard-notes", False),     # both words, but only in a path
                 ("node /srv/orkestra/dashboard.js", False), ("/usr/bin/python3 /x/orkestra start --title dashboard", False),
                 ("python3 /x/orkestra", False), ("", False), ("python3 -m http.server", False)]
        for command, want in cases:
            with mock.patch.object(MON, "_run_text", return_value=command):
                self.assertEqual(MON._is_orkestra_dashboard(1), want, command)
        with mock.patch.object(MON, "_run_text", return_value='python3 "/x y/orkestra" dashboard'):
            self.assertTrue(MON._is_orkestra_dashboard(1))
        with mock.patch.object(MON, "_run_text", return_value="python3 'unbalanced orkestra dashboard"):
            self.assertFalse(MON._is_orkestra_dashboard(1))                                         # unreadable: not touched

    def outcome(self, kind, replace=True, tty=True, replaced=False, pid=4242, code="en"):
        class Tty(io.StringIO):
            def isatty(self):
                return tty

        err, out = Tty(), Tty()
        with lang(code), mock.patch.object(MON, "probe_dashboard", return_value=kind), \
                mock.patch.object(MON, "_replace_listener", return_value=replaced) as stop, \
                mock.patch.object(MON, "listener_pid", return_value=pid), mock.patch.object(MON, "_process_age", return_value="2h 0m ago"), \
                mock.patch.object(sys, "stdin", Tty()), mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.dict(os.environ, {"CLAUDECODE": ""}):
            code_ = MON._busy_outcome(47616, OSError("Address already in use"), False, replace)
        return code_, out.getvalue(), err.getvalue(), stop

    @unittest.skipIf(os.name == "nt", "replace is POSIX only")
    def test_a_listener_that_is_not_a_dashboard_is_never_stopped(self):
        code, _, err, stop = self.outcome("other")
        self.assertEqual(code, 2)
        stop.assert_not_called()                                                                    # not even asked to
        self.assertIn("could not open 127.0.0.1:47616", err)

    @unittest.skipIf(os.name == "nt", "replace is POSIX only")
    def test_a_refused_replace_says_why_instead_of_suggesting_replace_again(self):
        code, _, err, stop = self.outcome("old", tty=False)
        self.assertEqual(code, 2)
        stop.assert_not_called()
        self.assertIn("--replace needs an interactive terminal", err)
        self.assertIn("older dashboard on 127.0.0.1:47616", err)
        self.assertIn("`kill 4242`", err)
        self.assertNotIn("or use `orkestra dashboard --replace`", err)
        _, _, err, _ = self.outcome("old", tty=False, pid=None)
        self.assertIn("Ctrl+C in the terminal that started it", err)
        self.assertNotIn("kill", err)
        _, _, err, _ = self.outcome("current", tty=False)
        self.assertIn("running dashboard on 127.0.0.1:47616", err)                                  # was the generic "may already be running"
        self.assertNotIn("It may already be running", err)
        code, _, err, stop = self.outcome("old", tty=True, replaced=False)                           # a terminal, but it would not stop
        stop.assert_called_once_with(47616)
        self.assertEqual(code, 2)
        self.assertIn("could not stop the older dashboard", err)
        self.assertNotIn("\u2014", err)

    @unittest.skipIf(os.name == "nt", "replace is POSIX only")
    def test_the_hint_stays_for_those_who_did_not_ask_for_replace_and_it_is_turkish_too(self):
        _, _, err, _ = self.outcome("old", replace=False)
        self.assertIn("or use `orkestra dashboard --replace`", err)
        _, _, err, _ = self.outcome("old", tty=False, code="tr")
        self.assertIn("--replace etkile\u015fimli bir terminal ister", err)
        self.assertIn("eski dashboard", err)
        self.assertNotIn("\u2014", err)
        _, _, err, _ = self.outcome("old", tty=True, replaced=False, code="tr")
        self.assertIn("durduramad\u0131", err)


class TestPageWords(unittest.TestCase):
    PAGE_KEYS = [k for k in MSG_MONITOR.EN if k.startswith(("monitor.ui_", "monitor.attn_unreadable", "monitor.brief_"))]

    def test_the_turkish_dashboard_copy_has_no_em_dash(self):
        self.assertGreater(len(self.PAGE_KEYS), 100)
        for name in self.PAGE_KEYS:
            self.assertNotIn("\u2014", MSG_MONITOR.TR[name], name)

    def test_the_turkish_dashboard_copy_speaks_to_the_viewer_one_way(self):
        formal = re.compile(r"\b(kapat\u0131n|ba\u015flat\u0131n|bak\u0131n|kontrol edin|gidin|se\u00e7in|yenileyin)\b", re.I)
        for name in self.PAGE_KEYS:
            self.assertIsNone(formal.search(MSG_MONITOR.TR[name]), name)
        self.assertIn("Kapat (Ctrl+C)", MSG_MONITOR.TR["monitor.ui_stale"])
        self.assertIn("yeniden ba\u015flat.", MSG_MONITOR.TR["monitor.ui_stale"])

    def test_the_turkish_dashboard_copy_uses_one_word_for_dashboard_and_for_build(self):
        for name in self.PAGE_KEYS:
            self.assertNotRegex(MSG_MONITOR.TR[name], r"\b[Pp]ano", name)
        self.assertIn("Dashboard sunucusuna", MSG_MONITOR.TR["monitor.ui_conn_offline"])
        self.assertIn("derleme {build}", MSG_MONITOR.TR["monitor.ui_build"])
        self.assertIn("derleme {build}", MSG_MONITOR.TR["monitor.ui_print"])
        self.assertNotIn("yap\u0131 {build}", MSG_MONITOR.TR["monitor.ui_build"] + MSG_MONITOR.TR["monitor.ui_print"])


if __name__ == "__main__":
    unittest.main()
