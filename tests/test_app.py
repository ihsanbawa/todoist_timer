import datetime
import os
import pytest
import tempfile
from unittest.mock import patch, MagicMock

# Ensure required environment variables for importing the app
os.environ.setdefault("TODOIST_API_TOKEN", "test_token")
os.environ.setdefault("TODOIST_CLIENT_SECRET", "secret")

from app import app, timers, init_db, get_task_link, add_task_link

@pytest.fixture
def client():
    """Fixture to provide a test client."""
    with app.test_client() as client:
        yield client

def mock_validate_hmac(payload, received_hmac):
    """Mock HMAC validation to always return True for tests."""
    return True


def setup_in_memory_db():
    """Utility to configure an isolated SQLite DB for tests."""
    fd, path = tempfile.mkstemp()
    os.close(fd)
    app.config["DB_PATH"] = path
    init_db()

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
    timers["67890:12345"] = {
        "start_time": datetime.datetime.now() - datetime.timedelta(minutes=5)
    }

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
    assert b"Missing event_name" in response.data or b"Malformed JSON" in response.data

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


@patch("app.validate_hmac", side_effect=mock_validate_hmac)
@patch("app.post_todoist_comment")
def test_add_to_beeminder(mock_comment, mock_hmac, client):
    """Link a task to a Beeminder goal via comment."""
    setup_in_memory_db()
    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "add to beeminder salah",
            "item": {"id": "123", "user_id": "u1"},
        },
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert get_task_link("123") == "salah"


@patch("app.validate_hmac", side_effect=mock_validate_hmac)
@patch("app.post_todoist_comment")
def test_remove_from_beeminder(mock_comment, mock_hmac, client):
    """Unlink a task from Beeminder."""
    setup_in_memory_db()
    add_task_link("123", "salah")
    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "remove from beeminder",
            "item": {"id": "123", "user_id": "u1"},
        },
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert get_task_link("123") is None


@patch("app.validate_hmac", side_effect=mock_validate_hmac)
@patch("app.requests.post")
def test_item_completed_sends_beeminder(mock_post, mock_hmac, client):
    """Completion of a linked task sends a Beeminder datapoint."""
    setup_in_memory_db()
    add_task_link("123", "salah")
    os.environ["BEEMINDER_AUTH_TOKEN"] = "token"
    mock_post.return_value.status_code = 200
    payload = {
        "event_name": "item:completed",
        "event_id": "abc123",
        "event_data": {"id": "123"},
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert mock_post.called
    url = mock_post.call_args[0][0]
    assert "salah" in url
    assert mock_post.call_args[1]["data"]["requestid"] == "abc123"

#
# NEW TEST: Ensures that elapsed times above an hour are correctly merged.
#
@patch("app.validate_hmac", side_effect=mock_validate_hmac)
@patch("app.get_current_description")
@patch("app.update_todoist_description")
def test_merge_elapsed_time(mock_update_desc, mock_get_desc, mock_hmac, client):
    """
    Test that stopping the timer twice merges times correctly when
    there is already a (Total Time: Xh Xm Xs) in the description.
    """
    # 1) Simulate existing total time of 0h 49m 41s in the description
    existing_desc = "Some other info (Total Time: 0h 49m 41s)"
    mock_get_desc.return_value = existing_desc

    # 2) Simulate a running timer for ~51m 28s
    timers["67890:12345"] = {
        "start_time": datetime.datetime.now() - datetime.timedelta(minutes=51, seconds=28)
    }

    # 3) Stop Timer event
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

    # 4) Verify the updated description merges times to (Total Time: 1h 41m 9s)
    # Explanation:
    #    0h 49m 41s --> 49*60 + 41 = 2981 seconds
    #    0h 51m 28s --> 51*60 + 28 = 3088 seconds
    #    total 6069 seconds = 1h 41m 9s
    updated_desc_arg = mock_update_desc.call_args[0][1]  # The `new_description` argument
    assert "(Total Time: 1h 41m 9s)" in updated_desc_arg
    assert "(Total Time: 0h 49m 41s)" not in updated_desc_arg
