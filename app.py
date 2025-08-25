# app.py
from flask import Flask, request
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
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple
import time

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
timers: Dict[str, Dict[str, Any]] = {}

# De-dupe stores
PROCESSED_DELIVERIES = OrderedDict()   # delivery_id -> True
PROCESSED_COMPLETIONS = OrderedDict()  # f"{task_id}:{completed_at}" -> True
PROCESSED_NOTES = OrderedDict()        # note_id -> True

MAX_DELIVERIES = 2000
MAX_COMPLETIONS = 5000
MAX_NOTES = 5000

# ---- Todoist ----
TODOIST_API_BASE_URL = "https://api.todoist.com/rest/v2"
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")
TODOIST_CLIENT_SECRET = os.getenv("TODOIST_CLIENT_SECRET")

if not TODOIST_API_TOKEN:
    raise RuntimeError("TODOIST_API_TOKEN not found in environment variables.")
if not TODOIST_CLIENT_SECRET:
    app.logger.warning("TODOIST_CLIENT_SECRET not set – HMAC validation will fail.")

# Label that triggers Beeminder logging when the task is completed
# Can be a label NAME (e.g., "beeminder") or a numeric ID string.
TRIGGER_LABEL_RAW = os.getenv("TODOIST_BEEMINDER_LABEL") or "beeminder"
TRIGGER_LABEL_NAME = TRIGGER_LABEL_RAW.lower()
TRIGGER_LABEL_ID = int(TRIGGER_LABEL_RAW) if TRIGGER_LABEL_RAW.isdigit() else None

# ---- Beeminder ----
BEEMINDER_API_BASE = "https://www.beeminder.com/api/v1"
BEEMINDER_USERNAME = os.getenv("BEEMINDER_USERNAME")
BEEMINDER_AUTH_TOKEN = os.getenv("BEEMINDER_AUTH_TOKEN")
BEEMINDER_GOAL_SLUG = os.getenv("BEEMINDER_GOAL_SLUG") or "dailyprayers"

if not (BEEMINDER_USERNAME and BEEMINDER_AUTH_TOKEN):
    app.logger.warning("BEEMINDER_USERNAME or BEEMINDER_AUTH_TOKEN not set – Beeminder posting will fail.")

# ---- Label cache (ID -> name), refreshed opportunistically ----
_label_cache: Dict[int, str] = {}
_label_cache_ts: float = 0.0
LABEL_CACHE_TTL_SEC = 600  # 10 minutes


# ============================
# Helpers
# ============================
def _dedupe_push(store: OrderedDict, key: str, maxlen: int) -> bool:
    if not key:
        return False
    if key in store:
        return True
    store[key] = True
    if len(store) > maxlen:
        store.popitem(last=False)
    return False

def _dedupe_delivery(delivery_id: str) -> bool:
    return _dedupe_push(PROCESSED_DELIVERIES, delivery_id or "", MAX_DELIVERIES)

def _dedupe_completion(key: str) -> bool:
    return _dedupe_push(PROCESSED_COMPLETIONS, key or "", MAX_COMPLETIONS)

def _dedupe_note(note_id: str) -> bool:
    return _dedupe_push(PROCESSED_NOTES, str(note_id or ""), MAX_NOTES)

def validate_hmac(payload: bytes, received_hmac: str) -> bool:
    """Validate Todoist webhook signature (base64(HMAC_SHA256(secret, raw_body)))."""
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

def comment_task_completed(task_id: str, task_title: Optional[str] = None) -> None:
    """Comment on ANY completed task, without the word 'beeminder' to avoid loops."""
    title = (task_title or "").strip()
    post_todoist_comment(task_id, f"Task completed ✅ — {title}" if title else "Task completed ✅")

def update_todoist_description(task_id: str, new_description: str) -> bool:
    """Update a Todoist task's description (works on active tasks)."""
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

def get_current_description(task_id: str) -> Optional[str]:
    """Fetch the current description of a Todoist task (active tasks only)."""
    try:
        url = f"{TODOIST_API_BASE_URL}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            app.logger.warning(f"Fetch task {task_id} failed ({resp.status_code})")
            return None
        return resp.json().get("description", "")
    except Exception as e:
        app.logger.error(f"Error fetching Todoist task: {e}")
        return None

def get_task_content(task_id: str) -> Optional[str]:
    """Fetch the task title/content (active tasks only; may 404 for completed)."""
    try:
        url = f"{TODOIST_API_BASE_URL}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None
        return (resp.json().get("content") or "").strip()
    except Exception:
        return None

