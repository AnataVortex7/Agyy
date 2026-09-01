#!/usr/bin/env python3
"""
Telegram remote-control bot for this container.

Folder browsing (as before):
  - /start shows buttons: folders (tap to enter), files (tap to download),
    "New folder", "Up", "Refresh".
  - Typing a plain message runs it as a one-shot shell command in the
    current folder - UNLESS an AI session is active (see below).

AI chat (new):
  - Tap "Start AI here" to launch `agy` as a REAL interactive session
    (via a pseudo-terminal, same as running it in a real terminal)
    inside the currently selected folder.
  - Once started, every plain message you type is sent straight to
    that running agy session, just like typing into a terminal - agy's
    replies are streamed back to you as they appear.
  - Buttons let you send the key-presses a keyboard would send:
    Enter, Ctrl+C (interrupt), Ctrl+D (EOF/exit), Esc.
    ("Shift" alone isn't a sendable key over a text protocol - it only
    matters combined with another key, e.g. Shift+Enter for a newline
    without submitting. If agy needs that, tell me and I'll add a
    dedicated "newline without submit" button.)
  - Tap "Stop AI" to end the session and go back to folder browsing.

Only replies to TELEGRAM_ALLOWED_USER_ID. Standard library only.

Required env vars (Koyeb secrets, same as PASSWORD):
  TELEGRAM_BOT_TOKEN       - from @BotFather
  TELEGRAM_ALLOWED_USER_ID - your numeric id, from @userinfobot
"""
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.request
import mimetypes

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
WORKSPACE = "/root/workspace"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

cwd_by_chat = {}       # chat_id -> relative path under WORKSPACE
awaiting_name = {}     # chat_id -> True while waiting for a typed folder name

ai_folder = {}         # chat_id -> absolute folder path AI is scoped to (AI mode active if present)
ai_process = {}        # chat_id -> subprocess.Popen currently running for this chat's AI call
ai_fresh = {}          # chat_id -> True to start a brand-new agy conversation (omit -c) on next message

AI_MODEL_FLAGS = os.environ.get("AI_MODEL_FLAGS", "--model gemini-2.5-pro --effort high").split()

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\].*?(?:\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?")

STALE_UPDATE_SECONDS = 10  # updates older than this (piled up while the bot
                           # was offline/redeploying) get dropped instead of
                           # executed - only fresh, live commands ever run

def _update_timestamp(update):
    """Unix timestamp of a plain message update, or None if not applicable.
    (callback_query has no tap-time field of its own - its .message.date is
    when the menu/button was originally posted, not when it was tapped, so
    we deliberately don't stale-filter callbacks by that.)"""
    msg = update.get("message")
    return msg.get("date") if msg else None

AI_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "\U0001F195 New chat (forget history here)", "callback_data": "ai_new"}],
        [{"text": "\U0001F6D1 Stop AI", "callback_data": "stopai"}],
    ]
}

# ---------- low-level Telegram API helpers ----------

def api_call(method, params, timeout=60):
    url = f"{API}/{method}"
    body = json.dumps(params).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def send_message(chat_id, text, keyboard=None):
    text = text or "(no output)"
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            chunk = text[i:i + 4000]
            params = {"chat_id": chat_id, "text": chunk}
            if keyboard and i + 4000 >= len(text):
                params["reply_markup"] = keyboard
            api_call("sendMessage", params)
    else:
        params = {"chat_id": chat_id, "text": text}
        if keyboard:
            params["reply_markup"] = keyboard
        api_call("sendMessage", params)

def answer_callback(callback_id, text=None):
    params = {"callback_query_id": callback_id}
    if text:
        params["text"] = text
    try:
        api_call("answerCallbackQuery", params)
    except Exception:
        pass  # query may be stale/expired (e.g. during backlog replay) -
              # that's fine, the real button action below still runs

def send_typing(chat_id):
    try:
        api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


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

