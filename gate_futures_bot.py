"""
Gate.io FUTURES auto-trading bot v2 - trend-breakout strategy with risk-based sizing.
Pure Python, no extra installs needed.

SETUP:
    export GATE_API_KEY="your_key"
    export GATE_API_SECRET="your_secret"
    export DRY_RUN="true"       # true = pretend only. false = real trades.
    export LEVERAGE="10"        # optional, default 5
    export DRY_USDT="50"        # test mode only: pretend balance

RUN:
    python gate_futures_bot.py              # run the bot
    python gate_futures_bot.py backtest     # test on the last 20 days, all busy pairs (no key needed)
    python gate_futures_bot.py backtest 20 all   # include literally every pair

STRATEGY (plain words):
  1. Only trade coins with real volume.
  2. Big picture first: the 1-hour trend of the coin AND of Bitcoin must not be against us.
  3. Entry: on the 15-minute chart, price breaks out of its last 20-candle range
     with a volume spike and healthy momentum (RSI). Long on breakout up, short on breakout down.
  4. Exit: stop-loss at 1.5x ATR (ATR = how much the coin normally moves),
     take-profit at 3x ATR (2:1 reward-to-risk), trailing stop once in profit,
     and a time limit of 12 hours.
  5. Position size is set by RISK: each trade risks about 2% of your balance if the stop is hit.
  6. After a trade closes, that coin rests for 1 hour. Bot pauses if balance drops 20%.

No strategy wins every time. Expect many small losses and some bigger wins.
"""
import os, sys, time, json, hmac, hashlib, urllib.request, urllib.parse, urllib.error
from bisect import bisect_right

# ---------- SETTINGS ----------
API_KEY = os.getenv("GATE_API_KEY", "")
API_SECRET = os.getenv("GATE_API_SECRET", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

LEVERAGE = int(os.getenv("LEVERAGE", "5"))
RISK_PCT = 0.02          # risk 2% of balance per trade (loss if stop-loss is hit)
MAX_MARGIN_PCT = 0.30    # never put more than 30% of balance into one trade's margin
MAX_POSITIONS = 3
MAX_DRAWDOWN = 0.20      # pause new trades if balance falls 20% from start
ALLOW_SHORT = True

STOP_ATR = 1.5           # stop-loss distance in ATRs
TP_ATR = 3.0             # take-profit distance in ATRs
TRAIL_AFTER_ATR = 1.5    # start trailing once profit reaches this many ATRs
MAX_HOLD_SECONDS = 12 * 3600
COOLDOWN_SECONDS = 3600

BREAKOUT_LEN = 20        # candles for the breakout range
VOLUME_SPIKE = 1.5       # breakout candle volume must be 1.5x average
MAX_CANDLE_ATR = 2.5     # skip over-stretched candles (don't chase)
MIN_24H_VOLUME = 5_000_000
SCAN_TOP = 40
FEE = 0.0005             # approx taker fee per side (used in backtest)

LOOP_SECONDS = 30
STATE_FILE = "futures_state.json"

HOST = "https://api.gateio.ws"
PREFIX = "/api/v4"
SETTLE = "usdt"
F = f"/futures/{SETTLE}"


# ---------- API HELPERS ----------
def call(method, path, query=None, body=None, auth=False):
    qs = urllib.parse.urlencode(query or {})
    payload = json.dumps(body) if body else ""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if auth:
        ts = str(int(time.time()))
        body_hash = hashlib.sha512(payload.encode()).hexdigest()
        msg = "\n".join([method, PREFIX + path, qs, body_hash, ts])
        sign = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
        headers.update({"KEY": API_KEY, "Timestamp": ts, "SIGN": sign})
    url = HOST + PREFIX + path + ("?" + qs if qs else "")
    req = urllib.request.Request(url, data=payload.encode() if payload else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gate error {e.code}: {e.read().decode()}")


def get_account():
    return call("GET", f"{F}/accounts", auth=True)


def show_balance():
    acc = get_account()
    print("\n=== YOUR GATE.IO FUTURES BALANCE ===")
    print(f"Total:     {acc.get('total')} USDT")
    print(f"Available: {acc.get('available')} USDT")
    print(f"Open PnL:  {acc.get('unrealised_pnl')} USDT")
    print("====================================\n")
    return acc


def get_positions():
    rows = call("GET", f"{F}/positions", auth=True)
    return {p["contract"]: p for p in rows if int(p.get("size", 0)) != 0}


def candles(contract, interval, limit):
    """Completed candles only (the unfinished last one is dropped)."""
    rows = call("GET", f"{F}/candlesticks",
                {"contract": contract, "interval": interval, "limit": limit})
    out = [{"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
            "l": float(r["l"]), "c": float(r["c"]), "v": float(r.get("v", 0))}
           for r in rows]
    return out[:-1]


# ---------- INDICATORS ----------
def ema(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, n=14):
    gains = losses = 0.0
    for i in range(len(values) - n, len(values)):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def atr(cs, n=14):
    trs = []
    for i in range(len(cs) - n, len(cs)):
        p = cs[i - 1]["c"]
        trs.append(max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - p), abs(cs[i]["l"] - p)))
    return sum(trs) / n


