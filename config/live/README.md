# Live configuration from the machine

`scripts/collect-live-config.sh` copies the working system settings from the Pi into this
folder (network, DHCP, openNDS, firewall and service files) with secrets removed. The
examples elsewhere in this repository are reconstructions; the files here are the truth.

Never commit `/etc/pisowifi.env` with its token, the dashboard password file, the openNDS
`faskey`, or `/var/lib/pisowifi/credits.sqlite3` (sales and customer device addresses).
