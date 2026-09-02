#!/bin/bash
set -e

# Setup rclone config using Environment Variable
mkdir -p /root/.config/rclone
if [ -n "$RCLONE_CONF_CONTENT" ]; then
    echo "Creating rclone config from environment variable..."
    echo "$RCLONE_CONF_CONTENT" > /root/.config/rclone/rclone.conf
else
    echo "ERROR: RCLONE_CONF_CONTENT environment variable is not set."
    echo "Sync will not work without Google Drive config."
fi

REMOTE_NAME="gdrive"
if [ -f /root/.config/rclone/rclone.conf ]; then
    EXTRACTED_NAME=$(grep -o '^\[.*\]$' /root/.config/rclone/rclone.conf | head -n 1 | tr -d '[]')
    if [ -n "$EXTRACTED_NAME" ]; then
        REMOTE_NAME="$EXTRACTED_NAME"
    fi
fi
echo "Using remote name: ${REMOTE_NAME}"

# Function to pull data from Drive
pull_data() {
    echo "Pulling data from Google Drive..."
    rclone copy ${REMOTE_NAME}:workspace /root/workspace || true
    rclone copy ${REMOTE_NAME}:.gemini /root/.gemini || true
    rclone copy ${REMOTE_NAME}:blocklist.json /root/blocklist.json || true
    echo "Pull complete."
}

# Function to push data to Drive
push_data() {
    rclone sync /root/workspace ${REMOTE_NAME}:workspace --exclude "project_code/**" || true
    rclone sync /root/.gemini ${REMOTE_NAME}:.gemini || true
    rclone copy /root/blocklist.json ${REMOTE_NAME}:blocklist.json || true
}

if [ "$1" == "pull" ]; then
    pull_data
elif [ "$1" == "loop" ]; then
    echo "Starting background sync loop..."
    while true; do
        sleep 60
        push_data
    done
fi
