# Troubleshooting

Problems met while building the real machine, with the fix that worked.

| Symptom | Cause | Fix |
|---|---|---|
| Two phones share one customer's time; "Your device" is the same on both | A router between the access point and the Pi gives the phones its own addresses (NAT), so the Pi sees one device | Put that device in **Bridge / Access Point** mode with DHCP off. `cat /var/lib/misc/dnsmasq.leases` must show one line per phone. |
| Phone joins the Wi-Fi but gets nothing; `tcpdump` shows the phone repeating "who-has 10.20.0.1" while the Pi answers | Access point stuck, not passing the Pi's replies | Power-cycle the access point. If it recurs, update its firmware. |
| openNDS restarts in a loop: "Could not get IP address information of enx…" | USB adapter reports `state UNKNOWN`; openNDS only accepts `UP` | One-line fix in `opennds-notes.md`. Re-apply after any openNDS upgrade. |
| `systemctl restart opennds` fails: "openNDS is already running … Retry later" | Shutdown takes longer than the restart | `systemctl stop opennds`, wait 30 s, `systemctl start opennds`. systemd also retries by itself after 20 s. |
| `ndsctl: opennds probably not yet started` | openNDS is down; while it's down the portal doesn't block anyone | See the two rows above; check `journalctl -u opennds -n 30`. |
| Portal unreachable from phones but `curl http://10.20.0.1:2080/` on the Pi gives 200 | openNDS firewall blocks port 2080 | `list users_to_router 'allow tcp port 2080'` and `fasport` in `/etc/config/opennds`, then stop/wait/start openNDS. |
| Phones see openNDS's "Click to Continue" (free internet) | openNDS isn't pointed at the portal | `fasport '2080'` and `faspath '/'` in `/etc/config/opennds`. |
| Customers can reach SSH on the Pi | openNDS always allows port 22 to the router | `pisowifi-guard` (drops 22, 8090, 8091 from the customer LAN). |
| Coins detected but "HTTP 401" in the coin reader log | The coin reader doesn't have the admin token (old version, or token unset) | `PISOWIFI_ADMIN_TOKEN` in `/etc/pisowifi.env`; `systemctl restart coin-reader` (not `enable --now`, which doesn't restart a running service). Waiting coins are resent from the spool. |
| Coins counted wrong (₱5 shows as ₱3) | Acceptor speed switch doesn't match | Speed switch on **Medium**; capture with `gpiomon` and decode with `coin_reader.py --replay`. |
| `gpiomon: unable to request lines … No such device or address` | PC/PD-bank pins can't detect edges on the H3 | Use PA/PG pins (the coin pin is PA12). |
| `gpiomon` says the line is busy | The coin reader holds lines 11 and 12 | `systemctl stop coin-reader` first; start it again afterwards. |
| CS LED connector reads ~6 V with the light off | Normal: the switch is open and the meter reads a floating voltage | It reads ~11.6 V when the light is on. |
| Dashboard unreachable | You're on the Piso WiFi (blocked on purpose), or the Pi's home address changed | Use your home Wi-Fi; `ip -br addr show end0` for the address. |
| Pasting a block with `set-password` breaks the password | The prompt swallows the next pasted line | Run `dashboard.py set-password` on its own. |
| Debug log shows the `faskey` | `debuglevel '3'` prints it | Keep `debuglevel '1'`; change the key if it was shared. |
| Backup `.img` is as big as the card | Disk imaging copies every byte | Zero the free space before imaging, then 7-Zip the `.img` (`operations.md`). |
