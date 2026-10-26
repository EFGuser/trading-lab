#!/usr/bin/env python3
"""15-minute-bar, educational PAPER-ONLY stock-scanning engine.
Free-source Yahoo data is NOT a guaranteed live exchange stream.
No brokers, real orders, payments, or GPT API calls.
"""
import argparse
import datetime as dt
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "intraday_state.json"
AUDIT = ROOT / "data" / "intraday_audit.jsonl"
PUBLIC = ROOT / "site" / "data" / "intraday_snapshot.json"
WATCH = ("AAPL", "NVDA", "MSFT", "AMZN", "AMD", "GOOGL", "TE", "SPY", "QQQ")
INITIAL = 10000.0
FEE = 1.0
SLIP = 0.001
MAX_HOLDINGS = 3

def stamp(t):
    return t.astimezone(UTC).isoformat(timespec="seconds")

def init():
    return {"version": 1, "cash": INITIAL, "positions": {}, "pending": {},
            "latest_bar": {}, "last_price": {}, "equity": [], "trades": [],
            "events": [], "scans": {}, "runs": 0, "total_events": 0,
            "start_benchmark": None}

def fresh(now):
    """Only scan normal market hours, not an exchange-calendar substitute."""
    t = now.astimezone(NY)
    return t.weekday() < 5 and dt.time(9, 45) <= t.time() <= dt.time(16, 40)

def parse_history(symbol):
    import yfinance as yf
    frame = yf.Ticker(symbol).history(period="1mo", interval="15m",
                                      auto_adjust=False, actions=False,
                                      prepost=False, timeout=15, raise_errors=True)
    if frame.empty:
        raise ValueError("provider supplied zero bars")
    result = []
    for index, x in frame.iterrows():
        start = index.to_pydatetime().astimezone(NY)
        # Only regular-session 15-minute bars. The final bar is 15:45-16:00.
        if start.weekday() >= 5 or not(dt.time(9, 30) <= start.time() < dt.time(16, 0)):
            continue
        try:
            o, h, l, c, v = [float(x[k]) for k in ("Open","High","Low","Close","Volume")]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(q) for q in (o, h, l, c, v)):
            continue
        if min(o, h, l, c) <= 0 or h < max(o,c) or l > min(o,c) or v < 0:
            continue
        result.append({"time": stamp(start),"open":o,"high":h,"low":l,
                       "close":c,"volume":v})
    result.sort(key=lambda a: a["time"])
    return result

def indicators(bars):
    if len(bars) < 80:
        raise ValueError("need 80 fifteen-minute bars to warm up")
    closes = [x["close"] for x in bars]
    def ema(n):
        seed = sum(closes[:n])/n
        k = 2/(n+1)
        for v in closes[n:]:
            seed = v*k + seed*(1-k)
        return seed
    up, down, tr = 0.0, 0.0, 0.0
    for i in range(len(bars)-14, len(bars)):
        d = closes[i]-closes[i-1]
        up += max(0,d)
        down += max(0,-d)
        tr += max(bars[i]["high"]-bars[i]["low"],
                  abs(bars[i]["high"]-closes[i-1]),
                  abs(bars[i]["low"]-closes[i-1]))
    rsi = (100 if down==0 and up>0 else
           50 if down==0 and up==0 else 100-100/(1+up/down))
    vavg = sum(x["volume"] for x in bars[-21:-1])/20
    return {"close":round(closes[-1],4), "ema12":round(ema(12),4),
            "ema48":round(ema(48),4), "rsi14":round(rsi,2),
            "atr14":round(tr/14,4), "relative_volume":round(bars[-1]["volume"]/vavg,2) if vavg else 0,
            "prev20_high":round(max(x["high"] for x in bars[-21:-1]),4),
            "prev_close":round(closes[-2],4),
            "volatility_pct":round(100*(tr/14)/closes[-1],2)}

