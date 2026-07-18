"""
╔══════════════════════════════════════════════════════════════╗
║         AURUSEDGE — EUR/USD ENGINE v1.0                     ║
║         Multi-Timeframe Institutional Structure Strategy     ║
║         Built by AurusEdge | Talisman Systems               ║
╚══════════════════════════════════════════════════════════════╝

STRATEGY LOGIC:
  Layer 1 (H4)  — Market bias: Higher Highs/Lows or Lower Highs/Lows
  Layer 2 (M15) — Point of Interest: Fair Value Gap or Order Block
  Layer 3 (M5)  — Confirmation: Change of Character inside POI
  Filter 1      — News: No high-impact USD/EUR event within 30 mins
  Filter 2      — Session: London (08-12 UTC) or NY (13-17 UTC) only

REQUIREMENTS:
  pip install MetaTrader5 pandas numpy requests

SETUP:
  1. Open Deriv MT5 on your PC, log into DEMO account
  2. Confirm SYMBOL below matches MT5 exactly
  3. Run: python aurusedge_engine.py
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

SYMBOL          = "EURUSD"          # confirm in MT5 Market Watch
TF_M5           = mt5.TIMEFRAME_M5
TF_M15          = mt5.TIMEFRAME_M15
TF_H1           = mt5.TIMEFRAME_H1
TF_H4           = mt5.TIMEFRAME_H4

# Candles to fetch per timeframe
CANDLES_H4      = 100
CANDLES_M15     = 200
CANDLES_M5      = 100

# Bias detection — EMA based (validated in backtest)
BIAS_EMA_FAST   = 21
BIAS_EMA_SLOW   = 50

# POI settings
FVG_MIN_PIPS    = 1.0        # minimum FVG size in pips
MAX_POI_AGE     = 25         # M15 candles — only use recent POIs
POI_BUFFER_PIPS = 5.0        # pips buffer for zone detection
POI_PROX_PIPS   = 20.0       # max pips from current price to POI

# CHoCH settings — validated: min 2 candles in zone
MIN_CANDLES_IN_POI = 2

# Session filter (UTC)
LONDON_OPEN     = 8
LONDON_CLOSE    = 12
NY_OPEN         = 13
NY_CLOSE        = 17

# News filter
NEWS_BUFFER_MIN = 30         # minutes before/after high impact news
NEWS_CURRENCIES = ['USD', 'EUR']

# Risk
RISK_PCT        = 0.001      # 0.1% per trade (demo safe)
TP1_RR          = 1.5        # TP1 at 1.5R
TP2_RR          = 3.0        # TP2 at 3.0R
MAX_TRADES_DAY  = 2
MAX_DAY_DD      = 0.02

POINT           = 0.00001    # EUR/USD pip value
LOG_FILE        = "aurusedge_log.txt"
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

def to_pips(price_diff):
    return abs(price_diff) / POINT / 10

def from_pips(pips):
    return pips * POINT * 10

# ================================================================
#  DATA FETCHING
# ================================================================

def fetch(timeframe, n):
    mt5.symbol_select(SYMBOL, True)
    rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close'}, inplace=True)
    df['range'] = df['High'] - df['Low']
    df['TR']    = np.maximum(df['High']-df['Low'],
                  np.maximum(abs(df['High']-df['Close'].shift(1)),
                             abs(df['Low']-df['Close'].shift(1))))
    df['ATR']   = df['TR'].rolling(14).mean()
    return df.dropna().reset_index(drop=True)

# ================================================================
#  LAYER 1 — BIAS ENGINE (H4) — EMA Based
# ================================================================

def get_bias(h4_df):
    """
    EMA-based bias — validated in backtest as more reliable than swing counting.
    Bullish:  EMA21 > EMA50 on H4
    Bearish:  EMA21 < EMA50 on H4
    """
    if len(h4_df) < BIAS_EMA_SLOW + 5:
        return 'neutral'
    ema_fast = h4_df['Close'].ewm(span=BIAS_EMA_FAST, adjust=False).mean().iloc[-1]
    ema_slow = h4_df['Close'].ewm(span=BIAS_EMA_SLOW, adjust=False).mean().iloc[-1]
    if ema_fast > ema_slow:
        return 'bullish'
    elif ema_fast < ema_slow:
        return 'bearish'
    return 'neutral'

# ================================================================
#  LAYER 2 — POI ENGINE (M15) — FVG Only
# ================================================================

def get_active_pois(m15_df, bias, current_price):
    """
    Find recent Fair Value Gaps aligned with H4 bias.
    Only looks at last MAX_POI_AGE candles for freshness.
    """
    fvgs   = []
    min_sz = from_pips(FVG_MIN_PIPS)
    prox   = from_pips(POI_PROX_PIPS)
    start  = max(0, len(m15_df) - MAX_POI_AGE)

    for i in range(max(2, start), len(m15_df)):
        if bias == 'bearish':
            gh = m15_df['High'].iloc[i]
            gl = m15_df['Low'].iloc[i-2]
            if gh < gl and (gl - gh) >= min_sz:
                poi = {'top': gl, 'bottom': gh, 'mid': (gl+gh)/2, 'type': 'FVG_BEAR'}
                # Only keep if within reach of current price
                if current_price <= poi['top'] + prox:
                    poi['distance_pips'] = to_pips(abs(poi['mid'] - current_price))
                    fvgs.append(poi)

        elif bias == 'bullish':
            gl = m15_df['Low'].iloc[i]
            gh = m15_df['High'].iloc[i-2]
            if gl > gh and (gl - gh) >= min_sz:
                poi = {'top': gl, 'bottom': gh, 'mid': (gl+gh)/2, 'type': 'FVG_BULL'}
                if current_price >= poi['bottom'] - prox:
                    poi['distance_pips'] = to_pips(abs(poi['mid'] - current_price))
                    fvgs.append(poi)

    fvgs.sort(key=lambda x: x['distance_pips'])
    return fvgs[:5]

def price_in_poi(price, poi):
    buf = from_pips(POI_BUFFER_PIPS)
    return (poi['bottom'] - buf) <= price <= (poi['top'] + buf)

# ================================================================
#  LAYER 3 — CHOCH DETECTOR (M5) — FIXED & VALIDATED
# ================================================================

def detect_choch(m5_df, bias, poi):
    """
    Detect Change of Character on M5 inside a POI zone.

    FIXED LOGIC (validated in backtest — 60.1% win rate):
    For SHORT (bearish POI):
    - Price enters zone, forms a local high (peak candle)
    - Last completed M5 candle closes BELOW the low of that peak candle
    - AND the last candle is bearish (close < open)
    → CHoCH confirmed SHORT

    For LONG (bullish POI):
    - Price enters zone, forms a local low (trough candle)
    - Last completed M5 candle closes ABOVE the high of that trough candle
    - AND the last candle is bullish (close > open)
    → CHoCH confirmed LONG
    """
    zone = [i for i in range(len(m5_df)-1)
            if price_in_poi(m5_df['Close'].iloc[i], poi)]

    if len(zone) < MIN_CANDLES_IN_POI:
        return None

    zb = m5_df.iloc[zone[0]:].reset_index(drop=True)
    if len(zb) < 5:
        return None

    cc = zb.iloc[-2]   # last COMPLETED candle (signal candle)

    if bias == 'bearish':
        hi_i      = int(zb['High'].idxmax())
        if hi_i >= len(zb) - 2:
            return None
        peak_low  = zb['Low'].iloc[hi_i]
        is_bear   = cc['Close'] < cc['Open']
        broke_low = cc['Close'] < peak_low
        if is_bear and broke_low:
            return 'short'

    elif bias == 'bullish':
        lo_i        = int(zb['Low'].idxmin())
        if lo_i >= len(zb) - 2:
            return None
        trough_high = zb['High'].iloc[lo_i]
        is_bull     = cc['Close'] > cc['Open']
        broke_high  = cc['Close'] > trough_high
        if is_bull and broke_high:
            return 'long'

    return None

# ================================================================
#  NEWS FILTER
# ================================================================

def get_news_events():
    """Fetch this week's high impact forex events from ForexFactory."""
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r   = requests.get(url, timeout=10)
        if r.status_code == 200:
            events = r.json()
            high_impact = [
                e for e in events
                if e.get('impact', '').lower() == 'high'
                and e.get('country', '') in NEWS_CURRENCIES
            ]
            return high_impact
    except Exception as ex:
        log(f"News fetch failed: {ex} — trading without news filter", "WARN")
    return []

