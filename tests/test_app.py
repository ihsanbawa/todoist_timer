import datetime
import logging
import pytest
from unittest.mock import patch, MagicMock
from app import app, timers, update_descriptions


@pytest.fixture
def client():
    """Fixture to provide a test client."""
    with app.test_client() as client:
        yield client


def mock_validate_hmac(payload, received_hmac):
    """Mock HMAC validation to always return True for tests."""
    return True


@patch("app.post_todoist_comment")
@patch("app.get_current_description", return_value=("", 200))
@patch("app.update_todoist_description", return_value=True)
@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_start_timer(mock_hmac, mock_update_desc, mock_get_desc, mock_comment, client):
    """Test starting a timer."""
    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "Start Timer",
            "item": {"id": "12345", "user_id": "67890"},
            "id": "note_1"
        }
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert "67890:12345" in timers


@patch("app.post_todoist_comment")
@patch("app.get_current_description", return_value=("Some other info (Total Time: 0h 0m 0s)", 200))
@patch("app.update_todoist_description", return_value=True)
@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_stop_timer_with_running_timer(mock_hmac, mock_update_desc, mock_get_desc, mock_comment, client):
    """Test stopping a timer when it is running."""
    timers["67890:12345"] = {
        "start_time": datetime.datetime.now() - datetime.timedelta(minutes=5)
    }

    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "Stop Timer",
            "item": {"id": "12345", "user_id": "67890"},
            "id": "note_2"
        }
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    assert "67890:12345" not in timers
    mock_comment.assert_called()


@patch("app.post_todoist_comment")
@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_stop_timer_without_running_timer(mock_hmac, mock_comment, client):
    """Test stopping a timer when no timer is running."""
    timers.pop("67890:12345", None)
    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "Stop Timer",
            "item": {"id": "12345", "user_id": "67890"},
            "id": "note_3"
        }
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200
    mock_comment.assert_called_once_with("12345", "No timer found to stop.")


@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_unhandled_event_type(mock_hmac, client):
    """Test handling of an unhandled event type."""
    payload = {
        "event_name": "reminder:fired",
        "event_data": {"id": "99999"}
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200


@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_item_added_event(mock_hmac, client):
    """Test that item:added is handled (logged at DEBUG, returns 200)."""
    payload = {
        "event_name": "item:added",
        "event_data": {"id": "11111", "content": "New task"}
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200


@patch("app.validate_hmac", side_effect=mock_validate_hmac)
def test_item_updated_non_completion(mock_hmac, client):
    """Test that item:updated without completion signals is handled (logged at DEBUG)."""
    payload = {
        "event_name": "item:updated",
        "event_data": {"id": "11111", "content": "Updated task"}
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200


@patch("app.post_todoist_comment")
@patch("app.validate_hmac", side_effect=mock_validate_hmac)
@patch("app.get_current_description")
@patch("app.update_todoist_description")
def test_merge_elapsed_time(mock_update_desc, mock_get_desc, mock_hmac, mock_comment, client):
    """
    Test that stopping the timer twice merges times correctly when
    there is already a (Total Time: Xh Xm Xs) in the description.
    """
    existing_desc = "Some other info (Total Time: 0h 49m 41s)"
    mock_get_desc.return_value = (existing_desc, 200)

    timers["67890:12345"] = {
        "start_time": datetime.datetime.now() - datetime.timedelta(minutes=51, seconds=28)
    }

    payload = {
        "event_name": "note:added",
        "event_data": {
            "content": "Stop Timer",
            "item": {"id": "12345", "user_id": "67890"},
            "id": "note_merge"
        }
    }
    headers = {"X-Todoist-Hmac-SHA256": "mock_signature"}
    response = client.post("/webhook", json=payload, headers=headers)
    assert response.status_code == 200

    updated_desc_arg = mock_update_desc.call_args[0][1]
    assert "(Total Time: 1h 41m 9s)" in updated_desc_arg
    assert "(Total Time: 0h 49m 41s)" not in updated_desc_arg


def test_update_descriptions_removes_timer_on_410(caplog):
    """Test that update_descriptions removes a timer when the task returns 410 Gone."""
    timers["user1:gone_task_410"] = {
        "start_time": datetime.datetime.now() - datetime.timedelta(minutes=3)
    }

    with patch("app.get_current_description", return_value=(None, 410)):
        with caplog.at_level(logging.INFO):
            update_descriptions()

    assert "user1:gone_task_410" not in timers
    assert "gone_task_410 returned 410" in caplog.text
    assert "removing from timer tracking" in caplog.text


def test_update_descriptions_removes_timer_on_404(caplog):
    """Test that update_descriptions removes a timer when the task returns 404."""
    timers["user1:gone_task_404"] = {
        "start_time": datetime.datetime.now() - datetime.timedelta(minutes=3)
    }

    with patch("app.get_current_description", return_value=(None, 404)):
        with caplog.at_level(logging.INFO):
            update_descriptions()

    assert "user1:gone_task_404" not in timers
    assert "gone_task_404 returned 404" in caplog.text
