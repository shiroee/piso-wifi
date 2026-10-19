"""Tests for creditd.py's coin and Insert Coin routes.

Run with the service's venv (it has Flask):
    cd app && /opt/pisowifi-venv/bin/python -m unittest test_creditd
"""
import json, logging, os, subprocess, sys, tempfile, threading, time, unittest, uuid
from pathlib import Path
from unittest import mock

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
sys.path.insert(0,str(HERE.parent/"coin"))

import creditd  # noqa: E402
import coin_reader  # noqa: E402

TOKEN="t"*32
MAC="aa:bb:cc:dd:ee:01"
OTHER="aa:bb:cc:dd:ee:02"
T0=1_800_000_000

def setUpModule():
    # Sales are grouped by local day; pin the zone so the tests don't depend on the machine.
    os.environ["TZ"]="Asia/Manila"; time.tzset()

def local_day(ts):
    return time.strftime("%Y-%m-%d",time.localtime(ts))

DAY=86400

class FakeClock:
    def __init__(self,now): self.now=now
    def time(self): return self.now

class CreditdTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.clock=FakeClock(T0)
        for name,value in (("DB",os.path.join(tmp.name,"credits.sqlite3")),("TOKEN",TOKEN),("MINUTES",5),
                           ("COIN_WINDOW",60),("COIN_GRACE",5),("time",self.clock),
                           ("redeem_limiter",creditd.RedeemLimiter())):
            p=mock.patch.object(creditd,name,value); p.start(); self.addCleanup(p.stop)
        self.client=creditd.app.test_client()

    def at(self,seconds): self.clock.now=T0+seconds

    def post(self,path,body,token=TOKEN):
        return self.client.post(path,json=body,headers={"X-Admin-Token":token})

    def get(self,path):
        return self.client.get(path,headers={"X-Admin-Token":TOKEN})

    def coin(self,pesos=5,**extra):
        return self.post("/api/coin",{"coin_id":str(uuid.uuid4()),"pesos":pesos,"pulses":pesos,**extra})

    def rows(self,sql,*args):
        c=creditd.db()
        try: return c.execute(sql,args).fetchall()
        finally: c.close()

