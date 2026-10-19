# Install guide: from a blank SD card to a working machine

This is the order that worked on the real machine (Orange Pi PC, Armbian Debian 13,
openNDS 10.3.1, Allan board, EAP110-Outdoor). Commands run on the Pi as root unless
they say "on your PC". Keep an HDMI screen and keyboard handy until the network works.

Interface names below are from the real board: `end0` (built-in Ethernet, WAN) and
`enx00e099004abb` (USB Ethernet, customer LAN). Yours may differ; check with `ip -br link`.

## 1. Armbian

1. Download the Armbian image for **Orange Pi PC** (Debian 13 "Trixie" worked) and write it
   to a microSD card (balenaEtcher or Win32 Disk Imager → **Write**).
2. Boot, follow the first-login prompts, set a strong root password, then:

```sh
apt update && apt full-upgrade -y
timedatectl set-timezone Asia/Manila
```

## 2. Get this repository onto the Pi

If the repository is private, the Pi needs a GitHub login to clone it (for example a
personal access token); otherwise copy it over as a zip with `scp`.


```sh
apt install -y git
git clone https://github.com/shiroee/piso-wifi.git /opt/pisowifi-buildkit
cd /opt/pisowifi-buildkit
bash scripts/install-deps.sh                 # Python venv + Flask, dnsmasq, nftables
apt install -y python3-libgpiod gpiod tcpdump
```

## 3. Network: WAN, customer LAN, DHCP, NAT

Examples are in `network/`; adapt the interface names first.

```sh
cp network/netplan-pisowifi.yaml.example /etc/netplan/20-pisowifi.yaml   # edit names
netplan try                                                                 # from a console
cp network/dnsmasq-pisowifi.conf.example /etc/dnsmasq.d/pisowifi.conf
dnsmasq --test && systemctl restart dnsmasq
cp network/99-pisowifi-forwarding.conf.example /etc/sysctl.d/99-pisowifi-forwarding.conf
sysctl --system
cp network/pisowifi-nat.nft.example /etc/pisowifi-nat.nft
cp systemd/pisowifi-nat.service.example /etc/systemd/system/pisowifi-nat.service
systemctl daemon-reload && systemctl enable --now pisowifi-nat
```

Check: a phone on the customer Wi-Fi gets a `10.20.0.x` address and can browse (openNDS
isn't installed yet, so it's free for now).

## 4. Access point

Set the access point (or any router you use as one) to **Access Point / Bridge** mode with
its **DHCP server off**, so each phone gets its own address from the Pi. Give it a fixed
management address outside the DHCP range, for example IP `10.20.0.2`, mask
`255.255.255.0`, gateway `10.20.0.1`, and **change its admin password**: customers can
reach that page. Check with `cat /var/lib/misc/dnsmasq.leases` that every phone has its own line.

## 5. openNDS (captive portal)

```sh
apt-cache policy opennds && apt install -y opennds   # or build per the openNDS docs
cp opennds/opennds.uci.example /etc/config/opennds    # edit the interface name
```

Apply the USB-adapter fix from `opennds-notes.md` ("rejected as not up"), then start it.
`systemctl restart opennds` can fail on this board; use stop, wait, start:

```sh
systemctl stop opennds; sleep 30; systemctl start opennds; sleep 30
ndsctl status | head -20
```

Install the firewall guard, which keeps customers away from SSH and the admin ports:

```sh
install -m 0644 network/pisowifi-guard.nft /etc/pisowifi-guard.nft
install -m 0644 systemd/pisowifi-guard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pisowifi-guard
```

Before going further, check that a phone let through by hand can browse:

```sh
ndsctl auth <phone-mac> 3 0 0 0 0 "test"     # 3 minutes; then open websites on the phone
ndsctl deauth <phone-mac>
```

## 6. Credit service

```sh
install -m 0600 config/pisowifi.env.example /etc/pisowifi.env
python3 -c "import secrets; print(secrets.token_hex(24))"   # paste as PISOWIFI_ADMIN_TOKEN
nano /etc/pisowifi.env                                       # also: PISOWIFI_ENABLE_NDS_AUTH=true
install -d -m 0750 /var/lib/pisowifi
(cd app && /opt/pisowifi-venv/bin/python -m unittest test_creditd)
install -m 0755 app/creditd.py /opt/pisowifi/creditd.py
install -m 0644 systemd/pisowifi-creditd.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pisowifi-creditd
```

## 7. Coin reader and slot light

Wire the coin acceptor through the Allan board (see `hardware.md`). Then:

```sh
(cd coin && python3 -m unittest test_coin_reader)
install -m 0755 coin/coin_reader.py /opt/pisowifi/coin_reader.py
python3 /opt/pisowifi/coin_reader.py --dry-run --led-line 11   # drop coins, then Ctrl+C
grep -q '^COIN_LED_LINE=' /etc/pisowifi.env || echo 'COIN_LED_LINE=11' >> /etc/pisowifi.env
install -m 0644 systemd/coin-reader.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now coin-reader
```

## 8. Customer portal

```sh
(cd portal && /opt/pisowifi-venv/bin/python -m unittest test_portal)
install -m 0755 portal/portal.py /opt/pisowifi/portal.py
install -m 0644 systemd/pisowifi-portal.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pisowifi-portal
curl -s -o /dev/null -w '%{http_code}\n' http://10.20.0.1:2080/    # 200
```

openNDS already points at it (`fasport 2080` in `/etc/config/opennds`). The short address
`http://piso.wifi` comes from dnsmasq plus the port-80 redirect in `pisowifi-guard.nft`.

## 9. Owner dashboard

```sh
(cd admin && /opt/pisowifi-venv/bin/python -m unittest test_dashboard)
install -m 0755 admin/dashboard.py /opt/pisowifi/dashboard.py
/opt/pisowifi-venv/bin/python /opt/pisowifi/dashboard.py set-password   # run on its own
install -m 0644 systemd/pisowifi-dashboard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pisowifi-dashboard
```

Open `http://<Pi's home-network address>:8091` from your home Wi-Fi (not the Piso WiFi).

## 10. Rates, name and look

Set the rates (`rates.md`), then on the dashboard **Settings → Name and look** enter the
Piso WiFi name customers should see.

## 11. Test, then back up

Go through `test-plan.md` (at least: two phones get separate time, a reboot restores paid
time, unpaid phones can't browse). Then make an SD card backup (`operations.md`).

## Checking everything is running

```sh
systemctl is-active opennds pisowifi-creditd pisowifi-portal pisowifi-dashboard coin-reader pisowifi-guard
```

Six times `active`. openNDS can take about 20 seconds longer after a boot.
