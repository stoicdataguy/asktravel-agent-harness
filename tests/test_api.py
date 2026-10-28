from fastapi.testclient import TestClient

from travel_agent.api.app import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_models_lists_agent():
    data = client.get("/v1/models").json()["data"]
    assert data[0]["id"] == "travel-agent"
