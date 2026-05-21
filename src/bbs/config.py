"""Configuration loader.

Reads `config.yaml`, validates required fields, exposes typed dataclasses.

On SIGHUP the application calls `Config.reload()`; if parsing fails, the
old config is retained and an error is logged (spec 12).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class DeviceConfig:
    serial_path: str = "/dev/ttyUSB0"
    baud: int = 115200
    expected_pubkey: str = ""


@dataclass
class BBSConfig:
    name: str = "MeshCore BBS"
    motd: str = "Welcome! Try HELP."
    admin_pubkeys: list[str] = field(default_factory=list)
    default_location: str = "Sydney"
    max_msg_chars: int = 200


@dataclass
class LimitsConfig:
    inbound_per_hour: int = 20
    inbound_per_minute: int = 5
    post_per_hour: int = 5
    post_per_day: int = 20
    mail_send_per_day: int = 10
    outbound_min_interval_ms: int = 1000
    outbound_per_recipient_min_interval_ms: int = 3000
    outbound_queue_max_depth: int = 100


@dataclass
class NewsFeed:
    slug: str
    url: str


@dataclass
class NewsConfig:
    feeds: list[NewsFeed] = field(default_factory=list)
    max_items_per_feed: int = 50
    refresh_interval_seconds: int = 900


@dataclass
class WeatherConfig:
    latitude: float = -33.87
    longitude: float = 151.21
    location_name: str = "Sydney"
    user_agent: str = "Mozilla/5.0 (compatible; meshcore-bbs/0.1)"
    cache_observation_seconds: int = 600
    cache_forecast_seconds: int = 3600


@dataclass
class ContactsConfig:
    prune_after_days: int = 30


@dataclass
class MailConfig:
    online_threshold_seconds: int = 900
    notify_min_interval_seconds: int = 600
    read_retention_days: int = 90


@dataclass
class HealthConfig:
    http_host: str = "0.0.0.0"
    http_port: int = 8080


@dataclass
class MetricsConfig:
    enabled: bool = False
    http_host: str = "0.0.0.0"
    http_port: int = 9090


@dataclass
class LoggingConfig:
    level: str = "INFO"
    path: str = "/data/bbs.log"


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    bbs: BBSConfig = field(default_factory=BBSConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    contacts: ContactsConfig = field(default_factory=ContactsConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    # Where this config was loaded from, for reload().
    _source_path: Path | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        p = Path(path)
        with p.open() as f:
            raw = yaml.safe_load(f) or {}
        cfg = cls.from_dict(raw)
        cfg._source_path = p
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        device = DeviceConfig(**raw.get("device", {}))
        bbs = BBSConfig(**raw.get("bbs", {}))
        limits = LimitsConfig(**raw.get("limits", {}))
        news_raw = raw.get("news", {})
        feeds = [NewsFeed(**f) for f in news_raw.get("feeds", [])]
        news = NewsConfig(
            feeds=feeds,
            max_items_per_feed=news_raw.get("max_items_per_feed", 50),
            refresh_interval_seconds=news_raw.get("refresh_interval_seconds", 900),
        )
        _weather_raw = raw.get("weather", {})
        weather = WeatherConfig(**{k: v for k, v in _weather_raw.items()
                                   if k in WeatherConfig.__dataclass_fields__})
        contacts = ContactsConfig(**raw.get("contacts", {}))
        mail = MailConfig(**raw.get("mail", {}))
        health = HealthConfig(**raw.get("health", {}))
        metrics = MetricsConfig(**raw.get("metrics", {}))
        logging_cfg = LoggingConfig(**raw.get("logging", {}))
        return cls(
            device=device,
            bbs=bbs,
            limits=limits,
            news=news,
            weather=weather,
            contacts=contacts,
            mail=mail,
            health=health,
            metrics=metrics,
            logging=logging_cfg,
        )

    def reload(self) -> bool:
        """Reload from disk in place. Returns True on success, False on parse error.

        On failure, the existing fields are preserved.
        """
        if self._source_path is None:
            log.warning("reload() called on a config with no source path")
            return False
        try:
            new = Config.load(self._source_path)
        except Exception as e:
            log.error("config reload failed: %s; keeping previous config", e)
            return False
        # Replace fields one by one so existing references still work.
        for name in (
            "device",
            "bbs",
            "limits",
            "news",
            "weather",
            "contacts",
            "mail",
            "health",
            "metrics",
            "logging",
        ):
            setattr(self, name, getattr(new, name))
        log.info("config reloaded from %s", self._source_path)
        return True
