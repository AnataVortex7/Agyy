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

# Function to attempt mounting (Primary Disk approach)
mount_data() {
    echo "Attempting to mount Google Drive as primary disk..."
    mkdir -p /root/gdrive
    
    # Attempt to mount the root of our GDrive folder to /root/gdrive
    if rclone mount gdrive: /root/gdrive --vfs-cache-mode writes --allow-other --daemon; then
        echo "Mount successful! Using Google Drive as primary disk."
        
        # Replace local directories with symlinks to the mount
        rm -rf /root/workspace
        mkdir -p /root/gdrive/workspace
        ln -s /root/gdrive/workspace /root/workspace
        
        rm -rf /root/.gemini
        mkdir -p /root/gdrive/.gemini
        ln -s /root/gdrive/.gemini /root/.gemini
        
        touch /root/gdrive/blocklist.json
        rm -f /root/blocklist.json
        ln -s /root/gdrive/blocklist.json /root/blocklist.json
        
        return 0
    else
        echo "Mount failed (likely FUSE is not allowed on this container). Falling back to sync method."
        return 1
    fi
}

# Function to pull data from Drive (Fallback)
pull_data() {
    echo "Pulling data from Google Drive (Fallback mode)..."
    rclone copy gdrive:workspace /root/workspace || true
    rclone copy gdrive:.gemini /root/.gemini || true
    rclone copy gdrive:blocklist.json /root/blocklist.json || true
    echo "Pull complete."
}

# Function to push data to Drive (Fallback)
push_data() {
    rclone sync /root/workspace gdrive:workspace --exclude "project_code/**" || true
    rclone sync /root/.gemini gdrive:.gemini || true
    rclone copy /root/blocklist.json gdrive:blocklist.json || true
}

if [ "$1" == "pull" ]; then
    # In pull step, we attempt mount first. If it succeeds, we write a flag file.
    if mount_data; then
        touch /root/.mount_success
    else
        # Fallback to pull
        pull_data
    fi
elif [ "$1" == "loop" ]; then
    # If mount succeeded, we don't need to do background sync!
    if [ -f /root/.mount_success ]; then
        echo "Google Drive is mounted. No background sync needed."
        # Just sleep forever to keep process alive if needed, or exit
        while true; do sleep 3600; done
    else
        echo "Starting background sync loop (Fallback mode)..."
        while true; do
            sleep 60
            push_data
        done
    fi
fi