def assess(ind, holding):
    checks = [
        {"name": "Uptrend EMA12 > EMA48", "pass": ind["ema12"]>ind["ema48"] and ind["close"]>ind["ema48"]},
        {"name": "RSI14 in 45-72", "pass": 45<=ind["rsi14"]<=72},
        {"name": "Volume >= 0.70x norm", "pass": ind["relative_volume"]>=0.70},
        {"name": "ATR volatility 0.1%-4%", "pass": 0.1<=ind["volatility_pct"]<=4.0},
        {"name": "Breakout / EMA12 reclaim", "pass":
             (ind["close"]>ind["prev20_high"] and ind["relative_volume"]>=1.05)
             or (ind["prev_close"]<=ind["ema12"] and ind["close"]>ind["ema12"])}
    ]
    if holding:
        trail = max(holding["max_close"],ind["close"])-2.3*ind["atr14"]
        exits = []
        if ind["close"]<trail: exits.append("2.3 ATR trailing threshold")
        if ind["close"]<ind["ema48"]: exits.append("Below 48-bar EMA")
        if ind["rsi14"]>80: exits.append("RSI above 80")
        return ("SELL" if exits else "HOLD",
                "; ".join(exits) if exits else "No exit signal", checks,
                round(trail,4))
    failed = [x["name"] for x in checks if not x["pass"]]
    return ("HOLD" if failed else "BUY",
            "Missing: "+", ".join(failed) if failed else "All five entry filters passed",
            checks, None)

def log(s, events, at, kind, sym=None, **details):
    e = {"checked_utc": stamp(at), "type": kind, "symbol": sym, **details}
    events.append(e)
    s["total_events"] += 1
    s["events"].insert(0,e)
    s["events"]=s["events"][:160]

def net(s):
    return s["cash"]+sum(p["qty"]*s["last_price"].get(sym,p["entry"])
                         for sym,p in s["positions"].items())

