#!/usr/bin/env python3
# <xbar.title>Cursor Usage</xbar.title>
# <xbar.version>1.2.18</xbar.version>
# <xbar.desc>Shows remaining Cursor Models (Grok · Composer) usage.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <swiftbar.runInBash>false</swiftbar.runInBash>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

"""A SwiftBar plugin that renders remaining Cursor Models (Grok · Composer) usage.

The local Cursor access token is read from the IDE's SQLite state database at
runtime and sent only to api2.cursor.sh. The token is never stored, logged, or
rendered. Conversation content is never read.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


DEFAULT_CURSOR_STATE_DB = (
    Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
)
DEFAULT_API_BASE = "https://api2.cursor.sh"
DASHBOARD_URL = "https://cursor.com/dashboard/spending"
ACCESS_TOKEN_KEY = "cursorAuth/accessToken"
RESET_STATE_FILENAME = "cursor-allowance-reset.json"
DAILY_USAGE_STATE_FILENAME = "cursor-daily-usage.json"
THRESHOLD_ALERT_STATE_FILENAME = "cursor-threshold-alerts.json"
USAGE_SPEED_STATE_FILENAME = "cursor-usage-speed.json"
NOTIFICATION_SETTINGS_FILENAME = "cursor-notification-settings.json"
SWIFTBAR_NOTIFICATION_PLUGIN = "cursor-usage.1m.py"
ALERT_THRESHOLDS = (50, 25, 10)
USAGE_SPEED_WINDOW_SECONDS = 60 * 60
USAGE_SPEED_HISTORY_SECONDS = 2 * 60 * 60
USAGE_TRACKING_QUOTA = "auto"
SECONDS_PER_DAY = 24 * 60 * 60
UNAVAILABLE = "—"
METRIC_LABEL_WIDTH = 14
HTTP_TIMEOUT_SECONDS = 8
MILLISECOND_EPOCH_THRESHOLD = 10_000_000_000


class AuthError(Exception):
    """Raised when Cursor rejects the locally stored access token."""


@dataclass(frozen=True)
class PlanInfo:
    """The billed Cursor plan associated with the current session."""

    name: str | None = None
    price: str | None = None
    included_amount_cents: float | None = None


@dataclass(frozen=True)
class PeriodUsage:
    """Included-plan spend for the current Cursor billing cycle."""

    included_spend_cents: float | None = None
    total_spend_cents: float | None = None
    remaining_cents: float | None = None
    limit_cents: float | None = None
    used_percent: float | None = None
    auto_percent_used: float | None = None
    api_percent_used: float | None = None
    billing_cycle_start: int | None = None
    billing_cycle_end: int | None = None
    display_message: str | None = None


@dataclass(frozen=True)
class UsageSnapshot:
    """A freshly fetched usage snapshot. Stale values are never reused."""

    usage: PeriodUsage
    plan: PlanInfo | None
    fetched_at: str


@dataclass(frozen=True)
class DailyUsageState:
    """A private, aggregate checkpoint for today's Cursor Models usage."""

    date: str
    used_today_percent: float
    used_today_cents: float
    previous_used_percent: float
    previous_included_spend_cents: float
    billing_cycle_end: int | None
    quota: str = USAGE_TRACKING_QUOTA
    day_start_percent: float = 0.0
    prior_today_percent: float = 0.0


@dataclass(frozen=True)
class ThresholdAlertState:
    """The alert thresholds already reported for one billing cycle."""

    billing_cycle_end: int
    notified_thresholds: tuple[int, ...]


@dataclass(frozen=True)
class UsageSpeedMeasurement:
    """The normalized included-plan consumption rate from local observations."""

    percent_per_hour: float
    observed_seconds: int


@dataclass(frozen=True)
class NotificationSettings:
    """User-controlled alert choices, stored without any session content."""

    reset_enabled: bool = True
    threshold_enabled: bool = True
    enabled_thresholds: tuple[int, ...] = ALERT_THRESHOLDS


def cursor_state_db() -> Path:
    """Resolve the Cursor state database without a machine-specific hardcode in callers."""

    configured = os.environ.get("CURSOR_STATE_DB")
    return Path(configured).expanduser() if configured else DEFAULT_CURSOR_STATE_DB


def api_base() -> str:
    """Allow an HTTPS origin override for enterprise or proxy deployments."""

    configured = os.environ.get("CURSOR_API_BASE", DEFAULT_API_BASE).strip()
    return configured.rstrip("/") if configured else DEFAULT_API_BASE


def as_int(value: Any) -> int | None:
    """Return a non-negative integer, rejecting booleans and malformed values."""

    if isinstance(value, bool):
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return integer if integer >= 0 else None


