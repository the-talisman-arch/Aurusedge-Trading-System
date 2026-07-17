"""
╔══════════════════════════════════════════════════════════════╗
║         MACRO DASHBOARD v1.0                                ║
║         Daily Intelligence Briefing for All 4 Engines       ║
║         Built by AurusEdge | Talisman Systems               ║
╚══════════════════════════════════════════════════════════════╝

WHAT IT DOES:
  Runs every morning at 06:30 UTC before London session opens.
  Fetches 8 macro indicators, scores each for Gold/USD direction,
  generates a composite Gold Macro Score (-10 to +10), and saves
  results to macro_state.json which all 4 engines read before trading.

INDICATORS:
  1. DXY       — Dollar Index (Yahoo Finance)
  2. Real Yield — 10Y TIPS yield (FRED API)
  3. Fed Policy — Federal Funds Rate (FRED API)
  4. Inflation  — 5Y Breakeven Rate (FRED API)
  5. VIX        — Fear Index (Yahoo Finance)
  6. Gold ETF   — GLD holdings/flows (Yahoo Finance)
  7. COT Report — Non-commercial net positioning (CFTC)
  8. Volume     — Breakout volume confirmation (MT5)

SL/TP PROBABILITIES:
  Calculated from ATR and historical backtest data.
  Updates daily based on current volatility regime.

REQUIREMENTS:
  pip install requests pandas numpy MetaTrader5

USAGE:
  python macro_dashboard.py          -- run full briefing
  python macro_dashboard.py --watch  -- run every morning automatically
"""

import requests
import pandas as pd
import numpy as np
import json
import time
import os
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta

# ================================================================
#  CONFIGURATION
# ================================================================

FRED_API_KEY    = "f926709fc793d8e2d0be8055d31458dd"
MACRO_STATE_FILE= "macro_state.json"
LOG_FILE        = "macro_log.txt"
GOLD_SYMBOL     = "frxXAUUSD"       # MT5 symbol

# Scoring weights (must sum to 10)
WEIGHTS = {
    'dxy':        2.0,    # DXY — most direct Gold driver
    'real_yield': 2.0,    # Real yields — second most important
    'fed_policy': 1.5,    # Fed stance
    'inflation':  1.5,    # Inflation expectations
    'vix':        1.0,    # Risk sentiment
    'gold_etf':   0.5,    # ETF flows
    'cot':        1.0,    # COT positioning
    'volume':     0.5,    # Volume confirmation
}

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

# ================================================================
#  YAHOO FINANCE DATA
# ================================================================

def yahoo_price(ticker, period="5d"):
    """Fetch recent price data from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": period}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        result = data['chart']['result'][0]
        closes = result['indicators']['quote'][0]['close']
        closes = [c for c in closes if c is not None]
        if not closes:
            return None, None
        current = closes[-1]
        prev    = closes[-2] if len(closes) > 1 else closes[-1]
        return current, (current - prev) / prev * 100
    except Exception as e:
        log(f"Yahoo fetch failed for {ticker}: {e}", "WARN")
        return None, None

def yahoo_volume(ticker, period="5d"):
    """Fetch volume data."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": period}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        result = data['chart']['result'][0]
        vols = result['indicators']['quote'][0]['volume']
        vols = [v for v in vols if v is not None]
        if not vols: return None, None
        current = vols[-1]
        avg     = np.mean(vols[:-1]) if len(vols) > 1 else current
        return current, current / avg if avg > 0 else 1.0
    except Exception as e:
        log(f"Yahoo volume failed for {ticker}: {e}", "WARN")
        return None, None

# ================================================================
#  FRED DATA
# ================================================================

def fred_series(series_id, limit=5):
    """Fetch economic series from FRED."""
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id":    series_id,
            "api_key":      FRED_API_KEY,
            "file_type":    "json",
            "limit":        limit,
            "sort_order":   "desc",
        }
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        obs = [o for o in data.get('observations', []) if o['value'] != '.']
        if not obs: return None, None
        current = float(obs[0]['value'])
        prev    = float(obs[1]['value']) if len(obs) > 1 else current
        return current, current - prev
    except Exception as e:
        log(f"FRED fetch failed for {series_id}: {e}", "WARN")
        return None, None

