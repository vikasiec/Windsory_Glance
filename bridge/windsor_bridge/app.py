"""FastAPI app: background pollers + the badge-facing /api/v1/glance endpoint.

Run:  uvicorn windsor_bridge.app:app --host 192.168.1.50 --port 8000
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response

from . import __version__
from .calendar_feed import fetch_events, time_label
from .config import Settings
from .vehicle import EVState, VehicleProvider, make_provider
from .weather import fetch_weather

log = logging.getLogger("windsor_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TITLE_MAX = 28


@dataclass
class State:
    ev: EVState | None = None
    ev_error: str | None = None
    ev_last_attempt: datetime | None = None
    ev_next_poll: datetime | None = None
    weather: dict | None = None
    weather_error: str | None = None
    weather_at: datetime | None = None
    events: list = field(default_factory=list)
    calendar_error: str | None = None


def vehicle_interval_s(s: Settings, provider: VehicleProvider, ev: EVState | None) -> int:
    """FR-1.2 adaptive cadence. 45 min parked, 15 min only while charging."""
    if not provider.touches_vehicle:
        return s.poll_ha_min * 60
    if ev is not None and ev.charging:
        return s.poll_charging_min * 60
    return s.poll_parked_min * 60


def _short(t: str, n: int = TITLE_MAX) -> str:
    t = " ".join(t.split())
    t = t.encode("ascii", "ignore").decode() or "(event)"  # badge pixel fonts are ASCII-only
    return t if len(t) <= n else t[: n - 2].rstrip() + ".."


def build_glance(s: Settings, st: State, now: datetime | None = None) -> dict:
    tz = ZoneInfo(s.timezone)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    ev = st.ev
    stale = (
        ev is None
        or st.ev_error is not None
        or (now - ev.polled_at) > timedelta(minutes=s.stale_after_min)
    )
    ev_out = {
        "model": s.vehicle_name,
        "soc": ev.soc if ev else None,
        "range_km": ev.range_km if ev else None,
        "charging": bool(ev and ev.charging),
        "plugged_in": bool(ev and ev.plugged_in),
        "charge_time_min": ev.charge_time_min if ev else 0,
        "locked": ev.locked if ev else None,
        "tire_psi": list(ev.tire_psi) if ev else [None] * 4,
        "cabin_temp_c": ev.cabin_temp_c if ev else None,
        "polled_at": ev.polled_at.astimezone(tz).strftime("%H:%M") if ev else None,
    }
    upcoming = [e for e in st.events if e["start"] + timedelta(minutes=1) > now or e["all_day"]]
    events = [{"title": _short(e["title"]), "time": time_label(e, now)} for e in upcoming[:3]]
    w = st.weather or {"location": s.location_name, "temp_c": None, "condition": "--", "aqi": None}
    return {
        "updated_at": now.strftime("%H:%M"),
        "date": now.strftime("%a %d %b"),
        "sod": now.hour * 3600 + now.minute * 60 + now.second,  # seconds-of-day, lets the badge tick a clock
        "stale": stale,
        "ev": ev_out,
        "weather": w,
        "next_event": events[0] if events else None,
        "events": events,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    if not s.badge_api_key:
        raise RuntimeError("BADGE_API_KEY must be set (e.g. python -c \"import secrets;print(secrets.token_urlsafe(24))\")")
    tz = ZoneInfo(s.timezone)
    st = State()
    provider = make_provider(s)
    tasks: list[asyncio.Task] = []

    async def vehicle_loop():
        failures = 0
        while True:
            st.ev_last_attempt = datetime.now(timezone.utc)
            try:
                st.ev = await provider.fetch()
                st.ev_error = None
                failures = 0
                log.info("vehicle poll ok: soc=%s charging=%s locked=%s", st.ev.soc, st.ev.charging, st.ev.locked)
                delay = vehicle_interval_s(s, provider, st.ev)
            except Exception as e:  # keep serving the cache
                failures += 1
                st.ev_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("vehicle poll failed (%d): %s", failures, st.ev_error)
                # back off 5, 10, 20 … min, never faster than charging cadence for a car-touching provider
                delay = min(vehicle_interval_s(s, provider, st.ev), 300 * 2 ** (failures - 1))
                if provider.touches_vehicle:
                    delay = max(delay, s.poll_charging_min * 60)
            st.ev_next_poll = datetime.now(timezone.utc) + timedelta(seconds=delay)
            await asyncio.sleep(delay)

    async def weather_loop(client: httpx.AsyncClient):
        while True:
            try:
                st.weather = await fetch_weather(client, s.lat, s.lon, s.timezone, s.location_name)
                st.weather_at = datetime.now(timezone.utc)
                st.weather_error = None
            except Exception as e:
                st.weather_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("weather fetch failed: %s", st.weather_error)
            await asyncio.sleep(s.weather_min * 60)

    async def calendar_loop(client: httpx.AsyncClient):
        while True:
            try:
                st.events = await fetch_events(client, s.calendar_ics_url, tz)
                st.calendar_error = None
            except Exception as e:
                st.calendar_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("calendar fetch failed: %s", st.calendar_error)
            await asyncio.sleep(s.calendar_min * 60)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        client = httpx.AsyncClient(timeout=15)
        tasks.append(asyncio.create_task(vehicle_loop()))
        if s.weather_enabled:
            tasks.append(asyncio.create_task(weather_loop(client)))
        if s.calendar_ics_url:
            tasks.append(asyncio.create_task(calendar_loop(client)))
        log.info("bridge %s up: provider=%s", __version__, provider.name)
        yield
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()
        await provider.aclose()

    app = FastAPI(title="Windsor Glance Bridge", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.st, app.state.provider = s, st, provider

    def check(key: str | None):
        if not key or not hmac.compare_digest(key.encode(), s.badge_api_key.encode()):
            raise HTTPException(status_code=401, detail="bad key")

    @app.get("/api/v1/glance")
    async def glance(x_badge_key: str | None = Header(default=None)):
        check(x_badge_key)
        body = json.dumps(build_glance(s, st), separators=(",", ":"), ensure_ascii=False)
        return Response(body, media_type="application/json")

    @app.get("/api/v1/debug")
    async def debug(x_badge_key: str | None = Header(default=None)):
        check(x_badge_key)
        iso = lambda d: d.isoformat() if d else None
        return {
            "provider": provider.name,
            "touches_vehicle": provider.touches_vehicle,
            "ev": st.ev.to_dict() if st.ev else None,
            "ev_error": st.ev_error,
            "ev_last_attempt": iso(st.ev_last_attempt),
            "ev_next_poll": iso(st.ev_next_poll),
            "weather_at": iso(st.weather_at),
            "weather_error": st.weather_error,
            "calendar_error": st.calendar_error,
            "glance_bytes": len(json.dumps(build_glance(s, st), separators=(",", ":"), ensure_ascii=False).encode()),
        }

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app


def __getattr__(name):  # lazy module-level `app` for `uvicorn windsor_bridge.app:app`
    if name == "app":
        a = create_app()
        globals()["app"] = a
        return a
    raise AttributeError(name)
