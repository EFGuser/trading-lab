#!/usr/bin/env python3
"""Data-driven PAPER-ONLY strategy arena.
Historical replay/backtesting is separated from forward paper trading.
Free completed DAILY stock bars, not streaming or licensed real-time quotes.
No broker connection, no actual buy/sell, no paid GPT API.
"""
import argparse
import datetime as dt
import json
import math
import statistics
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent
STATE=ROOT/"data"/"learning_state.json"
AUDIT=ROOT/"data"/"learning_audit.jsonl"
PUBLIC=ROOT/"site"/"data"/"learning_snapshot.json"
NY=ZoneInfo("America/New_York")
UTC=dt.timezone.utc
SYMS=("SPY","QQQ","AAPL","NVDA","MSFT","AMD","AMZN","GOOGL","TE")
START=10000.0
FEE=1.0
SLIP=.001
BASE={"style":"buy_hold","n":20}
POLICIES=(
    {"style":"buy_hold","n":20},
    {"style":"rotation","n":10},{"style":"rotation","n":20},{"style":"rotation","n":35},
    {"style":"trend","n":10,"slow":40},{"style":"trend","n":20,"slow":50},
    {"style":"trend","n":20,"slow":65},
    {"style":"momentum","n":10},{"style":"momentum","n":20},
    {"style":"reversion","n":33},{"style":"reversion","n":43},
    {"style":"breakout","n":10},{"style":"breakout","n":20}
)

def stamp():
    return dt.datetime.now(UTC).isoformat(timespec="seconds")
def usd(v):return round(float(v),2)
def fresh():
    return {"version":1,"forward":{"cash":START,"positions":{},"pending":{},
            "journal":[],"equity":[],"last_session":None},"study_events":[],
            "study_dates":[],"decision_log":[],"selected":None,"last_research":None,
            "total_events":0}
def ohlc(symbol):
    import yfinance as yf
    history=yf.Ticker(symbol).history(period="1y",interval="1d",auto_adjust=True,
                                     actions=False,timeout=20,raise_errors=True)
    now=dt.datetime.now(UTC).astimezone(NY)
    cutoff=str(now.date())
    rows={}
    for index,row in history.iterrows():
        day=str(index.date())
        if day>cutoff or (day==cutoff and now.time()<dt.time(17,15)):
            continue
        try:
            b={key:float(row[src]) for key,src in
               (("o","Open"),("h","High"),("l","Low"),("c","Close"),("v","Volume"))}
            if all(math.isfinite(x) for x in b.values()) and min(b["o"],b["h"],b["l"],b["c"])>0 and b["v"]>=0:
                rows[day]=b
        except (KeyError,TypeError,ValueError):
            continue
    return rows

def indicator(bars,calendar,idx):
    # At idx we only use history through this index, never future bars.
    hist=[bars[d] for d in calendar[:idx+1] if d in bars]
    if len(hist)<68:
        return None
    closes=[x["c"] for x in hist]
    delta=[closes[i]-closes[i-1] for i in range(len(closes)-14,len(closes))]
    up=sum(max(x,0) for x in delta)
    down=sum(max(-x,0) for x in delta)
    rsi=100 if down==0 and up>0 else 50 if down==0 else 100-100/(1+up/down)
    m=lambda n:sum(closes[-n:])/n
    return {"last":closes[-1], "sma10":m(10),"sma20":m(20),
            "sma40":m(40),"sma50":m(50),"sma65":m(65),
            "rsi":rsi,"chg10":closes[-1]/closes[-11]-1,
            "chg20":closes[-1]/closes[-21]-1,
            "chg35":closes[-1]/closes[-36]-1,
            "hi10":max(p["h"] for p in hist[-11:-1]),
            "hi20":max(p["h"] for p in hist[-21:-1]),
            "volume_ratio":hist[-1]["v"]/(sum(p["v"] for p in hist[-21:-1])/20) if sum(p["v"] for p in hist[-21:-1]) else 0,
            "closes":closes}

