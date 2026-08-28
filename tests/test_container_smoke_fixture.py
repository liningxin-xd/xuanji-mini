from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.container_dview_stub import _attribution_results


ROOT = Path(__file__).resolve().parents[1]


class ContainerSmokeFixtureTest(unittest.TestCase):
    def test_dview_stub_supports_direct_script_execution(self):
        env = dict(os.environ)
        for name in (
            "XUANJI_SMOKE_DVIEW_PORT",
            "XUANJI_SMOKE_DVIEW_COUNT_FILE",
            "XUANJI_SMOKE_ANALYSIS_PROFILE",
        ):
            env.pop(name, None)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "container_dview_stub.py")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertIn("XUANJI_SMOKE_DVIEW_PORT", completed.stderr)

    def test_profile_query_fixture_exercises_v2_post_primary_only(self):
        expected = {
            "primary_v1": (
                [
                    "game_id",
                    "is_reserve_auto_download",
                    "device_brand",
                    "channel_group",
                    "app_major_version",
                    "os_major_version",
                    "apk_size_tier",
                ],
                [],
            ),
            "primary_v2": (
                [
                    "game_id",
                    "is_reserve_auto_download",
                    "device_brand",
                    "channel_group",
                    "app_major_version",
                    "os_major_version",
                    "apk_size_tier",
                ],
                ["secondary", "game_background"],
            ),
        }
        for profile, (primary_steps, post_steps) in expected.items():
            with self.subTest(profile=profile):
                fixture = _attribution_results(profile)
                self.assertEqual(
                    primary_steps,
                    [
                        item["step_id"]
                        for item in fixture.values()
                        if item["phase"] == "primary"
                    ],
                )
                self.assertEqual(
                    post_steps,
                    [
                        item["step_id"]
                        for item in fixture.values()
                        if item["phase"] == "post_primary"
                    ],
                )


if __name__ == "__main__":
    unittest.main()
