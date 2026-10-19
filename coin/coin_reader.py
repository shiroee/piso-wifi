#!/usr/bin/env python3
"""PisoWiFi coin reader for the Orange Pi PC.

Watches the coin acceptor's signal on one GPIO line, turns each pulse train
into a coin, and posts the coin to pisowifi-creditd's /api/coin route
(http://127.0.0.1:$PISOWIFI_PORT/api/coin, authenticated with
PISOWIFI_ADMIN_TOKEN; both come from /etc/pisowifi.env). The credit service
applies the rate and decides which customer gets the coin.

Optionally drives the coin slot's light (COIN_LED_LINE, e.g. 11 = PA11 = header
pin 5 on the Allan board): on while a customer's Insert Coin window is open,
a quick double blink for each counted coin, off otherwise.

Each coin is written to a spool file on disk *before* it is sent, and removed
only after the credit service accepts it, so a crash, reboot or credit-service
outage can't silently lose a customer's money.

Signal measured on this build (Allan 1239 acceptor, speed switch on Medium,
connected through the board hat):

    gpiochip0 line 12 = PA12 = header pin 3
    active-low, ~50 ms low pulses, 150 ms apart, 1 pulse = PHP 1

Usage:
    coin_reader.py --dry-run              # log coins, send nothing
    coin_reader.py                        # send coins to pisowifi-creditd
    coin_reader.py --replay capture.txt   # decode a saved gpiomon capture
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import logging
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

log = logging.getLogger("coin-reader")

NS_PER_MS = 1_000_000
FALLING = "falling"
RISING = "rising"

DEFAULT_SPOOL = Path("/var/lib/pisowifi/coin-spool.jsonl")
DRY_RUN_SPOOL = Path(tempfile.gettempdir()) / "coin-reader-dry-run.jsonl"


# ---------------------------------------------------------------------------
# Pulse decoding: pure logic with no GPIO dependency, so it is unit-testable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecoderConfig:
    min_pulse_ms: float = 15.0  # shorter lows are noise, not pulses
    max_pulse_ms: float = 150.0  # longer lows are not coin pulses
    coin_gap_ms: float = 500.0  # this much silence after a pulse ends a coin


class PulseDecoder:
    """Turns edges on an active-low line into per-coin pulse counts.

    A pulse is a falling edge followed by a rising edge with a low time inside
    [min_pulse_ms, max_pulse_ms]. A coin is complete once coin_gap_ms passes
    after the last pulse ended without a new pulse starting.
    """

    def __init__(self, config: DecoderConfig | None = None) -> None:
        self.config = config or DecoderConfig()
        self.rejected = 0
        self._pulses = 0
        self._low_since: int | None = None
        self._last_pulse_end = 0
        self._stuck_warned = False

    def feed(self, edge: str, ts_ns: int) -> None:
        """Record one edge. ts_ns must use the same clock as poll()."""
        if edge == FALLING:
            self._low_since = ts_ns
            return
        if edge != RISING:
            raise ValueError(f"unknown edge type: {edge!r}")
        if self._low_since is None:
            return  # no pulse in progress, e.g. the pull-up switching on at start
        width_ms = (ts_ns - self._low_since) / NS_PER_MS
        self._low_since = None
        self._stuck_warned = False
        cfg = self.config
        if cfg.min_pulse_ms <= width_ms <= cfg.max_pulse_ms:
            self._pulses += 1
            self._last_pulse_end = ts_ns
            log.debug("pulse %d: %.1f ms", self._pulses, width_ms)
        else:
            self.rejected += 1
            log.warning(
                "ignored a %.1f ms low (a pulse is %.0f-%.0f ms)",
                width_ms,
                cfg.min_pulse_ms,
                cfg.max_pulse_ms,
            )

    def poll(self, now_ns: int) -> int:
        """Return the pulse count of a coin that has just finished, else 0."""
        cfg = self.config
        if self._low_since is not None:
            if (now_ns - self._low_since) / NS_PER_MS <= cfg.max_pulse_ms:
                return 0  # a pulse may be in progress: don't split the coin
            if not self._stuck_warned:
                log.warning(
                    "coin signal has been low for over %.0f ms; check the wiring",
                    cfg.max_pulse_ms,
                )
                self._stuck_warned = True
        if self._pulses and (now_ns - self._last_pulse_end) / NS_PER_MS >= cfg.coin_gap_ms:
            return self.flush()
        return 0

    def flush(self) -> int:
        """Return and clear any counted pulses (used at shutdown)."""
        pulses, self._pulses = self._pulses, 0
        return pulses


def decode_events(events: Iterable[tuple[str, int]], config: DecoderConfig) -> list[int]:
    """Decode a finished list of (edge, ts_ns) events into per-coin pulse counts."""
    decoder = PulseDecoder(config)
    coins: list[int] = []
    last_ts = None
    for edge, ts_ns in events:
        pulses = decoder.poll(ts_ns)  # did a coin end before this edge?
        if pulses:
            coins.append(pulses)
        decoder.feed(edge, ts_ns)
        last_ts = ts_ns
    if last_ts is not None:
        wait_ms = max(config.coin_gap_ms, config.max_pulse_ms) + 1
        pulses = decoder.poll(last_ts + int(wait_ms * NS_PER_MS))
        if pulses:
            coins.append(pulses)
    return coins


def parse_gpiomon(lines: Iterable[str], line_offset: int) -> Iterator[tuple[str, int]]:
    """Yield (edge, ts_ns) for one line from gpiomon v2 output.

    Anything that isn't an event line (prompts, notes like "5 pesos") is skipped,
    so a whole pasted terminal session can be replayed.
    """
    for raw in lines:
        parts = raw.split()
        if len(parts) < 4 or parts[1] not in (FALLING, RISING):
            continue
        if parts[3] != str(line_offset):
            continue
        try:
            yield parts[1], _parse_ts_ns(parts[0])
        except ValueError:
            continue


def _parse_ts_ns(text: str) -> int:
    seconds, _, fraction = text.partition(".")
    return int(seconds) * 1_000_000_000 + int(fraction.ljust(9, "0")[:9])


# ---------------------------------------------------------------------------
# Coin records and delivery to the credit service.
# ---------------------------------------------------------------------------


def make_record(pulses: int, pesos_per_pulse: int = 1) -> dict:
    """Build the JSON payload for one coin."""
    return {
        "coin_id": str(uuid.uuid4()),  # lets the credit service ignore retries
        "pesos": pulses * pesos_per_pulse,
        "pulses": pulses,
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class SinkError(Exception):
    """The credit service did not accept a coin."""


class Sink(Protocol):
    def send(self, record: dict) -> dict | None: ...


class HttpSink:
    """POSTs each coin as JSON. Any non-2xx reply or network error is a failure."""

    def __init__(self, url: str, token: str | None = None, timeout: float = 3.0) -> None:
        self.url = url
        self.timeout = timeout
        self._headers = {"Content-Type": "application/json"}
        if token:
            self._headers["X-Admin-Token"] = token  # never logged

    def send(self, record: dict) -> dict:
        """Returns the service's JSON reply ({} if it isn't JSON)."""
        request = urllib.request.Request(
            self.url,
            data=json.dumps(record).encode("utf-8"),
            method="POST",
            headers=self._headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise SinkError(f"HTTP {exc.code} from {self.url}") from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise SinkError(f"{self.url}: {exc}") from exc
        if not 200 <= status < 300:
            raise SinkError(f"HTTP {status} from {self.url}")
        try:
            reply = json.loads(body)
        except ValueError:
            return {}
        return reply if isinstance(reply, dict) else {}


class LogSink:
    """Dry-run sink: logs coins instead of sending them."""

    def send(self, record: dict) -> None:
        log.info("[dry run] would send %s", json.dumps(record))


def describe_acceptance(record: dict, reply: dict | None) -> str:
    """One log line saying what the credit service did with a coin."""
    pesos = record.get("pesos")
    if not reply or "minutes" not in reply:
        return f"coin {record.get('coin_id')} accepted: PHP {pesos}"
    who = reply.get("mac") or "nobody (no customer had tapped Insert Coin)"
    again = " (already recorded)" if reply.get("duplicate") else ""
    return f"coin accepted{again}: PHP {pesos} = {reply['minutes']} min for {who}"


class Spool:
    """JSON-lines file of coins the credit service has not accepted yet."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def pending(self) -> list[dict]:
        return self._read()[0]

    def flush(self, sink: Sink) -> int:
        """Send waiting coins oldest first; stop at the first failure."""
        records, dropped_bad_lines = self._read()
        sent = 0
        for record in records:
            try:
                reply = sink.send(record)
            except SinkError as exc:
                log.warning(
                    "credit service unavailable, %d coin(s) waiting: %s",
                    len(records) - sent,
                    exc,
                )
                break
            sent += 1
            log.info("%s", describe_acceptance(record, reply))
        if sent or dropped_bad_lines:
            try:
                self._rewrite(records[sent:])
            except OSError as exc:
                # The sent coins stay in the file and will be sent again;
                # the credit service should ignore a repeated coin_id.
                log.error("can't update spool %s: %s", self.path, exc)
        return sent

    def _read(self) -> tuple[list[dict], bool]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return [], False
        records, dropped = [], False
        for line in lines:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Most likely a write cut off by a power loss.
                log.error("dropping unreadable spool line: %r", line)
                dropped = True
        return records, dropped

    def _rewrite(self, records: list[dict]) -> None:
        if not records:
            self.path.unlink(missing_ok=True)
            return
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".coin-spool-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise


