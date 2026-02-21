#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    [ -f requirements.txt ] && .venv/bin/pip install -r requirements.txt -q
fi

.venv/bin/python3 gui.py
