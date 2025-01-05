# Todoist Timer Webhook

A personal Flask application that listens for Todoist webhooks, starts/stops timers based on special comments, and updates the task’s description with elapsed time. **This project is for personal use only** due to its complexity. If more users express interest, I will consider making it more widely available.

## Features

- **Start Timer**: Comment "`start timer`" on a Todoist task to begin tracking time.
- **Stop Timer**: Comment "`stop timer`" on the same task to stop tracking and save the elapsed time.
- **Automatic Updates**: Task descriptions are updated in the background to show how long a timer has been running or the total accumulated time.
- **Security**: Validates webhooks from Todoist via HMAC signatures.

## How It Works

1. **Webhook Endpoint**:
   - The application exposes a single `/webhook` endpoint which Todoist calls whenever a note/comment is added to a task.
   - If the comment contains specific trigger phrases (like `start timer` or `stop timer`), the application processes them accordingly.

2. **Timer Logic**:
   - Uses an in-memory Python dictionary (`timers`) keyed by `user_id:task_id` to track the start times.
   - When a timer is running, the app periodically updates the description (via APScheduler) to reflect the running time in minutes.
   - When a timer is stopped, it adds the elapsed time to the “Total Time” in the description or creates a new one if it doesn’t exist.

3. **Environment & Secrets**:
   - Expects a `.env` file with:
     - `TODOIST_API_TOKEN`
     - `TODOIST_CLIENT_SECRET`
   - These are used to authenticate requests to Todoist and validate incoming webhooks.

## Requirements

- **Python 3.7+** (due to usage of `datetime`, `typing`, APScheduler, etc.)
- **Flask** (for the webhook server)
- **APScheduler** (for background scheduling)
- **Requests** (for making Todoist API calls)
- **python-dotenv** (for loading environment variables from `.env`)

Install dependencies with:

```bash
pip install -r requirements.txt
```

*(Or install them individually if you aren’t using a `requirements.txt` file.)*

## Setup

1. **Clone the Repo**:
   ```bash
   git clone https://github.com/your-username/todoist-timer-webhook.git
   cd todoist-timer-webhook
   ```

2. **Create a `.env` File**:
   ```bash
   touch .env
   # Then fill in the required variables:
   # TODOIST_API_TOKEN=your_todoist_api_token
   # TODOIST_CLIENT_SECRET=your_todoist_client_secret
   ```

3. **Install Python Packages**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the App**:
   ```bash
   python app.py
   ```
   Or:
   ```bash
   FLASK_APP=app.py flask run --host=0.0.0.0 --port=5001
   ```
   The server will listen on port `5001` by default.

5. **Configure Todoist Webhook**:
   - Create a webhook in your Todoist integration settings pointing to your server URL (for example `https://yourdomain.com/webhook`).
   - Make sure you enter the same `TODOIST_CLIENT_SECRET` in your Todoist app settings as in your `.env`.

## Usage

- **Start a Timer**:
  Comment `start timer` on a Todoist task.
  The description will update with `(Timer Running: 0 minutes)` and periodically refresh.

- **Stop a Timer**:
  Comment `stop timer` on the same task.
  The elapsed time gets recorded in the description as `(Total Time: xh xm xs)`.

- **Multiple Timers**:
  Each user-task combination is tracked independently. Starting a new timer on a different task won’t affect other tasks.

## Troubleshooting

- **HMAC Signature Errors**:
  Make sure your `TODOIST_CLIENT_SECRET` matches what is configured in Todoist’s integration settings.
- **Description Not Updating**:
  Verify that APScheduler is running properly. Check logs for errors in fetching or updating the Todoist task.
- **Environment Variables**:
  Double-check `.env` files or your deployment environment if any variables (e.g., `TODOIST_API_TOKEN`) are missing.

## Contributing

At this point, **I have built this for my own personal use** and am not scaling it for more users due to complexity. However, if you’re interested in collaborating or improving it, feel free to open issues or pull requests, and we can discuss potentially expanding it for a broader audience.
