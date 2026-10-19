# Changelog

## 2.9
- Long times show in days ("31 days", "2 days and 1:59:41") on the portal, dashboard and vouchers.

## 2.8
- Coin slot light (Allan board CS LED, line 11): on while a customer can insert coins, blinks per coin.

## 2.7
- Dashboard "Name and look": Piso WiFi name everywhere, dashboard theme and accent colour.

## 2.6
- Modern redesign of the portal (tap-the-coin button, countdown ring) and a tabbed dashboard.

## 2.5
- Speed limit per customer, promo rates, pause/resume, vouchers, Insert Coin tap limit,
  English/Tagalog portal, giving unassigned coins to a customer. Sessions move to an `access` table.

## 2.4
- Short customer address `http://piso.wifi`.

## 2.3
- Customer portal (openNDS FAS), internet time via `ndsctl auth`, reboot recovery loop,
  late-coin protection.

## 2.2
- Owner dashboard; settings saved in the database; firewall guard; openNDS notes.

## 2.1
- Coin reader on PA12 (header pin 3) with a write-ahead spool; `/api/coin` with exactly-once
  recording; Insert Coin window.

## 2.0
- First build kit: network notes, openNDS example, credit/ledger prototype.
