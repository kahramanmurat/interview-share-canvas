from __future__ import annotations

from pathlib import Path

import yaml


def test_documented_http_contract_is_registered(app):
    spec_path = Path(__file__).parents[2] / "openapi.yaml"
    documented = yaml.safe_load(spec_path.read_text())
    registered = app.openapi()["paths"]

    for path, operations in documented["paths"].items():
        assert path in registered
        for method in operations:
            if method == "parameters":
                continue
            assert method in registered[path]


def test_frontend_is_served_by_fastapi(client):
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/interview-platform.dc.html"

    frontend = client.get("/interview-platform.dc.html")
    assert frontend.status_code == 200
    assert "Interview" in frontend.text
