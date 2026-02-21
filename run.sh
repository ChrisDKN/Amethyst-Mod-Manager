#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    [ -f requirements.txt ] && .venv/bin/pip install -r requirements.txt -q
fi

# Install any missing or newly added requirements
if [ -f requirements.txt ]; then
    .venv/bin/pip install -r requirements.txt -q --disable-pip-version-check 2>/dev/null
fi

.venv/bin/python3 gui.py
