"""
╔══════════════════════════════════════════════════════════════╗
║  KRONOS AI PREDICTOR MODULE                                  ║
║  Wraps Kronos foundation model for MGC1!/GC1!/SI1! signals  ║
║  Feeds last N candles → gets directional forecast           ║
╚══════════════════════════════════════════════════════════════╝
"""
 
import sys, os
import numpy as np
import pandas as pd
 
# Add Kronos repo to path
KRONOS_REPO = os.path.join(os.path.dirname(__file__), "repo")
sys.path.insert(0, KRONOS_REPO)
 
# ── GLOBALS ───────────────────────────────────────────────────
_predictor = None   # loaded once, reused every signal
 
def _load_model():
    """Load Kronos model once on first use."""
    global _predictor
    if _predictor is not None:
        return _predictor
 
    try:
        from model import Kronos, KronosTokenizer, KronosPredictor
 
        tokenizer_path = os.path.join(os.path.dirname(__file__), "tokenizer")
        model_path     = os.path.join(os.path.dirname(__file__), "model")
 
        print("Loading Kronos tokenizer...")
        tokenizer = KronosTokenizer.from_pretrained(tokenizer_path)
 
        print("Loading Kronos-small model...")
        model = Kronos.from_pretrained(model_path)
 
        _predictor = KronosPredictor(
            model,
            tokenizer,
            device      = "cpu",     # Railway free = CPU. Change to "cuda:0" if GPU available
            max_context = 128        # Use 128 candles for speed on CPU
        )
        print("Kronos loaded successfully.")
        return _predictor
 
    except Exception as e:
        print(f"Kronos load error: {e}")
        return None
 
# ── FETCH CANDLE DATA ─────────────────────────────────────────
def fetch_candles(symbol: str, tf_minutes: int, limit: int = 128) -> pd.DataFrame:
    """
    Fetch recent OHLCV candles from Yahoo Finance (free, no API key).
    Maps TradingView symbols to Yahoo format.
    """
    import requests
 
    # Symbol map: TradingView → Yahoo Finance
    symbol_map = {
        "MGC1!":   "GC=F",
        "GC1!":    "GC=F",
        "SI1!":    "SI=F",
        "XAUUSD":  "GC=F",
        "XAGUSD":  "SI=F",
    }
 
    yahoo_symbol = symbol_map.get(symbol.upper(), "GC=F")
 
    # Map timeframe minutes to Yahoo interval
    interval_map = {
        1:    "1m",
        5:    "5m",
        15:   "15m",
        30:   "30m",
        60:   "60m",
        240:  "1h",
        1440: "1d",
    }
    interval = interval_map.get(tf_minutes, "15m")
    period   = "5d" if tf_minutes <= 60 else "60d"
 
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
            f"?interval={interval}&range={period}"
        )
        r = requests.get(url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
 
        timestamps = data["chart"]["result"][0]["timestamp"]
        ohlcv      = data["chart"]["result"][0]["indicators"]["quote"][0]
 
        df = pd.DataFrame({
            "open":   ohlcv["open"],
            "high":   ohlcv["high"],
            "low":    ohlcv["low"],
            "close":  ohlcv["close"],
            "volume": ohlcv.get("volume", [0] * len(timestamps))
        }, index=pd.to_datetime(timestamps, unit="s"))
 
        df.dropna(inplace=True)
        return df.tail(limit)
 
    except Exception as e:
        print(f"Data fetch error: {e}")
        return pd.DataFrame()
 
# ── MAIN FORECAST FUNCTION ────────────────────────────────────
def get_kronos_forecast(symbol: str, tf_minutes: int = 15) -> dict:
    """
    Run Kronos forecast on recent candles.
 
    Returns:
        direction   : "BULL" | "BEAR" | "NEUTRAL"
        confidence  : "HIGH" | "MEDIUM" | "LOW"
        probability : float 0.0-1.0 (bull probability)
        candles_used: int
        error       : str or None
    """
    # Default fallback
    fallback = {
        "direction":    "NEUTRAL",
        "confidence":   "LOW",
        "probability":  0.5,
        "candles_used": 0,
        "error":        None
    }
 
    # Load model
    predictor = _load_model()
    if predictor is None:
        fallback["error"] = "Kronos model not loaded"
        return fallback
 
    # Fetch candle data
    df = fetch_candles(symbol, tf_minutes)
    if df.empty or len(df) < 10:
        fallback["error"] = "Not enough candle data"
        return fallback
 
    try:
        import pandas as pd
 
        # Prepare timestamps
        x_ts = df.index[:-1]   # historical timestamps
        y_ts = df.index[-1:]   # next candle timestamp
 
        # Run Kronos prediction
        forecast = predictor.predict(
            df       = df.iloc[:-1],
            x_timestamp = x_ts,
            y_timestamp = y_ts
        )
 
        # Kronos returns predicted OHLCV for next candle
        # We check if predicted close > last known close
        last_close     = float(df["close"].iloc[-1])
        predicted_close = float(forecast["close"].mean())
 
        change_pct = (predicted_close - last_close) / last_close * 100
 
        # Direction
        if change_pct > 0.05:
            direction   = "BULL"
            probability = min(0.5 + abs(change_pct) * 5, 0.95)
        elif change_pct < -0.05:
            direction   = "BEAR"
            probability = max(0.5 - abs(change_pct) * 5, 0.05)
        else:
            direction   = "NEUTRAL"
            probability = 0.5
 
        # Confidence based on magnitude
        if abs(change_pct) > 0.3:
            confidence = "HIGH"
        elif abs(change_pct) > 0.1:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
 
        return {
            "direction":    direction,
            "confidence":   confidence,
            "probability":  round(probability, 2),
            "candles_used": len(df),
            "change_pct":   round(change_pct, 4),
            "error":        None
        }
 
    except Exception as e:
        print(f"Kronos forecast error: {e}")
        fallback["error"] = str(e)
        return fallback
 
# ── SIGNAL AGREEMENT CHECK ────────────────────────────────────
def check_agreement(strat_signal: str, kronos: dict) -> dict:
    """
    Compare Strat signal direction vs Kronos forecast.
    Returns agreement level and combined confidence.
    """
    strat_dir   = "BULL" if strat_signal == "LONG" else "BEAR"
    kronos_dir  = kronos.get("direction", "NEUTRAL")
    k_conf      = kronos.get("confidence", "LOW")
 
    if kronos_dir == "NEUTRAL":
        return {
            "agrees":     False,
            "agreement":  "NEUTRAL",
            "combined":   "MEDIUM",
            "message":    "Kronos sees no clear direction"
        }
 
    agrees = strat_dir == kronos_dir
 
    if agrees and k_conf == "HIGH":
        combined  = "HIGH"
        agreement = "STRONG"
    elif agrees and k_conf == "MEDIUM":
        combined  = "MEDIUM"
        agreement = "MODERATE"
    elif agrees:
        combined  = "LOW"
        agreement = "WEAK"
    else:
        combined  = "LOW"
        agreement = "CONFLICT"
 
    return {
        "agrees":    agrees,
        "agreement": agreement,
        "combined":  combined,
        "message":   f"Kronos {'agrees' if agrees else 'CONFLICTS'} — {kronos_dir} ({k_conf})"
    }
 