def execute(s, events, symbol, bar, at):
    pending = s["pending"].pop(symbol)
    action = pending["side"]
    price = bar["open"]*(1+SLIP if action=="BUY" else 1-SLIP)
    if action=="BUY":
        if symbol in s["positions"] or len(s["positions"])>=MAX_HOLDINGS:
            log(s,events,at,"BLOCKED",symbol,reason="Position count changed")
            return
        qty=min(pending["qty"],int((s["cash"]-FEE)//price))
        if qty<=0:
            log(s,events,at,"BLOCKED",symbol,reason="No buying power")
            return
        cost=qty*price+FEE
        s["cash"]-=cost
        s["positions"][symbol]={"qty":qty,"entry":round(price,4),"basis":cost,
                                 "max_close":bar["close"],"opened":bar["time"]}
        pnl=None
    else:
        pos=s["positions"].pop(symbol,None)
        if not pos:
            log(s,events,at,"BLOCKED",symbol,reason="Position already closed")
            return
        qty=pos["qty"]
        proceeds=qty*price-FEE
        s["cash"]+=proceeds
        pnl=round(proceeds-pos["basis"],2)
    t = {"time":bar["time"],"symbol":symbol,"side":action,"qty":qty,
         "fill_price":round(price,4),"reason":pending["reason"],
         "signal_bar":pending["signal_bar"],"pnl":pnl,"mock":True}
    s["trades"].insert(0,t)
    s["trades"]=s["trades"][:200]
    log(s,events,at,"PAPER_FILL",symbol,**{k:v for k,v in t.items() if k!="symbol"})

def queue(s,events,symbol,bar,at,decision,ind):
    if decision["action"]=="HOLD":
        return
    action=decision["action"]
    if symbol in s["pending"]:
        log(s,events,at,"BLOCKED",symbol,reason="Already queued")
        return
    if action=="BUY":
        count=len(s["positions"])+sum(x["side"]=="BUY" for x in s["pending"].values())
        if count>=MAX_HOLDINGS:
            log(s,events,at,"BLOCKED",symbol,reason="Three-position cap")
            return
        v=net(s)
        entry=ind["close"]
        per_share=2*ind["atr14"]
        qty=min(int(v*.20//entry),int(v*.01//per_share) if per_share>0 else 0,
                int((s["cash"]-FEE)//entry))
        if qty<=0:
            log(s,events,at,"BLOCKED",symbol,reason="Risk budget or cash cap")
            return
    else:
        qty=s["positions"].get(symbol,{}).get("qty",0)
        if qty<=0:
            return
    s["pending"][symbol]={"side":action,"qty":qty,"signal_bar":bar["time"],
                          "reason":decision["reason"]}
    log(s,events,at,"PAPER_ORDER_QUEUED",symbol,side=action,qty=qty,
        signal_bar=bar["time"],reason=decision["reason"])

def cycle(history, now, state=None, errors=None):
    s=state if state is not None else (json.loads(STATE.read_text()) if STATE.exists() else init())
    if s["version"]!=1:
        raise ValueError("Unexpected intraday state version")
    events=[]
    s["runs"]+=1
    problems=dict(errors or {})
    all_new=[]
    scans={}
    for sym in WATCH:
        bars=history.get(sym,[])
        # A bar indexed at its START is not complete until 15 minutes later;
        # use a further 8-minute buffer for delayed publication.
        cutoff=now-dt.timedelta(minutes=8)
        bars=[b for b in bars if dt.datetime.fromisoformat(b["time"])+dt.timedelta(minutes=15)<=cutoff]
        last=s["latest_bar"].get(sym)
        eligible=[(i,b) for i,b in enumerate(bars) if last is None or b["time"]>last]
        if not bars:
            problems.setdefault(sym,"No completed 15-minute bars from provider")
            continue
        if fresh(now) and (now-(dt.datetime.fromisoformat(bars[-1]["time"])+dt.timedelta(minutes=15))).total_seconds()>45*60:
            problems.setdefault(sym,"Latest completed intraday bar is more than 45 minutes old")
            continue
        if last is None:
            eligible=eligible[-1:] # Never backfill fake executions on initial launch
        elif len(eligible)>3:
            # Missed more than 45 minutes: don't pretend orders filled during missing scans.
            eligible=eligible[-1:]
            if sym in s["pending"]:
                s["pending"].pop(sym)
                log(s,events,now,"PAPER_ORDER_CANCELLED",sym,reason="Skipped missed bars")
        for i, b in eligible:
            all_new.append((b["time"],sym,i,b,bars))
    all_new.sort(key=lambda x:(x[0],x[1]))
    for _,sym,i,bar,bars in all_new:
        bar_time=bar["time"]
        # At the close of a completed bar, trades signaled on an EARLIER bar
        # may be modeled at this bar's open, never the original signal close.
        order=s["pending"].get(sym)
        if order and order["signal_bar"]<bar_time:
            execute(s,events,sym,bar,now)
        ind=indicators(bars[:i+1]) if i>=79 else None
        if ind is None:
            log(s,events,now,"SKIP",sym,bar=bar_time,reason="Insufficient indicator lookback")
            s["latest_bar"][sym]=bar_time
            continue
        holding=s["positions"].get(sym)
        if holding:
            holding["max_close"]=max(holding["max_close"],bar["close"])
        action,reason,checks,trail=assess(ind,holding)
        s["last_price"][sym]=bar["close"]
        scan={"symbol":sym,"bar_time":bar_time,"action":action,"reason":reason,
              "indicators":ind,"checks":checks,"trailing":trail}
        scans[sym]=scan
        s["scans"][sym]=scan
        log(s,events,now,"SCAN",sym,bar=bar_time,action=action,
            reason=reason,indicators=ind,checks=checks)
        queue(s,events,sym,bar,now,scan,ind)
        s["latest_bar"][sym]=bar_time
        if sym=="SPY":
            if s["start_benchmark"] is None:
                s["start_benchmark"]=bar["close"]
            s["benchmark_last"]=bar["close"]
    if all_new:
        t=max(a[0] for a in all_new)
        s["equity"].append({"bar":t,"value":round(net(s),2)})
        s["equity"]=s["equity"][-600:]
    for sym,msg in problems.items():
        log(s,events,now,"DATA_WARNING",sym,reason=str(msg)[:150])
    age={}
    for sym,bar in s["latest_bar"].items():
        age[sym]=round((now-dt.datetime.fromisoformat(bar)).total_seconds()/60)
    status="UPDATED" if all_new else ("PROVIDER_UNAVAILABLE" if problems else "NO_NEW_COMPLETED_BAR")
    if not all_new and not problems:
        log(s,events,now,"NO_CHANGE",reason="No new completed 15-minute prices")
    view={"version":1,"mode":"15m delayed/provider-dependent, NOT guaranteed live",
          "timestamp_utc":stamp(now),"status":status,"updated_symbols":sorted(scans),
          "source":"yfinance/Yahoo Finance (unofficial intraday 15m bars)",
          "market_session":"OPEN_WINDOW" if fresh(now) else "OUTSIDE_TRADING_WINDOW",
          "age_minutes":age,"errors":problems,"scans":list(s["scans"].values()),
          "equity":round(net(s),2),"cash":round(s["cash"],2),
          "positions":s["positions"],"pending":s["pending"],
          "trades":s["trades"][:100],"audit":s["events"][:100],
          "history":s["equity"],"total_events":s["total_events"],"runs":s["runs"],
          "last_bars":s["latest_bar"],"starting_cash":INITIAL,
          "spy_buy_hold_pct":round(100*(s.get("benchmark_last", s["start_benchmark"] or 1)/(s["start_benchmark"] or 1)-1),2) if s["start_benchmark"] else None,
          "portfolio_return_pct":round(100*(net(s)/INITIAL-1),2)}
    return s,events,view

def save(s,events,view):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    PUBLIC.parent.mkdir(parents=True,exist_ok=True)
    STATE.write_text(json.dumps(s,separators=(",",":"),allow_nan=False)+"\n")
    PUBLIC.write_text(json.dumps(view,separators=(",",":"),allow_nan=False)+"\n")
    if events:
        with AUDIT.open("a",encoding="utf8") as f:
            for e in events:
                f.write(json.dumps(e,separators=(",",":"),allow_nan=False)+"\n")

def self_test():
    start=dt.datetime(2026,10,23,9,30,tzinfo=NY)
    history={}
    for sym in WATCH:
        bars=[]
        for i in range(110):
            t=start+dt.timedelta(minutes=15*i)
            # Fixture only; no market data network request in tests.
            p=100+i*.1+2*math.sin(i/7)
            bars.append({"time":stamp(t),"open":p-.1,"high":p+.3,
                         "low":p-.4,"close":p,"volume":90000+i*90})
        history[sym]=bars
    now=start+dt.timedelta(minutes=15*110+30)
    s,e,v=cycle(history,now,state=init())
    assert v["status"]=="UPDATED" and len(v["updated_symbols"])==len(WATCH)
    assert not v["trades"],"Never invent a fill during first scan"
    # Direct paper order fill test, including audit record.
    s2=init()
    s2["pending"]["AAPL"]={"side":"BUY","qty":2,"signal_bar":"2026-10-22T14:30:00+00:00","reason":"fixture"}
    ev=[]
    execute(s2,ev,"AAPL",{"time":"2026-10-23T14:30:00+00:00","open":100.0,"close":101.0},now)
    assert s2["positions"]["AAPL"]["qty"]==2 and len(s2["trades"])==1
    assert any(x["type"]=="PAPER_FILL" for x in ev)
    before=s["total_events"]
    s,e,v=cycle(history,now,state=s)
    assert not v["updated_symbols"] and not v["trades"],"Repeat run must not create trades"
    assert v["total_events"]==before+1
    s,e,v=cycle({},now,state=init(),errors={sym:"test failure" for sym in WATCH})
    assert v["status"]=="PROVIDER_UNAVAILABLE" and not v["trades"]
    print("PASS: first-run guard, completed-bar filtering, idempotence, provider failure")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--self-test",action="store_true")
    args=p.parse_args()
    if args.self_test:
        self_test()
        return
    now=dt.datetime.now(UTC)
    if not fresh(now):
        print("Outside normal NY session polling window; nothing to fetch")
        return
    history={}
    failures={}
    for sym in WATCH:
        try:
            history[sym]=parse_history(sym)
            print(sym,len(history[sym]),"intraday bars received")
        except Exception as ex:
            failures[sym]=type(ex).__name__+": "+str(ex)[:120]
            print(sym,"unavailable",failures[sym])
    s,e,v=cycle(history,now,errors=failures)
    save(s,e,v)
    print("Status:",v["status"],"portfolio:",v["equity"],
          "new scan symbols:",v["updated_symbols"],"audited:",len(e))

if __name__=="__main__":
    main()
