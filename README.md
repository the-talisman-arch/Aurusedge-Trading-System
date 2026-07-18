# AurusEdge Algorithmic Trading System

**Built by Talisman (Johnny Igorigo)**
**Electronics Engineering Student, Rivers State University**
**SIWES Placement — JIBS Engineering Services Limited**

---

## Overview

AurusEdge is a systematic, fully automated multi-instrument algorithmic trading portfolio built from scratch using Python and MetaTrader 5. The system was designed and developed between June and July 2026 using a rigorous quantitative research methodology — statistical profiling, strategy design, backtesting, and live demo deployment.

The project demonstrates the full quant finance workflow: from raw OHLCV data to live auto-executing engines across four instruments simultaneously.

---

## Portfolio Architecture

```
AurusEdge Portfolio
├── Phoenix Engine          → Crash 1000 Index (Deriv Synthetic)
├── AurusEdge Engine        → EUR/USD (Major Forex)
├── North Star Engine       → US100 / NASDAQ 100 (Index CFD)
├── Confluence Engine       → XAU/USD Gold (Commodity)
└── Macro Dashboard         → Daily intelligence layer for all engines
```

---

## Engines

### 1. Phoenix — Crash 1000 Post-Spike Recovery
**File:** `phoenix_engine.py`

**Strategy:** Post-spike recovery on Crash 1000 synthetic index. Detects crash spikes using a 4-sigma threshold on M5 returns, waits for bullish confirmation candle, enters long at next candle open targeting the pre-crash price level.

**Phase 1 Findings:**
- Instrument: Crash 1000 Index (Deriv synthetic)
- 78.4% of candles are bullish — strong upward drift
- +0.19% daily price drift confirmed
- Crash spikes occur 2.35x per day on average

**Backtest Results (138 days, M5):**
| Metric | Result |
|---|---|
| Total Trades | 177 |
| Win Rate | 59.9% |
| Expectancy | +0.23R per trade |
| Max Drawdown | -9.00% |
| Return | +167.3% |

**Entry Logic:**
1. Single M5 candle drops more than 4σ below 50-period rolling mean
2. Candle body exceeds 2× ATR14 — confirms genuine spike
3. Next candle closes bullish — confirmation of buyer re-entry
4. Enter LONG at open of candle after confirmation
5. Stop Loss: crash candle Low − 0.5×ATR
6. TP1: 50% of recovery distance (close 50% position)
7. TP2: Pre-crash open price (full recovery target)

**Risk Parameters:**
- Risk per trade: 0.1% (demo) → 1% (live)
- Max trades per day: 2
- Max daily drawdown: 2%
- Time exit: 30 candles if TP1 not hit

---

### 2. AurusEdge — EUR/USD SMC Strategy
**File:** `aurusedge_engine.py`

**Strategy:** Multi-timeframe Smart Money Concepts (SMC) strategy on EUR/USD. Uses H4 EMA bias, M15 Fair Value Gaps as Points of Interest, and M5 Change of Character (CHoCH) as entry trigger.

**Phase 1 Findings:**
- Hurst exponent ≈ 0.48 — random walk at price level
- Absolute return autocorrelation: 0.218 — strong volatility clustering
- Kurtosis: 86.759 — extreme fat tails (news events)
- London-NY overlap (13:00-17:00 UTC) is 98% more volatile than off-hours
- Current H4 bias confirmed bearish via swing structure

**Backtest Results (137 days, M5):**
| Metric | Result |
|---|---|
| Total Trades | 173 |
| Win Rate | 60.1% |
| Expectancy | +0.178R per trade |
| Trades per day | 1.39 |
| Return | Positive |

**Three-Layer Entry Logic:**
1. **Layer 1 (H4):** EMA21 > EMA50 = Bullish bias, EMA21 < EMA50 = Bearish bias
2. **Layer 2 (M15):** Identify Fair Value Gaps aligned with H4 bias within last 25 candles
3. **Layer 3 (M5):** CHoCH confirmed — bearish candle closing below peak low (for shorts) or bullish candle closing above trough high (for longs) inside POI zone

**Filters:**
- London session (08:00-12:00 UTC) and NY session (13:00-17:00 UTC) only
- High-impact USD/EUR news blocked 30 minutes before/after
- Macro gate from dashboard must be GREEN or YELLOW

---

### 3. North Star — US100 Opening Range Breakout
**File:** `northstar_engine.py`

**Strategy:** Opening Range Breakout (ORB) on NASDAQ 100 (OTC_NDX / US Tech 100). Defines a range from the first 30 minutes of NY open and trades clean breakouts with momentum confirmation.