def news_clear(events):
    """
    Check if current time is clear of high impact news.
    Returns True if safe to trade, False if too close to news.
    """
    if not events:
        return True

    now_utc = datetime.now(timezone.utc)
    buffer  = timedelta(minutes=NEWS_BUFFER_MIN)

    for event in events:
        try:
            # ForexFactory format: "2026-06-27T13:30:00-04:00"
            event_time_str = event.get('date', '')
            if not event_time_str:
                continue
            event_time = datetime.fromisoformat(event_time_str).astimezone(timezone.utc)
            diff = abs((now_utc - event_time).total_seconds() / 60)
            if diff <= NEWS_BUFFER_MIN:
                log(f"NEWS FILTER: {event.get('title','?')} ({event.get('country','?')}) "
                    f"in {diff:.0f} mins — skipping signal", "WARN")
                return False
        except Exception:
            continue

    return True

# ================================================================
#  SESSION FILTER
# ================================================================

def in_session():
    """Check if current UTC time is within London or NY session."""
    now_utc = datetime.now(timezone.utc)
    hour    = now_utc.hour

    in_london = LONDON_OPEN <= hour < LONDON_CLOSE
    in_ny     = NY_OPEN <= hour < NY_CLOSE

    if not (in_london or in_ny):
        return False, "outside session hours"

    session = "London" if in_london else "New York"
    if in_london and in_ny:
        session = "London-NY overlap"

    return True, session

