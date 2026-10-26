"""
NSE Post-IPO Consolidation Breakout Scanner
============================================
Scans recent NSE IPO stocks for consolidation + breakout setup.
Sends Telegram alerts when conditions are met.

Requirements:
    pip install yfinance pandas requests schedule

Usage:
    python nse_ipo_scanner.py              # Single scan
    python nse_ipo_scanner.py --watch      # Auto-scan every 30 min during market hours
    python nse_ipo_scanner.py --add TICKER # Add a ticker to watchlist
"""

import yfinance as yf
import pandas as pd
import requests
import json
import os
import argparse
import schedule
import time
from datetime import datetime, timedelta

# Simple EMA calculation — no external TA library needed
def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

# ─────────────────────────────────────────────
# CONFIGURATION  ← Edit these
# ─────────────────────────────────────────────

# Reads from environment variables first (for GitHub Actions / PythonAnywhere)
# If not set, falls back to the hardcoded values below
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")   # Your bot token
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")   # Your chat ID

WATCHLIST_FILE = "ipo_watchlist.json"

# Scanner thresholds
CONFIG = {
    "consolidation_days"    : 20,    # How many days to check for consolidation
    "max_range_pct"         : 15,    # Max % range during consolidation (tighter = better)
    "min_age_days"          : 14,    # Ignore stocks listed less than this
    "max_age_days"          : 540,   # Ignore stocks listed more than this (18 months)
    "vol_dry_up_ratio"      : 0.65,  # Recent vol should be < this × early vol
    "breakout_vol_ratio"    : 1.5,   # Breakout vol must be > this × avg vol
    "data_period"           : "1y",  # yfinance period for historical data
    "ema_fast"              : 20,
    "ema_slow"              : 50,
}

# ─────────────────────────────────────────────
# DEFAULT WATCHLIST  ← Fallback if stocks.txt missing
# ─────────────────────────────────────────────

DEFAULT_IPO_TICKERS = [
    # ── EXISTING WATCHLIST ──────────────────────────────────────────
    "BAJAJHFL.NS",      # Bajaj Housing Finance
    "PREMIER.NS",       # Premier Energies
    "NTPCGREEN.NS",     # NTPC Green Energy
    "RKSWAMY.NS",       # RK Swamy
    "GODIGIT.NS",       # Go Digit General Insurance
    "BHARTIHEXA.NS",    # Bharti Hexacom
    "INVENTURUS.NS",    # Inventurus Knowledge Solutions
    "MOBIKWIK.NS",      # MobiKwik
    "IXIGO.NS",         # Le Travenues Technology (ixigo)
    "AWFIS.NS",         # Awfis Space Solutions
    "EXICOM.NS",        # Exicom Tele-Systems
    "JSWINFRA.NS",      # JSW Infrastructure
    "SIGNATUREG.NS",    # Signature Global
    "TBOTEK.NS",        # TBO Tek

    # ── NEWLY ADDED (Oct 2026 listings) ────────────────────────────
    "HDFIRE.NS",        # HD Fire Protect Ltd           — listed Oct 21, 2026
    "FUSIONCX.NS",      # Fusion CX Ltd                 — listed Oct 22, 2026
    "SMT.NS",           # Sahajanand Medical Tech        — listed Oct 23, 2026
    "BONFIG.NS",        # Bonfiglioli Transmissions     — listed Oct 23, 2026
    "TONBO.NS",         # Tonbo Imaging India           — listed Oct 23, 2026
    "ZETWERK.NS",       # Zetwerk Manufacturing         — listed Oct 23, 2026

    # ── RECENTLY LISTED (Aug–Oct 2026) ─────────────────────────────
    "RUNWALENTR.NS",    # Runwal Enterprises            — listed Oct 5, 2026
    "MANIPALHOS.NS",    # Manipal Health Enterprises    — listed Aug 5, 2026
    "JNPR.NS",          # Juniper Green Energy          — listed Aug 6, 2026
    "MVELECTRO.NS",     # MV Electrosystems             — listed Aug 6, 2026
    "DHOOTTRANS.NS",    # Dhoot Transmission            — listed Aug 2026

    # ── LISTED EARLIER IN 2026 ─────────────────────────────────────
    "FRACTAL.NS",       # Fractal Analytics             — listed Feb 16, 2026
    "GAUDIUMIVF.NS",    # Gaudium IVF & Women Health    — listed Feb 27, 2026
    "SHADOWFAX.NS",     # Shadowfax Technologies        — listed Jan 28, 2026
    "HEXAGON.NS",       # Hexagon Nutrition             — listed Jun 12, 2026

    # ── NOT YET LISTED — add once they list on NSE ─────────────────
    # "VISHNIRMITI.NS", # Vishal Nirmiti Ltd
    # "SRITINDIA.NS",   # SRIT India Ltd
    # "MONEYVIEW.NS",   # Moneyview Ltd
]


