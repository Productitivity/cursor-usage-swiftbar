from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "cursor-usage.1m.py"
SPEC = importlib.util.spec_from_file_location("cursor_usage", PLUGIN_PATH)
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)


LIVE_USAGE_PAYLOAD = {
    "billingCycleStart": "1786574090000",
    "billingCycleEnd": "1789252490000",
    "planUsage": {
        "totalSpend": 460,
        "includedSpend": 460,
        "remaining": 39540,
        "limit": 40000,
        "remainingBonus": False,
        "autoPercentUsed": 0.23,
        "apiPercentUsed": 0,
        "totalPercentUsed": 0.184,
    },
    "displayMessage": "You've used 1% of your included usage",
}

LIVE_PLAN_PAYLOAD = {
    "planInfo": {
        "planName": "Ultra",
        "includedAmountCents": 40000,
        "price": "$200/mo",
        "billingCycleEnd": "1789252490000",
        "planOwner": "PLAN_OWNER_STRIPE",
    }
}


def sample_usage(**overrides: object) -> plugin.PeriodUsage:
    usage = plugin.parse_period_usage(LIVE_USAGE_PAYLOAD)
    if not overrides:
        return usage
    values = usage.__dict__.copy()
    values.update(overrides)
    return plugin.PeriodUsage(**values)


def sample_snapshot(usage: plugin.PeriodUsage | None = None) -> plugin.UsageSnapshot:
    return plugin.UsageSnapshot(
        usage=usage or sample_usage(),
        plan=plugin.parse_plan_info(LIVE_PLAN_PAYLOAD),
        fetched_at="2026-08-13T06:04:00+00:00",
    )


