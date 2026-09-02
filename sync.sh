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

# Function to pull data from Drive
pull_data() {
    echo "Pulling data from Google Drive..."
    rclone copy gdrive:workspace /root/workspace || true
    rclone copy gdrive:.gemini /root/.gemini || true
    rclone copy gdrive:blocklist.json /root/blocklist.json || true
    echo "Pull complete."
}

# Function to push data to Drive
push_data() {
    rclone sync /root/workspace gdrive:workspace --exclude "project_code/**" || true
    rclone sync /root/.gemini gdrive:.gemini || true
    rclone copy /root/blocklist.json gdrive:blocklist.json || true
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
