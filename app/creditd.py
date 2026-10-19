#!/usr/bin/env python3
"""Local Piso WiFi credit/session prototype.

Not a production payment service. coin/coin_reader.py posts each coin to
/api/coin, which records it exactly once per coin_id and credits it to the
customer whose Insert Coin window is open (ledger only; no access is granted).
Test-credit endpoint records ledger entries only. openNDS authorization is
disabled by default. Report and settings routes back admin/dashboard.py.

With PISOWIFI_ENABLE_NDS_AUTH=true, /api/connect turns a customer's coin credit
into internet time with `ndsctl auth`, and a background loop keeps openNDS in
step with the ledger (re-authorizing paid time after a reboot, and starting
time for coins nobody started). portal/portal.py is the customer page.

Also: per-customer speed limits, promo rates (bigger amounts buy bonus time),
pausing time, printed vouchers, a limit on Insert Coin taps without coins,
and giving an unassigned coin to a customer from the dashboard.
"""
import os, sqlite3, time, subprocess, re, uuid, json, sys, threading, secrets
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify

DB=os.environ.get("PISOWIFI_DB","/var/lib/pisowifi/credits.sqlite3")
TOKEN=os.environ.get("PISOWIFI_ADMIN_TOKEN","REPLACE_WITH_LONG_RANDOM_SECRET")
MINUTES=int(os.environ.get("PISOWIFI_MINUTES_PER_PESO","5"))
BIND=os.environ.get("PISOWIFI_BIND","127.0.0.1")
PORT=int(os.environ.get("PISOWIFI_PORT","8090"))
ENABLE_NDS=os.environ.get("PISOWIFI_ENABLE_NDS_AUTH","false").lower()=="true"
COIN_WINDOW=int(os.environ.get("PISOWIFI_COIN_WINDOW_SECONDS","60"))
COIN_GRACE=int(os.environ.get("PISOWIFI_COIN_GRACE_SECONDS","5"))
DOWNLOAD_KBPS=int(os.environ.get("PISOWIFI_DOWNLOAD_KBPS","0"))  # 0 = no limit
UPLOAD_KBPS=int(os.environ.get("PISOWIFI_UPLOAD_KBPS","0"))
TITLE=os.environ.get("PISOWIFI_PORTAL_NAME","Piso WiFi").strip() or "Piso WiFi"
THEME_MODES=("auto","dark","light")
THEME_ACCENTS=("gold","blue","green","purple","red")
app=Flask(__name__)

MAX_PROMOS=10
EMPTY_TAPS_ALLOWED=3          # Insert Coin windows without a coin...
EMPTY_TAP_PERIOD=600          # ...within this many seconds...
TAP_BLOCK_SECONDS=300         # ...then that phone waits this long.
VOUCHER_ALPHABET="ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread
VOUCHER_LENGTH=8
REDEEM_FAILS_PER_PHONE=5      # wrong codes before a phone is locked out...
REDEEM_LOCK_SECONDS=600       # ...for this long
REDEEM_FAILS_PER_MINUTE=30    # wrong codes from all phones before everyone waits a minute

