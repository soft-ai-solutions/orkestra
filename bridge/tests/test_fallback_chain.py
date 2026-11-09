"""engine.chain: a turn that ends on a usage limit cools its profile down and the job continues on the next profile, on a
new session, with that profile's binary, model and account (CLAUDE_CONFIG_DIR). Pure helpers of fallback.py first, then
the bridge end to end with fake agents (no network, no real ~/.orkestra)."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_bridge_turns import B, TurnBase, wait_for  # noqa: E402
from orkestra_core import fallback as FB  # noqa: E402
from orkestra_core.i18n import t  # noqa: E402

LIMITED = """#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "a.ran"), "w").write("1")
print(json.dumps({"type": "result", "is_error": True, "result": "You've hit your weekly limit · resets Nov 9 at 1pm"}))
sys.exit(1)
"""

RECORDER = """#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
here = os.path.dirname(os.path.abspath(__file__))
json.dump({"argv": sys.argv[1:], "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "prompt": prompt},
          open(os.path.join(here, "b.json"), "w"))
print(json.dumps({"type": "result", "result": "continued"}))
"""


class TestFallbackHelpers(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="fallback-")).resolve()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chain_keeps_valid_profiles_in_order(self):
        cfg = {"engine": {"chain": [{"engine": "claude", "name": "a"}, "junk", {"engine": "nope"},
                                    {"engine": "cursor", "model": "grok"}]}}
        got = FB.chain(cfg)
        self.assertEqual([(p["name"], p["engine"], p["index"]) for p in got], [("a", "claude", 0), ("cursor-4", "cursor", 1)])
        self.assertEqual(FB.chain({}), [])

    def test_resolve_bin_picks_the_newest_glob_match(self):
        for v in ("2.1.9", "2.1.318", "2.1.30"):
            d = self.tmp / f"anthropic.claude-code-{v}" / "bin"
            d.mkdir(parents=True)
            (d / "claude").write_text("#!/bin/sh\n")
            (d / "claude").chmod(0o755)
        got = FB.resolve_bin({"engine": "claude", "bin": str(self.tmp / "anthropic.claude-code-*" / "bin" / "claude")})
        self.assertIn("2.1.318", got)
        self.assertIsNone(FB.resolve_bin({"engine": "claude", "bin": str(self.tmp / "missing-*" / "claude")}))

    def test_with_model_replaces_every_model_flag(self):
        self.assertEqual(FB.with_model(["--model", "x", "--effort", "high", "--model=y"], "m"),
                         ["--effort", "high", "--model", "m"])
        self.assertEqual(FB.with_model(["--model", "x"], ""), ["--model", "x"])

    def test_limit_texts(self):
        for text in ("You've hit your weekly limit · resets Nov 9", "Claude AI usage limit reached|1793",
                     "You've hit your usage limit", "Credit balance is too low", "Invalid API key · Please run /login"):
            self.assertTrue(FB.is_limit_error(text), text)
        for text in ("API Error: 529 overloaded", "done", "", None):
            self.assertFalse(FB.is_limit_error(text), text)

    def test_apply_env_sets_or_removes_the_account(self):
        env = {"CLAUDE_CONFIG_DIR": "/inherited"}
        self.assertEqual(FB.apply_env({"engine": "claude", "config_dir": "/acct"}, dict(env))["CLAUDE_CONFIG_DIR"], "/acct")
        self.assertNotIn("CLAUDE_CONFIG_DIR", FB.apply_env({"engine": "claude", "config_dir": ""}, dict(env)))
        self.assertEqual(FB.apply_env({"engine": "cursor"}, dict(env)), env)

    def test_cooldown_skips_a_limited_profile_until_it_expires(self):
        exe = self.tmp / "agent"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        profiles = FB.chain({"engine": {"chain": [{"name": "a", "engine": "claude", "bin": str(exe)},
                                                  {"name": "b", "engine": "claude", "bin": str(exe)}]}})
        FB.mark_limited(self.tmp, profiles[0], 60, now=1000.0)
        self.assertEqual(FB.pick(profiles, self.tmp, now=1010.0)["name"], "b")
        self.assertEqual(FB.pick(profiles, self.tmp, now=1061.0)["name"], "a")
        FB.mark_limited(self.tmp, profiles[1], 60, now=1010.0)
        self.assertIsNone(FB.pick(profiles, self.tmp, now=1020.0))

    def test_profile_for_session_finds_the_account_that_holds_the_transcript(self):
        a, b = self.tmp / "acct-a", self.tmp / "acct-b"
        sid = "e41ea997-d70c-4fb1-8926-8474b759a100"
        (b / "projects" / "-Users-x-Projects-demo").mkdir(parents=True)
        (b / "projects" / "-Users-x-Projects-demo" / f"{sid}.jsonl").write_text("{}\n")
        profiles = FB.chain({"engine": {"chain": [{"name": "a", "engine": "claude", "config_dir": str(a)},
                                                  {"name": "b", "engine": "claude", "config_dir": str(b)}]}})
        self.assertEqual(FB.profile_for_session(profiles, sid)["name"], "b")
        self.assertIsNone(FB.profile_for_session(profiles, "00000000-0000-0000-0000-000000000000"))
        self.assertIsNone(FB.profile_for_session(profiles, "../../etc"))


class TestBridgeFailover(TurnBase):
    def setUp(self):
        super().setUp()
        self.a = self.agent(LIMITED)
        b = self.tmp / "b-agent.py"
        b.write_text(RECORDER)
        b.chmod(0o755)
        self.b = str(b)
        self.acct_b = str(self.tmp / "acct-b")

    def chain(self, *profiles):
        (self.home / "config.json").write_text(json.dumps({"engine": {"chain": list(profiles)}}))

    def test_a_limit_moves_the_job_to_the_next_profile_with_its_account_and_model(self):
        self.chain({"name": "first", "engine": "claude", "bin": self.a, "model": "claude-opus-5-5"},
                   {"name": "second", "engine": "claude", "bin": self.b, "config_dir": self.acct_b,
                    "model": "claude-opus-5-5"})
        profile = FB.chain(B.OP.global_config())[0]
        job = self.job(profile=profile)
        old_sid = job["session_id"]
        B.run_turn(self.cfg(self.a), job, "/orkestra:run demo work", new=True)
        rec_path = self.tmp / "b.json"
        self.assertTrue(wait_for(lambda: rec_path.exists() and rec_path.stat().st_size > 0, timeout=20))
        rec = json.loads(rec_path.read_text())
        self.assertEqual(rec["config_dir"], self.acct_b)
        self.assertEqual(rec["argv"][rec["argv"].index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(rec["argv"].count("--model"), 1)
        self.assertIn("--session-id", rec["argv"])                       # a NEW session on the other account
        self.assertIn("orkestra", rec["prompt"])
        done = wait_for(lambda: (B.load_job(job["id"]) or {}).get("status") in ("done", "error", "awaiting_user")
                        and B.load_job(job["id"]), timeout=20)
        self.assertEqual(done["profile"]["name"], "second")
        self.assertNotEqual(done["session_id"], old_sid)
        self.assertEqual(done["failovers"][0]["from"], "first")
        self.assertTrue(FB.limited_until(self.home, profile))
        self.assertTrue(any("second" in str(n.get("last_message")) for n in self.notified))

    def test_every_profile_limited_hands_the_job_back_without_auto_resume(self):
        self.chain({"name": "only", "engine": "claude", "bin": self.a})
        profile = FB.chain(B.OP.global_config())[0]
        job = self.job(profile=profile)
        with mock.patch.object(B, "watchdog_cfg", lambda: {"auto_resume": "headless", "max_auto_resumes": 3}):
            B.run_turn(self.cfg(self.a), job, "go", new=True)
            done = wait_for(lambda: (B.load_job(job["id"]) or {}).get("status") == "awaiting_user"
                            and B.load_job(job["id"]), timeout=20)
        self.assertIn(t("bridge.turn.chain_exhausted"), done["last_message"])
        self.assertFalse(done.get("auto_resumes"))

    def test_without_a_chain_nothing_changes(self):
        job = self.job()
        B.run_turn(self.cfg(self.a), job, "go", new=True)
        done = wait_for(lambda: (B.load_job(job["id"]) or {}).get("status") in ("done", "error", "awaiting_user")
                        and B.load_job(job["id"]), timeout=20)
        self.assertNotIn("profile", done)
        self.assertNotIn("failovers", done)
        self.assertFalse((self.home / "bridge" / "limits.json").exists())

    def test_new_job_starts_on_the_first_profile_that_is_not_cooling_down(self):
        self.chain({"name": "first", "engine": "claude", "bin": self.a},
                   {"name": "second", "engine": "claude", "bin": self.b, "config_dir": self.acct_b})
        FB.mark_limited(self.home, {"name": "first"}, 600)
        with mock.patch.object(B, "run_turn", lambda *a, **k: None), \
                mock.patch.object(B.R, "open_run_for_request", lambda *a, **k: {"run_id": "001-x"}), \
                mock.patch.object(B, "project_active_run", lambda p: None):
            job = B.start_job(self.cfg(self.a), "demo", "do the thing", None, source="api")
        self.assertEqual(job["profile"]["name"], "second")
        self.assertEqual(job["engine"], "claude")


if __name__ == "__main__":
    unittest.main()
