from flask import Flask, request, jsonify
import datetime
from dotenv import load_dotenv
import os
import logging
import hmac
import hashlib
import base64
import requests

# New imports for scheduling and regex
from apscheduler.schedulers.background import BackgroundScheduler
import re

# Load environment variables from .env
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()  # Logs will be viewable in your environment (e.g. Railway)
    ]
)
app = Flask(__name__)

# In-memory store for timers
timers = {}

TODOIST_API_BASE_URL = "https://api.todoist.com/rest/v2"
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")

if not TODOIST_API_TOKEN:
    raise RuntimeError("TODOIST_API_TOKEN not found in environment variables.")

def validate_hmac(payload, received_hmac):
    """Validate the HMAC signature in the request."""
    client_secret = os.getenv("TODOIST_CLIENT_SECRET")
    if not client_secret:
        app.logger.error("TODOIST_CLIENT_SECRET not found in environment variables.")
        return False

    try:
        # Generate expected HMAC
        expected_hmac = hmac.new(client_secret.encode(), payload, hashlib.sha256).digest()
        expected_hmac_b64 = base64.b64encode(expected_hmac).decode()

        # Compare HMACs
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
        payload = {
            "task_id": task_id,
            "content": content
        }
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code in (200, 201):
            app.logger.info(f"Successfully posted comment to task {task_id}: {content}")
        else:
            app.logger.error(
                f"Failed to post comment to task {task_id}. "
                f"Status code: {response.status_code}, Response: {response.text}"
            )
    except Exception as e:
        app.logger.error(f"Error posting comment to Todoist: {e}")

