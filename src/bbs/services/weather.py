"""Weather via Open-Meteo (https://open-meteo.com).

Free, no API key, no rate limits for non-commercial use. Returns current
conditions for a configured lat/lon. Cached in `weather_cache`.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from ..config import WeatherConfig
from ..db import Database

log = logging.getLogger(__name__)

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

_WMO_CODES: dict[int, str] = {
    0: "clear",
    1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "icy fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm w/hail", 99: "thunderstorm w/hail",
}

_WIND_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


class WeatherService:
    def __init__(self, db: Database, cfg: WeatherConfig) -> None:
        self.db = db
        self.cfg = cfg

    async def summary_for(self, location: str | None = None) -> str:
        key = "obs:default"
        now = int(time.time())

        cached = await self.db.get_weather_cache(key)
        if cached and (now - cached[1]) < self.cfg.cache_observation_seconds:
            try:
                return _format_summary(json.loads(cached[0]), self.cfg.location_name)
            except Exception:
                pass

        params = {
            "latitude": self.cfg.latitude,
            "longitude": self.cfg.longitude,
            "current": "temperature_2m,weather_code,wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kmh",
            "timezone": "auto",
        }
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": self.cfg.user_agent},
                timeout=httpx.Timeout(20.0, connect=5.0),
            ) as client:
                resp = await client.get(_OPEN_METEO_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            log.warning("weather fetch failed: %s", e)
            return "! Weather lookup failed."

        await self.db.set_weather_cache(key, json.dumps(data), now)
        return _format_summary(data, self.cfg.location_name)


def _format_summary(data: dict, location_name: str) -> str:
    cur = data.get("current", {})
    temp = cur.get("temperature_2m")
    code = cur.get("weather_code")
    wind_spd = cur.get("wind_speed_10m")
    wind_deg = cur.get("wind_direction_10m")

    parts: list[str] = [location_name]
    if temp is not None:
        parts.append(f"{temp:.0f}°C")
    if code is not None:
        parts.append(_WMO_CODES.get(int(code), f"code {code}"))
    if wind_spd is not None and wind_deg is not None:
        compass = _WIND_DIRS[round(wind_deg / 45) % 8]
        parts.append(f"wind {compass} {wind_spd:.0f}km/h")

    return " ".join(parts)
