"""Owner commands: the bridge marks a headless session as the owner's (Telegram passphrase, checked by the OpenClaw
plugin) and the hooks then allow what a release needs (commit, push, branch, PR, tag, deploy) for that session only,
while anything that cannot be undone stays refused whatever the words of the request say."""
import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts lib/ on sys.path)
from orkestra_core import guards, paths
from orkestra_core import state as ST
from orkestra_core.paths import DEFAULT_GLOBAL_CONFIG

CFG = copy.deepcopy(DEFAULT_GLOBAL_CONFIG)
SID = "11111111-2222-4333-8444-555566667777"


def bash(cmd, cwd="/w/proj"):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}


def binding(project="/w/proj"):
    return {"project": project, "run_id": "001-x",
            "state": {"phase": "implement", "status": "active", "approved": {"deps": []}}}


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="owner-grant-")
        patcher = mock.patch.dict(os.environ, {"ORKESTRA_HOME": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)


class OwnerGrantState(HomeCase):
    def test_an_owner_session_gets_the_release_grants(self):
        ST.write_owner_grant(SID, "job123456789")
        cats = set(ST.current_grant(SID, None))
        self.assertTrue({"commit", "push", "branch", "pr", "tag", "sync", "stash", "gh_write", "deploy"} <= cats)

    def test_irreversible_grants_are_stripped_even_when_the_request_asks_for_them(self):
        ST.write_grant(SID, "p1", ["commit", "push", "force", "hard_reset", "discard", "amend"], "x")
        ST.write_owner_grant(SID, "job123456789")
        cats = set(ST.current_grant(SID, "p1"))
        self.assertFalse(cats & {"force", "hard_reset", "discard", "amend"})
        self.assertIn("push", cats)

    def test_other_sessions_and_plain_sessions_are_untouched(self):
        ST.write_owner_grant(SID, "job123456789")
        self.assertEqual(ST.current_grant("another-session", None), [])
        ST.write_grant("plain", "p1", ["force", "push"], "x")
        self.assertEqual(sorted(ST.current_grant("plain", "p1")), ["force", "push"])   # no owner file: as before

    def test_an_expired_or_cleared_owner_grant_gives_nothing(self):
        ST.write_owner_grant(SID, "job123456789")
        ST.clear_owner_grant(SID)
        self.assertEqual(ST.current_grant(SID, None), [])
        ST.write_owner_grant(SID, "job123456789")
        old = time.time() - ST.OWNER_TTL - 5
        with mock.patch.object(ST.time, "time", return_value=old + ST.OWNER_TTL + 10):
            self.assertIsNotNone(ST.owner_grant(SID))
        f = paths.grants_dir() / "owner" / f"{SID}.json"
        data = f.read_text()
        f.write_text(data.replace('"ts": ', '"ts": 1, "old_ts": '))
        self.assertIsNone(ST.owner_grant(SID))
        self.assertEqual(ST.current_grant(SID, None), [])

    def test_a_session_id_cannot_reach_another_file(self):
        ST.write_owner_grant("../../x", "job")
        self.assertFalse((Path(self.tmp).parent / "x.json").exists())
        self.assertIsNone(ST.owner_grant(""))


class OwnerGrantGuards(HomeCase):
    def decide(self, cmd, grants):
        return guards.pre_tool_decision(bash(cmd), CFG, binding(), grants)

    def owner(self):
        ST.write_owner_grant(SID, "job123456789")
        return ST.current_grant(SID, None)

    def test_deploy_runs_in_an_owner_session(self):
        grants = self.owner()
        for c in ["npm run deploy:spaceship", "npm run release:prod:local", "vercel --prod", "fly deploy",
                  "wrangler deploy", "firebase deploy", "prisma migrate deploy", "docker push x/y",
                  "rsync -av dist/ root@host:/var/www", "ssh deploy@host 'systemctl restart app'", "make deploy",
                  "terraform apply"]:
            self.assertFalse(self.decide(c, grants).denied, c)

    def test_destroying_remote_things_stays_refused_in_an_owner_session(self):
        grants = self.owner()
        for c in ["vercel remove site", "fly destroy app", "fly apps destroy app", "terraform destroy",
                  "kubectl delete pod x", "helm uninstall app", "wrangler delete", "prisma migrate reset",
                  "prisma db push", "prisma db execute --file x.sql", "supabase db reset", "drizzle-kit drop",
                  "ssh root@host 'rm -rf /var/www'", "ssh root@host \"psql -c 'DROP TABLE users'\"",
                  "rsync -av --delete dist/ root@host:/var/www", "aws s3 rm s3://bucket/key",
                  "aws s3 sync dist s3://bucket --delete", "heroku apps:destroy app", "railway down",
                  "firebase functions:delete fn", "netlify sites:delete"]:
            d = self.decide(c, grants)
            self.assertTrue(d.denied, c)
            self.assertEqual(d.guard, "deploy", c)

    def test_without_the_owner_grant_deploy_is_refused_as_before(self):
        for c in ["npm run deploy:spaceship", "vercel --prod", "fly deploy"]:
            self.assertTrue(self.decide(c, ["commit", "push"]).denied, c)

    def test_git_release_steps_pass_and_history_rewrites_do_not(self):
        ST.write_grant(SID, "p1", ["force", "hard_reset", "discard"], "x")
        ST.write_owner_grant(SID, "job123456789")
        grants = ST.current_grant(SID, "p1")
        for c in ["git commit -m x", "git push origin main", "git checkout -b feature/x", "git tag v1.2.3"]:
            self.assertFalse(guards.pre_tool_decision(bash(c), CFG, None, grants).denied, c)
        for c in ["git push --force origin feature/x", "git push -f origin feature/x", "git reset --hard HEAD~1",
                  "git clean -fd", "git checkout -- .", "git commit --amend -m y"]:
            self.assertTrue(guards.pre_tool_decision(bash(c), CFG, None, grants).denied, c)

    def test_recursive_delete_outside_temp_stays_refused(self):
        grants = self.owner()
        self.assertTrue(self.decide("rm -rf src", grants).denied)


if __name__ == "__main__":
    unittest.main()
