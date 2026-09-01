#!/usr/bin/env python3
"""
Watches the 'agent' tmux session and pulls out any long login/OAuth URL
it prints, fully un-wrapped, into /root/oauth_data.json.

Key trick: `tmux capture-pane -J` re-joins lines that tmux only wrapped
because of terminal width. That's what was corrupting the URL before
(the terminal wraps a 700-char URL across 5 lines, and copying those
visually-wrapped lines by hand drops/adds characters at the breaks).
Capturing with -J gives back the original single unbroken line.
"""
import json
import os
import re
import subprocess
import time

SESSION = "agent"
OUT_FILE = "/root/oauth_data.json"

# Matches http(s) URLs with no whitespace in them (a wrapped-and-rejoined
# URL will have no spaces once -J has done its job).
URL_RE = re.compile(r"https?://\S+")

def capture_pane():
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", SESSION, "-p", "-J", "-S", "-500"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except Exception:
        return ""

def find_best_url(text):
    urls = URL_RE.findall(text)
    if not urls:
        return None
    # Prefer the longest URL on screen (the real auth link, not a short
    # doc/reference link that might also be printed).
    return max(urls, key=len).rstrip(").,\"'")

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
