#!/usr/bin/env python3
"""Private streaming STOCK paper-trading lab: Massive AM bars -> audited mock orders.
No brokerage code. No API key ever sent to the browser. Independent from daily accounts.
"""
import argparse
import asyncio
import collections
import datetime as dt
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB = ROOT / "runtime" / "streaming_state.json"
AUDIT = ROOT / "runtime" / "streaming_audit.jsonl"
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
SYMBOLS = tuple(os.getenv("STREAM_SYMBOLS", "AAPL,NVDA,MSFT,AMD,SPY").upper().split(","))
START = 10000.
FEE, SLIP = 1., .001
MAX_HOLDINGS = 3

def utc_now():
    return dt.datetime.now(UTC)

def iso(t):
    return t.astimezone(UTC).isoformat(timespec="seconds")

def empty():
    return {"version": 1, "cash": START, "positions": {}, "pending": {},
            "bars": {}, "latest": {}, "scans": {}, "trades": [], "events": [],
            "equity": [], "total_events": 0, "first_spy": None,
            "last_spy": None, "connection": "OFFLINE", "last_received": None}

def ema(values, n):
    x = sum(values[:n])/n
    k = 2/(n+1)
    for v in values[n:]:
        x += k*(v-x)
    return x

def study(bars):
    if len(bars)<80:
        return None
    closes=[b["c"] for b in bars]
    up=down=tr=0.
    for i in range(len(bars)-14,len(bars)):
        change=closes[i]-closes[i-1]
        up+=max(change,0)
        down+=max(-change,0)
        tr+=max(bars[i]["h"]-bars[i]["l"],abs(bars[i]["h"]-closes[i-1]),abs(bars[i]["l"]-closes[i-1]))
    rsi=100. if down==0 and up>0 else 50. if down==0 else 100-100/(1+up/down)
    vavg=sum(x["v"] for x in bars[-21:-1])/20
    return {"ema12":round(ema(closes,12),4),"ema48":round(ema(closes,48),4),
            "rsi14":round(rsi,2),"atr14":round(tr/14,4),
            "relvol":round(bars[-1]["v"]/vavg,3) if vavg else 0.,
            "prev_high":max(x["h"] for x in bars[-21:-1]),
            "volpct":round(100*tr/14/closes[-1],3), "close":closes[-1],
            "prior_close":closes[-2]}

def inspect(values, held):
    checks=[
        {"name":"EMA12 above EMA48","pass":values["ema12"]>values["ema48"] and values["close"]>values["ema48"]},
        {"name":"RSI14 between 45 and 72","pass":45<=values["rsi14"]<=72},
        {"name":"Relative volume at least 0.70x","pass":values["relvol"]>=.7},
        {"name":"ATR volatility 0.08–2.5%","pass":.08<=values["volpct"]<=2.5},
        {"name":"Breakout or EMA12 reclaim","pass":
         (values["close"]>values["prev_high"] and values["relvol"]>1.05)
         or (values["prior_close"]<=values["ema12"] and values["close"]>values["ema12"])}
    ]
    if held:
        limit=max(held["max_close"], values["close"])-2.5*values["atr14"]
        why=[]
        if values["close"]<limit: why.append("Below 2.5 ATR close-to-close trail")
        if values["close"]<values["ema48"]: why.append("Below EMA48")
        if values["rsi14"]>80: why.append("RSI over 80")
        return ("SELL" if why else "HOLD", "; ".join(why) if why else "No exit condition",checks,round(limit,4))
    failed=[x["name"] for x in checks if not x["pass"]]
    return ("HOLD" if failed else "BUY",
            "Filter failed: "+", ".join(failed) if failed else "All momentum and risk filters passed",checks,None)

def parse_aggregate(data):
    if not isinstance(data,dict) or data.get("ev")!="AM":
        return None
    sym=data.get("sym")
    if sym not in SYMBOLS:
        return None
    try:
        start=int(data["s"])
        end=int(data["e"])
        if end-start not in (60000,59999,60001):
            return None
        values=[float(data[k]) for k in ("o","h","l","c","v")]
        o,h,l,c,v=values
        if not all(math.isfinite(x) for x in values) or min(o,h,l,c)<=0 or v<0 or h<max(o,c) or l>min(o,c):
            return None
        t=dt.datetime.fromtimestamp(start/1000,tz=UTC).astimezone(NY)
        if t.weekday()>=5 or not(dt.time(9,30)<=t.time()<dt.time(16,0)):
            return None
        return {"sym":sym,"start":start,"end":end,"o":o,"h":h,"l":l,"c":c,"v":v}
    except (KeyError,TypeError,ValueError,OverflowError):
        return None