# ─────────────────────────────────────────────
# LOAD TICKERS  ← Reads stocks.txt if available
# ─────────────────────────────────────────────

def load_tickers() -> list:
    """
    Read tickers from stocks.txt (managed via HTML dashboard).
    Falls back to DEFAULT_IPO_TICKERS if file not found.
    stocks.txt format: one ticker per line, no .NS needed.
    Lines starting with # are ignored (comments).
    """
    if os.path.exists("stocks.txt"):
        tickers = []
        with open("stocks.txt") as f:
            for line in f:
                line = line.strip().upper()
                if line and not line.startswith("#"):
                    # Add .NS if not already there
                    t = line if line.endswith(".NS") else line + ".NS"
                    tickers.append(t)
        if tickers:
            print(f"📋 Loaded {len(tickers)} tickers from stocks.txt")
            return tickers
        else:
            print("⚠️  stocks.txt is empty — using default list")
    else:
        print(f"📋 stocks.txt not found — using {len(DEFAULT_IPO_TICKERS)} default tickers")
    return DEFAULT_IPO_TICKERS[:]


# ─────────────────────────────────────────────
# WATCHLIST MANAGEMENT
# ─────────────────────────────────────────────

def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
            return data.get("tickers", DEFAULT_IPO_TICKERS[:])
    return DEFAULT_IPO_TICKERS[:]

def save_watchlist(tickers):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump({"tickers": tickers, "updated": datetime.now().isoformat()}, f, indent=2)

def add_to_watchlist(ticker):
    if not ticker.endswith(".NS"):
        ticker = ticker.upper() + ".NS"
    tickers = load_watchlist()
    if ticker not in tickers:
        tickers.append(ticker)
        save_watchlist(tickers)
        print(f"✅ Added {ticker} to watchlist ({len(tickers)} total)")
    else:
        print(f"ℹ️  {ticker} already in watchlist")


# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def fetch_data(ticker: str) -> pd.DataFrame | None:
    """Fetch OHLCV from Yahoo Finance."""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period=CONFIG["data_period"], auto_adjust=True)
        if df.empty or len(df) < CONFIG["consolidation_days"] + 5:
            return None
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception as e:
        print(f"  ⚠️  {ticker}: fetch error — {e}")
        return None

def get_listing_age(df: pd.DataFrame) -> int:
    """Return days since first data point (proxy for listing date)."""
    return (datetime.now() - df.index[0]).days


# ─────────────────────────────────────────────
# TECHNICAL CHECKS
# ─────────────────────────────────────────────

