#!/usr/bin/env python3
"""
Periodic backup/restore of persistent state to a Telegram group.

Koyeb's free tier has no persistent disk - everything in the container
(workspace files, agy's login/session, this bot's IP-blocklist) is wiped
on every redeploy/restart. This script uses a Telegram group as free
"storage": it tars up all state, uploads it as a document, and pins that
message so the latest backup can always be found again (bots can't list
chat history, but they CAN read the currently pinned message via getChat).

Setup:
  1. Create a Telegram group (or reuse one) and add this bot to it.
  2. Promote the bot to admin with "Pin messages" permission - required,
     otherwise backups upload fine but can't be found again after restart.
  3. Send any message in the group, then read its chat id (a negative
     number) - easiest way: add @userinfobot to the same group briefly,
     or call https://api.telegram.org/bot<TOKEN>/getUpdates after
     posting in the group and look for "chat":{"id": ...}.
  4. Set Koyeb secrets:
       TELEGRAM_BACKUP_GROUP_ID   - the group's chat id (e.g. -1001234567890)
       BACKUP_INTERVAL_MINUTES    - optional, default 20

Usage:
  python3 backup.py restore   # one-shot, run at container startup
  python3 backup.py loop      # runs forever, backs up every N minutes
  python3 backup.py once      # one-shot manual backup
"""
import io
import json
import os
import sys
import tarfile
import time
import urllib.request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GROUP_ID = os.environ.get("TELEGRAM_BACKUP_GROUP_ID")
INTERVAL_MINUTES = float(os.environ.get("BACKUP_INTERVAL_MINUTES", "20"))
NOTIFY_USER_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

ROOT = "/root"
WORKSPACE = os.path.join(ROOT, "workspace")
ARCHIVE_NAME = "agyy_backup.tar.gz"

# Files/dirs directly under /root that are skipped even though this
# script otherwise grabs every dotfile/dotdir - keeps ephemeral state
# out of the backup.
SKIP_NAMES = {"oauth_data.json", ".bash_logout"}


def log(msg):
    print(f"[backup] {msg}", flush=True)


def api_call(method, params=None, timeout=60):
    url = f"{API}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def notify(text):
    if not NOTIFY_USER_ID:
        return
    try:
        api_call("sendMessage", {"chat_id": int(NOTIFY_USER_ID), "text": text})
    except Exception as e:
        log(f"notify failed: {e}")


def build_archive():
    """Tar up the workspace plus every dotfile/dotdir directly under
    /root - this catches agy's login/session state and this app's own
    state (auth_blocklist.json) whatever it happens to be named,
    without ever touching the app's own .py/.sh scripts (so restoring
    an old backup can never clobber newer code)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if os.path.isdir(WORKSPACE):
            tar.add(WORKSPACE, arcname="workspace")
        for name in sorted(os.listdir(ROOT)):
            if name == "workspace" or name in SKIP_NAMES:
                continue
            if name.startswith(".") or name == "auth_blocklist.json":
                tar.add(os.path.join(ROOT, name), arcname=name)
    buf.seek(0)
    return buf.read()


def send_backup():
    if not (BOT_TOKEN and GROUP_ID):
        log("TELEGRAM_BACKUP_GROUP_ID or TELEGRAM_BOT_TOKEN not set - skipping backup.")
        return False

    data = build_archive()
    size_mb = len(data) / (1024 * 1024)
    if size_mb > 45:
        log(f"backup is {size_mb:.1f}MB - too close to Telegram's 50MB bot upload limit, skipping.")
        notify(f"Backup skipped: archive is {size_mb:.1f}MB, over the safe size limit.")
        return False

    boundary = "----agyybackupboundary"
    body = []
    body.append(f"--{boundary}\r\n".encode())
    body.append(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{GROUP_ID}\r\n'.encode())
    body.append(f"--{boundary}\r\n".encode())
    caption = f'agyy backup - {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}'
    body.append(f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode())
    body.append(f"--{boundary}\r\n".encode())
    body.append(f'Content-Disposition: form-data; name="document"; filename="{ARCHIVE_NAME}"\r\n'.encode())
    body.append(b"Content-Type: application/gzip\r\n\r\n")
    body.append(data)
    body.append(f"\r\n--{boundary}--\r\n".encode())
    payload = b"".join(body)

    req = urllib.request.Request(f"{API}/sendDocument", data=payload, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())

    if not result.get("ok"):
        log(f"sendDocument failed: {result}")
        return False

    message_id = result["result"]["message_id"]
    try:
        api_call("pinChatMessage", {
            "chat_id": GROUP_ID, "message_id": message_id, "disable_notification": True,
        })
    except Exception as e:
        log(f"pinChatMessage failed (bot needs 'Pin messages' admin right in the group): {e}")
        notify(
            "Backup uploaded but could NOT be pinned - make the bot an admin "
            "with 'Pin messages' in the backup group, or restore will fail "
            "after the next redeploy."
        )

    log(f"backup uploaded ({size_mb:.2f}MB).")
    return True


def restore_backup():
    if not (BOT_TOKEN and GROUP_ID):
        log("TELEGRAM_BACKUP_GROUP_ID or TELEGRAM_BOT_TOKEN not set - skipping restore.")
        return False

    try:
        chat = api_call("getChat", {"chat_id": GROUP_ID})
    except Exception as e:
        log(f"getChat failed: {e}")
        return False

    pinned = chat.get("result", {}).get("pinned_message")
    if not pinned or "document" not in pinned:
        log("no pinned backup found in the group - starting fresh.")
        return False

    file_id = pinned["document"]["file_id"]
    try:
        info = api_call("getFile", {"file_id": file_id})
        file_path = info["result"]["file_path"]
        with urllib.request.urlopen(f"{FILE_API}/{file_path}", timeout=120) as resp:
            data = resp.read()
    except Exception as e:
        log(f"downloading backup failed: {e}")
        return False

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(ROOT, filter="data")
    except Exception as e:
        log(f"extracting backup failed: {e}")
        return False

    log("restore complete.")
    notify("Restored your previous session (workspace + agy login) from the Telegram backup.")
    return True


def loop():
    log(f"backup loop starting - every {INTERVAL_MINUTES} minutes.")
    while True:
        time.sleep(INTERVAL_MINUTES * 60)
        try:
            send_backup()
        except Exception as e:
            log(f"backup failed: {e}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if action == "restore":
        restore_backup()
    elif action == "once":
        send_backup()
    else:
        loop()
