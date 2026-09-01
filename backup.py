#!/usr/bin/env python3
"""
Change-triggered backup/restore of persistent state to a Telegram group.

Koyeb's free tier has no persistent disk - everything in the container
(workspace files, agy's login/session, this bot's IP-blocklist) is wiped
on every redeploy/restart. This script uses a Telegram group as free
"storage": it tars up all state, uploads it as a document, and pins that
message so the latest backup can always be found again (bots can't list
chat history, but they CAN read the currently pinned message via getChat).

Backups are NOT on a fixed timer. A lightweight watcher polls file
mtimes/sizes every few seconds and fires a backup shortly after it sees
things settle (workspace file added/changed/removed, agy login/session
file written, etc.) - so a redeploy can only ever lose a few seconds of
work, not up to a whole timer window. A long MAX_BACKUP_INTERVAL_MINUTES
still runs as a safety net in case a change is ever somehow missed.

Setup:
  1. Create a Telegram group (or reuse one) and add this bot to it.
  2. Promote the bot to admin with "Pin messages" permission - required,
     otherwise backups upload fine but can't be found again after restart.
  3. Send any message in the group, then read its chat id (a negative
     number) - easiest way: add @userinfobot to the same group briefly,
     or call https://api.telegram.org/bot<TOKEN>/getUpdates after
     posting in the group and look for "chat":{"id": ...}.
  4. Set Koyeb secrets:
       TELEGRAM_BACKUP_GROUP_ID       - the group's chat id (e.g. -1001234567890)
       BACKUP_POLL_SECONDS            - optional, default 5
       BACKUP_DEBOUNCE_SECONDS        - optional, default 8
       MAX_BACKUP_INTERVAL_MINUTES    - optional safety-net ceiling, default 30

  On startup, and every time it connects, the bot posts a "connected"
  message into the backup group so you can always tell which group is
  wired up.

Usage:
  python3 backup.py restore   # one-shot, run at container startup
  python3 backup.py watch     # runs forever, backs up on change (debounced)
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
POLL_SECONDS = float(os.environ.get("BACKUP_POLL_SECONDS", "5"))
DEBOUNCE_SECONDS = float(os.environ.get("BACKUP_DEBOUNCE_SECONDS", "8"))
MAX_INTERVAL_MINUTES = float(os.environ.get("MAX_BACKUP_INTERVAL_MINUTES", "30"))
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


_warned = set()


def notify_once(key, text):
    """Send a Telegram warning at most once per run for a given issue -
    everything else (including repeats of the same issue) just goes to
    the log, so the bot chat doesn't get flooded with repeated
    background-backup errors."""
    if key in _warned:
        log(text)
        return
    _warned.add(key)
    log(text)
    notify(text)


def announce_connected():
    """Posts a message into the backup group so it's always obvious which
    group is wired up. Runs on every startup (called from restore_backup)."""
    if not (BOT_TOKEN and GROUP_ID):
        return
    try:
        api_call("sendMessage", {
            "chat_id": GROUP_ID,
            "text": (
                "\U0001F517 Agyy connected to this group for backups. "
                "Workspace + agy login/config will be backed up here "
                "automatically whenever something changes."
            ),
        })
    except Exception as e:
        log(f"could not announce connection in group (wrong chat id, or bot not a member?): {e}")


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
        notify_once(
            "size",
            f"Backup skipped: archive is {size_mb:.1f}MB, over the safe size limit. "
            "(This won't be repeated in chat - check logs for further occurrences.)",
        )
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
        notify_once(
            "pin",
            "Backup uploaded but could NOT be pinned - make the bot an admin "
            "with 'Pin messages' in the backup group, or restore will fail "
            f"after the next redeploy. ({e})",
        )

    log(f"backup uploaded ({size_mb:.2f}MB).")
    return True


def restore_backup():
    if not (BOT_TOKEN and GROUP_ID):
        log("TELEGRAM_BACKUP_GROUP_ID or TELEGRAM_BOT_TOKEN not set - skipping restore.")
        return False

    announce_connected()

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


def snapshot():
    """Lightweight signature (path -> mtime, size) of everything
    build_archive() would tar up. Cheap enough to poll every few
    seconds; any add/edit/delete anywhere in it changes the signature -
    including agy writing its login/session files the moment a login
    completes, so login is backed up automatically too, not just files.
    Directories are recorded too (not just files inside them) so an
    empty new folder still counts as a change."""
    sig = {}

    def add_dir(path):
        for dirpath, _dirs, files in os.walk(path):
            try:
                st = os.stat(dirpath)
                sig[dirpath] = ("dir", st.st_mtime_ns)
            except OSError:
                pass
            for f in files:
                fp = os.path.join(dirpath, f)
                try:
                    st = os.stat(fp)
                    sig[fp] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass

    if os.path.isdir(WORKSPACE):
        add_dir(WORKSPACE)
    try:
        names = sorted(os.listdir(ROOT))
    except OSError:
        names = []
    for name in names:
        if name == "workspace" or name in SKIP_NAMES:
            continue
        if name.startswith(".") or name == "auth_blocklist.json":
            path = os.path.join(ROOT, name)
            if os.path.isdir(path):
                add_dir(path)
            else:
                try:
                    st = os.stat(path)
                    sig[path] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass
    return sig


def watch():
    """Backs up as soon as something changes (debounced so a burst of
    writes triggers one backup, not dozens), instead of on a fixed
    timer - so a redeploy can only ever lose the last few seconds of
    work. MAX_INTERVAL_MINUTES is just a safety net in case a change is
    ever somehow missed by the poll."""
    log(
        f"watching for changes every {POLL_SECONDS}s, backing up "
        f"{DEBOUNCE_SECONDS}s after things settle "
        f"(safety-net backup at least every {MAX_INTERVAL_MINUTES} min)."
    )
    if not (BOT_TOKEN and GROUP_ID):
        log("TELEGRAM_BACKUP_GROUP_ID or TELEGRAM_BOT_TOKEN not set - watcher has nothing to do.")
        return
    notify(
        f"\U0001F440 Backup watcher is live - checking every {POLL_SECONDS}s, "
        f"backs up ~{DEBOUNCE_SECONDS}s after a change settles."
    )
    last_backed_up_sig = snapshot()
    last_seen_sig = last_backed_up_sig
    last_change_time = None
    last_backup_time = time.time()

    while True:
        time.sleep(POLL_SECONDS)
        try:
            sig = snapshot()
        except Exception as e:
            log(f"snapshot failed: {e}")
            continue

        if sig != last_seen_sig:
            last_seen_sig = sig
            last_change_time = time.time()

        pending_change = last_seen_sig != last_backed_up_sig
        settled = last_change_time is not None and (time.time() - last_change_time) >= DEBOUNCE_SECONDS
        overdue = (time.time() - last_backup_time) >= MAX_INTERVAL_MINUTES * 60

        if (pending_change and settled) or (pending_change and overdue):
            log(f"change detected ({len(last_seen_sig)} tracked paths) - backing up...")
            try:
                if send_backup():
                    last_backed_up_sig = last_seen_sig
                    last_backup_time = time.time()
            except Exception as e:
                log(f"backup failed: {e}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if action == "restore":
        restore_backup()
    elif action == "once":
        send_backup()
    else:
        watch()
