"""
╔══════════════════════════════════════════════════════════════╗
║         NORTH STAR — US100 ENGINE v1.0                      ║
║         Opening Range Breakout Strategy                      ║
║         Built by AurusEdge | Talisman Systems               ║
╚══════════════════════════════════════════════════════════════╝

STRATEGY LOGIC:
  1. Wait for NY session open (14:00 UTC)
  2. Define Opening Range from first 30 minutes (14:00–14:30 UTC)
  3. Filter: OR range must be 80–350 points
  4. Wait for breakout candle above/below OR with strong body
  5. Enter in breakout direction — SL at OR midpoint
  6. TP1 at 0.8R (close 50%), TP2 at 2.5R
  7. Move stop to breakeven after TP1
  8. Close all positions by 17:00 UTC

BACKTEST RESULTS (136 days):
  Win rate:   59.7%
  Expectancy: +0.22R per trade
  Return:     +28.9%
  Max DD:     -5%

REQUIREMENTS:
  pip install MetaTrader5 pandas numpy requests

SETUP:
  1. Open Deriv MT5 and log into DEMO account
  2. Confirm OTC_NDX is visible in Market Watch
  3. Run: python northstar_engine.py --status
  4. Run: python northstar_engine.py
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import requests
import time
import winsound
from datetime import datetime, timezone, timedelta

# Macro gate
try:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from macro_dashboard import get_engine_gate
    MACRO_AVAILABLE = True
except ImportError:
    MACRO_AVAILABLE = False
    def get_engine_gate(name): return True, "Macro module not found"

# ================================================================
#  CONFIGURATION
# ================================================================

SYMBOL          = "OTC_NDX"            # Confirm in MT5 Market Watch
TIMEFRAME       = mt5.TIMEFRAME_M5
TF_H4           = mt5.TIMEFRAME_H4
CANDLES_M5      = 100
CANDLES_H4      = 80

# Opening Range
OR_START_HOUR   = 14                   # UTC
OR_START_MIN    = 0
OR_END_MIN      = 30                   # OR ends at 14:30 UTC
TRADE_END_HOUR  = 17                   # Close all positions by 17:00 UTC

# OR quality filter
MIN_OR_RANGE    = 80                   # minimum OR size in points
MAX_OR_RANGE    = 350                  # maximum OR size in points

# Breakout confirmation
BREAKOUT_CONF   = 1.0                  # close must be X points beyond OR boundary
MIN_BODY_RATIO  = 0.3                  # breakout candle body must be 30%+ of range

# Stop loss — OR midpoint (validated as best in backtest)
SL_OFFSET_PCT   = 0.3                  # SL at 30% inside the OR from the boundary

# Targets
TP1_RR          = 0.8                  # TP1 at 0.8R (close 50%)
TP2_RR          = 2.5                  # TP2 at 2.5R (close remaining 50%)

# H4 bias filter
BIAS_EMA_FAST   = 21
BIAS_EMA_SLOW   = 50

# Risk
RISK_PCT        = 0.001                # 0.1% demo safe mode
MAX_TRADES_DAY  = 1                    # one OR setup per day
MAX_DAY_DD      = 0.02                 # 2% daily drawdown limit

# News filter
NEWS_BUFFER_MIN = 30
NEWS_CURRENCIES = ['USD']              # US100 only affected by USD news

LOG_FILE        = "northstar_log.txt"
ALERT_SOUND     = True

# ================================================================
#  LOGGING
# ================================================================

def log(msg, level="INFO"):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def beep(freq=800, dur=300):
    if ALERT_SOUND:
        try: winsound.Beep(freq, dur)
        except Exception: pass

def alert(msg):
    log(msg, "SIGNAL")
    beep(800,200); beep(1000,200); beep(1200,400)

# ================================================================
#  MT5 CONNECTION
# ================================================================

def connect():
    if not mt5.initialize():
        log(f"MT5 init failed: {mt5.last_error()}", "ERROR")
        return False
    info = mt5.account_info()
    if info is None:
        log("Cannot get account info. Is MT5 logged in?", "ERROR")
        return False
    log(f"Connected | Account: {info.login} | Balance: ${info.balance:.2f} | Server: {info.server}")
    if info.trade_mode != 0:
        log("WARNING: Not a demo account. Stopping for safety.", "ERROR")
        return False
    return True

def get_balance():
    info = mt5.account_info()
    return info.balance if info else None

def get_symbol_info():
    mt5.symbol_select(SYMBOL, True)
    return mt5.symbol_info(SYMBOL)

# ================================================================
#  DATA
# ================================================================

def fetch(timeframe, n):
    mt5.symbol_select(SYMBOL, True)
    rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close'}, inplace=True)
    df['candle_range'] = df['High'] - df['Low']
    df['TR']  = np.maximum(df['High']-df['Low'],
                np.maximum(abs(df['High']-df['Close'].shift(1)),
                           abs(df['Low']-df['Close'].shift(1))))
    df['ATR'] = df['TR'].rolling(14).mean()
    df['body'] = abs(df['Close'] - df['Open'])
    return df.dropna().reset_index(drop=True)

# ================================================================
#  BIAS ENGINE (H4 EMA)
# ================================================================

def get_bias(h4_df):
    if len(h4_df) < BIAS_EMA_SLOW + 5:
        return 'neutral'
    ema_fast = h4_df['Close'].ewm(span=BIAS_EMA_FAST, adjust=False).mean().iloc[-1]
    ema_slow = h4_df['Close'].ewm(span=BIAS_EMA_SLOW, adjust=False).mean().iloc[-1]
    if ema_fast > ema_slow: return 'bullish'
    elif ema_fast < ema_slow: return 'bearish'
    return 'neutral'

# ================================================================
#  SESSION & NEWS FILTERS
# ================================================================

def in_or_window():
    """Check if currently in Opening Range definition window (14:00–14:30 UTC)."""
    now = datetime.now(timezone.utc)
    return (now.hour == OR_START_HOUR and
            OR_START_MIN <= now.minute < OR_END_MIN)

def in_trading_window():
    """Check if currently in trading window (14:30–17:00 UTC)."""
    now = datetime.now(timezone.utc)
    h = now.hour; m = now.minute
    after_or  = (h == OR_START_HOUR and m >= OR_END_MIN) or (h > OR_START_HOUR)
    before_end = h < TRADE_END_HOUR
    return after_or and before_end

def past_trade_end():
    """Check if trading window has closed for today."""
    now = datetime.now(timezone.utc)
    return now.hour >= TRADE_END_HOUR

def get_news_events():
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        if r.status_code == 200:
            return [e for e in r.json()
                    if e.get('impact','').lower()=='high'
                    and e.get('country','') in NEWS_CURRENCIES]
    except Exception as ex:
        log(f"News fetch failed: {ex} — trading without news filter", "WARN")
    return []

def news_clear(events):
    if not events: return True
    now_utc = datetime.now(timezone.utc)
    for event in events:
        try:
            et = datetime.fromisoformat(event.get('date','')).astimezone(timezone.utc)
            diff = abs((now_utc - et).total_seconds() / 60)
            if diff <= NEWS_BUFFER_MIN:
                log(f"NEWS FILTER: {event.get('title','?')} in {diff:.0f} mins — skipping", "WARN")
                return False
        except Exception:
            continue
    return True

# ================================================================
#  POSITION SIZING
# ================================================================

def calc_position(entry, sl, direction, balance, sym_info):
    stop_dist = abs(entry - sl)
    if stop_dist <= 0 or stop_dist > 500:
        return None

    if direction == 'long':
        tp1 = entry + stop_dist * TP1_RR
        tp2 = entry + stop_dist * TP2_RR
    else:
        tp1 = entry - stop_dist * TP1_RR
        tp2 = entry - stop_dist * TP2_RR

    risk_amt  = balance * RISK_PCT
    point_val = sym_info.trade_contract_size * sym_info.point
    raw_lots  = risk_amt / (stop_dist * point_val) if point_val > 0 else risk_amt / stop_dist
    lots      = max(sym_info.volume_min,
                    min(sym_info.volume_max,
                        round(raw_lots / sym_info.volume_step) * sym_info.volume_step))

    return {
        'entry':    round(entry, sym_info.digits),
        'sl':       round(sl, sym_info.digits),
        'tp1':      round(tp1, sym_info.digits),
        'tp2':      round(tp2, sym_info.digits),
        'lots':     round(lots, 2),
        'risk_amt': round(risk_amt, 2),
        'stop_pts': round(stop_dist, 1),
        'rr_tp1':   TP1_RR,
        'rr_tp2':   TP2_RR,
        'digits':   sym_info.digits,
    }

# ================================================================
#  MT5 ORDER EXECUTION
# ================================================================

def place_order(pos, direction):
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        log("Cannot get tick price", "ERROR"); return None

    price      = tick.ask if direction == 'long' else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if direction == 'long' else mt5.ORDER_TYPE_SELL

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       pos['lots'],
        "type":         order_type,
        "price":        price,
        "sl":           pos['sl'],
        "tp":           pos['tp2'],
        "deviation":    30,
        "magic":        20250003,
        "comment":      "NorthStar v1.0",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Order failed: {mt5.last_error()} | {getattr(result,'comment','')}", "ERROR")
        return None

    log(f"ORDER PLACED | Ticket:{result.order} | {direction.upper()} | "
        f"Price:{price:.1f} | SL:{pos['sl']:.1f} | TP:{pos['tp2']:.1f} | "
        f"Lots:{pos['lots']} | Risk:${pos['risk_amt']:.2f}", "TRADE")
    return result.order

def close_partial(ticket, lots, direction):
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None: return False

    price      = tick.bid if direction == 'long' else tick.ask
    close_type = mt5.ORDER_TYPE_SELL if direction == 'long' else mt5.ORDER_TYPE_BUY

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       lots,
        "type":         close_type,
        "position":     ticket,
        "price":        price,
        "deviation":    30,
        "magic":        20250003,
        "comment":      "NorthStar TP1",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Partial close failed: {mt5.last_error()}", "ERROR"); return False
    log(f"TP1 PARTIAL CLOSE | Lots:{lots} | Price:{price:.1f}", "TRADE")
    return True

def modify_sl(ticket, new_sl, tp2, digits):
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   SYMBOL,
        "position": ticket,
        "sl":       round(new_sl, digits),
        "tp":       round(tp2,    digits),
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"SL modify failed: {mt5.last_error()}", "ERROR"); return False
    log(f"STOP → BREAKEVEN | New SL: {new_sl:.1f}", "TRADE")
    return True

def close_full(ticket, lots, direction, reason=""):
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None: return False

    price      = tick.bid if direction == 'long' else tick.ask
    close_type = mt5.ORDER_TYPE_SELL if direction == 'long' else mt5.ORDER_TYPE_BUY

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       lots,
        "type":         close_type,
        "position":     ticket,
        "price":        price,
        "deviation":    30,
        "magic":        20250003,
        "comment":      f"NorthStar {reason}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Full close failed: {mt5.last_error()}", "ERROR"); return False
    log(f"POSITION CLOSED ({reason}) | Price:{price:.1f}", "TRADE")
    return True

def position_alive(ticket):
    positions = mt5.positions_get(ticket=ticket)
    return positions is not None and len(positions) > 0

def get_lots(ticket):
    positions = mt5.positions_get(ticket=ticket)
    return positions[0].volume if positions else None

# ================================================================
#  DAILY STATE
# ================================================================

class DailyState:
    def __init__(self):
        self.date       = None
        self.or_high    = None
        self.or_low     = None
        self.or_range   = None
        self.or_ready   = False
        self.traded     = False
        self.start_bal  = None
        self.or_bars    = []

    def reset(self, balance):
        today = datetime.now(timezone.utc).date()
        if self.date != today:
            self.date      = today
            self.or_high   = None
            self.or_low    = None
            self.or_range  = None
            self.or_ready  = False
            self.traded    = False
            self.start_bal = balance
            self.or_bars   = []
            log(f"New day: {today} | Balance: ${balance:.2f} | "
                f"OR window: 14:00–14:30 UTC | Trade window: 14:30–17:00 UTC")
            return True
        return False

    def can_trade(self, balance):
        if self.traded:
            return False, "Already traded today"
        if self.start_bal and balance < self.start_bal * (1 - MAX_DAY_DD):
            return False, "Daily drawdown limit hit"
        return True, "OK"

# ================================================================
#  TRADE STATE
# ================================================================

class Trade:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active    = False
        self.ticket    = None
        self.direction = None
        self.entry     = None
        self.sl        = None
        self.tp1       = None
        self.tp2       = None
        self.lots      = None
        self.tp1_hit   = False
        self.risk_amt  = None
        self.pos       = None

    def open(self, ticket, direction, pos):
        self.active    = True
        self.ticket    = ticket
        self.direction = direction
        self.entry     = pos['entry']
        self.sl        = pos['sl']
        self.tp1       = pos['tp1']
        self.tp2       = pos['tp2']
        self.lots      = pos['lots']
        self.risk_amt  = pos['risk_amt']
        self.pos       = pos
        self.tp1_hit   = False

# ================================================================
#  MAIN ENGINE
# ================================================================

def run():
    log("=" * 60)
    log("  NORTH STAR ENGINE v1.0 — STARTING")
    log(f"  Symbol:    {SYMBOL}")
    log(f"  Strategy:  Opening Range Breakout")
    log(f"  OR Window: 14:00–14:30 UTC (15:00–15:30 WAT)")
    log(f"  Trade Win: 14:30–17:00 UTC (15:30–18:00 WAT)")
    log(f"  Risk:      {RISK_PCT*100:.1f}% per trade (DEMO MODE)")
    log("=" * 60)

    if not connect():
        return

    sym_info = get_symbol_info()
    if sym_info is None:
        log(f"Cannot find {SYMBOL} in MT5. Run --find-symbols.", "ERROR")
        return

    log(f"Symbol | Contract size: {sym_info.trade_contract_size} | "
        f"Min lot: {sym_info.volume_min} | Digits: {sym_info.digits}")

    daily   = DailyState()
    trade   = Trade()
    news    = []
    last_news_fetch = None
    last_m5_time    = None

    log("Engine live. Monitoring 14:00–17:00 UTC daily...")

    while True:
        try:
            balance = get_balance()
            if balance is None:
                log("MT5 connection lost. Reconnecting...", "WARN")
                if not connect(): time.sleep(15); continue

            daily.reset(balance)
            now_utc = datetime.now(timezone.utc)

            # ── Refresh news every hour ───────────────────────────────
            if last_news_fetch is None or (now_utc - last_news_fetch).seconds > 3600:
                news            = get_news_events()
                last_news_fetch = now_utc
                log(f"News refreshed: {len(news)} high-impact USD events this week")

            # ── Fetch M5 data ─────────────────────────────────────────
            m5_df = fetch(TIMEFRAME, CANDLES_M5)
            if m5_df is None:
                log("M5 data unavailable. Retrying...", "WARN")
                time.sleep(30); continue

            completed     = m5_df.iloc[:-1].copy()
            latest_m5_t   = completed['time'].iloc[-1]
            current_price = m5_df['Close'].iloc[-1]

            new_candle = latest_m5_t != last_m5_time
            if new_candle:
                last_m5_time = latest_m5_t
                log(f"Candle | {latest_m5_t} | Price: {current_price:.1f}")

            # ── Manage open position ──────────────────────────────────
            if trade.active:
                if not position_alive(trade.ticket):
                    log(f"Position {trade.ticket} closed by broker", "TRADE")
                    trade.reset(); continue

                hi = completed['High'].iloc[-1]
                lo = completed['Low'].iloc[-1]
                d  = trade.direction

                # ── Session end — force close ─────────────────────────
                if past_trade_end():
                    lots = get_lots(trade.ticket)
                    if lots:
                        close_full(trade.ticket, lots, d, "SESSION_END")
                    alert(f"SESSION END | Position closed at {current_price:.1f}")
                    trade.reset()
                    time.sleep(30); continue

                act_sl = trade.entry if trade.tp1_hit else trade.sl

                if d == 'long':
                    if lo <= act_sl:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, d, "STOP")
                        res = "BREAKEVEN" if trade.tp1_hit else "STOP"
                        alert(f"{res} HIT | Long | Entry:{trade.entry:.1f} | Exit:{act_sl:.1f}")
                        trade.reset()
                    elif not trade.tp1_hit and hi >= trade.tp1:
                        half = round(trade.lots / 2, 2)
                        sym  = get_symbol_info()
                        if half >= sym.volume_min:
                            if close_partial(trade.ticket, half, d):
                                modify_sl(trade.ticket, trade.entry, trade.tp2, trade.pos['digits'])
                                trade.tp1_hit = True
                                alert(f"TP1 HIT | Long | +{TP1_RR}R | "
                                      f"Stop → Breakeven {trade.entry:.1f}")
                                beep(600,150); beep(900,150); beep(1200,300)
                        else:
                            close_full(trade.ticket, trade.lots, d, "TP1_FULL")
                            alert(f"TP1 FULL CLOSE | Long | +{TP1_RR}R")
                            trade.reset()
                    elif trade.tp1_hit and hi >= trade.tp2:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, d, "TP2")
                        alert(f"FULL WIN — TP2 HIT | Long | +{TP2_RR}R | "
                              f"Est. +${trade.risk_amt * TP2_RR:.2f}")
                        beep(500,100); beep(700,100); beep(900,100); beep(1200,400)
                        trade.reset()

                else:  # short
                    if hi >= act_sl:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, d, "STOP")
                        res = "BREAKEVEN" if trade.tp1_hit else "STOP"
                        alert(f"{res} HIT | Short | Entry:{trade.entry:.1f} | Exit:{act_sl:.1f}")
                        trade.reset()
                    elif not trade.tp1_hit and lo <= trade.tp1:
                        half = round(trade.lots / 2, 2)
                        sym  = get_symbol_info()
                        if half >= sym.volume_min:
                            if close_partial(trade.ticket, half, d):
                                modify_sl(trade.ticket, trade.entry, trade.tp2, trade.pos['digits'])
                                trade.tp1_hit = True
                                alert(f"TP1 HIT | Short | +{TP1_RR}R | "
                                      f"Stop → Breakeven {trade.entry:.1f}")
                                beep(600,150); beep(900,150); beep(1200,300)
                        else:
                            close_full(trade.ticket, trade.lots, d, "TP1_FULL")
                            alert(f"TP1 FULL CLOSE | Short | +{TP1_RR}R")
                            trade.reset()
                    elif trade.tp1_hit and lo <= trade.tp2:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, d, "TP2")
                        alert(f"FULL WIN — TP2 HIT | Short | +{TP2_RR}R | "
                              f"Est. +${trade.risk_amt * TP2_RR:.2f}")
                        beep(500,100); beep(700,100); beep(900,100); beep(1200,400)
                        trade.reset()

                time.sleep(30); continue

            # ── Outside trading hours — sleep longer ──────────────────
            if not in_or_window() and not in_trading_window():
                next_or = "14:00 UTC (15:00 WAT)"
                if not new_candle:
                    time.sleep(60); continue
                log(f"Outside session. Waiting for OR window at {next_or}")
                time.sleep(60); continue

            # ── Build Opening Range (14:00–14:30 UTC) ─────────────────
            if in_or_window() and not daily.or_ready:
                or_bars = completed[
                    (completed['time'].dt.hour == OR_START_HOUR) &
                    (completed['time'].dt.minute < OR_END_MIN)
                ]
                if len(or_bars) >= 3:
                    daily.or_high  = or_bars['High'].max()
                    daily.or_low   = or_bars['Low'].min()
                    daily.or_range = daily.or_high - daily.or_low
                    log(f"OR building | Bars: {len(or_bars)} | "
                        f"High: {daily.or_high:.1f} | Low: {daily.or_low:.1f} | "
                        f"Range: {daily.or_range:.1f} pts")
                time.sleep(30); continue

            # ── OR just closed — validate it ──────────────────────────
            if not daily.or_ready and daily.or_high is not None:
                if daily.or_range < MIN_OR_RANGE:
                    log(f"OR too tight ({daily.or_range:.1f} pts < {MIN_OR_RANGE}). Skipping today.", "WARN")
                    daily.traded = True   # skip today
                elif daily.or_range > MAX_OR_RANGE:
                    log(f"OR too wide ({daily.or_range:.1f} pts > {MAX_OR_RANGE}). Skipping today.", "WARN")
                    daily.traded = True
                else:
                    daily.or_ready = True
                    log(f"OR CONFIRMED | High: {daily.or_high:.1f} | Low: {daily.or_low:.1f} | "
                        f"Range: {daily.or_range:.1f} pts | Watching for breakout...", "SIGNAL")
                    beep(400, 200)

            # ── Breakout detection (14:30–17:00 UTC) ──────────────────
            if not daily.or_ready or daily.traded or not in_trading_window():
                time.sleep(30); continue

            can_trade, reason = daily.can_trade(balance)
            if not can_trade:
                log(f"Trade skipped: {reason}", "WARN")
                time.sleep(30); continue

            if not news_clear(news):
                time.sleep(30); continue

            # Macro gate
            macro_ok, macro_msg = get_engine_gate('northstar')
            if not macro_ok:
                log(f"MACRO GATE: {macro_msg}", "WARN")
                time.sleep(30); continue
            else:
                log(f"Macro: {macro_msg}")

            if not new_candle:
                time.sleep(30); continue

            # Analyze last completed candle for breakout
            bar = completed.iloc[-1]
            body        = abs(bar['Close'] - bar['Open'])
            cr          = bar['candle_range']
            body_ratio  = body / cr if cr > 0 else 0

            # Get H4 bias
            h4_df = fetch(TF_H4, CANDLES_H4)
            bias  = get_bias(h4_df.iloc[:-1]) if h4_df is not None else 'neutral'

            direction = None

            # LONG breakout
            if (bar['Close'] > daily.or_high + BREAKOUT_CONF and
                bar['Close'] > bar['Open'] and
                body_ratio  >= MIN_BODY_RATIO and
                bias        != 'bearish'):
                direction = 'long'
                or_range  = daily.or_range
                sl        = daily.or_high - or_range * SL_OFFSET_PCT

            # SHORT breakout
            elif (bar['Close'] < daily.or_low - BREAKOUT_CONF and
                  bar['Open']  > bar['Close'] and
                  body_ratio   >= MIN_BODY_RATIO and
                  bias         != 'bullish'):
                direction = 'short'
                or_range  = daily.or_range
                sl        = daily.or_low + or_range * SL_OFFSET_PCT

            if direction is None:
                time.sleep(30); continue

            # ── Signal confirmed — calculate and place ─────────────────
            entry    = m5_df['Open'].iloc[-1]   # next candle open
            sym_info = get_symbol_info()
            pos      = calc_position(entry, sl, direction, balance, sym_info)

            if pos is None:
                log("Position calc invalid — skipping", "WARN")
                time.sleep(30); continue

            log(f"\n{'='*55}", "SIGNAL")
            log(f"  NORTH STAR ENTRY SIGNAL", "SIGNAL")
            log(f"{'='*55}", "SIGNAL")
            log(f"  Direction:    {direction.upper()}", "SIGNAL")
            log(f"  H4 Bias:      {bias.upper()}", "SIGNAL")
            log(f"  OR Range:     {daily.or_low:.1f} — {daily.or_high:.1f} ({daily.or_range:.1f} pts)", "SIGNAL")
            log(f"  Entry:        {pos['entry']:.1f}", "SIGNAL")
            log(f"  Stop Loss:    {pos['sl']:.1f}  ({pos['stop_pts']:.1f} pts)", "SIGNAL")
            log(f"  Take Profit1: {pos['tp1']:.1f}  ({pos['rr_tp1']}R)", "SIGNAL")
            log(f"  Take Profit2: {pos['tp2']:.1f}  ({pos['rr_tp2']}R)", "SIGNAL")
            log(f"  Lot Size:     {pos['lots']}", "SIGNAL")
            log(f"  Risk $:       ${pos['risk_amt']:.2f}", "SIGNAL")
            log(f"{'='*55}", "SIGNAL")

            ticket = place_order(pos, direction)
            if ticket:
                trade.open(ticket, direction, pos)
                daily.traded = True
                alert(f"ORDER PLACED | {direction.upper()} {SYMBOL} | "
                      f"Entry:{pos['entry']:.1f} | SL:{pos['sl']:.1f} | "
                      f"TP1:{pos['tp1']:.1f} | TP2:{pos['tp2']:.1f}")
            else:
                log("Order placement failed.", "ERROR")

        except KeyboardInterrupt:
            log("Engine stopped by user.")
            if trade.active:
                log(f"WARNING: Position {trade.ticket} may still be open on MT5.", "WARN")
            break
        except Exception as e:
            log(f"Unexpected error: {e}", "ERROR")
            time.sleep(30)

        time.sleep(30)

    mt5.shutdown()
    log("North Star engine shut down cleanly.")

# ================================================================
#  UTILITIES
# ================================================================

def find_symbols():
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}"); return
    syms = mt5.symbols_get()
    print("\nUS100/Index symbols on your MT5:")
    print("-" * 40)
    for s in syms:
        if any(x in s.name.upper() for x in ['NDX','NAS','US100','USTEC','NQ']):
            print(f"  {s.name}  —  {s.description}")
    mt5.shutdown()

# ================================================================
#  ENTRY POINT
# ================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--find-symbols":
        find_symbols()

    elif len(sys.argv) > 1 and sys.argv[1] == "--status":
        if not connect(): exit()
        mt5.symbol_select(SYMBOL, True)
        h4   = fetch(TF_H4, 60)
        tick = mt5.symbol_info_tick(SYMBOL)
        now  = datetime.now(timezone.utc)
        print(f"\nNorth Star Status")
        print(f"  Symbol:   {SYMBOL}")
        print(f"  Price:    {tick.ask:.1f}" if tick else "  Price:   N/A")
        print(f"  H4 Bias:  {get_bias(h4.iloc[:-1]).upper()}" if h4 is not None else "  H4 Bias: N/A")
        print(f"  UTC Time: {now.strftime('%H:%M')} UTC")
        in_or  = in_or_window()
        in_trd = in_trading_window()
        print(f"  OR Window:    {'ACTIVE' if in_or  else 'Inactive (14:00–14:30 UTC)'}")
        print(f"  Trade Window: {'ACTIVE' if in_trd else 'Inactive (14:30–17:00 UTC)'}")
        mt5.shutdown()

    else:
        run()