def selection(policy,data,calendar,index):
    if policy["style"]=="buy_hold":
        return ["SPY"] if calendar[index] in data.get("SPY",{}) else [], {"SPY":"Passive SPY benchmark"}
    ranking=[]
    details={}
    for sym,series in data.items():
        if sym=="SPY" or calendar[index] not in series:
            continue
        f=indicator(series,calendar,index)
        if not f:continue
        name=policy["style"]
        if name=="rotation":
            score=f["last"]/f["closes"][-policy["n"]-1]-1
            allowed=True
            explanation=f"{policy['n']}-session relative strength {score:+.1%}; rotate into top two"
        elif name=="momentum":
            score=f["last"]/f["closes"][-policy["n"]-1]-1
            allowed=score>0 and f["last"]>f["sma50"]
            explanation=f"{policy['n']}-day return {score:+.1%}; above SMA50"
        elif name=="trend":
            fast=sum(f["closes"][-policy["n"]:])/policy["n"]
            slow=sum(f["closes"][-policy["slow"]:])/policy["slow"]
            score=fast/slow-1
            allowed=fast>slow and f["last"]>slow
            explanation=f"SMA{policy['n']} {fast:.2f} vs SMA{policy['slow']} {slow:.2f}"
        elif name=="reversion":
            score=100-f["rsi"]
            allowed=f["rsi"]<policy["n"] and f["last"]>f["sma50"]*.85
            explanation=f"RSI14 {f['rsi']:.1f} < {policy['n']}"
        else:
            high=f["hi10"] if policy["n"]==10 else f["hi20"]
            score=f["last"]/high-1
            allowed=f["last"]>high and f["volume_ratio"]>.65
            explanation=f"Close over previous {policy['n']}-session high, volume >=0.65x"
        details[sym]=explanation+(" ✓" if allowed else " ✕")
        if allowed:ranking.append((score,sym))
    ranking.sort(reverse=True)
    return [sym for _,sym in ranking[:2]],details

def account():return {"cash":START,"positions":{},"journal":[],"equity":[]}
def worth(acc,last):
    return acc["cash"]+sum(p["qty"]*last.get(sym,p["entry"])
        for sym,p in acc["positions"].items())