def deliver(record: dict, spool: Spool, sink: Sink) -> None:
    """Save the coin to disk first, then try to send everything waiting."""
    log.info("coin detected: PHP %d (%d pulses)", record["pesos"], record["pulses"])
    try:
        spool.add(record)
    except OSError as exc:
        log.error("can't write spool %s (%s); sending without a backup", spool.path, exc)
        try:
            sink.send(record)
        except SinkError as send_exc:
            log.critical("COIN NOT RECORDED: %s (%s)", json.dumps(record), send_exc)
        return
    spool.flush(sink)


# ---------------------------------------------------------------------------
# Coin slot light.
# ---------------------------------------------------------------------------

BLINK_STEP = 0.15  # seconds per off/on step of the coin blink
BLINK_STEPS = 4  # off, on, off, on


class LedLogic:
    """Decides whether the coin slot light is on: on while a customer's Insert
    Coin window is open, a double blink for each counted coin, off otherwise.
    Pure logic (no GPIO), so it is unit-testable."""

    def __init__(self) -> None:
        self.window_open = False
        self._blink_start: float | None = None

    def coin(self, now: float) -> None:
        self._blink_start = now

    def blinking(self, now: float) -> bool:
        return self._blink_start is not None and now - self._blink_start < BLINK_STEP * BLINK_STEPS

    def lit(self, now: float) -> bool:
        if self._blink_start is not None:
            elapsed = now - self._blink_start
            if elapsed < BLINK_STEP * BLINK_STEPS:
                return int(elapsed / BLINK_STEP) % 2 == 1  # starts with "off" so the blink shows
            self._blink_start = None
        return self.window_open


