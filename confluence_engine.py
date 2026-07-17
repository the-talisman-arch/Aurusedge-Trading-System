"""
╔══════════════════════════════════════════════════════════════╗
║         CONFLUENCE — XAU/USD ENGINE v1.0                    ║
║         Gold London Session FVG Strategy                    ║
║         Built by AurusEdge | Talisman Systems               ║
╚══════════════════════════════════════════════════════════════╝

STRATEGY LOGIC:
  Layer 1 (H4+H1) — Dual timeframe bias alignment
                     Both H4 and H1 EMA21/EMA50 must agree
  Layer 2 (M15)   — Fair Value Gap identification
  Layer 3 (M5)    — CHoCH confirmation inside FVG
  Session          — London session only (07:00–12:00 UTC)
  News filter      — No USD/XAU events within 30 minutes

BACKTEST RESULTS (138 days):
  Win rate:    55.4%
  Expectancy: +0.0887R per trade
  Trades:      85 over 138 days
  Return:      +16.1%

REQUIREMENTS:
  pip install MetaTrader5 pandas numpy requests

SETUP:
  1. Open Deriv MT5 and log into DEMO account
  2. Run: python confluence_engine.py --find-symbols
  3. Update SYMBOL if needed
  4. Run: python confluence_engine.py
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import requests
import time
import winsound
import sys
import os
from datetime import datetime, timezone, timedelta

# Import macro dashboard gate
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from macro_dashboard import get_engine_gate, read_macro_state
    MACRO_AVAILABLE = True
except ImportError:
    MACRO_AVAILABLE = False
    def get_engine_gate(name): return True, "Macro module not found"
    def read_macro_state(): return None

# ================================================================
#  CONFIGURATION
# ================================================================

SYMBOL          = "frxXAUUSD"          # confirm in MT5
TF_M5           = mt5.TIMEFRAME_M5
TF_M15          = mt5.TIMEFRAME_M15
TF_H1           = mt5.TIMEFRAME_H1
TF_H4           = mt5.TIMEFRAME_H4

CANDLES_M5      = 100
CANDLES_M15     = 200
CANDLES_H1      = 100
CANDLES_H4      = 80

# Session — London only (best performing in backtest)
SESSION_START   = 7    # UTC
SESSION_END     = 12   # UTC

# Bias — dual timeframe (H4 + H1 must agree)
BIAS_EMA_FAST   = 21
BIAS_EMA_SLOW   = 50

# POI — FVG settings
FVG_MIN_SIZE    = 2.0          # minimum $2 FVG
MAX_POI_AGE     = 20           # M15 candles
POI_BUFFER      = 3.0          # $3 buffer for zone detection
POI_PROX        = 15.0         # max $15 from price to POI

# CHoCH
MIN_ZONE_BARS   = 2

# Trade management
TP1_RR          = 1.5
TP2_RR          = 3.0
MIN_STOP        = 6.0          # minimum $6 stop
MAX_STOP        = 70.0         # maximum $70 stop
MIN_ATR         = 3.0          # skip very low volatility

# Risk
RISK_PCT        = 0.001        # 0.1% demo safe mode
MAX_TRADES_DAY  = 2
MAX_DAY_DD      = 0.02

# News
NEWS_BUFFER_MIN = 30
NEWS_CURRENCIES = ['USD', 'XAU']

LOG_FILE        = "confluence_log.txt"
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
    beep(700,200); beep(900,200); beep(1100,200); beep(1300,400)

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
    df['range'] = df['High'] - df['Low']
    df['TR']    = np.maximum(df['High']-df['Low'],
                  np.maximum(abs(df['High']-df['Close'].shift(1)),
                             abs(df['Low']-df['Close'].shift(1))))
    df['ATR']   = df['TR'].rolling(14).mean()
    df['body']  = abs(df['Close'] - df['Open'])
    return df.dropna().reset_index(drop=True)

# ================================================================
#  DUAL TIMEFRAME BIAS
# ================================================================

def get_ema_bias(df):
    if len(df) < BIAS_EMA_SLOW + 5:
        return 'neutral'
    ema_f = df['Close'].ewm(span=BIAS_EMA_FAST, adjust=False).mean().iloc[-1]
    ema_s = df['Close'].ewm(span=BIAS_EMA_SLOW, adjust=False).mean().iloc[-1]
    if ema_f > ema_s: return 'bullish'
    elif ema_f < ema_s: return 'bearish'
    return 'neutral'

def get_dual_bias(h4_df, h1_df):
    """Both H4 and H1 must agree for a valid bias signal."""
    bias_h4 = get_ema_bias(h4_df)
    bias_h1 = get_ema_bias(h1_df)
    if bias_h4 == bias_h1 and bias_h4 != 'neutral':
        return bias_h4
    return 'neutral'

# ================================================================
#  FVG DETECTION (M15)
# ================================================================

def get_fvgs(m15_df, bias):
    fvgs  = []
    start = max(0, len(m15_df) - MAX_POI_AGE)
    for i in range(max(2, start), len(m15_df)):
        if bias == 'bearish':
            gh = m15_df['High'].iloc[i]
            gl = m15_df['Low'].iloc[i-2]
            if gh < gl and (gl - gh) >= FVG_MIN_SIZE:
                fvgs.append({'top':gl,'bottom':gh,'mid':(gl+gh)/2,'type':'FVG_BEAR'})
        elif bias == 'bullish':
            gl = m15_df['Low'].iloc[i]
            gh = m15_df['High'].iloc[i-2]
            if gl > gh and (gl - gh) >= FVG_MIN_SIZE:
                fvgs.append({'top':gl,'bottom':gh,'mid':(gl+gh)/2,'type':'FVG_BULL'})
    return fvgs[-6:]

def price_in_poi(price, poi):
    return (poi['bottom'] - POI_BUFFER) <= price <= (poi['top'] + POI_BUFFER)

def get_active_pois(m15_df, bias, price):
    fvgs = get_fvgs(m15_df, bias)
    if not fvgs: return []
    if bias == 'bearish':
        pois = [p for p in fvgs if price <= p['top'] + POI_PROX]
    else:
        pois = [p for p in fvgs if price >= p['bottom'] - POI_PROX]
    for p in pois:
        p['distance'] = abs(p['mid'] - price)
    pois.sort(key=lambda x: x['distance'])
    return pois[:3]

# ================================================================
#  CHOCH DETECTOR (M5)
# ================================================================

def detect_choch(m5_df, bias, poi):
    zone = [i for i in range(len(m5_df)-1) if price_in_poi(m5_df['Close'].iloc[i], poi)]
    if len(zone) < MIN_ZONE_BARS: return None
    zb = m5_df.iloc[zone[0]:].reset_index(drop=True)
    if len(zb) < 5: return None
    cc = zb.iloc[-2]
    if bias == 'bearish':
        hi_i = int(zb['High'].idxmax())
        if hi_i >= len(zb)-2: return None
        peak_low = zb['Low'].iloc[hi_i]
        if cc['Open'] > cc['Close'] and cc['Close'] < peak_low:
            log(f"CHoCH BEARISH | Zone high: {zb['High'].iloc[hi_i]:.2f} | Break: {cc['Close']:.2f}", "SIGNAL")
            return 'short'
    elif bias == 'bullish':
        lo_i = int(zb['Low'].idxmin())
        if lo_i >= len(zb)-2: return None
        trough_high = zb['High'].iloc[lo_i]
        if cc['Close'] > cc['Open'] and cc['Close'] > trough_high:
            log(f"CHoCH BULLISH | Zone low: {zb['Low'].iloc[lo_i]:.2f} | Break: {cc['Close']:.2f}", "SIGNAL")
            return 'long'
    return None

# ================================================================
#  SESSION & NEWS
# ================================================================

def in_session():
    now = datetime.now(timezone.utc)
    h = now.hour
    active = SESSION_START <= h < SESSION_END
    if active:
        return True, "London"
    return False, "outside session"

def get_news():
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        if r.status_code == 200:
            return [e for e in r.json()
                    if e.get('impact','').lower()=='high'
                    and e.get('country','') in NEWS_CURRENCIES]
    except Exception as ex:
        log(f"News fetch failed: {ex}", "WARN")
    return []

def news_clear(events):
    if not events: return True
    now = datetime.now(timezone.utc)
    for e in events:
        try:
            et = datetime.fromisoformat(e.get('date','')).astimezone(timezone.utc)
            diff = abs((now - et).total_seconds() / 60)
            if diff <= NEWS_BUFFER_MIN:
                log(f"NEWS BLOCK: {e.get('title','?')} in {diff:.0f} mins", "WARN")
                return False
        except Exception:
            continue
    return True

# ================================================================
#  POSITION SIZING
# ================================================================

def calc_position(entry, sl, direction, balance, sym_info):
    stop_dist = abs(entry - sl)
    if stop_dist < MIN_STOP or stop_dist > MAX_STOP:
        return None

    if direction == 'long':
        tp1 = entry + stop_dist * TP1_RR
        tp2 = entry + stop_dist * TP2_RR
    else:
        tp1 = entry - stop_dist * TP1_RR
        tp2 = entry - stop_dist * TP2_RR

    risk_amt  = balance * RISK_PCT
    pt_val    = sym_info.trade_contract_size * sym_info.point
    raw_lots  = risk_amt / (stop_dist * pt_val) if pt_val > 0 else risk_amt / stop_dist
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
        'stop_pts': round(stop_dist, 2),
        'rr_tp1':   TP1_RR,
        'rr_tp2':   TP2_RR,
        'digits':   sym_info.digits,
    }

# ================================================================
#  ORDER EXECUTION
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
        "magic":        20250004,
        "comment":      "Confluence v1.0",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Order failed: {mt5.last_error()} | {getattr(result,'comment','')}", "ERROR")
        return None
    log(f"ORDER PLACED | Ticket:{result.order} | {direction.upper()} | "
        f"${price:.2f} | SL:${pos['sl']:.2f} | TP:${pos['tp2']:.2f} | "
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
        "magic":        20250004,
        "comment":      "Confluence TP1",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Partial close failed: {mt5.last_error()}", "ERROR"); return False
    log(f"TP1 PARTIAL CLOSE | Lots:{lots} | Price:${price:.2f}", "TRADE")
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
    log(f"STOP → BREAKEVEN | New SL: ${new_sl:.2f}", "TRADE")
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
        "magic":        20250004,
        "comment":      f"Confluence {reason}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Full close failed: {mt5.last_error()}", "ERROR"); return False
    log(f"POSITION CLOSED ({reason}) | Price:${price:.2f}", "TRADE")
    return True

def position_alive(ticket):
    pos = mt5.positions_get(ticket=ticket)
    return pos is not None and len(pos) > 0

def get_lots(ticket):
    pos = mt5.positions_get(ticket=ticket)
    return pos[0].volume if pos else None

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
            log(f"New day: {today} | Balance: ${balance:.2f} | "
                f"Session: {SESSION_START}:00–{SESSION_END}:00 UTC")

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
    def __init__(self): self.reset()
    def reset(self):
        self.active=False; self.ticket=None; self.direction=None
        self.entry=None; self.sl=None; self.tp1=None; self.tp2=None
        self.lots=None; self.tp1_hit=False; self.risk_amt=None; self.pos=None
    def open(self, ticket, direction, pos):
        self.active=True; self.ticket=ticket; self.direction=direction
        self.entry=pos['entry']; self.sl=pos['sl']
        self.tp1=pos['tp1']; self.tp2=pos['tp2']
        self.lots=pos['lots']; self.risk_amt=pos['risk_amt']
        self.pos=pos; self.tp1_hit=False

# ================================================================
#  MAIN ENGINE
# ================================================================

def run():
    log("=" * 60)
    log("  CONFLUENCE ENGINE v1.0 — STARTING")
    log(f"  Symbol:    {SYMBOL} (XAU/USD Gold)")
    log(f"  Strategy:  London Session FVG + Dual TF Bias + CHoCH")
    log(f"  Session:   {SESSION_START}:00–{SESSION_END}:00 UTC ({SESSION_START+1}:00–{SESSION_END+1}:00 WAT)")
    log(f"  Risk:      {RISK_PCT*100:.1f}% per trade (DEMO MODE)")
    log("=" * 60)

    if not connect(): return

    sym_info = get_symbol_info()
    if sym_info is None:
        log(f"Cannot find {SYMBOL}. Run --find-symbols.", "ERROR"); return

    log(f"Symbol | Point: {sym_info.point} | Min lot: {sym_info.volume_min} | Digits: {sym_info.digits}")

    limits          = DailyLimits()
    trade           = Trade()
    news            = []
    last_news       = None
    last_m5_time    = None
    last_h4_time    = None
    last_m15_time   = None
    last_macro_date = None
    current_bias    = 'neutral'
    current_pois    = []

    log("Engine live. Scanning every 30 seconds...")
    log("Gold Macro Report will print at session start (07:00 UTC) each day.")

    while True:
        try:
            balance = get_balance()
            if balance is None:
                log("MT5 connection lost. Reconnecting...", "WARN")
                if not connect(): time.sleep(15); continue

            limits.reset_if_new_day(balance)

            # ── Daily Macro Report — print on startup and each new day ──
            now_utc    = datetime.now(timezone.utc)
            today_date = now_utc.date()

            if last_macro_date != today_date:
                last_macro_date = today_date
                try:
                    from macro_dashboard import run_dashboard
                    log("=" * 65)
                    log("  RUNNING DAILY GOLD MACRO REPORT")
                    log("=" * 65)
                    state = run_dashboard()
                    if state:
                        score = state.get('total_score', 0)
                        bias  = state.get('macro_bias', 'UNKNOWN')
                        rec   = state.get('recommendation', '')
                        probs = state.get('probabilities')
                        inds  = state.get('indicators', {})

                        log("=" * 65, "SIGNAL")
                        log("  GOLD MACRO REPORT — TODAY'S BRIEFING", "SIGNAL")
                        log("=" * 65, "SIGNAL")
                        log(f"  Date:              {today_date}", "SIGNAL")
                        log(f"  Gold Macro Score:  {score:+.2f} / ±10.0", "SIGNAL")
                        log(f"  Macro Bias:        {bias}", "SIGNAL")
                        log(f"  Recommendation:    {rec}", "SIGNAL")
                        log("  ---", "SIGNAL")

                        # Print each indicator
                        ind_names = {
                            'dxy':        'DXY (Dollar Index)',
                            'real_yield': 'Real Yield (10Y TIPS)',
                            'fed_policy': 'Fed Policy (FFR)',
                            'inflation':  'Inflation Expectations',
                            'vix':        'VIX (Fear Index)',
                            'gold_etf':   'Gold ETF Flows (GLD)',
                            'cot':        'COT Net Positioning',
                            'volume':     'Volume Confirmation',
                        }
                        for key, name in ind_names.items():
                            ind = inds.get(key, {})
                            val  = ind.get('value', 'N/A')
                            ibias = ind.get('bias', 'N/A')
                            sc   = ind.get('score', 0)
                            arrow = 'UP' if sc > 0 else ('DOWN' if sc < 0 else 'FLAT')
                            log(f"  {name:<28} {val:<18} {ibias} ({arrow})", "SIGNAL")

                        log("  ---", "SIGNAL")

                        if probs:
                            atr = state.get('atr')
                            log(f"  ATR (M5):          ${atr:.2f}" if atr else "  ATR: N/A", "SIGNAL")
                            log(f"  P(Stop Loss Hit):  {probs['p_sl']}%", "SIGNAL")
                            log(f"  P(TP1 Hit):        {probs['p_tp1']}%", "SIGNAL")
                            log(f"  P(TP2 Hit):        {probs['p_tp2']}%", "SIGNAL")
                            log(f"  P(Breakeven Exit): {probs['p_be']}%", "SIGNAL")
                            log(f"  Expected Edge:     {probs['edge_r']:+.3f}R per trade", "SIGNAL")

                        log("=" * 65, "SIGNAL")
                        beep(400,100); beep(600,100); beep(800,200)

                except Exception as e:
                    log(f"Macro report failed: {e} — continuing without it", "WARN")

            # Refresh news hourly
            now = datetime.now(timezone.utc)
            if last_news is None or (now - last_news).seconds > 3600:
                news = get_news(); last_news = now
                log(f"News refreshed: {len(news)} high-impact events this week")

            # Fetch all timeframes
            m5_df  = fetch(TF_M5,  CANDLES_M5)
            m15_df = fetch(TF_M15, CANDLES_M15)
            h1_df  = fetch(TF_H1,  CANDLES_H1)
            h4_df  = fetch(TF_H4,  CANDLES_H4)

            if any(x is None for x in [m5_df, m15_df, h1_df, h4_df]):
                log("Data fetch failed. Retrying...", "WARN")
                time.sleep(30); continue

            completed     = m5_df.iloc[:-1].copy()
            latest_m5_t   = completed['time'].iloc[-1]
            current_price = m5_df['Close'].iloc[-1]

            new_candle = latest_m5_t != last_m5_time
            if new_candle:
                last_m5_time = latest_m5_t
                log(f"Candle | {latest_m5_t} | Gold: ${current_price:.2f}")

            # Manage open position
            if trade.active:
                if not position_alive(trade.ticket):
                    log(f"Position {trade.ticket} closed by broker (SL/TP hit)", "TRADE")
                    trade.reset(); continue

                hi = completed['High'].iloc[-1]
                lo = completed['Low'].iloc[-1]
                d  = trade.direction
                act_sl = trade.entry if trade.tp1_hit else trade.sl

                if d == 'long':
                    if lo <= act_sl:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, d, "STOP")
                        res = "BREAKEVEN" if trade.tp1_hit else "STOP"
                        alert(f"{res} HIT | Long | Entry:${trade.entry:.2f} | Exit:${act_sl:.2f}")
                        trade.reset()
                    elif not trade.tp1_hit and hi >= trade.tp1:
                        half = round(trade.lots/2, 2)
                        sym  = get_symbol_info()
                        if half >= sym.volume_min:
                            if close_partial(trade.ticket, half, d):
                                modify_sl(trade.ticket, trade.entry, trade.tp2, trade.pos['digits'])
                                trade.tp1_hit = True
                                alert(f"TP1 HIT | Long | +{TP1_RR}R | Stop → Breakeven ${trade.entry:.2f}")
                                beep(600,150); beep(900,150); beep(1200,300)
                        else:
                            close_full(trade.ticket, trade.lots, d, "TP1_FULL")
                            trade.reset()
                    elif trade.tp1_hit and hi >= trade.tp2:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, d, "TP2")
                        alert(f"FULL WIN — TP2 HIT | Long | +{TP2_RR}R | Est. +${trade.risk_amt*TP2_RR:.2f}")
                        beep(500,100); beep(700,100); beep(900,100); beep(1200,400)
                        trade.reset()
                else:
                    if hi >= act_sl:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, d, "STOP")
                        res = "BREAKEVEN" if trade.tp1_hit else "STOP"
                        alert(f"{res} HIT | Short | Entry:${trade.entry:.2f} | Exit:${act_sl:.2f}")
                        trade.reset()
                    elif not trade.tp1_hit and lo <= trade.tp1:
                        half = round(trade.lots/2, 2)
                        sym  = get_symbol_info()
                        if half >= sym.volume_min:
                            if close_partial(trade.ticket, half, d):
                                modify_sl(trade.ticket, trade.entry, trade.tp2, trade.pos['digits'])
                                trade.tp1_hit = True
                                alert(f"TP1 HIT | Short | +{TP1_RR}R | Stop → Breakeven ${trade.entry:.2f}")
                                beep(600,150); beep(900,150); beep(1200,300)
                        else:
                            close_full(trade.ticket, trade.lots, d, "TP1_FULL")
                            trade.reset()
                    elif trade.tp1_hit and lo <= trade.tp2:
                        lots = get_lots(trade.ticket)
                        if lots: close_full(trade.ticket, lots, d, "TP2")
                        alert(f"FULL WIN — TP2 HIT | Short | +{TP2_RR}R | Est. +${trade.risk_amt*TP2_RR:.2f}")
                        beep(500,100); beep(700,100); beep(900,100); beep(1200,400)
                        trade.reset()

                time.sleep(30); continue

            # Update bias on new H4 candle
            latest_h4_t = h4_df['time'].iloc[-2]
            if latest_h4_t != last_h4_time:
                last_h4_time  = latest_h4_t
                current_bias  = get_dual_bias(h4_df.iloc[:-1], h1_df.iloc[:-1])
                log(f"Bias UPDATE | H4+H1 aligned: {current_bias.upper()} | Gold: ${current_price:.2f}")

            # Update POIs on new M15 candle
            latest_m15_t = m15_df['time'].iloc[-2]
            if latest_m15_t != last_m15_time and current_bias != 'neutral':
                last_m15_time = latest_m15_t
                current_pois  = get_active_pois(m15_df.iloc[:-1], current_bias, current_price)
                if current_pois:
                    log(f"M15 POIs | {current_bias.upper()} | {len(current_pois)} zones | "
                        f"Nearest: {current_pois[0]['type']} @ ${current_pois[0]['mid']:.2f} "
                        f"(${current_pois[0]['distance']:.1f} away)")

            if not new_candle:
                time.sleep(30); continue

            log(f"M5 scan | Gold: ${current_price:.2f} | Bias: {current_bias.upper()} | "
                f"POIs: {len(current_pois)}")

            # Filters
            ok_session, session_name = in_session()
            if not ok_session: time.sleep(30); continue
            if current_bias == 'neutral': time.sleep(30); continue
            if not current_pois: time.sleep(30); continue
            if not news_clear(news): time.sleep(30); continue

            # Macro gate
            macro_ok, macro_msg = get_engine_gate('confluence')
            if not macro_ok:
                log(f"MACRO GATE: {macro_msg}", "WARN")
                time.sleep(30); continue
            else:
                log(f"Macro: {macro_msg}")

            can_trade, reason = limits.can_trade(balance)
            if not can_trade:
                log(f"Trade skipped: {reason}", "WARN")
                time.sleep(30); continue

            # ATR filter — skip low vol
            atr = completed['ATR'].iloc[-1]
            if pd.isna(atr) or atr < MIN_ATR:
                time.sleep(30); continue

            # CHoCH detection
            m5_slice = completed.tail(60)
            for poi in current_pois[:2]:
                direction = detect_choch(m5_slice, current_bias, poi)
                if direction is None: continue

                entry    = m5_df['Open'].iloc[-1]
                sym_info = get_symbol_info()
                sl = poi['bottom'] - atr*0.5 if direction=='long' else poi['top'] + atr*0.5
                pos = calc_position(entry, sl, direction, balance, sym_info)

                if pos is None:
                    log("Position calc invalid — skipping", "WARN"); continue

                log(f"\n{'='*55}", "SIGNAL")
                log(f"  CONFLUENCE ENTRY SIGNAL", "SIGNAL")
                log(f"{'='*55}", "SIGNAL")
                log(f"  Direction:    {direction.upper()}", "SIGNAL")
                log(f"  Session:      {session_name}", "SIGNAL")
                log(f"  Bias:         H4+H1 {current_bias.upper()}", "SIGNAL")
                log(f"  POI:          {poi['type']} @ ${poi['bottom']:.2f}–${poi['top']:.2f}", "SIGNAL")
                log(f"  Entry:        ${pos['entry']:.2f}", "SIGNAL")
                log(f"  Stop Loss:    ${pos['sl']:.2f}  (${pos['stop_pts']:.1f})", "SIGNAL")
                log(f"  Take Profit1: ${pos['tp1']:.2f}  ({pos['rr_tp1']}R)", "SIGNAL")
                log(f"  Take Profit2: ${pos['tp2']:.2f}  ({pos['rr_tp2']}R)", "SIGNAL")
                log(f"  Lot Size:     {pos['lots']}", "SIGNAL")
                log(f"  Risk $:       ${pos['risk_amt']:.2f}", "SIGNAL")
                log(f"{'='*55}", "SIGNAL")

                ticket = place_order(pos, direction)
                if ticket:
                    trade.open(ticket, direction, pos)
                    limits.record()
                    alert(f"ORDER PLACED | {direction.upper()} Gold | "
                          f"Entry:${pos['entry']:.2f} | SL:${pos['sl']:.2f} | "
                          f"TP1:${pos['tp1']:.2f} | TP2:${pos['tp2']:.2f}")
                break

        except KeyboardInterrupt:
            log("Engine stopped by user.")
            if trade.active:
                log(f"WARNING: Position {trade.ticket} may still be open.", "WARN")
            break
        except Exception as e:
            log(f"Unexpected error: {e}", "ERROR")
            time.sleep(30)

        time.sleep(30)

    mt5.shutdown()
    log("Confluence engine shut down cleanly.")

# ================================================================
#  UTILITIES
# ================================================================

def find_symbols():
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}"); return
    syms = mt5.symbols_get()
    print("\nGold symbols on your MT5:")
    print("-" * 40)
    for s in syms:
        if any(x in s.name.upper() for x in ['XAU','GOLD']):
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
        h4 = fetch(TF_H4, CANDLES_H4)
        h1 = fetch(TF_H1, CANDLES_H1)
        tick = mt5.symbol_info_tick(SYMBOL)
        now  = datetime.now(timezone.utc)
        ok_session, session = in_session()
        bias = get_dual_bias(h4.iloc[:-1], h1.iloc[:-1]) if h4 is not None and h1 is not None else 'N/A'
        print(f"\nConfluence Status Check")
        print(f"  Symbol:   {SYMBOL}")
        print(f"  Price:    ${tick.ask:.2f}" if tick else "  Price:   N/A")
        print(f"  Bias:     {bias.upper()} (H4+H1 aligned)")
        print(f"  UTC Time: {now.strftime('%H:%M')} UTC")
        print(f"  Session:  {'ACTIVE — ' + session if ok_session else 'INACTIVE (07:00–12:00 UTC)'}")
        mt5.shutdown()

    else:
        run()