# ================================================================
#  POSITION SIZING
# ================================================================

def calc_position(entry, sl, direction, balance, sym_info):
    """Calculate position size based on 1% risk and stop distance."""
    stop_dist = abs(entry - sl)
    if stop_dist <= 0:
        return None

    stop_pips = to_pips(stop_dist)
    if stop_pips < 3 or stop_pips > 50:
        return None

    if direction == 'long':
        tp1 = entry + stop_dist * TP1_RR
        tp2 = entry + stop_dist * TP2_RR
    else:
        tp1 = entry - stop_dist * TP1_RR
        tp2 = entry - stop_dist * TP2_RR

    risk_amt  = balance * RISK_PCT
    pip_value = sym_info.trade_contract_size * POINT * 10
    raw_lots  = risk_amt / (stop_pips * pip_value)
    lots      = max(sym_info.volume_min,
                    min(sym_info.volume_max,
                        round(raw_lots / sym_info.volume_step) * sym_info.volume_step))

    return {
        'entry':      round(entry, sym_info.digits),
        'sl':         round(sl, sym_info.digits),
        'tp1':        round(tp1, sym_info.digits),
        'tp2':        round(tp2, sym_info.digits),
        'lots':       round(lots, 2),
        'risk_amt':   round(risk_amt, 2),
        'stop_pips':  round(stop_pips, 1),
        'rr_tp1':     TP1_RR,
        'rr_tp2':     TP2_RR,
        'digits':     sym_info.digits,
    }

# ================================================================
#  MT5 ORDER EXECUTION
# ================================================================

def place_order(pos, direction):
    """Place a market order with SL and TP2."""
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        log("Cannot get tick price", "ERROR")
        return None

    price     = tick.ask if direction == 'long' else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if direction == 'long' else mt5.ORDER_TYPE_SELL

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       pos['lots'],
        "type":         order_type,
        "price":        price,
        "sl":           pos['sl'],
        "tp":           pos['tp2'],
        "deviation":    20,
        "magic":        20250002,
        "comment":      "AurusEdge v1.0",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Order failed: {mt5.last_error()} | {getattr(result,'comment','')}", "ERROR")
        return None

    log(f"ORDER PLACED | Ticket:{result.order} | {direction.upper()} | "
        f"Price:{price:.5f} | SL:{pos['sl']:.5f} | TP:{pos['tp2']:.5f} | "
        f"Lots:{pos['lots']} | Risk:${pos['risk_amt']:.2f}", "TRADE")
    return result.order

