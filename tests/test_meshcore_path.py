"""Unit tests for mesh path resolution helpers."""

from __future__ import annotations

from bbs.transport.meshcore import _resolve_path_hex


class _FakeMc:
    def __init__(self, names: dict[str, str]) -> None:
        self._names = names

    def get_contact_by_key_prefix(self, prefix: str):
        name = self._names.get(prefix.lower())
        if name is None:
            return None
        return {"adv_name": name, "public_key": prefix + "0" * (64 - len(prefix))}


def test_resolve_path_hex_with_contact_names():
    mc = _FakeMc({"aabbcc": "RepeaterA", "ddeeff": "RepeaterB"})
    path = _resolve_path_hex("aabbccddeeff", 3, mc)
    assert path == ["RepeaterA", "RepeaterB"]


def test_resolve_path_hex_falls_back_to_hash_prefix():
    mc = _FakeMc({})
    path = _resolve_path_hex("aabbcc", 3, mc)
    assert path == ["aabbcc"]
