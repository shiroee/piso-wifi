# Hardware

## Parts

| Part | Used here | Notes |
|---|---|---|
| Computer | Orange Pi PC (Allwinner H3, 1 GB) | Armbian Community, Debian 13 Trixie, armhf |
| Coin acceptor | Allan Universal Coinslot 1239 / 1239A | Programmed for ₱1, ₱5, ₱10, ₱20; speed switch on **Medium** |
| Interface board | Allan Custom Board (hat) for Orange Pi PC | Plugs onto the 40-pin header; 12 V in, step-down for the Pi, fan, relay, coin and CS LED connectors |
| Customer network | USB Ethernet adapter (`cdc_ether`, shows as `enx…`) | Reports `state UNKNOWN`; openNDS needs a one-line fix (see `opennds-notes.md`) |
| Access point | TP-Link EAP110-Outdoor | Must hand phones straight to the Pi (Access Point / Bridge mode, no DHCP, no NAT) |
| Power | 12 V supply into the hat | The hat feeds the Pi; use one power source only |

## Pins (measured on the real board)

| Function | Header pin | SoC pin | `gpiochip0` line | Details |
|---|---|---|---|---|
| Coin signal | **3** | PA12 | **12** | Input, internal pull-up, active-low. About 50 ms per pulse, 150 ms apart. 1 pulse = ₱1. |
| Coin slot light | **5** | PA11 | **11** | Output, active-high. The hat's **CS LED** connector reads ~11.6 V when on, ~6 V (floating) when off. |

Both pins are also I2C0 pins, so the `i2c0` overlay must stay **off** (`armbian-config`,
`/boot/armbianEnv.txt`).

Line number = bank × 32 + pin, with PA=0, PB=1, PC=2, PD=3 … PG=6 (so PA12 = 12, PC4 = 68).
On the H3 only the PA and PG banks can report edges, so `gpiomon` refuses PC/PD lines.

Testing a pin by hand (stop the coin reader first, it holds lines 11 and 12):

```sh
systemctl stop coin-reader
gpiomon -c gpiochip0 --bias=pull-up 12      # drop coins: one falling edge per peso
gpioset -c gpiochip0 11=1                   # coin slot light on until Ctrl+C
systemctl start coin-reader
```

## Measured coin pulses

| Coin | Falling edges | Train length |
|---|---|---|
| ₱1 | 1 | 50 ms |
| ₱5 | 5 | 0.65 s |
| ₱10 | 10 | 1.4 s |
| ₱20 | 20 | 2.9 s |

The reader ends a coin after 0.5 s of silence and ignores lows shorter than 15 ms or
longer than 150 ms. Real captures are in `coin/test_coin_reader.py`.

## Safety

- **Never connect 5 V or 12 V to a GPIO pin.** The Orange Pi's pins take 3.3 V only. The coin
  acceptor and the slot light run at 12 V and must go through the hat.
- **Don't turn the hat's blue adjuster screw.** It sets the voltage that powers the Pi.
- Before leaving the machine unattended, measure header pin 3 to GND while the coin reader
  runs: it must stay at or below 3.3 V.
- The relay on the hat (SRD-12VDC) isn't used by this software.

## Network topology

```
Internet -- home router (192.168.100.1)
               |
             end0  Orange Pi PC  enx00e099004abb (10.20.0.1/24)
                                        |
                          access point in Bridge / AP mode
                                        |
                                   customers' phones (10.20.0.50-200)
```

The access point must not act as a router. If it does, every phone hides behind one
address and **all phones share one customer's time** (see `troubleshooting.md`).
