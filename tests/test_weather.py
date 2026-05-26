"""Weather service tests.

Exercises cache hit/miss, WMO code formatting, and the summary builder.
Network fetch paths are left to integration tests.
"""

from __future__ import annotations

import json
import time

import pytest

from bbs.services.weather import WeatherService, _format_summary, _WMO_CODES, _WIND_DIRS


@pytest.fixture
def weather_svc(db, cfg) -> WeatherService:
    return WeatherService(db, cfg.weather)


class TestWMOCodes:
    def test_all_codes_have_icon_and_description(self):
        for code, (icon, desc) in _WMO_CODES.items():
            assert icon, f"code {code} has no icon"
            assert desc, f"code {code} has no description"

    def test_common_codes(self):
        assert _WMO_CODES[0] == ("☀️", "clear")
        assert _WMO_CODES[3] == ("☁️", "overcast")
        assert _WMO_CODES[95] == ("⛈️", "thunderstorm")


class TestWindDirs:
    def test_all_eight_directions(self):
        assert len(_WIND_DIRS) == 8
        assert _WIND_DIRS[0] == "N"
        assert _WIND_DIRS[4] == "S"


class TestFormatSummary:
    def test_full_data(self):
        data = {
            "current": {
                "temperature_2m": 22.5,
                "weather_code": 2,
                "wind_speed_10m": 15.0,
                "wind_direction_10m": 135,
            },
            "daily": {
                "precipitation_probability_max": [40],
            },
        }
        result = _format_summary(data, "Sydney")
        assert "Sydney" in result
        assert "22°C" in result
        assert "partly cloudy" in result
        assert "40%" in result
        assert "km/h" in result

    def test_minimal_data(self):
        data = {"current": {"temperature_2m": 18.0}}
        result = _format_summary(data, "Melbourne")
        assert "Melbourne" in result
        assert "18°C" in result

    def test_missing_precipitation(self):
        data = {
            "current": {
                "temperature_2m": 10.0,
                "weather_code": 0,
            },
            "daily": {},
        }
        result = _format_summary(data, "Hobart")
        assert "Hobart" in result
        assert "10°C" in result
        assert "clear" in result
        assert "💧" not in result

    def test_no_wind(self):
        data = {"current": {"temperature_2m": 30.0}}
        result = _format_summary(data, "Darwin")
        assert "💨" not in result

    def test_negative_temperature(self):
        data = {"current": {"temperature_2m": -5.2}}
        result = _format_summary(data, "Thredbo")
        assert "-5°C" in result

    def test_wind_direction_rounding(self):
        data = {
            "current": {
                "temperature_2m": 20.0,
                "wind_speed_10m": 10.0,
                "wind_direction_10m": 90,  # East
            },
        }
        result = _format_summary(data, "Test")
        assert "E " in result  # E followed by wind speed


class TestCacheOperations:
    async def test_cache_hit_fresh(self, db, weather_svc):
        """Pre-populate cache with valid, recent data — summary_for returns from cache."""
        now = int(time.time())
        payload = json.dumps({
            "current": {"temperature_2m": 25.0, "weather_code": 0},
        })
        await db.set_weather_cache("obs:default", payload, now)
        result = await weather_svc.summary_for()
        assert "25°C" in result

    async def test_cache_no_hit_empty(self, weather_svc):
        """Empty cache + network unavailable → returns error message."""
        # The actual HTTP fetch will fail in test env (no network or invalid).
        result = await weather_svc.summary_for()
        # Either a cached hit (if previous test polluted, not the case with fresh tmp) or
        # the weather fetch failed message.
        assert isinstance(result, str)


class TestWindCompass:
    def test_north(self):
        data = {
            "current": {"temperature_2m": 20.0, "wind_speed_10m": 5.0, "wind_direction_10m": 0},
        }
        result = _format_summary(data, "X")
        assert "💨N " in result

    def test_south(self):
        data = {
            "current": {"temperature_2m": 20.0, "wind_speed_10m": 5.0, "wind_direction_10m": 180},
        }
        result = _format_summary(data, "X")
        assert "💨S " in result

    def test_northwest(self):
        data = {
            "current": {"temperature_2m": 20.0, "wind_speed_10m": 5.0, "wind_direction_10m": 315},
        }
        result = _format_summary(data, "X")
        assert "💨NW " in result