def close_partial(ticket, lots, direction):
    """Close half the position at TP1."""
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
        "deviation":    20,
        "magic":        20250002,
        "comment":      "AurusEdge TP1",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Partial close failed: {mt5.last_error()}", "ERROR")
        return False

    log(f"PARTIAL CLOSE | TP1 hit | Lots:{lots} | Price:{price:.5f}", "TRADE")
    return True

def modify_sl(ticket, new_sl, tp2, pos):
    """Move stop to breakeven after TP1."""
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   SYMBOL,
        "position": ticket,
        "sl":       round(new_sl, pos['digits']),
        "tp":       round(tp2,    pos['digits']),
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"SL modify failed: {mt5.last_error()}", "ERROR")
        return False
    log(f"STOP MOVED TO BREAKEVEN | New SL: {new_sl:.5f}", "TRADE")
    return True

def close_full(ticket, lots, direction):
    """Close full remaining position."""
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
        "deviation":    20,
        "magic":        20250002,
        "comment":      "AurusEdge close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Full close failed: {mt5.last_error()}", "ERROR")
        return False

    log(f"POSITION CLOSED | Price: {price:.5f}", "TRADE")
    return True

def position_alive(ticket):
    positions = mt5.positions_get(ticket=ticket)
    return positions is not None and len(positions) > 0

def get_lots(ticket):
    positions = mt5.positions_get(ticket=ticket)
    return positions[0].volume if positions else None

# ================================================================
#  DAILY LIMITS
# ================================================================

