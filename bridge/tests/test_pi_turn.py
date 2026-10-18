"""A bridge turn for engine pi starts `pi --mode json` and never falls through to claude."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["ORKESTRA_NO_DAEMON"] = "1"
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "plugins" / "orkestra" / "lib"))
sys.path.insert(0, str(_ROOT / "bridge"))

import orkestra_bridge as B
from orkestra_core.i18n import t


def _job():
    return {"id": "j1", "engine": "pi", "session_id": "pi-job1", "project_path": "/tmp/demo",
            "status": "queued", "turns": [], "last_message": "", "approval_mode": "autopilot"}


class TestPiTurn(unittest.TestCase):
    def test_prepare_uses_json_mode_and_not_the_claude_binary(self) -> None:
        import tempfile
        from orkestra_core import pikit as PK
        job = _job()
        cfg = {"claude_bin": "/bin/claude"}
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"ORKESTRA_PI_HOME": home}), \
                mock.patch.object(B, "save_job"), mock.patch.object(B, "notify"), \
                mock.patch.object(B.EN, "binary", return_value="/usr/bin/pi"):
            bare = B._prepare_turn(cfg, _job(), "hello", True)
            self.assertIsNone(bare)
            PK.install()
            prep = B._prepare_turn(cfg, job, "hello", True)
        self.assertIsNotNone(prep)
        self.assertEqual(prep["spec"]["argv"][:5],
                         ["/usr/bin/pi", "--mode", "json", "--session-id", "job1"])
        self.assertIn("--extension", prep["spec"]["argv"])
        self.assertNotIn("/bin/claude", prep["spec"]["argv"])
        self.assertEqual(prep["engine"], "pi")

    def test_missing_pi_is_an_error_and_does_not_start_claude(self) -> None:
        job = _job()
        cfg = {"claude_bin": "/bin/claude"}
        with mock.patch.object(B.EN, "binary", return_value=None), \
                mock.patch.object(B, "save_job") as save, \
                mock.patch.object(B, "notify"):
            prep = B._prepare_turn(cfg, job, "hello", True)
        self.assertIsNone(prep)
        self.assertEqual(job["status"], "error")
        self.assertIn("pi", job["last_message"])
        self.assertNotIn("/bin/claude", job["last_message"])
        self.assertEqual(job["last_message"], t("bridge.turn.binary_missing", engine="pi", binary="pi"))
        save.assert_called()
