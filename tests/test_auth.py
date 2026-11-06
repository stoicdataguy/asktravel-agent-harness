import pytest
from fastapi.testclient import TestClient

from travel_agent.api import auth
from travel_agent.api.app import app
from travel_agent.config import get_settings

KEY = "s3cret-key"


@pytest.fixture
def client():
    return TestClient(app)  # no `with`: skips the lifespan, so no Postgres


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", KEY)
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": "Bearer wrong"}, {"authorization": KEY}, {"authorization": "Basic x"}],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/models"),
        ("post", "/v1/chat/completions"),
        ("get", "/api/itineraries"),
        ("get", "/api/memory"),
        ("delete", "/api/memory"),
    ],
)
def test_requests_without_the_key_get_401(client, with_key, method, path, headers):
    response = client.request(method, path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_key_lets_requests_through(client, with_key):
    response = client.get("/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "travel-agent"


def test_health_stays_open(client, with_key):
    assert client.get("/health").status_code == 200


def test_no_key_configured_means_no_check(client):
    assert client.get("/v1/models").status_code == 200


def test_api_key_headers(with_key):
    assert auth.api_key_headers() == {"authorization": f"Bearer {KEY}"}


def test_api_key_headers_without_a_key():
    assert auth.api_key_headers() == {}


def test_missing_key_is_logged_at_startup(caplog):
    auth.check_auth_config()
    assert "AGENT_API_KEY is not set" in caplog.text