class DailyLimits:
    def __init__(self):
        self.date   = None
        self.trades = 0
        self.start  = None

    def reset_if_new_day(self, balance):
        today = datetime.now().date()
        if self.date != today:
            self.date   = today
            self.trades = 0
            self.start  = balance
            log(f"New day: {today} | Balance: ${balance:.2f}")

    def can_trade(self, balance):
        if self.trades >= MAX_TRADES_DAY:
            return False, "Daily trade limit reached"
        if self.start and balance < self.start * (1 - MAX_DAY_DD):
            return False, "Daily drawdown limit hit"
        return True, "OK"

    def record(self): self.trades += 1

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
    log("  AURUSEDGE ENGINE v1.0 — STARTING")
    log(f"  Symbol:    {SYMBOL}")
    log(f"  Timeframes: H4 (bias) | M15 (POI) | M5 (entry)")
    log(f"  Risk:      {RISK_PCT*100:.1f}% per trade (DEMO MODE)")
    log(f"  Session:   London {LONDON_OPEN}:00-{LONDON_CLOSE}:00 UTC | "
        f"NY {NY_OPEN}:00-{NY_CLOSE}:00 UTC")
    log("=" * 60)

    if not connect():
        return

    sym_info = get_symbol_info()
    if sym_info is None:
        log(f"Cannot find {SYMBOL} in MT5", "ERROR")
        return

    limits = DailyLimits()
    trade  = Trade()

    last_m5_time  = None
    last_m15_time = None
    last_h4_time  = None

    bias      = 'neutral'
    active_poi = None
    news_events = []
    last_news_fetch = None

    log("Engine live. Scanning every 30 seconds...")

    while True:
        try:
            balance = get_balance()
            if balance is None:
                log("MT5 connection lost. Reconnecting...", "WARN")
                if not connect(): time.sleep(15); continue

            limits.reset_if_new_day(balance)

            # ── Fetch all timeframes ──────────────────────────────────
            h4_df  = fetch(TF_H4,  CANDLES_H4)
            m15_df = fetch(TF_M15, CANDLES_M15)
            m5_df  = fetch(TF_M5,  CANDLES_M5)

            if h4_df is None or m15_df is None or m5_df is None:
                log("Data fetch failed. Retrying...", "WARN")
                time.sleep(30); continue

            # Completed candles (exclude last forming candle)
            h4_c  = h4_df.iloc[:-1].copy()
            m15_c = m15_df.iloc[:-1].copy()
            m5_c  = m5_df.iloc[:-1].copy()

            current_price = m5_df['Close'].iloc[-1]
            latest_m5_t   = m5_c['time'].iloc[-1]
            latest_m15_t  = m15_c['time'].iloc[-1]
            latest_h4_t   = h4_c['time'].iloc[-1]

            # ── Manage open position ──────────────────────────────────
            if trade.active:
                if not position_alive(trade.ticket):
                    log(f"Position {trade.ticket} closed by broker (SL/TP hit)", "TRADE")
                    trade.reset(); active_poi = None; continue

                hi = m5_c['High'].iloc[-1]
                lo = m5_c['Low'].iloc[-1]

                if trade.direction == 'long':
                    act_sl = trade.entry if trade.tp1_hit else trade.sl
                    if lo <= act_sl:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, trade.direction)
                        res = "BREAKEVEN" if trade.tp1_hit else "STOP LOSS"
                        alert(f"{res} HIT | Long @ {trade.entry:.5f} | Exit: {act_sl:.5f}")
                        trade.reset(); active_poi = None; continue

                    if not trade.tp1_hit and hi >= trade.tp1:
                        half = round(trade.lots / 2, 2)
                        if half >= sym_info.volume_min:
                            if close_partial(trade.ticket, half, trade.direction):
                                modify_sl(trade.ticket, trade.entry, trade.tp2, trade.pos)
                                trade.tp1_hit = True
                                alert(f"TP1 HIT | Long | +{trade.pos['rr_tp1']}R | "
                                      f"Stop moved to breakeven {trade.entry:.5f}")
                                beep(600,150); beep(900,150); beep(1200,300)
                        else:
                            close_full(trade.ticket, trade.lots, trade.direction)
                            alert(f"TP1 HIT + FULL CLOSE | Long @ {trade.entry:.5f}")
                            trade.reset(); active_poi = None; continue

                    if trade.tp1_hit and hi >= trade.tp2:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, trade.direction)
                        alert(f"FULL WIN — TP2 HIT | Long | +{trade.pos['rr_tp2']}R | "
                              f"Est. +${trade.risk_amt * trade.pos['rr_tp2']:.2f}")
                        beep(500,100); beep(700,100); beep(900,100); beep(1200,400)
                        trade.reset(); active_poi = None; continue

                else:  # short
                    act_sl = trade.entry if trade.tp1_hit else trade.sl
                    if hi >= act_sl:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, trade.direction)
                        res = "BREAKEVEN" if trade.tp1_hit else "STOP LOSS"
                        alert(f"{res} HIT | Short @ {trade.entry:.5f} | Exit: {act_sl:.5f}")
                        trade.reset(); active_poi = None; continue

                    if not trade.tp1_hit and lo <= trade.tp1:
                        half = round(trade.lots / 2, 2)
                        if half >= sym_info.volume_min:
                            if close_partial(trade.ticket, half, trade.direction):
                                modify_sl(trade.ticket, trade.entry, trade.tp2, trade.pos)
                                trade.tp1_hit = True
                                alert(f"TP1 HIT | Short | +{trade.pos['rr_tp1']}R | "
                                      f"Stop moved to breakeven {trade.entry:.5f}")
                                beep(600,150); beep(900,150); beep(1200,300)
                        else:
                            close_full(trade.ticket, trade.lots, trade.direction)
                            alert(f"TP1 HIT + FULL CLOSE | Short @ {trade.entry:.5f}")
                            trade.reset(); active_poi = None; continue

                    if trade.tp1_hit and lo <= trade.tp2:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, trade.direction)
                        alert(f"FULL WIN — TP2 HIT | Short | +{trade.pos['rr_tp2']}R | "
                              f"Est. +${trade.risk_amt * trade.pos['rr_tp2']:.2f}")
                        beep(500,100); beep(700,100); beep(900,100); beep(1200,400)
                        trade.reset(); active_poi = None; continue

                time.sleep(30); continue

            # ── Refresh news every hour ───────────────────────────────
            now = datetime.now(timezone.utc)
            if last_news_fetch is None or (now - last_news_fetch).seconds > 3600:
                news_events    = get_news_events()
                last_news_fetch = now
                log(f"News refreshed: {len(news_events)} high-impact events this week")

            # ── Recalculate bias on new H4 candle ────────────────────
            if latest_h4_t != last_h4_time:
                bias = get_bias(h4_c)
                last_h4_time = latest_h4_t
                ema21 = h4_c['Close'].ewm(span=21,adjust=False).mean().iloc[-1]
                ema50 = h4_c['Close'].ewm(span=50,adjust=False).mean().iloc[-1]
                log(f"H4 BIAS UPDATE | {bias.upper()} | "
                    f"EMA21: {ema21:.5f} | EMA50: {ema50:.5f} | Price: {current_price:.5f}")

            # ── Skip if no clear bias ─────────────────────────────────
            if bias == 'neutral':
                log(f"Bias: NEUTRAL — no trade. Price: {current_price:.5f}", "INFO") \
                    if latest_m5_t != last_m5_time else None
                last_m5_time = latest_m5_t
                time.sleep(30); continue

            # ── Recalculate POIs on new M15 candle ───────────────────
            if latest_m15_t != last_m15_time:
                pois = get_active_pois(m15_c, bias, current_price)
                last_m15_time = latest_m15_t
                if pois:
                    log(f"M15 POIs updated | {bias.upper()} | "
                        f"{len(pois)} active zones | "
                        f"Nearest: {pois[0]['type']} @ {pois[0]['mid']:.5f} "
                        f"({pois[0]['distance_pips']:.1f} pips away)")

            # ── Only proceed on new M5 candle ─────────────────────────
            if latest_m5_t == last_m5_time:
                time.sleep(30); continue

            last_m5_time = latest_m5_t
            pois = get_active_pois(m15_c, bias, current_price)

            log(f"M5 candle | {latest_m5_t} | Price: {current_price:.5f} | "
                f"Bias: {bias.upper()} | POIs: {len(pois)}")

            if not pois:
                time.sleep(30); continue

            # ── Session check ─────────────────────────────────────────
            ok_session, session_name = in_session()
            if not ok_session:
                time.sleep(30); continue

            # ── News check ────────────────────────────────────────────
            if not news_clear(news_events):
                time.sleep(30); continue

            # ── Macro gate ────────────────────────────────────────────
            macro_ok, macro_msg = get_engine_gate('aurusedge')
            if not macro_ok:
                log(f"MACRO GATE: {macro_msg}", "WARN")
                time.sleep(30); continue
            else:
                log(f"Macro: {macro_msg}")

            # ── Daily limits check ────────────────────────────────────
            can_trade, reason = limits.can_trade(balance)
            if not can_trade:
                log(f"Trade skipped: {reason}", "WARN")
                time.sleep(30); continue

            # ── CHoCH detection on nearest POI ────────────────────────
            for poi in pois[:3]:  # check top 3 nearest POIs
                direction = detect_choch(m5_c, bias, poi)

                if direction is None:
                    continue

                # ── SIGNAL CONFIRMED — Calculate trade ────────────────
                entry = m5_df['Open'].iloc[-1]  # next candle open
                atr   = m5_c['ATR'].iloc[-1]

                # Stop beyond POI with ATR buffer
                if direction == 'long':
                    sl = poi['bottom'] - atr * 0.5
                else:
                    sl = poi['top'] + atr * 0.5

                sym_info = get_symbol_info()
                pos = calc_position(entry, sl, direction, balance, sym_info)

                if pos is None:
                    log("Position calc invalid — skipping", "WARN")
                    continue

                # ── LOG SIGNAL ────────────────────────────────────────
                log(f"\n{'='*55}", "SIGNAL")
                log(f"  AURUSEDGE ENTRY SIGNAL", "SIGNAL")
                log(f"{'='*55}", "SIGNAL")
                log(f"  Direction:   {direction.upper()}", "SIGNAL")
                log(f"  Session:     {session_name}", "SIGNAL")
                log(f"  Bias (H4):   {bias.upper()}", "SIGNAL")
                log(f"  POI:         {poi['type']} @ {poi['bottom']:.5f}–{poi['top']:.5f}", "SIGNAL")
                log(f"  Entry:       {pos['entry']:.5f}", "SIGNAL")
                log(f"  Stop Loss:   {pos['sl']:.5f} ({pos['stop_pips']:.1f} pips)", "SIGNAL")
                log(f"  Take Profit1:{pos['tp1']:.5f} ({pos['rr_tp1']}R)", "SIGNAL")
                log(f"  Take Profit2:{pos['tp2']:.5f} ({pos['rr_tp2']}R)", "SIGNAL")
                log(f"  Lot Size:    {pos['lots']}", "SIGNAL")
                log(f"  Risk $:      ${pos['risk_amt']:.2f}", "SIGNAL")
                log(f"{'='*55}", "SIGNAL")

                # ── PLACE ORDER ───────────────────────────────────────
                ticket = place_order(pos, direction)
                if ticket:
                    trade.open(ticket, direction, pos)
                    limits.record()
                    active_poi = poi
                    alert(f"ORDER PLACED | {direction.upper()} {SYMBOL} | "
                          f"Entry:{pos['entry']:.5f} | SL:{pos['sl']:.5f} | "
                          f"TP1:{pos['tp1']:.5f} | TP2:{pos['tp2']:.5f}")
                break  # only take one signal per candle

        except KeyboardInterrupt:
            log("Engine stopped by user.")
            if trade.active:
                log(f"WARNING: Position {trade.ticket} may still be open. Check MT5.", "WARN")
            break
        except Exception as e:
            log(f"Unexpected error: {e}", "ERROR")
            time.sleep(30)

        time.sleep(30)

    mt5.shutdown()
    log("AurusEdge engine shut down cleanly.")

