"""Configuration parsing and validation."""

import pytest

from bbs.config import Config, DeviceConfig, _validate_device


def test_device_defaults_serial():
    cfg = Config.from_dict({})
    assert cfg.device.connection == "serial"
    assert cfg.device.tcp_port == 5000


def test_device_tcp_config():
    cfg = Config.from_dict({
        "device": {
            "connection": "tcp",
            "tcp_host": "192.168.1.50",
            "tcp_port": 5001,
            "expected_pubkey": "ab" * 32,
        },
    })
    assert cfg.device.connection == "tcp"
    assert cfg.device.tcp_host == "192.168.1.50"
    assert cfg.device.tcp_port == 5001


def test_device_connection_case_insensitive():
    cfg = Config.from_dict({"device": {"connection": "TCP", "tcp_host": "repeater.local"}})
    assert cfg.device.connection == "tcp"


def test_device_invalid_connection():
    with pytest.raises(ValueError, match="device.connection"):
        Config.from_dict({"device": {"connection": "ble"}})


def test_device_tcp_requires_host():
    with pytest.raises(ValueError, match="tcp_host"):
        Config.from_dict({"device": {"connection": "tcp"}})


def test_device_tcp_port_range():
    with pytest.raises(ValueError, match="tcp_port"):
        Config.from_dict({"device": {"connection": "tcp", "tcp_host": "x", "tcp_port": 0}})


def test_validate_device_normalizes_connection():
    dev = DeviceConfig(connection=" TCP ", tcp_host="host")
    _validate_device(dev)
    assert dev.connection == "tcp"
