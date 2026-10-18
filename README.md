# 🚆 CP Train Ticket Discord Bot

An automated Discord bot designed for **Comboios de Portugal (CP)** that books Intercidades (IC) train tickets right when reservations unlock (24 hours before train departure, e.g., for the *Passe Ferroviário Verde*).

The bot handles station searches, automated Keycloak login, seat selection optimization, and resilient scheduling backed by a persistent SQLite database.

---

## 📋 Table of Contents
- [Features](#-features)
- [Bot Commands](#-bot-commands)
- [Configuration (.env)](#-configuration-env)
- [Storing Secrets in Google Secret Manager](#-storing-secrets-in-google-secret-manager)
- [Local Installation](#-local-installation)
- [Running as a Background Service (systemd)](#-running-as-a-background-service-systemd)
  - [Service Management (Start, Logs, Stop, Restart)](#-service-management)
- [Crash Resilience & Reliability](#-crash-resilience--reliability)

---

## ✨ Features
* **Automated 24h Booking**: Locks seats 5 minutes before the 24-hour mark, holds the seat, waits until the exact second of unlock, and completes checkout.
* **Retries Under Load**: Every checkout step is retried with backoff for as long as the 15-minute seat lock lasts. If the lock runs out, a new seat is locked and checkout starts over (up to 2 times). It never retries blindly after a confirmation that may already have gone through.
* **Train Watch**: `!watch` retries buying a full train every N minutes until a seat frees up.
* **Manual Seat Picker**: While a seat is locked, the 🪑 button shows the train's seat map and lets you move to any free seat. 🛑 aborts the booking.
* **Seat Preference Engine**: Automatically inspects the train carriage map and upgrades to preferred window/table seats.
* **Access Control**: Only the users in `DISCORD_ALLOWED_USER_IDS` (by default, the `DISCORD_USER_PING` user) can use the bot.
* **Live Discord Feedback**: Streams real-time updates directly to Discord (seat locked, countdown timer, discount applied, final PDF link).
* **Crash-Proof Persistence**: Scheduled jobs are saved in a local SQLite database (`jobs.sqlite`) so no scheduled booking is lost if the bot or VM restarts.

---

## 🤖 Bot Commands

| Command | Description | Example |
| :--- | :--- | :--- |
| `!ticket [YYYY-MM-DD]` | Starts the interactive route search and booking wizard. | `!ticket 2026-09-04` |
| `!watch [YYYY-MM-DD]` | Same wizard, but checks a (full) train every N minutes (default 15) from the 24h unlock mark until departure, and buys the first seat that frees up. | `!watch 2026-09-04` |
| `!list` | Lists scheduled bookings, watches, and bookings in progress. | `!list` |
| `!cancel` | Opens an interactive dropdown to cancel any scheduled booking or watch. | `!cancel` |
| `!ping` | Health check displaying bot latency and count of scheduled jobs / bookings in progress. | `!ping` |

While a booking is in progress, its live status message has two buttons:
* **🪑 View / change seat**: Shows the seat map of each car (`123` free, `·` taken, `[123]` yours) with dropdowns to move to any free seat. Available while the seat is locked and before confirmation starts.
* **🛑 Abort**: Stops the booking (and the watch, for watched trains).

If a scheduled booking can't lock any seat, the failure message offers a **👀 Watch this train** button.

---

## ⚙️ Configuration (.env)

Create a `.env` file in the root directory (for local runs; on the VM prefer [Secret Manager](#-storing-secrets-in-google-secret-manager)):

```env
# Discord Configuration
DISCORD_BOT_TOKEN="your_bot_token_here"
DISCORD_USER_PING="<@your_discord_user_id>"
DISCORD_CHANNEL_ID="optional_default_channel_id"
DISCORD_ALLOWED_USER_IDS="your_discord_user_id"   # comma separated; defaults to the DISCORD_USER_PING user

# CP (Comboios de Portugal) Credentials
CP_EMAIL="your_cp_email@example.com"
CP_PASSWORD="your_cp_password"
CP_NAME="Your Full Name"
CP_MOBILE="912345678"
CP_PASSENGER_ID="12345678"          # Citizen Card (Cartão de Cidadão)
CP_GREEN_PASS="12345678901"         # Green Rail Pass Number (Passe Ferroviário Verde)
```

Optional environment variables (normally set in the systemd unit, not in the secret):

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `COMBOIO_SECRET` | unset | Secret Manager secret to load settings from (see below). |
| `COMBOIO_DATA_DIR` | `.` | Directory of `jobs.sqlite`. |

---

## 🔐 Storing Secrets in Google Secret Manager

Instead of a `.env` file on the VM's disk, the bot can read the same `KEY="value"` content from a Secret Manager secret at startup. It authenticates as the VM's service account through the metadata server, so no credentials file is needed. `.env` keeps working for local runs.

One-time setup (from Cloud Shell or any machine with `gcloud`; replace `VM`, `ZONE`, `PROJECT`):

```bash
gcloud services enable secretmanager.googleapis.com
gcloud secrets create comboio-env --replication-policy=automatic
gcloud secrets versions add comboio-env --data-file=.env     # your local .env

# Let the VM's service account read this one secret
SA=$(gcloud compute instances describe VM --zone ZONE --format='value(serviceAccounts[0].email)')
gcloud secrets add-iam-policy-binding comboio-env   --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor

# VMs created with the default access scopes can't call Secret Manager; widen the scope
# (IAM above still limits what the VM can do). Requires a stop/start:
gcloud compute instances stop VM --zone ZONE
gcloud compute instances set-service-account VM --zone ZONE --service-account "$SA" --scopes cloud-platform
gcloud compute instances start VM --zone ZONE
```

Then add this line to the `[Service]` section of the systemd unit, restart the bot, and delete the `.env` from the VM (`shred -u .env`):

```ini
Environment=COMBOIO_SECRET=projects/PROJECT/secrets/comboio-env
```

To change a setting later, add a new version (`gcloud secrets versions add comboio-env --data-file=.env`) and restart the bot. Consider destroying old versions (`gcloud secrets versions destroy N --secret comboio-env`): Secret Manager bills per active version beyond the free tier.

---

## 🚀 Local Installation

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd tickets
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate    # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the bot:**
   ```bash
   python discord-bot.py
   ```

---

## 🛡️ Running as a Background Service (systemd)

On Linux VMs (Debian, Ubuntu), configure `systemd` so the bot runs continuously in the background and **automatically starts on boot or restarts after crashes**.

### 1. Create the Service File
```bash
sudo nano /etc/systemd/system/discord-bot.service
```

Paste the following configuration (adjust the user and paths if different):

```ini
[Unit]
Description=Discord CP Ticket Automation Bot
After=network.target

[Service]
Type=simple
User=ladol2003
WorkingDirectory=/home/ladol2003/discord-bot/tickets
ExecStart=/home/ladol2003/discord-bot/tickets/venv/bin/python3 discord-bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# Optional: load settings from Secret Manager instead of .env
# Environment=COMBOIO_SECRET=projects/PROJECT/secrets/comboio-env

[Install]
WantedBy=multi-user.target
```

> **Note**: `Restart=always` and `RestartSec=5` ensure the service automatically restarts within 5 seconds if the Python process crashes or is killed.

### 2. Register & Enable the Service
```bash
# Reload systemd to detect the new service file
sudo systemctl daemon-reload

# Enable the bot to start automatically on VM boot
sudo systemctl enable discord-bot

# Start the bot immediately
sudo systemctl start discord-bot
```

---

## 🕹️ Service Management

Use these standard commands to manage and monitor the bot:

| Action | Command |
| :--- | :--- |
| **Check Status** | `sudo systemctl status discord-bot` |
| **View Live Logs** | `sudo journalctl -u discord-bot -f` |
| **Restart Bot** | `sudo systemctl restart discord-bot` |
| **Stop Bot** | `sudo systemctl stop discord-bot` |
| **Start Bot** | `sudo systemctl start discord-bot` |

To exit the log view (`journalctl`), press `Ctrl + C`.

---

## 🔒 Crash Resilience & Reliability

### Is it safe from crashes?
* **Google Cloud Compute Engine Uptime**: Google Cloud VM instances offer 99.95%+ availability. For a lightweight bot (<100MB RAM, negligible CPU), VM-level crashes are rare.
* **Automatic Process Restarts**: The `systemd` supervisor monitors the process 24/7. If an unhandled exception or memory issue ever terminates Python, `systemd` restarts it automatically within 5 seconds.
* **Automatic Reboot Recovery**: If the entire VM reboots (e.g. OS updates), `systemd` automatically launches the bot as soon as the network is ready.
* **Persistent Scheduled Purchases**: Bookings scheduled with APScheduler are saved to disk in `jobs.sqlite`. When the bot restarts, it reloads all scheduled jobs from the database, ensuring no bookings are lost.
* **Late Triggers Still Run**: A trigger missed by up to 10 minutes (e.g. the bot was restarting) still runs instead of being silently skipped.
* **Don't Restart Mid-Booking**: A booking that has already fired lives in memory only. Restarting the bot between the seat lock and checkout loses it, so check `!list` for bookings in progress before restarting or updating.