def update_todoist_description(task_id, new_description):
    """
    Helper function to update a Todoist task's description
    using the official POST /rest/v2/tasks/{task_id} endpoint.
    """
    try:
        update_url = f"{TODOIST_API_BASE_URL}/tasks/{task_id}"
        headers = {
            "Authorization": f"Bearer {TODOIST_API_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"description": new_description}
        response = requests.post(update_url, headers=headers, json=payload)

        if response.status_code != 200:
            app.logger.error(
                f"Failed to update task {task_id} description. "
                f"Status: {response.status_code}, Response: {response.text}"
            )
            return False

        app.logger.info(f"Successfully updated task {task_id} description to: {new_description}")
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
        resp = requests.get(get_url, headers=headers)

        if resp.status_code != 200:
            app.logger.error(
                f"Failed to fetch task {task_id}. "
                f"Status: {resp.status_code}, Response: {resp.text}"
            )
            return None

        task_data = resp.json()
        return task_data.get("description", "")
    except Exception as e:
        app.logger.error(f"Error fetching Todoist task description: {e}")
        return None

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # Log receipt of webhook
        app.logger.info("Received webhook request.")
        app.logger.info(f"Request Headers: {request.headers}")
        app.logger.info(f"Raw Request Data: {request.data}")

        # Validate HMAC
        received_hmac = request.headers.get("X-Todoist-Hmac-SHA256", "")
        if not received_hmac:
            app.logger.error("Missing HMAC signature in headers.")
            return jsonify({"error": "Missing HMAC signature."}), 401

        if not validate_hmac(request.data, received_hmac):
            app.logger.error("Invalid HMAC signature.")
            return jsonify({"error": "Unauthorized"}), 401

        # Parse JSON payload
        data = request.get_json()
        if not data:
            app.logger.error("Empty or invalid JSON payload.")
            return jsonify({"error": "Malformed JSON payload."}), 400

        app.logger.info(f"Parsed JSON Payload: {data}")

        event_name = data.get("event_name")
        if not event_name:
            app.logger.error("Missing event_name in payload.")
            return jsonify({"error": "Missing event_name in payload."}), 400

        # We only handle note:added
        if event_name != "note:added":
            app.logger.info(f"Unhandled event type: {event_name}")
            return jsonify({"message": "Event not handled"}), 200

        # Extract relevant fields from `event_data`
        event_data = data.get("event_data", {})
        if not event_data:
            app.logger.error("Missing event_data in payload.")
            return jsonify({"error": "Missing event_data in payload."}), 400

        task_id = event_data.get("item", {}).get("id")  # Extract task_id from `item`
        user_id = event_data.get("item", {}).get("user_id")  # Extract user_id from `item`
        comment_text = event_data.get("content", "").lower()

        if not task_id or not user_id:
            app.logger.error("Invalid payload: Missing task_id or user_id.")
            return jsonify({"error": "Invalid payload: Missing task_id or user_id."}), 400

        # Log current timers for debugging
        app.logger.info(f"Current timers: {timers}")

        # Timer logic
        timer_key = f"{user_id}:{task_id}"
        app.logger.info(f"Processing command for key: {timer_key}")

        if "start timer" in comment_text:
            # START TIMER
            if timer_key in timers:
                app.logger.info(f"Timer already running for key: {timer_key}")
                return jsonify({"message": "Timer already running."}), 200

            timers[timer_key] = {"start_time": datetime.datetime.now()}
            app.logger.info(f"Timer started for key: {timer_key}")

            # INSTANT FEEDBACK: Immediately update task description with "(Timer Running: 0 minutes)"
            current_desc = get_current_description(task_id)
            if current_desc is not None:
                # Remove any old snippet
                pattern = r"\(Timer Running: \d+ minutes\)"
                updated_desc = re.sub(pattern, "", current_desc).strip()

                # Add snippet for immediate feedback
                timer_snippet = "(Timer Running: 0 minutes)"
                if updated_desc:
                    updated_desc = f"{updated_desc} {timer_snippet}".strip()
                else:
                    updated_desc = timer_snippet

                update_todoist_description(task_id, updated_desc)

            return jsonify({"message": "Timer started."}), 200

        elif "stop timer" in comment_text:
            # STOP TIMER
            if timer_key not in timers:
                app.logger.info(f"No timer running for key: {timer_key}")
                post_todoist_comment(task_id, "No timer found to stop.")
                return jsonify({"message": "No timer running for this task."}), 200

            start_time = timers[timer_key]["start_time"]
            elapsed_time = datetime.datetime.now() - start_time
            del timers[timer_key]

            # Calculate final elapsed time
            elapsed_seconds = elapsed_time.total_seconds()
            hours, remainder = divmod(elapsed_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            elapsed_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"

            # OPTIONAL: Post the elapsed time as a comment (can remove if undesired)
            post_todoist_comment(task_id, f"Elapsed time: {elapsed_str}")

            # Now, also update the Todoist description to show final total time
            current_desc = get_current_description(task_id)
            if current_desc is not None:
                # Remove any "(Timer Running: XX minutes)" snippet
                pattern = r"\(Timer Running: \d+ minutes\)"
                updated_desc = re.sub(pattern, "", current_desc).strip()

                # Append the final "Total Time" snippet
                # e.g. "(Total Time: 1h 23m 45s)"
                total_time_snippet = f"(Total Time: {elapsed_str})"
                if updated_desc:
                    updated_desc = f"{updated_desc} {total_time_snippet}".strip()
                else:
                    updated_desc = total_time_snippet

                update_todoist_description(task_id, updated_desc)

            app.logger.info(f"Timer stopped for key: {timer_key}. Elapsed time: {elapsed_str}")
            return jsonify({"message": f"Timer stopped. Total time: {elapsed_str}"}), 200

        app.logger.info("No action taken for the comment.")
        return jsonify({"message": "No action taken."}), 200

    except Exception as e:
        app.logger.error(f"Error in webhook processing: {e}")
        return jsonify({"error": "Internal server error."}), 500

def update_descriptions():
    """Update running tasks' Todoist descriptions to show elapsed time."""
    now = datetime.datetime.now()

    for timer_key, data in timers.items():
        # timer_key might be "user_id:task_id"
        try:
            user_id, task_id = timer_key.split(":")
        except ValueError:
            app.logger.error(f"Timer key '{timer_key}' is invalid.")
            continue

        start_time = data.get("start_time")
        if not start_time:
            continue

        # Calculate elapsed time in minutes
        elapsed = now - start_time
        elapsed_minutes = int(elapsed.total_seconds() // 60)

        # 1) Fetch the current task description
        current_description = get_current_description(task_id)
        if current_description is None:
            continue  # We already logged the error

        # 2) Remove any old "(Timer Running: X minutes)" snippet
        pattern = r"\(Timer Running: \d+ minutes\)"
        updated_description = re.sub(pattern, "", current_description).strip()

        # 3) Append the new snippet
        timer_snippet = f"(Timer Running: {elapsed_minutes} minutes)"
        if updated_description:
            updated_description = f"{updated_description} {timer_snippet}".strip()
        else:
            updated_description = timer_snippet

        # 4) Update the task description
        update_todoist_description(task_id, updated_description)

def start_scheduler():
    """Start the APScheduler background job to update descriptions every 1 minute."""
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=update_descriptions,
        trigger="interval",
        minutes=1  # Changed to 1 minute for quicker testing
    )
    scheduler.start()

if __name__ == '__main__':
    # Start the scheduler
    start_scheduler()

    # Run the Flask app
    app.run(port=5001, host='0.0.0.0')
