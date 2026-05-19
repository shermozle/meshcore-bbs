"""Weather lookup via the Australian Bureau of Meteorology JSON feeds.

The BoM publishes per-station observation feeds at predictable URLs:
  http://www.bom.gov.au/fwo/IDN60901/IDN60901.94768.json
where `IDN60901` is the issuing-area code and `94768` is the station ID. A
forecast feed lives elsewhere; for v0.1 we surface observations and a short
forecast hint pulled from the same payload's `times` block when available.

Cached payloads live in `weather_cache`. Observation TTL defaults to 10 min,
forecast TTL to 1 hour.

If a user asks for a non-default location, we treat the argument as a station
ID (the BoM equivalent of a postcode). Postcode → station resolution is not
worth implementing for v0.1; if it's the wrong identifier, we surface a
helpful error.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from ..config import WeatherConfig
from ..db import Database

log = logging.getLogger(__name__)


class WeatherService:
    def __init__(self, db: Database, cfg: WeatherConfig) -> None:
        self.db = db
        self.cfg = cfg

    async def summary_for(self, location: str | None = None) -> str:
        station = (location or self.cfg.bom_station).strip()
        key = f"obs:{station}"
        now = int(time.time())

        cached = await self.db.get_weather_cache(key)
        if cached and (now - cached[1]) < self.cfg.cache_observation_seconds:
            try:
                payload = json.loads(cached[0])
                return _format_summary(payload)
            except Exception:
                pass  # fall through to refresh

        url = _build_url(station)
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": self.cfg.user_agent},
                timeout=httpx.Timeout(20.0, connect=5.0),
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"! Unknown station: {station}"
            log.warning("weather fetch failed: %s", e)
            return "! Weather lookup failed."
        except Exception as e:
            log.warning("weather fetch failed: %s", e)
            return "! Weather lookup failed."

        await self.db.set_weather_cache(key, json.dumps(data), now)
        return _format_summary(data)


def _build_url(station: str) -> str:
    # Station identifier convention: IDXNNNNN.NNNNN  (e.g. IDN60901.94768)
    if "." in station:
        area, sid = station.split(".", 1)
        return f"http://www.bom.gov.au/fwo/{area}/{area}.{sid}.json"
    return f"http://www.bom.gov.au/fwo/{station}.json"


def _format_summary(data: dict) -> str:
    """Format a BoM observation JSON into a one- or two-line summary."""
    obs = data.get("observations", {})
    rows = obs.get("data") or []
    if not rows:
        return "! No observation data."
    latest = rows[0]
    name = latest.get("name", "Unknown")
    temp = latest.get("air_temp")
    weather = (latest.get("weather") or "").strip()
    wind_dir = latest.get("wind_dir", "")
    wind_spd = latest.get("wind_spd_kmh")

    parts: list[str] = [name]
    if temp is not None:
        parts.append(f"{temp:.0f}°C")
    if weather and weather != "-":
        parts.append(weather.lower())
    if wind_dir and wind_spd is not None:
        parts.append(f"wind {wind_dir} {wind_spd:.0f}km/h")

    return " ".join(parts)