def as_float(value: Any) -> float | None:
    """Return a finite non-negative number for spend and percentage fields."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0:  # NaN check without importing math.
        return None
    return number


def as_percent(value: Any) -> float | None:
    """Accept a usage percentage, including values slightly over 100 after overage."""

    number = as_float(value)
    return number if number is not None and number <= 1000 else None


def as_epoch_seconds(value: Any) -> int | None:
    """Normalize Cursor millisecond timestamps to unix seconds."""

    integer = as_int(value)
    if integer is None:
        return None
    return integer // 1000 if integer > MILLISECOND_EPOCH_THRESHOLD else integer


def as_optional_str(value: Any) -> str | None:
    """Keep short display strings and drop empty or non-string values."""

    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def used_percent_from_spend(included_spend_cents: float | None, limit_cents: float | None) -> float | None:
    """Derive included usage from charged cents so the gauge matches the dashboard spend."""

    if included_spend_cents is None or limit_cents is None or limit_cents <= 0:
        return None
    return included_spend_cents / limit_cents * 100


def remaining_percent(used_percent: float | None) -> float | None:
    """Clamp remaining allowance at zero once the included plan is exhausted."""

    if used_percent is None:
        return None
    return max(0.0, 100.0 - used_percent)


def dashboard_used_percent(value: float | None) -> int | None:
    """Match Cursor's dashboard integer, which ceilings fractional usage such as 0.76% to 1%."""

    if value is None:
        return None
    if value <= 0:
        return 0
    integer = int(value)
    if value > integer:
        integer += 1
    return min(100, integer)


def dashboard_remaining_percent(used_percent: float | None) -> int | None:
    """Show remaining allowance using the same integer the dashboard would show as used."""

    used = dashboard_used_percent(used_percent)
    if used is None:
        return None
    return max(0, 100 - used)


def primary_used_percent(usage: PeriodUsage) -> float | None:
    """Alerts and rate tracking follow the Cursor Models (Grok · Composer) quota only."""

    return usage.auto_percent_used


def parse_period_usage(payload: dict[str, Any]) -> PeriodUsage:
    """Extract included-plan spend while ignoring conversation and model-bucket lists."""

    raw_plan_usage = payload.get("planUsage")
    plan_usage = raw_plan_usage if isinstance(raw_plan_usage, dict) else {}
    included_spend_cents = as_float(plan_usage.get("includedSpend"))
    limit_cents = as_float(plan_usage.get("limit"))
    remaining_cents = as_float(plan_usage.get("remaining"))
    if remaining_cents is None and included_spend_cents is not None and limit_cents is not None:
        remaining_cents = max(0.0, limit_cents - included_spend_cents)
    used_percent = used_percent_from_spend(included_spend_cents, limit_cents)
    if used_percent is None and remaining_cents is not None and limit_cents and limit_cents > 0:
        used_percent = max(0.0, (limit_cents - remaining_cents) / limit_cents * 100)
    return PeriodUsage(
        included_spend_cents=included_spend_cents,
        total_spend_cents=as_float(plan_usage.get("totalSpend")),
        remaining_cents=remaining_cents,
        limit_cents=limit_cents,
        used_percent=used_percent,
        auto_percent_used=as_percent(plan_usage.get("autoPercentUsed")),
        api_percent_used=as_percent(plan_usage.get("apiPercentUsed")),
        billing_cycle_start=as_epoch_seconds(payload.get("billingCycleStart")),
        billing_cycle_end=as_epoch_seconds(payload.get("billingCycleEnd")),
        display_message=as_optional_str(payload.get("displayMessage")),
    )


def parse_plan_info(payload: dict[str, Any] | None) -> PlanInfo | None:
    """Keep only the plan label, price, and included-amount fields."""

    if not isinstance(payload, dict):
        return None
    raw_plan = payload.get("planInfo")
    if not isinstance(raw_plan, dict):
        return None
    plan = PlanInfo(
        name=as_optional_str(raw_plan.get("planName")),
        price=as_optional_str(raw_plan.get("price")),
        included_amount_cents=as_float(raw_plan.get("includedAmountCents")),
    )
    if plan.name is None and plan.price is None and plan.included_amount_cents is None:
        return None
    return plan


def snapshot_is_usable(snapshot: UsageSnapshot | None) -> bool:
    """Require Cursor Models remaining percent before rendering a value."""

    if snapshot is None:
        return False
    usage = snapshot.usage
    return usage.auto_percent_used is not None


def read_access_token(db_path: Path) -> str | None:
    """Read the current Cursor access token without copying or persisting it."""

    uris = (
        f"file:{db_path}?mode=ro",
        f"file:{db_path}?mode=ro&immutable=1",
    )
    for uri in uris:
        token = read_access_token_from_uri(uri)
        if token:
            return token
    return None


