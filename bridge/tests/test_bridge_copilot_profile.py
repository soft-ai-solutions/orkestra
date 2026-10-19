"""The bridge builds Copilot options through the same resolver as `orkestra run` (engines.copilot_resolve)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["ORKESTRA_NO_DAEMON"] = "1"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "plugins" / "orkestra" / "lib"))

import orkestra_bridge as B  # noqa: E402
from orkestra_core import engines as EN  # noqa: E402


class BridgeCopilotProfileTest(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("ORKESTRA_HOME")
        self.tmp = tempfile.mkdtemp(prefix="bridge-profile-")
        os.environ["ORKESTRA_HOME"] = self.tmp

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ORKESTRA_HOME", None)
        else:
            os.environ["ORKESTRA_HOME"] = self.old

    def test_defaults_request_words_and_job_choice(self):
        o = B.copilot_options({})
        self.assertEqual(EN.copilot_profile_of(o), {"model": "gpt-6-luna", "reasoning_effort": "max",
                                                    "context": "long_context", "allow_all": True})
        o = B.copilot_options({}, request="use gpt-6-sol, use default context, no allow all")
        self.assertEqual(EN.copilot_profile_of(o), {"model": "gpt-6-sol", "reasoning_effort": "max",
                                                    "context": "default", "allow_all": False})
        o = B.copilot_options({}, selected_model="m-job", selected_context="default", selected_allow_all=False)
        self.assertEqual((o["model"], o["context"], o["allow_all"]), ("m-job", "default", False))

    def test_request_words_never_raise_allow_all_or_flip_context(self):
        cfg = {"copilot_allow_all": False, "copilot_context": "long_context"}
        o = B.copilot_options(cfg, request="add CORS allow all origins and change the default context menu")
        self.assertEqual((o["allow_all"], o["context"]), (False, "long_context"))
        o = B.copilot_options(cfg, request="with allow all", selected_allow_all=False)
        self.assertIs(o["allow_all"], False)

    def test_bridge_config_keys_beat_global_config_and_garbage_is_rejected(self):
        o = B.copilot_options({"copilot_reasoning_effort": "high", "copilot_allow_all": False})
        self.assertEqual((o["reasoning_effort"], o["allow_all"]), ("high", False))
        with self.assertRaises(EN.EngineError):
            B.copilot_options({"copilot_context": "huge"})


if __name__ == "__main__":
    unittest.main()
