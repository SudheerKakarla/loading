"""Optional permissions, approved delivery dates, and dispatch/receiving calendars.

These contracts are software capabilities. A file that is present is validated.
A file that is absent leaves the default plan unchanged. Nothing here authenticates
a business approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Mapping

import pandas as pd

from config import norm_id

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_WEEKDAY_NAME = {number: name for name, number in _WEEKDAYS.items()}


def parse_bool(value: object, *, blank_default: bool | None, field_name: str) -> bool:
    """Native bool, or trimmed ``true`` / ``false`` / ``1`` / ``0``.

    Blank uses ``blank_default``. ``None`` as the default means blank is an error.
    Integers and other numbers are rejected, including ``1`` and ``0``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be true, false, 1, or 0, not a number.")
    if value is None:
        if blank_default is None:
            raise ValueError(f"{field_name} must be true or false.")
        return blank_default
    text = str(value).strip().lower()
    if text in {"", "nan", "none", "<na>"}:
        if blank_default is None:
            raise ValueError(f"{field_name} must be true or false.")
        return blank_default
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise ValueError(f"{field_name} must be true, false, 1, or 0.")


def parse_user_date(value: object, field_name: str) -> date:
    """Strict ``YYYY-MM-DD`` text, or a ``datetime.date`` that is not a datetime."""
    if isinstance(value, datetime):
        raise ValueError(f"{field_name} must be an ISO date, not a datetime.")
    if isinstance(value, date):
        return value
    if value is None:
        raise ValueError(f"{field_name} is missing.")
    text = str(value).strip()
    if not _ISO_DATE.fullmatch(text):
        raise ValueError(f"{field_name} must be YYYY-MM-DD, got {value!r}.")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a real date: {value!r}.") from exc


def coerce_stored_date(value: object, field_name: str) -> date:
    """Read a date already accepted into a table. Pandas may store it as a timestamp."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_user_date(value, field_name)


def clean_po_numbers(value: object) -> str:
    """Sorted, de-duplicated semicolon list. Planning never reads this."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "<na>"}:
        return ""
    found: list[str] = []
    for part in text.replace(",", ";").split(";"):
        token = part.strip()
        if token and token not in found:
            found.append(token)
    return ";".join(sorted(found))


@dataclass(frozen=True)
class WeeklyRule:
    calendar_type: str
    ship_to: str
    weekday: int
    is_open: bool
    start_date: date
    end_date: date


@dataclass(frozen=True)
class PlanningCalendar:
    """Immutable dated and weekly rules. Missing rules mean the date is open."""

    dated_dispatch: Mapping[date, bool] = field(default_factory=dict)
    dated_receiving_global: Mapping[date, bool] = field(default_factory=dict)
    dated_receiving_specific: Mapping[tuple[str, date], bool] = field(default_factory=dict)
    weekly_dispatch: tuple[WeeklyRule, ...] = ()
    weekly_receiving_global: tuple[WeeklyRule, ...] = ()
    weekly_receiving_specific: Mapping[str, tuple[WeeklyRule, ...]] = field(default_factory=dict)

    def dispatch_open(self, day: date) -> bool:
        if day in self.dated_dispatch:
            return bool(self.dated_dispatch[day])
        weekly = _weekly_hit(self.weekly_dispatch, day)
        if weekly is not None:
            return weekly
        return True

    def receiving_open(self, ship_to: str, day: date) -> bool:
        specific = self.dated_receiving_specific.get((ship_to, day))
        if specific is not None:
            return bool(specific)
        if day in self.dated_receiving_global:
            return bool(self.dated_receiving_global[day])
        weekly_specific = _weekly_hit(self.weekly_receiving_specific.get(ship_to, ()), day)
        if weekly_specific is not None:
            return weekly_specific
        weekly_global = _weekly_hit(self.weekly_receiving_global, day)
        if weekly_global is not None:
            return weekly_global
        return True

    def snapshot(self) -> dict:
        """Detached metadata. Weekday numbers are Monday=0 through Sunday=6."""
        dated_open = []
        dated_closed = []
        for day, is_open in sorted(self.dated_dispatch.items()):
            (dated_open if is_open else dated_closed).append(
                {"calendar_type": "dispatch", "ship_to": "", "date": day.isoformat()}
            )
        for day, is_open in sorted(self.dated_receiving_global.items()):
            (dated_open if is_open else dated_closed).append(
                {"calendar_type": "receiving", "ship_to": "", "date": day.isoformat()}
            )
        for (ship_to, day), is_open in sorted(self.dated_receiving_specific.items()):
            (dated_open if is_open else dated_closed).append(
                {"calendar_type": "receiving", "ship_to": ship_to, "date": day.isoformat()}
            )
        weekly = []
        groups = (
            ("dispatch", "", self.weekly_dispatch),
            ("receiving", "", self.weekly_receiving_global),
        )
        for calendar_type, ship_to, rules in groups:
            for rule in rules:
                weekly.append(_weekly_snapshot(calendar_type, ship_to, rule))
        for ship_to, rules in sorted(self.weekly_receiving_specific.items()):
            for rule in rules:
                weekly.append(_weekly_snapshot("receiving", ship_to, rule))
        return {
            "version": 1,
            "precedence_receiving": [
                "dated_specific",
                "dated_global",
                "weekly_specific",
                "weekly_global",
                "open",
            ],
            "precedence_dispatch": ["dated_global", "weekly_global", "open"],
            "dated_open": dated_open,
            "dated_closed": dated_closed,
            "weekly": weekly,
        }


