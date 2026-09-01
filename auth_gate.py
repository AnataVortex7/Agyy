#!/usr/bin/env python3
"""
Auth gate for the Antigravity agent terminal.
- Reads PASSWORD from environment (Koyeb env var, plain text).
- Prompts for password (max 3 tries).
- On 3 failures, blocks the client IP for 24 hours (state kept in the
  container's local filesystem).

NOTE (Koyeb free tier): there is no persistent volume, so the blocklist
and any agy login/session state are LOST whenever the Service restarts
or redeploys. This is a known limitation of the free tier.
"""
import json
import os
import sys
import time
import subprocess

DATA_DIR = "/root"
BLOCKLIST_FILE = os.path.join(DATA_DIR, "auth_blocklist.json")
BLOCK_HOURS = 24
MAX_TRIES = 3

def load_blocklist():
    if not os.path.exists(BLOCKLIST_FILE):
        return {}
    try:
        with open(BLOCKLIST_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_blocklist(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BLOCKLIST_FILE, "w") as f:
        json.dump(data, f)

def get_client_ip():
    # ttyd forwards these when behind HF's proxy; fall back to "global"
    return (
        os.environ.get("REMOTE_ADDR")
        or os.environ.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or "global"
    )

def is_blocked(blocklist, ip):
    entry = blocklist.get(ip)
    if not entry:
        return False
    blocked_until = entry.get("blocked_until", 0)
    if time.time() < blocked_until:
        return True
    # expired, clean up
    del blocklist[ip]
    save_blocklist(blocklist)
    return False

def record_failure(blocklist, ip):
    entry = blocklist.get(ip, {"fails": 0, "blocked_until": 0})
    entry["fails"] += 1
    if entry["fails"] >= MAX_TRIES:
        entry["blocked_until"] = time.time() + BLOCK_HOURS * 3600
        entry["fails"] = 0
    blocklist[ip] = entry
    save_blocklist(blocklist)
    return entry

def clear_failures(blocklist, ip):
    if ip in blocklist:
        del blocklist[ip]
        save_blocklist(blocklist)

def verify_password(candidate, stored_password):
    return candidate == stored_password

def main():
    stored_password = os.environ.get("PASSWORD")
    if not stored_password:
        print("ERROR: PASSWORD secret is not set on this Space.")
        print("Go to Space Settings -> Repository secrets -> Add secret named PASSWORD.")
        sys.exit(1)

    ip = get_client_ip()
    blocklist = load_blocklist()

    if is_blocked(blocklist, ip):
        entry = blocklist[ip]
        remaining = int(entry["blocked_until"] - time.time())
        hrs = remaining // 3600
        mins = (remaining % 3600) // 60
        print(f"This IP is temporarily blocked due to failed login attempts.")
        print(f"Try again in {hrs}h {mins}m.")
        sys.exit(1)

    tries_left = MAX_TRIES
    while tries_left > 0:
        try:
            candidate = input(f"Enter password ({tries_left} tries left): ")
        except EOFError:
            sys.exit(1)

        if verify_password(candidate, stored_password):
            clear_failures(blocklist, ip)
            print("Access granted.\n")
            break

        tries_left -= 1
        entry = record_failure(load_blocklist(), ip)
        blocklist = load_blocklist()
        if is_blocked(blocklist, ip):
            print("Too many failed attempts. This IP is now blocked for 24 hours.")
            sys.exit(1)
        elif tries_left > 0:
            print("Wrong password.")
    else:
        sys.exit(1)

    # Success: hand off to the real session (tmux + agy).
    # -A = attach if the session already exists, else create it.
    workspace = "/root/workspace"
    os.makedirs(workspace, exist_ok=True)
    os.chdir(workspace)
    subprocess.run(["tmux", "new-session", "-A", "-s", "agent", "agy"])

if __name__ == "__main__":
    main()
