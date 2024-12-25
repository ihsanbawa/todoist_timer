# **Todoist Timer: Technical Documentation**

## **1. Overview**

**Purpose**: A small application that integrates with Todoist to enable time tracking on individual tasks. The user can start or stop timers via Todoist comments (e.g., “Start Timer” or “Stop Timer”). The app receives a webhook from Todoist, stores timing data, and posts elapsed time back to Todoist as comments.

**Primary Objective**: Provide a **lightweight, cost-effective** timer solution that can be deployed on free or low-cost hosting (e.g., Railway.app), and scaled up if needed.

---

## **2. High-Level Architecture**

Below is a simple diagram illustrating the Phase 1 or Phase 2 architecture, which is sufficient for most small-scale usage:

```
     +-----------+        +----------------------+
     |   User    |        | 1. User comments     |
     | (Todoist) |------->| "Start Timer" on     |
     +-----------+        | a Todoist task       |
                           +----------------------+
                                          |
                                          v
                           +---------------------------+
                           |   Todoist Webhooks       |
                           |  (Sends event to App)    |
                           +-----------+---------------+
                                       |
                                       v
                          +----------------------------+
                          |     Timer App (Flask)     |
                          |----------------------------|
                          |  /webhook endpoint        |
                          |  In-memory or JSON store  |
                          |  Timer logic & scheduling |
                          +-----------+----------------+
                                      |
                                      v
                           +---------------------------+
                           |    Todoist REST API      |
                           | (Post timer comments)    |
                           +---------------------------+
```

1. **User** adds a comment in Todoist, like “Start Timer.”
2. **Todoist** calls the **webhook** endpoint in our **Timer App** with event details (user ID, task ID, comment text).
3. **Timer App**:
   - Parses the comment, starts or stops the timer in an **in-memory** (or **JSON**/SQLite) store.
   - When stopping the timer, posts a comment via the **Todoist REST API** stating the total time elapsed.

---

## **3. Components & Responsibilities**

### **3.1. Timer App**

- **Language**: Python (Flask or FastAPI for ease of building a small REST/Webhook service).  
- **Endpoints**:
  1. `POST /webhook`: Receives JSON payload from Todoist whenever a comment is added to a task.  
  2. Optionally, future endpoints for manual start/stop or a small web interface (Phase 3).

- **Core Modules**:
  - **timer_logic.py**: Functions for starting, stopping, and updating timers.
  - **storage.py**: Abstracts the storage mechanism (in-memory dict, JSON, or SQLite).
  - **webhook_handler.py**: Parses Todoist webhook payloads to invoke `start_timer()` or `stop_timer()`.
  - **todoist_api.py**: Utility to post comments/updates to Todoist using `requests`.

- **Deployment**:  
  - Hosted on **Railway.app** (Phase 1: free tier).  
  - A single container or small instance that runs continuously, listening for webhooks.

### **3.2. Storage Layer**

- **Phase 1**:  
  - **In-memory dictionary** (simple and fast, but not persistent on restarts).  
- **Phase 2**:  
  - **JSON file** or **SQLite** database for basic persistence.  
- **Phase 4**:  
  - Potentially **Redis** if horizontal scaling or shared state across instances is needed.

### **3.3. Scheduler / Background Jobs (Phases 2+)**

- Uses something like Python’s **`schedule`** or **APScheduler** to periodically:
  - Check all active timers.  
  - Post an update comment (“Timer running: X minutes”) to Todoist.  
  - This provides “live” feedback to users.

---

## **4. Design Rationales**

### **4.1. Why Use Webhooks (Instead of Polling)?**

- **Reduced API Calls & Cost**: Polling Todoist for changes would consume resources, potentially leading to cost or hitting rate limits.  
- **Real-Time Updates**: Webhook approach notifies the app immediately when a user comments on a task. This is more responsive and cost-effective.

### **4.2. Why In-Memory or JSON Storage for Phase 1?**

- **Simplicity & Zero Cost**: No external database needed.  
- **Ease of Prototyping**: Faster to implement, minimal dependencies.  
- **Sufficient for Small Scale**: If the app restarts infrequently and usage is low, in-memory or JSON is usually enough.

### **4.3. Why a Small Python Web Framework (Flask/FastAPI)?**

