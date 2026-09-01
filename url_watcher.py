#!/usr/bin/env python3
"""
Watches the 'agent' tmux session and pulls out any long login/OAuth URL
it prints, fully un-wrapped, into /root/oauth_data.json.

Key trick: agy hard-wraps long URLs itself (real newlines, sometimes
mid-word, no space at the break) rather than relying on terminal
soft-wrap. So instead of regex-matching one line at a time, we group
the pane output into blank-line-separated paragraphs, find the one
that starts with "http", and glue its lines back together with no
separator at all -- which reconstructs the URL exactly as printed.
"""
import json
import os
import subprocess
import time

SESSION = "agent"
OUT_FILE = "/root/oauth_data.json"

def capture_pane():
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", SESSION, "-p", "-S", "-500"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except Exception:
        return ""

def find_best_url(text):
    """
    agy prints long URLs by manually word-wrapping them with real
    newlines (not terminal soft-wrap), often splitting mid-word with
    no space at the break. So: split the pane into blank-line-separated
    paragraphs, find the paragraph that starts with 'http', and glue
    all of its lines back together with NO separator (matching how it
    was actually wrapped) instead of trying to regex-match one line
    at a time.
    """
    lines = text.replace("\r", "").split("\n")

    paragraphs = []
    current = []
    for line in lines:
        if line.strip() == "":
            if current:
                paragraphs.append(current)
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append(current)

    candidates = []
    for para in paragraphs:
        first = para[0].strip()
        if first.startswith("http://") or first.startswith("https://"):
            joined = "".join(l.strip() for l in para)
            candidates.append(joined)

    if not candidates:
        return None
    # Prefer the longest reconstructed URL (the real auth link, not a
    # short doc/reference link that might also be printed elsewhere).
    return max(candidates, key=len).rstrip(").,\"'")

def write_out(url):
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"url": url, "updated": time.time()}, f)
    os.replace(tmp, OUT_FILE)

def main():
    last_url = None
    # Seed the file so the page doesn't 404 before the first URL shows up.
    write_out(None)
    while True:
        text = capture_pane()
        url = find_best_url(text)
        if url and url != last_url:
            last_url = url
            write_out(url)
        time.sleep(2)

if __name__ == "__main__":
    main()