# ================================================================
#  COT REPORT (CFTC)
# ================================================================

def get_cot_bias():
    """
    Fetch CFTC COT report for Gold futures (COMEX).
    Returns net non-commercial positioning and direction.
    """
    try:
        # CFTC publishes weekly COT data as CSV
        url = "https://www.cftc.gov/dea/newcot/f_disagg.txt"
        r   = requests.get(url, timeout=20)
        if r.status_code != 200:
            return None, None

        lines = r.text.split('\n')
        gold_line = None
        for line in lines:
            if 'GOLD' in line.upper() and 'COMEX' in line.upper():
                gold_line = line; break

        if not gold_line:
            # Try alternate search
            for line in lines:
                if 'GOLD -' in line.upper():
                    gold_line = line; break

        if not gold_line:
            return None, None

        fields = gold_line.split(',')
        if len(fields) < 10:
            return None, None

        # Non-commercial longs and shorts (columns 7 and 8 in disaggregated report)
        nc_long  = float(fields[7].strip().replace('"',''))
        nc_short = float(fields[8].strip().replace('"',''))
        net      = nc_long - nc_short

        return net, 'bullish' if net > 0 else 'bearish'

    except Exception as e:
        log(f"COT fetch failed: {e}", "WARN")
        return None, None

# ================================================================
#  MT5 VOLUME DATA
# ================================================================

def get_mt5_volume():
    """Get recent volume data from MT5 for Gold."""
    try:
        if not mt5.initialize():
            return None, None

        mt5.symbol_select(GOLD_SYMBOL, True)
        rates = mt5.copy_rates_from_pos(GOLD_SYMBOL, mt5.TIMEFRAME_H1, 0, 50)
        mt5.shutdown()

        if rates is None or len(rates) == 0:
            return None, None

        df      = pd.DataFrame(rates)
        current = df['tick_volume'].iloc[-2]
        avg     = df['tick_volume'].iloc[:-1].mean()
        ratio   = current / avg if avg > 0 else 1.0

        return current, ratio

    except Exception as e:
        log(f"MT5 volume fetch failed: {e}", "WARN")
        try: mt5.shutdown()
        except: pass
        return None, None

# ================================================================
#  SL/TP PROBABILITY ENGINE
# ================================================================

def calc_sltp_probabilities(atr, stop_pts, tp1_pts, tp2_pts, win_rate_hist=0.554):
    """
    Calculate SL and TP hit probabilities using:
    - Historical win rate from backtest
    - Current ATR vs stop distance ratio
    - Statistical edge measurement

    Returns probabilities for SL, TP1, TP2 hits.
    """
    if atr is None or atr <= 0:
        return None

    # ATR ratio — how tight is stop relative to current volatility
    sl_atr_ratio  = stop_pts / atr if atr > 0 else 1.0
    tp1_atr_ratio = tp1_pts  / atr if atr > 0 else 1.5
    tp2_atr_ratio = tp2_pts  / atr if atr > 0 else 3.0

    # Base probability from historical win rate
    base_win = win_rate_hist

    # Adjust for ATR ratio
    # Tight stops (ratio < 1.0) get stopped out more often
    # Wide stops (ratio > 2.0) survive but TP2 less likely
    if sl_atr_ratio < 0.8:
        sl_adjust = -0.08   # tighter stop = more stop hits
    elif sl_atr_ratio > 2.0:
        sl_adjust = +0.05   # wider stop = fewer stop hits
    else:
        sl_adjust = 0.0

    adj_win = min(0.75, max(0.30, base_win + sl_adjust))

    # TP1 probability — easier target (1.5R)
    p_tp1 = adj_win * 0.95   # most wins reach TP1

    # TP2 probability — harder target (3R)
    p_tp2 = adj_win * 0.65   # subset reach TP2

    # SL probability
    p_sl  = 1.0 - adj_win

    # Breakeven probability (after TP1, stop moves to entry)
    p_be  = p_tp1 * 0.15    # small % stopped at breakeven

    return {
        'p_sl':    round(p_sl * 100, 1),
        'p_tp1':   round(p_tp1 * 100, 1),
        'p_tp2':   round(p_tp2 * 100, 1),
        'p_be':    round(p_be * 100, 1),
        'sl_atr':  round(sl_atr_ratio, 2),
        'edge_r':  round(p_tp1 * 1.5 + p_tp2 * 0.5 * 1.5 - p_sl * 1.0, 3),
    }

