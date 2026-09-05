from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from adapters.web.dashboard import DASHBOARD_HTML, setup_dashboard


def test_dashboard_title_and_landmarks() -> None:
    html = DASHBOARD_HTML
    assert "检测台" in html
    assert re.search(r"<h1[^>]*>运行检测台</h1>", html)
    assert 'class="sidebar"' in html
    assert 'class="workspace"' in html
    assert 'id="overview"' in html
    assert 'id="rooms"' in html
    assert 'id="logs"' in html
    assert 'id="failed"' in html


def test_dashboard_connection_status_is_accessible() -> None:
    html = DASHBOARD_HTML
    assert 'id="conn"' in html
    assert 'role="status"' in html
    assert 'aria-atomic="true"' in html


def test_dashboard_sections_use_buttons() -> None:
    html = DASHBOARD_HTML
    assert "function sec(" in html
    assert "<button type=\"button\"" in html or "button type=\"button\"" in html
    assert "aria-expanded" in html
    assert "onclick=\"toggleSec(this)\"" in html


def test_dashboard_inline_script_keeps_js_newline_escapes() -> None:
    start = DASHBOARD_HTML.find("<script>")
    end = DASHBOARD_HTML.rfind("</script>")
    js = DASHBOARD_HTML[start + 8 : end]
    assert start != -1 and end > start
    assert "errs.join('\\n')" in js
    assert "errs.join('\n')" not in js.replace("errs.join('\\n')", "")


def test_dashboard_is_self_contained() -> None:
    html = DASHBOARD_HTML
    lowered = html.lower()
    assert "fonts.googleapis" not in lowered
    assert "cdn.jsdelivr" not in lowered
    assert "unpkg.com" not in lowered
    assert "https://" not in lowered
    assert "<script src=" not in lowered
    assert "<link rel=\"stylesheet\"" not in lowered


def test_dashboard_has_no_game_mutating_controls() -> None:
    html = DASHBOARD_HTML.lower()
    for forbidden in ("/startgame", "/cancel", "/smite", "/flee", "踢人", "重发消息"):
        assert forbidden not in html


def test_dashboard_nav_switches_views_by_hash() -> None:
    html = DASHBOARD_HTML
    assert "function showView(" in html
    assert "e.preventDefault()" in html
    assert "var VIEWS = ['overview','rooms','logs','failed']" in html
    assert 'main > section{display:none;}' in html
    assert "class=\"is-active\"" in html


def test_dashboard_failed_section_has_clear_button() -> None:
    html = DASHBOARD_HTML
    assert 'id="btn-clear-failed"' in html
    assert 'onclick="clearFailed()"' in html
    assert "/api/observe/failed/clear" in html
    assert "function clearFailed(" in html


def test_clear_failed_deliveries_endpoint_marks_store() -> None:
    class _Store:
        def __init__(self) -> None:
            self.calls = 0

        async def clear_failed_deliveries(self) -> int:
            self.calls += 1
            return 4

    async def scenario() -> None:
        store = _Store()
        app = web.Application()
        app["runtime"] = SimpleNamespace(store=store)
        setup_dashboard(app)
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                resp = await client.post("/api/observe/failed/clear")
                body = await resp.json()
                assert resp.status == 200
                assert body["已清除"] == 4
                assert store.calls == 1

    asyncio.run(scenario())


def test_clear_failed_deliveries_endpoint_without_store_method() -> None:
    async def scenario() -> None:
        app = web.Application()
        app["runtime"] = SimpleNamespace(store=SimpleNamespace())
        setup_dashboard(app)
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                resp = await client.post("/api/observe/failed/clear")
                body = await resp.json()
                assert resp.status == 501
                assert body["错误"]

    asyncio.run(scenario())


def test_dashboard_status_uses_vector_icons_not_emoji() -> None:
    html = DASHBOARD_HTML
    assert 'viewBox="0 0 24 24"' in html
    assert "🐺" not in html
    assert "▾" not in html
    assert "●" not in html


def test_dashboard_business_colors_live_in_tokens() -> None:
    style = re.search(r"<style>(.*)</style>", DASHBOARD_HTML, re.S)
    assert style is not None
    root = re.search(r":root\{.*?\n\}", style.group(1), re.S)
    assert root is not None
    rest = style.group(1).replace(root.group(0), "", 1)
    assert not re.search(r"#[0-9A-Fa-f]{3,8}", rest)