def iso_to_unix(ts: Optional[str]) -> Optional[int]:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception as e:
        app.logger.warning(f"Could not parse timestamp '{ts}': {e}")
        return None

def _refresh_label_cache_if_needed():
    global _label_cache, _label_cache_ts
    now = time.time()
    if now - _label_cache_ts < LABEL_CACHE_TTL_SEC:
        return
    try:
        headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}"}
        resp = requests.get(f"{TODOIST_API_BASE_URL}/labels", headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json() or []
            _label_cache = {int(lbl["id"]): lbl.get("name", "").strip() for lbl in data if "id" in lbl}
            _label_cache_ts = now
            app.logger.info(f"Refreshed label cache with {len(_label_cache)} labels.")
        else:
            app.logger.warning(f"Label list failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        app.logger.error(f"Label cache refresh error: {e}")

def _coerce_labels_to_names(raw_labels: List[Any]) -> Tuple[List[str], List[int]]:
    """
    Accept both names and numeric IDs as seen in community examples.
    Return (label_names_lower, label_ids).
    """
    if not raw_labels:
        return [], []
    names: List[str] = []
    ids: List[int] = []
    # Determine if labels are IDs (ints/str digits) or names
    all_numbers = all(isinstance(x, int) or (isinstance(x, str) and x.isdigit()) for x in raw_labels)
    if all_numbers:
        ids = [int(x) for x in raw_labels]
        _refresh_label_cache_if_needed()
        for lid in ids:
            n = _label_cache.get(lid)
            if n:
                names.append(n.lower())
        return names, ids
    # Otherwise treat as names
    names = [str(x).strip().lower() for x in raw_labels]
    return names, []

def post_beeminder_datapoint(value: float, comment: str, timestamp: Optional[int], requestid: Optional[str]) -> bool:
    """Create a datapoint on Beeminder for the configured goal (idempotent via requestid)."""
    if not (BEEMINDER_USERNAME and BEEMINDER_AUTH_TOKEN):
        app.logger.error("Beeminder credentials missing.")
        return False
    try:
        url = f"{BEEMINDER_API_BASE}/users/{BEEMINDER_USERNAME}/goals/{BEEMINDER_GOAL_SLUG}/datapoints.json"
        data = {"auth_token": BEEMINDER_AUTH_TOKEN, "value": value, "comment": comment}
        if timestamp is not None:
            data["timestamp"] = timestamp
        if requestid:
            data["requestid"] = requestid
        resp = requests.post(url, data=data, timeout=15)
        if resp.status_code in (200, 201):
            app.logger.info(f"Beeminder datapoint OK: {resp.text}")
            return True
        app.logger.error(f"Beeminder datapoint FAILED ({resp.status_code}): {resp.text}")
        return False
    except Exception as e:
        app.logger.error(f"Error posting to Beeminder: {e}")
        return False

# ---------- Completion normalization (mirrors how other repos read event_data) ----------
def _as_bool(v: Any) -> bool:
    return bool(v) and str(v).lower() not in ("false", "0", "none", "null")

def _normalize_completion(event_name: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalize various community-seen completion signals to one shape:
    { task_id, content, labels (names lower), label_ids, completed_at_iso }
    Supports:
      - item:completed  (common)
      - task:completed  (seen in some REST-created webhooks)
      - item:updated + checked true or update_intent=='item_completed'
    """
    ev = data.get("event_data") or {}
    # case: item:completed (typical)
    if event_name == "item:completed":
        task = ev
        task_id = str(task.get("id") or "")
        content = (task.get("content") or "").strip()
        labels_raw = task.get("labels") or []
        names, ids = _coerce_labels_to_names(labels_raw)
        completed_at = task.get("completed_at") or data.get("triggered_at")
        return {"task_id": task_id, "content": content, "label_names": names, "label_ids": ids, "completed_at": completed_at}

    # case: task:completed (seen in some community repos)
    if event_name == "task:completed":
        # some payloads mirror item:completed; others may send a subset
        task = ev
        # Try common keys first, then fallbacks
        task_id = str(task.get("id") or task.get("task_id") or "")
        content = (task.get("content") or "").strip()
        labels_raw = task.get("labels") or []
        names, ids = _coerce_labels_to_names(labels_raw)
        completed_at = task.get("completed_at") or data.get("triggered_at")
        return {"task_id": task_id, "content": content, "label_names": names, "label_ids": ids, "completed_at": completed_at}

    # case: item:updated → completion
    if event_name == "item:updated":
        task = ev
        # If Todoist includes 'update_intent', use that; otherwise judge by checked/completed_at.
        intent = (task.get("update_intent") or "").lower()
        checked = _as_bool(task.get("checked"))
        completed_at = task.get("completed_at") or data.get("triggered_at")
        if intent == "item_completed" or (checked and completed_at):
            task_id = str(task.get("id") or "")
            content = (task.get("content") or "").strip()
            labels_raw = task.get("labels") or []
            names, ids = _coerce_labels_to_names(labels_raw)
            return {"task_id": task_id, "content": content, "label_names": names, "label_ids": ids, "completed_at": completed_at}

    return None


# ============================
# Webhook endpoint
# ============================
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        received_hmac = request.headers.get("X-Todoist-Hmac-SHA256", "")
        delivery_id = request.headers.get("X-Todoist-Delivery-ID")  # unique per event

        # Validate HMAC (community examples sometimes skip, but we keep it)
        if not received_hmac or not validate_hmac(request.data, received_hmac):
            app.logger.error("Invalid or missing HMAC.")
            return "", 401

        # De-dupe retries by delivery id
        if _dedupe_delivery(delivery_id):
            app.logger.info(f"Duplicate delivery {delivery_id}; skipping.")
            return "", 200

        body = request.get_json(silent=True) or {}
        event_name = (body.get("event_name") or "").strip()
        event_data = body.get("event_data") or {}
        app.logger.info(f"Event: {event_name}")

        # ======= Completion handling like other users do =======
        normalized = _normalize_completion(event_name, body)
        if normalized:
            task_id = normalized["task_id"]
            task_title = normalized["content"]
            label_names = normalized["label_names"]
            label_ids = normalized["label_ids"]
            completed_at = normalized["completed_at"]
            ts = iso_to_unix(completed_at)

            # completion de-dupe
            completion_key = f"{task_id}:{completed_at or ''}"
            if _dedupe_completion(completion_key):
                app.logger.info(f"Duplicate completion {completion_key}; skipping.")
                return "", 200

            # Always comment "Task completed"
            if task_id:
                comment_task_completed(task_id, task_title)

            # If labeled for Beeminder, count +1
            label_match = (TRIGGER_LABEL_NAME in label_names) or (TRIGGER_LABEL_ID is not None and TRIGGER_LABEL_ID in label_ids)
            if label_match:
                bm_comment = f"Todoist: {task_title}"
                reqid = f"complete:{completion_key}"
                ok = post_beeminder_datapoint(value=1, comment=bm_comment, timestamp=ts, requestid=reqid)
                post_todoist_comment(task_id, "Counted ✅" if ok else "Failed to count ❌")

            return "", 200

        # ===== Comment triggers (note:added) =====
        if event_name == "note:added":
            task_id = event_data.get("item", {}).get("id")
            user_id = event_data.get("item", {}).get("user_id")
            note_id = event_data.get("id")
            comment_text = (event_data.get("content") or "")
            lowered = comment_text.lower()
            note_time = event_data.get("posted_at") or event_data.get("posted") or body.get("triggered_at")
            ts = iso_to_unix(note_time)

            if not task_id or not user_id:
                app.logger.error("Invalid payload: Missing task_id or user_id.")
                return "", 400

            if _dedupe_note(str(note_id) if note_id is not None else ""):
                app.logger.info(f"Duplicate note {note_id}; skipping.")
                return "", 200

            # Strict trigger: exactly "beeminder" or "bm" to add +1
            if lowered.strip() in ("beeminder", "bm"):
                title = get_task_content(task_id) or "(untitled task)"
                bm_comment = f"Todoist (comment): {title}"
                reqid = f"note:{note_id}" if note_id else f"note:{task_id}:{ts or ''}"
                ok = post_beeminder_datapoint(value=1, comment=bm_comment, timestamp=ts, requestid=reqid)
                post_todoist_comment(task_id, "Counted ✅" if ok else "Failed to count ❌")
                return "", 200

            # Timer controls
            timer_key = f"{user_id}:{task_id}"
            if "start timer" in lowered:
                if timer_key not in timers:
                    timers[timer_key] = {"start_time": datetime.datetime.now()}
                    current_desc = get_current_description(task_id)
                    if current_desc is not None:
                        pattern = r"\(Timer Running: \d+ minutes\)"
                        updated = re.sub(pattern, "", current_desc).strip()
                        snippet = "(Timer Running: 0 minutes)"
                        updated = f"{updated} {snippet}".strip() if updated else snippet
                        update_todoist_description(task_id, updated)
                return "", 200

            if "stop timer" in lowered:
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
