"""Dashboard HTTP API tests."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bbs import __version__
from bbs.dashboard import DashboardDeps, build_history, build_stats, build_status
from bbs.health import make_health_app
from bbs.health_state import HealthState


@pytest.fixture
def log_file(tmp_path: Path) -> str:
    p = tmp_path / "bbs.log"
    p.write_text("2026-01-01T00:00:00 INFO bbs: started\nline two\n", encoding="utf-8")
    return str(p)


@pytest.fixture
async def dashboard_app(
    cfg, db, transport, dispatcher, outbound_worker, log_file,
):
    state = HealthState(transport_connected=True, last_event_at=time.time())
    deps = DashboardDeps(
        cfg=cfg,
        db=db,
        state=state,
        dispatcher=dispatcher,
        outbound=outbound_worker,
        transport=transport,
        metrics=None,
        log_path=log_file,
    )
    app = await make_health_app(db, state, None, deps)
    return app, deps, log_file


@pytest.mark.asyncio
async def test_api_status(dashboard_app):
    app, deps, _ = dashboard_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["version"] == __version__
        assert data["bbs_name"] == deps.cfg.bbs.name
        assert data["transport_connected"] is True


@pytest.mark.asyncio
async def test_api_stats_and_history(dashboard_app, db):
    app, _, _ = dashboard_app
    now = int(time.time())
    await db.audit(None, "startup", "test")
    async with TestClient(TestServer(app)) as client:
        stats = await (await client.get("/api/stats")).json()
        assert "counts" in stats
        assert stats["counts"]["users"] >= 0

        history = await (await client.get("/api/history")).json()
        assert history["days"] == 14
        assert "active_users_by_day" in history


@pytest.mark.asyncio
async def test_api_logs_tail(dashboard_app, log_file):
    app, _, _ = dashboard_app
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get("/api/logs?lines=10")).json()
        assert "started" in "\n".join(data["lines"])
        assert data["path"] == log_file


@pytest.mark.asyncio
async def test_root_redirects_to_dashboard(dashboard_app):
    app, _, _ = dashboard_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == "/dashboard"


@pytest.mark.asyncio
async def test_dashboard_html(dashboard_app):
    app, _, _ = dashboard_app
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/dashboard")
        assert resp.status == 200
        text = await resp.text()
        assert "MeshCore BBS" in text
        assert "/api/status" in text


@pytest.mark.asyncio
async def test_health_includes_extended_status(dashboard_app):
    app, _, _ = dashboard_app
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get("/health")).json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data


@pytest.mark.asyncio
async def test_build_functions_directly(cfg, db, transport, dispatcher, outbound_worker, log_file):
    deps = DashboardDeps(
        cfg=cfg, db=db, state=HealthState(True, time.time()),
        dispatcher=dispatcher, outbound=outbound_worker, transport=transport,
        metrics=None, log_path=log_file,
    )
    status = await build_status(deps)
    assert status["version"] == __version__
    stats = await build_stats(deps)
    assert "counts" in stats
    history = await build_history(deps)
    assert history["days"] == 14