def read_access_token_from_uri(uri: str) -> str | None:
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            (ACCESS_TOKEN_KEY,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not row or not isinstance(row[0], str):
        return None
    token = row[0].strip()
    return token or None


def curl_config_header(name: str, value: str) -> str:
    """Format a curl config header without putting the secret on the process command line."""

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'header = "{name}: {escaped}"'


def curl_binary() -> str | None:
    """Prefer the system curl so a restricted SwiftBar PATH still works."""

    system_curl = Path("/usr/bin/curl")
    if system_curl.is_file() and os.access(system_curl, os.X_OK):
        return str(system_curl)
    return shutil.which("curl")


def cursor_rpc(method: str, token: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST a Connect RPC JSON body through curl so macOS system certificates are used."""

    curl_path = curl_binary()
    if not curl_path:
        return None
    url = f"{api_base()}/aiserver.v1.DashboardService/{method}"
    config = "\n".join(
        [
            f'url = "{url}"',
            'request = "POST"',
            curl_config_header("Authorization", f"Bearer {token}"),
            curl_config_header("Content-Type", "application/json"),
            curl_config_header("Connect-Protocol-Version", "1"),
            curl_config_header("Accept", "application/json"),
            curl_config_header("User-Agent", "cursor-usage-swiftbar/1.0"),
        ]
    )
    try:
        result = subprocess.run(
            [
                curl_path,
                "--config",
                "-",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "2",
                "--max-time",
                str(HTTP_TIMEOUT_SECONDS),
                "--retry",
                "2",
                "--retry-delay",
                "1",
                "--retry-max-time",
                str(HTTP_TIMEOUT_SECONDS),
                "--data-binary",
                json.dumps(payload),
                "--write-out",
                "\n%{http_code}",
            ],
            input=config,
            capture_output=True,
            check=False,
            text=True,
            timeout=HTTP_TIMEOUT_SECONDS + 6,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stdout = result.stdout or ""
    body, separator, status_code = stdout.rpartition("\n")
    if not separator:
        return None
    if status_code in {"401", "403"}:
        raise AuthError
    if result.returncode != 0 or status_code != "200":
        return None
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def fetch_snapshot(token: str, now: datetime | None = None) -> UsageSnapshot | None:
    """Fetch current-period usage. Plan info is optional and must not hide a good usage payload."""

    usage_payload = cursor_rpc("GetCurrentPeriodUsage", token, {})
    if usage_payload is None:
        return None
    usage = parse_period_usage(usage_payload)
    plan = None
    try:
        plan = parse_plan_info(cursor_rpc("GetPlanInfo", token, {}))
    except AuthError:
        raise
    fetched_at = (now or datetime.now().astimezone()).astimezone().isoformat()
    snapshot = UsageSnapshot(usage=usage, plan=plan, fetched_at=fetched_at)
    return snapshot if snapshot_is_usable(snapshot) else None


def plugin_state_path(filename: str) -> Path | None:
    """Use SwiftBar's private plugin data directory for aggregate-only markers."""

    data_directory = os.environ.get("SWIFTBAR_PLUGIN_DATA_PATH")
    return Path(data_directory) / filename if data_directory else None


def reset_state_path() -> Path | None:
    return plugin_state_path(RESET_STATE_FILENAME)


def daily_usage_state_path() -> Path | None:
    return plugin_state_path(DAILY_USAGE_STATE_FILENAME)


def threshold_alert_state_path() -> Path | None:
    return plugin_state_path(THRESHOLD_ALERT_STATE_FILENAME)


def usage_speed_state_path() -> Path | None:
    return plugin_state_path(USAGE_SPEED_STATE_FILENAME)


def notification_settings_path() -> Path | None:
    return plugin_state_path(NOTIFICATION_SETTINGS_FILENAME)


def write_json_atomically(path: Path, payload: dict[str, Any]) -> bool:
    """Persist plugin state without leaving a partial file behind."""

    temporary_path = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(json.dumps(payload), encoding="utf-8")
        temporary_path.replace(path)
    except OSError:
        return False
    return True


def read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_reset_marker(path: Path) -> int | None:
    payload = read_json_object(path)
    return as_int(payload.get("billing_cycle_end")) if payload else None


def write_reset_marker(path: Path, billing_cycle_end: int) -> bool:
    return write_json_atomically(path, {"billing_cycle_end": billing_cycle_end})


def read_daily_usage_state(path: Path) -> DailyUsageState | None:
    payload = read_json_object(path)
    if payload is None or not isinstance(payload.get("date"), str):
        return None
    used_today_percent = as_float(payload.get("used_today_percent"))
    used_today_cents = as_float(payload.get("used_today_cents"))
    previous_used_percent = as_float(payload.get("previous_used_percent"))
    previous_included_spend_cents = as_float(payload.get("previous_included_spend_cents"))
    if (
        used_today_percent is None
        or used_today_cents is None
        or previous_used_percent is None
        or previous_included_spend_cents is None
        or payload.get("quota") != USAGE_TRACKING_QUOTA
    ):
        return None
    day_start_percent = as_float(payload.get("day_start_percent"))
    if day_start_percent is None:
        day_start_percent = max(0.0, previous_used_percent - used_today_percent)
    prior_today_percent = as_float(payload.get("prior_today_percent"))
    if prior_today_percent is None:
        prior_today_percent = 0.0
    return DailyUsageState(
        date=payload["date"],
        used_today_percent=used_today_percent,
        used_today_cents=used_today_cents,
        previous_used_percent=previous_used_percent,
        previous_included_spend_cents=previous_included_spend_cents,
        billing_cycle_end=as_int(payload.get("billing_cycle_end")),
        quota=USAGE_TRACKING_QUOTA,
        day_start_percent=day_start_percent,
        prior_today_percent=prior_today_percent,
    )


def write_daily_usage_state(path: Path, state: DailyUsageState) -> bool:
    return write_json_atomically(
        path,
        {
            "date": state.date,
            "used_today_percent": state.used_today_percent,
            "used_today_cents": state.used_today_cents,
            "previous_used_percent": state.previous_used_percent,
            "previous_included_spend_cents": state.previous_included_spend_cents,
            "billing_cycle_end": state.billing_cycle_end,
            "quota": state.quota,
            "day_start_percent": state.day_start_percent,
            "prior_today_percent": state.prior_today_percent,
        },
    )


def read_threshold_alert_state(path: Path) -> ThresholdAlertState | None:
    payload = read_json_object(path)
    if payload is None:
        return None
    billing_cycle_end = as_int(payload.get("billing_cycle_end"))
    raw_thresholds = payload.get("notified_thresholds")
    if billing_cycle_end is None or not isinstance(raw_thresholds, list):
        return None
    thresholds = tuple(sorted({value for value in raw_thresholds if value in ALERT_THRESHOLDS}))
    return ThresholdAlertState(billing_cycle_end=billing_cycle_end, notified_thresholds=thresholds)


def write_threshold_alert_state(path: Path, state: ThresholdAlertState) -> bool:
    return write_json_atomically(
        path,
        {
            "billing_cycle_end": state.billing_cycle_end,
            "notified_thresholds": list(state.notified_thresholds),
        },
    )


def read_usage_speed_observations(path: Path) -> list[tuple[int, float, int | None]]:
    payload = read_json_object(path)
    if payload is None or payload.get("quota") != USAGE_TRACKING_QUOTA:
        return []
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, list):
        return []
    observations: list[tuple[int, float, int | None]] = []
    for item in raw_observations:
        if not isinstance(item, dict):
            continue
        observed_at = as_int(item.get("observed_at"))
        used_percent = as_percent(item.get("used_percent"))
        billing_cycle_end = as_int(item.get("billing_cycle_end"))
        if observed_at is not None and used_percent is not None:
            observations.append((observed_at, used_percent, billing_cycle_end))
    return sorted(observations)


def write_usage_speed_observations(
    path: Path, observations: list[tuple[int, float, int | None]]
) -> bool:
    return write_json_atomically(
        path,
        {
            "quota": USAGE_TRACKING_QUOTA,
            "observations": [
                {
                    "observed_at": observed_at,
                    "used_percent": used_percent,
                    "billing_cycle_end": billing_cycle_end,
                }
                for observed_at, used_percent, billing_cycle_end in observations
            ]
        },
    )


def read_notification_settings(path: Path) -> NotificationSettings | None:
    payload = read_json_object(path)
    if payload is None:
        return None
    reset_enabled = payload.get("reset_enabled")
    threshold_enabled = payload.get("threshold_enabled")
    raw_thresholds = payload.get("enabled_thresholds")
    if not isinstance(reset_enabled, bool) or not isinstance(threshold_enabled, bool):
        return None
    if not isinstance(raw_thresholds, list):
        return None
    enabled_thresholds = tuple(threshold for threshold in ALERT_THRESHOLDS if threshold in raw_thresholds)
    return NotificationSettings(
        reset_enabled=reset_enabled,
        threshold_enabled=threshold_enabled,
        enabled_thresholds=enabled_thresholds,
    )


def write_notification_settings(path: Path, settings: NotificationSettings) -> bool:
    return write_json_atomically(
        path,
        {
            "reset_enabled": settings.reset_enabled,
            "threshold_enabled": settings.threshold_enabled,
            "enabled_thresholds": list(settings.enabled_thresholds),
        },
    )


def notification_settings() -> NotificationSettings:
    settings_path = notification_settings_path()
    if settings_path is None:
        return NotificationSettings()
    return read_notification_settings(settings_path) or NotificationSettings()


def toggle_notification_setting(action: str) -> bool:
    settings_path = notification_settings_path()
    if settings_path is None:
        return False
    settings = notification_settings()
    if action == "--toggle-reset-alert":
        next_settings = NotificationSettings(
            reset_enabled=not settings.reset_enabled,
            threshold_enabled=settings.threshold_enabled,
            enabled_thresholds=settings.enabled_thresholds,
        )
    elif action == "--toggle-threshold-alert":
        next_settings = NotificationSettings(
            reset_enabled=settings.reset_enabled,
            threshold_enabled=not settings.threshold_enabled,
            enabled_thresholds=settings.enabled_thresholds,
        )
    elif action.startswith("--toggle-threshold-"):
        threshold = as_int(action[len("--toggle-threshold-") :])
        if threshold not in ALERT_THRESHOLDS:
            return False
        enabled_thresholds = set(settings.enabled_thresholds)
        if threshold in enabled_thresholds:
            enabled_thresholds.remove(threshold)
        else:
            enabled_thresholds.add(threshold)
        next_settings = NotificationSettings(
            reset_enabled=settings.reset_enabled,
            threshold_enabled=settings.threshold_enabled,
            enabled_thresholds=tuple(value for value in ALERT_THRESHOLDS if value in enabled_thresholds),
        )
    else:
        return False
    return write_notification_settings(settings_path, next_settings)


def is_reset_event(previous_billing_cycle_end: int | None, current_billing_cycle_end: int | None) -> bool:
    """A later billing-cycle deadline indicates Cursor has started a new included-usage window."""

    return (
        previous_billing_cycle_end is not None
        and current_billing_cycle_end is not None
        and current_billing_cycle_end > previous_billing_cycle_end
    )


def send_notification(title: str, subtitle: str, body: str) -> None:
    query = urlencode(
        {
            "plugin": SWIFTBAR_NOTIFICATION_PLUGIN,
            "title": title,
            "subtitle": subtitle,
            "body": body,
        }
    )
    try:
        subprocess.run(
            ["/usr/bin/open", "-g", f"swiftbar://notify?{query}"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def notify_if_allowance_reset(usage: PeriodUsage) -> None:
    if usage.billing_cycle_end is None:
        return
    state_path = reset_state_path()
    if state_path is None:
        return
    previous_billing_cycle_end = read_reset_marker(state_path)
    if not write_reset_marker(state_path, usage.billing_cycle_end):
        return
    if is_reset_event(previous_billing_cycle_end, usage.billing_cycle_end) and notification_settings().reset_enabled:
        send_notification(
            "Cursor usage reset",
            "A new billing period started.",
            "Check remaining usage in the menu bar.",
        )


def notify_if_threshold_crossed(usage: PeriodUsage) -> None:
    remaining = remaining_percent(primary_used_percent(usage))
    if remaining is None or usage.billing_cycle_end is None:
        return
    settings = notification_settings()
    if not settings.threshold_enabled or not settings.enabled_thresholds:
        return
    state_path = threshold_alert_state_path()
    if state_path is None:
        return
    previous_state = read_threshold_alert_state(state_path)
    if previous_state is None or previous_state.billing_cycle_end != usage.billing_cycle_end:
        write_threshold_alert_state(
            state_path,
            ThresholdAlertState(billing_cycle_end=usage.billing_cycle_end, notified_thresholds=()),
        )
        return
    crossed_thresholds = tuple(
        threshold
        for threshold in settings.enabled_thresholds
        if remaining <= threshold and threshold not in previous_state.notified_thresholds
    )
    if not crossed_thresholds:
        return
    next_state = ThresholdAlertState(
        billing_cycle_end=usage.billing_cycle_end,
        notified_thresholds=tuple(sorted((*previous_state.notified_thresholds, *crossed_thresholds))),
    )
    if not write_threshold_alert_state(state_path, next_state):
        return
    labels = ", ".join(f"{threshold}%" for threshold in crossed_thresholds)
    send_notification(
        "Cursor remaining alert",
        f"Currently {format_percent(remaining)} remaining",
        f"Remaining crossed the {labels} threshold.",
    )


def billing_cycle_started_on_local_date(usage: PeriodUsage, now: datetime) -> bool:
    """True when the current Cursor Models window began on this local calendar day."""

    if usage.billing_cycle_start is None:
        return False
    started = datetime.fromtimestamp(usage.billing_cycle_start, tz=now.tzinfo)
    return started.date() == now.date()


def make_daily_usage_state(
    *,
    date: str,
    auto_percent_used: float,
    day_start_percent: float,
    prior_today_percent: float,
    billing_cycle_end: int | None,
) -> DailyUsageState:
    """Today's usage is the increase since the first observation saved for this date."""

    return DailyUsageState(
        date=date,
        used_today_percent=prior_today_percent + max(0.0, auto_percent_used - day_start_percent),
        used_today_cents=0,
        previous_used_percent=auto_percent_used,
        previous_included_spend_cents=0,
        billing_cycle_end=billing_cycle_end,
        quota=USAGE_TRACKING_QUOTA,
        day_start_percent=day_start_percent,
        prior_today_percent=prior_today_percent,
    )


def today_usage(usage: PeriodUsage, now: datetime | None = None) -> DailyUsageState | None:
    """Track today's Cursor Models usage from one-minute checkpoints."""

    if usage.auto_percent_used is None:
        return None
    local_now = (now or datetime.now().astimezone()).astimezone()
    today = local_now.date().isoformat()
    state_path = daily_usage_state_path()
    if state_path is None:
        return None
    previous_state = read_daily_usage_state(state_path)
    if (
        previous_state is not None
        and previous_state.date == today
        and previous_state.billing_cycle_end != usage.billing_cycle_end
    ):
        next_state = make_daily_usage_state(
            date=today,
            auto_percent_used=usage.auto_percent_used,
            day_start_percent=0,
            prior_today_percent=previous_state.used_today_percent,
            billing_cycle_end=usage.billing_cycle_end,
        )
    elif billing_cycle_started_on_local_date(usage, local_now):
        next_state = make_daily_usage_state(
            date=today,
            auto_percent_used=usage.auto_percent_used,
            day_start_percent=0,
            prior_today_percent=0,
            billing_cycle_end=usage.billing_cycle_end,
        )
    elif previous_state is None or previous_state.date != today:
        next_state = make_daily_usage_state(
            date=today,
            auto_percent_used=usage.auto_percent_used,
            day_start_percent=usage.auto_percent_used,
            prior_today_percent=0,
            billing_cycle_end=usage.billing_cycle_end,
        )
    else:
        next_state = make_daily_usage_state(
            date=today,
            auto_percent_used=usage.auto_percent_used,
            day_start_percent=previous_state.day_start_percent,
            prior_today_percent=previous_state.prior_today_percent,
            billing_cycle_end=usage.billing_cycle_end,
        )
    if not write_daily_usage_state(state_path, next_state):
        return None
    return next_state


def usage_speed_per_hour(usage: PeriodUsage, now: datetime | None = None) -> UsageSpeedMeasurement | None:
    if usage.auto_percent_used is None:
        return None
    state_path = usage_speed_state_path()
    if state_path is None:
        return None
    local_now = (now or datetime.now().astimezone()).astimezone()
    current_seconds = int(local_now.timestamp())
    earliest_kept = current_seconds - USAGE_SPEED_HISTORY_SECONDS
    observations = [
        observation
        for observation in read_usage_speed_observations(state_path)
        if observation[0] >= earliest_kept and observation[2] == usage.billing_cycle_end
    ]
    current_observation = (current_seconds, usage.auto_percent_used, usage.billing_cycle_end)
    if not observations or observations[-1][:2] != current_observation[:2]:
        observations.append(current_observation)
    if not write_usage_speed_observations(state_path, observations):
        return None
    cutoff = current_seconds - USAGE_SPEED_WINDOW_SECONDS
    baseline_candidates = [observation for observation in observations if observation[0] <= cutoff]
    if not baseline_candidates:
        return None
    baseline = max(baseline_candidates, key=lambda observation: observation[0])
    elapsed_seconds = current_seconds - baseline[0]
    if elapsed_seconds <= 0:
        return None
    consumed_percent = max(0.0, usage.auto_percent_used - baseline[1])
    return UsageSpeedMeasurement(
        percent_per_hour=consumed_percent * USAGE_SPEED_WINDOW_SECONDS / elapsed_seconds,
        observed_seconds=elapsed_seconds,
    )


def now_timestamp(now: datetime | None = None) -> float:
    return (now or datetime.now().astimezone()).astimezone().timestamp()


def elapsed_cycle_days(usage: PeriodUsage, now: datetime | None = None) -> float | None:
    """Days already spent in the current billing cycle, including a fractional day."""

    if usage.billing_cycle_start is None:
        return None
    elapsed = (now_timestamp(now) - usage.billing_cycle_start) / SECONDS_PER_DAY
    if elapsed <= 0:
        return None
    if usage.billing_cycle_end is not None and usage.billing_cycle_end > usage.billing_cycle_start:
        cycle_days = (usage.billing_cycle_end - usage.billing_cycle_start) / SECONDS_PER_DAY
        elapsed = min(elapsed, cycle_days)
    return elapsed


def remaining_cycle_days(usage: PeriodUsage, now: datetime | None = None) -> float | None:
    """Days left in the current billing cycle, including a fractional day."""

    if usage.billing_cycle_end is None:
        return None
    remaining = (usage.billing_cycle_end - now_timestamp(now)) / SECONDS_PER_DAY
    if remaining <= 0:
        return None
    return remaining


def average_daily_pace(usage: PeriodUsage, now: datetime | None = None) -> float | None:
    """Cursor Models used so far divided by elapsed cycle days."""

    elapsed = elapsed_cycle_days(usage, now)
    if elapsed is None or usage.auto_percent_used is None:
        return None
    return usage.auto_percent_used / elapsed


def target_daily_pace(usage: PeriodUsage, now: datetime | None = None) -> float | None:
    """Remaining Cursor Models divided by remaining cycle days."""

    remaining_days = remaining_cycle_days(usage, now)
    remaining = remaining_percent(usage.auto_percent_used)
    if remaining_days is None or remaining is None:
        return None
    return remaining / remaining_days


def daily_pace_color(average: float | None, target: float | None) -> str | None:
    if average is None or target is None:
        return None
    if average > target:
        return "orange"
    return None


def format_usd(cents: float | None) -> str:
    if cents is None:
        return UNAVAILABLE
    return f"${cents / 100:.2f}"


def format_percent(value: float) -> str:
    """Always show two decimal places so small remaining/used changes stay visible."""

    return f"{max(0.0, value):.2f}%"


def format_usage_percent(value: float) -> str:
    return format_percent(value)


def format_remaining_percent(remaining: float | None) -> str | None:
    """Show remaining Cursor Models with two decimal places."""

    if remaining is None:
        return None
    return format_percent(max(0.0, min(100.0, remaining)))


def format_reset(timestamp: int | None) -> str:
    if timestamp is None:
        return UNAVAILABLE
    try:
        local_time = datetime.fromtimestamp(timestamp).astimezone()
        return f"{local_time:%b} {local_time.day}, {local_time:%H:%M}"
    except (OverflowError, OSError, ValueError):
        return UNAVAILABLE


def format_reset_date(timestamp: int | None) -> str:
    if timestamp is None:
        return UNAVAILABLE
    try:
        local_time = datetime.fromtimestamp(timestamp).astimezone()
        return f"{local_time:%b} {local_time.day}"
    except (OverflowError, OSError, ValueError):
        return UNAVAILABLE


def format_cycle_reset(timestamp: int | None) -> str:
    date = format_reset_date(timestamp)
    remaining = format_remaining_time(timestamp)
    if date == UNAVAILABLE and remaining == UNAVAILABLE:
        return UNAVAILABLE
    if date == UNAVAILABLE:
        return remaining
    if remaining == UNAVAILABLE:
        return date
    return f"{date} · {remaining}"


def format_daily_pace_line(average: float | None, target: float | None) -> str:
    if average is None:
        return UNAVAILABLE
    if target is None:
        return f"{format_percent(average)}/day"
    return f"{format_percent(average)} / {format_percent(target)}{pace_status(average, target)}"


def format_remaining_time(timestamp: int | None) -> str:
    if timestamp is None:
        return UNAVAILABLE
    try:
        seconds = max(0, int(timestamp - datetime.now().astimezone().timestamp()))
    except (OverflowError, OSError, ValueError):
        return UNAVAILABLE
    days, remainder = divmod(seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_window(start: int | None, end: int | None) -> str:
    if start is None or end is None or end <= start:
        return "current period"
    minutes = max(1, (end - start) // 60)
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)} days"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} min"


def menu_text_color() -> str:
    return "#F4F6F8" if os.environ.get("OS_APPEARANCE", "").casefold() == "dark" else "#1E293B"


def menu_secondary_color() -> str:
    return "#B8C2CC" if os.environ.get("OS_APPEARANCE", "").casefold() == "dark" else "#52606D"


def remaining_color(remaining: float) -> str | None:
    if remaining >= 50:
        return None
    if remaining >= 20:
        return "orange"
    return "red"


def setting_state_label(enabled: bool) -> str:
    return "On" if enabled else "Off"


def print_section(title: str, href: str | None = None) -> None:
    extra = f" href={href}" if href else ""
    print(f"{title} | color={menu_secondary_color()} size=11{extra}")


def print_metric(label: str, value: str, tone: str | None = None, href: str | None = None) -> None:
    row = f"{label:<{METRIC_LABEL_WIDTH}}{value}"
    color = tone or menu_text_color()
    extra = f" href={href}" if href else ""
    print(f"{row} | font=Menlo size=12 trim=false color={color}{extra}")


def print_submenu_metric(label: str, value: str, tone: str | None = None) -> None:
    row = f"{label:<{METRIC_LABEL_WIDTH}}{value}"
    color = tone or menu_text_color()
    print(f"-- {row} | font=Menlo size=12 trim=false color={color}")


def print_hourly_submenu(usage_speed: UsageSpeedMeasurement | None) -> None:
    if usage_speed is None:
        print_submenu_metric("Hourly", "collecting 1h…", menu_secondary_color())
        return
    print_submenu_metric("Hourly", f"{format_percent(usage_speed.percent_per_hour)}/h")


def print_info(text: str, extra: str = "", tone: str | None = None) -> None:
    color = tone or menu_secondary_color()
    suffix = f" {extra}" if extra else ""
    print(f"{text} | color={color}{suffix}")


def pace_status(average: float | None, target: float | None) -> str:
    if average is None or target is None:
        return ""
    if average > target:
        return "  ·  Over pace"
    return "  ·  On pace"


def menu_action_attributes(action: str) -> str:
    return (
        f"bash={Path(sys.executable).resolve()} param1={Path(__file__).resolve()} "
        f"param2={action} terminal=false refresh=true"
    )


def print_notification_settings() -> None:
    print("---")
    print(f"Alerts | color={menu_text_color()}")
    if notification_settings_path() is None:
        print("-- Can't save settings | color=orange")
        return
    settings = notification_settings()
    print(
        f"-- Notify on reset: {setting_state_label(settings.reset_enabled)} | "
        f"{menu_action_attributes('--toggle-reset-alert')}"
    )
    print(
        f"-- Remaining alerts: {setting_state_label(settings.threshold_enabled)} | "
        f"{menu_action_attributes('--toggle-threshold-alert')}"
    )
    print(f"-- Thresholds | color={menu_secondary_color()}")
    for threshold in ALERT_THRESHOLDS:
        enabled = threshold in settings.enabled_thresholds
        print(
            f"---- {threshold}% remaining: {setting_state_label(enabled)} | "
            f"{menu_action_attributes(f'--toggle-threshold-{threshold}')}"
        )


def parse_local_timestamp(timestamp: Any) -> datetime | None:
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def format_updated(timestamp: str | None) -> str:
    observed_at = parse_local_timestamp(timestamp)
    if observed_at is None:
        return UNAVAILABLE
    return f"{observed_at:%H:%M}"


def data_status(timestamp: str | None, now: datetime | None = None) -> tuple[str, str]:
    observed_at = parse_local_timestamp(timestamp)
    if observed_at is None:
        return "Unknown", "orange"
    local_now = (now or datetime.now().astimezone()).astimezone()
    age_seconds = max(0, int((local_now - observed_at).total_seconds()))
    if age_seconds <= 2 * 60:
        return "OK · just now", "green"
    age_minutes = max(1, age_seconds // 60)
    if age_seconds <= 10 * 60:
        return f"Delayed · {age_minutes}m ago", "orange"
    return f"Stale · {age_minutes}m ago", "red"


def menu_bar_title(usage: PeriodUsage) -> str:
    formatted = format_remaining_percent(remaining_percent(usage.auto_percent_used))
    if formatted is not None:
        return f"Cursor {formatted}"
    return "—"


def print_error_menu(message: str) -> None:
    print("Cursor ERROR | color=red")
    print("---")
    print_info(message, tone="red")
    print_info("Status: ERROR", tone="red")
    print_info("Previous values are not shown")
    print("Refresh | refresh=true")
    print_notification_settings()


def print_menu(snapshot: UsageSnapshot | None, error_message: str | None = None) -> None:
    if snapshot is None:
        print_error_menu(error_message or "Couldn't fetch current usage")
        return

    usage = snapshot.usage
    notify_if_allowance_reset(usage)
    notify_if_threshold_crossed(usage)
    cursor_remaining = remaining_percent(usage.auto_percent_used)
    formatted_remaining = format_remaining_percent(cursor_remaining)
    other_remaining = remaining_percent(usage.api_percent_used)
    formatted_other = format_remaining_percent(other_remaining)
    print(menu_bar_title(usage))
    print("---")
    print_section("Remaining", href=DASHBOARD_URL)
    if formatted_remaining is None or cursor_remaining is None:
        print_metric("Cursor Models", UNAVAILABLE, href=DASHBOARD_URL)
    else:
        print_metric(
            "Cursor Models",
            formatted_remaining,
            remaining_color(cursor_remaining),
            href=DASHBOARD_URL,
        )
    if formatted_other is None or other_remaining is None:
        print_metric("Other Models", UNAVAILABLE)
    else:
        print_metric("Other Models", formatted_other, remaining_color(other_remaining))
    print("---")
    print_section("Pace")
    daily = today_usage(usage)
    if daily is None:
        print_metric("Today", UNAVAILABLE)
    else:
        print_metric("Today", format_usage_percent(daily.used_today_percent))
    average_pace = average_daily_pace(usage)
    target_pace = target_daily_pace(usage)
    print_metric(
        "Average",
        format_daily_pace_line(average_pace, target_pace),
        daily_pace_color(average_pace, target_pace) if average_pace is not None else None,
    )
    print_hourly_submenu(usage_speed_per_hour(usage))
    print("---")
    print_section("Cycle")
    print_metric("Resets", format_cycle_reset(usage.billing_cycle_end))
    if snapshot.plan and snapshot.plan.name:
        plan_label = snapshot.plan.name
        if snapshot.plan.price:
            plan_label += f" · {snapshot.plan.price}"
        print_submenu_metric("Plan", plan_label)
    status_text, status_color = data_status(snapshot.fetched_at)
    if not status_text.startswith("OK"):
        print("---")
        print_metric("Status", f"{status_text} · {format_updated(snapshot.fetched_at)}", status_color)
    print_notification_settings()


def main() -> int:
    try:
        token = read_access_token(cursor_state_db())
        if token is None:
            print_error_menu("Couldn't find a local Cursor login")
            return 1
        print_menu(fetch_snapshot(token))
    except AuthError:
        print_error_menu("Cursor session expired. Open Cursor and sign in again.")
        return 1
    except Exception as error:  # Defensive: SwiftBar should always receive valid output.
        print_error_menu(f"Couldn't read usage: {type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1].startswith("--toggle-"):
        sys.exit(0 if toggle_notification_setting(sys.argv[1]) else 1)
    sys.exit(main())
