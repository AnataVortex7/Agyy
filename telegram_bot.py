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
import pty
import re
import select
import signal
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
ai_sessions = {}       # chat_id -> AgySession

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\].*?(?:\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?")

AI_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "\u23CE Enter", "callback_data": "ai_enter"},
            {"text": "\u2303\u23CE Ctrl+Enter", "callback_data": "ai_ctrlenter"},
        ],
        [
            {"text": "\u21E7\u2191 Shift+Up", "callback_data": "ai_shift_up"},
            {"text": "\u21E7\u2193 Shift+Down", "callback_data": "ai_shift_down"},
        ],
        [
            {"text": "\u2303C", "callback_data": "ai_ctrlc"},
            {"text": "\u2303D", "callback_data": "ai_ctrld"},
            {"text": "Esc", "callback_data": "ai_esc"},
        ],
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

# ---------- interactive AI (agy) session, backed by a real pty ----------

class AgySession:
    def __init__(self, chat_id, cwd):
        self.chat_id = chat_id
        self.alive = True
        self.buffer = b""
        self.last_data_time = time.time()
        self.master_fd, slave_fd = pty.openpty()
        self.proc = subprocess.Popen(
            ["agy"], cwd=cwd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            preexec_fn=os.setsid, close_fds=True,
        )
        os.close(slave_fd)
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._flusher, daemon=True).start()

    def _auto_respond(self, data):
        """
        Real terminals silently answer certain queries the moment they see
        them (device attributes, mode requests, kitty keyboard protocol
        queries). Our pty never answered these, so agy sat waiting forever.
        We scan for known query sequences and write back a safe canned
        answer immediately, so agy's handshake can complete.
        """
        # DECRQM - "is mode N set?" -> answer "not recognized" (0)
        for m in re.finditer(rb"\x1b\[\?(\d+)\$p", data):
            mode = m.group(1)
            self.write_raw(b"\x1b[?" + mode + b";0$y")
        # Primary Device Attributes query -> claim to be a basic VT220-ish terminal
        if re.search(rb"\x1b\[c(?!\d)", data) or b"\x1b[0c" in data:
            self.write_raw(b"\x1b[?1;2c")
        # Kitty keyboard protocol "what flags are active?" -> answer "none"
        if b"\x1b[?u" in data:
            self.write_raw(b"\x1b[?0u")

    def _reader(self):
        while self.alive:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 1.0)
                if ready:
                    data = os.read(self.master_fd, 4096)
                    if not data:
                        self.alive = False
                        break
                    self._auto_respond(data)
                    self.buffer += data
                    self.last_data_time = time.time()
            except OSError:
                self.alive = False
                break

    def _flusher(self):
        while self.alive:
            time.sleep(0.8)
            if self.buffer and (time.time() - self.last_data_time) > 0.8:
                raw = self.buffer
                self.buffer = b""
                text = ANSI_RE.sub("", raw.decode(errors="ignore")).replace("\r", "")
                text = text.strip()
                if text:
                    send_message(self.chat_id, text, AI_KEYBOARD)
        if self.buffer:
            text = ANSI_RE.sub("", self.buffer.decode(errors="ignore")).replace("\r", "").strip()
            if text:
                send_message(self.chat_id, text)
        send_message(self.chat_id, "(AI session ended)")
        ai_sessions.pop(self.chat_id, None)

    def write_text(self, text):
        os.write(self.master_fd, (text + "\n").encode())

    def write_raw(self, raw_bytes):
        os.write(self.master_fd, raw_bytes)

    def stop(self):
        self.alive = False
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            os.close(self.master_fd)
        except Exception:
            pass

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

    session = ai_sessions.get(chat_id)

    if data == "startai":
        if session and session.alive:
            send_message(chat_id, "AI is already running here.", AI_KEYBOARD)
        else:
            cwd = get_cwd_path(chat_id)
            ai_sessions[chat_id] = AgySession(chat_id, cwd)
            send_message(chat_id, "AI started. Just type normally to chat with it.", AI_KEYBOARD)
        return

    if data == "stopai":
        if session:
            session.stop()
            ai_sessions.pop(chat_id, None)
        show_menu(chat_id)
        return

    if data == "ai_enter" and session:
        session.write_raw(b"\n")
        return
    if data == "ai_ctrlenter" and session:
        # Plain terminals can't literally distinguish Ctrl+Enter from Enter
        # (both are historically just carriage return). Modern apps that
        # DO distinguish it (kitty keyboard protocol / xterm "CSI u" mode)
        # expect this escape sequence for Ctrl+Enter:
        session.write_raw(b"\x1b[13;5u")
        return
    if data == "ai_shift_up" and session:
        session.write_raw(b"\x1b[1;2A")
        return
    if data == "ai_shift_down" and session:
        session.write_raw(b"\x1b[1;2B")
        return
    if data == "ai_ctrlc" and session:
        session.write_raw(b"\x03")
        return
    if data == "ai_ctrld" and session:
        session.write_raw(b"\x04")
        return
    if data == "ai_esc" and session:
        session.write_raw(b"\x1b")
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

    if text in ("/start", "/help", "/menu"):
        awaiting_name.pop(chat_id, None)
        show_menu(chat_id)
        return

    if text == "/stopai":
        session = ai_sessions.get(chat_id)
        if session:
            session.stop()
            ai_sessions.pop(chat_id, None)
            send_message(chat_id, "AI session stopped.")
        else:
            send_message(chat_id, "No AI session running.")
        show_menu(chat_id)
        return

    session = ai_sessions.get(chat_id)
    if session and session.alive:
        session.write_text(text)
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
