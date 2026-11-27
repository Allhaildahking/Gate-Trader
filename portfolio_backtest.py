"""Shared-account portfolio backtester for Gate-Trader. Never places orders."""
from __future__ import annotations
import argparse, math, time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
import gate_futures_bot as strategy

@dataclass
class SimConfig:
    starting_balance: float = 1000.0
    risk_pct: float = 0.01
    leverage: float = 5.0
    max_margin_pct: float = 0.30
    max_positions: int = 3
    max_drawdown: float = 0.20
    fee_rate: float = 0.0005
    slippage_bps: float = 3.0
    cooldown_bars: int = 4
    stop_atr: float = 1.5
    target_atr: float = 3.0
    max_hold_seconds: int = 12 * 3600

def _trend_at(rows, ends, ts):
    return strategy.trend(rows[:bisect_right(ends, ts)])

def _signal_before_entry(rows, i, h1, h1_ends, btc_h1, btc_ends):
    # i is the next bar: only rows[:i] are known at its open.
    if i < 61:
        return None
    sig = strategy.breakout_signal(rows[:i][-60:])
    if not sig:
        return None
    side, atr = sig
    closed_at = rows[i-1]["t"] + 900
    if strategy.trend_allows(side, _trend_at(h1, h1_ends, closed_at),
                              _trend_at(btc_h1, btc_ends, closed_at)):
        return side, atr
    return None