**Phase 1 Findings:**
- 96 trading days analyzed
- Average Opening Range: 134 points
- 75% of days break above OR high
- 65.6% of days break below OR low
- 59.4% of days break only ONE side — directional edge confirmed
- NY open (14:00-16:00 UTC) is 161% more volatile than after-hours

**Backtest Results (136 days, M5):**
| Metric | Result |
|---|---|
| Total Trades | 72 |
| Win Rate | 59.7% |
| Expectancy | +0.22R per trade |
| Trades per week | ~2.3 |
| Max Drawdown | Under 5% |

**Entry Logic:**
1. Define Opening Range: highest High and lowest Low of 14:00-14:30 UTC candles
2. Filter: OR range must be 80-350 points (quality filter)
3. LONG: Candle closes above OR High with body ≥ 30% of range AND H4 bias not bearish
4. SHORT: Candle closes below OR Low with body ≥ 30% of range AND H4 bias not bullish
5. Stop Loss: OR midpoint (30% inside range from boundary)
6. TP1: 0.8R (close 50%), TP2: 2.5R
7. Session close: All positions closed at 17:00 UTC

---

### 4. Confluence — XAU/USD Gold London Session FVG
**File:** `confluence_engine.py`

**Strategy:** Gold London session strategy using dual timeframe bias (H4 + H1 alignment), M15 Fair Value Gap identification, and M5 CHoCH confirmation. Includes full macro intelligence layer.

**Phase 1 Findings:**
- Price range analyzed: $4,769 — $6,156 (major bull run then correction)
- Kurtosis: 42.84 — extreme fat tails (macro-driven spikes)
- Absolute return autocorrelation: 0.327 — strongest volatility clustering of all 4 instruments
- London-NY overlap (12:00-15:00 UTC) is 85% more volatile than off-hours
- Current bias: Mixed (recovery phase from $5,595 high)

**Backtest Results (138 days, M5 — London session only):**
| Metric | Result |
|---|---|
| Total Trades | 85 |
| Win Rate | 55.4% |
| Expectancy | +0.089R per trade |
| Trades per day | 0.62 |
| Return | +16.1% |

**Entry Logic:**
1. **Dual TF Bias:** H4 EMA21/50 AND H1 EMA21/50 must both agree on direction
2. **FVG (M15):** Minimum $2 gap, maximum 20 candles old
3. **CHoCH (M5):** Bearish candle closing below peak low inside FVG (short) or bullish candle closing above trough high inside FVG (long)
4. **ATR Filter:** Skip candles with ATR < $3 (low volatility days)
5. Stop: FVG boundary ± 0.5×ATR
6. TP1: 1.5R (close 50%), TP2: 3.0R

**Unique Feature — Gold Macro Report:**
On startup and each new day, Confluence fetches and prints a full macro intelligence briefing including:
- DXY direction and score
- 10Y TIPS Real Yield (FRED API)
- Federal Funds Rate stance (FRED API)
- 5Y Inflation Breakeven expectations (FRED API)
- VIX fear index level
- Gold ETF (GLD) flows
- CFTC COT non-commercial net positioning
- MT5 volume confirmation ratio
- Composite Gold Macro Score (-10 to +10)
- SL/TP hit probabilities based on current ATR

---

### 5. Macro Dashboard
**File:** `macro_dashboard.py`

**Purpose:** Daily intelligence layer that all 4 engines reference before placing trades.

**Data Sources:**
| Indicator | Source | API |
|---|---|---|
| DXY Dollar Index | Yahoo Finance | Free |
| Real Yields (10Y TIPS) | St. Louis FRED | Free key |
| Fed Funds Rate | St. Louis FRED | Free key |
| Inflation Expectations (5Y) | St. Louis FRED | Free key |
| VIX Fear Index | Yahoo Finance | Free |
| Gold ETF Flows (GLD) | Yahoo Finance | Free |
| COT Positioning | CFTC.gov | Free |
| Volume | MT5 tick data | Free |

**Scoring System:**
Each indicator scored -2 to +2 based on Gold direction implication. Weighted composite score produces Gold Macro Score (-10 to +10).

**Engine Gate System:**
- Score > +1: GREEN — Trade freely
- Score 0 to +1: YELLOW — Trade with caution
- Score < -1: RED — Engine blocked from trading

**SL/TP Probability Engine:**
Calculates real-time hit probabilities using current ATR, historical win rate, and stop distance ratio:
- P(Stop Loss Hit)
- P(TP1 Hit)
- P(TP2 Hit)
- P(Breakeven Exit)
- Expected Edge per trade in R

---

## Technical Stack

| Component | Technology |
|---|---|
| Language | Python 3.11 |
| Broker | Deriv (demo account) |
| Platform | MetaTrader 5 |
| MT5 Bridge | MetaTrader5 Python library |
| Data (historical) | Deriv WebSocket API |
| Data (macro) | Yahoo Finance, FRED API, CFTC |
| Execution | Fully automated (FOK fill mode) |

