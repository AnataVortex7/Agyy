#!/usr/bin/env python3
"""
Telegram remote-control bot for this container - button-driven version.

How it works:
  - /start shows a menu: buttons for each subfolder (tap to enter) and
    each file (tap to download it back to you), plus action buttons.
  - Tap "New folder", then type a name -> folder is created and entered.
  - Once inside a folder, just type a normal message (no "/") and it
    runs as a shell command right there (e.g. type "agy status").
    The command's output is sent back, followed by the menu again.
  - Send any file/photo -> saved straight into the currently selected
    folder.

Only replies to TELEGRAM_ALLOWED_USER_ID; everyone else is ignored.
Standard library only (urllib) - no extra pip/apt packages needed.

Required env vars (Koyeb secrets, same as PASSWORD):
  TELEGRAM_BOT_TOKEN       - from @BotFather
  TELEGRAM_ALLOWED_USER_ID - your numeric id, from @userinfobot
"""
import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
import mimetypes

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
WORKSPACE = "/root/workspace"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

cwd_by_chat = {}          # chat_id -> relative path under WORKSPACE
awaiting_name = {}        # chat_id -> True while we're waiting for a typed folder name

# ---------- low-level Telegram API helpers ----------

def api_call(method, params, timeout=60):
    url = f"{API}/{method}"
    body = json.dumps(params).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def send_message(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text or "(no output)"}
    if keyboard:
        params["reply_markup"] = keyboard
    if len(params["text"]) > 4000:
        full = params["text"]
        for i in range(0, len(full), 4000):
            chunk_params = {"chat_id": chat_id, "text": full[i:i + 4000]}
            if keyboard and i + 4000 >= len(full):
                chunk_params["reply_markup"] = keyboard
            api_call("sendMessage", chunk_params)
    else:
        api_call("sendMessage", params)

def answer_callback(callback_id, text=None):
    params = {"callback_query_id": callback_id}
    if text:
        params["text"] = text
    api_call("answerCallbackQuery", params)

def send_document(chat_id, filepath):
    url = f"{API}/sendDocument"
    boundary = "----claudebotboundary"
    filename = os.path.basename(filepath)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(filepath, "rb") as f:
        file_bytes = f.read()

    body = []
    body.append(f"--{boundary}\r\n".encode())
    body.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    body.append(f"{chat_id}\r\n".encode())
    body.append(f"--{boundary}\r\n".encode())
    body.append(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode())
    body.append(f"Content-Type: {mime}\r\n\r\n".encode())
    body.append(file_bytes)
    body.append(f"\r\n--{boundary}--\r\n".encode())
    payload = b"".join(body)

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())

# ---------- folder helpers ----------

def get_cwd_path(chat_id):
    rel = cwd_by_chat.get(chat_id, "")
    path = os.path.normpath(os.path.join(WORKSPACE, rel))
    if not path.startswith(WORKSPACE):
        path = WORKSPACE
        cwd_by_chat[chat_id] = ""
    os.makedirs(path, exist_ok=True)
    return path

def set_cwd_rel(chat_id, new_rel):
    path = os.path.normpath(os.path.join(WORKSPACE, new_rel))
    if not path.startswith(WORKSPACE):
        path = WORKSPACE
    rel = os.path.relpath(path, WORKSPACE)
    cwd_by_chat[chat_id] = "" if rel == "." else rel

def build_menu(chat_id):
    cwd = get_cwd_path(chat_id)
    rel = cwd_by_chat.get(chat_id, "")
    label = "workspace/" if not rel else f"workspace/{rel}/"

    items = sorted(os.listdir(cwd))
    dirs = [i for i in items if os.path.isdir(os.path.join(cwd, i))]
    files = [i for i in items if not os.path.isdir(os.path.join(cwd, i))]

    rows = []
    for d in dirs:
        target = d if not rel else f"{rel}/{d}"
        rows.append([{"text": f"\U0001F4C1 {d}", "callback_data": f"cd:{target}"}])
    for fname in files:
        target = fname if not rel else f"{rel}/{fname}"
        rows.append([{"text": f"\U0001F4C4 {fname}", "callback_data": f"get:{target}"}])

    action_row = []
    if rel:
        action_row.append({"text": "\u2B06\uFE0F Up", "callback_data": "up"})
    action_row.append({"text": "\u2795 New folder", "callback_data": "newfolder"})
    action_row.append({"text": "\U0001F504 Refresh", "callback_data": "refresh"})
    rows.append(action_row)

    text = f"{label}\n\nTap a folder to enter it, a file to download it.\nJust type a message to run it as a command here."
    return text, {"inline_keyboard": rows}

