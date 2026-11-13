"""The bundled dashboard: served, self-consistent, and free of invented data."""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).parent.parent / "app" / "static" / "dashboard"


def test_index_is_served_and_branded(test_client):
    resp = test_client.get("/dashboard/")
    assert resp.status_code == 200 and "NightJar" in resp.text and "ContextForge" not in resp.text


def test_every_script_and_stylesheet_the_page_references_resolves(test_client):
    html = test_client.get("/dashboard/").text
    assets = re.findall(r'(?:src|href)="((?:js|css)/[^"]+)"', html)
    assert assets, "expected the page to reference its scripts and styles"
    for path in assets:
        assert test_client.get(f"/dashboard/{path}").status_code == 200, path


def test_no_removed_features_remain_in_the_page_or_scripts():
    text = "".join(p.read_text() for p in [DASHBOARD / "index.html", *DASHBOARD.glob("js/*.js")])
    assert "/v1/threshold" not in text, "the adaptive-threshold endpoints answer 410"
    assert "page-threshold" not in text and "data-page=\"threshold\"" not in text


def test_no_fabricated_request_data_ships_with_the_dashboard():
    assert not (DASHBOARD / "js" / "data.js").exists()
    text = "".join(p.read_text() for p in DASHBOARD.glob("js/*.js"))
    assert "DEMO_REQUESTS" not in text and "DEMO_SUMMARY" not in text


def test_dashboard_files_are_not_in_docs():
    assert not (DASHBOARD.parent.parent.parent / "docs" / "dashboard").exists()