def trade(acc,day,sym,side,bar,reason,journal=True):
    px=bar["o"]*(1+SLIP if side=="BUY" else 1-SLIP)
    if side=="SELL":
        p=acc["positions"].pop(sym,None)
        if not p:return None
        qty=p["qty"];received=qty*px-FEE
        acc["cash"]+=received
        pnl=received-p["basis"]
    else:
        if sym in acc["positions"]:return None
        nav=acc["cash"]+sum(p["basis"] for p in acc["positions"].values())
        qty=min(int((nav*.24)//px),int((acc["cash"]-FEE)//px))
        if qty<1:return None
        basis=qty*px+FEE
        acc["cash"]-=basis
        acc["positions"][sym]={"qty":qty,"entry":px,"basis":basis}
        pnl=None
    entry={"date":day,"symbol":sym,"side":side,"quantity":qty,
           "fill":round(px,4),"pnl":usd(pnl) if pnl is not None else None,
           "reason":reason,"fees":FEE,"slippage_pct":0.1}
    if journal:acc["journal"].append(entry)
    return entry

def simulate(policy,data,calendar,start,end,record=False):
    """Rebalance on historical day i using ONLY observations at day i-1.
    Execute at i open, mark portfolio at i close, include fees and slippage.
    """
    acc=account();peak=START;drawdown=0;prev=[]
    for i in range(start,end):
        today=calendar[i]
        last={}
        for sym,series in data.items():
            if today in series:last[sym]=series[today]["c"]
        if i==start or (i-start)%5==0:
            desired,checks=selection(policy,data,calendar,i-1)
            # Never use today's close to select the same day's open.
            reason=f"{policy['style']} selection after {calendar[i-1]} close"
            for sym in list(acc["positions"]):
                if sym not in desired and sym in data and today in data[sym]:
                    trade(acc,today,sym,"SELL",data[sym][today],reason)
            for sym in desired:
                if sym not in acc["positions"] and sym in data and today in data[sym]:
                    trade(acc,today,sym,"BUY",data[sym][today],reason)
        nav=worth(acc,last)
        peak=max(peak,nav)
        drawdown=max(drawdown,1-nav/peak)
        if record:acc["equity"].append({"date":today,"value":usd(nav)})
    nav=acc["equity"][-1]["value"] if record and acc["equity"] else worth(acc,{
        sym:series.get(calendar[end-1],{}).get("c",100)
        for sym,series in data.items()})
    numorders=len(acc["journal"])
    score=(nav/START-1)-0.55*drawdown-0.00075*numorders
    return {"score":round(score,5),"return_pct":round(100*(nav/START-1),2),
            "drawdown_pct":round(100*drawdown,2),"orders":numorders,
            "trades":acc["journal"][-160:] if record else [],
            "equity":acc["equity"][-240:] if record else []}

def study(data,calendar):
    if len(calendar)<175:
        raise ValueError("Need at least 175 completed daily trading sessions")
    hold=max(155,len(calendar)-35)
    val=max(120,hold-40)
    if val<110 or hold-val<25 or len(calendar)-hold<20:
        raise ValueError("Not enough chronological observations")
    ranked=[]
    for p in POLICIES:
        try:
            train=simulate(p,data,calendar,80,val)
            ranked.append((train["score"],p,train))
        except Exception:
            continue
    ranked.sort(key=lambda x:x[0],reverse=True)
    # Validate promising candidates + fixed benchmarks; holdout NOT used to select winner.
    shortlist=ranked[:5]
    for item in ranked:
        if item[1]["style"] in ("buy_hold","rotation"):
            if item not in shortlist:shortlist.append(item)
    validation=[]
    for _,p,tr in shortlist:
        result=simulate(p,data,calendar,val,hold)
        validation.append({"policy":p,"training":tr,
                           "validation":result})
    validation.sort(key=lambda x:x["validation"]["score"],reverse=True)
    champion=validation[0]["policy"]
    holdout=simulate(champion,data,calendar,hold,len(calendar),record=True)
    spy=simulate(BASE,data,calendar,hold,len(calendar),record=True)
    return {"champion":champion,"candidates":validation,
            "trained_count":len(ranked),"training_until":calendar[val-1],
            "validation_until":calendar[hold-1],
            "holdout_start":calendar[hold],"holdout_end":calendar[-1],
            "holdout":holdout,"benchmark":spy}

def emit(s,ev,event,session,**extras):
    rec={"observed_utc":stamp(),"type":event,"session":session,**extras}
    ev.append(rec)
    s["study_events"].insert(0,rec)
    s["study_events"]=s["study_events"][:150]
    s["total_events"]+=1

def forward_tick(s,ev,data,calendar,policy):
    """Forward paper account starts only today, never backdated.
       Queued orders fill at the first *future observed* session open.
    """
    a=s["forward"];latest=calendar[-1];last=a["last_session"]
    if last==latest:return "NO_NEW_SESSION"
    prev_index=calendar.index(last) if last in calendar else None
    if prev_index is not None and len(calendar)-prev_index>4:
        for sym in list(a["pending"]):
            emit(s,ev,"PAPER_CANCEL",latest,symbol=sym,reason="Historical session gap, do not invent fills")
        a["pending"]={}
        emit(s,ev,"PAPER_GAP",latest,reason="Reconnected after more than 3 trading sessions; old orders cancelled")
    elif prev_index is not None:
        for sym,order in list(a["pending"].items()):
            a["pending"].pop(sym,None)
            bar=data.get(sym,{}).get(latest)
            if not bar:
                emit(s,ev,"PAPER_CANCEL",latest,symbol=sym,reason="No eligible price bar")
                continue
            filled=trade(a,latest,sym,order["side"],bar,order["reason"])
            if filled:
                emit(s,ev,"PAPER_FILL",latest,**filled)
    # Record day-end account mark and choose new orders based on *latest completed close*.
    if prev_index is None or ((len(calendar)-1)%5)==0:
        targets,details=selection(policy,data,calendar,len(calendar)-1)
        for sym,explanation in details.items():
            emit(s,ev,"SCAN",latest,symbol=sym,decision="TARGET" if sym in targets else "SKIP",
                 evidence=explanation)
        old=a["positions"]
        future={}
        for sym in old:
            if sym not in targets:
                future[sym]={"side":"SELL","reason":"Next-session paper rebalance; no longer in selected candidates","signalled":latest}
        count=len(old)-len(future)
        for sym in targets:
            if sym not in old and count<2:
                future[sym]={"side":"BUY","reason":details.get(sym,"Rule-based target selection"),"signalled":latest}
                count+=1
        a["pending"]=future
        for sym,order in future.items():
            emit(s,ev,"PAPER_QUEUE",latest,symbol=sym,side=order["side"],
                 why=order["reason"],execution="NEXT_NEW_SESSION_OPEN")
    marks={sym:data[sym][latest]["c"] for sym in a["positions"] if latest in data.get(sym,{})}
    a["equity"].append({"date":latest,"value":usd(worth(a,marks))})
    a["equity"]=a["equity"][-220:]
    a["journal"]=a["journal"][-200:]
    a["last_session"]=latest
    return "NEW_SESSION"

def cycle(data,s=None):
    if s is None:s=fresh()
    if s.get("version")!=1:raise ValueError("Invalid saved research state")
    ev=[]
    # Require SPY's completed session calendar; never substitute made-up prices.
    calendar=sorted(data.get("SPY",{}))
    if len(calendar)<175:raise ValueError("Not enough actual SPY trading sessions")
    if (dt.date.today()-dt.date.fromisoformat(calendar[-1])).days>8:
        raise ValueError("Market prices older than 8 days")
    result=study(data,calendar)
    previous=s.get("selected")
    # Only change champion on a NEW trading session; do not change retroactively
    # on repeated scheduled runs with the exact same close.
    is_new=s.get("last_research")!=calendar[-1]
    if is_new:
        s["selected"]=result["champion"]
        s["last_research"]=calendar[-1]
        emit(s,ev,"RESEARCH",calendar[-1],selected=s["selected"],
             training_end=result["training_until"],validation_end=result["validation_until"],
             holdout_start=result["holdout_start"],holdout_return=result["holdout"]["return_pct"],
             benchmark_return=result["benchmark"]["return_pct"],candidates=result["trained_count"],
             note="Policy chosen using training/validation only. Holdout is a historical backtest.")
        if previous!=s["selected"]:
            emit(s,ev,"POLICY_UPDATE",calendar[-1],old=previous,new=s["selected"],
                 explanation="Selected via separate chronological validation; not GPT weight training")
    choice=s["selected"] or result["champion"]
    mode=forward_tick(s,ev,data,calendar,choice) if is_new else "NO_NEW_SESSION"
    a=s["forward"]
    view={"updated_utc":stamp(),"latest_session":calendar[-1],"mode":mode,
          "source":"Yahoo Finance historical daily OHLCV through yfinance — NOT real-time",
          "engine":"Rule-based walk-forward strategy search, no GPT API",
          "policy":choice,"research":result,"forward":{"cash":usd(a["cash"]),
           "portfolio":usd(worth(a,{sym:data[sym][calendar[-1]]["c"] for sym in a["positions"]
                if calendar[-1] in data.get(sym,{})})),
           "positions":a["positions"],"pending":a["pending"],
           "journal":a["journal"][-100:][::-1],
           "equity":a["equity"],"last_session":a["last_session"]},
          "recent_events":s["study_events"][:100],"total_events":s["total_events"],
          "watchlist":sorted(data)}
    return s,ev,view

def save(s,events,view):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    PUBLIC.parent.mkdir(parents=True,exist_ok=True)
    STATE.write_text(json.dumps(s,separators=(",",":"),allow_nan=False)+"\n")
    PUBLIC.write_text(json.dumps(view,separators=(",",":"),allow_nan=False)+"\n")
    if events:
        with AUDIT.open("a",encoding="utf8") as f:
            for e in events:f.write(json.dumps(e,separators=(",",":"),allow_nan=False)+"\n")

def fixture():
    # TESTS ONLY. Never persisted or shown as sourced market history.
    dates=[];date=dt.date(2025,8,1)
    while len(dates)<240:
        if date.weekday()<5:dates.append(str(date))
        date+=dt.timedelta(days=1)
    result={}
    for index,sym in enumerate(SYMS):
        m={}
        for i,d in enumerate(dates):
            # Every asset follows a different historical-style path.
            p=75+index*17+(i*(.16+(index%3)*.12)) + (6+index)*math.sin(i/11+index*.4)
            m[d]={"o":p-.2,"h":p+1.2,"l":p-1.3,"c":p,"v":1000000+10000*i}
        result[sym]=m
    return result,dates

def self_test():
    data,days=fixture()
    result=study(data,days)
    assert result["training_until"]<result["validation_until"]<result["holdout_start"]<=result["holdout_end"]
    a=simulate({"style":"buy_hold","n":20},data,days,180,220,True)
    assert any(t["side"]=="BUY" for t in a["trades"])
    assert a["trades"][0]["date"]==days[180]
    # Forward tests: no historical fills at first initialization.
    a=fresh()
    # Avoid fixture age test here; fake time isn't a live feed.
    ev=[]
    assert forward_tick(a,ev,data,days,{"style":"rotation","n":10})=="NEW_SESSION"
    assert not a["forward"]["journal"],"First scan must not fabricate a forward fill"
    assert a["forward"]["pending"],"Rotation policy must generate rule-based mock order"
    first_ev=a["total_events"]
    assert forward_tick(a,ev,data,days,{"style":"rotation","n":10})=="NO_NEW_SESSION"
    assert a["total_events"]==first_ev
    # Add a subsequent historical bar to exercise next-session open fill.
    new_date=str(dt.date.fromisoformat(days[-1])+dt.timedelta(days=1))
    for series in data.values():
        prev=series[days[-1]]
        series[new_date]={"o":prev["c"],"c":prev["c"]*1.01,
                          "h":prev["c"]*1.03,"l":prev["c"]*.99,"v":1000000}
    assert forward_tick(a,ev,data,days+[new_date],{"style":"rotation","n":10})=="NEW_SESSION"
    assert a["forward"]["journal"],"Second observed session must fill pending virtual trade"
    assert any(x["type"]=="PAPER_FILL" for x in ev)
    print("PASS: chronological splits, next-bar backtest, non-backdated forward orders, idempotence, fills")

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args()
    if args.self_test:
        self_test()
        return
    data={}
    errors={}
    for sym in SYMS:
        try:
            bars=ohlc(sym)
            if len(bars)<175:
                errors[sym]="Fewer than 175 historical daily bars"
            else:
                data[sym]=bars
            print(sym,len(bars),"daily bars")
        except Exception as exc:
            errors[sym]=type(exc).__name__+": "+str(exc)[:120]
            print("Market data warning",sym,errors[sym])
    if "SPY" not in data or len(data)<4:
        raise SystemExit("Not enough verified real market data: no simulated substitute permitted")
    s,events,view=cycle(data)
    view["data_warnings"]=errors
    save(s,events,view)
    print("Paper arena",view["latest_session"],"historical holdout orders",
          len(view["research"]["holdout"]["trades"]),"forward fills",
          len(view["forward"]["journal"]),"queued",list(view["forward"]["pending"]))

if __name__=="__main__":
    main()