def show_menu(chat_id):
    text, keyboard = build_menu(chat_id)
    send_message(chat_id, text, keyboard)

# ---------- action handlers ----------

def handle_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    data = callback["data"]
    answer_callback(callback["id"])

    if data == "up":
        rel = cwd_by_chat.get(chat_id, "")
        parent = os.path.dirname(rel)
        set_cwd_rel(chat_id, parent)
        show_menu(chat_id)

    elif data == "refresh":
        show_menu(chat_id)

    elif data == "newfolder":
        awaiting_name[chat_id] = True
        send_message(chat_id, "Send the new folder's name as a message.")

    elif data.startswith("cd:"):
        set_cwd_rel(chat_id, data[3:])
        show_menu(chat_id)

    elif data.startswith("get:"):
        filepath = os.path.join(WORKSPACE, data[4:])
        if os.path.isfile(filepath):
            send_document(chat_id, filepath)
        else:
            send_message(chat_id, "File not found (maybe it moved).")

def run_command_here(chat_id, command):
    cwd = get_cwd_path(chat_id)
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=120,
        )
        out = (result.stdout or "") + (result.stderr or "")
        send_message(chat_id, out.strip() or f"(done, exit code {result.returncode})")
    except subprocess.TimeoutExpired:
        send_message(chat_id, "Command timed out after 120s.")
    show_menu(chat_id)

def handle_file(chat_id, file_id, filename):
    info = api_call("getFile", {"file_id": file_id})
    file_path = info["result"]["file_path"]
    cwd = get_cwd_path(chat_id)
    dest = os.path.join(cwd, filename)
    urllib.request.urlretrieve(f"{FILE_API}/{file_path}", dest)
    send_message(chat_id, f"Saved: {filename}")
    show_menu(chat_id)

def process_update(update):
    if "callback_query" in update:
        cb = update["callback_query"]
        user_id = str(cb.get("from", {}).get("id", ""))
        if ALLOWED_USER_ID and user_id != str(ALLOWED_USER_ID):
            return
        handle_callback(cb)
        return

    msg = update.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = str(msg.get("from", {}).get("id", ""))
    if ALLOWED_USER_ID and user_id != str(ALLOWED_USER_ID):
        return

    if "document" in msg:
        doc = msg["document"]
        handle_file(chat_id, doc["file_id"], doc.get("file_name", "file"))
        return
    if "photo" in msg:
        photo = msg["photo"][-1]
        handle_file(chat_id, photo["file_id"], f"photo_{photo['file_id']}.jpg")
        return

    text = msg.get("text", "")
    if not text:
        return

    if text in ("/start", "/help"):
        awaiting_name.pop(chat_id, None)
        show_menu(chat_id)
        return

    if awaiting_name.get(chat_id):
        awaiting_name.pop(chat_id, None)
        name = text.strip()
        cwd = get_cwd_path(chat_id)
        os.makedirs(os.path.join(cwd, name), exist_ok=True)
        rel = cwd_by_chat.get(chat_id, "")
        set_cwd_rel(chat_id, f"{rel}/{name}" if rel else name)
        show_menu(chat_id)
        return

    # any other plain text = run it as a command in the current folder
    run_command_here(chat_id, text)

def main():
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set - telegram bot disabled.")
        return
    if not ALLOWED_USER_ID:
        print("WARNING: TELEGRAM_ALLOWED_USER_ID not set - bot will ignore everyone.")

    offset = 0
    while True:
        try:
            updates = api_call("getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                try:
                    process_update(update)
                except Exception as e:
                    chat_id = (
                        update.get("message", {}).get("chat", {}).get("id")
                        or update.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                    )
                    if chat_id:
                        send_message(chat_id, f"Error: {e}")
        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    main()
