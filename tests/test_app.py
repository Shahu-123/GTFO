from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import firesim.app as app_module
from firesim.config import Settings


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = Settings(_env_file=None, FIRESIM_DATA_DIR=tmp_path, OPENAI_API_KEY="")
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    return TestClient(app_module.app)


def test_sample_room_is_served(client: TestClient) -> None:
    body = client.get("/api/rooms/sample").json()
    assert body["room"]["name"] == "Sample bedroom"
    assert body["problems"] == []


def test_unknown_room_is_404(client: TestClient) -> None:
    assert client.get("/api/rooms/../../etc").status_code == 404
    assert client.get("/api/rooms/000000000000").status_code == 404


def test_upload_without_api_key_is_503(client: TestClient) -> None:
    response = client.post("/api/rooms", files={"video": ("clip.mp4", b"x", "video/mp4")})
    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_index_is_served(client: TestClient) -> None:
    assert "GTFO" in client.get("/").text


def test_fire_simulation_endpoint(client: TestClient) -> None:
    response = client.post(
        "/api/rooms/sample/fire", json={"ignition_object_id": "bed", "duration_s": 120}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["t"]) == len(body["layer_height_m"]) == 121
    assert body["events"][0]["text"] == "Fire starts at the queen bed."


def test_fire_simulation_rejects_unknown_object(client: TestClient) -> None:
    response = client.post("/api/rooms/sample/fire", json={"ignition_object_id": "nope"})
    assert response.status_code == 400
    assert "No object" in response.json()["detail"]


def test_escape_endpoint_returns_fire_and_person_timelines(client: TestClient) -> None:
    body = {
        "fire": {"ignition_object_id": "curtains", "duration_s": 120},
        "person": {"start": {"x": 2.85, "y": 3.0}, "start_object_id": "bed", "asleep": True},
    }
    response = client.post("/api/rooms/sample/escape", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["escape"]["summary"]["outcome"] == "escaped"
    assert len(result["fire"]["t"]) == 121


def test_risk_endpoint(client: TestClient) -> None:
    response = client.post("/api/rooms/sample/risk")
    assert response.status_code == 200
    body = response.json()
    assert body["total_cases"] == 6
    for name in ("heat", "danger"):
        assert body[name]["nx"] * body[name]["ny"] == len(body[name]["values"])