class WindowWatcher:
    """Asks the credit service whether an Insert Coin window is open, at most
    every `interval` seconds. Any problem counts as closed (light off)."""

    def __init__(self, url: str, token: str | None, interval: float = 0.5, timeout: float = 1.0) -> None:
        self.url = url
        self.interval = interval
        self.timeout = timeout
        self._headers = {"Accept": "application/json"}
        if token:
            self._headers["X-Admin-Token"] = token  # never logged
        self._next = 0.0
        self._open = False
        self._warned = False

    def poll(self, now: float) -> bool:
        if now < self._next:
            return self._open
        self._next = now + self.interval
        try:
            request = urllib.request.Request(self.url, headers=self._headers)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read() or b"{}")
            self._open = isinstance(reply, dict) and reply.get("active") is True
            self._warned = False
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            if not self._warned:
                log.warning("can't read the Insert Coin window (%s); coin slot light stays off", exc)
                self._warned = True
            self._open = False
        return self._open


def window_url(coin_url: str) -> str:
    """The credit service's Insert Coin status route, next to its coin route."""
    parts = urllib.parse.urlsplit(coin_url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/api/insert-coin", "", ""))


# ---------------------------------------------------------------------------
# GPIO loop and command line.
# ---------------------------------------------------------------------------


def make_spool(args: argparse.Namespace) -> Spool:
    # A dry run must never drain the real spool into a sink that discards coins.
    return Spool(DRY_RUN_SPOOL if args.dry_run else args.spool)


def run(args: argparse.Namespace) -> int:
    try:
        import gpiod
        from gpiod.line import Bias, Direction, Edge, Value
    except ImportError:
        log.error("libgpiod v2 Python bindings not found (apt install python3-libgpiod)")
        return 1
    if not hasattr(gpiod, "request_lines"):
        log.error("found libgpiod v1 Python bindings; this script needs v2")
        return 1

    config = DecoderConfig(args.min_pulse_ms, args.max_pulse_ms, args.coin_gap_ms)
    decoder = PulseDecoder(config)
    sink: Sink = LogSink() if args.dry_run else HttpSink(args.url, args.token, args.timeout)
    spool = make_spool(args)

    stopping = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    lines = {
        args.line: gpiod.LineSettings(
            direction=Direction.INPUT,
            edge_detection=Edge.BOTH,
            bias=Bias.PULL_UP,
        )
    }
    if args.led_line is not None:
        lines[args.led_line] = gpiod.LineSettings(
            direction=Direction.OUTPUT,
            output_value=Value.INACTIVE,
            active_low=args.led_active_low,
        )
    led = LedLogic()
    watcher = WindowWatcher(window_url(args.url), args.token) if args.led_line is not None and not args.dry_run else None
    try:
        request = gpiod.request_lines(
            args.chip,
            consumer="coin-reader",
            config=lines,
            # Room for many coins' edges if a slow HTTP send blocks the loop.
            event_buffer_size=1024,
        )
    except OSError as exc:
        log.error(
            "can't open %s line %s: %s (is gpiomon or another program using it?)",
            args.chip,
            " and ".join(str(n) for n in lines),
            exc,
        )
        return 1

    target = "dry run" if args.dry_run else f"sending to {args.url}"
    log.info("watching %s line %d (%s)", args.chip, args.line, target)
    if args.led_line is not None:
        log.info("coin slot light on line %d%s", args.led_line, " (active-low)" if args.led_active_low else "")
    led_on = False
    retry_ns = int(args.retry_seconds * 1_000_000_000)
    next_retry = time.monotonic_ns()
    last_seqno = None

    with request:
        while not stopping:
            try:
                wait_ms = 50 if led.blinking(time.monotonic()) else 100  # finer steps while blinking
                ready = request.wait_edge_events(timedelta(milliseconds=wait_ms))
            except InterruptedError:
                continue
            if ready:
                for event in request.read_edge_events():
                    if last_seqno is not None and event.line_seqno != last_seqno + 1:
                        log.error(
                            "missed %d edge(s); a coin may be undercounted",
                            event.line_seqno - last_seqno - 1,
                        )
                    last_seqno = event.line_seqno
                    falling = event.event_type == gpiod.EdgeEvent.Type.FALLING_EDGE
                    # Event timestamps are CLOCK_MONOTONIC, same as time.monotonic_ns().
                    decoder.feed(FALLING if falling else RISING, event.timestamp_ns)

            now = time.monotonic_ns()
            pulses = decoder.poll(now)
            if pulses:
                led.coin(time.monotonic())  # blink first; sending can take a moment
                deliver(make_record(pulses, args.pesos_per_pulse), spool, sink)
                next_retry = now + retry_ns
            elif now >= next_retry:
                spool.flush(sink)
                next_retry = now + retry_ns

            if args.led_line is not None:
                seconds = time.monotonic()
                if watcher:
                    led.window_open = watcher.poll(seconds)
                want = led.lit(seconds)
                if want != led_on:
                    request.set_value(args.led_line, Value.ACTIVE if want else Value.INACTIVE)
                    led_on = want

        pulses = decoder.flush()
        if pulses:
            log.info("stopping mid-coin; saving the %d pulse(s) counted so far", pulses)
            deliver(make_record(pulses, args.pesos_per_pulse), spool, sink)
        if args.led_line is not None:
            request.set_value(args.led_line, Value.INACTIVE)

    log.info("stopped")
    return 0


def default_url() -> str:
    """pisowifi-creditd's coin route on this board (it binds to localhost)."""
    return f"http://127.0.0.1:{os.environ.get('PISOWIFI_PORT') or '8090'}/api/coin"


def _usable_token(token: str | None) -> bool:
    # Same rule pisowifi-creditd applies before it accepts a request.
    return bool(token) and not token.startswith("REPLACE_") and len(token) >= 24


def _valid_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PisoWiFi coin reader (Orange Pi PC)")
    p.add_argument("--chip", default="/dev/gpiochip0", help="GPIO chip (default: %(default)s)")
    p.add_argument("--line", type=int, default=12, help="line offset (default 12 = PA12 = header pin 3)")
    led_default = os.environ.get("COIN_LED_LINE", "").strip()
    p.add_argument(
        "--led-line",
        type=int,
        default=int(led_default) if led_default.isdigit() else None,
        help="line for the coin slot light, e.g. 11 = PA11 = header pin 5 (default: COIN_LED_LINE, off if unset)",
    )
    p.add_argument(
        "--led-active-low",
        action="store_true",
        default=os.environ.get("COIN_LED_ACTIVE_LOW", "").strip().lower() in ("1", "true", "yes"),
        help="the light turns on when the line is low (default: COIN_LED_ACTIVE_LOW)",
    )
    p.add_argument(
        "--url",
        default=os.environ.get("COIN_READER_URL") or default_url(),
        help="coin route of the credit service (default: %(default)s)",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("PISOWIFI_ADMIN_TOKEN") or None,
        help="admin token for the credit service (default: PISOWIFI_ADMIN_TOKEN)",
    )
    p.add_argument("--dry-run", action="store_true", help="log coins instead of sending them")
    p.add_argument("--replay", type=Path, metavar="FILE", help="decode a saved gpiomon capture and exit")
    p.add_argument("--pesos-per-pulse", type=int, default=1)
    p.add_argument("--min-pulse-ms", type=float, default=15.0)
    p.add_argument("--max-pulse-ms", type=float, default=150.0)
    p.add_argument("--coin-gap-ms", type=float, default=500.0)
    p.add_argument("--spool", type=Path, default=DEFAULT_SPOOL, help="default: %(default)s")
    p.add_argument("--retry-seconds", type=float, default=15.0, help="how often to resend waiting coins")
    p.add_argument("--timeout", type=float, default=3.0, help="HTTP timeout in seconds")
    p.add_argument("-v", "--verbose", action="store_true", help="log every pulse")
    args = p.parse_args(argv)

    if not args.dry_run and not args.replay:
        if not _valid_url(args.url):
            p.error(f"not a valid http(s) URL: {args.url!r}")
        if not _usable_token(args.token):
            p.error(
                "set a long PISOWIFI_ADMIN_TOKEN in /etc/pisowifi.env "
                "(the credit service refuses requests without it)"
            )
    if args.led_line is not None and (args.led_line < 0 or args.led_line == args.line):
        p.error("--led-line must be a different line from the coin line")
    if not 0 < args.min_pulse_ms < args.max_pulse_ms:
        p.error("need 0 < --min-pulse-ms < --max-pulse-ms")
    if args.coin_gap_ms <= 0 or args.retry_seconds <= 0 or args.timeout <= 0:
        p.error("--coin-gap-ms, --retry-seconds and --timeout must be positive")
    if args.pesos_per_pulse < 1:
        p.error("--pesos-per-pulse must be at least 1")
    return args


def replay(args: argparse.Namespace) -> int:
    try:
        lines = args.replay.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.error("can't read %s: %s", args.replay, exc)
        return 1
    config = DecoderConfig(args.min_pulse_ms, args.max_pulse_ms, args.coin_gap_ms)
    coins = decode_events(parse_gpiomon(lines, args.line), config)
    if not coins:
        print(f"no coins found on line {args.line}")
    for number, pulses in enumerate(coins, start=1):
        pesos = pulses * args.pesos_per_pulse
        unit = "pulse" if pulses == 1 else "pulses"
        print(f"coin {number}: {pulses} {unit} = PHP {pesos}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    if args.replay:
        return replay(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