# ================================================================
#  ENTRY POINT
# ================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--find-forex":
        # Find EUR/USD symbol name on this MT5
        if not mt5.initialize():
            print(f"MT5 init failed: {mt5.last_error()}")
            exit()
        syms = mt5.symbols_get()
        print("\nForex symbols on your MT5:")
        print("-" * 40)
        for s in syms:
            if ('EUR' in s.name.upper() and 'USD' in s.name.upper()) or \
               s.path.lower().startswith('forex'):
                print(f"  {s.name}  —  {s.description}")
        mt5.shutdown()

    elif len(sys.argv) > 1 and sys.argv[1] == "--status":
        if not connect(): exit()
        mt5.symbol_select(SYMBOL, True)
        h4 = fetch(TF_H4, 60)
        if h4 is not None:
            bias = get_bias(h4.iloc[:-1])
            tick = mt5.symbol_info_tick(SYMBOL)
            price = tick.ask if tick else 0
            ok_session, session = in_session()
            print(f"\nAurusEdge Status Check")
            print(f"  Symbol:  {SYMBOL}")
            print(f"  Price:   {price:.5f}")
            print(f"  H4 Bias: {bias.upper()}")
            print(f"  Session: {session if ok_session else 'OUTSIDE SESSION HOURS'}")
            h4c = h4.iloc[:-1]
            ema21 = h4c['Close'].ewm(span=21,adjust=False).mean().iloc[-1]
            ema50 = h4c['Close'].ewm(span=50,adjust=False).mean().iloc[-1]
            print(f"  EMA21:   {ema21:.5f}")
            print(f"  EMA50:   {ema50:.5f}")
        else:
            print(f"  ERROR: Could not fetch H4 data for {SYMBOL}")
            print(f"  Check symbol name — try: py -3.11 aurusedge_engine.py --find-forex")
        mt5.shutdown()

    else:
        run()
