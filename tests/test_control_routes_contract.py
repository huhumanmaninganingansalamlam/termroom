"""V1 control/credential route identity frozen before feature extraction."""
from __future__ import annotations

import json
from pathlib import Path

from termroom.app import create_app
from termroom.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_control_v1_routes_preserve_paths_methods_names_and_response_types(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    app = create_app(Settings.create(
        projects, state_dir=tmp_path / "state",
        login_password="schema-contract-password",
        access_token="schema-contract-token",
    ))
    routes = [
        {
            "path": route.path,
            "methods": sorted(getattr(route, "methods", ())),
            "name": route.name,
            "status_code": getattr(route, "status_code", None),
            "response_class": getattr(
                getattr(route, "response_class", None), "__name__", None,
            ),
        }
        for route in app.routes
        if route.path.startswith(("/api/control/v1", "/api/client-credentials"))
    ]
    routes.sort(key=lambda route: (route["path"], route["methods"], route["name"]))
    assert routes == json.loads(
        (ROOT / "tests/control_routes_v1.json").read_text(encoding="utf-8")
    )
    assert len(routes) == 45
    endpoints = [
        route.endpoint
        for route in app.routes
        if route.path.startswith(("/api/control/v1", "/api/client-credentials"))
    ]
    assert all(endpoint.__module__ == "termroom.control_api" for endpoint in endpoints)
    assert not any(
        '"/api/control/v1' in line
        for line in (ROOT / "termroom/app.py").read_text().splitlines()
        if "@app." in line
    )
