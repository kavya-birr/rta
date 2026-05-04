"""Advisor calendar — meeting/event store backed by JSON.

Each event has: id, title, with_who (client name/PAN), kind, starts_at,
ends_at, location, notes, status, source.

External-calendar integration story: every event is exported as iCalendar
(.ics) format. Both Google Calendar and Microsoft Teams (via Outlook)
support ICS subscription URLs — point them at this app's `/clients/
calendar/feed.ics` endpoint and meetings appear automatically.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings


EVENT_KINDS = [
    ("review",       "Portfolio Review",   "blue"),
    ("kyc",          "KYC / Onboarding",   "amber"),
    ("call",         "Discovery Call",     "cyan"),
    ("followup",     "Follow-up",          "violet"),
    ("planning",     "Financial Planning", "emerald"),
    ("other",        "Other",              "muted"),
]
KIND_LABELS = {k: lbl for k, lbl, _ in EVENT_KINDS}
KIND_COLORS = {k: c for k, _, c in EVENT_KINDS}


def _store_path() -> Path:
    base = Path(getattr(settings, "DATA_DIR", getattr(settings, "BASE_DIR", ".")))
    return base / "calendar_events.json"


def _load() -> list[dict[str, Any]]:
    p = _store_path()
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(events: list[dict[str, Any]]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, default=str)


def _parse_dt(s: str) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def list_events() -> list[dict[str, Any]]:
    events = _load()
    events.sort(key=lambda e: e.get("starts_at") or "")
    return events


def events_for_date(target: dt.date) -> list[dict[str, Any]]:
    """All events whose start date matches `target`. Sorted by time."""
    events = _load()
    out = []
    for ev in events:
        s = _parse_dt(ev.get("starts_at", ""))
        if s and s.date() == target:
            out.append(ev)
    out.sort(key=lambda e: e.get("starts_at") or "")
    return out


def events_in_range(start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    """All events between [start, end] inclusive."""
    events = _load()
    out = []
    for ev in events:
        s = _parse_dt(ev.get("starts_at", ""))
        if s and start <= s.date() <= end:
            out.append(ev)
    out.sort(key=lambda e: e.get("starts_at") or "")
    return out


def get_event(event_id: str) -> dict[str, Any] | None:
    for ev in _load():
        if ev.get("id") == event_id:
            return ev
    return None


def create_event(
    title: str,
    starts_at: str,
    ends_at: str = "",
    with_who: str = "",
    pan: str = "",
    kind: str = "other",
    location: str = "",
    notes: str = "",
) -> dict[str, Any]:
    s = _parse_dt(starts_at)
    if s is None:
        s = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
    e = _parse_dt(ends_at) if ends_at else (s + dt.timedelta(minutes=30))
    if e < s:
        e = s + dt.timedelta(minutes=30)

    event = {
        "id": uuid.uuid4().hex[:12],
        "title": (title or "Untitled meeting").strip(),
        "with_who": (with_who or "").strip(),
        "pan": (pan or "").strip().upper(),
        "kind": kind if kind in KIND_LABELS else "other",
        "starts_at": s.strftime("%Y-%m-%dT%H:%M"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M"),
        "location": (location or "").strip(),
        "notes": (notes or "").strip(),
        "status": "scheduled",
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    events = _load()
    events.append(event)
    _save(events)
    return event


def update_event(event_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    events = _load()
    for i, ev in enumerate(events):
        if ev.get("id") != event_id:
            continue
        for k in ("title", "with_who", "pan", "kind", "location", "notes", "status"):
            if k in fields:
                ev[k] = (fields[k] or "").strip() if isinstance(fields[k], str) else fields[k]
        if "starts_at" in fields:
            s = _parse_dt(fields["starts_at"])
            if s:
                ev["starts_at"] = s.strftime("%Y-%m-%dT%H:%M")
        if "ends_at" in fields:
            e = _parse_dt(fields["ends_at"])
            if e:
                ev["ends_at"] = e.strftime("%Y-%m-%dT%H:%M")
        events[i] = ev
        _save(events)
        return ev
    return None


def delete_event(event_id: str) -> bool:
    events = _load()
    new = [e for e in events if e.get("id") != event_id]
    if len(new) == len(events):
        return False
    _save(new)
    return True


def stats_for_today() -> dict[str, Any]:
    today = dt.date.today()
    todays = events_for_date(today)
    upcoming_7d = events_in_range(today, today + dt.timedelta(days=7))
    return {
        "today_count": len(todays),
        "upcoming_7d_count": len(upcoming_7d),
        "next_event": todays[0] if todays else None,
        "todays_events": todays,
    }


# ─────────────────────────────────────────────────────────────────────
# iCalendar (.ics) export — Google Calendar / Outlook / Teams subscribable
# ─────────────────────────────────────────────────────────────────────

def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _to_ics_dt(s: str) -> str:
    """Convert ISO local datetime string to iCal floating local time format."""
    d = _parse_dt(s)
    if not d:
        return ""
    return d.strftime("%Y%m%dT%H%M%S")


def render_ics() -> str:
    """Build a complete .ics calendar feed from all stored events."""
    now_stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//openreversefeed//Advisor Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Advisor Calendar",
        "X-WR-TIMEZONE:Asia/Kolkata",
    ]
    for ev in _load():
        uid = f"{ev.get('id', uuid.uuid4().hex[:12])}@openreversefeed"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART;TZID=Asia/Kolkata:{_to_ics_dt(ev.get('starts_at', ''))}",
            f"DTEND;TZID=Asia/Kolkata:{_to_ics_dt(ev.get('ends_at', ''))}",
            f"SUMMARY:{_ics_escape(ev.get('title', ''))}",
        ]
        desc_parts = []
        if ev.get("with_who"):
            desc_parts.append(f"With: {ev['with_who']}")
        if ev.get("kind") in KIND_LABELS:
            desc_parts.append(f"Type: {KIND_LABELS[ev['kind']]}")
        if ev.get("notes"):
            desc_parts.append(ev["notes"])
        if desc_parts:
            lines.append(f"DESCRIPTION:{_ics_escape(chr(10).join(desc_parts))}")
        if ev.get("location"):
            lines.append(f"LOCATION:{_ics_escape(ev['location'])}")
        lines.append(f"STATUS:{'CONFIRMED' if ev.get('status') == 'scheduled' else 'TENTATIVE'}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