# ---------- STRATEGY ----------
def trend(h1):
    """1-hour trend: 'up', 'down' or None."""
    if len(h1) < 55:
        return None
    c = [x["c"] for x in h1]
    e20, e50 = ema(c, 20), ema(c, 50)
    if c[-1] > e50[-1] and e20[-1] > e50[-1] and e20[-1] > e20[-4]:
        return "up"
    if c[-1] < e50[-1] and e20[-1] < e50[-1] and e20[-1] < e20[-4]:
        return "down"
    return None


def breakout_signal(c15):
    """Looks at the last completed 15m candle. Returns ('long'|'short', atr) or None."""
    if len(c15) < 40:
        return None
    last = c15[-1]
    prev = c15[-1 - BREAKOUT_LEN:-1]
    hi = max(x["h"] for x in prev)
    lo = min(x["l"] for x in prev)
    avg_vol = sum(x["v"] for x in prev) / len(prev)
    a = atr(c15)
    if a <= 0 or avg_vol <= 0:
        return None
    if last["v"] < VOLUME_SPIKE * avg_vol:
        return None
    if last["h"] - last["l"] > MAX_CANDLE_ATR * a:
        return None
    r = rsi([x["c"] for x in c15])
    if last["c"] > hi and 55 < r < 75:
        return "long", a
    if last["c"] < lo and 25 < r < 45:
        return "short", a
    return None


def trend_allows(side, coin_trend, btc_trend):
    want = "up" if side == "long" else "down"
    opposite = "down" if side == "long" else "up"
    return coin_trend == want and btc_trend != opposite


def new_pos(side, entry, a, now):
    d = 1 if side == "long" else -1
    return {"side": side, "dir": d, "entry": entry, "atr": a, "best": entry,
            "stop": entry - d * STOP_ATR * a, "tp": entry + d * TP_ATR * a, "time": now}


def update_trailing(pos, extreme):
    d = pos["dir"]
    pos["best"] = max(pos["best"], extreme) if d == 1 else min(pos["best"], extreme)
    if (pos["best"] - pos["entry"]) * d >= TRAIL_AFTER_ATR * pos["atr"]:
        new = pos["best"] - d * STOP_ATR * pos["atr"]
        pos["stop"] = max(pos["stop"], new) if d == 1 else min(pos["stop"], new)


def check_exit(pos, price, now):
    d = pos["dir"]
    if (price - pos["stop"]) * d <= 0:
        return "stop-loss / trailing stop"
    if (price - pos["tp"]) * d >= 0:
        return "take-profit"
    if now - pos["time"] >= MAX_HOLD_SECONDS:
        return "time limit"
    return None


def size_contracts(balance, price, a, info):
    """Risk-based size. Returns (contracts, note)."""
    stop_pct = STOP_ATR * a / price
    if stop_pct * LEVERAGE > 0.6:
        return 0, (f"stop is {stop_pct:.1%} away, too wide for {LEVERAGE}x leverage "
                   f"(liquidation risk). Skipped.")
    notional = balance * RISK_PCT / stop_pct
    notional = min(notional, balance * MAX_MARGIN_PCT * LEVERAGE)
    mult = float(info["quanto_multiplier"])
    n = int(notional / (price * mult))
    n = min(n, int(info.get("order_size_max", n)))
    if n < int(info.get("order_size_min", 1)):
        return 0, "balance too small for this coin's minimum order. Skipped."
    return n, f"~{n * price * mult:.2f} USDT position"


# ---------- TRADING ----------
def place(contract, size, close=False):
    side = "CLOSE" if close else ("LONG" if size > 0 else "SHORT")
    print(f"  -> {'[DRY RUN] ' if DRY_RUN else ''}{side} {contract} size={size}")
    if DRY_RUN:
        return True
    order = {"contract": contract, "size": 0 if close else size, "price": "0", "tif": "ioc"}
    if close:
        order["close"] = True
    res = call("POST", f"{F}/orders", body=order, auth=True)
    print("     order status:", res.get("status"), res.get("finish_as", ""))
    return True


