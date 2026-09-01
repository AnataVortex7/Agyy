FROM ubuntu:24.04

# Basic tools
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    bash \
    git \
    tmux \
    ttyd \
    python3 \
    nginx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /root

# Install Antigravity CLI (agy)
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"

# Copy entrypoint + auth gate + copy-link feature
COPY entrypoint.sh /root/entrypoint.sh
COPY auth_gate.py /root/auth_gate.py
COPY nginx.conf /root/nginx.conf
COPY url_watcher.py /root/url_watcher.py
COPY oauth_link.html /root/oauth_link.html
COPY telegram_bot.py /root/telegram_bot.py
COPY backup.py /root/backup.py
RUN chmod +x /root/entrypoint.sh /root/auth_gate.py /root/url_watcher.py /root/telegram_bot.py /root/backup.py

# NOTE: Koyeb's free tier has no persistent volume, so the container's
# local filesystem is wiped on every redeploy/restart. To survive that,
# set TELEGRAM_BACKUP_GROUP_ID (see backup.py) - workspace files + agy's
# login/config get backed up to that Telegram group automatically and
# restored on startup, so you won't need to log in to Google again after
# a redeploy.
EXPOSE 8000
CMD ["/root/entrypoint.sh"]
