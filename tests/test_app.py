import pytest
from app import app  # Import the Flask app

@pytest.fixture
def client():
    """Fixture to create a test client for the Flask app."""
    with app.test_client() as client:
        yield client

def test_start_timer(client):
    """Test starting a timer."""
    payload = {
        "event_data": {
            "id": "123",
            "user_id": "456",
            "content": "Start Timer"
        }
    }
    response = client.post('/webhook', json=payload)
    assert response.status_code == 200
    assert response.get_json()["message"] == "Timer started."

def test_stop_timer(client):
    """Test stopping a timer."""
    # Start the timer first
    start_payload = {
        "event_data": {
            "id": "123",
            "user_id": "456",
            "content": "Start Timer"
        }
    }
    client.post('/webhook', json=start_payload)

    # Stop the timer
    stop_payload = {
        "event_data": {
            "id": "123",
            "user_id": "456",
            "content": "Stop Timer"
        }
    }
    response = client.post('/webhook', json=stop_payload)
    assert response.status_code == 200
    assert "Timer stopped" in response.get_json()["message"]

def test_invalid_payload(client):
    """Test invalid payload."""
    invalid_payload = {"event_data": {}}
    response = client.post('/webhook', json=invalid_payload)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid payload"
