# Network

| File | Goes to | What it does |
|---|---|---|
| `netplan-pisowifi.yaml.example` | `/etc/netplan/` | WAN by DHCP on `end0`, customer LAN `10.20.0.1/24` on the USB adapter |
| `dnsmasq-pisowifi.conf.example` | `/etc/dnsmasq.d/pisowifi.conf` | DHCP `10.20.0.50–200` and DNS for the customer LAN; `piso.wifi` and `status.client` names |
| `99-pisowifi-forwarding.conf.example` | `/etc/sysctl.d/` | IP forwarding |
| `pisowifi-nat.nft.example` | `/etc/pisowifi-nat.nft` | Masquerade customer traffic out of `end0` (with `systemd/pisowifi-nat.service.example`) |
| `pisowifi-guard.nft` | `/etc/pisowifi-guard.nft` | Drops SSH, 8090 and 8091 from the customer LAN; redirects `http://piso.wifi` to the portal |

The `.example` files are reconstructions of the working setup. For the exact files from
the machine, run `scripts/collect-live-config.sh` and look in `config/live/`.

Before changing the network: keep a screen and keyboard connected, find the interface
names with `ip -br link` (unplug the USB adapter to see which one disappears), and use
`netplan try` so a mistake rolls back by itself.

Only one DHCP server may serve the customer LAN: the Pi's dnsmasq. The access point must
be in Access Point / Bridge mode with its own DHCP off.