class CoinRouteTests(CreditdTest):
    def test_needs_admin_token(self):
        r=self.post("/api/coin",{"coin_id":str(uuid.uuid4()),"pesos":5},token="wrong")
        self.assertEqual(r.status_code,401)
        self.assertEqual(self.rows("SELECT * FROM coin"),[])

    def test_coin_without_insert_coin_is_kept_unassigned(self):
        r=self.coin(5)
        self.assertEqual(r.status_code,200)
        self.assertEqual((r.json["mac"],r.json["minutes"],r.json["duplicate"]),(None,25,False))
        status=self.get("/api/status").json
        self.assertEqual((status["coin_pesos"],status["unassigned_coin_pesos"]),(5,5))

    def test_same_coin_id_is_recorded_once(self):
        body={"coin_id":str(uuid.uuid4()),"pesos":10,"pulses":10}
        first=self.post("/api/coin",body); again=self.post("/api/coin",body)
        self.assertFalse(first.json["duplicate"]); self.assertTrue(again.json["duplicate"])
        self.assertEqual(again.json["pesos"],10)
        self.assertEqual(len(self.rows("SELECT * FROM coin")),1)
        self.assertEqual(len(self.rows("SELECT * FROM ledger WHERE kind='coin'")),1)

    def test_minutes_use_the_service_rate_not_the_sender(self):
        r=self.coin(5,minutes=999)
        self.assertEqual(r.json["minutes"],25)
        self.assertEqual(self.rows("SELECT minutes FROM ledger WHERE kind='coin'")[0]["minutes"],25)

    def test_bad_payloads_are_rejected_and_not_recorded(self):
        good=str(uuid.uuid4())
        for body in ({"pesos":5},{"coin_id":"not-a-uuid","pesos":5},{"coin_id":good,"pesos":0},
                     {"coin_id":good,"pesos":True},{"coin_id":good,"pesos":"5"},
                     {"coin_id":good,"pesos":5,"pulses":-1},{"coin_id":good,"pesos":5,"detected_at":"x"*41}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/coin",body).status_code,400)
        self.assertEqual(self.rows("SELECT * FROM ledger"),[])

    def test_existing_test_credit_route_still_works(self):
        r=self.post("/api/test-credit",{"pesos":2})
        self.assertEqual((r.status_code,r.json["minutes"],r.json["ledger_only"]),(200,10,True))

class InsertCoinTests(CreditdTest):
    def test_coin_goes_to_customer_who_tapped_insert_coin(self):
        self.assertEqual(self.post("/api/insert-coin",{"mac":MAC.upper()}).status_code,200)
        self.at(10); r=self.coin(5)
        self.assertEqual(r.json["mac"],MAC)  # stored lower-case
        status=self.get("/api/insert-coin").json
        self.assertEqual((status["active"],status["mac"],status["pesos"],status["minutes"]),(True,MAC,5,25))

    def test_each_coin_restarts_the_countdown(self):
        self.post("/api/insert-coin",{"mac":MAC})
        self.at(50); self.coin(1)
        self.at(100)
        status=self.get("/api/insert-coin").json
        self.assertTrue(status["active"]); self.assertEqual(status["seconds_left"],10)

    def test_second_customer_waits_until_window_and_grace_pass(self):
        self.post("/api/insert-coin",{"mac":MAC})
        self.at(10); busy=self.post("/api/insert-coin",{"mac":OTHER})
        self.assertEqual(busy.status_code,409); self.assertEqual(busy.json["retry_after"],55)
        self.at(64); self.assertEqual(self.post("/api/insert-coin",{"mac":OTHER}).status_code,409)
        self.at(65); self.assertEqual(self.post("/api/insert-coin",{"mac":OTHER}).status_code,200)

    def test_coin_counted_just_after_window_ends_still_goes_to_that_customer(self):
        self.post("/api/insert-coin",{"mac":MAC})
        self.at(62); self.assertEqual(self.coin(20).json["mac"],MAC)
        self.at(70); self.assertEqual(self.coin(1).json["mac"],MAC)  # the late coin restarted the countdown

    def test_coin_after_grace_is_unassigned(self):
        self.post("/api/insert-coin",{"mac":MAC})
        self.at(65); self.assertIsNone(self.coin(5).json["mac"])

    def test_done_releases_slot_after_grace_and_catches_in_flight_coin(self):
        self.post("/api/insert-coin",{"mac":MAC})
        self.at(5); self.coin(5)
        self.at(6); done=self.post("/api/insert-coin/done",{"mac":MAC})
        self.assertEqual((done.status_code,done.json["pesos"],done.json["minutes"]),(200,5,25))
        self.at(9); self.assertEqual(self.coin(10).json["mac"],MAC)  # pulses still arriving after Done
        status=self.get("/api/insert-coin").json
        self.assertEqual((status["active"],status["pesos"]),(False,15))
        self.at(10); self.assertEqual(self.post("/api/insert-coin",{"mac":OTHER}).status_code,409)
        self.at(11); self.assertEqual(self.post("/api/insert-coin",{"mac":OTHER}).status_code,200)

    def test_tapping_again_keeps_the_running_total(self):
        self.post("/api/insert-coin",{"mac":MAC})
        self.at(5); self.coin(5)
        self.at(20); r=self.post("/api/insert-coin",{"mac":MAC})
        self.assertEqual((r.json["pesos"],r.json["minutes"]),(5,25))

    def test_done_for_other_customer_is_refused(self):
        self.post("/api/insert-coin",{"mac":MAC})
        self.assertEqual(self.post("/api/insert-coin/done",{"mac":OTHER}).status_code,404)

    def test_insert_coin_needs_valid_mac_and_token(self):
        self.assertEqual(self.post("/api/insert-coin",{"mac":"nope"}).status_code,400)
        self.assertEqual(self.post("/api/insert-coin",{"mac":MAC},token="wrong").status_code,401)
        self.assertFalse(self.get("/api/insert-coin").json["active"])

class CoinReaderEndToEndTests(CreditdTest):
    """The real coin_reader.HttpSink and Spool talking to creditd over HTTP."""

    def setUp(self):
        super().setUp()
        # The reader stamps coins with the real clock, as on the Pi (same machine).
        self.clock.now=int(time.time())
        from werkzeug.serving import make_server
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self.server=make_server("127.0.0.1",0,creditd.app,threaded=True)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url=f"http://127.0.0.1:{self.server.server_port}/api/coin"
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.spool=coin_reader.Spool(Path(tmp.name)/"coin-spool.jsonl")
        coin_reader.log.propagate=False; coin_reader.log.disabled=True
        self.addCleanup(setattr,coin_reader.log,"disabled",False)

    def test_coin_reaches_the_customer(self):
        self.post("/api/insert-coin",{"mac":MAC})
        coin_reader.deliver(coin_reader.make_record(20),self.spool,coin_reader.HttpSink(self.url,TOKEN))
        self.assertEqual(self.spool.pending(),[])
        self.assertEqual(self.get("/api/insert-coin").json["minutes"],100)

    def test_wrong_token_keeps_coin_in_spool_until_fixed(self):
        record=coin_reader.make_record(5)
        coin_reader.deliver(record,self.spool,coin_reader.HttpSink(self.url,"w"*32))
        self.assertEqual(self.spool.pending(),[record])
        self.assertEqual(self.spool.flush(coin_reader.HttpSink(self.url,TOKEN)),1)
        self.assertEqual(self.get("/api/status").json["coin_pesos"],5)

    def test_resend_after_lost_reply_does_not_double_credit(self):
        record=coin_reader.make_record(10)
        sink=coin_reader.HttpSink(self.url,TOKEN)
        sink.send(record)  # recorded, but pretend the reply was lost...
        self.spool.add(record)  # ...so the coin is still waiting in the spool
        self.assertEqual(self.spool.flush(sink),1)
        self.assertEqual(self.get("/api/status").json["coin_pesos"],10)

class SettingsTests(CreditdTest):
    def test_rate_change_is_saved_and_used_for_coins(self):
        self.assertEqual(self.post("/api/rate",{"minutes_per_peso":10}).status_code,200)
        # A restart reloads MINUTES from the env file (still 5); the saved rate must win.
        self.assertEqual(self.get("/api/status").json["minutes_per_peso"],10)
        self.assertEqual(self.coin(5).json["minutes"],50)

    def test_settings_round_trip(self):
        r=self.post("/api/settings",{"minutes_per_peso":6,"coin_window_seconds":90})
        self.assertEqual(r.status_code,200)
        s=self.get("/api/settings").json
        self.assertEqual(s["settings"],{"minutes_per_peso":6,"coin_window_seconds":90,"coin_grace_seconds":5,
                                        "download_kbps":0,"upload_kbps":0})
        self.assertEqual(s["limits"]["coin_grace_seconds"],{"min":3,"max":60})

    def test_bad_settings_change_nothing(self):
        for body in ({"minutes_per_peso":0},{"coin_window_seconds":5},{"minutes_per_peso":True},
                     {"unknown":1},{},{"minutes_per_peso":7,"coin_grace_seconds":1}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/settings",body).status_code,400)
        self.assertEqual(self.get("/api/settings").json["settings"]["minutes_per_peso"],5)

    def test_window_setting_is_used(self):
        self.post("/api/settings",{"coin_window_seconds":30})
        self.assertEqual(self.post("/api/insert-coin",{"mac":MAC}).json["seconds_left"],30)
        self.at(34); self.assertEqual(self.post("/api/insert-coin",{"mac":OTHER}).status_code,409)
        self.at(35); self.assertEqual(self.post("/api/insert-coin",{"mac":OTHER}).status_code,200)

    def test_settings_need_token(self):
        self.assertEqual(self.post("/api/settings",{"minutes_per_peso":9},token="wrong").status_code,401)
        self.assertEqual(self.client.get("/api/settings").status_code,401)

class SalesTests(CreditdTest):
    def coin_at(self,seconds,pesos):
        self.at(seconds); self.assertEqual(self.coin(pesos).status_code,200)

    def test_totals_daily_and_coin_mix(self):
        self.coin_at(-40*DAY,1); self.coin_at(-10*DAY,20); self.coin_at(-DAY,10)
        self.coin_at(0,5); self.coin_at(60,1)
        self.post("/api/test-credit",{"pesos":100})  # test credit is not money
        self.at(120); r=self.get("/api/sales?days=14").json
        t=r["totals"]
        self.assertEqual((t["today"]["pesos"],t["today"]["coins"]),(6,2))
        self.assertEqual(t["last_7_days"]["pesos"],16)
        self.assertEqual(t["last_30_days"]["pesos"],36)
        self.assertEqual((t["all_time"]["pesos"],t["all_time"]["coins"]),(37,5))
        self.assertEqual(len(r["daily"]),14)
        self.assertEqual(r["daily"][-1],{"date":local_day(T0),"pesos":6,"coins":2})
        self.assertEqual(r["daily"][-2],{"date":local_day(T0-DAY),"pesos":10,"coins":1})
        self.assertEqual(r["daily"][-11]["pesos"],20)
        self.assertEqual(sum(d["pesos"] for d in r["daily"]),36)
        self.assertEqual(r["coin_mix"],[{"pesos":1,"count":1},{"pesos":5,"count":1},
                                        {"pesos":10,"count":1},{"pesos":20,"count":1}])
        self.assertEqual(r["unassigned"]["pesos"],37)
        self.assertEqual(r["last_coin_ts"],T0+60)

    def test_empty_ledger(self):
        r=self.get("/api/sales?days=7").json
        self.assertEqual(r["totals"]["all_time"],{"pesos":0,"coins":0})
        self.assertEqual([d["pesos"] for d in r["daily"]],[0]*7)
        self.assertIsNone(r["last_coin_ts"])

    def test_days_must_be_sensible(self):
        for days in ("0","91","abc","-3"):
            with self.subTest(days=days):
                self.assertEqual(self.get(f"/api/sales?days={days}").status_code,400)

    def test_recent_coins_newest_first(self):
        self.post("/api/insert-coin",{"mac":MAC})
        self.coin_at(1,5); self.coin_at(2,10)
        coins=self.get("/api/coins?limit=1").json["coins"]
        self.assertTrue(uuid.UUID(coins[0].pop("coin_id")))
        self.assertEqual(coins,[{"ts":T0+2,"pesos":10,"minutes":50,"mac":MAC,"pulses":10}])
        self.assertEqual(self.get("/api/coins?limit=500").status_code,400)

NDS_JSON={"client_list_length":"1","clients":{"00:1f:fb:bb:25:bc":{
    "mac":"00:1f:fb:bb:25:bc","ip":"10.20.0.121","state":"Authenticated","session_start":"1800000000",
    "session_end":"1800000180","last_active":"1800000010","token":"secret-token",
    "download_this_session":"12","upload_this_session":"3"}}}

class ClientsTests(CreditdTest):
    def fake_run(self,stdout="",error=None):
        def run(*a,**kw):
            if error: raise error
            return subprocess.CompletedProcess(a[0],0,stdout=stdout,stderr="")
        p=mock.patch.object(creditd.subprocess,"run",side_effect=run); p.start(); self.addCleanup(p.stop)

    def test_clients_are_listed_without_tokens(self):
        self.fake_run(json.dumps(NDS_JSON))
        clients=self.get("/api/clients").json["clients"]
        self.assertEqual(len(clients),1)
        self.assertEqual((clients[0]["mac"],clients[0]["state"],clients[0]["session_end"]),
                         ("00:1f:fb:bb:25:bc","Authenticated","1800000180"))
        self.assertNotIn("token",clients[0])

    def test_list_shaped_output_is_accepted(self):
        self.fake_run(json.dumps({"clients":[{"mac":MAC,"ip":"10.20.0.50","state":"Preauthenticated"}]}))
        self.assertEqual(self.get("/api/clients").json["clients"][0]["state"],"Preauthenticated")

    def test_opennds_down_is_reported_not_crashed(self):
        for error in (subprocess.CalledProcessError(1,["ndsctl"]),FileNotFoundError(),
                      subprocess.TimeoutExpired(["ndsctl"],5)):
            with self.subTest(error=type(error).__name__):
                self.fake_run(error=error)
                self.assertEqual(self.get("/api/clients").status_code,502)
        self.fake_run("ndsctl: opennds probably not yet started")
        self.assertEqual(self.get("/api/clients").status_code,502)

class FakeNds:
    """Behaves like ndsctl on openNDS 10.3.1 as observed on the board."""
    def __init__(self):
        self.authed={}; self.calls=[]; self.down=False
    def run(self,argv,**kw):
        self.calls.append(argv[1:])
        if self.down:
            return subprocess.CompletedProcess(argv,1,"","ndsctl: opennds probably not yet started (Error: No such file or directory)\n")
        cmd,mac=argv[1],argv[2]
        if cmd=="auth":
            if mac in self.authed:
                return subprocess.CompletedProcess(argv,1,f"Failed to authenticate client {','.join(argv[2:])}.\n","")
            self.authed[mac]=int(argv[3])
            return subprocess.CompletedProcess(argv,0,f"Client {','.join(argv[2:])} authenticated.\n","")
        if cmd=="deauth":
            if self.authed.pop(mac,None) is None:
                return subprocess.CompletedProcess(argv,0,f"Client {mac} not found.\n","")
            return subprocess.CompletedProcess(argv,0,f"Client {mac} deauthenticated.\n","")
        raise AssertionError(argv)

class PortalFlowTest(CreditdTest):
    def setUp(self):
        super().setUp()
        self.nds=FakeNds()
        for target,name,value in ((creditd,"ENABLE_NDS",True),(creditd.subprocess,"run",self.nds.run)):
            p=mock.patch.object(target,name,value); p.start(); self.addCleanup(p.stop)

    def customer(self,mac=MAC):
        return self.get(f"/api/customer?mac={mac}").json

    def buy(self,pesos,mac=MAC):
        self.post("/api/insert-coin",{"mac":mac}); self.coin(pesos)

class ConnectTests(PortalFlowTest):
    def test_coins_become_internet_time(self):
        self.buy(5)
        self.assertEqual(self.customer()["credit_minutes"],25)
        r=self.post("/api/connect",{"mac":MAC})
        self.assertEqual((r.status_code,r.json["minutes_added"],r.json["seconds_left"]),(200,25,25*60))
        self.assertEqual(self.nds.authed,{MAC:25})
        state=self.customer()
        self.assertEqual((state["credit_minutes"],state["session"]["active"]),(0,True))
        self.assertEqual(state["window"],{"state":"mine","open":False,"seconds_left":0,"pesos":5,"minutes":25})

    def test_buying_more_while_online_adds_to_time_left(self):
        self.buy(5); self.post("/api/connect",{"mac":MAC})
        self.at(10*60); self.buy(10)
        r=self.post("/api/connect",{"mac":MAC})
        self.assertEqual(r.json["seconds_left"],(15+50)*60)  # 15 min left + 50 bought
        self.assertEqual(self.nds.authed,{MAC:65})
        self.assertIn(["deauth",MAC],self.nds.calls)  # openNDS can't extend in place

    def test_no_credit(self):
        self.post("/api/insert-coin",{"mac":MAC})
        r=self.post("/api/connect",{"mac":MAC})
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.customer()["window"]["open"],False)  # window closed anyway
        self.assertEqual(self.nds.calls,[])

    def test_opennds_down_keeps_the_credit(self):
        self.buy(5); self.nds.down=True
        r=self.post("/api/connect",{"mac":MAC})
        self.assertEqual(r.status_code,502)
        self.assertEqual(self.customer()["credit_minutes"],25)
        self.assertEqual(self.rows("SELECT * FROM ledger WHERE kind='paid_session'"),[])

    def test_switched_off_until_enabled(self):
        self.buy(5)
        with mock.patch.object(creditd,"ENABLE_NDS",False):
            r=self.post("/api/connect",{"mac":MAC})
        self.assertEqual(r.status_code,409); self.assertIn("PISOWIFI_ENABLE_NDS_AUTH",r.json["error"])

    def test_customer_sees_busy_coinslot(self):
        self.post("/api/insert-coin",{"mac":OTHER})
        self.at(10)
        w=self.customer()["window"]
        self.assertEqual(w,{"state":"busy","retry_after":55})
        self.assertEqual(self.customer(OTHER)["window"]["state"],"mine")

class LateCoinTests(PortalFlowTest):
    def test_coin_detected_before_this_window_is_not_given_to_this_customer(self):
        self.at(100); self.post("/api/insert-coin",{"mac":OTHER})
        seen=time.strftime("%Y-%m-%dT%H:%M:%S%z",time.localtime(T0+30))  # inserted before OTHER tapped
        r=self.coin(5,detected_at=seen[:-2]+":"+seen[-2:])
        self.assertIsNone(r.json["mac"])
        self.assertEqual(self.customer(OTHER)["credit_minutes"],0)

    def test_coin_detected_during_this_window_is_credited_even_if_late(self):
        self.post("/api/insert-coin",{"mac":MAC})
        seen=time.strftime("%Y-%m-%dT%H:%M:%S%z",time.localtime(T0+5))
        self.at(40); r=self.coin(10,detected_at=seen[:-2]+":"+seen[-2:])
        self.assertEqual(r.json["mac"],MAC)

class ReconcileTests(PortalFlowTest):
    def reconcile(self):
        return self.post("/api/sessions/reconcile",{}).json

    def test_paid_time_comes_back_after_a_reboot(self):
        self.buy(20); self.post("/api/connect",{"mac":MAC})
        self.nds.authed.clear()  # reboot: openNDS forgot everyone
        self.at(30*60)
        r=self.reconcile()
        self.assertEqual(r["reauthorized"],[MAC])
        self.assertEqual(self.nds.authed,{MAC:70})  # 100 bought - 30 used

    def test_still_online_customers_are_left_alone(self):
        self.buy(5); self.post("/api/connect",{"mac":MAC})
        self.assertEqual(self.reconcile()["reauthorized"],[])
        self.assertEqual(self.nds.authed,{MAC:25})

    def test_last_minute_is_not_reauthorized(self):
        self.buy(1); self.post("/api/connect",{"mac":MAC}); self.nds.authed.clear()
        self.at(4*60+30)
        self.assertEqual(self.reconcile()["reauthorized"],[])

    def test_credit_nobody_started_is_started_after_the_window(self):
        self.buy(5)
        self.assertEqual(self.reconcile()["started"],[])  # still inserting coins
        self.at(66)  # window (60 s) and grace (5 s) are over
        self.assertEqual(self.reconcile()["started"],[MAC])
        self.assertEqual(self.nds.authed,{MAC:25})
        self.assertEqual(self.customer()["credit_minutes"],0)

    def test_switched_off_does_nothing(self):
        self.buy(5); self.at(66)
        with mock.patch.object(creditd,"ENABLE_NDS",False):
            self.assertEqual(self.reconcile(),{"ok":True,"started":[],"reauthorized":[],"errors":[]})
        self.assertEqual(self.nds.calls,[])

def auth_calls(nds):
    return [c for c in nds.calls if c[0]=="auth"]

class SpeedLimitTests(PortalFlowTest):
    def test_limits_are_passed_to_opennds(self):
        self.post("/api/settings",{"download_kbps":2000,"upload_kbps":500})
        self.buy(5); self.post("/api/connect",{"mac":MAC})
        self.assertEqual(auth_calls(self.nds)[-1],["auth",MAC,"25","500","2000","0","0","piso"])

    def test_default_is_no_limit(self):
        self.buy(5); self.post("/api/connect",{"mac":MAC})
        self.assertEqual(auth_calls(self.nds)[-1][3:5],["0","0"])

class PromoTests(CreditdTest):
    def test_price_uses_the_best_mix(self):
        tiers=[(10,70),(20,150)]
        cases={1:5,3:15,9:45,10:70,15:95,20:150,30:220,40:300}
        for pesos,minutes in cases.items():
            with self.subTest(pesos=pesos):
                self.assertEqual(creditd.price(pesos,5,tiers),minutes)

    def test_promo_applies_to_the_window_total(self):
        self.post("/api/promos",{"promos":[{"pesos":10,"minutes":70}]})
        self.post("/api/insert-coin",{"mac":MAC})
        self.assertEqual(self.coin(5).json["minutes"],25)
        self.assertEqual(self.coin(5).json["minutes"],45)  # ₱10 in total buys 70
        self.assertEqual(self.get("/api/insert-coin").json["minutes"],70)

    def test_unassigned_coin_still_gets_the_promo(self):
        self.post("/api/promos",{"promos":[{"pesos":10,"minutes":70}]})
        self.assertEqual(self.coin(10).json["minutes"],70)

    def test_rate_card(self):
        r=self.post("/api/promos",{"promos":[{"pesos":10,"minutes":70},{"pesos":50,"minutes":400}]}).json
        self.assertEqual(r["rate_card"],[{"pesos":1,"minutes":5},{"pesos":5,"minutes":25},{"pesos":10,"minutes":70},
                                         {"pesos":20,"minutes":140},{"pesos":50,"minutes":400}])
        self.assertEqual(self.post("/api/promos",{"promos":[]}).json["promos"],[])

    def test_bad_promos(self):
        for body in ({"promos":"10=70"},{"promos":[{"pesos":1,"minutes":9}]},
                     {"promos":[{"pesos":10,"minutes":70},{"pesos":10,"minutes":80}]},
                     {"promos":[{"pesos":10,"minutes":0}]},
                     {"promos":[{"pesos":p,"minutes":1} for p in range(2,14)]}):
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.post("/api/promos",body).status_code,400)

