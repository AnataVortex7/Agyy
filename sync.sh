# जर RCLONE_CONFIG_BASE64 रिकामे असेल तर बॅकअप पूर्ण बायपास करा
if [ -z "$RCLONE_CONFIG_BASE64" ]; then
    echo "No rclone config found. Skipping backup and drive sync entirely."
    exit 0
fi

#!/bin/bash
set -e

mkdir -p /root/.config/rclone

if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    echo "Creating rclone config from base64..."
    echo "$RCLONE_CONFIG_BASE64" | base64 -d > /root/.config/rclone/rclone.conf
fi

REMOTE_NAME="gdrive"
echo "Using remote name: ${REMOTE_NAME}"

pull_data() {
    echo "Pulling data from Google Drive..."
    rclone copy "${REMOTE_NAME}:workspace" /root/workspace || true
    rclone copy "${REMOTE_NAME}:.gemini" /root/.gemini || true
    rclone copy "${REMOTE_NAME}:blocklist.json" /root/blocklist.json || true
    echo "Pull complete."
}

push_data() {
    echo "Pushing data to Google Drive..."
    rclone sync -L /root/workspace "${REMOTE_NAME}:workspace" --exclude "project_code/**" || true
    rclone sync -L /root/.gemini "${REMOTE_NAME}:.gemini" || true
    if [ -f /root/blocklist.json ]; then
        rclone copy /root/blocklist.json "${REMOTE_NAME}:blocklist.json" || true
    fi
    echo "Push complete."
}

if [ "$1" = "pull" ]; then
    pull_data
elif [ "$1" = "push" ]; then
    push_data
elif [ "$1" = "loop" ]; then
    echo "Starting background sync loop..."
    while true; do
        push_data
        sleep 60
    done
fi