class CursorUsageTests(unittest.TestCase):
    def test_parse_period_usage_uses_charged_spend_not_display_percent(self) -> None:
        usage = plugin.parse_period_usage(LIVE_USAGE_PAYLOAD)

        self.assertEqual(usage.included_spend_cents, 460)
        self.assertEqual(usage.limit_cents, 40000)
        self.assertEqual(usage.remaining_cents, 39540)
        self.assertAlmostEqual(usage.used_percent or 0, 1.15)
        self.assertAlmostEqual(usage.auto_percent_used or 0, 0.23)
        self.assertEqual(usage.billing_cycle_end, 1789252490)

    def test_parse_period_usage_computes_remaining_when_field_is_missing(self) -> None:
        payload = {
            "planUsage": {"includedSpend": 2500, "limit": 10000},
            "billingCycleEnd": 1789252490,
        }

        usage = plugin.parse_period_usage(payload)

        self.assertEqual(usage.remaining_cents, 7500)
        self.assertEqual(usage.used_percent, 25)
        self.assertEqual(usage.billing_cycle_end, 1789252490)

    def test_malformed_spend_values_are_ignored(self) -> None:
        usage = plugin.parse_period_usage({"planUsage": {"includedSpend": -1, "limit": "nope"}})

        self.assertIsNone(usage.included_spend_cents)
        self.assertIsNone(usage.limit_cents)
        self.assertIsNone(usage.used_percent)

    def test_parse_plan_info_keeps_only_display_fields(self) -> None:
        plan = plugin.parse_plan_info(LIVE_PLAN_PAYLOAD)

        assert plan is not None
        self.assertEqual(plan.name, "Ultra")
        self.assertEqual(plan.price, "$200/mo")
        self.assertEqual(plan.included_amount_cents, 40000)

    def test_read_access_token_returns_the_cursor_auth_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "state.vscdb"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("cursorAuth/accessToken", "  test-token  "),
            )
            connection.commit()
            connection.close()

            token = plugin.read_access_token(db_path)

        self.assertEqual(token, "test-token")

    def test_read_access_token_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "state.vscdb"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            connection.commit()
            connection.close()

            self.assertIsNone(plugin.read_access_token(db_path))

    def test_dashboard_used_percent_matches_cursor_ceiling(self) -> None:
        self.assertEqual(plugin.dashboard_used_percent(0), 0)
        self.assertEqual(plugin.dashboard_used_percent(0.23), 1)
        self.assertEqual(plugin.dashboard_used_percent(0.7605), 1)
        self.assertEqual(plugin.dashboard_used_percent(3.8025), 4)
        self.assertEqual(plugin.dashboard_used_percent(1.0), 1)
        self.assertEqual(plugin.dashboard_remaining_percent(0.23), 99)
        self.assertEqual(plugin.dashboard_remaining_percent(0), 100)

    def test_format_remaining_percent_keeps_api_decimals(self) -> None:
        self.assertEqual(plugin.format_remaining_percent(99.77), "99.77%")
        self.assertEqual(plugin.format_remaining_percent(100), "100.00%")
        self.assertEqual(plugin.format_remaining_percent(50), "50.00%")
        self.assertEqual(plugin.format_remaining_percent(0), "0.00%")
        self.assertEqual(plugin.format_percent(3.0975), "3.10%")
        self.assertEqual(plugin.format_percent(0.4), "0.40%")
        self.assertIsNone(plugin.format_remaining_percent(None))

    def test_menu_bar_title_is_the_remaining_percentage_with_cursor_label(self) -> None:
        output = io.StringIO()
        snapshot = sample_snapshot()

        with patch.object(plugin, "today_usage", return_value=plugin.DailyUsageState(
            date="2026-08-13",
            used_today_percent=0.4,
            used_today_cents=160,
            previous_used_percent=1.15,
            previous_included_spend_cents=460,
            billing_cycle_end=1789252490,
        )), patch.object(plugin, "usage_speed_per_hour", return_value=None), patch.object(
            plugin, "average_daily_pace", return_value=1.68
        ), patch.object(plugin, "target_daily_pace", return_value=3.40), patch.object(
            plugin, "data_status", return_value=("OK · just now", "green")
        ):
            with redirect_stdout(output):
                plugin.print_menu(snapshot)

        menu_lines = output.getvalue().splitlines()
        self.assertEqual(menu_lines[0], "Cursor 99.77%")
        self.assertTrue(
            any(line.startswith("Remaining |") and "href=https://cursor.com/dashboard/spending" in line for line in menu_lines)
        )
        self.assertTrue(
            any("Cursor Models" in line and "99.77%" in line and "href=" in line and "color=green" not in line for line in menu_lines)
        )
        self.assertFalse(any("Grok · Composer" in line for line in menu_lines))
        self.assertTrue(
            any("Other Models" in line and "100.00%" in line and "href=" not in line for line in menu_lines)
        )
        self.assertTrue(any(line.startswith("Cycle |") and "color=" in line for line in menu_lines))
        self.assertFalse(
            any("disabled=true" in line for line in menu_lines if "Cursor Models" in line or line.startswith("Cycle |"))
        )
        self.assertFalse(any("Open Dashboard" in line for line in menu_lines))
        self.assertFalse(any(line.startswith("Refresh |") for line in menu_lines))
        self.assertTrue(any("Today" in line and "0.40%" in line for line in menu_lines))
        self.assertTrue(any(line.startswith("-- ") and "Hourly" in line and "collecting 1h" in line for line in menu_lines))
        self.assertFalse(any(not line.startswith("-- ") and "Hourly" in line for line in menu_lines))
        self.assertTrue(any(line.startswith("Pace |") for line in menu_lines))
        self.assertTrue(
            any("Average" in line and " / " in line and "On pace" in line and "color=green" not in line for line in menu_lines)
        )
        self.assertFalse(any("Target" in line and "/day" in line for line in menu_lines))
        self.assertTrue(any("Resets" in line and " · " in line for line in menu_lines))
        self.assertFalse(any("Resets in" in line for line in menu_lines))
        self.assertFalse(any("Next reset" in line for line in menu_lines))
        self.assertFalse(any("Window" in line for line in menu_lines))
        self.assertTrue(any(line.startswith("-- ") and "Plan" in line and "Ultra · $200/mo" in line for line in menu_lines))
        self.assertFalse(any(not line.startswith("-- ") and "Plan" in line for line in menu_lines))
        self.assertFalse(any("Status" in line for line in menu_lines))
        self.assertFalse(any("Open Dashboard" in line for line in menu_lines))
        self.assertFalse(any(line.startswith("Refresh |") for line in menu_lines))

    def test_missing_snapshot_displays_an_error_instead_of_a_stale_value(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            plugin.print_menu(None)

        menu_lines = output.getvalue().splitlines()
        self.assertEqual(menu_lines[0], "Cursor ERROR | color=red")
        self.assertTrue(
            any("Previous values are not shown |" in line and "color=" in line for line in menu_lines)
        )
        self.assertIn("Refresh | refresh=true", menu_lines)
        self.assertFalse(any("Open Dashboard" in line for line in menu_lines))

    def test_dark_appearance_uses_bright_readable_menu_text(self) -> None:
        with patch.dict(os.environ, {"OS_APPEARANCE": "Dark"}):
            self.assertEqual(plugin.menu_text_color(), "#F4F6F8")
            self.assertEqual(plugin.menu_secondary_color(), "#B8C2CC")

    def test_data_status_reports_freshness_and_missing_timestamps(self) -> None:
        now = datetime(2026, 8, 13, 6, 5, tzinfo=timezone.utc).astimezone()

        self.assertEqual(
            plugin.data_status("2026-08-13T06:04:00+00:00", now),
            ("OK · just now", "green"),
        )
        self.assertEqual(
            plugin.data_status("2026-08-13T05:57:00+00:00", now),
            ("Delayed · 8m ago", "orange"),
        )
        self.assertEqual(plugin.data_status(None, now), ("Unknown", "orange"))

    def test_stale_status_is_shown_when_data_is_not_ok(self) -> None:
        output = io.StringIO()
        snapshot = sample_snapshot()

        with patch.object(plugin, "today_usage", return_value=plugin.DailyUsageState(
            date="2026-08-13",
            used_today_percent=0.4,
            used_today_cents=160,
            previous_used_percent=1.15,
            previous_included_spend_cents=460,
            billing_cycle_end=1789252490,
        )), patch.object(plugin, "usage_speed_per_hour", return_value=None), patch.object(
            plugin, "data_status", return_value=("Stale · 20m ago", "red")
        ):
            with redirect_stdout(output):
                plugin.print_menu(snapshot)

        self.assertTrue(
            any("Status" in line and "Stale · 20m ago" in line and "color=red" in line for line in output.getvalue().splitlines())
        )

    def test_reset_event_requires_a_newer_billing_cycle(self) -> None:
        self.assertFalse(plugin.is_reset_event(None, 200))
        self.assertFalse(plugin.is_reset_event(200, 200))
        self.assertFalse(plugin.is_reset_event(200, 100))
        self.assertTrue(plugin.is_reset_event(200, 300))

    def test_today_usage_starts_at_zero_then_accumulates_new_spend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "cursor-daily-usage.json"
            now = datetime(2026, 8, 13, 6, tzinfo=timezone.utc).astimezone()
            started_earlier = int(now.timestamp()) - 7 * 24 * 60 * 60
            first = sample_usage(auto_percent_used=1.15, billing_cycle_start=started_earlier)
            second = sample_usage(auto_percent_used=1.4, billing_cycle_start=started_earlier)

            with patch.object(plugin, "daily_usage_state_path", return_value=state_path):
                first_usage = plugin.today_usage(first, now)
                second_usage = plugin.today_usage(second, now)

        assert first_usage is not None
        assert second_usage is not None
        self.assertEqual(first_usage.used_today_percent, 0)
        self.assertAlmostEqual(first_usage.day_start_percent, 1.15)
        self.assertAlmostEqual(second_usage.used_today_percent, 0.25)

    def test_today_usage_uses_first_observation_as_the_next_day_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "cursor-daily-usage.json"
            kst = timezone(timedelta(hours=9))
            day1 = datetime(2026, 8, 13, 22, 0, tzinfo=kst)
            day2 = datetime(2026, 8, 14, 10, 0, tzinfo=kst)
            started = int(datetime(2026, 8, 13, 7, 34, tzinfo=kst).timestamp())

            with patch.object(plugin, "daily_usage_state_path", return_value=state_path):
                plugin.today_usage(
                    sample_usage(auto_percent_used=1.08, billing_cycle_start=started),
                    day1,
                )
                first_today = plugin.today_usage(
                    sample_usage(auto_percent_used=1.20, billing_cycle_start=started),
                    day2,
                )
                later_today = plugin.today_usage(
                    sample_usage(auto_percent_used=1.45, billing_cycle_start=started),
                    day2,
                )

        assert first_today is not None
        assert later_today is not None
        self.assertEqual(first_today.used_today_percent, 0)
        self.assertAlmostEqual(first_today.day_start_percent, 1.20)
        self.assertAlmostEqual(later_today.used_today_percent, 0.25)

    def test_today_usage_equals_period_used_when_the_cycle_started_today(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "cursor-daily-usage.json"
            now = datetime(2026, 8, 13, 18, 9, tzinfo=timezone(timedelta(hours=9)))
            started = int(datetime(2026, 8, 13, 7, 34, tzinfo=now.tzinfo).timestamp())
            usage = sample_usage(auto_percent_used=1.08, billing_cycle_start=started)

            with patch.object(plugin, "daily_usage_state_path", return_value=state_path):
                first_usage = plugin.today_usage(usage, now)
                state_path.write_text(
                    json.dumps(
                        {
                            "date": "2026-08-13",
                            "used_today_percent": 0.11,
                            "used_today_cents": 0,
                            "previous_used_percent": 0.97,
                            "previous_included_spend_cents": 0,
                            "billing_cycle_end": usage.billing_cycle_end,
                            "quota": "auto",
                        }
                    ),
                    encoding="utf-8",
                )
                caught_up = plugin.today_usage(usage, now)

        assert first_usage is not None
        assert caught_up is not None
        self.assertAlmostEqual(first_usage.used_today_percent, 1.08)
        self.assertEqual(first_usage.day_start_percent, 0)
        self.assertAlmostEqual(caught_up.used_today_percent, 1.08)

    def test_today_usage_rebases_legacy_dollar_pool_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "cursor-daily-usage.json"
            state_path.write_text(
                json.dumps(
                    {
                        "date": "2026-08-13",
                        "used_today_percent": 3.0975,
                        "used_today_cents": 0,
                        "previous_used_percent": 4.455,
                        "previous_included_spend_cents": 0,
                        "billing_cycle_end": 1789252490,
                    }
                ),
                encoding="utf-8",
            )
            now = datetime(2026, 8, 13, 6, tzinfo=timezone.utc).astimezone()

            with patch.object(plugin, "daily_usage_state_path", return_value=state_path):
                rebased = plugin.today_usage(sample_usage(auto_percent_used=0.92), now)

        assert rebased is not None
        self.assertAlmostEqual(rebased.used_today_percent, 0.92)
        self.assertAlmostEqual(rebased.previous_used_percent, 0.92)
        self.assertEqual(rebased.quota, "auto")

    def test_today_usage_adds_the_new_window_after_a_billing_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "cursor-daily-usage.json"
            now = datetime(2026, 8, 13, 6, tzinfo=timezone.utc).astimezone()
            started_earlier = int(now.timestamp()) - 20 * 24 * 60 * 60
            before_reset = sample_usage(
                auto_percent_used=99,
                billing_cycle_start=started_earlier,
                billing_cycle_end=100,
            )
            after_reset = sample_usage(
                auto_percent_used=2, billing_cycle_end=200
            )

            with patch.object(plugin, "daily_usage_state_path", return_value=state_path):
                plugin.today_usage(before_reset, now)
                plugin.today_usage(
                    sample_usage(
                        auto_percent_used=99.5,
                        billing_cycle_start=started_earlier,
                        billing_cycle_end=100,
                    ),
                    now,
                )
                reset_usage = plugin.today_usage(after_reset, now)

        assert reset_usage is not None
        self.assertAlmostEqual(reset_usage.used_today_percent, 2.5)

    def test_usage_speed_is_available_after_one_hour_of_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "cursor-usage-speed.json"
            start = datetime(2026, 8, 13, 6, tzinfo=timezone.utc).astimezone()
            initial = sample_usage(auto_percent_used=20)
            later = sample_usage(auto_percent_used=23)

            with patch.object(plugin, "usage_speed_state_path", return_value=state_path):
                initial_speed = plugin.usage_speed_per_hour(initial, start)
                speed = plugin.usage_speed_per_hour(later, start + timedelta(hours=1))

        self.assertIsNone(initial_speed)
        self.assertIsNotNone(speed)
        assert speed is not None
        self.assertEqual(speed.observed_seconds, 3600)
        self.assertEqual(speed.percent_per_hour, 3)

    def test_daily_pace_uses_fractional_cycle_days(self) -> None:
        usage = sample_usage(auto_percent_used=0.23)
        assert usage.billing_cycle_start is not None
        now = datetime.fromtimestamp(
            usage.billing_cycle_start + 12 * 60 * 60, tz=timezone.utc
        ).astimezone()

        self.assertAlmostEqual(plugin.elapsed_cycle_days(usage, now), 0.5)
        self.assertAlmostEqual(plugin.remaining_cycle_days(usage, now), 30.5)
        self.assertAlmostEqual(plugin.average_daily_pace(usage, now), 0.46)
        self.assertAlmostEqual(plugin.target_daily_pace(usage, now), 99.77 / 30.5)
        self.assertEqual(plugin.daily_pace_color(0.46, 99.77 / 30.5), None)
        self.assertEqual(plugin.daily_pace_color(4.0, 3.0), "orange")
        self.assertIsNone(plugin.remaining_color(97.82))
        self.assertEqual(plugin.remaining_color(20), "orange")
        self.assertEqual(plugin.remaining_color(10), "red")
        self.assertEqual(
            plugin.format_daily_pace_line(1.68, 3.40),
            "1.68% / 3.40%  ·  On pace",
        )
        self.assertEqual(
            plugin.format_daily_pace_line(4.0, 3.0),
            "4.00% / 3.00%  ·  Over pace",
        )
        self.assertEqual(plugin.format_daily_pace_line(None, 3.0), plugin.UNAVAILABLE)
        reset_at = 1789252490
        expected_date = datetime.fromtimestamp(reset_at).astimezone()
        self.assertEqual(
            plugin.format_reset_date(reset_at),
            f"{expected_date:%b} {expected_date.day}",
        )
        self.assertTrue(plugin.format_cycle_reset(reset_at).startswith(plugin.format_reset_date(reset_at) + " · "))

    def test_daily_pace_is_missing_before_the_cycle_starts(self) -> None:
        usage = sample_usage(auto_percent_used=0.23)
        assert usage.billing_cycle_start is not None
        now = datetime.fromtimestamp(usage.billing_cycle_start - 60, tz=timezone.utc).astimezone()

        self.assertIsNone(plugin.elapsed_cycle_days(usage, now))
        self.assertIsNone(plugin.average_daily_pace(usage, now))

    def test_usage_speed_ignores_legacy_observations_without_quota(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "cursor-usage-speed.json"
            start = datetime(2026, 8, 13, 6, tzinfo=timezone.utc).astimezone()
            state_path.write_text(
                json.dumps(
                    {
                        "observations": [
                            {
                                "observed_at": int(start.timestamp()),
                                "used_percent": 4.455,
                                "billing_cycle_end": 1789252490,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(plugin, "usage_speed_state_path", return_value=state_path):
                speed = plugin.usage_speed_per_hour(
                    sample_usage(auto_percent_used=0.92),
                    start + timedelta(hours=1),
                )

        self.assertIsNone(speed)

    def test_threshold_alerts_only_fire_when_a_remaining_level_is_crossed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "cursor-threshold-alerts.json"
            usages = [
                sample_usage(auto_percent_used=45, billing_cycle_end=900),
                sample_usage(auto_percent_used=51, billing_cycle_end=900),
                sample_usage(auto_percent_used=52, billing_cycle_end=900),
                sample_usage(auto_percent_used=76, billing_cycle_end=900),
            ]

            with patch.object(plugin, "threshold_alert_state_path", return_value=state_path), patch.object(
                plugin, "send_notification"
            ) as send_notification:
                for usage in usages:
                    plugin.notify_if_threshold_crossed(usage)

        self.assertEqual(
            send_notification.call_args_list,
            [
                call(
                    "Cursor remaining alert",
                    "Currently 49.00% remaining",
                    "Remaining crossed the 50% threshold.",
                ),
                call(
                    "Cursor remaining alert",
                    "Currently 24.00% remaining",
                    "Remaining crossed the 25% threshold.",
                ),
            ],
        )

    def test_notification_settings_actions_toggle_alert_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "cursor-notification-settings.json"

            with patch.object(plugin, "notification_settings_path", return_value=settings_path):
                self.assertTrue(plugin.toggle_notification_setting("--toggle-reset-alert"))
                self.assertTrue(plugin.toggle_notification_setting("--toggle-threshold-25"))
                settings = plugin.notification_settings()

        self.assertFalse(settings.reset_enabled)
        self.assertTrue(settings.threshold_enabled)
        self.assertEqual(settings.enabled_thresholds, (50, 10))

    def test_notification_settings_render_as_a_submenu_after_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "cursor-notification-settings.json"
            output = io.StringIO()

            with patch.object(plugin, "notification_settings_path", return_value=settings_path):
                with redirect_stdout(output):
                    plugin.print_notification_settings()

        menu_lines = output.getvalue().splitlines()
        self.assertEqual(menu_lines[0], "---")
        self.assertEqual(menu_lines[1], "Alerts | color=#1E293B")
        self.assertIn("-- Notify on reset: On", menu_lines[2])
        self.assertIn("-- Remaining alerts: On", menu_lines[3])
        self.assertIn("---- 50% remaining: On", menu_lines[5])

    def test_fetch_snapshot_requires_usable_usage_even_if_plan_info_is_missing(self) -> None:
        with patch.object(plugin, "cursor_rpc", side_effect=[LIVE_USAGE_PAYLOAD, None]) as rpc:
            snapshot = plugin.fetch_snapshot("token", datetime(2026, 8, 13, 6, tzinfo=timezone.utc))

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertAlmostEqual(snapshot.usage.auto_percent_used or 0, 0.23)
        self.assertIsNone(snapshot.plan)
        self.assertEqual(
            [call.args[0] for call in rpc.call_args_list],
            ["GetCurrentPeriodUsage", "GetPlanInfo"],
        )

    def test_fetch_snapshot_returns_none_when_usage_payload_is_empty(self) -> None:
        with patch.object(plugin, "cursor_rpc", return_value={}):
            self.assertIsNone(plugin.fetch_snapshot("token"))

    def test_fetch_snapshot_requires_cursor_models_percent(self) -> None:
        payload = {
            "planUsage": {"includedSpend": 460, "limit": 40000, "remaining": 39540},
            "billingCycleEnd": "1789252490000",
        }
        with patch.object(plugin, "cursor_rpc", side_effect=[payload, LIVE_PLAN_PAYLOAD]):
            self.assertIsNone(plugin.fetch_snapshot("token"))

    def test_cursor_rpc_keeps_the_access_token_out_of_argv(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok": true}\n200', stderr=""
        )
        with patch.object(plugin.shutil, "which", return_value="/usr/bin/curl"), patch.object(
            plugin.subprocess, "run", return_value=completed
        ) as run:
            payload = plugin.cursor_rpc("GetCurrentPeriodUsage", "super-secret-token", {})

        self.assertEqual(payload, {"ok": True})
        argv = " ".join(run.call_args.args[0])
        self.assertNotIn("super-secret-token", argv)
        self.assertIn("super-secret-token", run.call_args.kwargs["input"])

    def test_cursor_rpc_raises_auth_error_on_unauthorized(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="\n401", stderr="")
        with patch.object(plugin.shutil, "which", return_value="/usr/bin/curl"), patch.object(
            plugin.subprocess, "run", return_value=completed
        ):
            with self.assertRaises(plugin.AuthError):
                plugin.cursor_rpc("GetCurrentPeriodUsage", "token", {})


if __name__ == "__main__":
    unittest.main()