def set_leverage(contract, lev):
    if DRY_RUN:
        return
    try:
        call("POST", f"{F}/positions/{contract}/leverage", query={"leverage": str(lev)}, auth=True)
    except Exception as e:
        print(f"  (leverage note for {contract}: {e})")


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), indent=2)


def run_round(contracts, state, cooldown, start_balance):
    now = time.time()
    tickers = call("GET", f"{F}/tickers")
    price, active = {}, []
    for t in tickers:
        c = t["contract"]
        if c in contracts and t.get("last"):
            price[c] = float(t["last"])
            vol = float(t.get("volume_24h_quote") or 0)
            if vol >= MIN_24H_VOLUME:
                active.append((vol, c))
    active.sort(reverse=True)
    candidates = [c for _, c in active[:SCAN_TOP]]

    live = {} if DRY_RUN else get_positions()

    # 1) manage open trades
    for name in list(state.keys()):
        pos = state[name]
        if not DRY_RUN and name not in live:
            print(f"{name}: no longer open on Gate (closed or liquidated). Removing.")
            del state[name]
            cooldown[name] = now + COOLDOWN_SECONDS
            save_state(state)
            continue
        last = price.get(name)
        if not last:
            continue
        update_trailing(pos, last)
        reason = check_exit(pos, last, now)
        move = (last / pos["entry"] - 1) * pos["dir"]
        if reason:
            print(f"{name}: closing {pos['side']} - {reason} ({move:+.2%} price move)")
            place(name, 0, close=True)
            del state[name]
            cooldown[name] = now + COOLDOWN_SECONDS
        save_state(state)

    # 2) new trades - only scan right after a 15-minute candle closes
    if len(state) >= MAX_POSITIONS or (now % 900) > 150:
        return
    if DRY_RUN:
        avail = total = float(os.getenv("DRY_USDT", "50"))
    else:
        acc = get_account()
        avail, total = float(acc["available"]), float(acc["total"])
        if start_balance and total < start_balance * (1 - MAX_DRAWDOWN):
            print(f"Balance is down more than {MAX_DRAWDOWN:.0%}. No new trades.")
            return

    btc_trend = trend(candles("BTC_USDT", "1h", 100))
    for name in candidates:
        if len(state) >= MAX_POSITIONS:
            break
        if name in state or (not DRY_RUN and name in live) or cooldown.get(name, 0) > now:
            continue
        try:
            c15 = candles(name, "15m", 60)
            if not c15 or now - (c15[-1]["t"] + 900) > 150:
                continue  # signal is stale
            sig = breakout_signal(c15)
            if not sig:
                continue
            side, a = sig
            if side == "short" and not ALLOW_SHORT:
                continue
            if not trend_allows(side, trend(candles(name, "1h", 100)), btc_trend):
                continue
            n, note = size_contracts(avail, price[name], a, contracts[name])
            if n == 0:
                print(f"{name}: {side.upper()} signal - {note}")
                continue
            print(f"{name}: {side.upper()} breakout. {note}, {LEVERAGE}x, "
                  f"stop {STOP_ATR * a / price[name]:.2%} away")
            set_leverage(name, LEVERAGE)
            place(name, n if side == "long" else -n)
            state[name] = new_pos(side, price[name], a, now)
            save_state(state)
        except Exception as e:
            print(f"{name}: skip ({e})")
        time.sleep(0.25)


