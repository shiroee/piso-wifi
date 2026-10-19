# Running the machine

## Daily use

- **Dashboard:** `http://<Pi's home-network address>:8091` from your home Wi-Fi. Sales,
  coin mix, connected phones, recent coins, vouchers and settings.
- **Customers check their time** at `http://piso.wifi`.
- **Unassigned coins** (inserted before anyone tapped Insert Coin) show a warning on the
  dashboard. Give them to the right customer from **Recent coins → Give to a customer**.

## Vouchers

Dashboard → **Vouchers** → choose how many, minutes each and your selling price →
**Create vouchers** → **Print**. Each code works once. **Cancel unused** stops a lost batch.
Five wrong codes lock a phone out for 10 minutes.

## Updating the software

On your PC, pull or download the new version; on the Pi:

```sh
cd /opt/pisowifi-buildkit && git pull
bash scripts/update-code.sh
```

The script runs every test first, backs up the sales database to `/root/`, installs the
four programs and restarts them. openNDS and the network aren't touched, so customers
stay online.

## Keeping this repository in sync with the machine

```sh
cd /opt/pisowifi-buildkit && bash scripts/collect-live-config.sh
git add config/live && git commit -m "Update live config" && git push
```

Secrets are removed by the script; still read the diff before pushing.

## SD card backup (Win32 Disk Imager)

On the Pi (with no customers connected):

```sh
systemctl is-active opennds pisowifi-creditd pisowifi-portal pisowifi-dashboard coin-reader pisowifi-guard
apt clean && journalctl --vacuum-size=50M
systemctl stop coin-reader pisowifi-portal pisowifi-dashboard pisowifi-creditd opennds
dd if=/dev/zero of=/zero.fill bs=1M status=progress; sync; rm -f /zero.fill; sync
poweroff
```

`dd` ends with "No space left on device": that's expected. It fills the free space with
zeros so the image compresses well.

On your PC: card into the reader (if Windows offers to **format** it, click **Cancel**),
Win32 Disk Imager → new file name → the card's drive letter → **Read Only Allocated
Partitions** → **Read** (never Write) → **Verify Only**. Then compress the `.img` with
7-Zip (7z format). The `.img` is always the card's full size; the `.7z` is about the size
of the data. Restore by extracting the `.img` and using **Write** onto a card at least as big.

Keep the image private: it contains your admin token, dashboard password and sales.
