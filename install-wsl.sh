#!/usr/bin/env bash
# Install specular-sentinel as a systemd timer inside WSL2.
# Run from the repo root: sudo bash install-wsl.sh
# Idempotent: safe to re-run after editing sentinel.py or the units.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo: sudo bash install-wsl.sh" >&2
  exit 1
fi

install -d /opt/specular-sentinel
install -m 644 sentinel.py /opt/specular-sentinel/sentinel.py

install -d /etc/specular-sentinel
if [ ! -f /etc/specular-sentinel/env ]; then
  install -m 600 env.example /etc/specular-sentinel/env
  echo "created /etc/specular-sentinel/env; set INFRA_REPORT_KEY before enabling"
fi
chmod 600 /etc/specular-sentinel/env

install -m 644 systemd/specular-sentinel.service /etc/systemd/system/specular-sentinel.service
install -m 644 systemd/specular-sentinel.timer /etc/systemd/system/specular-sentinel.timer

systemctl daemon-reload
systemctl enable --now specular-sentinel.timer

echo "installed; next runs:"
systemctl list-timers specular-sentinel.timer --no-pager
echo "one-off manual pass: sudo systemctl start specular-sentinel.service"
echo "logs: journalctl -u specular-sentinel.service -n 20"
