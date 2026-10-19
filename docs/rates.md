# Rates and promos

The price list is a **base rate** (minutes per ₱1) plus up to **10 promo tiers**. When a
customer inserts coins in one go, they get the best combination of the base rate and the
promos for the total. For example, ₱5 + ₱5 inserted together are priced as ₱10.

Coins inserted in a later Insert Coin window are priced separately and added on top.

## Edna's Piso Wifi rates

| Amount | Time | Minutes |
|---|---|---|
| ₱1 | 10 min | base rate: 10 per peso |
| ₱5 | 1 hour | 60 |
| ₱10 | 3 hours | 180 |
| ₱15 | 6 hours | 360 |
| ₱20 | 10 hours | 600 |
| ₱25 | 15 hours | 900 |
| ₱30 | 1 day | 1440 |
| ₱40 | 2 days | 2880 |
| ₱50 | 3 days | 4320 |
| ₱100 | 7 days | 10080 |
| ₱400 | 31 days | 44640 |

In-between amounts use the best mix: ₱35 = 1 day 1 hr, ₱60 = 3 days 3 hrs, ₱200 = 14 days.

## Setting them

On the dashboard: **Settings → Rate and coinslot → Minutes per peso**, then **Promos**.

Or from the Pi:

```sh
set -a; . /etc/pisowifi.env; set +a
curl -s -X POST http://127.0.0.1:8090/api/settings -H "X-Admin-Token: $PISOWIFI_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"minutes_per_peso":10}'
curl -s -X POST http://127.0.0.1:8090/api/promos -H "X-Admin-Token: $PISOWIFI_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"promos":[{"pesos":5,"minutes":60},{"pesos":10,"minutes":180},{"pesos":15,"minutes":360},{"pesos":20,"minutes":600},{"pesos":25,"minutes":900},{"pesos":30,"minutes":1440},{"pesos":40,"minutes":2880},{"pesos":50,"minutes":4320},{"pesos":100,"minutes":10080},{"pesos":400,"minutes":44640}]}'
```

When you change prices, update the printed materials too (`tools/print-materials`).
