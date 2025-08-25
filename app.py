# app.py
from flask import Flask, request, jsonify
import datetime
from dotenv import load_dotenv
import os
import logging
import hmac
import hashlib
import base64
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import re

# ============================
# Bootstrap / Config
# ============================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
app = Flask(__name__)

# In-memory store for timers (resets on restart)
timers = {}

# ---- Todoist ----
TODOIST_API_BASE_URL = "https://api.todoist.com/rest/v2"
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")
TODOIST_CLIENT_SECRET = os.getenv("TODOIST_CLIENT_SECRET")

if not TODOIST_API_TOKEN:
    raise RuntimeError("TODOIST_API_TOKEN not found in environment variables.")
if not TODOIST_CLIENT_SECRET:
    app.logger.warning("TODOIST_CLIENT_SECRET not set – HMAC validation will fail.")

# Label that triggers Beeminder logging when the task is completed
TRIGGER_LABEL = (os.getenv("TODOIST_BEEMINDER_LABEL") or "beeminder").lower()

# ---- Beeminder ----
BEEMINDER_API_BASE = "https://www.beeminder.com/api/v1"
BEEMINDER_USERNAME = os.getenv("BEEMINDER_USERNAME")
BEEMINDER_AUTH_TOKEN = os.getenv("BEEMINDER_AUTH_TOKEN")
BEEMINDER_GOAL_SLUG = os.getenv("BEEMINDER_GOAL_SLUG") or "dailyprayers"

if not (BEEMINDER_USERNAME and BEEMINDER_AUTH_TOKEN):
    app.logger.warning("BEEMINDER_USERNAME or BEEMINDER_AUTH_TOKEN not set – Beeminder posting will fail.")

# ============================
# Helpers
# ============================
def validate_hmac(payload: bytes, received_hmac: str) -> bool:
    """
    Validate Todoist webhook signature:
    header 'X-Todoist-Hmac-SHA256' == base64(HMAC_SHA256(client_secret, raw_body))
    """
    try:
        mac = hmac.new(TODOIST_CLIENT_SECRET.encode(), payload, hashlib.sha256).digest()
        expected = base64.b64encode(mac).decode()
        return hmac.compare_digest(received_hmac, expected)
    except Exception as e:
        app.logger.error(f"Error validating HMAC: {e}")
        return False