def empty_calendar() -> PlanningCalendar:
    return PlanningCalendar()


def load_calendars(
    dated: pd.DataFrame | None,
    weekly: pd.DataFrame | None,
    customer_ids: set[str],
) -> PlanningCalendar:
    """Validate optional calendar tables. ``None`` means the file was absent."""
    dated_rules = _dated_rules(dated, customer_ids) if dated is not None else None
    weekly_rules = _weekly_rules(weekly, customer_ids) if weekly is not None else None
    return _assemble(dated_rules or [], weekly_rules or [])


def apply_delivery_overrides(rows: list[dict], overrides: pd.DataFrame | None) -> None:
    """Copy approved RDDs onto order rows that already carry a valid original date.

    ``rows`` are mutated in place. The caller's original table is not.
    """
    for row in rows:
        row["original_rdd"] = row["rdd"]
        row["effective_rdd"] = row["rdd"]
        row["override_reason"] = ""
    if overrides is None:
        return
    columns = {str(column).strip().lower(): column for column in overrides.columns}
    missing = [name for name in ("order_id", "approved_rdd", "reason") if name not in columns]
    if missing:
        raise ValueError("delivery_date_overrides is missing columns: " + ", ".join(missing))
    if overrides.empty:
        return
    known = {row["order_id"] for row in rows}
    seen: set[str] = set()
    approved: dict[str, tuple[date, str]] = {}
    for record in overrides.to_dict("records"):
        order_id = norm_id(record[columns["order_id"]])
        if not order_id:
            raise ValueError("delivery_date_overrides has a blank order_id.")
        if order_id in seen:
            raise ValueError(f"Duplicate delivery-date override for {order_id}.")
        seen.add(order_id)
        if order_id not in known:
            raise ValueError(f"Delivery-date override names unknown order {order_id}.")
        approved_rdd = parse_user_date(record[columns["approved_rdd"]], f"{order_id} approved_rdd")
        reason = str(record[columns["reason"]]).strip()
        if not reason or reason.lower() in {"nan", "none", "<na>"}:
            raise ValueError(f"Delivery-date override for {order_id} needs a reason.")
        approved[order_id] = (approved_rdd, reason)
    for row in rows:
        if row["order_id"] not in approved:
            continue
        approved_rdd, reason = approved[row["order_id"]]
        row["rdd"] = approved_rdd
        row["effective_rdd"] = approved_rdd
        row["override_reason"] = reason


def _assemble(dated_rows: list[dict], weekly_rows: list[WeeklyRule]) -> PlanningCalendar:
    dated_dispatch: dict[date, bool] = {}
    dated_receiving_global: dict[date, bool] = {}
    dated_receiving_specific: dict[tuple[str, date], bool] = {}
    for row in dated_rows:
        if row["calendar_type"] == "dispatch":
            dated_dispatch[row["date"]] = row["is_open"]
        elif row["ship_to"] == "":
            dated_receiving_global[row["date"]] = row["is_open"]
        else:
            dated_receiving_specific[(row["ship_to"], row["date"])] = row["is_open"]
    weekly_dispatch = tuple(rule for rule in weekly_rows if rule.calendar_type == "dispatch")
    weekly_receiving_global = tuple(
        rule for rule in weekly_rows if rule.calendar_type == "receiving" and rule.ship_to == ""
    )
    specific: dict[str, list[WeeklyRule]] = {}
    for rule in weekly_rows:
        if rule.calendar_type == "receiving" and rule.ship_to:
            specific.setdefault(rule.ship_to, []).append(rule)
    return PlanningCalendar(
        dated_dispatch=dated_dispatch,
        dated_receiving_global=dated_receiving_global,
        dated_receiving_specific=dated_receiving_specific,
        weekly_dispatch=weekly_dispatch,
        weekly_receiving_global=weekly_receiving_global,
        weekly_receiving_specific={key: tuple(value) for key, value in specific.items()},
    )


