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

# ---- MySQL driver ----
import pymysql
from urllib.parse import urlparse

# ============================
# Bootstrap / Logging
# ============================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

app = Flask(__name__)

# In-memory store for timers
timers = {}

# ============================
# Env helpers
# ============================
def require_env(name: str) -> str | None:
    val = os.getenv(name)
    if not val:
        app.logger.warning(f"ENV MISSING: {name} is not set")
    return val

# Cache tokens once at startup (we still validate presence at use-sites)
TODOIST_API_BASE_URL = "https://api.todoist.com/rest/v2"
TODOIST_API_TOKEN = require_env("TODOIST_API_TOKEN")
BEEMINDER_AUTH_TOKEN = require_env("BEEMINDER_AUTH_TOKEN")

if not TODOIST_API_TOKEN:
    raise RuntimeError("TODOIST_API_TOKEN not found in environment variables.")

# ============================
# MySQL utilities
# ============================
def _parse_mysql_from_env():
    """
    Support either a single MYSQL_URL or individual MYSQL* vars
    (Railway provides both). Returns dict usable by PyMySQL.
    """
    url = os.getenv("MYSQL_URL") or os.getenv("JAWSDB_URL")
    if url:
        u = urlparse(url)
        return {
            "host": u.hostname,
            "port": u.port or 3306,
            "user": u.username,
            "password": u.password,
            "database": u.path.lstrip("/"),
        }
    # Fallback to individual vars
    return {
        "host": os.getenv("MYSQLHOST", "localhost"),
        "port": int(os.getenv("MYSQLPORT", "3306")),
        "user": os.getenv("MYSQLUSER"),
        "password": os.getenv("MYSQLPASSWORD"),
        "database": os.getenv("MYSQLDATABASE") or os.getenv("MYSQL_DATABASE"),
    }

DB_CFG = _parse_mysql_from_env()

def get_db_connection():
    """
    Return a new MySQL connection. We open/close per call to be
    thread-safe with APScheduler and Flask's default threaded server.
    """
    conn = pymysql.connect(
        host=DB_CFG["host"],
        port=DB_CFG["port"],
        user=DB_CFG["user"],
        password=DB_CFG["password"],
        database=DB_CFG["database"],
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
    )
    return conn

def init_db():
    """Initialize the MySQL table for task links."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS task_link (
                    task_id VARCHAR(64) PRIMARY KEY,
                    goal_slug VARCHAR(255)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
    finally:
        conn.close()

# Ensure the database exists on import
init_db()

# ============================
# DB operations
# ============================
def add_task_link(task_id: str, goal_slug: str) -> None:
    """Upsert mapping of task_id -> goal_slug (MySQL REPLACE keeps your logic)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "REPLACE INTO task_link (task_id, goal_slug) VALUES (%s, %s)",
                (task_id, goal_slug),
            )
    finally:
        conn.close()

def remove_task_link(task_id: str) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM task_link WHERE task_id = %s", (task_id,))
    finally:
        conn.close()

