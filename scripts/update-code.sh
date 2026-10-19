#!/usr/bin/env bash
# Install the code from this repository onto the Pi and restart the Piso WiFi services.
# Runs every test first and stops if any fail. Does not touch openNDS, dnsmasq, the network,
# /etc/pisowifi.env or the sales database, so customers who are online stay online.
# Usage (on the Pi, from the repository folder): sudo bash scripts/update-code.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run: sudo bash $0"; exit 1; }
cd "$(dirname "$0")/.."
venv=/opt/pisowifi-venv/bin/python

echo "Running tests..."
(cd coin && python3 -m unittest -q test_coin_reader)
(cd app && $venv -m unittest -q test_creditd)
(cd portal && $venv -m unittest -q test_portal)
(cd admin && $venv -m unittest -q test_dashboard)

echo "Backing up the sales database..."
[[ -e /var/lib/pisowifi/credits.sqlite3 ]] && cp -a /var/lib/pisowifi/credits.sqlite3 "/root/credits-$(date +%Y%m%d-%H%M%S).sqlite3"

echo "Installing..."
install -d -m 0755 /opt/pisowifi
install -m 0755 app/creditd.py /opt/pisowifi/creditd.py
install -m 0755 coin/coin_reader.py /opt/pisowifi/coin_reader.py
install -m 0755 portal/portal.py /opt/pisowifi/portal.py
install -m 0755 admin/dashboard.py /opt/pisowifi/dashboard.py
systemctl restart pisowifi-creditd coin-reader pisowifi-portal pisowifi-dashboard
sleep 3
systemctl is-active pisowifi-creditd coin-reader pisowifi-portal pisowifi-dashboard
