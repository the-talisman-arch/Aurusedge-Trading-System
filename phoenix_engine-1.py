"""
╔══════════════════════════════════════════════════════════════╗
║         PHOENIX — AUTO EXECUTION ENGINE v2.0                ║
║         CRASH 1000 Post-Spike Recovery Strategy             ║
║         Built by AurusEdge | Talisman Systems               ║
╚══════════════════════════════════════════════════════════════╝

REQUIREMENTS:
  pip install MetaTrader5 pandas numpy

SETUP:
  1. Open Deriv MT5 on your PC and log into DEMO account
  2. Run: python phoenix_engine.py --find-symbols
  3. Confirm SYMBOL below matches your MT5 exactly
  4. Run: python phoenix_engine.py

WHAT IT DOES AUTOMATICALLY:
  - Detects crash spikes on Crash 1000 every M5 candle
  - Waits for bullish confirmation candle
  - Places BUY order at market automatically
  - Sets Stop Loss and Take Profit on MT5
  - Closes 50% at TP1 and moves stop to breakeven
  - Closes remaining 50% at TP2
  - Time exits if TP1 not hit in 30 candles
  - Logs everything to phoenix_log.txt

SAFETY:
  - DEMO ACCOUNT ONLY for 4 weeks
  - Max 2 trades per day
  - Max 2% daily drawdown before engine pauses
  - Risk fixed at 0.1% per trade (safe for demo validation)
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import winsound
from datetime import datetime

# ================================================================
#  CONFIGURATION
# ================================================================

SYMBOL          = "Crash 1000 Index"
TIMEFRAME       = mt5.TIMEFRAME_M5
CANDLES_NEEDED  = 500

# Strategy parameters
SPIKE_SIGMA     = 4.0
SPIKE_BODY_ATR  = 2.0
ROLLING_WINDOW  = 50
ATR_PERIOD      = 14
TIME_EXIT_BARS  = 30
MAX_TRADES_DAY  = 2
MAX_DAY_DD_PCT  = 0.02
RISK_PCT        = 0.001          # 0.1% risk — safe for demo validation
                                  # Change to 0.01 after 4 weeks proven

LOG_FILE        = "phoenix_log.txt"
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
    beep(1000, 500)
    beep(1200, 300)

# ================================================================
#  MT5 CONNECTION
# ================================================================

def connect():
    if not mt5.initialize():
        log(f"MT5 init failed: {mt5.last_error()}", "ERROR")
        return False
    info = mt5.account_info()
    if info is None:
        log("Cannot fetch account info. Is MT5 logged in?", "ERROR")
        return False
    log(f"Connected | Account: {info.login} | Balance: ${info.balance:.2f} | Server: {info.server}")
    if info.trade_mode != 0:
        log("WARNING: This does not appear to be a DEMO account. Stopping for safety.", "ERROR")
        return False
    return True

def get_balance():
    info = mt5.account_info()
    return info.balance if info else None

def get_symbol_info():
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        log(f"Cannot get symbol info for {SYMBOL}", "ERROR")
        return None
    return info

# ================================================================
#  DATA
# ================================================================

def fetch_candles():
    mt5.symbol_select(SYMBOL, True)
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, CANDLES_NEEDED)
    if rates is None or len(rates) == 0:
        log(f"No candle data. MT5 error: {mt5.last_error()}", "WARN")
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df.rename(columns={'open':'Open','high':'High','low':'Low',
                        'close':'Close','tick_volume':'Volume'}, inplace=True)
    return df

# ================================================================
#  INDICATORS
# ================================================================

def add_indicators(df):
    df = df.copy()
    df['lret']      = np.log(df['Close'] / df['Close'].shift(1))
    df['TR']        = np.maximum(df['High']-df['Low'],
                      np.maximum(abs(df['High']-df['Close'].shift(1)),
                                 abs(df['Low']-df['Close'].shift(1))))
    df['ATR']       = df['TR'].rolling(ATR_PERIOD).mean()
    df['roll_mean'] = df['lret'].rolling(ROLLING_WINDOW).mean()
    df['roll_std']  = df['lret'].rolling(ROLLING_WINDOW).std()
    df['body']      = df['Open'] - df['Close']
    df['is_crash']  = (
        (df['lret'] < df['roll_mean'] - SPIKE_SIGMA * df['roll_std']) &
        (df['body'] > SPIKE_BODY_ATR * df['ATR'])
    )
    return df.dropna().reset_index(drop=True)

# ================================================================
#  POSITION CALCULATOR
# ================================================================

def calculate_position(entry, crash_low, crash_open, atr, balance, sym_info):
    sl = crash_low - 0.5 * atr
    sd = entry - sl

    if sd <= 0 or sd > 200:
        return None

    recovery_dist = crash_open - entry
    if recovery_dist <= 0:
        return None

    tp1 = entry + recovery_dist * 0.5
    tp2 = crash_open

    if (tp1 - entry) / sd < 0.5:
        return None

    risk_amount = balance * RISK_PCT
    raw_lots    = risk_amount / (sd * sym_info.trade_contract_size)
    lot_min     = sym_info.volume_min
    lot_max     = sym_info.volume_max
    lot_step    = sym_info.volume_step

    # Round to nearest valid lot step
    lots = max(lot_min, min(lot_max, round(raw_lots / lot_step) * lot_step))
    lots = round(lots, 2)

    point = sym_info.point
    digits = sym_info.digits

    return {
        'entry':       round(entry, digits),
        'stop_loss':   round(sl, digits),
        'take_profit1':round(tp1, digits),
        'take_profit2':round(tp2, digits),
        'stop_dist':   round(sd, digits),
        'risk_amount': round(risk_amount, 2),
        'lot_size':    lots,
        'rr_tp1':      round((tp1 - entry) / sd, 2),
        'rr_tp2':      round((tp2 - entry) / sd, 2),
        'point':       point,
        'digits':      digits,
    }

# ================================================================
#  MT5 ORDER EXECUTION
# ================================================================

def place_buy_order(pos):
    """Place a market buy order with SL and TP2."""
    sym_info = mt5.symbol_info(SYMBOL)
    if sym_info is None:
        log("Cannot get symbol info for order placement", "ERROR")
        return None

    price = mt5.symbol_info_tick(SYMBOL).ask
    if price is None:
        log("Cannot get current ask price", "ERROR")
        return None

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    SYMBOL,
        "volume":    pos['lot_size'],
        "type":      mt5.ORDER_TYPE_BUY,
        "price":     price,
        "sl":        pos['stop_loss'],
        "tp":        pos['take_profit2'],
        "deviation": 20,
        "magic":     20250001,
        "comment":   "Phoenix v2.0",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None:
        log(f"Order send failed: {mt5.last_error()}", "ERROR")
        return None

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Order rejected: {result.retcode} — {result.comment}", "ERROR")
        return None

    log(f"BUY ORDER PLACED | Ticket: {result.order} | "
        f"Lots: {pos['lot_size']} | Price: {price:.3f} | "
        f"SL: {pos['stop_loss']:.3f} | TP: {pos['take_profit2']:.3f}", "TRADE")
    return result.order

def close_partial(ticket, lots_to_close, pos):
    """Close a portion of an open position."""
    price = mt5.symbol_info_tick(SYMBOL).bid
    if price is None:
        log("Cannot get bid price for partial close", "ERROR")
        return False

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    SYMBOL,
        "volume":    lots_to_close,
        "type":      mt5.ORDER_TYPE_SELL,
        "position":  ticket,
        "price":     price,
        "deviation": 20,
        "magic":     20250001,
        "comment":   "Phoenix TP1 partial close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Partial close failed: {mt5.last_error()}", "ERROR")
        return False

    log(f"PARTIAL CLOSE | Lots: {lots_to_close} | Price: {price:.3f}", "TRADE")
    return True

def modify_sl(ticket, new_sl, tp, pos):
    """Move stop loss to breakeven after TP1."""
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   SYMBOL,
        "position": ticket,
        "sl":       round(new_sl, pos['digits']),
        "tp":       round(tp, pos['digits']),
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"SL modify failed: {mt5.last_error()}", "ERROR")
        return False
    log(f"STOP MOVED TO BREAKEVEN | New SL: {new_sl:.3f}", "TRADE")
    return True

def close_full_position(ticket, lots):
    """Close entire remaining position."""
    price = mt5.symbol_info_tick(SYMBOL).bid
    if price is None:
        log("Cannot get bid price for full close", "ERROR")
        return False

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    SYMBOL,
        "volume":    lots,
        "type":      mt5.ORDER_TYPE_SELL,
        "position":  ticket,
        "price":     price,
        "deviation": 20,
        "magic":     20250001,
        "comment":   "Phoenix time/manual exit",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"Full close failed: {mt5.last_error()}", "ERROR")
        return False

    log(f"POSITION CLOSED | Price: {price:.3f}", "TRADE")
    return True

def check_position_alive(ticket):
    """Check if a position is still open."""
    positions = mt5.positions_get(ticket=ticket)
    return positions is not None and len(positions) > 0

def get_position_lots(ticket):
    """Get current lots of open position."""
    positions = mt5.positions_get(ticket=ticket)
    if positions and len(positions) > 0:
        return positions[0].volume
    return None

# ================================================================
#  DAILY LIMITS
# ================================================================

class DailyLimits:
    def __init__(self):
        self.date         = None
        self.trades_taken = 0
        self.start_bal    = None

    def reset_if_new_day(self, balance):
        today = datetime.now().date()
        if self.date != today:
            self.date         = today
            self.trades_taken = 0
            self.start_bal    = balance
            log(f"New day: {today} | Start balance: ${balance:.2f}")

    def can_trade(self, balance):
        if self.trades_taken >= MAX_TRADES_DAY:
            return False, f"Daily limit: {MAX_TRADES_DAY} trades taken"
        if self.start_bal and balance < self.start_bal * (1 - MAX_DAY_DD_PCT):
            return False, f"Daily drawdown limit hit ({MAX_DAY_DD_PCT*100:.0f}%)"
        return True, "OK"

    def record_trade(self):
        self.trades_taken += 1

# ================================================================
#  TRADE STATE
# ================================================================

class TradeState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active     = False
        self.ticket     = None
        self.entry      = None
        self.sl         = None
        self.tp1        = None
        self.tp2        = None
        self.lots       = None
        self.tp1_hit    = False
        self.entry_bar  = None
        self.risk_amt   = None
        self.pos        = None

    def open(self, ticket, pos, bar_idx):
        self.active    = True
        self.ticket    = ticket
        self.entry     = pos['entry']
        self.sl        = pos['stop_loss']
        self.tp1       = pos['take_profit1']
        self.tp2       = pos['take_profit2']
        self.lots      = pos['lot_size']
        self.tp1_hit   = False
        self.entry_bar = bar_idx
        self.risk_amt  = pos['risk_amount']
        self.pos       = pos

    def bars_in(self, current_bar):
        return current_bar - self.entry_bar if self.entry_bar else 0

# ================================================================
#  MAIN ENGINE
# ================================================================

def run():
    log("=" * 58)
    log("  PHOENIX AUTO-EXECUTION ENGINE v2.0 — STARTING")
    log(f"  Symbol:    {SYMBOL}")
    log(f"  Timeframe: M5")
    log(f"  Risk:      {RISK_PCT*100:.1f}% per trade (DEMO SAFE MODE)")
    log(f"  Max trades/day: {MAX_TRADES_DAY}")
    log("=" * 58)

    if not connect():
        return

    sym_info = get_symbol_info()
    if sym_info is None:
        return

    log(f"Symbol info | Contract size: {sym_info.trade_contract_size} | "
        f"Min lot: {sym_info.volume_min} | Step: {sym_info.volume_step}")

    limits      = DailyLimits()
    trade       = TradeState()
    state       = 'watching'
    crash_idx   = None
    crash_low   = None
    crash_open  = None
    last_bar_t  = None
    bar_count   = 0

    log("Engine live. Scanning for crash spikes every 30 seconds...")

    while True:
        try:
            balance = get_balance()
            if balance is None:
                log("MT5 connection lost. Reconnecting...", "WARN")
                if not connect(): time.sleep(15); continue

            limits.reset_if_new_day(balance)

            df = fetch_candles()
            if df is None: time.sleep(30); continue

            df = add_indicators(df)
            if len(df) < ROLLING_WINDOW + ATR_PERIOD + 5:
                log(f"Building indicator history ({len(df)} candles)...", "WARN")
                time.sleep(30); continue

            latest_bar_t = df['time'].iloc[-2]
            if latest_bar_t == last_bar_t:
                time.sleep(30); continue

            last_bar_t = latest_bar_t
            bar_count += 1

            completed = df.iloc[:-1].copy()
            current   = completed.iloc[-1]

            log(f"Candle [{bar_count}] {current['time']} | "
                f"O:{current['Open']:.3f} H:{current['High']:.3f} "
                f"L:{current['Low']:.3f} C:{current['Close']:.3f}")

            # ── Manage open position ──────────────────────────────────────────
            if trade.active:
                # Verify position still exists on MT5
                if not check_position_alive(trade.ticket):
                    log(f"Position {trade.ticket} closed externally (SL/TP hit by broker).", "TRADE")
                    trade.reset(); state = 'watching'; continue

                hi = current['High']; lo = current['Low']

                # Time exit — close full position if TP1 not hit in time
                if trade.bars_in(bar_count) >= TIME_EXIT_BARS and not trade.tp1_hit:
                    log(f"TIME EXIT triggered after {TIME_EXIT_BARS} bars", "TRADE")
                    current_lots = get_position_lots(trade.ticket)
                    if current_lots:
                        close_full_position(trade.ticket, current_lots)
                    alert("TIME EXIT — Position closed after 30 bars without TP1")
                    trade.reset(); state = 'watching'; continue

                # TP1 hit — close 50%, move stop to breakeven
                if not trade.tp1_hit and hi >= trade.tp1:
                    half_lots = round(trade.lots / 2, 2)
                    if half_lots >= sym_info.volume_min:
                        closed = close_partial(trade.ticket, half_lots, trade.pos)
                        if closed:
                            modify_sl(trade.ticket, trade.entry, trade.tp2, trade.pos)
                            trade.tp1_hit = True
                            alert(f"TP1 HIT at {trade.tp1:.3f} | "
                                  f"Closed 50% | Stop moved to breakeven {trade.entry:.3f}")
                            beep(800, 200); beep(1000, 200); beep(1200, 300)
                    else:
                        # Lot too small to split — close full at TP1
                        close_full_position(trade.ticket, trade.lots)
                        alert(f"TP1 HIT — Full close (lot too small to split) at {trade.tp1:.3f}")
                        trade.reset(); state = 'watching'; continue

                # TP2 hit — close remaining 50%
                if trade.tp1_hit and hi >= trade.tp2:
                    current_lots = get_position_lots(trade.ticket)
                    if current_lots:
                        close_full_position(trade.ticket, current_lots)
                    alert(f"FULL WIN — TP2 HIT at {trade.tp2:.3f} | "
                          f"Est. +{trade.risk_amt * trade.pos['rr_tp2']:.2f}")
                    beep(600,150); beep(800,150); beep(1000,150); beep(1200,300)
                    trade.reset(); state = 'watching'; continue

                continue   # still in trade, skip signal detection

            # ── Signal detection state machine ───────────────────────────────
            if state == 'watching':
                if current['is_crash']:
                    state      = 'saw_crash'
                    crash_idx  = bar_count
                    crash_low  = current['Low']
                    crash_open = current['Open']
                    sigma = current['lret'] / current['roll_std'] if current['roll_std'] != 0 else 0
                    log(f"CRASH SPIKE | Open:{crash_open:.3f} Low:{crash_low:.3f} "
                        f"({sigma:.1f}sigma) | Waiting for confirmation...", "SIGNAL")
                    beep(400, 200)

            elif state == 'saw_crash':
                if bar_count - crash_idx > 3:
                    log("Crash signal expired (no confirmation in 3 bars). Resetting.")
                    state = 'watching'

                elif current['is_crash']:
                    crash_idx = bar_count
                    crash_low = min(crash_low, current['Low'])
                    crash_open = current['Open']
                    log(f"SECONDARY CRASH | New low: {crash_low:.3f}", "SIGNAL")

                elif current['Close'] > current['Open']:
                    state = 'confirmed'
                    log(f"CONFIRMATION BULLISH | Close:{current['Close']:.3f} | "
                        f"Placing order on next candle...", "SIGNAL")
                    beep(600, 200); beep(800, 200)

                else:
                    log("Confirmation candle bearish — waiting...")

            elif state == 'confirmed':
                can_trade, reason = limits.can_trade(balance)
                if not can_trade:
                    log(f"Trade skipped: {reason}", "WARN")
                    state = 'watching'; continue

                entry_px = current['Open']
                sym_info = get_symbol_info()
                pos = calculate_position(
                    entry=entry_px, crash_low=crash_low,
                    crash_open=crash_open, atr=current['ATR'],
                    balance=balance, sym_info=sym_info
                )

                if pos is None:
                    log("Position calc failed — invalid levels. Skipping.", "WARN")
                    state = 'watching'; continue

                # Log the signal
                log(f"\n{'='*52}", "SIGNAL")
                log(f"  PHOENIX AUTO-ENTRY", "SIGNAL")
                log(f"{'='*52}", "SIGNAL")
                log(f"  Direction:     LONG (Buy)", "SIGNAL")
                log(f"  Entry:         {pos['entry']:.3f}", "SIGNAL")
                log(f"  Stop Loss:     {pos['stop_loss']:.3f}", "SIGNAL")
                log(f"  Take Profit 1: {pos['take_profit1']:.3f} ({pos['rr_tp1']:.1f}R)", "SIGNAL")
                log(f"  Take Profit 2: {pos['take_profit2']:.3f} ({pos['rr_tp2']:.1f}R)", "SIGNAL")
                log(f"  Lot Size:      {pos['lot_size']}", "SIGNAL")
                log(f"  Risk $:        ${pos['risk_amount']:.2f}", "SIGNAL")
                log(f"{'='*52}", "SIGNAL")

                # Place the order
                ticket = place_buy_order(pos)

                if ticket:
                    trade.open(ticket, pos, bar_count)
                    limits.record_trade()
                    alert(f"ORDER PLACED | Ticket:{ticket} | "
                          f"Entry:{pos['entry']:.3f} | SL:{pos['stop_loss']:.3f} | "
                          f"TP1:{pos['take_profit1']:.3f} | TP2:{pos['take_profit2']:.3f}")
                else:
                    log("Order placement failed. Skipping trade.", "ERROR")

                state = 'watching'

        except KeyboardInterrupt:
            log("Engine stopped by user.")
            if trade.active:
                log(f"WARNING: Position {trade.ticket} may still be open on MT5. Check manually.", "WARN")
            break
        except Exception as e:
            log(f"Unexpected error: {e}", "ERROR")
            time.sleep(30)

        time.sleep(30)

    mt5.shutdown()
    log("Engine shut down cleanly.")

# ================================================================
#  SYMBOL FINDER
# ================================================================

def find_crash_symbols():
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        return
    symbols = mt5.symbols_get()
    if symbols is None:
        print("Could not fetch symbols.")
        mt5.shutdown()
        return
    print("\nCRASH/BOOM symbols on your MT5:")
    print("-" * 40)
    for s in symbols:
        if 'crash' in s.name.lower() or 'boom' in s.name.lower():
            print(f"  {s.name}")
    mt5.shutdown()

# ================================================================
#  ENTRY POINT
# ================================================================

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--find-symbols":
        find_crash_symbols()
    else:
        run()
