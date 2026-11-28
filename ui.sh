#!/bin/bash
# Open quarto in your browser. Keep the Terminal window open while you use it.
# Usage, from the quarto folder:  bash ui.sh     (or double-click "Start Quarto.command")
set -e
cd "$(dirname "$0")"
echo "Starting Quarto..."
if [ ! -d .venv ]; then
  echo "Setting up Python virtual environment (first run only)..."
  python3 -m venv .venv
fi
source .venv/bin/activate
if ! python -c "import yaml, requests" >/dev/null 2>&1; then
  echo "Installing dependencies..."
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
fi
python -m quarto.ui