# ---------- BACKTEST ----------
def backtest(days=20, everything=False):
    """Replays the strategy on past data. Uses every pair the live bot would watch
    (24h volume above MIN_24H_VOLUME). Add 'all' to include every single pair."""
    days = max(2, min(days, 20))   # Gate gives at most 2000 candles per request
    n15 = days * 96 + 60
    print(f"Backtest: last {days} days of 15-minute data.")
    tickers = call("GET", f"{F}/tickers")
    rows = call("GET", f"{F}/contracts")
    ok = {c["name"] for c in rows if not c.get("in_delisting")}
    ranked = sorted(tickers, key=lambda t: float(t.get("volume_24h_quote") or 0), reverse=True)
    names = [t["contract"] for t in ranked if t["contract"] in ok and
             (everything or float(t.get("volume_24h_quote") or 0) >= MIN_24H_VOLUME)]
    print(f"Testing {len(names)} pairs. This can take several minutes - keep Termux open.\n")

    btc_h1 = candles("BTC_USDT", "1h", 700)
    btc_ends = [x["t"] + 3600 for x in btc_h1]
    trades = []          # (exit_time, R)
    per_coin = []
    for k, name in enumerate(names, 1):
        try:
            c15 = candles(name, "15m", n15)
            h1 = candles(name, "1h", 700)
        except Exception as e:
            print(f"[{k}/{len(names)}] {name} skipped")
            continue
        time.sleep(0.15)
        ends = [x["t"] + 3600 for x in h1]
        rs, pos, cool = [], None, 0
        n = len(c15)
        for i in range(60, n):
            cd = c15[i]
            if pos:
                d, ex = pos["dir"], None
                if (cd["l"] <= pos["stop"]) if d == 1 else (cd["h"] >= pos["stop"]):
                    ex = pos["stop"]
                elif (cd["h"] >= pos["tp"]) if d == 1 else (cd["l"] <= pos["tp"]):
                    ex = pos["tp"]
                elif cd["t"] - pos["time"] >= MAX_HOLD_SECONDS:
                    ex = cd["c"]
                if ex is None:
                    update_trailing(pos, cd["h"] if d == 1 else cd["l"])
                else:
                    stop_pct = STOP_ATR * pos["atr"] / pos["entry"]
                    pnl = (ex - pos["entry"]) / pos["entry"] * d - 2 * FEE
                    rs.append(pnl / stop_pct)
                    trades.append((cd["t"], pnl / stop_pct))
                    pos, cool = None, i + 4
                continue
            if i < cool or i + 1 >= n:
                continue
            sig = breakout_signal(c15[i - 59:i + 1])
            if not sig:
                continue
            side, a = sig
            if side == "short" and not ALLOW_SHORT:
                continue
            tnow = cd["t"] + 900
            ct = trend(h1[:bisect_right(ends, tnow)])
            bt = trend(btc_h1[:bisect_right(btc_ends, tnow)])
            if trend_allows(side, ct, bt):
                nxt = c15[i + 1]
                pos = new_pos(side, nxt["o"], a, nxt["t"])
        if rs:
            per_coin.append((sum(rs), name, len(rs)))
        if k % 20 == 0:
            print(f"  ...{k}/{len(names)} pairs done, {len(trades)} trades so far")

    if not trades:
        print("\nNo trades were triggered in this period.")
        return

    def summary(label, rs):
        if not rs:
            return
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf")
        print(f"{label:14s} trades={len(rs):4d}  win%={100 * len(wins) / len(rs):3.0f}  "
              f"avg R={sum(rs) / len(rs):+.2f}  total R={sum(rs):+7.1f}  profit factor={pf:.2f}")

    trades.sort()
    allr = [r for _, r in trades]
    mid = trades[len(trades) // 2][0]
    print("\n===== RESULT =====")
    summary("ALL", allr)
    summary("first half", [r for t, r in trades if t < mid])
    summary("second half", [r for t, r in trades if t >= mid])
    per_coin.sort()
    print("\nWorst 5 pairs:", ", ".join(f"{n} {r:+.1f}R" for r, n, _ in per_coin[:5]))
    print("Best 5 pairs: ", ", ".join(f"{n} {r:+.1f}R" for r, n, _ in per_coin[-5:][::-1]))
    profitable = sum(1 for r, _, _ in per_coin if r > 0)
    print(f"Pairs that made money: {profitable} of {len(per_coin)}")
    print(f"\n1 R = the amount risked per trade (about {RISK_PCT:.0%} of balance).")
    print("Profit factor above 1.0 = made money after fees. Results from a few")
    print("weeks of history are a rough check, not a promise.")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "backtest":
        nums = [x for x in sys.argv[2:] if x.isdigit()]
        backtest(int(nums[0]) if nums else 20, everything="all" in sys.argv[2:])
        return
    if not API_KEY or not API_SECRET:
        print("Set GATE_API_KEY and GATE_API_SECRET first (see top of file).")
        return
    if not DRY_RUN:
        print("LIVE TRADING BLOCKED: exchange-hosted protective orders, fill reconciliation,")
        print("restart recovery and protective-order cleanup are not implemented and tested.")
        print("Keep DRY_RUN=true. No real orders were sent.")
        return
    print("MODE:", "DRY RUN (no real trades)")
    print(f"Leverage {LEVERAGE}x | risk per trade {RISK_PCT:.0%} of balance | "
          f"stop {STOP_ATR}x ATR | target {TP_ATR}x ATR")
    acc = show_balance()
    start_balance = float(acc.get("total") or 0)

    rows = call("GET", f"{F}/contracts")
    contracts = {c["name"]: c for c in rows
                 if not c.get("in_delisting") and c.get("status", "trading") == "trading"
                 and int(c.get("leverage_max", 100)) >= LEVERAGE}
    print(f"Watching {len(contracts)} futures contracts on Gate.io")

    state, cooldown = load_state(), {}
    while True:
        try:
            run_round(contracts, state, cooldown, start_balance)
        except Exception as e:
            print("Round error:", e)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