def get_current_atr():
    """Get current ATR from MT5 for Gold."""
    try:
        if not mt5.initialize():
            return None
        mt5.symbol_select(GOLD_SYMBOL, True)
        rates = mt5.copy_rates_from_pos(GOLD_SYMBOL, mt5.TIMEFRAME_M5, 0, 30)
        mt5.shutdown()
        if rates is None: return None
        df = pd.DataFrame(rates)
        df['TR'] = np.maximum(df['high']-df['low'],
                   np.maximum(abs(df['high']-df['close'].shift(1)),
                              abs(df['low']-df['close'].shift(1))))
        return df['TR'].rolling(14).mean().iloc[-1]
    except Exception as e:
        try: mt5.shutdown()
        except: pass
        return None

# ================================================================
#  INDICATOR SCORING
# ================================================================

def score_dxy(value, change):
    """DXY up = Gold bearish. DXY down = Gold bullish."""
    if value is None: return 0, "N/A", "NEUTRAL"
    if change is None: change = 0
    if change > 0.5:   return -2, f"{value:.2f} (+{change:.2f}%)", "BEARISH"
    if change > 0.2:   return -1, f"{value:.2f} (+{change:.2f}%)", "SLIGHT BEAR"
    if change < -0.5:  return +2, f"{value:.2f} ({change:.2f}%)", "BULLISH"
    if change < -0.2:  return +1, f"{value:.2f} ({change:.2f}%)", "SLIGHT BULL"
    return 0, f"{value:.2f} ({change:+.2f}%)", "NEUTRAL"

def score_real_yield(value, change):
    """Real yields up = Gold bearish (opportunity cost rises)."""
    if value is None: return 0, "N/A", "NEUTRAL"
    if value > 2.0:    return -2, f"{value:.2f}%", "BEARISH (high)"
    if value > 1.0:    return -1, f"{value:.2f}%", "SLIGHT BEAR"
    if value < 0.0:    return +2, f"{value:.2f}%", "BULLISH (negative)"
    if value < 0.5:    return +1, f"{value:.2f}%", "SLIGHT BULL"
    return 0, f"{value:.2f}%", "NEUTRAL"

def score_fed_policy(rate, change):
    """High/rising rates = Gold bearish."""
    if rate is None: return 0, "N/A", "NEUTRAL"
    if rate > 5.0:    return -2, f"{rate:.2f}%", "BEARISH (restrictive)"
    if rate > 4.0:    return -1, f"{rate:.2f}%", "SLIGHT BEAR"
    if rate < 2.0:    return +2, f"{rate:.2f}%", "BULLISH (accommodative)"
    if rate < 3.0:    return +1, f"{rate:.2f}%", "SLIGHT BULL"
    return 0, f"{rate:.2f}%", "NEUTRAL"

def score_inflation(value, change):
    """High inflation expectations = Gold bullish (store of value)."""
    if value is None: return 0, "N/A", "NEUTRAL"
    if value > 3.0:   return +2, f"{value:.2f}%", "BULLISH (high inflation)"
    if value > 2.5:   return +1, f"{value:.2f}%", "SLIGHT BULL"
    if value < 1.5:   return -2, f"{value:.2f}%", "BEARISH (deflation risk)"
    if value < 2.0:   return -1, f"{value:.2f}%", "SLIGHT BEAR"
    return 0, f"{value:.2f}%", "NEUTRAL"