def _stats(trades, start, end, max_dd):
    wins = [x["pnl"] for x in trades if x["pnl"] > 0]
    losses = [-x["pnl"] for x in trades if x["pnl"] <= 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    return {
        "trades": len(trades), "win_rate_pct": 100*len(wins)/len(trades) if trades else 0.0,
        "net_pnl": end-start, "return_pct": 100*(end/start-1) if start else 0.0,
        "profit_factor": gross_win/gross_loss if gross_loss else (math.inf if gross_win else 0.0),
        "expectancy": sum(x["pnl"] for x in trades)/len(trades) if trades else 0.0,
        "total_r": sum(x["r"] for x in trades), "max_drawdown_pct": max_dd*100,
    }

def simulate_portfolio(data, btc_h1, contract_meta, config=None):
    """data[symbol] = {'m15': ascending OHLCV bars, 'h1': ascending OHLCV bars}."""
    c = config or SimConfig()
    balance = peak = c.starting_balance
    max_dd, positions, cooldown, trades = 0.0, {}, defaultdict(int), []
    btc_ends = [b["t"]+3600 for b in btc_h1]
    prepared, timeline = {}, set()
    for sym, d in data.items():
        m15, h1 = d.get("m15", []), d.get("h1", [])
        if len(m15) < 62 or not h1: continue
        prepared[sym] = {"m":m15, "h":h1, "he":[b["t"]+3600 for b in h1],
                         "ix":{b["t"]:i for i,b in enumerate(m15)}}
        timeline.update(b["t"] for b in m15[61:])
    for ts in sorted(timeline):
        # Existing positions first. If stop and target hit in one OHLC candle,
        # assume stop first: conservative because intrabar path is unknown.
        for sym in list(positions):
            s, i = prepared[sym], prepared[sym]["ix"].get(ts)
            if i is None: continue
            bar, p = s["m"][i], positions[sym]
            d = p["dir"]
            stop_hit = bar["l"] <= p["stop"] if d == 1 else bar["h"] >= p["stop"]
            tp_hit = bar["h"] >= p["tp"] if d == 1 else bar["l"] <= p["tp"]
            reason, px = (("stop",p["stop"]) if stop_hit else
                          ("target",p["tp"]) if tp_hit else
                          ("time",bar["c"]) if ts-p["time"] >= c.max_hold_seconds else (None,None))
            if reason:
                slip=c.slippage_bps/10000
                px *= 1-slip if d == 1 else 1+slip
                gross=(px-p["entry"])*d*p["qty"]*p["mult"]
                fee=abs(px*p["qty"]*p["mult"])*c.fee_rate
                pnl=gross-fee-p["entry_fee"]
                balance += gross-fee
                trades.append({"symbol":sym,"side":p["side"],"pnl":pnl,
                               "r":pnl/p["risk"] if p["risk"] else 0.0,
                               "reason":reason,"fees":p["entry_fee"]+fee})
                del positions[sym]; cooldown[sym]=ts+c.cooldown_bars*900
                continue
            # Trail becomes active only for subsequent bars, never this candle.
            extreme=bar["h"] if d==1 else bar["l"]
            p["best"]=max(p["best"],extreme) if d==1 else min(p["best"],extreme)
            if (p["best"]-p["entry"])*d >= 1.5*p["atr"]:
                candidate=p["best"]-d*c.stop_atr*p["atr"]
                p["stop"]=max(p["stop"],candidate) if d==1 else min(p["stop"],candidate)

        # Mark-to-market equity for drawdown guard; include open PnL and entry fees.
        unrealized=0.0
        for sym,p in positions.items():
            i=prepared[sym]["ix"].get(ts)
            if i is not None:
                close=prepared[sym]["m"][i]["c"]
                unrealized+=(close-p["entry"])*p["dir"]*p["qty"]*p["mult"]
        equity=balance+unrealized
        peak=max(peak,equity)
        dd=(peak-equity)/peak if peak else 0.0
        max_dd=max(max_dd,dd)
        if dd >= c.max_drawdown or len(positions) >= c.max_positions: continue

        candidates=[]
        for sym,s in prepared.items():
            if sym in positions or ts < cooldown[sym]: continue
            i=s["ix"].get(ts)
            if i is None: continue
            sig=_signal_before_entry(s["m"],i,s["h"],s["he"],btc_h1,btc_ends)
            if sig: candidates.append((sym,sig[0],sig[1],s["m"][i]["o"]))
        for sym,side,atr,raw in sorted(candidates):
            if len(positions)>=c.max_positions: break
            meta=contract_meta.get(sym,{})
            try:
                mult=float(meta.get("quanto_multiplier",1))
                min_qty=int(meta.get("order_size_min",1))
                max_qty=int(meta.get("order_size_max",2**31-1))
            except (TypeError,ValueError): continue
            d=1 if side=="long" else -1
            slip=c.slippage_bps/10000
            entry=raw*(1+slip if d==1 else 1-slip)
            stop_dist=c.stop_atr*atr
            if entry<=0 or stop_dist<=0 or mult<=0: continue
            risk_budget=max(0,balance)*c.risk_pct
            by_risk=risk_budget/(stop_dist*mult)
            by_margin=(max(0,balance)*c.max_margin_pct*c.leverage)/(entry*mult)
            qty=min(int(by_risk),int(by_margin),max_qty)
            if qty<min_qty: continue
            notional=qty*mult*entry
            if notional/c.leverage > balance*c.max_margin_pct: continue
            entry_fee=notional*c.fee_rate
            balance-=entry_fee
            positions[sym]={"side":side,"dir":d,"entry":entry,"time":ts,"atr":atr,
                "best":entry,"stop":entry-d*stop_dist,"tp":entry+d*c.target_atr*atr,
                "qty":qty,"mult":mult,"entry_fee":entry_fee,"risk":stop_dist*qty*mult}
    # Force-close/mark open positions at final close, including both-side fees.
    for sym,p in list(positions.items()):
        bar=prepared[sym]["m"][-1]; d=p["dir"]; slip=c.slippage_bps/10000
        px=bar["c"]*(1-slip if d==1 else 1+slip)
        gross=(px-p["entry"])*d*p["qty"]*p["mult"]
        fee=abs(px*p["qty"]*p["mult"])*c.fee_rate
        pnl=gross-fee-p["entry_fee"]; balance+=gross-fee
        trades.append({"symbol":sym,"side":p["side"],"pnl":pnl,
                       "r":pnl/p["risk"] if p["risk"] else 0.0,
                       "reason":"end-of-test mark-to-market","fees":p["entry_fee"]+fee})
    return {"summary":_stats(trades,c.starting_balance,balance,max_dd),
            "trades":trades,"ending_balance":balance}

def run(days=20, pair_limit=59, starting_balance=1000.0):
    # The current one-shot Gate candle endpoint supports at most 2,000 bars.
    days=max(2,min(int(days),20))
    print(f"Portfolio backtest: {days} days | top {pair_limit} pairs")
    tickers=strategy.call("GET",f"{strategy.F}/tickers")
    contracts=strategy.call("GET",f"{strategy.F}/contracts")
    meta={x["name"]:x for x in contracts if not x.get("in_delisting")}
    ranked=sorted(tickers,key=lambda x:float(x.get("volume_24h_quote") or 0),reverse=True)
    names=[x["contract"] for x in ranked if x["contract"] in meta and
           float(x.get("volume_24h_quote") or 0)>=strategy.MIN_24H_VOLUME][:pair_limit]
    btc=strategy.candles("BTC_USDT","1h",min(2000,days*24+60))
    data={}
    for n,sym in enumerate(names,1):
        try:
            m=strategy.candles(sym,"15m",min(2000,days*96+60))
            h=strategy.candles(sym,"1h",min(2000,days*24+100))
            if m and h: data[sym]={"m15":m,"h1":h}
        except Exception as exc: print(f"{sym}: skipped ({exc})")
        if n%10==0: print(f"Downloaded {n}/{len(names)}")
        time.sleep(0.12)
    result=simulate_portfolio(data,btc,meta,SimConfig(starting_balance=starting_balance))
    s=result["summary"]
    print("\n===== SHARED-ACCOUNT PORTFOLIO RESULT =====")
    print(f"Trades {s['trades']} | win {s['win_rate_pct']:.1f}% | PF {s['profit_factor']:.2f}")
    print(f"PnL {s['net_pnl']:+.2f} USDT ({s['return_pct']:+.2f}%) | expectancy {s['expectancy']:+.3f}")
    print(f"Total R {s['total_r']:+.2f}R | max DD {s['max_drawdown_pct']:.2f}% | end {result['ending_balance']:.2f}")
    by=defaultdict(float)
    for t in result["trades"]: by[t["symbol"]]+=t["pnl"]
    print("Worst pairs:",sorted(by.items(),key=lambda x:x[1])[:5])
    print("Best pairs: ",sorted(by.items(),key=lambda x:x[1],reverse=True)[:5])
    return result

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--days",type=int,default=20)
    p.add_argument("--pairs",type=int,default=59)
    p.add_argument("--balance",type=float,default=1000)
    a=p.parse_args()
    run(a.days,a.pairs,a.balance)
