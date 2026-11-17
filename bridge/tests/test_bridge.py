import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from windsor_bridge.app import State, build_glance, create_app, vehicle_interval_s
from windsor_bridge.calendar_feed import parse_events, time_label
from windsor_bridge.config import Settings
from windsor_bridge.vehicle import EVState, MockProvider, _locked, _psi, _truthy

KEY = "test-key-123"


def settings(**kw):
    base = dict(badge_api_key=KEY, weather_enabled=False, vehicle_provider="mock", mock_scenario="parked")
    base.update(kw)
    return Settings(**base)


def full_state():
    st = State()
    st.ev = EVState(soc=78, range_km=342, locked=True, tire_psi=[33, 33, 32, 33], cabin_temp_c=27)
    st.weather = {"location": "Pune", "temp_c": 24, "condition": "Partly cloudy", "aqi": 182,
                  "aqi_cat": "Unhealthy", "humidity": 48, "hi": 31, "lo": 17}
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime.now(tz)
    st.events = [
        {"title": "Architecture Sync with a very long title that overflows", "start": now + timedelta(hours=i + 1), "all_day": False}
        for i in range(3)
    ]
    return st


def test_payload_under_1kb_and_schema():
    g = build_glance(settings(), full_state())
    raw = json.dumps(g, separators=(",", ":")).encode()
    assert len(raw) < 1024, len(raw)
    assert g["stale"] is False
    assert set(g["ev"]) >= {"model", "soc", "range_km", "charging", "plugged_in", "charge_time_min",
                            "locked", "tire_psi", "cabin_temp_c"}
    assert g["next_event"]["title"].endswith("..") and len(g["next_event"]["title"]) <= 28


def test_stale_when_old_or_missing():
    s = settings()
    assert build_glance(s, State())["stale"] is True
    st = full_state()
    st.ev.polled_at = datetime.now(timezone.utc) - timedelta(minutes=200)
    assert build_glance(s, st)["stale"] is True


def test_adaptive_intervals():
    s = settings()

    class Car:
        touches_vehicle = True

    assert vehicle_interval_s(s, Car(), EVState(charging=False)) == 45 * 60
    assert vehicle_interval_s(s, Car(), EVState(charging=True)) == 15 * 60
    assert vehicle_interval_s(s, MockProvider(s), EVState(charging=True)) == 2 * 60


def test_auth_and_endpoint():
    with TestClient(create_app(settings())) as c:
        assert c.get("/api/v1/glance").status_code == 401
        assert c.get("/api/v1/glance", headers={"X-Badge-Key": "nope"}).status_code == 401
        r = c.get("/api/v1/glance", headers={"X-Badge-Key": KEY})
        assert r.status_code == 200 and len(r.content) < 1024
        assert r.json()["ev"]["soc"] == 78
        assert c.get("/healthz").json() == {"ok": True}


def test_requires_key():
    with pytest.raises(RuntimeError):
        create_app(Settings(badge_api_key=""))


def test_ha_parsers():
    assert _locked({"entity_id": "lock.car", "state": "locked"}) is True
    assert _locked({"entity_id": "binary_sensor.car_lock", "state": "on"}) is False
    assert _truthy({"state": "not_charging"}) is False
    assert _truthy({"state": "charging"}) is True
    assert _psi({"state": "2.3", "attributes": {"unit_of_measurement": "bar"}}) == 33
    assert _psi({"state": "230", "attributes": {"unit_of_measurement": "kPa"}}) == 33


ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
DTSTART:20261118T043000Z
DTEND:20261118T053000Z
SUMMARY:Architecture Sync
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART:20261117T100000Z
DTEND:20261117T110000Z
SUMMARY:Already over
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART;VALUE=DATE:20261119
DTEND;VALUE=DATE:20261120
SUMMARY:Holiday
END:VEVENT
END:VCALENDAR
"""


def test_calendar():
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 11, 17, 21, 15, tzinfo=tz)
    evs = parse_events(ICS, tz, now)
    assert [e["title"] for e in evs] == ["Architecture Sync", "Holiday"]
    assert time_label(evs[0], now) == "Tmrw 10:00 AM"
    assert time_label(evs[0], now.replace(day=18, hour=8)) == "10:00 AM"
