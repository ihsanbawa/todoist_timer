import datetime
import pytest
from unittest.mock import patch
from app import app, timers

@pytest.fixture
def client():
    """Fixture to provide a test client."""
    with app.test_client() as client:
        yield client

def mock_validate_hmac(payload, received_hmac):
    """Mock HMAC validation to always return True for tests."""
    return True

@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_start_timer(mock_hmac, client):
    """Test starting a timer."""
    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "Start Timer",
            "item": {"id": "12345", "user_id": "67890"}
        }
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert b"Timer started" in response.data

@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_stop_timer_with_running_timer(mock_hmac, client):
    """Test stopping a timer when it is running."""
    # Simulate a running timer
    timers["67890:12345"] = {"start_time": datetime.datetime.now() - datetime.timedelta(minutes=5)}

    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "Stop Timer",
            "item": {"id": "12345", "user_id": "67890"}
        }
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert b"Timer stopped" in response.data

@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_stop_timer_without_running_timer(mock_hmac, client):
    """Test stopping a timer when no timer is running."""
    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "Stop Timer",
            "item": {"id": "12345", "user_id": "67890"}
        }
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert b"No timer running for this task." in response.data

@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_invalid_payload(mock_hmac, client):
    """Test handling of invalid payloads."""
    payload = {"invalid_key": "invalid_value"}
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 400

@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_unhandled_event_type(mock_hmac, client):
    """Test handling of unhandled event types."""
    payload = {
        "event_name": "task:completed",
        "event_data": {
            "content": "Start Timer",
            "item": {"id": "12345", "user_id": "67890"}
        }
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert b"Event not handled" in response.data
