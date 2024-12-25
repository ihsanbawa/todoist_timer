from flask import Flask, request, jsonify
import datetime
from pyngrok import ngrok
from dotenv import load_dotenv
import os
import logging

# Load environment variables from .env
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# In-memory store for timers
timers = {}

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # Log request headers and raw request data for debugging
        app.logger.info(f"Request Headers: {request.headers}")
        app.logger.info(f"Raw Request Data: {request.data}")

        # Parse JSON payload
        data = request.json
        app.logger.info(f"Parsed JSON Payload: {data}")

        # Validate and process payload
        if not data:
            app.logger.error("No data received in request.")
            return jsonify({"error": "No data received"}), 400

        event_name = data.get("event_name")
        if event_name != "note:added":
            app.logger.info(f"Unhandled event type: {event_name}")
            return jsonify({"message": "Event not handled"}), 200

        # Extract relevant fields
        task_id = data.get("event_data", {}).get("item_id")
        user_id = data.get("event_data", {}).get("user_id")
        comment_text = data.get("event_data", {}).get("content", "").lower()

        if not task_id or not user_id:
            app.logger.error("Invalid payload: Missing task_id or user_id")
            return jsonify({"error": "Invalid payload"}), 400

        # Timer logic
        timer_key = f"{user_id}:{task_id}"

        if "start timer" in comment_text:
            if timer_key in timers:
                app.logger.info(f"Timer already running for key: {timer_key}")
                return jsonify({"message": "Timer already running."}), 200
            timers[timer_key] = {"start_time": datetime.datetime.now()}
            app.logger.info(f"Timer started for key: {timer_key}")
            return jsonify({"message": "Timer started."}), 200

        elif "stop timer" in comment_text:
            if timer_key not in timers:
                app.logger.info(f"No timer running for key: {timer_key}")
                return jsonify({"message": "No timer running for this task."}), 200

            start_time = timers[timer_key]["start_time"]
            elapsed_time = datetime.datetime.now() - start_time
            del timers[timer_key]

            elapsed_seconds = elapsed_time.total_seconds()
            hours, remainder = divmod(elapsed_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            elapsed_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"

            app.logger.info(f"Timer stopped for key: {timer_key}. Elapsed time: {elapsed_str}")
            return jsonify({"message": f"Timer stopped. Total time: {elapsed_str}"}), 200

        app.logger.info("No action taken for the comment.")
        return jsonify({"message": "No action taken."}), 200

    except Exception as e:
        app.logger.error(f"Error in webhook processing: {e}")
        return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    # Retrieve ngrok auth token from environment variables
    ngrok_auth_token = os.getenv("NGROK_AUTH_TOKEN")
    if not ngrok_auth_token:
        raise RuntimeError("NGROK_AUTH_TOKEN not found in environment variables.")

    # Set the ngrok auth token
    ngrok.set_auth_token(ngrok_auth_token)

    # Start ngrok and expose Flask app
    public_url = ngrok.connect("5001").public_url
    print(f" * ngrok tunnel: {public_url}")
    app.logger.info(f"ngrok tunnel running at: {public_url}")

    # Run the Flask app
    app.run(port=5001)