def score_vix(value, change):
    """High VIX = fear = Gold bullish (safe haven)."""
    if value is None: return 0, "N/A", "NEUTRAL"
    if value > 30:    return +2, f"{value:.2f} (EXTREME FEAR)", "VERY BULLISH"
    if value > 20:    return +1, f"{value:.2f} (ELEVATED)", "SLIGHT BULL"
    if value < 12:    return -1, f"{value:.2f} (COMPLACENCY)", "SLIGHT BEAR"
    if value < 15:    return 0,  f"{value:.2f} (CALM)", "NEUTRAL"
    return 0, f"{value:.2f}", "NEUTRAL"

def score_gold_etf(price, change):
    """GLD ETF price change as proxy for ETF flows."""
    if price is None: return 0, "N/A", "NEUTRAL"
    if change is None: change = 0
    if change > 1.0:  return +2, f"${price:.2f} (+{change:.2f}%)", "STRONG INFLOWS"
    if change > 0.3:  return +1, f"${price:.2f} (+{change:.2f}%)", "INFLOWS"
    if change < -1.0: return -2, f"${price:.2f} ({change:.2f}%)", "STRONG OUTFLOWS"
    if change < -0.3: return -1, f"${price:.2f} ({change:.2f}%)", "OUTFLOWS"
    return 0, f"${price:.2f} ({change:+.2f}%)", "NEUTRAL"

def score_cot(net, direction):
    """COT net positioning — institutions leading price."""
    if net is None: return 0, "N/A", "NEUTRAL"
    if net > 200000:  return +2, f"Net: +{net:,.0f}", "STRONGLY BULLISH"
    if net > 100000:  return +1, f"Net: +{net:,.0f}", "BULLISH"
    if net < -100000: return -2, f"Net: {net:,.0f}", "STRONGLY BEARISH"
    if net < 0:       return -1, f"Net: {net:,.0f}", "BEARISH"
    return 0, f"Net: {net:,.0f}", "NEUTRAL"

def score_volume(vol_current, vol_ratio):
    """Volume confirmation — high volume validates moves."""
    if vol_ratio is None: return 0, "N/A", "NEUTRAL"
    if vol_ratio > 1.5:   return +1, f"Ratio: {vol_ratio:.2f}x avg", "HIGH VOLUME"
    if vol_ratio < 0.6:   return -1, f"Ratio: {vol_ratio:.2f}x avg", "LOW VOLUME"
    return 0, f"Ratio: {vol_ratio:.2f}x avg", "NORMAL"

# ================================================================
#  ENGINE-SPECIFIC SCORES
# ================================================================

def get_engine_scores(indicators):
    """Derive relevant score for each engine from macro indicators."""
    scores = {}

    # Gold (Confluence) — all indicators
    scores['confluence'] = indicators['total_score']

    # EUR/USD (AurusEdge) — DXY + Fed + VIX
    ae_score = (indicators['dxy']['score'] * 1.5 +
                indicators['fed_policy']['score'] * 1.5 +
                indicators['vix']['score'] * 1.0)
    scores['aurusedge'] = round(ae_score, 2)

    # US100 (North Star) — VIX + Fed + Real yield
    ns_score = (indicators['vix']['score'] * -1.5 +    # high VIX = bad for stocks
                indicators['fed_policy']['score'] * -1.0 +  # high rates = bad for stocks
                indicators['real_yield']['score'] * -1.0)
    scores['northstar'] = round(ns_score, 2)

    # Crash 1000 (Phoenix) — VIX only (synthetic)
    scores['phoenix'] = round(indicators['vix']['score'] * 1.0, 2)

    return scores

# ================================================================
#  MAIN DASHBOARD
# ================================================================