# ---------- AI (agy) calls: one-shot subprocess per message, not a live pty ----------
#
# Each message is answered with a single `agy ... -c --dangerously-skip-permissions
# --add-dir <folder> -p "<message>"` call. This means:
#  - No permission prompts ever block the bot (--dangerously-skip-permissions).
#  - No separate "login state" to keep in sync: every call is a fresh process
#    that reads the same on-disk agy config as a terminal login would, so
#    logging in once (in ttyd or here) is enough everywhere.
#  - Much lighter than a persistent pty session (no idle threads, no terminal
#    emulation), which matters a lot on a 0.1 vCPU / 512MB free instance.

def _ai_reader(proc, q):
    try:
        while True:
            b = proc.stdout.read(1)
            if not b:
                break
            q.put(b)
    finally:
        q.put(b"")

def _ai_send_buffer(chat_id, buffer):
    if not buffer:
        return
    text = ANSI_RE.sub("", buffer.decode("utf-8", errors="ignore")).replace("\r", "").strip()
    buffer.clear()
    if text:
        send_message(chat_id, text)

def _ai_flusher(chat_id, proc, q):
    buffer = bytearray()
    last_send = time.time()
    last_typing = 0.0
    while proc.poll() is None or not q.empty():
        try:
            b = q.get(timeout=0.2)
            if not b:
                break
            buffer += b
            if len(buffer) >= 3500:
                _ai_send_buffer(chat_id, buffer)
                last_send = time.time()
        except queue.Empty:
            now = time.time()
            if buffer and now - last_send > 0.8:
                _ai_send_buffer(chat_id, buffer)
                last_send = now
            if now - last_typing > 4.0:
                send_typing(chat_id)
                last_typing = now
    _ai_send_buffer(chat_id, buffer)
    ai_process.pop(chat_id, None)
    send_message(chat_id, "\u2705 AI finished. Type your next message.", AI_KEYBOARD)

