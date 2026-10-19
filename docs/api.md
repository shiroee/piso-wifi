# API reference

Three small Flask services run on the Pi. Only the credit service touches the database
or openNDS; the portal and the dashboard call it on `127.0.0.1` with the admin token,
which never leaves the Pi.

| Service | File | Listens on | Who uses it |
|---|---|---|---|
| Credit service | `app/creditd.py` | `127.0.0.1:8090` | coin reader, portal, dashboard |
| Customer portal | `portal/portal.py` | `10.20.0.1:2080` | customers' phones (openNDS sends them here) |
| Owner dashboard | `admin/dashboard.py` | `0.0.0.0:8091` | the owner, from the home network only |

## Credit service (`creditd.py`)

Every `/api/...` route needs the header `X-Admin-Token: $PISOWIFI_ADMIN_TOKEN`.
Bodies are JSON. A customer is identified by `mac` (lower-case `aa:bb:cc:dd:ee:ff`).

| Method and path | What it does |
|---|---|
| `GET /health` | Liveness check, no token. |
| `GET /api/status` | Rate, ledger counts, coin totals, unassigned coin pesos. |
| `POST /api/coin` | One coin from the coin reader: `{coin_id, pesos, pulses, detected_at}`. Recorded once per `coin_id`; credited to whoever holds the Insert Coin window, otherwise unassigned. |
| `POST /api/insert-coin` | Customer tapped Insert Coin: `{mac}`. `409` if another customer holds the slot, `429` after too many taps without a coin. |
| `GET /api/insert-coin` | The open window and what it has collected (the coin reader polls this for the slot light). |
| `POST /api/insert-coin/done` | Close the customer's window early: `{mac}`. |
| `GET /api/customer?mac=` | Everything the portal shows one phone: credit, time left, pause state, window state, rate card. |
| `POST /api/connect` | Turn the customer's credit into internet time (`ndsctl auth`): `{mac}`. Adds to time already running. |
| `POST /api/pause` / `POST /api/resume` | Stop or restart the customer's clock: `{mac}`. |
| `POST /api/redeem` | Use a voucher code: `{mac, code}`. Rate-limited per phone and overall. |
| `POST /api/sessions/reconcile` | Run the 20-second background check now (re-authorize paid time, start waiting credit). |
| `GET/POST /api/settings` | `minutes_per_peso`, `coin_window_seconds`, `coin_grace_seconds`, `download_kbps`, `upload_kbps`. Saved in the database; override `/etc/pisowifi.env`. |
| `POST /api/rate` | Old way to set `minutes_per_peso`; still works. |
| `GET/POST /api/promos` | Promo tiers `{promos: [{pesos, minutes}, ...]}` (up to 10) and the resulting rate card. |
| `GET/POST /api/appearance` | `title` (the Piso WiFi name), `dashboard_mode` (`auto`/`dark`/`light`), `dashboard_accent`. |
| `GET /api/sales?days=14` | Totals (today, 7 days, 30 days, all time), daily takings, coin mix, unassigned coins. |
| `GET /api/coins?limit=50` | Recent coins, newest first. |
| `POST /api/coins/assign` | Give an unassigned coin to a customer: `{coin_id, mac}`. |
| `GET /api/clients` | openNDS's client list (`ndsctl json`), without tokens. |
| `POST /api/vouchers` | Create a batch: `{count, minutes, price}`. |
| `GET /api/voucher-batches` | Batches with used and cancelled counts. |
| `GET /api/vouchers?batch=` | The codes in one batch. |
| `POST /api/vouchers/void` | Cancel every unused code in a batch: `{batch}`. |
| `POST /api/test-credit`, `POST /api/session-test` | Left from the first build kit, for testing only. |

Example, from the Pi:

```sh
set -a; . /etc/pisowifi.env; set +a
curl -s http://127.0.0.1:8090/api/status -H "X-Admin-Token: $PISOWIFI_ADMIN_TOKEN"
```

## Customer portal (`portal.py`)

The portal never trusts the phone to say who it is: it looks up the phone's MAC from the
request's IP in `/proc/net/arp`. POST routes need the header `X-Requested-With: piso-portal`.

| Method and path | What it does |
|---|---|
| `GET /` and any other path | The customer page (also answers captive-portal checks). |
| `GET /api/state` | This phone's state (from `/api/customer`). |
| `POST /api/insert-coin`, `/api/connect`, `/api/pause`, `/api/resume` | The same actions for this phone. |
| `POST /api/redeem` | `{code}` for this phone. |

## Owner dashboard (`dashboard.py`)

Password login (hashed in `/etc/pisowifi-dashboard.passwd`, set with
`dashboard.py set-password`). POST routes need the page's CSRF token in `X-CSRF-Token`.

| Method and path | What it does |
|---|---|
| `GET/POST /login`, `POST /logout` | Sign in and out. Five wrong passwords lock a device out for 15 minutes. |
| `GET /` | The dashboard (Sales, Customers, Vouchers, Settings tabs). |
| `GET /api/overview` | Everything the dashboard shows, in one call. |
| `POST /api/settings`, `/api/promos`, `/api/vouchers`, `/api/vouchers/void`, `/api/coins/assign`, `/api/appearance` | Passed through to the credit service, with only the allowed fields. |
| `GET /vouchers/<batch>/print` | Printable voucher cards for the unused codes in a batch. |