class PauseTests(PortalFlowTest):
    def online(self,pesos=20):
        self.buy(pesos); self.post("/api/connect",{"mac":MAC})

    def test_pause_keeps_the_time_and_resume_gives_it_back(self):
        self.online()  # 100 min
        self.at(10*60)
        r=self.post("/api/pause",{"mac":MAC})
        self.assertEqual((r.status_code,r.json["paused_seconds"]),(200,90*60))
        self.assertNotIn(MAC,self.nds.authed)
        session=self.customer()["session"]
        self.assertEqual((session["paused"],session["active"],session["paused_seconds"]),(True,False,5400))
        self.at(70*60)  # an hour later, nothing was used
        self.assertEqual(self.reconcile_now()["reauthorized"],[])
        r=self.post("/api/resume",{"mac":MAC})
        self.assertEqual(r.json["seconds_left"],5400)
        self.assertEqual(self.nds.authed,{MAC:90})

    def reconcile_now(self):
        return self.post("/api/sessions/reconcile",{}).json

    def test_buying_more_while_paused_adds_and_resumes(self):
        self.online(); self.at(10*60); self.post("/api/pause",{"mac":MAC})
        self.at(20*60); self.buy(5)
        r=self.post("/api/connect",{"mac":MAC})
        self.assertEqual(r.json["seconds_left"],(90+25)*60)
        self.assertFalse(self.customer()["session"]["paused"])

    def test_refusals(self):
        self.assertEqual(self.post("/api/resume",{"mac":MAC}).status_code,409)
        self.online(1)
        self.at(4*60+30)
        self.assertEqual(self.post("/api/pause",{"mac":MAC}).status_code,409)  # under a minute left
        self.at(60)
        self.post("/api/pause",{"mac":MAC})
        self.assertEqual(self.post("/api/pause",{"mac":MAC}).status_code,409)

    def test_opennds_down_does_not_pause(self):
        self.online(); self.nds.down=True
        self.assertEqual(self.post("/api/pause",{"mac":MAC}).status_code,502)
        self.assertFalse(self.customer()["session"]["paused"])

