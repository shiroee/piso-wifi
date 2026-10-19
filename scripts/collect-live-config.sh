#!/usr/bin/env bash
# Copy the working system settings from this Pi into config/live/ so the repository
# matches the real machine. Secrets are removed: the admin token, the openNDS faskey.
# Never collected: the dashboard password file, the sales database, SSH keys,
# DHCP leases and firewall dumps (they list customers' phone addresses).
# Usage (on the Pi, from the repository folder): sudo bash scripts/collect-live-config.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run: sudo bash $0"; exit 1; }
cd "$(dirname "$0")/.."
out=config/live
mkdir -p "$out"

copy() {  # copy SRC DEST-NAME if it exists
  if [[ -e "$1" ]]; then cp "$1" "$out/$2"; echo "  $1 -> $out/$2"; else echo "  (missing) $1"; fi
}

echo "Collecting into $out/:"
for f in /etc/netplan/*.yaml; do [[ -e "$f" ]] && copy "$f" "netplan-$(basename "$f")"; done
copy /etc/dnsmasq.d/pisowifi.conf dnsmasq-pisowifi.conf
copy /etc/config/opennds opennds.uci
copy /etc/pisowifi-guard.nft pisowifi-guard.nft
for unit in pisowifi-nat pisowifi-guard pisowifi-creditd pisowifi-portal pisowifi-dashboard coin-reader; do
  if systemctl cat "$unit" >/dev/null 2>&1; then systemctl cat "$unit" > "$out/$unit.service"; echo "  $unit.service"; fi
done
for f in /etc/pisowifi-nat.nft /etc/nftables.conf; do [[ -e "$f" ]] && copy "$f" "$(basename "$f")"; done
# Not collected: `nft list ruleset` (openNDS rules list customer phone addresses).

# Secrets out.
if [[ -e /etc/pisowifi.env ]]; then
  sed -E 's/^(PISOWIFI_ADMIN_TOKEN=).*/\1REDACTED/' /etc/pisowifi.env > "$out/pisowifi.env.redacted"
  echo "  /etc/pisowifi.env -> $out/pisowifi.env.redacted (token removed)"
fi
[[ -e "$out/opennds.uci" ]] && sed -i -E "s/(option faskey ).*/\1'REDACTED'/" "$out/opennds.uci"
sed -i -E 's/(faskey[^a-zA-Z0-9]+)[0-9a-f]{16,}/\1REDACTED/g' "$out"/* 2>/dev/null || true

if grep -rqiE '[0-9a-f]{48,}' "$out"; then
  echo "WARNING: something that looks like a secret is still in $out/. Check before committing."
fi
echo "Done. Review the files, then commit them."
