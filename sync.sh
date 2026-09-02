#!/bin/bash
set -e

# Setup rclone config & Service Account
mkdir -p /root/.config/rclone

if [ -n "$SA_KEY_BASE64" ]; then
    echo "Creating service account file from base64..."
    echo "$SA_KEY_BASE64" | base64 -d > /root/sa.json
fi

ROOT_FOLDER_ID="${GDRIVE_ROOT_ID:-1US7h00XWr9FDT_5i7U0BpNsRtPa0DgMM}"
REMOTE_NAME="gdrive"

cat << EOF > /root/.config/rclone/rclone.conf
[${REMOTE_NAME}]
type = drive
scope = drive
root_folder_id = ${ROOT_FOLDER_ID}
service_account_file = /root/sa.json
EOF

echo "Using remote name: ${REMOTE_NAME}"

# Function to pull data from Drive
pull_data() {
    echo "Pulling data from Google Drive..."
    rclone copy "${REMOTE_NAME}:workspace" /root/workspace || true
    rclone copy "${REMOTE_NAME}:.gemini" /root/.gemini || true
    # blocklist नसेल तरी क्रॅश होऊ नये म्हणून:
    rclone copy "${REMOTE_NAME}:blocklist.json" /root/blocklist.json || true
    echo "Pull complete."
}

# Function to push data to Drive
push_data() {
    echo "Pushing data to Google Drive..."
    rclone copy /root/workspace "${REMOTE_NAME}:workspace" --exclude "project_code/**" || true
    rclone copy /root/.gemini "${REMOTE_NAME}:.gemini" || true
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
