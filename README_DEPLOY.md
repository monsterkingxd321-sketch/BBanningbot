# Deployment Guide / Hosting Instructions

This repository contains all necessary deployment and hosting files to host the Telegram Mass Reporting Bot on **Railway**, **Render**, **Koyeb**, **Fly.io**, **Docker**, or **any Linux VPS**.

---

## 1. Deploy on Railway (Fastest & Recommended)

1. Push this project to your **GitHub** account.
2. Go to [railway.app](https://railway.app) and sign in.
3. Click **"New Project"** -> **"Deploy from GitHub repo"** and select your repository.
4. Go to the project **Settings** -> **Variables** and add these Environment Variables:
   * `API_ID` : `your_api_id`
   * `API_HASH` : `your_api_hash`
   * `BOT_TOKEN` : `your_bot_token` (from @BotFather)
   * `DEFAULT_ADMINS` : `5286579067,8568245247,8761433872` (your Telegram numeric user IDs)
   * `MONGO_URI` : `mongodb+srv://varof12117_db_user:z7YhGZeAbPogsF52@cluster0.hwnzc6j.mongodb.net/`
   * `MONGO_DB_NAME` : `banning_bot`
5. Railway will automatically detect the provided `railway.json` & `Dockerfile`, build the container, and start your bot!

---

## 2. Deploy on Any Linux VPS with Docker Compose

1. Clone or upload your code to your server:
   ```bash
   git clone <your-repo-url> /opt/banningbot
   cd /opt/banningbot
   ```
2. Create or verify `ben/.env`:
   ```bash
   cp .env.example ben/.env
   nano ben/.env
   ```
3. Start the bot in background:
   ```bash
   docker-compose up -d --build
   ```
4. View real-time logs:
   ```bash
   docker-compose logs -f
   ```

---

## 3. Deploy on Render / Koyeb

1. Create a **Background Worker** on Render or a **Standard App** on Koyeb.
2. Connect your GitHub repository.
3. Select **Docker** as the environment (it will automatically use the included `Dockerfile`).
4. Set the environment variables in the dashboard:
   * `API_ID`
   * `API_HASH`
   * `BOT_TOKEN`
   * `DEFAULT_ADMINS`
   * `MONGO_URI`
   * `MONGO_DB_NAME`
5. Click **Deploy**.

---

## 4. Deploy directly with Python (VPS without Docker)

```bash
cd ben
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -u main.py
```

### Run as a Systemd Background Service (Linux VPS)
Create `/etc/systemd/system/banningbot.service`:
```ini
[Unit]
Description=Telegram Banning Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/banningbot/ben
ExecStart=/opt/banningbot/ben/venv/bin/python -u main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable banningbot
sudo systemctl start banningbot
```

---

## Included Hosting Configuration Files
* `Dockerfile` (in root and in `ben/` for containerized hosting)
* `docker-compose.yml` (for 1-command VPS hosting with auto-restart)
* `railway.json` (for native Railway auto-detection and health checks)
* `Procfile` (for Heroku, Render, and Railway process definition)
* `requirements.txt` (clean dependencies including `telethon`, `pymongo`, `PySocks`, `cryptg`)
* `runtime.txt` (`python-3.11.9`)
* `.dockerignore` (prevents unnecessary files from bloating docker builds)
* `.env.example` (template with all required environment variables)