def db():
    os.makedirs(os.path.dirname(DB),exist_ok=True)
    c=sqlite3.connect(DB,timeout=10)
    c.row_factory=sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS ledger(
      id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL,
      pesos INTEGER NOT NULL DEFAULT 0, minutes INTEGER NOT NULL DEFAULT 0,
      mac TEXT NOT NULL DEFAULT '', expires INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '')""")
    # One row per physical coin; the primary key makes coin_reader.py retries harmless.
    c.execute("""CREATE TABLE IF NOT EXISTS coin(
      coin_id TEXT PRIMARY KEY, ledger_id INTEGER NOT NULL, ts INTEGER NOT NULL,
      pesos INTEGER NOT NULL, pulses INTEGER NOT NULL DEFAULT 0, detected_at TEXT NOT NULL DEFAULT '')""")
    # One coinslot, so at most one Insert Coin window (single row, id=1).
    c.execute("""CREATE TABLE IF NOT EXISTS coin_window(
      id INTEGER PRIMARY KEY CHECK(id=1), mac TEXT NOT NULL, opened INTEGER NOT NULL,
      expires INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0)""")
    # Settings changed from the dashboard; they override /etc/pisowifi.env and survive restarts.
    c.execute("""CREATE TABLE IF NOT EXISTS setting(
      name TEXT PRIMARY KEY, value INTEGER NOT NULL, ts INTEGER NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ledger_kind_mac ON ledger(kind,mac)")
    # Current internet time per customer. The ledger keeps the history.
    c.execute("""CREATE TABLE IF NOT EXISTS access(
      mac TEXT PRIMARY KEY, expires INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0)""")
    # Promo tiers on top of the per-peso rate, e.g. 10 pesos = 70 minutes.
    c.execute("CREATE TABLE IF NOT EXISTS promo(pesos INTEGER PRIMARY KEY, minutes INTEGER NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS voucher(
      code TEXT PRIMARY KEY, batch TEXT NOT NULL, minutes INTEGER NOT NULL, price INTEGER NOT NULL DEFAULT 0,
      created INTEGER NOT NULL, used_by TEXT NOT NULL DEFAULT '', used_at INTEGER NOT NULL DEFAULT 0,
      void INTEGER NOT NULL DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS tap_strike(mac TEXT PRIMARY KEY, strikes INTEGER NOT NULL, last INTEGER NOT NULL)")
    # Text settings from the dashboard: the Piso WiFi name and the dashboard theme.
    c.execute("CREATE TABLE IF NOT EXISTS text_setting(name TEXT PRIMARY KEY, value TEXT NOT NULL, ts INTEGER NOT NULL)")
    if c.execute("PRAGMA user_version").fetchone()[0]<1:
        # v1: session end times move from the ledger into `access`.
        c.execute("INSERT OR IGNORE INTO access(mac,expires) SELECT mac, MAX(expires) FROM ledger "
                  "WHERE kind='paid_session' AND mac!='' GROUP BY mac")
        c.execute("PRAGMA user_version=1")
    c.commit()
    return c

def auth(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        if TOKEN.startswith("REPLACE_") or len(TOKEN)<24 or request.headers.get("X-Admin-Token","") != TOKEN:
            return jsonify(error="Set a long admin token in /etc/pisowifi.env and send X-Admin-Token."),401
        return fn(*a,**kw)
    return wrapped

def valid_mac(mac):
    return isinstance(mac,str) and re.fullmatch(r"(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}",mac) is not None

def is_int(v):
    return isinstance(v,int) and not isinstance(v,bool)

def norm_coin_id(v):
    """coin_reader.py sends a UUID. Returns it normalized, or None if invalid."""
    if not isinstance(v,str) or len(v)>64: return None
    try: return str(uuid.UUID(v))
    except ValueError: return None

# name -> (allowed min, allowed max). Defaults come from /etc/pisowifi.env.
SETTING_LIMITS={"minutes_per_peso":(1,1440),"coin_window_seconds":(15,600),"coin_grace_seconds":(3,60),
                "download_kbps":(0,1000000),"upload_kbps":(0,1000000)}

def env_defaults():
    return {"minutes_per_peso":MINUTES,"coin_window_seconds":COIN_WINDOW,"coin_grace_seconds":COIN_GRACE,
            "download_kbps":DOWNLOAD_KBPS,"upload_kbps":UPLOAD_KBPS}

def settings(c):
    s=env_defaults()
    for r in c.execute("SELECT name,value FROM setting"):
        if r["name"] in s: s[r["name"]]=r["value"]
    return s

def save_setting(c,name,value,now):
    c.execute("INSERT OR REPLACE INTO setting(name,value,ts) VALUES(?,?,?)",(name,value,now))
    c.execute("INSERT INTO ledger(ts,kind,minutes,note) VALUES(?,?,?,?)",(now,"setting",value,name))

def slot_holder(c,now):
    """The window that holds the coinslot: open, or ended less than the grace
    period ago, so a coin still being counted goes to the right customer."""
    return c.execute("SELECT mac,opened,expires,closed FROM coin_window WHERE id=1 AND expires+?>?",
                     (settings(c)["coin_grace_seconds"],now)).fetchone()

def window_totals(c,mac,opened):
    r=c.execute("SELECT COALESCE(SUM(pesos),0) p, COALESCE(SUM(minutes),0) m FROM ledger WHERE kind='coin' AND mac=? AND ts>=?",
                (mac,opened)).fetchone()
    return r["p"],r["m"]

def promos(c):
    return [(r["pesos"],r["minutes"]) for r in c.execute("SELECT pesos,minutes FROM promo ORDER BY pesos")]

def price(pesos,base,tiers):
    """Most minutes `pesos` buys: each peso at the base rate, or any mix of promo tiers."""
    best=[0]*(max(0,pesos)+1)
    for a in range(1,len(best)):
        m=best[a-1]+base
        for p,mins in tiers:
            if p<=a and best[a-p]+mins>m: m=best[a-p]+mins
        best[a]=m
    return best[max(0,pesos)]

def price_now(c,pesos):
    return price(pesos,settings(c)["minutes_per_peso"],promos(c))

def rate_card(c):
    """What the portal lists: the usual coins plus every promo amount."""
    base,tiers=settings(c)["minutes_per_peso"],promos(c)
    amounts=sorted({1,5,10,20}|{p for p,_ in tiers})
    return [{"pesos":a,"minutes":price(a,base,tiers)} for a in amounts]

def access(c,mac):
    """(expires, paused_seconds). While paused, expires is meaningless."""
    r=c.execute("SELECT expires,paused FROM access WHERE mac=?",(mac,)).fetchone()
    return (r["expires"],r["paused"]) if r else (0,0)

def set_access(c,mac,expires,paused):
    c.execute("INSERT OR REPLACE INTO access(mac,expires,paused) VALUES(?,?,?)",(mac,expires,paused))

def ndsctl(*args):
    if not ENABLE_NDS:
        raise RuntimeError("openNDS authorization is disabled; keep test mode until tested.")
    # Fixed executable and argument array; no shell invocation.
    return subprocess.run(["ndsctl",*args],check=True,capture_output=True,text=True,timeout=5)

class NdsError(Exception):
    """openNDS did not do what we asked."""

def nds(*args):
    """Run ndsctl. Returns (ok_exit, output). Raises NdsError if ndsctl can't run."""
    try:
        r=subprocess.run(["ndsctl",*args],capture_output=True,text=True,timeout=5)
    except (FileNotFoundError,subprocess.SubprocessError) as e:
        raise NdsError(f"ndsctl {args[0]} could not run: {e}") from e
    return r.returncode==0,(r.stdout or "")+(r.stderr or "")

def nds_auth(mac,minutes,up=0,down=0):
    # Observed on openNDS 10.3.1: "Client <mac>,... authenticated." on success,
    # "Failed to authenticate client ..." when the client is already authenticated.
    # Argument order: sessiontimeout uploadrate downloadrate uploadquota downloadquota customstring.
    ok,out=nds("auth",mac,str(minutes),str(up),str(down),"0","0","piso")
    low=out.lower()
    return ok and "authenticated" in low and "failed" not in low

def nds_grant(c,mac,minutes):
    """Authorize mac for exactly `minutes` from now at the current speed limits,
    replacing any current session."""
    s=settings(c); up,down=s["upload_kbps"],s["download_kbps"]
    if nds_auth(mac,minutes,up,down): return
    nds("deauth",mac)  # an authenticated client must be deauthenticated before auth works again
    if not nds_auth(mac,minutes,up,down):
        raise NdsError(f"ndsctl auth {mac} {minutes} failed; is openNDS running?")

def nds_deauth(mac):
    ok,out=nds("deauth",mac)
    if not ok: raise NdsError(f"ndsctl deauth {mac} failed: {out.strip()[:120]}")

def parse_ts(text):
    try: return int(datetime.fromisoformat(text).timestamp())
    except (TypeError,ValueError,OverflowError): return None

CREDIT_SQL=("SUM(CASE WHEN kind IN ('coin','voucher') THEN minutes ELSE -minutes END)",
            "kind IN ('coin','voucher','paid_session')")

def balance(c,mac):
    """Credited minutes (coins and vouchers) not yet turned into internet time."""
    r=c.execute(f"SELECT COALESCE({CREDIT_SQL[0]},0) b FROM ledger WHERE mac=? AND {CREDIT_SQL[1]}",(mac,)).fetchone()
    return max(0,r["b"])

def paid_until(c,mac):
    expires,paused=access(c,mac)
    return 0 if paused else expires

def apply_balance(c,mac,now):
    """Turn all waiting credit into internet time, added on top of any time left
    (buying more also ends a pause). Call inside a write transaction; raises
    NdsError (nothing recorded) on failure."""
    credit=balance(c,mac)
    if credit<=0: return None
    expires,paused=access(c,mac)
    have=paused if paused else max(0,expires-now)
    total=-(-(have+credit*60)//60)  # whole minutes for openNDS, rounded up
    nds_grant(c,mac,total)
    expires=now+total*60
    set_access(c,mac,expires,0)
    c.execute("INSERT INTO ledger(ts,kind,minutes,mac,expires,note) VALUES(?,?,?,?,?,?)",
              (now,"paid_session",credit,mac,expires,"credit turned into internet time"))
    return {"minutes_added":credit,"expires":expires,"seconds_left":expires-now}

def try_start(c,mac,now):
    """Start waiting credit right away if internet access is on; otherwise the
    credit waits (and the reconcile loop retries). Never raises."""
    if not ENABLE_NDS: return None
    try: return apply_balance(c,mac,now)
    except NdsError: return None

def reconcile(now):
    """Start time for credit nobody started, and re-authorize paid time openNDS
    has forgotten (after a reboot) or dropped (idle timeout)."""
    done={"started":[],"reauthorized":[],"errors":[]}
    if not ENABLE_NDS: return done
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        holder=slot_holder(c,now)
        waiting=c.execute(f"SELECT mac, {CREDIT_SQL[0]} b FROM ledger "
                          f"WHERE mac!='' AND {CREDIT_SQL[1]} GROUP BY mac HAVING b>0").fetchall()
        for r in waiting:
            if holder and holder["mac"]==r["mac"]: continue  # still inserting coins
            try:
                apply_balance(c,r["mac"],now); done["started"].append(r["mac"])
            except NdsError as e:
                done["errors"].append(str(e))
        started=set(done["started"])
        s=settings(c)
        for r in c.execute("SELECT mac, expires e FROM access WHERE paused=0 AND expires>?",(now+60,)).fetchall():
            if r["mac"] in started: continue
            try:
                # Fails harmlessly when the client is still authenticated.
                if nds_auth(r["mac"],-(-(r["e"]-now)//60),s["upload_kbps"],s["download_kbps"]):
                    done["reauthorized"].append(r["mac"])
            except NdsError as e:
                done["errors"].append(str(e))
    return done

def reconcile_forever(interval=20):
    while True:
        try:
            result=reconcile(int(time.time()))
            if any(result.values()): print("reconcile:",json.dumps(result),flush=True)
        except Exception as e:  # keep the loop alive; the next pass retries
            print(f"reconcile failed: {e}",file=sys.stderr,flush=True)
        time.sleep(interval)

@app.get("/health")
def health():
    return jsonify(ok=True, mode="prototype", gpio_enabled=False, opennds_auth_enabled=ENABLE_NDS)

@app.get("/api/status")
@auth
def status():
    with db() as c:
        n=c.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]
        active=c.execute("SELECT COUNT(*) n FROM access WHERE paused=0 AND expires>?",(int(time.time()),)).fetchone()["n"]
        paused=c.execute("SELECT COUNT(*) n FROM access WHERE paused>0").fetchone()["n"]
        coins=c.execute("SELECT COALESCE(SUM(pesos),0) p FROM ledger WHERE kind='coin'").fetchone()["p"]
        unassigned=c.execute("SELECT COALESCE(SUM(pesos),0) p FROM ledger WHERE kind='coin' AND mac=''").fetchone()["p"]
        rate_=settings(c)["minutes_per_peso"]
    return jsonify(ok=True, minutes_per_peso=rate_, ledger_events=n, unexpired_ledger_sessions=active,
                   paused_sessions=paused, coin_pesos=coins, unassigned_coin_pesos=unassigned,
                   gpio_enabled=False, opennds_auth_enabled=ENABLE_NDS)

@app.post("/api/rate")
@auth
def rate():
    data=request.get_json(silent=True) or {}
    value=data.get("minutes_per_peso")
    if not isinstance(value,int) or not 1<=value<=1440:
        return jsonify(error="minutes_per_peso must be an integer 1..1440"),400
    with db() as c:
        # Saved as a setting, so the new rate survives a restart.
        save_setting(c,"minutes_per_peso",value,int(time.time()))
    return jsonify(ok=True,minutes_per_peso=value)

@app.post("/api/test-credit")
@auth
def test_credit():
    """Ledger-only test. It intentionally does not authorize a client."""
    data=request.get_json(silent=True) or {}
    pesos=data.get("pesos",1); mac=data.get("mac","")
    if not isinstance(pesos,int) or not 1<=pesos<=1000: return jsonify(error="pesos must be integer 1..1000"),400
    if mac and not valid_mac(mac): return jsonify(error="invalid MAC format"),400
    with db() as c:
        mins=price_now(c,pesos)
        c.execute("INSERT INTO ledger(ts,kind,pesos,minutes,mac,note) VALUES(?,?,?,?,?,?)",
                  (int(time.time()),"test_credit",pesos,mins,mac,"TEST ONLY; no access authorized"))
    return jsonify(ok=True,ledger_only=True,pesos=pesos,minutes=mins,
                   warning="No client access has been granted.")

@app.post("/api/session-test")
@auth
def session_test():
    """Disabled-by-default openNDS test path. Only use after verifying ndsctl syntax."""
    data=request.get_json(silent=True) or {}
    mac=data.get("mac",""); pesos=data.get("pesos",1)
    if not valid_mac(mac): return jsonify(error="provide a valid client MAC"),400
    if not isinstance(pesos,int) or not 1<=pesos<=1000: return jsonify(error="pesos must be integer 1..1000"),400
    with db() as c: minutes=pesos*settings(c)["minutes_per_peso"]
    expires=int(time.time()+minutes*60)
    if not ENABLE_NDS: return jsonify(error="disabled: set PISOWIFI_ENABLE_NDS_AUTH=true only after testing ndsctl on your system"),409
    try:
        # Confirm exact syntax on your installed openNDS before enabling.
        ndsctl("auth",mac)
    except Exception as e:
        return jsonify(error="ndsctl auth failed; session not recorded",detail=str(e)),502
    with db() as c:
        c.execute("INSERT INTO ledger(ts,kind,pesos,minutes,mac,expires,note) VALUES(?,?,?,?,?,?,?)",
                  (int(time.time()),"session",pesos,minutes,mac,expires,"test/admin-created session"))
    return jsonify(ok=True,mac=mac,minutes=minutes,expires_unix=expires,
                   warning="Prototype: expiry daemon not yet implemented; manually revoke/verify sessions.")

@app.post("/api/insert-coin")
@auth
def insert_coin():
    """Customer tapped Insert Coin. Coins in the next coin_window_seconds go to
    this MAC; each coin restarts the countdown. Tapping again keeps the total
    (and only restarts the countdown once a coin is in, so nobody can hold the
    slot by tapping). A phone that opens EMPTY_TAPS_ALLOWED windows without a
    coin within EMPTY_TAP_PERIOD waits TAP_BLOCK_SECONDS."""
    data=request.get_json(silent=True) or {}
    mac=data.get("mac","")
    if not valid_mac(mac): return jsonify(error="provide a valid client MAC"),400
    mac=mac.lower(); now=int(time.time())
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        s=settings(c); w=slot_holder(c,now)
        if w and w["mac"]!=mac:
            return jsonify(error="coinslot busy: another customer is inserting coins",
                           retry_after=w["expires"]+s["coin_grace_seconds"]-now),409
        pesos,mins=window_totals(c,mac,w["opened"]) if w else (0,0)
        if w and (pesos or not w["closed"]):  # tapping again during your own window
            # Only coins restart the countdown; reopening after Done needs a coin in.
            expires=now+s["coin_window_seconds"] if pesos else w["expires"]
            c.execute("UPDATE coin_window SET expires=?,closed=0 WHERE id=1",(expires,))
            return jsonify(ok=True,mac=mac,seconds_left=expires-now,pesos=pesos,minutes=mins)
        judge_last_window(c,now)  # an empty window closed with Done counts too
        wait=tap_block(c,mac,now)
        if wait:
            return jsonify(error="too many Insert Coin taps without coins",retry_after=wait),429
        c.execute("INSERT OR REPLACE INTO coin_window(id,mac,opened,expires,closed) VALUES(1,?,?,?,0)",
                  (mac,now,now+s["coin_window_seconds"]))
    return jsonify(ok=True,mac=mac,seconds_left=s["coin_window_seconds"],pesos=0,minutes=0)

def judge_last_window(c,now):
    """Count a strike against whoever let the last window end without a coin
    (once per window: the old window row is removed here)."""
    prev=c.execute("SELECT mac,opened FROM coin_window WHERE id=1").fetchone()
    if not prev: return
    pesos,_=window_totals(c,prev["mac"],prev["opened"])
    if pesos:
        c.execute("DELETE FROM tap_strike WHERE mac=?",(prev["mac"],))
    else:
        r=c.execute("SELECT strikes,last FROM tap_strike WHERE mac=?",(prev["mac"],)).fetchone()
        strikes=r["strikes"]+1 if r and now-r["last"]<EMPTY_TAP_PERIOD else 1
        c.execute("INSERT OR REPLACE INTO tap_strike(mac,strikes,last) VALUES(?,?,?)",(prev["mac"],strikes,now))
    c.execute("DELETE FROM coin_window WHERE id=1")

def tap_block(c,mac,now):
    r=c.execute("SELECT strikes,last FROM tap_strike WHERE mac=?",(mac,)).fetchone()
    if r and r["strikes"]>=EMPTY_TAPS_ALLOWED and now-r["last"]<TAP_BLOCK_SECONDS:
        return TAP_BLOCK_SECONDS-(now-r["last"])
    return 0

@app.get("/api/insert-coin")
@auth
def insert_coin_status():
    """Current Insert Coin window and what has been credited to it so far."""
    now=int(time.time())
    with db() as c:
        w=slot_holder(c,now)
        if not w: return jsonify(ok=True,active=False)
        pesos,mins=window_totals(c,w["mac"],w["opened"])
    return jsonify(ok=True,active=not w["closed"] and w["expires"]>now,mac=w["mac"],
                   seconds_left=max(0,w["expires"]-now),pesos=pesos,minutes=mins)

@app.post("/api/insert-coin/done")
@auth
def insert_coin_done():
    """Customer finished. A coin still being counted is credited to them for
    coin_grace_seconds more, then the coinslot is free for the next customer."""
    data=request.get_json(silent=True) or {}
    mac=data.get("mac","")
    if not valid_mac(mac): return jsonify(error="provide a valid client MAC"),400
    mac=mac.lower(); now=int(time.time())
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        w=slot_holder(c,now)
        if not w or w["mac"]!=mac: return jsonify(error="no Insert Coin window for this MAC"),404
        c.execute("UPDATE coin_window SET expires=?,closed=1 WHERE id=1",(min(w["expires"],now),))
        pesos,mins=window_totals(c,mac,w["opened"])
    return jsonify(ok=True,mac=mac,pesos=pesos,minutes=mins)

@app.post("/api/coin")
@auth
def coin():
    """One coin from coin_reader.py, recorded exactly once per coin_id.

    Credited to the customer holding the coinslot, else recorded unassigned so
    the money is never lost. A coin detected before the current window opened
    (delivered late from the reader's spool) belongs to someone earlier, so it
    is recorded unassigned rather than given to whoever holds the slot now.
    Minutes use this service's rate, not the reader's."""
    data=request.get_json(silent=True) or {}
    coin_id=norm_coin_id(data.get("coin_id"))
    pesos=data.get("pesos"); pulses=data.get("pulses",0); detected=data.get("detected_at","")
    if not coin_id: return jsonify(error="coin_id must be a UUID"),400
    if not is_int(pesos) or not 1<=pesos<=1000: return jsonify(error="pesos must be integer 1..1000"),400
    if not is_int(pulses) or not 0<=pulses<=1000: return jsonify(error="pulses must be integer 0..1000"),400
    if not isinstance(detected,str) or len(detected)>40: return jsonify(error="detected_at must be a short string"),400
    now=int(time.time())
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        dup=c.execute("SELECT l.pesos,l.minutes,l.mac FROM coin JOIN ledger l ON l.id=coin.ledger_id WHERE coin.coin_id=?",
                      (coin_id,)).fetchone()
        if dup:
            return jsonify(ok=True,duplicate=True,coin_id=coin_id,pesos=dup["pesos"],
                           minutes=dup["minutes"],mac=dup["mac"] or None)
        s=settings(c); w=slot_holder(c,now)
        seen=parse_ts(detected)
        late=w is not None and seen is not None and seen<w["opened"]-2  # 2 s clock slack
        mac=w["mac"] if w and not late else ""
        if mac:  # promos apply to the window's running total: ₱5 then ₱5 prices as ₱10
            before,_=window_totals(c,mac,w["opened"])
            mins=price_now(c,before+pesos)-price_now(c,before)
        else:
            mins=price_now(c,pesos)
        note=("coin" if mac else "coin; detected before the current Insert Coin window (late delivery)" if late
              else "coin; no customer had tapped Insert Coin")
        cur=c.execute("INSERT INTO ledger(ts,kind,pesos,minutes,mac,note) VALUES(?,?,?,?,?,?)",
                      (now,"coin",pesos,mins,mac,note))
        c.execute("INSERT INTO coin(coin_id,ledger_id,ts,pesos,pulses,detected_at) VALUES(?,?,?,?,?,?)",
                  (coin_id,cur.lastrowid,now,pesos,pulses,detected))
        if mac and not w["closed"]:
            c.execute("UPDATE coin_window SET expires=? WHERE id=1",(now+s["coin_window_seconds"],))
    return jsonify(ok=True,duplicate=False,coin_id=coin_id,pesos=pesos,minutes=mins,mac=mac or None)

@app.get("/api/customer")
@auth
def customer():
    """Everything the portal shows one customer: credit, paid time, coinslot."""
    mac=request.args.get("mac","")
    if not valid_mac(mac): return jsonify(error="provide a valid client MAC"),400
    mac=mac.lower(); now=int(time.time())
    with db() as c:
        s=settings(c); w=slot_holder(c,now)
        credit=balance(c,mac); expires,paused=access(c,mac)
        until=0 if paused else expires
        rates=rate_card(c); blocked=0 if w else tap_block(c,mac,now)
        if w and w["mac"]==mac:
            pesos,mins=window_totals(c,mac,w["opened"])
            win={"state":"mine","open":not w["closed"] and w["expires"]>now,
                 "seconds_left":max(0,w["expires"]-now),"pesos":pesos,"minutes":mins}
        elif w:
            win={"state":"busy","retry_after":max(1,w["expires"]+s["coin_grace_seconds"]-now)}
        else:
            win={"state":"free"}
    return jsonify(ok=True,mac=mac,credit_minutes=credit,minutes_per_peso=s["minutes_per_peso"],rates=rates,
                   session={"active":until>now,"expires":until,"seconds_left":max(0,until-now),
                            "paused":paused>0,"paused_seconds":paused},
                   window=win,insert_blocked_for=blocked,internet_enabled=ENABLE_NDS)

def customer_mac():
    data=request.get_json(silent=True) or {}
    mac=data.get("mac","")
    return mac.lower() if valid_mac(mac) else None

def internet_off():
    return jsonify(error="internet access is switched off: set PISOWIFI_ENABLE_NDS_AUTH=true after testing ndsctl"),409

@app.post("/api/pause")
@auth
def pause():
    """Stop the customer's clock: disconnect them and keep their time left."""
    mac=customer_mac()
    if not mac: return jsonify(error="provide a valid client MAC"),400
    if not ENABLE_NDS: return internet_off()
    now=int(time.time())
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            expires,paused=access(c,mac)
            if paused: return jsonify(error="time is already paused"),409
            left=expires-now
            if left<60: return jsonify(error="less than a minute left; nothing to pause"),409
            nds_deauth(mac)
            set_access(c,mac,now,left)
            c.execute("INSERT INTO ledger(ts,kind,minutes,mac,note) VALUES(?,?,?,?,?)",
                      (now,"pause",left//60,mac,f"paused with {left} s left"))
    except NdsError as e:
        return jsonify(error="couldn't pause",detail=str(e)[:200]),502
    return jsonify(ok=True,mac=mac,paused_seconds=left)

@app.post("/api/resume")
@auth
def resume():
    """Start the customer's paused clock again (plus any credit waiting)."""
    mac=customer_mac()
    if not mac: return jsonify(error="provide a valid client MAC"),400
    if not ENABLE_NDS: return internet_off()
    now=int(time.time())
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            _,paused=access(c,mac)
            if not paused: return jsonify(error="time isn't paused"),409
            started=apply_balance(c,mac,now)  # credit waiting? adds it and ends the pause
            if started: return jsonify(ok=True,mac=mac,seconds_left=started["seconds_left"])
            total=-(-paused//60)
            nds_grant(c,mac,total)
            set_access(c,mac,now+total*60,0)
            c.execute("INSERT INTO ledger(ts,kind,minutes,mac,note) VALUES(?,?,?,?,?)",
                      (now,"resume",total,mac,"resumed paused time"))
    except NdsError as e:
        return jsonify(error="couldn't resume",detail=str(e)[:200]),502
    return jsonify(ok=True,mac=mac,seconds_left=total*60)

@app.post("/api/connect")
@auth
def connect():
    """Customer tapped Start (or their window ended): close their Insert Coin
    window and turn their credit into internet time with openNDS."""
    data=request.get_json(silent=True) or {}
    mac=data.get("mac","")
    if not valid_mac(mac): return jsonify(error="provide a valid client MAC"),400
    if not ENABLE_NDS:
        return jsonify(error="internet access is switched off: set PISOWIFI_ENABLE_NDS_AUTH=true after testing ndsctl"),409
    mac=mac.lower(); now=int(time.time())
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            w=slot_holder(c,now)
            if w and w["mac"]==mac and not w["closed"]:
                c.execute("UPDATE coin_window SET expires=?,closed=1 WHERE id=1",(min(w["expires"],now),))
            result=apply_balance(c,mac,now)
            if result is None:
                return jsonify(error="no credit yet: insert coins first"),409
    except NdsError as e:
        return jsonify(error="couldn't switch on internet access",detail=str(e)[:200]),502
    return jsonify(ok=True,mac=mac,**result)

@app.post("/api/sessions/reconcile")
@auth
def reconcile_now():
    return jsonify(ok=True,**reconcile(int(time.time())))

@app.get("/api/settings")
@auth
def get_settings():
    with db() as c: s=settings(c)
    return jsonify(ok=True,settings=s,limits={k:{"min":lo,"max":hi} for k,(lo,hi) in SETTING_LIMITS.items()})

@app.post("/api/settings")
@auth
def post_settings():
    """Change one or more settings. Every value is checked before any is saved."""
    data=request.get_json(silent=True) or {}
    changes={k:v for k,v in data.items() if k in SETTING_LIMITS}
    if not changes: return jsonify(error="send at least one of: "+", ".join(SETTING_LIMITS)),400
    for k,v in changes.items():
        lo,hi=SETTING_LIMITS[k]
        if not is_int(v) or not lo<=v<=hi: return jsonify(error=f"{k} must be an integer {lo}..{hi}"),400
    now=int(time.time())
    with db() as c:
        for k,v in changes.items(): save_setting(c,k,v,now)
        s=settings(c)
    return jsonify(ok=True,settings=s)

@app.get("/api/sales")
@auth
def sales():
    """Coin takings (real coins only, not test credits) in local time."""
    days=request.args.get("days","14")
    if not days.isdigit() or not 1<=int(days)<=90: return jsonify(error="days must be 1..90"),400
    days=int(days); now=int(time.time())
    q="SELECT COALESCE(SUM(pesos),0) p, COUNT(*) n FROM ledger WHERE kind='coin'"
    local="date(ts,'unixepoch','localtime')"
    with db() as c:
        def since(back):  # totals from the start of the day `back` days ago
            r=c.execute(f"{q} AND {local}>=date(?,'unixepoch','localtime',?)",(now,f"-{back} days")).fetchone()
            return {"pesos":r["p"],"coins":r["n"]}
        everything=c.execute(q).fetchone()
        today=date.fromisoformat(c.execute("SELECT date(?,'unixepoch','localtime') d",(now,)).fetchone()["d"])
        rows={r["d"]:r for r in c.execute(
            f"SELECT {local} d, SUM(pesos) p, COUNT(*) n FROM ledger WHERE kind='coin' "
            f"AND {local}>=date(?,'unixepoch','localtime',?) GROUP BY d",(now,f"-{days-1} days"))}
        mix=c.execute("SELECT co.pesos, COUNT(*) n FROM coin co JOIN ledger l ON l.id=co.ledger_id "
                      "WHERE date(l.ts,'unixepoch','localtime')>=date(?,'unixepoch','localtime',?) GROUP BY co.pesos ORDER BY co.pesos",
                      (now,f"-{days-1} days")).fetchall()
        unassigned=c.execute(q+" AND mac=''").fetchone()
        last=c.execute("SELECT MAX(ts) t FROM ledger WHERE kind='coin'").fetchone()["t"]
        totals={"today":since(0),"last_7_days":since(6),"last_30_days":since(29),
                "all_time":{"pesos":everything["p"],"coins":everything["n"]}}
    daily=[]
    for back in range(days-1,-1,-1):
        d=(today-timedelta(days=back)).isoformat(); r=rows.get(d)
        daily.append({"date":d,"pesos":r["p"] if r else 0,"coins":r["n"] if r else 0})
    return jsonify(ok=True,days=days,totals=totals,daily=daily,
                   coin_mix=[{"pesos":r["pesos"],"count":r["n"]} for r in mix],
                   unassigned={"pesos":unassigned["p"],"coins":unassigned["n"]},last_coin_ts=last)

@app.get("/api/coins")
@auth
def recent_coins():
    limit=request.args.get("limit","50")
    if not limit.isdigit() or not 1<=int(limit)<=200: return jsonify(error="limit must be 1..200"),400
    with db() as c:
        rows=c.execute("SELECT co.coin_id,l.ts,l.pesos,l.minutes,l.mac,co.pulses FROM coin co JOIN ledger l ON l.id=co.ledger_id "
                       "ORDER BY l.id DESC LIMIT ?",(int(limit),)).fetchall()
    return jsonify(ok=True,coins=[{"coin_id":r["coin_id"],"ts":r["ts"],"pesos":r["pesos"],"minutes":r["minutes"],
                                   "mac":r["mac"] or None,"pulses":r["pulses"]} for r in rows])

CLIENT_FIELDS=("mac","ip","state","session_start","session_end","last_active",
               "download_this_session","upload_this_session")

def parse_nds_clients(text):
    """Client list from `ndsctl json`. Tolerates a dict keyed by MAC or a list."""
    data=json.loads(text)
    clients=data.get("clients",{}) if isinstance(data,dict) else {}
    items=clients.values() if isinstance(clients,dict) else clients if isinstance(clients,list) else []
    return [{k:cl.get(k) for k in CLIENT_FIELDS} for cl in items if isinstance(cl,dict)]

@app.get("/api/clients")
@auth
def clients():
    """Read-only view of openNDS clients. Works even while NDS auth is disabled."""
    try:
        out=subprocess.run(["ndsctl","json"],check=True,capture_output=True,text=True,timeout=5).stdout
        return jsonify(ok=True,clients=parse_nds_clients(out))
    except FileNotFoundError:
        return jsonify(error="ndsctl not found; is openNDS installed?"),502
    except (subprocess.SubprocessError,ValueError) as e:
        return jsonify(error="openNDS is not running or returned unreadable output",detail=str(e)[:200]),502

# ---- name and look -----------------------------------------------------------------

def appearance(c):
    a={"title":TITLE,"dashboard_mode":"auto","dashboard_accent":"gold"}
    for r in c.execute("SELECT name,value FROM text_setting"):
        if r["name"] in a: a[r["name"]]=r["value"]
    return a

def check_appearance(data):
    """Validated changes, or (None, error)."""
    changes={}
    if "title" in data:
        title=data["title"]
        if not isinstance(title,str) or not title.strip(): return None,"title can't be empty"
        title=" ".join(title.split())  # one line, single spaces
        if len(title)>40: return None,"title must be at most 40 characters"
        if re.search(r"[\x00-\x1f\x7f<>]",title): return None,"title can't contain < > or control characters"
        changes["title"]=title
    if "dashboard_mode" in data:
        if data["dashboard_mode"] not in THEME_MODES: return None,"dashboard_mode must be one of: "+", ".join(THEME_MODES)
        changes["dashboard_mode"]=data["dashboard_mode"]
    if "dashboard_accent" in data:
        if data["dashboard_accent"] not in THEME_ACCENTS: return None,"dashboard_accent must be one of: "+", ".join(THEME_ACCENTS)
        changes["dashboard_accent"]=data["dashboard_accent"]
    if not changes: return None,"send at least one of: title, dashboard_mode, dashboard_accent"
    return changes,None

@app.get("/api/appearance")
@auth
def get_appearance():
    with db() as c: a=appearance(c)
    return jsonify(ok=True,**a,choices={"modes":list(THEME_MODES),"accents":list(THEME_ACCENTS)})

@app.post("/api/appearance")
@auth
def set_appearance():
    changes,error=check_appearance(request.get_json(silent=True) or {})
    if error: return jsonify(error=error),400
    now=int(time.time())
    with db() as c:
        for k,v in changes.items():
            c.execute("INSERT OR REPLACE INTO text_setting(name,value,ts) VALUES(?,?,?)",(k,v,now))
        c.execute("INSERT INTO ledger(ts,kind,note) VALUES(?,?,?)",(now,"appearance",json.dumps(changes,ensure_ascii=False)))
        a=appearance(c)
    return jsonify(ok=True,**a)

# ---- promos ------------------------------------------------------------------------

@app.get("/api/promos")
@auth
def get_promos():
    with db() as c:
        return jsonify(ok=True,promos=[{"pesos":p,"minutes":m} for p,m in promos(c)],rate_card=rate_card(c))

@app.post("/api/promos")
@auth
def set_promos():
    """Replace all promo tiers, e.g. [{"pesos":10,"minutes":70}]. Send [] to remove them."""
    data=request.get_json(silent=True) or {}
    tiers=data.get("promos")
    if not isinstance(tiers,list) or len(tiers)>MAX_PROMOS:
        return jsonify(error=f"promos must be a list of at most {MAX_PROMOS}"),400
    seen=set()
    for t in tiers:
        if not isinstance(t,dict) or not is_int(t.get("pesos")) or not is_int(t.get("minutes")):
            return jsonify(error="each promo needs whole-number pesos and minutes"),400
        if not 2<=t["pesos"]<=1000 or not 1<=t["minutes"]<=100000:
            return jsonify(error="promo pesos must be 2..1000 and minutes 1..100000"),400
        if t["pesos"] in seen: return jsonify(error=f"two promos for ₱{t['pesos']}"),400
        seen.add(t["pesos"])
    now=int(time.time())
    with db() as c:
        c.execute("DELETE FROM promo")
        for t in tiers: c.execute("INSERT INTO promo(pesos,minutes) VALUES(?,?)",(t["pesos"],t["minutes"]))
        c.execute("INSERT INTO ledger(ts,kind,note) VALUES(?,?,?)",(now,"promos",json.dumps(tiers)))
        return jsonify(ok=True,promos=[{"pesos":p,"minutes":m} for p,m in promos(c)],rate_card=rate_card(c))

# ---- vouchers ------------------------------------------------------------------------

def norm_code(text):
    if not isinstance(text,str): return None
    code=re.sub(r"[\s-]","",text).upper()
    return code if len(code)==VOUCHER_LENGTH and all(ch in VOUCHER_ALPHABET for ch in code) else None

class RedeemLimiter:
    """Slows down code guessing: per phone, and across all phones (MACs can be faked)."""
    def __init__(self):
        self.lock=threading.Lock(); self.phones={}; self.recent=[]
    def wait(self,mac,now):
        with self.lock:
            self.recent=[t for t in self.recent if now-t<60]
            if len(self.recent)>=REDEEM_FAILS_PER_MINUTE: return 60-(now-self.recent[0])
            n,last=self.phones.get(mac,(0,0))
            if n>=REDEEM_FAILS_PER_PHONE and now-last<REDEEM_LOCK_SECONDS: return REDEEM_LOCK_SECONDS-(now-last)
            return 0
    def failed(self,mac,now):
        with self.lock:
            n,last=self.phones.get(mac,(0,0))
            self.phones[mac]=(n+1 if now-last<REDEEM_LOCK_SECONDS else 1,now); self.recent.append(now)
    def succeeded(self,mac):
        with self.lock: self.phones.pop(mac,None)

redeem_limiter=RedeemLimiter()

@app.post("/api/vouchers")
@auth
def create_vouchers():
    """Make a batch of codes, each worth `minutes`. `price` is what you sell one for (for your records)."""
    data=request.get_json(silent=True) or {}
    count,minutes,price_=data.get("count"),data.get("minutes"),data.get("price",0)
    if not is_int(count) or not 1<=count<=200: return jsonify(error="count must be 1..200"),400
    if not is_int(minutes) or not 1<=minutes<=100000: return jsonify(error="minutes must be 1..100000"),400
    if not is_int(price_) or not 0<=price_<=100000: return jsonify(error="price must be 0..100000"),400
    now=int(time.time())
    batch=datetime.fromtimestamp(now).strftime("%Y%m%d")+"-"+"".join(secrets.choice(VOUCHER_ALPHABET) for _ in range(4))
    codes=[]
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        while len(codes)<count:
            code="".join(secrets.choice(VOUCHER_ALPHABET) for _ in range(VOUCHER_LENGTH))
            try:
                c.execute("INSERT INTO voucher(code,batch,minutes,price,created) VALUES(?,?,?,?,?)",
                          (code,batch,minutes,price_,now))
                codes.append(code)
            except sqlite3.IntegrityError:
                continue  # already exists; draw another
        c.execute("INSERT INTO ledger(ts,kind,minutes,pesos,note) VALUES(?,?,?,?,?)",
                  (now,"voucher_batch",minutes,price_,f"{batch}: {count} codes"))
    return jsonify(ok=True,batch=batch,minutes=minutes,price=price_,codes=codes)

@app.get("/api/voucher-batches")
@auth
def voucher_batches():
    with db() as c:
        rows=c.execute("SELECT batch, MIN(created) created, MAX(minutes) minutes, MAX(price) price, COUNT(*) total, "
                       "SUM(used_by!='') used, SUM(void=1 AND used_by='') void FROM voucher "
                       "GROUP BY batch ORDER BY created DESC LIMIT 50").fetchall()
    return jsonify(ok=True,batches=[dict(r) for r in rows])

@app.get("/api/vouchers")
@auth
def list_vouchers():
    batch=request.args.get("batch","")
    if not batch: return jsonify(error="batch is required"),400
    with db() as c:
        rows=c.execute("SELECT code,minutes,price,created,used_by,used_at,void FROM voucher WHERE batch=? ORDER BY code",
                       (batch,)).fetchall()
    if not rows: return jsonify(error="no such batch"),404
    return jsonify(ok=True,batch=batch,vouchers=[dict(r) for r in rows])

@app.post("/api/vouchers/void")
@auth
def void_vouchers():
    """Cancel every unused code in a batch (lost or stolen cards)."""
    data=request.get_json(silent=True) or {}
    batch=data.get("batch","")
    if not isinstance(batch,str) or not batch: return jsonify(error="batch is required"),400
    with db() as c:
        n=c.execute("UPDATE voucher SET void=1 WHERE batch=? AND used_by='' AND void=0",(batch,)).rowcount
        c.execute("INSERT INTO ledger(ts,kind,note) VALUES(?,?,?)",(int(time.time()),"voucher_void",f"{batch}: {n} codes"))
    return jsonify(ok=True,batch=batch,voided=n)

@app.post("/api/redeem")
@auth
def redeem():
    """Customer typed a voucher code: credit its minutes and start them."""
    data=request.get_json(silent=True) or {}
    mac=data.get("mac","")
    if not valid_mac(mac): return jsonify(error="provide a valid client MAC"),400
    mac=mac.lower(); now=int(time.time())
    wait=redeem_limiter.wait(mac,now)
    if wait: return jsonify(error="too many wrong codes",retry_after=wait),429
    code=norm_code(data.get("code"))
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        v=c.execute("SELECT code,minutes,used_by,void FROM voucher WHERE code=?",(code,)).fetchone() if code else None
        if not v or v["void"]:
            redeem_limiter.failed(mac,now)
            return jsonify(error="unknown voucher code"),404
        if v["used_by"]:
            redeem_limiter.failed(mac,now)
            return jsonify(error="this voucher has already been used"),409
        redeem_limiter.succeeded(mac)
        c.execute("UPDATE voucher SET used_by=?,used_at=? WHERE code=?",(mac,now,code))
        c.execute("INSERT INTO ledger(ts,kind,minutes,mac,note) VALUES(?,?,?,?,?)",
                  (now,"voucher",v["minutes"],mac,f"voucher {code}"))
        started=try_start(c,mac,now)
    return jsonify(ok=True,mac=mac,minutes=v["minutes"],started=started is not None,
                   seconds_left=started["seconds_left"] if started else None)

# ---- giving an unassigned coin to a customer ------------------------------------------

@app.post("/api/coins/assign")
@auth
def assign_coin():
    """Owner gives a coin that arrived with no Insert Coin window to a customer."""
    data=request.get_json(silent=True) or {}
    coin_id=norm_coin_id(data.get("coin_id")); mac=data.get("mac","")
    if not coin_id: return jsonify(error="coin_id must be a UUID"),400
    if not valid_mac(mac): return jsonify(error="provide a valid client MAC"),400
    mac=mac.lower(); now=int(time.time())
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT l.id,l.mac,l.minutes,l.pesos FROM coin co JOIN ledger l ON l.id=co.ledger_id WHERE co.coin_id=?",
                    (coin_id,)).fetchone()
        if not r: return jsonify(error="no such coin"),404
        if r["mac"]: return jsonify(error="this coin already belongs to a customer"),409
        c.execute("UPDATE ledger SET mac=?,note=note||? WHERE id=?",(mac,f"; given to {mac} by the owner",r["id"]))
        c.execute("INSERT INTO ledger(ts,kind,pesos,mac,note) VALUES(?,?,?,?,?)",
                  (now,"assign",r["pesos"],mac,f"coin {coin_id}"))
        started=try_start(c,mac,now)
    return jsonify(ok=True,coin_id=coin_id,mac=mac,minutes=r["minutes"],started=started is not None)

if __name__=="__main__":
    if ENABLE_NDS: threading.Thread(target=reconcile_forever,daemon=True).start()
    app.run(host=BIND,port=PORT,debug=False)
