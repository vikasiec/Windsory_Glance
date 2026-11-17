"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("WINDSOR_ENV_FILE", ".env"))
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Logical field -> env var holding the Home Assistant entity_id
HA_ENTITY_KEYS = {
    "soc": "HA_ENTITY_SOC",
    "range": "HA_ENTITY_RANGE",
    "charging": "HA_ENTITY_CHARGING",
    "plugged_in": "HA_ENTITY_PLUGGED",
    "charge_time": "HA_ENTITY_CHARGE_TIME",
    "lock": "HA_ENTITY_LOCK",
    "tyre_fl": "HA_ENTITY_TYRE_FL",
    "tyre_fr": "HA_ENTITY_TYRE_FR",
    "tyre_rl": "HA_ENTITY_TYRE_RL",
    "tyre_rr": "HA_ENTITY_TYRE_RR",
    "cabin_temp": "HA_ENTITY_CABIN_TEMP",
}


@dataclass
class Settings:
    badge_api_key: str = ""
    timezone: str = "Asia/Kolkata"
    vehicle_name: str = "MG Windsor EV"

    # vehicle
    vehicle_provider: str = "mock"  # mock | homeassistant | saic
    poll_parked_min: int = 45
    poll_charging_min: int = 15
    poll_ha_min: int = 2  # reading HA's cached state is cheap; HA itself rate-limits the car
    stale_after_min: int = 105
    mock_scenario: str = "auto"  # auto | parked | charging | unlocked | lowbatt | lowtyre

    # Home Assistant
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = ""
    ha_entities: dict = field(default_factory=dict)

    # SAIC / MG iSMART (experimental)
    mg_user: str = ""
    mg_pass: str = ""
    mg_vin: str = ""
    saic_base_uri: str = ""
    saic_region: str = ""
    saic_tenant_id: str = ""
    saic_phone_country_code: str = "+91"

    # weather
    weather_enabled: bool = True
    location_name: str = "Pune"
    lat: float = 18.5089
    lon: float = 73.9259
    weather_min: int = 30

    # calendar
    calendar_ics_url: str = ""
    calendar_min: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            badge_api_key=_env("BADGE_API_KEY", ""),
            timezone=_env("TZ_NAME", "Asia/Kolkata"),
            vehicle_name=_env("VEHICLE_NAME", "MG Windsor EV"),
            vehicle_provider=_env("VEHICLE_PROVIDER", "mock").lower(),
            poll_parked_min=_int("POLL_PARKED_MIN", 45),
            poll_charging_min=_int("POLL_CHARGING_MIN", 15),
            poll_ha_min=_int("POLL_HA_MIN", 2),
            stale_after_min=_int("STALE_AFTER_MIN", 105),
            mock_scenario=_env("MOCK_SCENARIO", "auto").lower(),
            ha_url=_env("HA_URL", "http://homeassistant.local:8123").rstrip("/"),
            ha_token=_env("HA_TOKEN", ""),
            ha_entities={k: _env(v) for k, v in HA_ENTITY_KEYS.items()},
            mg_user=_env("MG_USER", ""),
            mg_pass=_env("MG_PASS", ""),
            mg_vin=_env("MG_VIN", ""),
            saic_base_uri=_env("SAIC_BASE_URI", ""),
            saic_region=_env("SAIC_REGION", ""),
            saic_tenant_id=_env("SAIC_TENANT_ID", ""),
            saic_phone_country_code=_env("SAIC_PHONE_COUNTRY_CODE", "+91"),
            weather_enabled=_bool("WEATHER_ENABLED", True),
            location_name=_env("LOCATION_NAME", "Pune"),
            lat=_float("PUNE_LAT", 18.5089),
            lon=_float("PUNE_LON", 73.9259),
            weather_min=_int("WEATHER_MIN", 30),
            calendar_ics_url=_env("CALENDAR_ICS_URL", ""),
            calendar_min=_int("CALENDAR_MIN", 10),
        )
