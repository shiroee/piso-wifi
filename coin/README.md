# Coin reader

`coin_reader.py` watches the Allan 1239/1239A coin signal, counts the pulses
for each coin, and posts the coin to `pisowifi-creditd` at `/api/coin`.

## Measured signal (2026-10-16)
- Connected through the Allan board hat (the acceptor's COIN wire is not wired to GPIO directly).
- gpiochip0 line 12 = PA12 = header pin 3. Internal pull-up on; active-low.
- About 50 ms low per pulse, 150 ms apart (acceptor speed switch on Medium).
- 1 pulse = ₱1. Verified with ₱1, ₱5, ₱10 and ₱20 coins.
- Keep the `i2c0` overlay disabled: PA12 is also an I2C pin.

Still to do before unattended use: with the reader running, measure header pin 3
to GND with a multimeter. It must stay at or below 3.3 V. If it reads higher,
disconnect the hat.

## How it counts
- A pulse is a low lasting 15–150 ms; shorter blips are noise, longer lows are logged as a wiring problem.
- A coin ends after 0.5 s with no new pulse, so a ₱20 (about 2.9 s of pulses) is one coin.
- Each coin gets a `coin_id` and is written to `/var/lib/pisowifi/coin-spool.jsonl` before it is sent. It is removed only after the credit service accepts it, so a crash, reboot or outage doesn't lose money.
- The credit service applies the rate (`PISOWIFI_MINUTES_PER_PESO`) and decides which customer the coin belongs to.

## Commands
```sh
python3 -m unittest test_coin_reader         # tests, no hardware needed
sudo python3 coin_reader.py --dry-run        # log coins, send nothing
python3 coin_reader.py --replay capture.txt  # decode a saved gpiomon session
```
To make a capture: `gpiomon -c gpiochip0 --bias=pull-up 12 > capture.txt` (stop the coin-reader service first, since only one program can watch the line).

## Coin slot light
With `COIN_LED_LINE=11` in `/etc/pisowifi.env` (PA11, header pin 5 on the Allan
board, its "CS LED" connector), the coin reader lights the coin slot while a
customer's Insert Coin window is open, double-blinks it for every counted coin, and
turns it off otherwise. It asks the credit service about the window twice a second,
so the light comes on within half a second of tapping Insert Coin. PA11 is also an
I2C pin: keep the `i2c0` overlay disabled.
