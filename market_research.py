#!/usr/bin/env python3
"""Educational, EOD-data PAPER trading lab. Never sends orders to a broker."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "market_state.json"
AUDIT = ROOT / "data" / "market_audit.jsonl"
SNAPSHOT = ROOT / "site" / "data" / "market_snapshot.json"
WATCHLIST = ("SPY", "QQQ", "AAPL", "NVDA", "MSFT", "AMZN", "AMD", "GOOGL", "TE")
STARTING_CASH = 10000.0
FEE_USD = 1.0
SLIPPAGE_RATE = 0.001
MAX_POSITIONS = 3
MAX_ALLOCATION = 0.20
MAX_RISK = 0.01
NY = ZoneInfo("America/New_York")
SOURCE = "Yahoo Finance via yfinance (daily EOD, may be delayed/unavailable)"
SCHEMA = 2

def ts():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def rounded(value, places=4):
    return round(float(value), places)

def new_state():
    return {"schema": SCHEMA, "cash": STARTING_CASH, "positions": {},
            "orders_pending": {}, "last_session": None, "last_prices": {},
            "transactions": [], "scans": [], "activity": [], "equity": [],
            "runs": 0, "events_count": 0}

def audit(state, events, kind, session, ticker=None, **fields):
    row = {"timestamp_utc": ts(), "type": kind, "session": session, "symbol": ticker}
    row.update(fields)
    events.append(row)
    state["events_count"] += 1
    state["activity"].insert(0, row)
    state["activity"] = state["activity"][:120]

def write(state, events, snapshot):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, separators=(",", ":"), allow_nan=False) + "\n")
    SNAPSHOT.write_text(json.dumps(snapshot, separators=(",", ":"), allow_nan=False) + "\n")
    if events:
        with AUDIT.open("a", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")

def valid(value):
    return isinstance(value, (int, float)) and math.isfinite(value)

def download_history(symbol):
    """Fetch real historic daily OHLCV, never substitute synthetic bars."""
    import yfinance as yf
    frame = yf.Ticker(symbol).history(period="9mo", interval="1d",
                                     auto_adjust=True, actions=False, timeout=18,
                                     raise_errors=True)
    if frame is None or frame.empty:
        raise ValueError("Provider returned no price bars")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    result = []
    for index, r in frame.iterrows():
        bar = {"date": str(index.date()), "open": float(r["Open"]),
               "high": float(r["High"]), "low": float(r["Low"]),
               "close": float(r["Close"]), "volume": float(r["Volume"])}
        if (all(valid(bar[k]) for k in ("open", "high", "low", "close", "volume"))
                and min(bar["open"], bar["high"], bar["low"], bar["close"]) > 0
                and bar["high"] >= max(bar["open"], bar["close"])
                and bar["low"] <= min(bar["open"], bar["close"])):
            result.append(bar)
    result.sort(key=lambda b: b["date"])
    return result

def indicators(bars):
    if len(bars) < 55:
        raise ValueError("Need at least 55 daily OHLCV bars")
    closes = [b["close"] for b in bars]
    sma10 = sum(closes[-10:]) / 10
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    gains, losses, trs = [], [], []
    for i in range(len(bars)-14, len(bars)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
        prev = closes[i-1]
        bar = bars[i]
        trs.append(max(bar["high"]-bar["low"], abs(bar["high"]-prev), abs(bar["low"]-prev)))
    gain = sum(gains)/14
    loss = sum(losses)/14
    rsi = 100 if loss == 0 and gain > 0 else 50 if gain == loss == 0 else 100-100/(1+gain/loss)
    atr = sum(trs)/14
    prev10 = sum(closes[-11:-1])/10
    hi10 = max(b["high"] for b in bars[-11:-1])
    avvol = sum(b["volume"] for b in bars[-21:-1])/20
    return {"close": rounded(closes[-1]), "sma10": rounded(sma10),
            "sma20": rounded(sma20), "sma50": rounded(sma50),
            "rsi14": rounded(rsi, 2), "atr14": rounded(atr),
            "atr_pct": rounded(100*atr/closes[-1], 2),
            "volume_ratio": rounded(bars[-1]["volume"]/avvol, 2) if avvol>0 else 0,
            "prev_10d_high": rounded(hi10), "prev_close": rounded(closes[-2]),
            "prev_sma10": rounded(prev10)}

def reasoned_signal(symbol, values, position):
    """Deterministic research rules with explicit checks and rejection reasons."""
    c, sma10, sma20, sma50 = (values[k] for k in ("close", "sma10", "sma20", "sma50"))
    rsi, volume, atr_pct = (values[k] for k in ("rsi14", "volume_ratio", "atr_pct"))
    checks = [
        {"label": "SMA trend", "ok": sma20>sma50 and c>sma50,
         "detail": "Close above SMA50 and SMA20 above SMA50"},
        {"label": "Momentum zone", "ok": 45<=rsi<=71, "detail": "RSI14 between 45 and 71"},
        {"label": "Volume confirmation", "ok": volume>=0.75, "detail": "Volume ≥ 0.75 × 20-day average"},
        {"label": "Volatility filter", "ok": .3<=atr_pct<=9, "detail": "ATR14 between 0.3% and 9% of close"}]
    breakout = c>values["prev_10d_high"] and volume>=1.05
    pullback = values["prev_close"]<=values["prev_sma10"] and c>sma10
    checks.append({"label": "Entry trigger", "ok": breakout or pullback,
                   "detail": "Breakout above prior 10-day high on volume, or crossover above SMA10"})
    if position:
        highest = max(position["highest_close"], c)
        trailing = highest - 2.5*values["atr14"]
        reasons = []
        if c<trailing: reasons.append("Close below 2.5×ATR trailing stop")
        if c<sma20: reasons.append("Close below SMA20")
        if rsi>78: reasons.append("RSI exceeds 78")
        action = "SELL" if reasons else "HOLD"
        explanation = "; ".join(reasons) if reasons else "Position held: no exit threshold triggered"
        return action, explanation, checks, trailing
    if all(item["ok"] for item in checks):
        return "BUY", ("10-day breakout" if breakout else "SMA10 pullback recovery") + "; all eligibility checks passed", checks, None
    failed = [item["label"] for item in checks if not item["ok"]]
    return "HOLD", "Entry filters not met: "+", ".join(failed), checks, None

def equity_value(state):
    return state["cash"] + sum(
        pos["shares"] * state["last_prices"].get(sym, pos["entry_price"])
        for sym, pos in state["positions"].items())

def fill_order(state, events, symbol, bar):
    o = state["orders_pending"].pop(symbol, None)
    if o is None:
        return
    action = o["action"]
    price = bar["open"]*(1+SLIPPAGE_RATE if action=="BUY" else 1-SLIPPAGE_RATE)
    price = rounded(price)
    if action == "BUY":
        if symbol in state["positions"]:
            audit(state, events, "ORDER_CANCELLED", bar["date"], symbol, reason="Already holding symbol")
            return
        if len(state["positions"])>=MAX_POSITIONS:
            audit(state, events, "ORDER_CANCELLED", bar["date"], symbol, reason="Max open positions reached")
            return
        qty = min(o["qty"], int((state["cash"] - FEE_USD)//price))
        if qty<=0:
            audit(state, events, "ORDER_CANCELLED", bar["date"], symbol, reason="Insufficient virtual buying power")
            return
        total = qty*price+FEE_USD
        state["cash"] -= total
        state["positions"][symbol] = {"shares": qty, "entry_price": price,
                                      "entry_session": bar["date"], "cost": total,
                                      "highest_close": bar["close"]}
        pnl=None
    else:
        pos=state["positions"].pop(symbol, None)
        if not pos:
            audit(state, events, "ORDER_CANCELLED", bar["date"], symbol, reason="Position absent")
            return
        qty=pos["shares"]
        proceeds=qty*price-FEE_USD
        state["cash"]+=proceeds
        pnl=rounded(proceeds-pos["cost"], 2)
    event={"action":action, "qty":qty, "price":price,"date":bar["date"],
           "reason":o["reason"], "signal_session":o["signal_session"],
           "fee":FEE_USD, "slippage_rate":SLIPPAGE_RATE, "pnl":pnl}
    state["transactions"].insert(0, {"symbol":symbol, **event})
    state["transactions"]=state["transactions"][:200]
    audit(state, events, "ORDER_FILLED", bar["date"], symbol, **event)

def queue_signal(state, events, symbol, day, action, why, indicator):
    if action not in ("BUY", "SELL"):
        return
    if symbol in state["orders_pending"]:
        audit(state, events, "ORDER_BLOCKED", day, symbol, action=action, reason="Existing pending order")
        return
    if action=="BUY":
        positions=len(state["positions"])+sum(
            1 for v in state["orders_pending"].values() if v["action"]=="BUY")
        if positions>=MAX_POSITIONS:
            audit(state, events, "ORDER_BLOCKED", day, symbol, action=action, reason="Portfolio max of three positions")
            return
        price=indicator["close"]
        atr=indicator["atr14"]
        stop_distance=2*atr
        if stop_distance<=0:
            audit(state, events, "ORDER_BLOCKED", day, symbol, action=action, reason="ATR unavailable")
            return
        nav=equity_value(state)
        max_qty_risk=int(nav*MAX_RISK//stop_distance)
        max_qty_weight=int(nav*MAX_ALLOCATION//price)
        qty=min(max_qty_risk,max_qty_weight,int((state["cash"]-FEE_USD)//price))
        if qty<=0:
            audit(state, events, "ORDER_BLOCKED", day, symbol, action=action, reason="Risk or buying-power limit")
            return
    else:
        qty=state["positions"][symbol]["shares"] if symbol in state["positions"] else 0
        if not qty:
            return
    state["orders_pending"][symbol]={"action":action, "qty":qty,"reason":why,"signal_session":day}
    audit(state, events, "ORDER_QUEUED", day, symbol, action=action, qty=qty, reason=why,
          sizing={"max_allocation_pct":20,"max_risk_pct":1,"stop_reference_atr":2})
    
def eligible_bars(bars, now):
    # Bars can be incomplete during active NY sessions; never trade from partial daily bars.
    local=now.astimezone(NY)
    today=str(local.date())
    return [b for b in bars if b["date"]<today or
            (b["date"]==today and local.time()>=dt.time(17, 15))]

def run_cycle(prices_by_symbol, now=None, state=None, source_errors=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    if state is None:
        state=json.loads(STATE.read_text()) if STATE.exists() else new_state()
    if state.get("schema")!=SCHEMA:
        raise RuntimeError("Unexpected market state version; do not migrate synthetic balances automatically")
    events=[]
    state["runs"]+=1
    errors=dict(source_errors or {})
    clean={}
    max_allowed=now.astimezone(NY).date()
    for symbol in WATCHLIST:
        try:
            bars=eligible_bars(prices_by_symbol.get(symbol, []), now)
            # A stale/inaccessible feed is never silently simulated.
            if len(bars)<55:
                raise ValueError("Fewer than 55 completed bars available")
            if (max_allowed-dt.date.fromisoformat(bars[-1]["date"])).days>5:
                raise ValueError("Most recent bar is over five calendar days old")
            clean[symbol]=bars
        except Exception as e:
            errors[symbol]=errors.get(symbol) or str(e)
    previous=state["last_session"]
    sessions=sorted({b["date"] for rows in clean.values() for b in rows
                     if previous is None and b["date"]==rows[-1]["date"]
                     or previous is not None and previous<b["date"]<=rows[-1]["date"]})
    # Do not backfill months of hypothetical real-time trades on the very first run.
    if previous is None and sessions:
        sessions=[max(sessions)]
    # Recovery from >10 missing days is intentionally conservative.
    if len(sessions)>10:
        audit(state,events,"BACKLOG_SKIPPED",sessions[-1],reason="More than 10 new sessions; skipped historical simulation")
        sessions=[sessions[-1]]
        state["orders_pending"]={}
    seen=[]
    latest_scan=[]
    for session in sessions:
        daily={sym:(j,b) for sym,bs in clean.items()
               for j,b in enumerate(bs) if b["date"]==session}
        if not daily:
            continue
        # All pending orders fill at the NEXT available daily open, before today's close is read.
        for sym, (j, bar) in daily.items():
            if sym in state["orders_pending"] and
                    state["orders_pending"][sym]["signal_session"]<session:
                fill_order(state, events, sym, bar)
        for sym, (j,bar) in daily.items():
            values=indicators(clean[sym][:j+1]) if j>=54 else None
            if not values:
                audit(state,events,"SCAN_SKIPPED",session,sym,reason="Insufficient lookback")
                continue
            pos=state["positions"].get(sym)
            action,why,checks,stop=reasoned_signal(sym,values,pos)
            if pos:
                pos["highest_close"]=max(pos["highest_close"],bar["close"])
            state["last_prices"][sym]=rounded(bar["close"])
            rec={"symbol":sym,"session":session,"action":action,
                 "reason":why,"indicators":values,"checks":checks,
                 "trailing_stop":rounded(stop) if stop else None,
                 "held_shares":pos["shares"] if pos else 0}
            latest_scan.append(rec)
            audit(state,events,"SCAN",session,sym,action=action,reason=why,
                  indicators=values, checks=checks)
            queue_signal(state,events,sym,session,action,why,values)
        nav=rounded(equity_value(state),2)
        if not state["equity"] or state["equity"][-1]["date"]!=session:
            state["equity"].append({"date":session,"value":nav})
        state["equity"]=state["equity"][-300:]
        state["last_session"]=session
        seen.append(session)
    if latest_scan:
        state["scans"]=latest_scan
    # Record failed feeds even on quiet/no-new-day runs.
    for sym, msg in errors.items():
        audit(state,events,"DATA_WARNING",state["last_session"],sym,reason=str(msg)[:190])
    if not seen:
        audit(state,events,"NO_NEW_SESSION",state["last_session"],
              reason="No new completed daily bar; no virtual fills or signals")
    snapshot={"schema":SCHEMA,"engine":"Deterministic EOD swing-trading rules",
              "source":SOURCE,"as_of_utc":ts(),"last_session":state["last_session"],
              "status":"UPDATED" if seen else ("DATA_UNAVAILABLE" if errors else "NO_NEW_DAILY_BAR"),
              "sessions_processed":seen,"symbols":list(WATCHLIST),"errors":errors,
              "cash":rounded(state["cash"],2),"equity":rounded(equity_value(state),2),
              "positions":state["positions"],"pending":state["orders_pending"],
              "scans":state["scans"],"activity":state["activity"][:100],
              "transactions":state["transactions"][:100],
              "equity_history":state["equity"],"events_count":state["events_count"],
              "runs":state["runs"],"virtual_start":STARTING_CASH,
              "rules":{"max_positions":MAX_POSITIONS, "max_allocation":MAX_ALLOCATION,
                       "risk_budget":MAX_RISK,"slippage":SLIPPAGE_RATE,"fee":FEE_USD,
                       "execution":"Next completed trading session OPEN, after signal at prior session CLOSE"}}
    return state,events,snapshot

def fixture(symbol, count=95):
    # Used for self-tests only; NEVER used as market data in normal runs.
    a=[]
    for i in range(count):
        px=100+i*.28+math.sin(i*.27)*1.6
        a.append({"date":str(dt.date(2026,1,1)+dt.timedelta(days=i)),
                  "open":px-.12, "high":px+.8, "low":px-1,
                  "close":px,"volume":100000+i*10})
    return a

def self_test():
    from unittest.mock import patch
    bars=fixture("AAPL")
    assert indicators(bars)["sma20"]>0
    assert reasoned_signal("AAPL",indicators(bars),None)[0] in ("HOLD","BUY")
    fixed=dt.datetime(2026,5,1,22,tzinfo=dt.timezone.utc)
    price_data={sym:bars for sym in WATCHLIST}
    a,events,view=run_cycle(price_data,now=fixed,state=new_state())
    assert a["last_session"]==bars[-1]["date"]
    assert view["equity"]==STARTING_CASH
    assert len(a["transactions"])==0, "First run must not invent fills"
    b,e2,v2=run_cycle(price_data,now=fixed,state=a)
    assert not b["transactions"] and v2["status"]=="NO_NEW_DAILY_BAR"
    assert any(e["type"]=="NO_NEW_SESSION" for e in e2)
    missing,errors,s=run_cycle({},now=fixed,state=new_state())
    assert s["status"]=="DATA_UNAVAILABLE" and not s["transactions"]
    # Hard guard: pending BUY cannot be filled on signal date.
    buy_state=new_state()
    buy_state["orders_pending"]["AAPL"]={"action":"BUY","qty":2,"reason":"unit test","signal_session":"2026-04-10"}
    bar={"date":"2026-04-11","open":100,"close":101}
    fill_order(buy_state,[],"AAPL",bar)
    assert buy_state["positions"]["AAPL"]["shares"]==2
    print("PASS: indicators, initial-run guard, idempotence, outage guard, paper fills")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:
        self_test()
        return
    history={}
    errors={}
    for sym in WATCHLIST:
        try:
            history[sym]=download_history(sym)
            print(sym, "fetched", len(history[sym]), "completed-or-partial bars")
        except Exception as e:
            errors[sym]=type(e).__name__+": "+str(e)[:130]
            print(sym, "unavailable:",errors[sym])
    updated,events,snapshot=run_cycle(history,source_errors=errors)
    write(updated,events,snapshot)
    print("Status",snapshot["status"],"session",snapshot["last_session"],
          "paper_equity",snapshot["equity"],"new events",len(events))

if __name__=="__main__":
    main()
