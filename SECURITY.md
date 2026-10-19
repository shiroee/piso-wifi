# Security notes

Never commit these (they're in `.gitignore` or not in the repository at all):

- `/etc/pisowifi.env`: holds `PISOWIFI_ADMIN_TOKEN`, which controls the credit service.
- `/etc/pisowifi-dashboard.passwd` and `/var/lib/pisowifi/dashboard.secret`.
- The openNDS `faskey` from `/etc/config/opennds`.
- `/var/lib/pisowifi/credits.sqlite3`: sales and customers' phone addresses.
- SD card images (`*.img`, `*.7z`): they contain all of the above.

`scripts/collect-live-config.sh` removes the token and the faskey; still read the diff.

If a secret leaks: make a new token (`python3 -c "import secrets; print(secrets.token_hex(24))"`),
put it in `/etc/pisowifi.env`, and restart `pisowifi-creditd coin-reader pisowifi-portal
pisowifi-dashboard`. For the dashboard, run `dashboard.py set-password`.

The design keeps the admin API on `127.0.0.1`, blocks the admin ports from the customer
LAN (`pisowifi-guard`), identifies phones by MAC from the ARP table rather than anything
the phone sends, and rate-limits logins and voucher codes.
