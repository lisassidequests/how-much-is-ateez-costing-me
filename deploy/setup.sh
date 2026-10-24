#!/usr/bin/env bash
# One-command setup on an Ubuntu VM (e.g. Oracle Cloud free tier).
# Run from anywhere:  bash deploy/setup.sh
# First run creates .env and stops so you can fill it in; run it again to start the bot.
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update -y
sudo apt-get install -y python3 python3-venv git

python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created .env. Edit it (nano .env), add your Telegram token and Anthropic key, then run this script again."
  exit 0
fi

sed "s|__DIR__|$PWD|g; s|__USER__|$USER|g" deploy/ateez-bot.service | sudo tee /etc/systemd/system/ateez-bot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now ateez-bot
sudo systemctl restart ateez-bot
echo
echo "Bot is running. Logs: journalctl -u ateez-bot -f"
echo "Update later with: git pull && bash deploy/setup.sh"