def post_todoist_comment(task_id: str, content: str) -> None:
    """Post a comment to a Todoist task."""
    try:
        url = f"{TODOIST_API_BASE_URL}/comments"
        headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"}
        resp = requests.post(url, json={"task_id": task_id, "content": content}, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            app.logger.info(f"Comment posted on task {task_id}: {content}")
        else:
            app.logger.error(f"Failed to post comment ({resp.status_code}): {resp.text}")
    except Exception as e:
        app.logger.error(f"Error posting comment to Todoist: {e}")

def update_todoist_description(task_id: str, new_description: str) -> bool:
    """Update a Todoist task's description."""
    try:
        url = f"{TODOIST_API_BASE_URL}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json={"description": new_description}, timeout=15)
        if resp.status_code != 200:
            app.logger.error(f"Failed to update description ({resp.status_code}): {resp.text}")
            return False
        app.logger.info(f"Updated task {task_id} description.")
        return True
    except Exception as e:
        app.logger.error(f"Error updating Todoist description: {e}")
        return False

def get_current_description(task_id: str):
    """Fetch the current description of a Todoist task."""
    try:
        url = f"{TODOIST_API_BASE_URL}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            app.logger.error(f"Failed to fetch task ({resp.status_code}): {resp.text}")
            return None
        return resp.json().get("description", "")
    except Exception as e:
        app.logger.error(f"Error fetching Todoist task: {e}")
        return None

def get_task_content(task_id: str) -> str | None:
    """Fetch the task title/content."""
    try:
        url = f"{TODOIST_API_BASE_URL}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            app.logger.error(f"Failed to fetch task content ({resp.status_code}): {resp.text}")
            return None
        return (resp.json().get("content") or "").strip()
    except Exception as e:
        app.logger.error(f"Error fetching Todoist task content: {e}")
        return None

def iso_to_unix(ts: str):
    """Convert ISO8601 string to unix seconds (int)."""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception as e:
        app.logger.warning(f"Could not parse timestamp '{ts}': {e}")
        return None

def post_beeminder_datapoint(value: float, comment: str, timestamp: int, requestid: str) -> bool:
    """Create a datapoint on Beeminder for the configured goal."""
    if not (BEEMINDER_USERNAME and BEEMINDER_AUTH_TOKEN):
        app.logger.error("Beeminder credentials missing.")
        return False
    try:
        url = f"{BEEMINDER_API_BASE}/users/{BEEMINDER_USERNAME}/goals/{BEEMINDER_GOAL_SLUG}/datapoints.json"
        data = {
            "auth_token": BEEMINDER_AUTH_TOKEN,
            "value": value,
            "comment": comment,
        }
        if timestamp is not None:
            data["timestamp"] = timestamp
        if requestid:
            data["requestid"] = requestid  # idempotency (avoid duplicates on retries)
        resp = requests.post(url, data=data, timeout=15)
        if resp.status_code in (200, 201):
            app.logger.info(f"Beeminder datapoint OK: {resp.text}")
            return True
        app.logger.error(f"Beeminder datapoint FAILED ({resp.status_code}): {resp.text}")
        return False
    except Exception as e:
        app.logger.error(f"Error posting to Beeminder: {e}")
        return False

# ============================
# Webhook endpoint
# ============================
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        received_hmac = request.headers.get("X-Todoist-Hmac-SHA256", "")
        delivery_id = request.headers.get("X-Todoist-Delivery-ID")  # unique per event

        # Validate HMAC
        if not received_hmac or not validate_hmac(request.data, received_hmac):
            app.logger.error("Invalid or missing HMAC.")
            return "", 401

        data = request.get_json(silent=True) or {}
        event_name = data.get("event_name")
        event_data = data.get("event_data") or {}
        app.logger.info(f"Event: {event_name}")

        # ===== Todoist -> Beeminder on task completion (label-triggered) =====
        if event_name == "item:completed":
            task = event_data
            task_id = task.get("id")
            task_content = (task.get("content") or "").strip()
            # In Sync v9, labels are names (strings)
            labels = [str(l).lower() for l in (task.get("labels") or [])]
            completed_at = task.get("completed_at") or data.get("triggered_at")
            ts = iso_to_unix(completed_at)

            app.logger.info(f"Completed task '{task_content}' ({task_id}); labels={labels}")

            if TRIGGER_LABEL in labels:
                bm_comment = f"Todoist: {task_content}"
                reqid = delivery_id or f"{task_id}:{completed_at or ''}"
                ok = post_beeminder_datapoint(value=1, comment=bm_comment, timestamp=ts, requestid=reqid)
                if task_id:
                    post_todoist_comment(task_id, "Logged to Beeminder ✅" if ok else "Beeminder logging failed ❌")
                return "", 200
            else:
                app.logger.info("Completed task lacks trigger label; ignoring.")
                return "", 200

        # ===== Comment triggers (note:added) =====
        if event_name == "note:added":
            task_id = event_data.get("item", {}).get("id")
            user_id = event_data.get("item", {}).get("user_id")
            note_id = event_data.get("id")
            comment_text = (event_data.get("content") or "").lower()
            note_time = event_data.get("posted_at") or event_data.get("posted") or data.get("triggered_at")
            ts = iso_to_unix(note_time)

            if not task_id or not user_id:
                app.logger.error("Invalid payload: Missing task_id or user_id.")
                return "", 400

            # --- NEW: "beeminder" comment => +1 to dailyprayers ---
            if re.search(r"\bbeeminder\b", comment_text):
                # Try to include the task title in the datapoint comment
                title = get_task_content(task_id) or "(untitled task)"
                bm_comment = f"Todoist (comment): {title}"
                reqid = delivery_id or (f"note:{note_id}" if note_id else None)
                ok = post_beeminder_datapoint(value=1, comment=bm_comment, timestamp=ts, requestid=reqid)
                post_todoist_comment(task_id, "Logged to Beeminder via comment ✅" if ok else "Beeminder logging failed ❌")
                return "", 200

            # --- Existing timer controls ---
            timer_key = f"{user_id}:{task_id}"
            if "start timer" in comment_text:
                if timer_key in timers:
                    return "", 200
                timers[timer_key] = {"start_time": datetime.datetime.now()}

                current_desc = get_current_description(task_id)
                if current_desc is not None:
                    pattern = r"\(Timer Running: \d+ minutes\)"
                    updated_desc = re.sub(pattern, "", current_desc).strip()
                    snippet = "(Timer Running: 0 minutes)"
                    updated_desc = f"{updated_desc} {snippet}".strip() if updated_desc else snippet
                    update_todoist_description(task_id, updated_desc)
                return "", 200

            if "stop timer" in comment_text:
                if timer_key not in timers:
                    post_todoist_comment(task_id, "No timer found to stop.")
                    return "", 200

                start_time = timers[timer_key]["start_time"]
                elapsed = datetime.datetime.now() - start_time
                del timers[timer_key]

                elapsed_seconds = int(elapsed.total_seconds())
                hours, rem = divmod(elapsed_seconds, 3600)
                minutes, seconds = divmod(rem, 60)
                elapsed_str = f"{hours}h {minutes}m {seconds}s"

                # Optional comment; remove if you don't want it
                post_todoist_comment(task_id, f"Elapsed time: {elapsed_str}")

                current_desc = get_current_description(task_id)
                if current_desc is not None:
                    total_time_pattern = r"\(Total Time: (\d+)h (\d+)m (\d+)s\)"
                    match = re.search(total_time_pattern, current_desc)
                    if match:
                        existing_h = int(match.group(1))
                        existing_m = int(match.group(2))
                        existing_s = int(match.group(3))
                        total = existing_h * 3600 + existing_m * 60 + existing_s + elapsed_seconds
                        nh, rem = divmod(total, 3600)
                        nm, ns = divmod(rem, 60)
                        new_total_str = f"{nh}h {nm}m {ns}s"
                    else:
                        new_total_str = elapsed_str

                    strip_pattern = r"\(Total Time: .*?\)|\(Timer Running: .*?\)"
                    updated_desc = re.sub(strip_pattern, "", current_desc).strip()
                    snippet = f"(Total Time: {new_total_str})"
                    updated_desc = f"{updated_desc} {snippet}".strip() if updated_desc else snippet
                    update_todoist_description(task_id, updated_desc)

                return "", 200

            return "", 200

        # Unhandled events
        app.logger.info(f"Unhandled event: {event_name}")
        return "", 200

    except Exception as e:
        app.logger.error(f"Webhook error: {e}")
        return "", 500

# ============================
# Background job: update running timer snippets every minute
# ============================
def update_descriptions():
    now = datetime.datetime.now()
    for timer_key, data in list(timers.items()):
        try:
            _, task_id = timer_key.split(":")
        except ValueError:
            app.logger.error(f"Bad timer key '{timer_key}'")
            continue

        start_time = data.get("start_time")
        if not start_time:
            continue

        elapsed_minutes = int((now - start_time).total_seconds() // 60)
        current_description = get_current_description(task_id)
        if current_description is None:
            continue

        pattern = r"\(Timer Running: \d+ minutes\)"
        updated_description = re.sub(pattern, "", current_description).strip()
        snippet = f"(Timer Running: {elapsed_minutes} minutes)"
        updated_description = f"{updated_description} {snippet}".strip() if updated_description else snippet
        update_todoist_description(task_id, updated_description)

def start_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(func=update_descriptions, trigger="interval", minutes=1)
    scheduler.start()

# ============================
# Entrypoint
# ============================
if __name__ == '__main__':
    start_scheduler()
    app.run(port=5001, host='0.0.0.0')
