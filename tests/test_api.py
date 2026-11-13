from fastapi.testclient import TestClient


def test_health_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"]["conversations"] == 0


def test_webhook_ignores_non_message_events(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api2.db"))

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/webhook/waha",
            json={"event": "message.ack", "payload": {"id": "x"}},
        )
        assert response.status_code == 200
        assert response.json()["skipped"] == "event"