def get_task_link(task_id: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT goal_slug FROM task_link WHERE task_id = %s", (task_id,))
            row = cur.fetchone()
            return row["goal_slug"] if row else None
    finally:
        conn.close()

# ============================
# Todoist helpers
# ============================
def validate_hmac(payload: bytes, received_hmac: str) -> bool:
    """Validate the HMAC signature in the request."""
    client_secret = os.getenv("TODOIST_CLIENT_SECRET")
    if not client_secret:
        app.logger.error("TODOIST_CLIENT_SECRET not found in environment variables.")
        return False
    try:
        expected_hmac = hmac.new(client_secret.encode(), payload, hashlib.sha256).digest()
        expected_hmac_b64 = base64.b64encode(expected_hmac).decode()
        return hmac.compare_digest(received_hmac, expected_hmac_b64)
    except Exception as e:
        app.logger.error(f"Error validating HMAC: {e}")
        return False

def post_todoist_comment(task_id, content):
    """Post a comment to a Todoist task."""
    try:
        url = f"{TODOIST_API_BASE_URL}/comments"
        headers = {
            "Authorization": f"Bearer {TODOIST_API_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"task_id": task_id, "content": content}
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            app.logger.info(f"Posted comment to task {task_id}: {content}")
        else:
            app.logger.error(
                f"Failed to post comment to task {task_id}. "
                f"Status: {response.status_code}, Body: {response.text}"
            )
    except Exception as e:
        app.logger.error(f"Error posting comment to Todoist: {e}")

def update_todoist_description(task_id, new_description):
    """Update a Todoist task's description."""
    try:
        update_url = f"{TODOIST_API_BASE_URL}/tasks/{task_id}"
        headers = {
            "Authorization": f"Bearer {TODOIST_API_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"description": new_description}
        response = requests.post(update_url, headers=headers, json=payload, timeout=10)
        if response.status_code != 200:
            app.logger.error(
                f"Failed to update task {task_id} description. "
                f"Status: {response.status_code}, Body: {response.text}"
            )
            return False
        app.logger.info(f"Updated task {task_id} description to: {new_description}")
        return True
    except Exception as e:
        app.logger.error(f"Error updating Todoist description: {e}")
        return False

def get_current_description(task_id):
    """Fetch the current description of a Todoist task."""
    try:
        get_url = f"{TODOIST_API_BASE_URL}/tasks/{task_id}"
        headers = {
            "Authorization": f"Bearer {TODOIST_API_TOKEN}",
            "Content-Type": "application/json"
        }
        resp = requests.get(get_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            app.logger.error(
                f"Failed to fetch task {task_id}. "
                f"Status: {resp.status_code}, Body: {resp.text}"
            )
            return None
        task_data = resp.json()
        return task_data.get("description", "")
    except Exception as e:
        app.logger.error(f"Error fetching Todoist task description: {e}")
        return None

def extract_task_id(event_data: dict):
    """
    Todoist webhooks can place the task id in different keys depending on event type.
    Try several likely locations and log what we see.
    """
    candidates = [
        event_data.get("id"),
        event_data.get("task_id"),
        event_data.get("item_id"),
        (event_data.get("item") or {}).get("id"),
        (event_data.get("item") or {}).get("task_id"),
    ]
    for c in candidates:
        if c:
            return str(c)
    return None

# ============================
# Beeminder helpers
# ============================
def send_beeminder_plus_one(goal_slug: str, task_id: str, requestid: str) -> bool:
    if not BEEMINDER_AUTH_TOKEN:
        app.logger.error("[Beeminder] BEEMINDER_AUTH_TOKEN missing; cannot send to Beeminder.")
        return False
    url = f"https://www.beeminder.com/api/v1/users/me/goals/{goal_slug}/datapoints.json"
    payload = {"value": 1, "auth_token": BEEMINDER_AUTH_TOKEN, "requestid": requestid}
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.status_code in (200, 201)
    except Exception as e:
        app.logger.exception(f"[Beeminder] exception: {e}")
        return False

# ============================
# Flask routes
# ============================
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        app.logger.info("Received webhook request.")
        app.logger.info(f"Request Headers: {dict(request.headers)}")
        app.logger.info(f"Raw Request Data: {request.data}")

        received_hmac = request.headers.get("X-Todoist-Hmac-SHA256", "")
        if not received_hmac:
            app.logger.error("Missing HMAC signature in headers.")
            return jsonify({"error": "Missing HMAC signature."}), 401

        if not validate_hmac(request.data, received_hmac):
            app.logger.error("Invalid HMAC signature.")
            return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json()
        if not data:
            app.logger.error("Empty or invalid JSON payload.")
            return jsonify({"error": "Malformed JSON payload."}), 400

        app.logger.info(f"Parsed JSON Payload: {data}")

        event_name = data.get("event_name")
        if not event_name:
            app.logger.error("Missing event_name in payload.")
            return jsonify({"error": "Missing event_name in payload."}), 400

        event_data = data.get("event_data", {})
        if not event_data:
            app.logger.error("Missing event_data in payload.")
            return jsonify({"error": "Missing event_data in payload."}), 400

        # ---------------------------
        # NOTE: commands via comments
        # ---------------------------
        if event_name == "note:added":
            task_id = (event_data.get("item") or {}).get("id")
            user_id = (event_data.get("item") or {}).get("user_id")
            comment_text = event_data.get("content", "").lower()

            if not task_id or not user_id:
                app.logger.error("Invalid payload: Missing task_id or user_id.")
                return jsonify({"error": "Invalid payload: Missing task_id or user_id."}), 400

            # Beeminder link commands
            if comment_text.startswith("add to beeminder"):
                match = re.search(r"add to beeminder\s+(\S+)", comment_text)
                if match:
                    goal = match.group(1)
                    add_task_link(str(task_id), goal)
                    post_todoist_comment(task_id, f"Task linked to goal '{goal}'.")
                    return jsonify({"message": "Task linked"}), 200

            elif comment_text.startswith("remove from beeminder"):
                remove_task_link(str(task_id))
                post_todoist_comment(task_id, "Task unlinked from Beeminder.")
                return jsonify({"message": "Task unlinked"}), 200

            elif comment_text.startswith("beeminder status"):
                goal = get_task_link(str(task_id))
                if goal:
                    post_todoist_comment(task_id, f"Task linked to goal '{goal}'.")
                else:
                    post_todoist_comment(task_id, "Task not linked to Beeminder.")
                return jsonify({"message": "Status sent"}), 200

            # Timer logic
            app.logger.info(f"Current timers: {timers}")
            timer_key = f"{user_id}:{task_id}"
            app.logger.info(f"Processing command for key: {timer_key}")

            if "start timer" in comment_text:
                if timer_key in timers:
                    app.logger.info(f"Timer already running for key: {timer_key}")
                    return jsonify({"message": "Timer already running."}), 200

                timers[timer_key] = {"start_time": datetime.datetime.now()}
                app.logger.info(f"Timer started for key: {timer_key}")

                current_desc = get_current_description(task_id)
                if current_desc is not None:
                    pattern = r"\(Timer Running: \d+ minutes\)"
                    updated_desc = re.sub(pattern, "", current_desc).strip()
                    timer_snippet = "(Timer Running: 0 minutes)"
                    updated_desc = f"{updated_desc} {timer_snippet}".strip() if updated_desc else timer_snippet
                    update_todoist_description(task_id, updated_desc)

                return jsonify({"message": "Timer started."}), 200

            elif "stop timer" in comment_text:
                if timer_key not in timers:
                    app.logger.info(f"No timer running for key: {timer_key}")
                    post_todoist_comment(task_id, "No timer found to stop.")
                    return jsonify({"message": "No timer running for this task."}), 200

                start_time = timers[timer_key]["start_time"]
                elapsed_time = datetime.datetime.now() - start_time
                del timers[timer_key]

                elapsed_seconds = int(elapsed_time.total_seconds())
                hours, remainder = divmod(elapsed_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                elapsed_str = f"{hours}h {minutes}m {seconds}s"

                post_todoist_comment(task_id, f"Elapsed time: {elapsed_str}")

                current_desc = get_current_description(task_id)
                if current_desc is not None:
                    total_time_pattern = r"\(Total Time: (\d+)h (\d+)m (\d+)s\)"
                    match = re.search(total_time_pattern, current_desc)

                    if match:
                        existing_hours = int(match.group(1))
                        existing_minutes = int(match.group(2))
                        existing_seconds = int(match.group(3))
                        total_seconds = (
                            existing_hours * 3600 +
                            existing_minutes * 60 +
                            existing_seconds +
                            elapsed_seconds
                        )
                        new_hours, remainder = divmod(total_seconds, 3600)
                        new_minutes, new_seconds = divmod(remainder, 60)
                        new_elapsed_str = f"{int(new_hours)}h {int(new_minutes)}m {int(new_seconds)}s"
                    else:
                        new_elapsed_str = elapsed_str

                    total_time_and_running_pattern = r"\(Total Time: .*?\)|\(Timer Running: .*?\)"
                    updated_desc = re.sub(total_time_and_running_pattern, "", current_desc).strip()
                    total_time_snippet = f"(Total Time: {new_elapsed_str})"
                    updated_desc = f"{updated_desc} {total_time_snippet}".strip() if updated_desc else total_time_snippet
                    update_todoist_description(task_id, updated_desc)

                app.logger.info(f"Timer stopped for key: {timer_key}. Elapsed time: {elapsed_str}")
                return jsonify({"message": f"Timer stopped. Total time: {elapsed_str}"}), 200

            app.logger.info("No action taken for the comment.")
            return jsonify({"message": "No action taken."}), 200

        # ---------------------------
        # NOTE: Task completed → Beeminder +1
        # ---------------------------
        elif event_name == "item:completed":
            task_id = extract_task_id(event_data)

            if not task_id:
                app.logger.error("[Completed] Could not extract task_id from event_data")
                return jsonify({"message": "Completion processed (no task id)"}), 200

            goal = get_task_link(task_id)

            if not goal:
                app.logger.warning(
                    f"[Completed] No goal linked for task {task_id}. Skipping Beeminder."
                )
                return jsonify({"message": "Completion processed (no linked goal)"}), 200

            requestid = data.get("event_id") or f"{task_id}-{datetime.datetime.utcnow().timestamp()}"
            ok = send_beeminder_plus_one(goal_slug=goal, task_id=task_id, requestid=requestid)

            timestamp = datetime.datetime.utcnow().isoformat()
            if ok:
                comment = (
                    f"Beeminder datapoint +1 sent to goal '{goal}'.\n"
                    f"Request ID: {requestid}\n"
                    f"Timestamp: {timestamp} UTC"
                )
            else:
                comment = (
                    f"Attempted Beeminder +1 to goal '{goal}' but request failed.\n"
                    f"Request ID: {requestid}\n"
                    f"Timestamp: {timestamp} UTC"
                )
            post_todoist_comment(task_id, comment)

            return jsonify({"message": "Completion processed", "beeminder_ok": ok}), 200

        else:
            app.logger.info(f"Unhandled event type: {event_name}")
            return jsonify({"message": "Event not handled"}), 200

    except Exception as e:
        app.logger.exception(f"Error in webhook processing: {e}")
        return jsonify({"error": "Internal server error."}), 500

# ============================
# Background timer updater
# ============================
def update_descriptions():
    """Update running tasks' Todoist descriptions to show elapsed time."""
    now = datetime.datetime.now()
    for timer_key, data in list(timers.items()):
        try:
            _, task_id = timer_key.split(":")
        except ValueError:
            app.logger.error(f"Timer key '{timer_key}' is invalid.")
            continue

        start_time = data.get("start_time")
        if not start_time:
            continue

        elapsed = now - start_time
        elapsed_minutes = int(elapsed.total_seconds() // 60)

        current_description = get_current_description(task_id)
        if current_description is None:
            continue

        pattern = r"\(Timer Running: \d+ minutes\)"
        updated_description = re.sub(pattern, "", current_description).strip()
        timer_snippet = f"(Timer Running: {elapsed_minutes} minutes)"
        updated_description = f"{updated_description} {timer_snippet}".strip() if updated_description else timer_snippet
        update_todoist_description(task_id, updated_description)

def start_scheduler():
    """Start the APScheduler background job to update descriptions every 1 minute."""
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(func=update_descriptions, trigger="interval", minutes=1)
    scheduler.start()

# ============================
# Health endpoint
# ============================
@app.get("/healthz")
def healthz():
    # Quick probe to ensure service is alive and DB reachable
    try:
        _ = get_task_link("nonexistent")
        db_ok = True
    except Exception as e:
        db_ok = False
        app.logger.exception(f"/healthz DB check failed: {e}")
    return {"ok": True, "db": db_ok}

# ============================
# Main
# ============================
if __name__ == '__main__':
    start_scheduler()
    app.run(port=5001, host='0.0.0.0')
