# openNDS 10.3.1 on Armbian: what we found on the real board (2026-10-16)

## Settings file
openNDS reads `/etc/config/opennds` (OpenWrt-style `option`/`list` lines).
`/etc/opennds/opennds.conf` is not used.

Current settings added by hand:
```
option gatewayinterface 'enx00e099004abb'
option debuglevel '1'
list users_to_router 'allow tcp port 53'
list users_to_router 'allow udp port 53'
list users_to_router 'allow udp port 67'
```
Keep `debuglevel` at 1. Level 3 prints the `faskey` secret into the journal.
If the key has ever been shared, set a new one before using FAS.

## USB Ethernet adapter rejected as "not up"
The adapter (`cdc_ether` driver) reports `state UNKNOWN`, and openNDS only
accepts `state UP`, so it exits with "Could not get IP address information".
Fix (keep the backup):
```sh
cp -a /usr/lib/opennds/libopennds.sh /root/libopennds.sh.orig
sed -i 's/grep -c -w "state UP"/grep -c -w -E "state (UP|UNKNOWN)"/' /usr/lib/opennds/libopennds.sh
bash /usr/lib/opennds/libopennds.sh gatewayip "enx00e099004abb" "inet"; echo   # should print 10.20.0.1
```
Reinstalling or upgrading openNDS undoes this. Run the `sed` line again afterwards.
A permanent alternative is to put the adapter in a network bridge (bridges report `UP`).

## Customers could reach SSH
openNDS always lets customers reach SSH (22) and 443 on the Pi, whatever
`users_to_router` says. `network/pisowifi-guard.nft` (loaded by
`pisowifi-guard.service`) drops SSH, the credit service and the dashboard
from the customer LAN in a separate firewall table openNDS never touches.

## Restarting openNDS
`systemctl restart opennds` can fail with "openNDS is already running ...
Retry later" because shutdown takes a while. systemd retries by itself after
20 seconds. To restart by hand: `systemctl stop opennds`, wait about 30
seconds, `systemctl start opennds`.

## `ndsctl` behaviour, tested with a phone
- `ndsctl auth <mac> <minutes> 0 0 0 0 "text"` grants a session; openNDS
  deauthenticates the client by itself at the session end.
- `ndsctl auth` on a client that is already authenticated fails. To add time,
  `ndsctl deauth` then `ndsctl auth` with the remaining plus new minutes.
- Sessions survive an openNDS restart. openNDS keeps them under `/tmp`, so they
  are expected to be lost on a reboot (not yet tested).
- A script warning appears on deauth (`line 2002: [: : integer expression
  expected`); the deauth still completes.
- Still to confirm: that an authenticated phone can actually load websites.

## Sending customers to the Piso WiFi portal
openNDS's default "Click to Continue" page gives free internet. Point openNDS
at `portal/portal.py` (port 2080) instead, as its FAS:
```sh
echo "address=/status.client/10.20.0.1" >> /etc/dnsmasq.d/pisowifi.conf
systemctl restart dnsmasq
cat >> /etc/config/opennds <<'EOF'
    option fasport '2080'
    option faspath '/'
    list users_to_router 'allow tcp port 2080'
EOF
systemctl stop opennds; sleep 30; systemctl start opennds; sleep 30
ndsctl status | grep -E 'FAS|Click'
```
openNDS writes its own name, `status.client`, into the portal address; the
dnsmasq line makes sure phones can resolve it. The portal ignores the `?fas=`
data openNDS adds: it identifies the phone from its IP address instead, so
the `faskey` isn't used. The `ndsctl status` FAS line should now point at port
2080. If a phone still shows openNDS's own page, check that line first.

## Short address for customers: http://piso.wifi
Customers check their time at `http://piso.wifi` instead of `http://10.20.0.1:2080`:
- dnsmasq answers the name: `address=/piso.wifi/10.20.0.1` in `/etc/dnsmasq.d/pisowifi.conf`.
- `network/pisowifi-guard.nft` redirects port 80 on 10.20.0.1 to the portal's port 2080.
- `PISOWIFI_PORTAL_ADDRESS=http://piso.wifi` in `/etc/pisowifi.env` makes the portal show it.
Typing `http://` matters: `.wifi` isn't a real web ending, so some browsers search for a
bare `piso.wifi` instead of opening it.

## Speed limits
The credit service passes the dashboard's speed limits to `ndsctl auth` as its upload
and download rate arguments, in kilobits per second (the dashboard shows Mbps and
multiplies by 1000). openNDS documents these arguments as "kb/s" without saying bits or
bytes; check with a speed test on a phone after setting a limit, and if the result is
8 times off, divide or multiply the dashboard value by 8. A new limit applies from each
customer's next purchase, resume or reconnect, not to sessions already running.
