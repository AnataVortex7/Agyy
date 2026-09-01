#!/bin/bash
set -e

mkdir -p /root/workspace
export HOME=/root
export PATH="/root/.local/bin:${PATH}"
cd /root/workspace

# 0) Restore workspace + agy login/config from the Telegram backup group
#    (if TELEGRAM_BACKUP_GROUP_ID is set) BEFORE anything else touches
#    the filesystem. Best-effort: if it fails or nothing's set yet, we
#    just start fresh. Runs synchronously so agy never sees a half-empty
#    workspace.
python3 /root/backup.py restore || true

# 1) nginx: multiplexes the single public port (8000) between the
#    terminal (ttyd, proxied) and the /oauth-link copy-page.
nginx -c /root/nginx.conf

# 2) Background watcher: tails the tmux pane and un-wraps any long
#    login/OAuth URL agy prints, writing it to /root/oauth_data.json
#    for the copy-page to pick up. No more broken/wrapped URLs.
python3 /root/url_watcher.py &

# 3) Telegram bot: lets you create folders, save/download files, and
#    run commands (including agy) from Telegram itself. Only replies
#    to TELEGRAM_ALLOWED_USER_ID. Does nothing if TELEGRAM_BOT_TOKEN
#    isn't set.
python3 /root/telegram_bot.py &

# 3b) Periodic backup loop: every BACKUP_INTERVAL_MINUTES (default 20),
#     re-uploads workspace + agy login/config to the Telegram backup
#     group so a redeploy never loses data. Does nothing if
#     TELEGRAM_BACKUP_GROUP_ID isn't set. Trigger one on-demand anytime
#     with /backup in Telegram.
python3 /root/backup.py loop &

# 4) ttyd now listens only on localhost:7681 (nginx forwards to it).
#    Every connection still runs auth_gate.py FIRST: it asks for the
#    password, blocks the IP for 24h after 3 wrong tries, and only on
#    success attaches/creates the tmux session running agy.
ttyd -p 7681 -i 127.0.0.1 -t rendererType=dom -W python3 /root/auth_gate.py
