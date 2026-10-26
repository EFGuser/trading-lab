#!/usr/bin/env python3
"""Scheduled, synthetic-price, PAPER-ONLY strategy research. Python standard library."""
import argparse
import copy
import datetime as dt
import json
import math
import random
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "state.json"
SNAPSHOT = ROOT / "site" / "data" / "snapshot.json"
START = 10000.0
FEE = 1.0
SLIP = 0.001
BOTS = ("Adaptive Researcher", "Moving Average", "RSI Reversion", "Breakout")
BASELINES = {
    "Moving Average": {"kind": "ma", "fast": 10, "slow": 30},
    "RSI Reversion": {"kind": "rsi", "entry": 30, "exit": 55},
    "Breakout": {"kind": "breakout", "entry": 20, "exit": 10},
}
REGIMES = ("trending", "sideways", "volatile", "bearish", "mixed")

def policy(kind):
    return {
        "ma": {"kind": "ma", "fast": 10, "slow": 30},
        "trend": {"kind": "trend", "fast": 20, "slow": 50},
        "rsi": {"kind": "rsi", "entry": 30, "exit": 55},
        "breakout": {"kind": "breakout", "entry": 20, "exit": 10},
    }[kind].copy()

def candle(prev, rng, regime, day):
    vol = {"trending": .012, "sideways": .012, "volatile": .03,
           "bearish": .02, "mixed": .018}[regime]
    drift = {
        "trending": .0016 if day % 80 < 55 else -.001,
        "sideways": (100 - prev) * .00015,
        "volatile": .0015 if day % 50 < 23 else -.002,
        "bearish": -.0014,
        "mixed": .0009 if day % 80 < 43 else -.0009,
    }[regime]
    o = prev * (1 + rng.gauss(0, vol * .20))
    c = max(2, o * (1 + drift + rng.gauss(0, vol)))
    h = max(o, c) * (1 + rng.random() * vol * .5)
    l = min(o, c) * (1 - rng.random() * vol * .5)
    return {"o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4)}

def path(seed, regime, n):
    rng = random.Random(seed)
    prices, last = [], 100.0
    for i in range(n):
        p = candle(last, rng, regime, i)
        prices.append(p)
        last = p["c"]
    return prices

def mean(prices, n):
    return sum(p["c"] for p in prices[-n:]) / n

def rsi(prices, n=14):
    changes = [prices[i]["c"] - prices[i - 1]["c"] for i in range(len(prices)-n, len(prices))]
    gain = sum(max(0, d) for d in changes)
    loss = sum(max(0, -d) for d in changes)
    return 100 if not loss and gain else 50 if not loss else 100 - 100 / (1 + gain / loss)

def decide(pol, prices, held):
    if len(prices) < 86:
        return "HOLD", "Collecting historical prices"
    k = pol["kind"]
    if k in ("ma", "trend"):
        fast, slow = pol["fast"], pol["slow"]
        a, b = mean(prices, fast), mean(prices, slow)
        if k == "ma":
            olda, oldb = mean(prices[:-1], fast), mean(prices[:-1], slow)
            buy = olda <= oldb and a > b
            sell = olda >= oldb and a < b
        else:
            buy = a > b and prices[-1]["c"] > a
            sell = a <= b or prices[-1]["c"] < a
        why = "Fast SMA {:.2f}; slow SMA {:.2f}".format(a, b)
    elif k == "rsi":
        value = rsi(prices)
        buy, sell = value < pol["entry"], value > pol["exit"]
        why = "RSI {:.1f}; buy under {}; exit over {}".format(value, pol["entry"], pol["exit"])
    else:
        entry, exit_ = pol["entry"], pol["exit"]
        high = max(p["h"] for p in prices[-entry-1:-1])
        low = min(p["l"] for p in prices[-exit_-1:-1])
        last = prices[-1]["c"]
        buy, sell = last > high, last < low
        why = "Close {:.2f}; breakout high {:.2f}; exit low {:.2f}".format(last, high, low)
    return ("SELL" if sell else "HOLD", why) if held else ("BUY" if buy else "HOLD", why)

def account():
    return {"cash": START, "shares": 0, "basis": 0, "wins": 0, "round_trips": 0,
            "orders": 0, "fees": 0, "pending": None, "peak": START, "max_dd": 0,
            "equity": [START], "journal": [], "last_decision": {"action": "HOLD", "reason": "Warming up"}}

def step_account(acc, pol, prices, day):
    current = prices[-1]
    order = acc["pending"]
    acc["pending"] = None
    if order:
        action, reason = order["action"], order["reason"]
        px = current["o"] * (1 + SLIP if action == "BUY" else 1 - SLIP)
        if action == "BUY" and acc["shares"] == 0:
            qty = max(0, int(min(acc["cash"] - FEE, (acc["cash"] + acc["shares"] * px) * .25) // px))
            if qty:
                acc["cash"] -= qty * px + FEE
                acc["basis"] = qty * px + FEE
                acc["shares"] = qty
                acc["orders"] += 1
                acc["fees"] += FEE
                acc["journal"].insert(0, {"day": day, "action": action, "qty": qty, "price": round(px, 2), "reason": reason})
        elif action == "SELL" and acc["shares"]:
            qty = acc["shares"]
            proceeds = qty * px - FEE
            acc["cash"] += proceeds
            acc["shares"] = 0
            acc["orders"] += 1
            acc["round_trips"] += 1
            acc["wins"] += int(proceeds > acc["basis"])
            acc["fees"] += FEE
            acc["journal"].insert(0, {"day": day, "action": action, "qty": qty, "price": round(px, 2), "reason": reason})
        acc["journal"] = acc["journal"][:50]
    action, reason = decide(pol, prices, acc["shares"] > 0)
    acc["last_decision"] = {"action": action, "reason": reason}
    if action != "HOLD":
        acc["pending"] = acc["last_decision"].copy()
    equity = acc["cash"] + acc["shares"] * current["c"]
    acc["equity"].append(round(equity, 2))
    acc["equity"] = acc["equity"][-500:]
    acc["peak"] = max(acc["peak"], equity)
    acc["max_dd"] = max(acc["max_dd"], (acc["peak"] - equity) / acc["peak"])

def test(pol, seed, regime):
    prices = path(seed, regime, 86)
    acc = account()
    rng = random.Random(seed + 12345)
    for i in range(175):
        prices.append(candle(prices[-1]["c"], rng, regime, i))
        step_account(acc, pol, prices, i + 1)
    return {"ret": acc["equity"][-1] / START - 1,
            "dd": acc["max_dd"], "orders": acc["orders"]}

def score(pol, regimes, seed):
    results = [test(pol, seed + i * 1009, regime) for i, regime in enumerate(regimes)]
    gains = [t["ret"] for t in results]
    dd = sum(t["dd"] for t in results) / len(results)
    return sum(gains) / len(gains) - .7 * dd - .25 * statistics.pstdev(gains)

def mutate(pol, rng):
    x = pol.copy()
    if rng.random() < .12:
        return policy(rng.choice(["ma", "trend", "rsi", "breakout"]))
    if pol["kind"] in ("ma", "trend"):
        key = rng.choice(["fast", "slow"])
        x[key] += rng.choice([-5, -3, 3, 5])
        x["fast"] = min(30, max(5, x["fast"]))
        x["slow"] = min(85, max(25, x["fast"] + 8, x["slow"]))
    elif pol["kind"] == "rsi":
        key = rng.choice(["entry", "exit"])
        x[key] += rng.choice([-4, -2, 2, 4])
        x["entry"] = min(42, max(15, x["entry"]))
        x["exit"] = min(80, max(48, x["entry"] + 11, x["exit"]))
    else:
        key = rng.choice(["entry", "exit"])
        x[key] += rng.choice([-4, -2, 2, 4])
        x["entry"] = min(45, max(8, x["entry"]))
        x["exit"] = min(25, max(4, x["exit"]))
    return x

def research(s):
    epoch = s["epoch"] + 1
    rng = random.Random(epoch * 71093)
    champion = s["champion"]
    candidates = [champion] + [mutate(champion, rng) for _ in range(8)]
    candidates += [policy(k) for k in ("ma", "trend", "rsi", "breakout")]
    unique = {json.dumps(p, sort_keys=True): p for p in candidates}
    training = [("trending", "sideways", "volatile"), 230000 + epoch * 111]
    ranked = sorted(unique.values(),
                    key=lambda p: score(p, *training), reverse=True)
    challenger = ranked[0]
    validation = [("bearish", "mixed", "volatile", "trending"), 860000 + epoch * 911]
    oldscore = score(champion, *validation)
    newscore = score(challenger, *validation)
    accepted = challenger != champion and newscore > oldscore + .002
    if accepted:
        s["champion"] = challenger
    s["epoch"] = epoch
    s["experiments"].insert(0, {
        "epoch": epoch, "accepted": accepted, "tested": len(unique),
        "proposed": challenger, "old_score": round(oldscore, 5),
        "test_score": round(newscore, 5),
        "note": "Accepted after out-of-sample synthetic validation" if accepted
        else "Not adopted: validation improvement insufficient"
    })
    s["experiments"] = s["experiments"][:50]

def fresh():
    return {"day": 0, "epoch": 0, "champion": policy("trend"),
            "agents": {name: account() for name in BOTS},
            "candles": path(44881, "mixed", 86), "experiments": []}

def run(days):
    s = json.loads(STATE.read_text()) if STATE.exists() else fresh()
    for _ in range(days):
        day = s["day"] + 1
        regime = REGIMES[((day - 1) // 60) % len(REGIMES)]
        rng = random.Random(97531 + day * 53)
        new = candle(s["candles"][-1]["c"], rng, regime, day)
        s["candles"].append(new)
        policies = {"Adaptive Researcher": s["champion"], **BASELINES}
        for name in BOTS:
            step_account(s["agents"][name], policies[name], s["candles"], day)
        s["day"] = day
        if day % 5 == 0:
            research(s)
        s["candles"] = s["candles"][-220:]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    public = copy.deepcopy(s)
    public.update({"last_run_utc": now, "market": "SYNTHETIC", "model_connected": False,
                   "model": "Rules-based optimizer, not GPT", "scenario": REGIMES[(s["day"] // 60) % len(REGIMES)]})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, separators=(",", ":")) + "\n")
    SNAPSHOT.write_text(json.dumps(public, separators=(",", ":")) + "\n")
    print("Synthetic day", s["day"], "research rounds", s["epoch"], "saved at", now)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.days <= 10:
        parser.error("--days must be 1 through 10")
    run(args.days)
