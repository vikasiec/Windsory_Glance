"""Open-Meteo weather + air quality (no API key)."""

from __future__ import annotations

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog",
    51: "Drizzle", 53: "Drizzle", 55: "Drizzle", 56: "Drizzle", 57: "Drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Rain", 67: "Rain",
    71: "Snow", 73: "Snow", 75: "Snow", 77: "Snow",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow", 86: "Snow",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}


def aqi_category(aqi: int | None) -> str:
    """US AQI bands, collapsed to short labels that fit the badge."""
    if aqi is None:
        return "--"
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Poor"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "V.Unhealthy"
    return "Hazardous"


async def fetch_weather(client: httpx.AsyncClient, lat: float, lon: float, tz: str, name: str) -> dict:
    common = {"latitude": lat, "longitude": lon, "timezone": tz}
    f = await client.get(FORECAST_URL, params={
        **common,
        "current": "temperature_2m,relative_humidity_2m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1,
    })
    f.raise_for_status()
    fj = f.json()
    aqi = None
    try:
        a = await client.get(AQ_URL, params={**common, "current": "us_aqi,pm2_5"})
        a.raise_for_status()
        aqi = a.json().get("current", {}).get("us_aqi")
    except httpx.HTTPError:
        pass
    cur = fj.get("current", {})
    daily = fj.get("daily", {})
    rnd = lambda v: round(v) if isinstance(v, (int, float)) else None
    return {
        "location": name,
        "temp_c": rnd(cur.get("temperature_2m")),
        "condition": WMO.get(cur.get("weather_code"), "--"),
        "aqi": rnd(aqi),
        "aqi_cat": aqi_category(rnd(aqi)),
        "humidity": rnd(cur.get("relative_humidity_2m")),
        "hi": rnd((daily.get("temperature_2m_max") or [None])[0]),
        "lo": rnd((daily.get("temperature_2m_min") or [None])[0]),
    }