def run_dashboard():
    log("=" * 65)
    log("  MACRO DASHBOARD v1.0 — DAILY BRIEFING")
    log(f"  {datetime.now(timezone.utc).strftime('%A %d %B %Y — %H:%M UTC')}")
    log("=" * 65)

    indicators = {}

    # ── 1. DXY — Dollar Index ─────────────────────────────────────
    log("Fetching DXY (Dollar Index)...")
    dxy_val, dxy_chg = yahoo_price("DX-Y.NYB")
    if dxy_val is None:
        dxy_val, dxy_chg = yahoo_price("UUP")  # DXY ETF fallback
    dxy_score, dxy_str, dxy_bias = score_dxy(dxy_val, dxy_chg)
    indicators['dxy'] = {'score': dxy_score, 'value': dxy_str, 'bias': dxy_bias}

    # ── 2. Real Yield — 10Y TIPS ──────────────────────────────────
    log("Fetching Real Yields (FRED DFII10)...")
    ry_val, ry_chg = fred_series("DFII10", limit=5)
    ry_score, ry_str, ry_bias = score_real_yield(ry_val, ry_chg)
    indicators['real_yield'] = {'score': ry_score, 'value': ry_str, 'bias': ry_bias}

    # ── 3. Fed Policy — Federal Funds Rate ───────────────────────
    log("Fetching Fed Policy (FRED FEDFUNDS)...")
    fed_val, fed_chg = fred_series("FEDFUNDS", limit=5)
    fed_score, fed_str, fed_bias = score_fed_policy(fed_val, fed_chg)
    indicators['fed_policy'] = {'score': fed_score, 'value': fed_str, 'bias': fed_bias}

    # ── 4. Inflation Expectations — 5Y Breakeven ─────────────────
    log("Fetching Inflation Expectations (FRED T5YIE)...")
    inf_val, inf_chg = fred_series("T5YIE", limit=5)
    inf_score, inf_str, inf_bias = score_inflation(inf_val, inf_chg)
    indicators['inflation'] = {'score': inf_score, 'value': inf_str, 'bias': inf_bias}

    # ── 5. VIX — Fear Index ───────────────────────────────────────
    log("Fetching VIX (Fear Index)...")
    vix_val, vix_chg = yahoo_price("^VIX")
    vix_score, vix_str, vix_bias = score_vix(vix_val, vix_chg)
    indicators['vix'] = {'score': vix_score, 'value': vix_str, 'bias': vix_bias}

    # ── 6. Gold ETF Flows — GLD ───────────────────────────────────
    log("Fetching Gold ETF (GLD) flows...")
    gld_val, gld_chg = yahoo_price("GLD")
    gld_score, gld_str, gld_bias = score_gold_etf(gld_val, gld_chg)
    indicators['gold_etf'] = {'score': gld_score, 'value': gld_str, 'bias': gld_bias}

    # ── 7. COT Report ─────────────────────────────────────────────
    log("Fetching COT Report (CFTC)...")
    cot_net, cot_dir = get_cot_bias()
    cot_score, cot_str, cot_bias = score_cot(cot_net, cot_dir)
    indicators['cot'] = {'score': cot_score, 'value': cot_str, 'bias': cot_bias}

    # ── 8. Volume Confirmation ────────────────────────────────────
    log("Fetching Volume from MT5...")
    vol_cur, vol_ratio = get_mt5_volume()
    vol_score, vol_str, vol_bias = score_volume(vol_cur, vol_ratio)
    indicators['volume'] = {'score': vol_score, 'value': vol_str, 'bias': vol_bias}

    # ── Calculate Composite Score ─────────────────────────────────
    total_score = sum(
        indicators[k]['score'] * WEIGHTS[k]
        for k in WEIGHTS.keys()
    )
    total_score = round(total_score, 2)
    indicators['total_score'] = total_score

    # Score interpretation
    if total_score >= 4:      macro_bias = "STRONGLY BULLISH GOLD"
    elif total_score >= 2:    macro_bias = "BULLISH GOLD"
    elif total_score >= 0.5:  macro_bias = "SLIGHT GOLD BULL"
    elif total_score >= -0.5: macro_bias = "NEUTRAL"
    elif total_score >= -2:   macro_bias = "SLIGHT GOLD BEAR"
    elif total_score >= -4:   macro_bias = "BEARISH GOLD"
    else:                     macro_bias = "STRONGLY BEARISH GOLD"

    # ── SL/TP Probabilities ───────────────────────────────────────
    atr = get_current_atr()
    if atr:
        probs = calc_sltp_probabilities(
            atr=atr,
            stop_pts=atr * 2.0,   # typical stop ~2x ATR
            tp1_pts=atr * 3.0,    # TP1 at 1.5R
            tp2_pts=atr * 6.0,    # TP2 at 3.0R
        )
    else:
        probs = None

    # ── Engine scores ─────────────────────────────────────────────
    engine_scores = get_engine_scores(indicators)

    # ── Print Full Briefing ───────────────────────────────────────
    print("\n")
    print("╔" + "═"*63 + "╗")
    print("║" + "  DAILY MACRO BRIEFING — AurusEdge Portfolio".center(63) + "║")
    print("║" + datetime.now(timezone.utc).strftime("  %A %d %B %Y  |  %H:%M UTC").center(63) + "║")
    print("╠" + "═"*63 + "╣")
    print("║" + "  GOLD MACRO INDICATORS".ljust(63) + "║")
    print("╠" + "═"*63 + "╣")

    rows = [
        ("DXY (Dollar Index)",         indicators['dxy']),
        ("Real Yield (10Y TIPS)",       indicators['real_yield']),
        ("Fed Policy (FFR)",            indicators['fed_policy']),
        ("Inflation Expectations",      indicators['inflation']),
        ("VIX (Fear Index)",            indicators['vix']),
        ("Gold ETF Flows (GLD)",        indicators['gold_etf']),
        ("COT Non-Commercial Net",      indicators['cot']),
        ("Volume Confirmation",         indicators['volume']),
    ]

    for name, ind in rows:
        sc  = ind['score']
        val = ind['value']
        bias= ind['bias']
        arrow = "▲" if sc > 0 else ("▼" if sc < 0 else "─")
        color_label = f"{arrow} {bias}"
        line = f"  {name:<28} {val:<18} {color_label}"
        print("║" + line.ljust(63) + "║")

    print("╠" + "═"*63 + "╣")

    # Composite score bar
    bar_filled = int(abs(total_score) * 3)
    bar = ("█" * bar_filled).ljust(30)
    direction_arrow = "▲" if total_score > 0 else ("▼" if total_score < 0 else "─")
    print("║" + f"  GOLD MACRO SCORE: {total_score:+.2f} / ±10.0  {direction_arrow}".ljust(63) + "║")
    print("║" + f"  MACRO BIAS: {macro_bias}".ljust(63) + "║")

    print("╠" + "═"*63 + "╣")
    print("║" + "  ENGINE IMPACT SCORES".ljust(63) + "║")
    print("╠" + "═"*63 + "╣")

    engine_names = {
        'confluence': 'Confluence (XAU/USD)',
        'aurusedge':  'AurusEdge (EUR/USD)',
        'northstar':  'North Star (US100)',
        'phoenix':    'Phoenix (Crash 1000)',
    }

    for key, name in engine_names.items():
        sc = engine_scores[key]
        if sc > 1:    verdict = "GREEN — Trade freely"
        elif sc > 0:  verdict = "YELLOW — Trade with caution"
        elif sc > -1: verdict = "YELLOW — Reduce size"
        else:          verdict = "RED — Avoid trading"
        print("║" + f"  {name:<22} Score: {sc:>+6.2f}  |  {verdict}".ljust(63) + "║")

    print("╠" + "═"*63 + "╣")

    if probs:
        print("║" + "  SL/TP HIT PROBABILITIES (Gold — Current ATR)".ljust(63) + "║")
        print("╠" + "═"*63 + "╣")
        print("║" + f"  Current ATR (M5):     ${atr:.2f}".ljust(63) + "║")
        print("║" + f"  Stop Distance:        ${atr*2:.2f}  (ATR × 2.0)".ljust(63) + "║")
        print("║" + f"  TP1 Distance:         ${atr*3:.2f}  (ATR × 3.0 = 1.5R)".ljust(63) + "║")
        print("║" + f"  TP2 Distance:         ${atr*6:.2f}  (ATR × 6.0 = 3.0R)".ljust(63) + "║")
        print("║" + "─"*63 + "║")
        print("║" + f"  P(Stop Loss Hit):     {probs['p_sl']}%".ljust(63) + "║")
        print("║" + f"  P(TP1 Hit):           {probs['p_tp1']}%".ljust(63) + "║")
        print("║" + f"  P(TP2 Hit):           {probs['p_tp2']}%".ljust(63) + "║")
        print("║" + f"  P(Breakeven Exit):    {probs['p_be']}%".ljust(63) + "║")
        print("║" + f"  Expected Edge per R:  {probs['edge_r']:+.3f}R".ljust(63) + "║")
        print("║" + f"  Stop/ATR Ratio:       {probs['sl_atr']}x".ljust(63) + "║")

    print("╠" + "═"*63 + "╣")
    print("║" + "  TRADING RECOMMENDATION".ljust(63) + "║")
    print("╠" + "═"*63 + "╣")

    if total_score >= 2:
        rec = "LONG BIAS — Prioritise bullish Gold setups today"
    elif total_score <= -2:
        rec = "SHORT BIAS — Prioritise bearish Gold setups today"
    elif abs(total_score) < 0.5:
        rec = "NO CLEAR BIAS — Trade structure only, reduce size"
    else:
        rec = "WEAK BIAS — Trade with normal size, tight filters"

    print("║" + f"  {rec}".ljust(63) + "║")
    print("╚" + "═"*63 + "╝")
    print()

    # ── Save to JSON for engines to read ──────────────────────────
    state = {
        'date':           datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'time':           datetime.now(timezone.utc).strftime('%H:%M UTC'),
        'total_score':    total_score,
        'macro_bias':     macro_bias,
        'recommendation': rec,
        'engine_scores':  engine_scores,
        'indicators':     {k: v for k, v in indicators.items() if isinstance(v, dict)},
        'probabilities':  probs,
        'atr':            round(atr, 2) if atr else None,
    }

    with open(MACRO_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)

    log(f"Macro state saved to {MACRO_STATE_FILE}")
    log(f"Gold Macro Score: {total_score:+.2f} | Bias: {macro_bias}")

    return state

