from __future__ import annotations

import unittest

from tests.container_dview_stub import _attribution_results


class ContainerSmokeFixtureTest(unittest.TestCase):
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