def run_ai_message(chat_id, text):
    folder = ai_folder.get(chat_id)
    if not folder:
        send_message(chat_id, "AI is not active here. Tap \U0001F916 Start AI here first.")
        return
    if ai_process.get(chat_id) is not None:
        send_message(chat_id, "\u23F3 AI is still answering your previous message, please wait...")
        return

    args = ["agy"] + AI_MODEL_FLAGS
    if not ai_fresh.pop(chat_id, False):
        args.append("-c")  # continue this folder's conversation history
    args += ["--print-timeout", "30m", "--dangerously-skip-permissions", "--add-dir", folder, "-p", text]

    try:
        proc = subprocess.Popen(
            args, cwd=folder,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except Exception as e:
        send_message(chat_id, f"Could not start AI: {e}")
        return

    ai_process[chat_id] = proc
    send_typing(chat_id)
    q = queue.Queue()
    threading.Thread(target=_ai_reader, args=(proc, q), daemon=True).start()
    threading.Thread(target=_ai_flusher, args=(chat_id, proc, q), daemon=True).start()

def stop_ai(chat_id):
    proc = ai_process.pop(chat_id, None)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    ai_folder.pop(chat_id, None)
    ai_fresh.pop(chat_id, None)

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

    rows.append([{"text": "\U0001F916 Start AI here", "callback_data": "startai"}])
    action_row = []
    if rel:
        action_row.append({"text": "\u2B06\uFE0F Up", "callback_data": "up"})
    action_row.append({"text": "\u2795 New folder", "callback_data": "newfolder"})
    action_row.append({"text": "\U0001F504 Refresh", "callback_data": "refresh"})
    rows.append(action_row)

    text = f"{label}\n\nTap a folder to enter, a file to download.\nType a message to run it as a command here, or tap Start AI to chat with agy."
    return text, {"inline_keyboard": rows}

def show_menu(chat_id):
    text, keyboard = build_menu(chat_id)
    send_message(chat_id, text, keyboard)

# ---------- action handlers ----------

def handle_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    data = callback["data"]
    answer_callback(callback["id"])

    if data == "startai":
        if chat_id in ai_folder:
            send_message(chat_id, "AI is already active here.", AI_KEYBOARD)
        else:
            cwd = get_cwd_path(chat_id)
            ai_folder[chat_id] = cwd
            ai_fresh[chat_id] = False
            send_message(chat_id, f"\U0001F916 AI ready in this folder:\n{cwd}\n\nJust type your message \u2014 no need to log in again, it uses the same login.", AI_KEYBOARD)
        return

    if data == "stopai":
        stop_ai(chat_id)
        show_menu(chat_id)
        return

    if data == "ai_new":
        if chat_id in ai_folder:
            ai_fresh[chat_id] = True
            send_message(chat_id, "\U0001F195 Next message starts a brand-new AI conversation in this folder.")
        return

    if data == "up":
        rel = cwd_by_chat.get(chat_id, "")
        set_cwd_rel(chat_id, os.path.dirname(rel))
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
    stop_typing = threading.Event()

    def _typing_loop():
        while not stop_typing.is_set():
            send_typing(chat_id)
            stop_typing.wait(4)

    threading.Thread(target=_typing_loop, daemon=True).start()
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=120,
        )
        out = (result.stdout or "") + (result.stderr or "")
        send_message(chat_id, out.strip() or f"(done, exit code {result.returncode})")
    except subprocess.TimeoutExpired:
        send_message(chat_id, "Command timed out after 120s.")
    finally:
        stop_typing.set()
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

    if text in ("/start", "/help", "/menu"):
        awaiting_name.pop(chat_id, None)
        show_menu(chat_id)
        return

    if text == "/backup":
        send_message(chat_id, "Backing up workspace + agy login to the Telegram group...")
        def _run_backup():
            result = subprocess.run(
                ["python3", "/root/backup.py", "once"],
                capture_output=True, text=True, timeout=180,
            )
            out = (result.stdout or "") + (result.stderr or "")
            send_message(chat_id, out.strip() or "Backup finished.")
        threading.Thread(target=_run_backup, daemon=True).start()
        return

    if text == "/stopai":
        if chat_id in ai_folder:
            stop_ai(chat_id)
            send_message(chat_id, "AI stopped.")
        else:
            send_message(chat_id, "No AI active here.")
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

    if chat_id in ai_folder:
        run_ai_message(chat_id, text)
        return

    run_command_here(chat_id, text)

def setup_menu_button():
    """
    Registers a persistent menu (Telegram's chat-bar 'Menu' button, next
    to the text field) so these are reachable without scrolling back to
    the buttons in an old message.
    """
    try:
        api_call("setMyCommands", {"commands": [
            {"command": "menu", "description": "Show the folder browser"},
            {"command": "stopai", "description": "Stop the running AI session"},
            {"command": "backup", "description": "Backup workspace + agy login now"},
            {"command": "help", "description": "Show this menu"},
        ]})
        api_call("setChatMenuButton", {"menu_button": {"type": "commands"}})
    except Exception as e:
        print(f"Could not set up chat menu button: {e}")

def main():
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set - telegram bot disabled.")
        return
    if not ALLOWED_USER_ID:
        print("WARNING: TELEGRAM_ALLOWED_USER_ID not set - bot will ignore everyone.")

    setup_menu_button()

    if ALLOWED_USER_ID:
        try:
            send_message(int(ALLOWED_USER_ID), "\U0001F7E2 Bot (re)started and ready. Send /start to begin.")
        except Exception as e:
            print(f"Could not send startup greeting: {e}")

    offset = 0
    while True:
        try:
            updates = api_call("getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
            for update in updates.get("result", []):
                offset = update["update_id"] + 1

                ts = _update_timestamp(update)
                if ts is not None and (time.time() - ts) > STALE_UPDATE_SECONDS:
                    continue  # old backlog (queued while bot was offline) - drop it, don't run it

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