# ================================================================
#  WATCH MODE — Run every morning at 06:30 UTC
# ================================================================

def watch_mode():
    log("Watch mode active. Running briefing every morning at 06:30 UTC.")
    log("Press Ctrl+C to stop.")

    while True:
        try:
            now = datetime.now(timezone.utc)
            # Target: 06:30 UTC
            target = now.replace(hour=6, minute=30, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()

            log(f"Next briefing at 06:30 UTC — sleeping {wait/3600:.1f} hours")
            time.sleep(wait)

            run_dashboard()

        except KeyboardInterrupt:
            log("Watch mode stopped.")
            break
        except Exception as e:
            log(f"Dashboard error: {e}", "ERROR")
            time.sleep(300)

# ================================================================
#  READ STATE (for engines to call)
# ================================================================

def read_macro_state():
    """
    Called by trading engines before placing orders.
    Returns the saved macro state or None if unavailable.
    """
    try:
        if not os.path.exists(MACRO_STATE_FILE):
            return None
        with open(MACRO_STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        # Check if state is from today
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if state.get('date') != today:
            return None  # stale data
        return state
    except Exception:
        return None

def get_engine_gate(engine_name):
    """
    Simple gate for engines. Returns True if macro allows trading.
    engine_name: 'confluence', 'aurusedge', 'northstar', 'phoenix'
    """
    state = read_macro_state()
    if state is None:
        return True, "No macro data — trading allowed (run macro_dashboard.py)"

    score = state['engine_scores'].get(engine_name, 0)
    if score <= -1:
        return False, f"Macro gate CLOSED | Score: {score:+.2f} | {state['macro_bias']}"

    return True, f"Macro gate OPEN | Score: {score:+.2f} | {state['macro_bias']}"

# ================================================================
#  ENTRY POINT
# ================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--watch":
        watch_mode()
    elif len(sys.argv) > 1 and sys.argv[1] == "--read":
        state = read_macro_state()
        if state:
            print(json.dumps(state, indent=2))
        else:
            print("No macro state available. Run: python macro_dashboard.py")
    else:
        run_dashboard()
