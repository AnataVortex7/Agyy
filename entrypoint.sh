#!/bin/bash
set -e

mkdir -p /root/workspace
export HOME=/root
export PATH="/root/.local/bin:${PATH}"
cd /root/workspace

# Web terminal, port matches Koyeb's configured port (set to 8000 in
# the Koyeb dashboard's "Exposed ports" / "Ports" section).
# Every connection runs auth_gate.py FIRST: it asks for the password
# (checked against the PASSWORD secret), blocks the IP for 24h after
# 3 wrong tries, and only on success attaches/creates the tmux
# session running agy.
ttyd -p 8000 -W python3 /root/auth_gate.py