class VoucherTests(PortalFlowTest):
    def batch(self,count=3,minutes=60,price=20):
        return self.post("/api/vouchers",{"count":count,"minutes":minutes,"price":price}).json

    def test_codes_are_readable_and_unique(self):
        r=self.batch(count=50)
        self.assertEqual(len(set(r["codes"])),50)
        for code in r["codes"]:
            self.assertRegex(code,r"^[A-HJ-NP-Z2-9]{8}$")

    def test_redeem_starts_the_time_and_works_once(self):
        code=self.batch()["codes"][0]
        typed=f"{code[:4].lower()}-{code[4:]}"
        r=self.post("/api/redeem",{"mac":MAC,"code":typed})
        self.assertEqual((r.status_code,r.json["started"],r.json["seconds_left"]),(200,True,3600))
        self.assertEqual(self.nds.authed,{MAC:60})
        self.assertEqual(self.post("/api/redeem",{"mac":OTHER,"code":code}).status_code,409)

    def test_wrong_codes_lock_the_phone(self):
        good=self.batch()["codes"][0]
        for _ in range(5):
            self.assertEqual(self.post("/api/redeem",{"mac":MAC,"code":"ZZZZZZZZ"}).status_code,404)
        r=self.post("/api/redeem",{"mac":MAC,"code":good})
        self.assertEqual((r.status_code,r.json["retry_after"]),(429,600))
        self.at(600)
        self.assertEqual(self.post("/api/redeem",{"mac":MAC,"code":good}).status_code,200)

    def test_guessing_from_many_phones_slows_everyone(self):
        for i in range(30):
            self.post("/api/redeem",{"mac":f"aa:bb:cc:dd:{i//256:02x}:{i%256:02x}","code":"ZZZZZZZZ"})
        self.assertEqual(self.post("/api/redeem",{"mac":MAC,"code":"ZZZZZZZZ"}).status_code,429)

    def test_void_and_batch_summary(self):
        r=self.batch(count=4)
        self.post("/api/redeem",{"mac":MAC,"code":r["codes"][0]})
        self.assertEqual(self.post("/api/vouchers/void",{"batch":r["batch"]}).json["voided"],3)
        self.assertEqual(self.post("/api/redeem",{"mac":OTHER,"code":r["codes"][1]}).status_code,404)
        b=self.get("/api/voucher-batches").json["batches"][0]
        self.assertEqual((b["batch"],b["total"],b["used"],b["void"],b["minutes"],b["price"]),(r["batch"],4,1,3,60,20))
        listed=self.get(f"/api/vouchers?batch={r['batch']}").json["vouchers"]
        self.assertEqual(sorted(v["code"] for v in listed),sorted(r["codes"]))

    def test_redeem_with_internet_off_keeps_the_credit(self):
        code=self.batch()["codes"][0]
        with mock.patch.object(creditd,"ENABLE_NDS",False):
            r=self.post("/api/redeem",{"mac":MAC,"code":code})
        self.assertEqual((r.json["started"],self.customer()["credit_minutes"]),(False,60))

    def test_bad_batch_requests(self):
        for body in ({"count":0,"minutes":60},{"count":3,"minutes":0},{"count":201,"minutes":60},{"count":3,"minutes":60,"price":-1}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/vouchers",body).status_code,400)

class TapLimitTests(CreditdTest):
    def tap(self,mac=MAC):
        return self.post("/api/insert-coin",{"mac":mac})

    def test_three_empty_windows_then_a_wait(self):
        for t in (0,70,140):
            self.at(t); self.assertEqual(self.tap().status_code,200)
        self.at(210)
        r=self.tap()
        self.assertEqual((r.status_code,r.json["retry_after"]),(429,300))
        self.assertEqual(self.get(f"/api/customer?mac={MAC}").json["insert_blocked_for"],300)
        self.assertEqual(self.tap(OTHER).status_code,200)  # others can still use the slot
        self.at(210+300+70)
        self.assertEqual(self.tap().status_code,200)

    def test_a_coin_clears_the_strikes(self):
        for t in (0,70):
            self.at(t); self.tap()
        self.coin(5)
        for t in (140,210,280):
            self.at(t); self.assertEqual(self.tap().status_code,200)

    def test_tapping_again_without_a_coin_does_not_extend(self):
        self.tap()
        self.at(30); self.assertEqual(self.tap().json["seconds_left"],30)
        self.coin(1)
        self.at(40); self.assertEqual(self.tap().json["seconds_left"],60)

    def test_done_then_tap_again_cannot_hold_the_slot(self):
        for t in (0,10,20):
            self.at(t); self.tap()
            self.post("/api/insert-coin/done",{"mac":MAC})
        self.at(30)
        self.assertEqual(self.tap().status_code,429)

class AssignTests(PortalFlowTest):
    def test_owner_gives_an_unassigned_coin_to_a_customer(self):
        self.coin(5)  # nobody had tapped Insert Coin
        coin_id=self.get("/api/coins?limit=1").json["coins"][0]["coin_id"]
        r=self.post("/api/coins/assign",{"coin_id":coin_id,"mac":MAC})
        self.assertEqual((r.status_code,r.json["started"]),(200,True))
        self.assertEqual(self.nds.authed,{MAC:25})
        self.assertEqual(self.get("/api/status").json["unassigned_coin_pesos"],0)
        self.assertEqual(self.post("/api/coins/assign",{"coin_id":coin_id,"mac":OTHER}).status_code,409)
        self.assertEqual(self.post("/api/coins/assign",{"coin_id":str(uuid.uuid4()),"mac":MAC}).status_code,404)

class AppearanceTests(CreditdTest):
    def test_defaults_and_choices(self):
        a=self.get("/api/appearance").json
        self.assertEqual((a["title"],a["dashboard_mode"],a["dashboard_accent"]),("Piso WiFi","auto","gold"))
        self.assertIn("purple",a["choices"]["accents"])

    def test_title_and_theme_are_saved(self):
        r=self.post("/api/appearance",{"title":"  Aling   Nena's  WiFi ","dashboard_mode":"dark","dashboard_accent":"blue"})
        self.assertEqual((r.status_code,r.json["title"]),(200,"Aling Nena's WiFi"))
        a=self.get("/api/appearance").json
        self.assertEqual((a["title"],a["dashboard_mode"],a["dashboard_accent"]),("Aling Nena's WiFi","dark","blue"))
        self.post("/api/appearance",{"dashboard_accent":"green"})  # others stay
        self.assertEqual(self.get("/api/appearance").json["title"],"Aling Nena's WiFi")

    def test_bad_values_change_nothing(self):
        for body in ({"title":""},{"title":"x"*41},{"title":"<script>"},{"title":"a\nb\x07"},{"title":5},
                     {"dashboard_mode":"neon"},{"dashboard_accent":"#ff0000"},{},
                     {"title":"Fine","dashboard_mode":"neon"}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/appearance",body).status_code,400)
        self.assertEqual(self.get("/api/appearance").json["title"],"Piso WiFi")

    def test_default_title_comes_from_the_env_file(self):
        with mock.patch.object(creditd,"TITLE","Barangay WiFi"):
            self.assertEqual(self.get("/api/appearance").json["title"],"Barangay WiFi")

class MigrationTests(unittest.TestCase):
    def test_existing_paid_time_moves_into_the_access_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=os.path.join(tmp,"old.sqlite3")
            old=__import__("sqlite3").connect(path)
            old.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL, "
                        "pesos INTEGER NOT NULL DEFAULT 0, minutes INTEGER NOT NULL DEFAULT 0, mac TEXT NOT NULL DEFAULT '', "
                        "expires INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '')")
            old.execute("INSERT INTO ledger(ts,kind,minutes,mac,expires) VALUES(1,'paid_session',25,?,5000)",(MAC,))
            old.execute("INSERT INTO ledger(ts,kind,minutes,mac,expires) VALUES(2,'paid_session',25,?,9000)",(MAC,))
            old.commit(); old.close()
            with mock.patch.object(creditd,"DB",path):
                c=creditd.db()
                self.assertEqual(creditd.access(c,MAC),(9000,0))
                self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0],1)
                c.close()

if __name__=="__main__":
    unittest.main()