- **Lightweight**: Simple to implement a single webhook endpoint and a few helper endpoints.  
- **Community & Library Support**: Python has rich libraries (`requests`, scheduling libraries).  
- **Fast Development**: Rapid prototyping with minimal boilerplate.

### **4.4. Why Railway.app for Deployment?**

- **Free Tier**: Enough to host a small always-on service without incurring monthly costs.  
- **Ease of Use**: Simple Git or Docker-based deployments, friendly UI for environment variables.  
- **Scalability**: If you exceed free resources, you can pay on a usage-based model (still relatively low cost).

### **4.5. Reasoning for Phased Approach**

1. **Phase 1**: Validate the core idea (start/stop timers) with minimal cost and complexity.  
2. **Phase 2**: Add **periodic updates** (live running time) and **basic persistence** so restarts don’t lose timers.  
3. **Phase 3**: Introduce a **web interface** and multi-user management, allowing users to see or control timers outside of Todoist comments.  
4. **Phase 4**: **Redis + Horizontal Scaling** for apps with more users or mission-critical reliability.  
5. **Phase 5**: **Advanced features** (analytics, notifications, CI/CD pipeline) once the system is stable and you have sufficient user adoption to warrant them.

This stepwise progression ensures you only **increase complexity** (and potential cost) as the user base and feature requirements grow.

---

## **5. Deployment & Operations**

### **5.1. Environment Variables**

- **`TODOIST_API_TOKEN`**: The personal or app-level token for Todoist.  
- **`FLASK_ENV`** or similar for controlling dev vs. production settings.  
- (Phases 2+) **`DB_PATH`** or **`JSON_PATH`** if storing a local file or SQLite DB.  
- (Phases 4+) **`REDIS_URL`** if connecting to a hosted Redis.

### **5.2. Build & Deploy (Railway)**

1. **Create a Railway Project**.  
2. **Add your repo** (or Docker image).  
3. **Configure Environment Variables** in the Railway dashboard.  
4. **Railway builds** and runs your app.  
5. **Copy the generated URL** (e.g., `https://mytodoisttimer.up.railway.app/webhook`).  
6. **Register the URL** in Todoist’s webhook settings.

### **5.3. Logging & Monitoring**

- **Railway Logs**: The console output of your application is visible in the Railway dashboard.  
- **Error Logging**: Use Python’s `logging` to log error messages.  
- (Optional) Integrate a free or low-cost logging/monitoring service if desired.

### **5.4. Scaling**

- **Phase 1-2**: Single instance is sufficient.  
- **Phase 3**: You might consider a slightly larger instance if the dashboard sees higher traffic.  
- **Phase 4**: Multiple containers + Redis. Railway can spin up multiple instances and you can add a managed Redis add-on.

---

## **6. Future Enhancements & Considerations**

1. **Security**  
   - Validate Todoist webhooks (e.g., by verifying request signatures, if Todoist provides them).  
   - If you add a public dashboard, secure it with basic auth or a user-based login.

2. **Error Handling & Retries**  
   - Handle 429 “Too Many Requests” or temporary network issues when calling Todoist’s API.  
   - Implement exponential backoff on retries.

3. **Notifications**  
   - Optionally notify users (via email or other means) if a timer is running beyond a set threshold.

4. **Analytics / Reporting**  
   - Summaries of total time spent per user/task.  
   - Could be shown in a simple UI or emailed to users weekly.

5. **CI/CD & Testing**  
   - Include unit tests mocking Todoist’s API.  
   - Automate deployment from GitHub → Railway upon merges to `main`.

---

## **7. Conclusion**

This **Todoist Timer** application is built to be **low-cost, lightweight, and easily extendable**. The architecture is simple by design:

- **Phase 1** focuses on MVP functionality (start/stop timer) and leverages **Todoist webhooks** for real-time event triggers.  
- **Phase 2** adds scheduled updates (live tracking) and basic persistence to avoid losing timers on restart.  
- **Phase 3** introduces a user-friendly web interface and improved multi-user support.  
- **Phase 4** scales the app with Redis and multiple instances, allowing for higher reliability and concurrency.  
- **Phase 5** polishes the solution with advanced analytics, notifications, and a more mature CI/CD pipeline.

By employing a **phased approach**, you keep early costs at **$0** on Railway while validating the idea. As usage grows, you can invest in more robust infrastructure and feature sets, ensuring the project remains **cost-effective and responsive** for the user base.