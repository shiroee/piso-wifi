#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run: sudo bash $0"; exit 1; }
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip sqlite3 curl iproute2 iptables nftables \
  dnsmasq git
install -d -m 0755 /opt/pisowifi
python3 -m venv /opt/pisowifi-venv
/opt/pisowifi-venv/bin/pip install --upgrade pip
/opt/pisowifi-venv/bin/pip install Flask
echo
echo "Dependencies installed. Check openNDS availability separately:"
echo "  apt-cache policy opennds"
echo "No network interfaces, firewall, DHCP, or GPIO settings were changed."
