from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from simulator.device_simulator import DeviceSimulator


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path, enable_background_workers=False, escalation_seconds=900, breaker_cooldown_seconds=1
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def platform(client, app):
    return app.state.platform


@pytest.fixture
def auth(client):
    cache: dict[str, dict] = {}

    def headers(username: str) -> dict:
        if username not in cache:
            resp = client.post("/api/v1/auth/token", data={"username": username, "password": "kapconnect"})
            assert resp.status_code == 200, resp.text
            cache[username] = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        return cache[username]

    return headers


@pytest.fixture
def sim() -> DeviceSimulator:
    return DeviceSimulator(seed=11)


def ingest(client, auth, message: str):
    return client.post(
        "/api/v1/ingest/hl7", content=message, headers={**auth("sigma.device"), "Content-Type": "application/hl7-v2"}
    )