---

## Research Methodology

Each strategy followed a strict 4-phase research process:

**Phase 1 — Statistical Profiling**
- Hurst exponent (trending vs random walk)
- Return autocorrelation at lags 1, 2, 3, 5, 10
- Absolute return autocorrelation (volatility clustering)
- Skewness and kurtosis (tail risk)
- Session volatility analysis (hourly ATR by UTC hour)
- ATR and candle structure analysis

**Phase 2 — Strategy Design**
- Edge mechanism identified from Phase 1 statistics
- Entry rules made fully operational (no vagueness)
- Risk model defined before any backtesting

**Phase 3 — Backtesting**
- Vectorized Python backtester built from scratch
- No look-ahead bias — signals generated on bar N, executed on bar N+1
- Spread costs included
- Parameter optimization with out-of-sample validation
- Regime analysis across different market conditions

**Phase 4 — Live Deployment**
- Demo account validation (4 weeks minimum)
- Live vs backtest performance comparison
- Execution quality monitoring
- Log analysis and edge measurement

---

## Portfolio Performance Summary

| Engine | Instrument | Win Rate | Expectancy | Trades/Day |
|---|---|---|---|---|
| Phoenix | Crash 1000 | 59.9% | +0.23R | 1.28 |
| AurusEdge | EUR/USD | 60.1% | +0.178R | 1.39 |
| North Star | US100 | 59.7% | +0.220R | 0.53 |
| Confluence | XAU/USD | 55.4% | +0.089R | 0.62 |

All four engines validated on Deriv MT5 demo account. Live deployment began June-July 2026.

---

## Running the System

**Requirements:**
```
pip install MetaTrader5 pandas numpy requests
```

**Daily Startup Sequence:**
```bash
# Window 1 — Gold (prints macro report on startup)
cd C:\Users\[username]\Downloads
py -3.11 confluence_engine.py

# Window 2 — EUR/USD
py -3.11 aurusedge_engine.py

# Window 3 — US100
py -3.11 northstar_engine.py
```

**Status Checks:**
```bash
py -3.11 confluence_engine.py --status
py -3.11 aurusedge_engine.py --status
py -3.11 northstar_engine.py --status
```

**Macro Dashboard (standalone):**
```bash
py -3.11 macro_dashboard.py          # run once
py -3.11 macro_dashboard.py --watch  # run daily at 06:30 UTC
```

---

## Risk Management

All engines share the same core risk framework:

- **Position sizing:** Percentage of balance per trade (not fixed dollar)
- **Demo risk:** 0.1% per trade
- **Live risk:** 1% per trade
- **Max daily trades:** 2 per engine
- **Max daily drawdown:** 2% before engine pauses
- **Compounding:** Risk calculated as % of current balance — position sizes grow automatically as account grows

---

## Project Context

This system was built as part of a longer-term career transition from Electronics Engineering into Quantitative Finance. It serves as proof-of-work for:

1. **FTMO Prop Firm Challenge** — $25,000-$50,000 funded account target
2. **Frankfurt University of Applied Sciences** — MSc Quantitative Finance application (2027 intake)
3. **Nigerian Banking/Fintech sector** — Systematic trading and risk systems

Additional portfolio projects built alongside this system:
- **NairaShield** — Monte Carlo FX risk engine (targets Stanbic IBTC)
- **CreditPulse** — Alternative SME credit scoring model (targets Standard Chartered)
- **FlareWatch/EmberTrace** — Flare optimization dashboard using VIIRS satellite data (NLNG placement)

---

## Repository Structure

```
aurusedge-trading-system/
├── README.md                    ← This file
├── phoenix_engine.py            ← Crash 1000 post-spike recovery
├── aurusedge_engine.py          ← EUR/USD SMC strategy
├── northstar_engine.py          ← US100 opening range breakout
├── confluence_engine.py         ← XAU/USD gold London session FVG
├── macro_dashboard.py           ← Daily macro intelligence layer
├── logs/
│   ├── phoenix_log.txt
│   ├── aurusedge_log.txt
│   ├── northstar_log.txt
│   ├── confluence_log.txt
│   └── macro_log.txt
└── data/
    └── macro_state.json         ← Daily macro state (auto-generated)
```

---

## Author

**Talisman (Johnny Igorigo)**
B.Tech Electronics Engineering — Rivers State University, Port Harcourt

*Building toward MSc Quantitative Finance — Frankfurt University of Applied Sciences*

---

*All strategies validated on demo accounts. Past backtest performance does not guarantee future results. This system is for educational and research purposes.*

