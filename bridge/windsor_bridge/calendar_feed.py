"""Upcoming events from an iCal (ICS) feed, e.g. Google Calendar's
"Secret address in iCal format". Recurring events are expanded."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx


def _as_dt(v, tz: ZoneInfo) -> tuple[datetime, bool]:
    if isinstance(v, datetime):
        return (v.replace(tzinfo=tz) if v.tzinfo is None else v.astimezone(tz)), False
    if isinstance(v, date):
        return datetime.combine(v, time(0), tz), True
    raise TypeError(v)


def parse_events(ics: bytes, tz: ZoneInfo, now: datetime, days: int = 7, limit: int = 3) -> list[dict]:
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    out = []
    for ev in recurring_ical_events.of(cal).between(now - timedelta(days=1), now + timedelta(days=days)):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start, all_day = _as_dt(ev.get("DTSTART").dt, tz)
        end_prop = ev.get("DTEND")
        end = _as_dt(end_prop.dt, tz)[0] if end_prop else start + (timedelta(days=1) if all_day else timedelta(hours=1))
        if end <= now:
            continue
        out.append({"title": str(ev.get("SUMMARY", "(busy)")).strip(), "start": start, "all_day": all_day})
    # timed events first within the same day, then chronological
    out.sort(key=lambda e: (e["start"].date(), e["all_day"], e["start"]))
    return out[:limit]


def time_label(e: dict, now: datetime) -> str:
    start: datetime = e["start"]
    d = (start.date() - now.date()).days
    if e["all_day"]:
        return "Today" if d <= 0 else "Tmrw" if d == 1 else start.strftime("%a")
    if start <= now:
        return "Now"
    hm = start.strftime("%I:%M %p").lstrip("0")
    if d == 0:
        return hm
    return ("Tmrw " if d == 1 else start.strftime("%a ")) + hm


async def fetch_events(client: httpx.AsyncClient, url: str, tz: ZoneInfo) -> list[dict]:
    r = await client.get(url, follow_redirects=True)
    r.raise_for_status()
    return parse_events(r.content, tz, datetime.now(tz))