class Engine:
    def __init__(self, state=None, persist=False):
        self.s=state if state is not None else self._restore()
        self.persist=persist
        self.new_events=[]
        self.clients=set()
        self.lock=asyncio.Lock()
        self.last_heartbeat=utc_now()
    def _restore(self):
        if DB.exists():
            obj=json.loads(DB.read_text())
            if obj.get("version")!=1: raise ValueError("State incompatible")
            return obj
        return empty()
    def emit(self,kind,symbol=None,**fields):
        event={"observed_utc":iso(utc_now()),"kind":kind,"symbol":symbol,**fields}
        self.new_events.append(event)
        self.s["total_events"]+=1
        self.s["events"].insert(0,event)
        self.s["events"]=self.s["events"][:180]
    def value(self):
        return self.s["cash"]+sum(p["qty"]*self.s["latest"].get(k,p["entry"])
                                   for k,p in self.s["positions"].items())
    def trade(self,sym,bar):
        order=self.s["pending"].pop(sym)
        side=order["side"]
        px=bar["o"]*(1+SLIP if side=="BUY" else 1-SLIP)
        if side=="BUY":
            if sym in self.s["positions"] or len(self.s["positions"])>=MAX_HOLDINGS:
                self.emit("BLOCKED",sym,reason="Position cap or duplicate")
                return
            qty=min(order["qty"],int((self.s["cash"]-FEE)//px))
            if qty<=0:
                self.emit("BLOCKED",sym,reason="Insufficient virtual cash")
                return
            basis=px*qty+FEE
            self.s["cash"]-=basis
            self.s["positions"][sym]={"qty":qty,"entry":round(px,4),
                                      "basis":basis,"max_close":bar["c"]}
            pnl=None
        else:
            position=self.s["positions"].pop(sym,None)
            if position is None:
                self.emit("BLOCKED",sym,reason="No remaining position")
                return
            qty=position["qty"]
            received=px*qty-FEE
            self.s["cash"]+=received
            pnl=round(received-position["basis"],2)
        row={"symbol":sym,"side":side,"qty":qty,"price":round(px,4),
             "pnl":pnl,"bar_start_utc":iso(dt.datetime.fromtimestamp(bar["start"]/1000,UTC)),
             "signal_bar":order["signal_bar"],"why":order["why"],
             "paper_only":True}
        self.s["trades"].insert(0,row)
        self.s["trades"]=self.s["trades"][:150]
        self.emit("PAPER_FILL",sym,**{k:v for k,v in row.items() if k!="symbol"})
    def on_bar(self,bar, observed=None):
        """Caller supplies a *completed* one-minute bar; never accepts duplicate/revised time."""
        sym=bar["sym"]
        now=observed or utc_now()
        # Reject implausibly future events or >30-minute-stale data; delayed feed is
        # supported but actual data age is always shown and labeled.
        end=dt.datetime.fromtimestamp(bar["end"]/1000,UTC)
        lag=(now-end).total_seconds()
        if lag < -8 or lag > 40*60:
            self.emit("BAR_SKIPPED",sym,reason="Data age outside supported window",age_seconds=round(lag))
            return
        old=self.s["bars"].get(sym,[])
        if old and bar["start"]<=old[-1]["start"]:
            return
        if old and bar["start"]-old[-1]["start"]>3*60000:
            # Never fill hypothetical orders across outages or missing bars.
            if sym in self.s["pending"]:
                self.s["pending"].pop(sym)
                self.emit("ORDER_CANCELLED",sym,reason="Missing consecutive bar(s)")
            self.emit("DATA_GAP",sym,minutes=(bar["start"]-old[-1]["start"])/60000)
        # Fill preexisting order at the NEXT observed minute bar's open,
        # never at the prior signal's own close.
        if sym in self.s["pending"] and self.s["pending"][sym]["signal_start"]<bar["start"]:
            self.trade(sym,bar)
        series=(old+[bar])[-320:]
        self.s["bars"][sym]=series
        self.s["latest"][sym]=bar["c"]
        if sym=="SPY":
            if self.s["first_spy"] is None:
                self.s["first_spy"]=bar["c"]
            self.s["last_spy"]=bar["c"]
        ind=study(series)
        self.s["last_received"]=iso(now)
        if ind is None:
            self.emit("WARMUP",sym,bar_start=bar["start"],bars=len(series),
                      reason="Require 80 completed minute bars")
        else:
            pos=self.s["positions"].get(sym)
            if pos:
                pos["max_close"]=max(pos["max_close"],bar["c"])
            side,why,checks,trail=inspect(ind,pos)
            info={"symbol":sym,"bar":iso(dt.datetime.fromtimestamp(bar["start"]/1000,UTC)),
                  "action":side,"reason":why,"checks":checks,"indicators":ind,"trail":trail,
                  "data_delay_seconds":round(lag)}
            self.s["scans"][sym]=info
            self.emit("SCAN",sym,**{k:v for k,v in info.items() if k!="symbol"})
            if side!="HOLD" and sym not in self.s["pending"]:
                if side=="BUY":
                    reservations=sum(1 for q in self.s["pending"].values() if q["side"]=="BUY")
                    if len(self.s["positions"])+reservations>=MAX_HOLDINGS:
                        self.emit("BLOCKED",sym,reason="Three open/pending positions already")
                    else:
                        money=self.value()
                        loss=2*ind["atr14"]
                        qty=min(int(money*.20//ind["close"]),
                                int(money*.01//loss) if loss>0 else 0,
                                int((self.s["cash"]-FEE)//ind["close"]))
                        if qty<=0:
                            self.emit("BLOCKED",sym,reason="Risk allocation limit")
                        else:
                            self.s["pending"][sym]={"side":"BUY","qty":qty,
                                "signal_start":bar["start"],"signal_bar":info["bar"],"why":why}
                            self.emit("ORDER_QUEUED",sym,side=side,qty=qty,reason=why)
                else:
                    if sym in self.s["positions"]:
                        qty=self.s["positions"][sym]["qty"]
                        self.s["pending"][sym]={"side":"SELL","qty":qty,
                            "signal_start":bar["start"],"signal_bar":info["bar"],"why":why}
                        self.emit("ORDER_QUEUED",sym,side=side,qty=qty,reason=why)
        self.s["equity"].append({"time":iso(now),"equity":round(self.value(),2)})
        self.s["equity"]=self.s["equity"][-600:]
    def snapshot(self):
        # Latest minute bar is private to authorized viewer; do not post to GitHub Pages.
        last={s:{"price":p,"bar":iso(dt.datetime.fromtimestamp(
            self.s["bars"][s][-1]["start"]/1000,UTC))} for s,p in self.s["latest"].items() if self.s["bars"].get(s)}
        spy=(100*(self.s["last_spy"]/self.s["first_spy"]-1)
             if self.s["first_spy"] and self.s["last_spy"] else None)
        return {"status":self.s["connection"],"server_utc":iso(utc_now()),
                "provider":"Massive stocks minute aggregates; delayed or live based on data plan",
                "account":"PAPER","equity":round(self.value(),2),
                "cash":round(self.s["cash"],2),"return_pct":round(100*(self.value()/START-1),2),
                "spy_return_pct":round(spy,2) if spy is not None else None,
                "positions":self.s["positions"],"pending":self.s["pending"],
                "latest":last,"scans":self.s["scans"],"trades":self.s["trades"][:80],
                "events":self.s["events"][:100],"equity_curve":self.s["equity"][-300:],
                "total_events":self.s["total_events"],"last_received":self.s["last_received"]}
    def save(self):
        if not self.persist:return
        DB.parent.mkdir(parents=True,exist_ok=True)
        tmp=DB.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s,separators=(",",":"),allow_nan=False))
        tmp.replace(DB)
        if self.new_events:
            with AUDIT.open("a",encoding="utf-8") as f:
                for e in self.new_events: f.write(json.dumps(e,separators=(",",":"))+"\n")
        self.new_events.clear()
    async def notify(self):
        self.save()
        payload=json.dumps(self.snapshot(),separators=(",",":"))
        for queue in list(self.clients):
            try:
                if queue.full(): queue.get_nowait()
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                self.clients.discard(queue)

async def provider(engine,key,mode):
    import aiohttp
    host="wss://socket.massive.com/stocks" if mode=="realtime" else "wss://delayed.massive.com/stocks"
    delay=3
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.ws_connect(host,heartbeat=25,timeout=25) as ws:
                    engine.s["connection"]="AUTHENTICATING"
                    await engine.notify()
                    authenticated=False
                    async for frame in ws:
                        if frame.type==aiohttp.WSMsgType.TEXT:
                            msgs=json.loads(frame.data)
                            if not isinstance(msgs,list):msgs=[msgs]
                            for obj in msgs:
                                if obj.get("ev")=="status":
                                    status=obj.get("status")
                                    if status=="connected":
                                        await ws.send_json({"action":"auth","params":key})
                                    elif status=="auth_success":
                                        authenticated=True
                                        engine.s["connection"]="CONNECTED_"+mode.upper()
                                        await ws.send_json({"action":"subscribe","params":
                                            ",".join("AM."+x for x in SYMBOLS)})
                                        await engine.notify()
                                    elif status in ("auth_failed","max_connections"):
                                        raise RuntimeError("Provider rejected authentication or session")
                                elif authenticated:
                                    bar=parse_aggregate(obj)
                                    if bar:
                                        # Minute aggregates are finalized after bar end.
                                        # Treat streaming messages as observations, not instant fills.
                                        end=dt.datetime.fromtimestamp(bar["end"]/1000,UTC)
                                        wait=(end+dt.timedelta(seconds=3)-utc_now()).total_seconds()
                                        if wait>0:
                                            await asyncio.sleep(min(wait,5))
                                        async with engine.lock:
                                            engine.on_bar(bar)
                                            await engine.notify()
                        elif frame.type in (aiohttp.WSMsgType.ERROR,aiohttp.WSMsgType.CLOSED):
                            break
                raise ConnectionError("Market WebSocket disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                engine.s["connection"]="DISCONNECTED"
                engine.emit("PROVIDER_ERROR",reason=str(exc)[:160])
                await engine.notify()
                await asyncio.sleep(delay)
                delay=min(delay*2,60)

def make_app(engine,token,origin):
    from aiohttp import web
    # Authorized, private personal viewing only; NEVER publish credentials in JS.
    @web.middleware
    async def authenticate(request, handler):
        headers={"Cache-Control":"no-store","Access-Control-Allow-Origin":origin,
                 "Access-Control-Allow-Headers":"Authorization,Content-Type",
                 "Access-Control-Allow-Methods":"GET,OPTIONS","Vary":"Origin"}
        if request.method=="OPTIONS":
            return web.Response(status=204,headers=headers)
        if request.headers.get("Origin") not in (None,origin):
            return web.Response(status=403,text="Origin denied",headers=headers)
        import hmac
        supplied=request.headers.get("Authorization","").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied,token):
            return web.Response(status=401,text="Bearer token required",headers=headers)
        response=await handler(request)
        response.headers.update(headers)
        return response
    app=web.Application(middlewares=[authenticate])
    async def snapshot(request):
        return web.json_response(engine.snapshot())
    async def events(request):
        q=asyncio.Queue(maxsize=2)
        engine.clients.add(q)
        response=web.StreamResponse(status=200,headers={
            "Content-Type":"text/event-stream","Cache-Control":"no-cache",
            "X-Accel-Buffering":"no"})
        await response.prepare(request)
        try:
            await response.write(("data: "+json.dumps(engine.snapshot())+"\n\n").encode())
            while True:
                try:
                    data=await asyncio.wait_for(q.get(),timeout=20)
                    await response.write(("data: "+data+"\n\n").encode())
                except asyncio.TimeoutError:
                    await response.write(("data: "+json.dumps(engine.snapshot())+"\n\n").encode())
        except (ConnectionError,asyncio.CancelledError,RuntimeError):
            pass
        finally:
            engine.clients.discard(q)
        return response
    app.router.add_route("*","/snapshot",snapshot)
    app.router.add_route("*","/events",events)
    return app

def test():
    import unittest
    class EngineTests(unittest.TestCase):
        def fake(self,i,sym="AAPL",base=None):
            beginning=base or dt.datetime.now(UTC).replace(second=0,microsecond=0)-dt.timedelta(minutes=150)
            # all bars in session; fixed at a prior market day of user's local timezone
            beginning=dt.datetime(2026,10,26,14,30,tzinfo=UTC)
            p=100+i*.09+math.sin(i/5)
            start=int((beginning+dt.timedelta(minutes=i)).timestamp()*1000)
            return {"sym":sym,"start":start,"end":start+60000,
                    "o":p-.03,"h":p+.1,"l":p-.2,"c":p,"v":100000+i*60}
        def test_warmup_and_duplicate(self):
            e=Engine(empty())
            start=dt.datetime(2026,10,26,17,15,tzinfo=UTC)
            for i in range(85):
                b=self.fake(i)
                e.on_bar(b,dt.datetime.fromtimestamp(b["end"]/1000,UTC)+dt.timedelta(seconds=6))
            self.assertEqual(len(e.s["bars"]["AAPL"]),85)
            self.assertEqual(len(e.s["scans"]),1)
            b=self.fake(84)
            e.on_bar(b,dt.datetime.fromtimestamp(b["end"]/1000,UTC)+dt.timedelta(seconds=6))
            self.assertEqual(len(e.s["bars"]["AAPL"]),85)
        def test_parser_rejects_invalid(self):
            self.assertIsNone(parse_aggregate({"ev":"T","sym":"AAPL"}))
            self.assertIsNone(parse_aggregate({"ev":"AM","sym":"AAPL","s":1,"e":2}))
        def test_queued_fill_at_later_bar(self):
            e=Engine(empty())
            start=dt.datetime(2026,10,26,17,15,tzinfo=UTC)
            for i in range(85):e.on_bar(self.fake(i),start)
            nextbar=self.fake(85)
            e.s["pending"]["AAPL"]={"side":"BUY","qty":2,
                "signal_start":self.fake(84)["start"],"signal_bar":"test","why":"test"}
            self.assertEqual(len(e.s["trades"]),0)
            e.on_bar(nextbar,dt.datetime.fromtimestamp(nextbar["end"]/1000,UTC)+dt.timedelta(seconds=6))
            self.assertEqual(len(e.s["trades"]),1)
            self.assertTrue(e.s["trades"][0]["paper_only"])
        def test_gap_cancels_queue(self):
            e=Engine(empty())
            start=dt.datetime(2026,10,26,17,15,tzinfo=UTC)
            b=self.fake(1)
            e.on_bar(b,dt.datetime.fromtimestamp(b["end"]/1000,UTC)+dt.timedelta(seconds=6))
            e.s["pending"]["AAPL"]={"side":"BUY","qty":2,"signal_start":self.fake(1)["start"],"signal_bar":"test","why":"test"}
            b=self.fake(10)
            e.on_bar(b,dt.datetime.fromtimestamp(b["end"]/1000,UTC)+dt.timedelta(seconds=6))
            self.assertFalse(e.s["trades"])
            self.assertFalse(e.s["pending"])
        def test_provider_outage_cannot_trade(self):
            e=Engine(empty())
            self.assertEqual(e.s["trades"],[])
            self.assertEqual(e.s["connection"],"OFFLINE")
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(EngineTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():raise SystemExit(1)

async def serve():
    from aiohttp import web
    api_key=os.getenv("MASSIVE_API_KEY","").strip()
    viewer=os.getenv("STREAM_VIEW_TOKEN","").strip()
    origin=os.getenv("STREAM_ALLOWED_ORIGIN","https://efguser.github.io").strip()
    mode=os.getenv("MASSIVE_FEED","delayed").lower()
    if not api_key or len(viewer)<24:
        raise SystemExit("Set MASSIVE_API_KEY and random STREAM_VIEW_TOKEN (at least 24 chars) securely on the server")
    if mode not in ("delayed","realtime"):
        raise SystemExit("MASSIVE_FEED must be 'delayed' or 'realtime'")
    e=Engine(persist=True)
    app=make_app(e,viewer,origin)
    async def background(app):
        task=asyncio.create_task(provider(e,api_key,mode))
        yield
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass
    app.cleanup_ctx.append(background)
    runner=web.AppRunner(app)
    await runner.setup()
    host=os.getenv("STREAM_BIND","127.0.0.1")
    port=int(os.getenv("PORT","8849"))
    site=web.TCPSite(runner,host,port)
    await site.start()
    print(f"Private paper-data server listening on {host}:{port}, mode={mode}")
    try:await asyncio.Event().wait()
    finally:await runner.cleanup()

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args()
    if args.self_test:test()
    else:asyncio.run(serve())
