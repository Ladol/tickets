# 🚆 CP Train Ticket Discord Bot

An automated Discord bot designed for **Comboios de Portugal (CP)** that books Intercidades (IC) train tickets right when reservations unlock (24 hours before train departure, e.g., for the *Passe Ferroviário Verde*).

The bot handles station searches, automated Keycloak login, seat selection optimization, and resilient scheduling backed by a persistent SQLite database.

---

## 📋 Table of Contents
- [Features](#-features)
- [Bot Commands](#-bot-commands)
- [Configuration (.env)](#-configuration-env)
- [Local Installation](#-local-installation)
- [Running as a Background Service (systemd)](#-running-as-a-background-service-systemd)
  - [Service Management (Start, Logs, Stop, Restart)](#-service-management)
- [Crash Resilience & Reliability](#-crash-resilience--reliability)

---

## ✨ Features
* **Automated 24h Booking**: Locks seats 5 minutes before the 24-hour mark, holds the seat, waits until the exact second of unlock, and completes checkout.
* **Seat Preference Engine**: Automatically inspects the train carriage map and upgrades to preferred window/table seats.
* **Live Discord Feedback**: Streams real-time updates directly to Discord (seat locked, countdown timer, discount applied, final PDF link).
* **Crash-Proof Persistence**: Scheduled jobs are saved in a local SQLite database (`jobs.sqlite`) so no scheduled booking is lost if the bot or VM restarts.

---

## 🤖 Bot Commands

| Command | Description | Example |
| :--- | :--- | :--- |
| `!ticket [YYYY-MM-DD]` | Starts the interactive route search and booking wizard. | `!ticket 2026-09-04` |
| `!list` | Lists all upcoming scheduled bookings with dates, times, and routes. | `!list` |
| `!cancel` | Opens an interactive dropdown to cancel any scheduled booking. | `!cancel` |
| `!ping` | Health check displaying bot latency and count of active scheduled jobs. | `!ping` |

---

## ⚙️ Configuration (.env)

Create a `.env` file in the root directory:

```env
# Discord Configuration
DISCORD_BOT_TOKEN="your_bot_token_here"
DISCORD_USER_PING="<@your_discord_user_id>"
DISCORD_CHANNEL_ID="optional_default_channel_id"

# CP (Comboios de Portugal) Credentials
CP_EMAIL="your_cp_email@example.com"
CP_PASSWORD="your_cp_password"
CP_NAME="Your Full Name"
CP_MOBILE="912345678"
CP_PASSENGER_ID="12345678"          # Citizen Card (Cartão de Cidadão)
CP_GREEN_PASS="12345678901"         # Green Rail Pass Number (Passe Ferroviário Verde)
```

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