def check_consolidation(df: pd.DataFrame) -> dict:
    """
    Consolidation = price in tight range + volume drying up.
    Uses the last N days as the 'base'.
    """
    n = CONFIG["consolidation_days"]
    recent = df.tail(n)

    # Price range check
    high  = recent["High"].max()
    low   = recent["Low"].min()
    rng   = ((high - low) / low) * 100

    # Volume dry-up check
    early_vol  = recent.head(n // 2)["Volume"].mean()
    late_vol   = recent.tail(n // 2)["Volume"].mean()
    vol_ratio  = late_vol / early_vol if early_vol > 0 else 1

    # ATR contraction (tighter ATR = better base)
    atr_early = recent.head(n // 2)["High"].sub(recent.head(n // 2)["Low"]).mean()
    atr_late  = recent.tail(5)["High"].sub(recent.tail(5)["Low"]).mean()
    atr_pct   = (atr_late / atr_early) * 100 if atr_early > 0 else 100

    is_tight       = rng < CONFIG["max_range_pct"]
    vol_drying     = vol_ratio < CONFIG["vol_dry_up_ratio"]
    atr_contracting = atr_pct < 70

    return {
        "consolidating"       : is_tight and vol_drying,
        "range_pct"           : round(rng, 2),
        "consolidation_high"  : round(high, 2),
        "consolidation_low"   : round(low, 2),
        "vol_dry_ratio"       : round(vol_ratio, 2),
        "atr_contraction_pct" : round(atr_pct, 1),
        "vol_drying"          : vol_drying,
        "atr_contracting"     : atr_contracting,
    }

def check_breakout(df: pd.DataFrame, consol_high: float) -> dict:
    """
    Breakout = latest close above consolidation high + volume surge.
    """
    n          = CONFIG["consolidation_days"]
    avg_vol    = df["Volume"].tail(n).mean()
    latest     = df.iloc[-1]
    prev       = df.iloc[-2]

    price_break   = latest["Close"] > consol_high
    prev_below    = prev["Close"] <= consol_high       # Just crossed today
    vol_surge     = latest["Volume"] > avg_vol * CONFIG["breakout_vol_ratio"]
    vol_ratio     = latest["Volume"] / avg_vol if avg_vol > 0 else 0

    # Breakout % above resistance
    break_pct = ((latest["Close"] - consol_high) / consol_high * 100) if price_break else 0

    return {
        "breaking_out"  : price_break and vol_surge,
        "price"         : round(latest["Close"], 2),
        "break_pct"     : round(break_pct, 2),
        "vol_ratio"     : round(vol_ratio, 2),
        "fresh_breakout": price_break and prev_below,
    }

def check_trend(df: pd.DataFrame) -> dict:
    """
    Healthy trend = price above key EMAs, EMAs in correct order.
    """
    close = df["Close"]
    ema20 = ema(close, CONFIG["ema_fast"])
    ema50 = ema(close, CONFIG["ema_slow"])

    if ema20 is None or ema50 is None:
        return {"healthy_trend": False}

    latest_close = close.iloc[-1]
    latest_e20   = ema20.iloc[-1]
    latest_e50   = ema50.iloc[-1]

    if pd.isna(latest_e20) or pd.isna(latest_e50):
        return {"healthy_trend": False}

    above_ema50   = latest_close > latest_e50
    ema20_gt_ema50 = latest_e20 > latest_e50

    # RS score proxy: 3-month % change
    months_ago = df.iloc[-63]["Close"] if len(df) >= 63 else df.iloc[0]["Close"]
    rs_score   = ((latest_close - months_ago) / months_ago * 100) if months_ago > 0 else 0

    return {
        "healthy_trend"  : above_ema50,
        "ema20_gt_ema50" : ema20_gt_ema50,
        "rs_score"       : round(rs_score, 1),
        "ema20"          : round(latest_e20, 2),
        "ema50"          : round(latest_e50, 2),
    }

def score_setup(consol: dict, trend: dict, breakout: dict) -> int:
    """
    Score the setup quality 0-10.
    Higher = stronger setup.
    """
    score = 0
    if consol["consolidating"]:         score += 2
    if consol["range_pct"] < 10:        score += 1    # Extra for super-tight base
    if consol["vol_drying"]:            score += 1
    if consol["atr_contracting"]:       score += 1
    if trend["healthy_trend"]:          score += 2
    if trend.get("ema20_gt_ema50"):     score += 1
    if trend.get("rs_score", 0) > 10:  score += 1    # Relative strength
    if breakout["breaking_out"]:        score += 1
    if breakout.get("fresh_breakout"):  score += 1    # Clean fresh break today
    return min(score, 10)


# ─────────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.ok
    except Exception:
        return False

def format_alert(ticker: str, consol: dict, trend: dict, breakout: dict, age: int, score: int) -> str:
    stars = "⭐" * score
    sym   = ticker.replace(".NS", "")
    kind  = "🚀 <b>BREAKOUT ALERT</b>" if breakout["breaking_out"] else "⏳ <b>WATCH LIST</b>"

    return (
        f"{kind}: <b>{sym}</b>  {stars}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Listed: {age} days ago\n"
        f"💰 Price:  ₹{breakout['price']}\n"
        f"📊 Setup score: {score}/10\n\n"
        f"🔲 <b>Base</b>\n"
        f"   Range:       {consol['range_pct']}%   (target <15%)\n"
        f"   Resistance:  ₹{consol['consolidation_high']}\n"
        f"   Support:     ₹{consol['consolidation_low']}\n"
        f"   Vol dry-up:  {int(consol['vol_dry_ratio']*100)}% of prev avg\n"
        f"   ATR compres: {consol['atr_contraction_pct']}%\n\n"
        f"📈 <b>Trend</b>\n"
        f"   EMA20: ₹{trend.get('ema20','N/A')}  EMA50: ₹{trend.get('ema50','N/A')}\n"
        f"   3M RS score: {trend.get('rs_score','N/A')}%\n\n"
        f"💥 <b>Breakout</b>\n"
        f"   Volume: {breakout['vol_ratio']}× avg\n"
        f"   Break above resistance: {breakout['break_pct']}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 Stop Loss: ₹{consol['consolidation_low']}\n"
        f"🎯 Target:   ₹{round(consol['consolidation_high'] * 1.20, 2)}  (+20%)\n"
    )


# ─────────────────────────────────────────────
# MAIN SCANNER
# ─────────────────────────────────────────────

def scan(verbose=True) -> list[dict]:
    # ✅ Now reads from stocks.txt first (managed via HTML dashboard)
    tickers = load_tickers()
    timestamp = datetime.now().strftime("%d-%b-%Y %H:%M")
    results = {"breakouts": [], "watch": [], "skipped": []}

    if verbose:
        print(f"\n{'='*56}")
        print(f"  NSE IPO Consolidation Breakout Scanner  |  {timestamp}")
        print(f"  Scanning {len(tickers)} tickers")
        print(f"{'='*56}\n")

    for ticker in tickers:
        if verbose:
            print(f"  Checking {ticker}...", end=" ")

        df = fetch_data(ticker)
        if df is None:
            results["skipped"].append(ticker)
            if verbose: print("⚠️  no data")
            continue

        age = get_listing_age(df)

        # Skip stocks outside IPO age window
        if age < CONFIG["min_age_days"] or age > CONFIG["max_age_days"]:
            results["skipped"].append(ticker)
            if verbose: print(f"↩️  {age}d old (out of range)")
            continue

        consol   = check_consolidation(df)
        trend    = check_trend(df)
        breakout = check_breakout(df, consol["consolidation_high"])
        score    = score_setup(consol, trend, breakout)

        entry = {
            "ticker"  : ticker,
            "price"   : breakout["price"],
            "score"   : score,
            "age_days": age,
            "consol"  : consol,
            "trend"   : trend,
            "breakout": breakout,
        }

        if breakout["breaking_out"] and consol["consolidating"]:
            results["breakouts"].append(entry)
            if verbose: print(f"🚀 BREAKOUT! ₹{breakout['price']}  vol={breakout['vol_ratio']}×  score={score}/10")
        elif consol["consolidating"] and trend["healthy_trend"]:
            results["watch"].append(entry)
            if verbose: print(f"⏳ WATCH  base={consol['range_pct']}%  score={score}/10")
        else:
            results["skipped"].append(ticker)
            if verbose: print(f"—  not set up yet")

    # Sort by score descending
    results["breakouts"].sort(key=lambda x: x["score"], reverse=True)
    results["watch"].sort(key=lambda x: x["score"], reverse=True)

    # Print summary
    if verbose:
        print(f"\n{'─'*56}")
        print(f"  🚀 Breakouts:  {len(results['breakouts'])}")
        print(f"  ⏳ Watch list: {len(results['watch'])}")
        print(f"  ↩️  Skipped:   {len(results['skipped'])}")
        print(f"{'─'*56}\n")

        for e in results["breakouts"]:
            sym = e["ticker"].replace(".NS","")
            print(f"  🚀 {sym:15s} ₹{e['price']:>8.2f}  vol={e['breakout']['vol_ratio']}×  score={e['score']}/10")

        print()
        for e in results["watch"][:10]:   # Top 10 watch
            sym = e["ticker"].replace(".NS","")
            print(f"  ⏳ {sym:15s} ₹{e['price']:>8.2f}  base={e['consol']['range_pct']:>4.1f}%  "
                  f"vol_dry={int(e['consol']['vol_dry_ratio']*100)}%  score={e['score']}/10")

    # Save results to JSON for dashboard
    save_results_json(results, timestamp)

    # Send Telegram alerts
    sent = 0
    for e in results["breakouts"]:
        msg = format_alert(e["ticker"], e["consol"], e["trend"], e["breakout"], e["age_days"], e["score"])
        if send_telegram(msg):
            sent += 1

    # Also send top 3 watch list items if no breakouts
    if not results["breakouts"] and results["watch"]:
        summary = "📋 <b>NSE IPO Watch List</b> — No breakouts yet\n\n"
        for e in results["watch"][:3]:
            sym = e["ticker"].replace(".NS","")
            summary += (f"⏳ <b>{sym}</b>  score={e['score']}/10\n"
                        f"   Base range: {e['consol']['range_pct']}%  "
                        f"Support: ₹{e['consol']['consolidation_low']}  "
                        f"Resistance: ₹{e['consol']['consolidation_high']}\n\n")
        send_telegram(summary)

    if verbose and sent:
        print(f"  📱 Sent {sent} Telegram alert(s)\n")

    return results


def save_results_json(results: dict, timestamp: str):
    """Save scan results to results.json for the HTML dashboard."""
    def clean(entry):
        return {
            "ticker"   : entry["ticker"].replace(".NS", ""),
            "price"    : entry["price"],
            "score"    : entry["score"],
            "age_days" : entry["age_days"],
            "range_pct": entry["consol"]["range_pct"],
            "support"  : entry["consol"]["consolidation_low"],
            "resistance": entry["consol"]["consolidation_high"],
            "vol_ratio": entry["breakout"]["vol_ratio"],
            "break_pct": entry["breakout"]["break_pct"],
            "rs_score" : entry["trend"].get("rs_score", 0),
            "ema20"    : entry["trend"].get("ema20", 0),
            "ema50"    : entry["trend"].get("ema50", 0),
            "stop_loss": entry["consol"]["consolidation_low"],
            "target"   : round(entry["consol"]["consolidation_high"] * 1.20, 2),
            "breaking_out": entry["breakout"]["breaking_out"],
        }

    output = {
        "timestamp" : timestamp,
        "breakouts" : [clean(e) for e in results["breakouts"]],
        "watch"     : [clean(e) for e in results["watch"]],
        "total_scanned": len(results["breakouts"]) + len(results["watch"]) + len(results["skipped"]),
    }

    with open("results.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"  💾 Results saved to results.json")


def is_market_hours():
    """Return True during NSE market hours (09:15–15:30 IST = 03:45–10:00 UTC)."""
    now = datetime.utcnow()
    open_t  = now.replace(hour=3,  minute=45, second=0)
    close_t = now.replace(hour=10, minute=0,  second=0)
    return open_t <= now <= close_t


# ─────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NSE IPO Consolidation Breakout Scanner")
    parser.add_argument("--watch",   action="store_true", help="Auto-scan every 30 min during market hours")
    parser.add_argument("--add",     metavar="TICKER",    help="Add a ticker to the watchlist")
    parser.add_argument("--list",    action="store_true", help="Show watchlist")
    parser.add_argument("--quiet",   action="store_true", help="Suppress console output")
    args = parser.parse_args()

    if args.add:
        add_to_watchlist(args.add)
        return

    if args.list:
        tickers = load_tickers()
        print(f"\nWatchlist ({len(tickers)} tickers):")
        for t in tickers:
            print(f"  {t}")
        return

    if args.watch:
        print("🔄 Watching market — scanning every 30 minutes during NSE hours...")
        print("   Press Ctrl+C to stop.\n")
        scan(verbose=not args.quiet)

        def job():
            if is_market_hours():
                scan(verbose=not args.quiet)
            else:
                print(f"  💤 {datetime.now().strftime('%H:%M')} — market closed, skipping scan")

        schedule.every(30).minutes.do(job)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        scan(verbose=not args.quiet)


if __name__ == "__main__":
    main()
