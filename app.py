from flask import Flask, request, jsonify
import datetime

app = Flask(__name__)

# In-memory store for timers
timers = {}

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    # Extract relevant information
    task_id = data.get("event_data", {}).get("id")
    user_id = data.get("event_data", {}).get("user_id")
    comment_text = data.get("event_data", {}).get("content", "").lower()

    if not task_id or not user_id:
        return jsonify({"error": "Invalid payload"}), 400

    # Unique key for each user-task timer
    timer_key = f"{user_id}:{task_id}"

    # Start Timer
    if "start timer" in comment_text:
        if timer_key in timers:
            return jsonify({"message": "Timer already running."}), 200
        timers[timer_key] = {"start_time": datetime.datetime.now()}
        return jsonify({"message": "Timer started."}), 200

    # Stop Timer
    elif "stop timer" in comment_text:
        if timer_key not in timers:
            return jsonify({"message": "No timer running for this task."}), 200

        start_time = timers[timer_key]["start_time"]
        elapsed_time = datetime.datetime.now() - start_time
        del timers[timer_key]

        # Format elapsed time
        elapsed_seconds = elapsed_time.total_seconds()
        hours, remainder = divmod(elapsed_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        elapsed_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"

        return jsonify({"message": f"Timer stopped. Total time: {elapsed_str}"}), 200

    return jsonify({"message": "No action taken."}), 200

if __name__ == '__main__':
    app.run(debug=True)
