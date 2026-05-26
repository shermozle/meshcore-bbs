"""Health endpoint tests.

Exercises the health check logic, problem detection, and the HTTP handler
without starting a real server (no aiohttp AppRunner).
"""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bbs.health import make_health_app, Metrics
from bbs.health_state import HEALTH_HEARTBEAT_THRESHOLD, HealthState


class TestHealthState:
    def test_default_state(self):
        state = HealthState()
        assert state.transport_connected is False
        assert state.last_event_at == 0.0

    def test_heartbeat_threshold(self):
        assert HEALTH_HEARTBEAT_THRESHOLD == 600


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_healthy_when_connected_and_recent(self, db):
        state = HealthState(transport_connected=True, last_event_at=time.time())
        app = await make_health_app(db, state, None, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unhealthy_transport_down(self, db):
        state = HealthState(transport_connected=False, last_event_at=time.time())
        app = await make_health_app(db, state, None, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            data = await resp.json()
            assert data["status"] == "unhealthy"
            assert "transport_disconnected" in data["problems"]

    @pytest.mark.asyncio
    async def test_unhealthy_no_recent_events(self, db):
        stale = time.time() - HEALTH_HEARTBEAT_THRESHOLD - 60
        state = HealthState(transport_connected=True, last_event_at=stale)
        app = await make_health_app(db, state, None, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            data = await resp.json()
            assert "no_recent_events" in data["problems"]

    @pytest.mark.asyncio
    async def test_healthy_stale_but_never_ticked(self, db):
        """If last_event_at is 0 (never ticked), don't flag as unhealthy."""
        state = HealthState(transport_connected=True, last_event_at=0.0)
        app = await make_health_app(db, state, None, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_db_check_passes(self, db):
        state = HealthState(transport_connected=True, last_event_at=time.time())
        app = await make_health_app(db, state, None, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_db_check_fails_after_close(self, db):
        """Closing the DB should make the health check fail."""
        await db.close()
        state = HealthState(transport_connected=True, last_event_at=time.time())
        app = await make_health_app(db, state, None, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            data = await resp.json()
            assert any("db_error" in p for p in data["problems"])


class TestHealthRoot:
    @pytest.mark.asyncio
    async def test_root_no_dashboard(self, db):
        state = HealthState()
        app = await make_health_app(db, state, None, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/")
            assert resp.status == 200
            text = await resp.text()
            assert "meshcore-bbs" in text


class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_metrics_endpoint_when_enabled(self, db):
        state = HealthState()
        metrics = Metrics()
        app = await make_health_app(db, state, metrics, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 200
            text = await resp.text()
            assert "bbs_messages_in_total" in text
            assert "bbs_outbound_queue_depth" in text

    @pytest.mark.asyncio
    async def test_no_metrics_endpoint_when_disabled(self, db):
        state = HealthState()
        app = await make_health_app(db, state, None, dashboard=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/metrics")
            assert resp.status == 404


class TestMetricsRegisteryIsolation:
    def test_separate_instances_dont_clash(self):
        m1 = Metrics()
        m2 = Metrics()
        assert m1.registry is not m2.registry
        assert m1.messages_in is not m2.messages_in
