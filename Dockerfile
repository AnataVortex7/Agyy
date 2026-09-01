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
RUN chmod +x /root/entrypoint.sh /root/auth_gate.py /root/url_watcher.py

# NOTE: No persistent volume on Koyeb's free tier. All state (agy login,
# workspace files) lives in the container's local filesystem and is LOST
# on every redeploy/restart. You'll need to log in to Google again each
# time the Service restarts.
EXPOSE 8000
CMD ["/root/entrypoint.sh"]