def _dated_rules(frame: pd.DataFrame, customer_ids: set[str]) -> list[dict]:
    columns = _require_columns(
        frame,
        ["calendar_type", "date", "is_open", "ship_to"],
        "planning_calendar",
    )
    if frame.empty:
        return []
    seen: set[tuple[str, str, date]] = set()
    rows = []
    for record in frame.to_dict("records"):
        calendar_type = _calendar_type(record[columns["calendar_type"]])
        ship_to = norm_id(record[columns["ship_to"]])
        _check_scope(calendar_type, ship_to, customer_ids, "planning_calendar")
        day = parse_user_date(record[columns["date"]], "planning_calendar date")
        is_open = parse_bool(record[columns["is_open"]], blank_default=None, field_name="planning_calendar is_open")
        key = (calendar_type, ship_to, day)
        if key in seen:
            raise ValueError(
                f"Duplicate planning_calendar row for {calendar_type} {ship_to or '(global)'} {day.isoformat()}."
            )
        seen.add(key)
        rows.append(
            {"calendar_type": calendar_type, "ship_to": ship_to, "date": day, "is_open": is_open}
        )
    return rows


def _weekly_rules(frame: pd.DataFrame, customer_ids: set[str]) -> list[WeeklyRule]:
    columns = _require_columns(
        frame,
        ["calendar_type", "ship_to", "weekday", "is_open", "start_date", "end_date"],
        "weekly_calendar",
    )
    if frame.empty:
        return []
    rules: list[WeeklyRule] = []
    for record in frame.to_dict("records"):
        calendar_type = _calendar_type(record[columns["calendar_type"]])
        ship_to = norm_id(record[columns["ship_to"]])
        _check_scope(calendar_type, ship_to, customer_ids, "weekly_calendar")
        weekday_text = str(record[columns["weekday"]]).strip().lower()
        if weekday_text not in _WEEKDAYS:
            raise ValueError(f"weekly_calendar weekday must be a full name, got {record[columns['weekday']]!r}.")
        is_open = parse_bool(record[columns["is_open"]], blank_default=None, field_name="weekly_calendar is_open")
        start = parse_user_date(record[columns["start_date"]], "weekly_calendar start_date")
        end = parse_user_date(record[columns["end_date"]], "weekly_calendar end_date")
        if start > end:
            raise ValueError("weekly_calendar start_date must be on or before end_date.")
        rules.append(
            WeeklyRule(calendar_type, ship_to, _WEEKDAYS[weekday_text], is_open, start, end)
        )
    for index, left in enumerate(rules):
        for right in rules[index + 1 :]:
            if (left.calendar_type, left.ship_to, left.weekday) != (
                right.calendar_type,
                right.ship_to,
                right.weekday,
            ):
                continue
            if left.start_date <= right.end_date and right.start_date <= left.end_date:
                name = _WEEKDAY_NAME[left.weekday]
                scope = left.ship_to or "(global)"
                raise ValueError(
                    f"Overlapping weekly_calendar ranges for {left.calendar_type} {scope} {name}."
                )
    return rules


def _calendar_type(value: object) -> str:
    text = str(value).strip().lower()
    if text not in {"dispatch", "receiving"}:
        raise ValueError(f"calendar_type must be dispatch or receiving, got {value!r}.")
    return text


def _check_scope(calendar_type: str, ship_to: str, customer_ids: set[str], label: str) -> None:
    if calendar_type == "dispatch":
        if ship_to:
            raise ValueError(f"{label} dispatch rows must leave ship_to blank.")
        return
    if ship_to and ship_to not in customer_ids:
        raise ValueError(f"{label} receiving ship_to {ship_to} is not in the customer table.")


def _require_columns(frame: pd.DataFrame, required: list[str], label: str) -> dict[str, str]:
    columns = {str(column).strip().lower(): column for column in frame.columns}
    missing = [name for name in required if name not in columns]
    if missing:
        raise ValueError(f"{label} is missing columns: " + ", ".join(missing))
    return columns


def _weekly_hit(rules: tuple[WeeklyRule, ...] | list[WeeklyRule], day: date) -> bool | None:
    for rule in rules:
        if rule.weekday != day.weekday():
            continue
        if rule.start_date <= day <= rule.end_date:
            return rule.is_open
    return None


def _weekly_snapshot(calendar_type: str, ship_to: str, rule: WeeklyRule) -> dict:
    return {
        "calendar_type": calendar_type,
        "ship_to": ship_to,
        "weekday": rule.weekday,
        "is_open": rule.is_open,
        "start_date": rule.start_date.isoformat(),
        "end_date": rule.end_date.isoformat(),
    }


def iter_dates(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
