"""
╔══════════════════════════════════════════════════════════════╗
║  KRONUS AI — MAIN SERVER  (v5 — outcomes + paper mode)      ║
║                                                              ║
║  NEW vs v4.2:                                                ║
║   • Every signal auto-tracked for outcome via yfinance:      ║
║     TP1 / TP2 / STOP / TIMEOUT  (confirmed, paper, AND       ║
║     skipped — so you see what you missed).                   ║
║   • Three-state action: ✅ Live / 📝 Paper / ⏸ Skip           ║
║   • MFE/MAE recorded on every trade for later analysis.      ║
║   • Persistent state (JSON file, atomic writes) — survives   ║
║     server restarts on persistent-disk hosts.                ║
║   • Thread-safe (RLock) — no data races on shared state.     ║
║   • Graceful degradation everywhere:                         ║
║       - yfinance missing → tracker disabled, rest works      ║
║       - Symbol not mapped → logged once, skipped             ║
║       - yfinance errors → retried next poll cycle            ║
║       - Disk write fails → logged, keeps running in-memory   ║
║       - Thread errors → caught per iteration, loop survives  ║
║       - Telegram edit fails → logged, pipeline continues     ║
║       - Bad webhook → validated & rejected with reason       ║
║   • Memory bounded: resolved trades >30d auto-purged on boot.║
║   • /outcomes JSON endpoint for external analysis.           ║
║                                                              ║
║  REQUIREMENTS:                                               ║
║    pip install flask requests yfinance pandas                ║
║                                                              ║
║  ENV VARS (all optional except TELEGRAM_* for alerts):       ║
║    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET,         ║
║    ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_TIMEOUT (8),      ║
║    CLAUDE_ENABLED (true), PUBLIC_URL, STATE_FILE,            ║
║    OUTCOME_POLL_SEC (60), TRADE_TIMEOUT_HRS (4),             ║
║    PURGE_DAYS (30)                                           ║
╚══════════════════════════════════════════════════════════════╝
"""
import os
import json
import logging
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify

# ── Optional: outcome tracking dependencies ───────────────────
try:
    import yfinance as yf
    import pandas as pd
    OUTCOME_TRACKING_AVAILABLE = True
except ImportError:
    OUTCOME_TRACKING_AVAILABLE = False

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

if not OUTCOME_TRACKING_AVAILABLE:
    log.warning("yfinance/pandas missing — outcome tracking DISABLED. "
                "Install: pip install yfinance pandas")

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "goldstrat2025")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_TIMEOUT    = int(os.environ.get("CLAUDE_TIMEOUT", "8"))
CLAUDE_ENABLED    = os.environ.get("CLAUDE_ENABLED", "true").lower() == "true"
PUBLIC_URL        = os.environ.get("PUBLIC_URL", "")

STATE_FILE        = os.environ.get("STATE_FILE", "kronus_state.json")
OUTCOME_POLL_SEC  = int(os.environ.get("OUTCOME_POLL_SEC", "60"))
TRADE_TIMEOUT_HRS = int(os.environ.get("TRADE_TIMEOUT_HRS", "4"))
PURGE_DAYS        = int(os.environ.get("PURGE_DAYS", "30"))

# ── SYMBOL MAPPING (TradingView → yfinance) ───────────────────
# Extend as you add instruments. Missing symbols just skip tracking.
YF_SYMBOL_MAP = {
    # Metals
    "MGC1!": "GC=F", "GC1!":  "GC=F",       # Gold
    "SIL1!": "SI=F", "SI1!":  "SI=F",       # Silver
    "HG1!":  "HG=F",                         # Copper
    "PL1!":  "PL=F",                         # Platinum
    # Equity index
    "MNQ1!": "NQ=F", "NQ1!":  "NQ=F",       # Nasdaq
    "MES1!": "ES=F", "ES1!":  "ES=F",       # S&P
    "MYM1!": "YM=F", "YM1!":  "YM=F",       # Dow
    "M2K1!": "RTY=F", "RTY1!": "RTY=F",     # Russell
    # Energy
    "MCL1!": "CL=F", "CL1!":  "CL=F",       # Crude
    "NG1!":  "NG=F",                         # Natgas
    # FX / bonds
    "M6E1!": "6E=F", "6E1!":  "6E=F",
    "ZB1!":  "ZB=F",
    "ZN1!":  "ZN=F",
}

# ── STATE (thread-safe) ───────────────────────────────────────
STATE_LOCK = threading.RLock()
TRACKING = {}            # trade_id (str) -> full trade record
UNMAPPED_WARNED = set()  # so we log missing symbols only once

# ── PERSISTENCE ───────────────────────────────────────────────
def _save_state_unlocked():
    """Caller must hold STATE_LOCK. Atomic write via temp + replace."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"tracking": TRACKING}, f, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error(f"State save failed: {e}")

def save_state():
    with STATE_LOCK:
        _save_state_unlocked()

def load_state():
    """Load state on startup. Purge resolved trades older than PURGE_DAYS."""
    try:
        if not os.path.exists(STATE_FILE):
            log.info("No state file — starting fresh")
            return
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        loaded = data.get("tracking", {}) or {}
        cutoff = datetime.now(timezone.utc) - timedelta(days=PURGE_DAYS)
        kept = {}
        for tid, t in loaded.items():
            rt = t.get("result_time")
            if rt:
                try:
                    if datetime.fromisoformat(rt) < cutoff:
                        continue
                except Exception:
                    pass
            kept[tid] = t
        with STATE_LOCK:
            TRACKING.update(kept)
        log.info(f"State loaded: {len(kept)} trades "
                 f"({len(loaded) - len(kept)} purged as >{PURGE_DAYS}d old)")
    except Exception as e:
        log.error(f"State load failed (starting fresh): {e}")

# ── SIGNAL VALIDATION ─────────────────────────────────────────
REQUIRED_FIELDS = ["symbol", "signal", "price", "stop", "target1", "target2"]
NUMERIC_FIELDS  = ["price", "stop", "target1", "target2"]

def validate_signal(sig):
    if not isinstance(sig, dict):
        return False, "not a dict"
    for f in REQUIRED_FIELDS:
        if sig.get(f) is None:
            return False, f"missing '{f}'"
    for f in NUMERIC_FIELDS:
        try:
            v = float(sig[f])
            if v != v:  # NaN
                return False, f"NaN '{f}'"
        except (TypeError, ValueError):
            return False, f"non-numeric '{f}': {sig[f]!r}"
    if sig.get("signal") not in ("LONG", "SHORT"):
        return False, f"bad direction '{sig.get('signal')}'"
    return True, "ok"

# ── TELEGRAM LOW-LEVEL ────────────────────────────────────────
def tg_api(method, payload, timeout=5):
    if not TELEGRAM_TOKEN:
        return {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
                          json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Telegram {method} error: {e}")
        return {}

def signal_buttons(sig):
    sym = sig.get("symbol", "?")
    direction = sig.get("signal", "?")
    return {"inline_keyboard": [
        [{"text": f"✅ Live {direction}", "callback_data": f"confirm|{sym}|{direction}"}],
        [
            {"text": "📝 Paper", "callback_data": f"paper|{sym}|{direction}"},
            {"text": "⏸ Skip",   "callback_data": f"skip|{sym}|{direction}"},
        ],
        [
            {"text": "📊 Today",    "callback_data": "today"},
            {"text": "📈 Outcomes", "callback_data": "outcomes"},
        ],
    ]}

def resolved_buttons():
    """Buttons shown after user acted or trade resolved."""
    return {"inline_keyboard": [[
        {"text": "📊 Today",    "callback_data": "today"},
        {"text": "📈 Outcomes", "callback_data": "outcomes"},
        {"text": "📋 Journal",  "callback_data": "journal"},
    ]]}

def menu_buttons():
    return {"inline_keyboard": [
        [{"text": "📊 Today",    "callback_data": "today"},
         {"text": "📋 Journal",  "callback_data": "journal"}],
        [{"text": "📈 Outcomes", "callback_data": "outcomes"},
         {"text": "⏸ Skipped",   "callback_data": "skipped"}],
        [{"text": "⚙️ Status",   "callback_data": "status"},
         {"text": "📖 Help",     "callback_data": "help"}],
    ]}

# ── MESSAGE FORMATTERS ────────────────────────────────────────
def format_signal_body(sig, ana=None):
    """The shared body — fast alert if ana is None, enriched if ana present."""
    dir_emoji = "📈" if sig.get("signal") == "LONG" else "📉"
    cct_txt = f"✓ {sig.get('mins_to_close')}m to close" if sig.get("cct_open") else "—"
    icc_txt = "✓" if sig.get("icc") else "—"
    fvg_txt = "✓" if sig.get("fvg") else "—"

    body = (
        f"{dir_emoji} *{sig.get('symbol')} — {sig.get('signal')}*"
        f"{'  ⚡' if not ana else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Tier:* {sig.get('tier')}  |  *Score:* {sig.get('score')}/100\n"
        f"*Session:* {sig.get('session')}  |  *TF:* {sig.get('tf')}m\n"
        f"*Combo:* {sig.get('combo')}\n\n"
        f"*Entry:* `{sig.get('price')}`\n"
        f"*Stop:*  `{sig.get('stop')}`\n"
        f"*TP1:*   `{sig.get('target1')}`\n"
        f"*TP2:*   `{sig.get('target2')}`\n\n"
        f"*TFC:* 4H {sig.get('tfc_4h')} / 1H {sig.get('tfc_1h')} / 15m {sig.get('tfc_15')}\n"
        f"*ICC:* {icc_txt}  |  *FVG:* {fvg_txt}  |  *Lvl:* {sig.get('near_level')}\n"
        f"*CCT:* {cct_txt}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    if ana:
        v = ana.get("verdict", "REVIEW")
        v_emoji = {"BUY": "✅", "SELL": "✅", "WAIT": "⏸", "REVIEW": "⚠️"}.get(v, "•")
        body += (f"\n{v_emoji} *Claude: {v}* ({ana.get('confidence')})\n"
                 f"_{ana.get('key_factor', '')}_\n\n"
                 f"{ana.get('reasoning', '')}")
    else:
        body += "\n🧠 _Claude analysis loading..._"
    return body

def format_mode_stamp(trade):
    mode = trade.get("mode", "pending")
    if mode == "pending":
        return ""
    emoji = {"live": "✅", "paper": "📝", "skipped": "⏸"}.get(mode, "•")
    label = {"live": "LIVE", "paper": "PAPER", "skipped": "SKIPPED"}.get(mode, mode.upper())
    try:
        ts = datetime.fromisoformat(trade.get("action_time", trade["entry_time"])).strftime("%H:%M:%S")
    except Exception:
        ts = "??:??"
    return f"\n\n{emoji} *{label}* at {ts}"

def format_outcome_footer(trade):
    result = trade.get("result")
    if not result:
        return ""
    emoji = {"TP1": "🎯", "TP2": "🎯🎯", "STOP": "🛑", "TIMEOUT": "⏱"}.get(result, "•")
    try:
        rt = datetime.fromisoformat(trade["result_time"]).strftime("%m/%d %H:%M UTC")
    except Exception:
        rt = str(trade.get("result_time", ""))
    mfe = trade.get("mfe", 0) or 0
    mae = trade.get("mae", 0) or 0
    return (f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} *OUTCOME: {result}*\n"
            f"Resolved: `{rt}`\n"
            f"MFE: `{mfe:+.2f}`  |  MAE: `{mae:+.2f}`")

def render_trade(trade):
    """Single source of truth for message content. Builds from trade state."""
    return (format_signal_body(trade["sig"], trade.get("ana"))
            + format_mode_stamp(trade)
            + format_outcome_footer(trade))

def buttons_for_trade(trade):
    if trade.get("mode") == "pending" and not trade.get("result"):
        return signal_buttons(trade["sig"])
    return resolved_buttons()

# ── TELEGRAM SEND/EDIT ────────────────────────────────────────
def send_fast_alert(sig):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram not configured")
        return 0
    resp = tg_api("sendMessage", {
        "chat_id":      TELEGRAM_CHAT_ID,
        "text":         format_signal_body(sig, ana=None),
        "parse_mode":   "Markdown",
        "reply_markup": signal_buttons(sig),
    })
    return resp.get("result", {}).get("message_id", 0)

def send_text(text, buttons=None):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if buttons:
        payload["reply_markup"] = buttons
    resp = tg_api("sendMessage", payload)
    return resp.get("result", {}).get("message_id", 0)

def edit_message(message_id, new_text, buttons=None):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id,
               "text": new_text, "parse_mode": "Markdown"}
    if buttons is not None:
        payload["reply_markup"] = buttons
    tg_api("editMessageText", payload)

def answer_callback(callback_id, text="", alert=False):
    tg_api("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text, "show_alert": alert,
    })

# ── CLAUDE ────────────────────────────────────────────────────
def analyze(sig):
    if not CLAUDE_ENABLED or not ANTHROPIC_API_KEY:
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude disabled or no key",
                "reasoning": "Trade the signal on its own merits."}
    prompt = f"""You are reviewing a live futures trade signal from a Strat + ICC + CCT system.

ROLE: LENIENT. Trust the scoring. Approve most A+ and B setups. Only flag WAIT if there's a clear red flag.

SIGNAL:
 • {sig.get('symbol')} {sig.get('signal')} — {sig.get('combo')}
 • Tier {sig.get('tier')} | Score {sig.get('score')}/100 | {sig.get('session')} | {sig.get('tf')}m
 • Entry {sig.get('price')} | Stop {sig.get('stop')} | TP1 {sig.get('target1')} | TP2 {sig.get('target2')}
 • TFC: 4H {sig.get('tfc_4h')} / 1H {sig.get('tfc_1h')} / 15m {sig.get('tfc_15')}
 • ICC: {sig.get('icc')} | FVG: {sig.get('fvg')} | Near: {sig.get('near_level')}
 • CCT: {sig.get('cct_open')} ({sig.get('mins_to_close')}m to close) | ATR: {sig.get('atr')}

Return ONLY valid JSON:
{{"verdict":"BUY|SELL|WAIT","confidence":"HIGH|MEDIUM|LOW","key_factor":"one sentence","reasoning":"2 sentences max"}}"""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 250,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=CLAUDE_TIMEOUT)
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        return json.loads(text)
    except requests.exceptions.Timeout:
        log.warning(f"Claude timeout after {CLAUDE_TIMEOUT}s")
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude timed out",
                "reasoning": "Trade on your own read."}
    except Exception as e:
        log.error(f"Claude error: {e}")
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude API error",
                "reasoning": f"Review manually. ({str(e)[:100]})"}

# ── SIGNAL PROCESSING ─────────────────────────────────────────
def process_signal_async(sig):
    """Fast alert → record → Claude → enrich. Runs in a thread."""
    try:
        ok, reason = validate_signal(sig)
        if not ok:
            log.error(f"Signal rejected: {reason} | {json.dumps(sig)[:200]}")
            return

        msg_id = send_fast_alert(sig)
        if not msg_id:
            log.error("Fast alert failed — aborting")
            return
        log.info(f"Fast alert sent, message_id={msg_id}")

        trade_id = str(msg_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        with STATE_LOCK:
            TRACKING[trade_id] = {
                "trade_id":     trade_id,
                "message_id":   msg_id,
                "sig":          sig,
                "ana":          None,
                "mode":         "pending",
                "entry_time":   now_iso,
                "action_time":  None,
                "result":       None,
                "result_time":  None,
                "tp1_hit_time": None,
                "mfe":          0.0,
                "mae":          0.0,
                "last_checked": None,
            }
            _save_state_unlocked()

        ana = analyze(sig)
        log.info(f"Claude: {ana.get('verdict')} ({ana.get('confidence')})")

        with STATE_LOCK:
            t = TRACKING.get(trade_id)
            if not t:
                return
            t["ana"] = ana
            trade_copy = deepcopy(t)
            _save_state_unlocked()

        edit_message(msg_id, render_trade(trade_copy), buttons_for_trade(trade_copy))
        log.info(f"Message {msg_id} enriched")
    except Exception as e:
        log.exception(f"process_signal_async error: {e}")

# ── OUTCOME TRACKER ───────────────────────────────────────────
def _fetch_history(yf_symbol):
    """Fetch recent 1m bars. Returns DataFrame or None on any failure."""
    try:
        t = yf.Ticker(yf_symbol)
        hist = t.history(period="2d", interval="1m", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        if hist.index.tz is None:
            hist.index = hist.index.tz_localize("UTC")
        else:
            hist.index = hist.index.tz_convert("UTC")
        return hist
    except Exception as e:
        log.warning(f"yfinance fetch failed for {yf_symbol}: {e}")
        return None

def _check_trade(trade, hist):
    """Update trade in place. Returns True if newly resolved this call."""
    sig = trade["sig"]
    try:
        entry_ts = pd.Timestamp(trade["entry_time"])
        if entry_ts.tz is None:
            entry_ts = entry_ts.tz_localize("UTC")
        else:
            entry_ts = entry_ts.tz_convert("UTC")
    except Exception as e:
        log.warning(f"Bad entry_time on {trade.get('trade_id')}: {e}")
        return False

    post = hist[hist.index > entry_ts]
    direction = sig["signal"]
    entry = float(sig["price"])
    stop  = float(sig["stop"])
    tp1   = float(sig["target1"])
    tp2   = float(sig["target2"])
    mfe = float(trade.get("mfe", 0) or 0)
    mae = float(trade.get("mae", 0) or 0)

    resolved, resolved_time = None, None

    for idx, row in post.iterrows():
        try:
            hi = float(row["High"])
            lo = float(row["Low"])
        except Exception:
            continue

        if direction == "LONG":
            mfe = max(mfe, hi - entry)
            mae = min(mae, lo - entry)
            if lo <= stop:
                resolved, resolved_time = "STOP", idx; break
            if hi >= tp2:
                resolved, resolved_time = "TP2", idx; break
            if hi >= tp1 and not trade.get("tp1_hit_time"):
                trade["tp1_hit_time"] = idx.isoformat()
        else:  # SHORT
            mfe = max(mfe, entry - lo)
            mae = min(mae, entry - hi)
            if hi >= stop:
                resolved, resolved_time = "STOP", idx; break
            if lo <= tp2:
                resolved, resolved_time = "TP2", idx; break
            if lo <= tp1 and not trade.get("tp1_hit_time"):
                trade["tp1_hit_time"] = idx.isoformat()

    trade["mfe"] = round(mfe, 4)
    trade["mae"] = round(mae, 4)
    trade["last_checked"] = datetime.now(timezone.utc).isoformat()

    if resolved:
        trade["result"] = resolved
        trade["result_time"] = (resolved_time.isoformat()
                                if hasattr(resolved_time, "isoformat")
                                else str(resolved_time))
        return True

    # Timeout check
    age = datetime.now(timezone.utc) - entry_ts.to_pydatetime()
    if age > timedelta(hours=TRADE_TIMEOUT_HRS):
        if trade.get("tp1_hit_time"):
            trade["result"] = "TP1"
            trade["result_time"] = trade["tp1_hit_time"]
        else:
            trade["result"] = "TIMEOUT"
            trade["result_time"] = datetime.now(timezone.utc).isoformat()
        return True
    return False

def _outcome_tick():
    if not OUTCOME_TRACKING_AVAILABLE:
        return

    with STATE_LOCK:
        active_ids = [tid for tid, t in TRACKING.items() if t.get("result") is None]

    if not active_ids:
        return

    # Group by yfinance symbol so we fetch each once
    by_yf = {}
    with STATE_LOCK:
        for tid in active_ids:
            t = TRACKING.get(tid)
            if not t:
                continue
            tv = t["sig"].get("symbol")
            yfs = YF_SYMBOL_MAP.get(tv)
            if not yfs:
                if tv not in UNMAPPED_WARNED:
                    log.warning(f"No yfinance mapping for '{tv}' — add to YF_SYMBOL_MAP "
                                f"for outcome tracking. Trade still logged.")
                    UNMAPPED_WARNED.add(tv)
                continue
            by_yf.setdefault(yfs, []).append(tid)

    if not by_yf:
        return

    # Fetch outside the lock
    hist_cache = {yfs: _fetch_history(yfs) for yfs in by_yf}

    for yfs, tids in by_yf.items():
        hist = hist_cache.get(yfs)
        if hist is None:
            continue
        for tid in tids:
            trade_copy = None
            newly_resolved = False
            try:
                with STATE_LOCK:
                    t = TRACKING.get(tid)
                    if not t or t.get("result") is not None:
                        continue
                    newly_resolved = _check_trade(t, hist)
                    if newly_resolved:
                        trade_copy = deepcopy(t)
                        _save_state_unlocked()
            except Exception as e:
                log.error(f"Check trade {tid} error: {e}")
                continue

            if newly_resolved and trade_copy:
                try:
                    edit_message(trade_copy["message_id"],
                                 render_trade(trade_copy),
                                 resolved_buttons())
                    log.info(f"Trade {tid} resolved: {trade_copy['result']}")
                except Exception as e:
                    log.error(f"Outcome edit failed for {tid}: {e}")

def outcome_tracker_loop():
    log.info(f"Outcome tracker running (every {OUTCOME_POLL_SEC}s, "
             f"timeout {TRADE_TIMEOUT_HRS}h)")
    while True:
        try:
            _outcome_tick()
        except Exception as e:
            log.exception(f"Tracker loop error: {e}")
        time.sleep(OUTCOME_POLL_SEC)

# ── REPORTING ─────────────────────────────────────────────────
def _all_trades():
    with STATE_LOCK:
        return [deepcopy(t) for t in TRACKING.values()]

def today_stats():
    today = datetime.now(timezone.utc).date()
    all_t = _all_trades()
    today_t = []
    for t in all_t:
        try:
            if datetime.fromisoformat(t["entry_time"]).date() == today:
                today_t.append(t)
        except Exception:
            continue
    if not today_t:
        return "*📊 Today's activity*\n\nNo signals yet today."
    live   = [t for t in today_t if t["mode"] == "live"]
    paper  = [t for t in today_t if t["mode"] == "paper"]
    skip   = [t for t in today_t if t["mode"] == "skipped"]
    pend   = [t for t in today_t if t["mode"] == "pending"]
    wins   = sum(1 for t in today_t if t.get("result") in ("TP1", "TP2"))
    losses = sum(1 for t in today_t if t.get("result") == "STOP")
    lines = [f"*📊 Today* ({today.isoformat()})\n",
             f"✅ Live:    {len(live)}",
             f"📝 Paper:   {len(paper)}",
             f"⏸ Skipped: {len(skip)}",
             f"⏳ Pending: {len(pend)}"]
    if wins + losses:
        wr = wins / (wins + losses) * 100
        lines.append(f"\n*Resolved:* {wins}W / {losses}L ({wr:.0f}%)")
    return "\n".join(lines)

def recent_journal():
    trades = [t for t in _all_trades() if t["mode"] in ("live", "paper")]
    if not trades:
        return "*📋 Journal*\n\nNo confirmed trades yet."
    trades.sort(key=lambda t: t.get("entry_time", ""), reverse=True)
    lines = ["*📋 Recent confirmed* (last 10)\n"]
    for t in trades[:10]:
        s = t["sig"]
        try:
            ts = datetime.fromisoformat(t["entry_time"]).strftime("%m/%d %H:%M")
        except Exception:
            ts = "??"
        me = "✅" if t["mode"] == "live" else "📝"
        r = t.get("result") or "open"
        lines.append(f"{me} `{ts}` {s.get('symbol')} *{s.get('signal')}* "
                     f"@ {s.get('price')} → {r}")
    return "\n".join(lines)

def recent_skipped():
    trades = [t for t in _all_trades() if t["mode"] == "skipped"]
    if not trades:
        return "*⏸ Skipped*\n\nNone yet."
    trades.sort(key=lambda t: t.get("entry_time", ""), reverse=True)
    lines = ["*⏸ Recent skipped* (would-be outcomes tracked)\n"]
    for t in trades[:10]:
        s = t["sig"]
        try:
            ts = datetime.fromisoformat(t["entry_time"]).strftime("%m/%d %H:%M")
        except Exception:
            ts = "??"
        r = t.get("result") or "open"
        lines.append(f"⏸ `{ts}` {s.get('symbol')} *{s.get('signal')}* "
                     f"@ {s.get('price')} → would-be: *{r}*")
    return "\n".join(lines)

def outcomes_summary():
    trades = _all_trades()
    resolved = [t for t in trades if t.get("result") in ("TP1", "TP2", "STOP", "TIMEOUT")]
    if not resolved:
        return "*📈 Outcomes*\n\nNo resolved trades yet."

    def _wr(arr):
        w = sum(1 for t in arr if t.get("result") in ("TP1", "TP2"))
        l = sum(1 for t in arr if t.get("result") == "STOP")
        return w, l, (w/(w+l)*100) if (w+l) else 0

    lines = ["*📈 Outcome breakdown*\n"]
    for mode in ("live", "paper", "skipped"):
        arr = [t for t in resolved if t.get("mode") == mode]
        if not arr:
            continue
        w, l, wr = _wr(arr)
        to = sum(1 for t in arr if t.get("result") == "TIMEOUT")
        me = {"live": "✅", "paper": "📝", "skipped": "⏸"}[mode]
        lines.append(f"{me} *{mode.upper()}* ({len(arr)}): {w}W / {l}L / {to}TO → {wr:.0f}% WR")

    lines.append("")
    for tier in ("A+", "B"):
        arr = [t for t in resolved if t["sig"].get("tier") == tier]
        if not arr:
            continue
        w, l, wr = _wr(arr)
        lines.append(f"*{tier} tier* ({len(arr)}): {w}W / {l}L → {wr:.0f}% WR")
    return "\n".join(lines)

def help_text():
    return (
        "*📖 Kronus AI v5 — Help*\n\n"
        "Alert fires in ~3s. Claude verdict updates it a few seconds later.\n\n"
        "*Actions on each signal:*\n"
        "• ✅ *Live* — real trade, logged & outcome tracked\n"
        "• 📝 *Paper* — practice trade, logged & tracked\n"
        "• ⏸ *Skip* — pass, but *outcome still tracked* (see what you missed)\n\n"
        "Every signal is auto-tracked for TP1 / TP2 / STOP / TIMEOUT regardless of "
        "your action. This builds the dataset for Phase 2 (broker auto-execution). "
        "MFE / MAE recorded too.\n\n"
        "*Commands:* `/menu` `/today` `/journal` `/skipped` `/outcomes` `/status` `/help`"
    )

def status_text():
    with STATE_LOCK:
        n = len(TRACKING)
        counts = {"pending": 0, "live": 0, "paper": 0, "skipped": 0}
        open_ = 0
        for t in TRACKING.values():
            counts[t.get("mode", "pending")] = counts.get(t.get("mode", "pending"), 0) + 1
            if t.get("result") is None:
                open_ += 1
    tracker = ("✅ yfinance" if OUTCOME_TRACKING_AVAILABLE
               else "❌ install yfinance+pandas")
    return (
        "*⚙️ Kronus AI — Status*\n\n"
        f"Version: *v5 (outcomes + paper)*\n"
        f"Claude: {'✅ ' + CLAUDE_MODEL if (CLAUDE_ENABLED and ANTHROPIC_API_KEY) else '❌'}\n"
        f"Telegram: {'✅' if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else '❌'}\n"
        f"Tracker: {tracker}\n"
        f"Poll: {OUTCOME_POLL_SEC}s | Timeout: {TRADE_TIMEOUT_HRS}h | State: `{STATE_FILE}`\n\n"
        f"Total: *{n}*  |  Open: *{open_}*\n"
        f"Live: {counts['live']}  |  Paper: {counts['paper']}  |  "
        f"Skip: {counts['skipped']}  |  Pending: {counts['pending']}"
    )

# ── ROUTES ────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    with STATE_LOCK:
        n = len(TRACKING)
        open_ = sum(1 for t in TRACKING.values() if t.get("result") is None)
    return jsonify({
        "status": "running",
        "bot": "Kronus AI v5 (outcomes + paper mode)",
        "claude": "enabled" if (CLAUDE_ENABLED and ANTHROPIC_API_KEY) else "disabled",
        "claude_model": CLAUDE_MODEL,
        "telegram": "enabled" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "disabled",
        "outcome_tracker": OUTCOME_TRACKING_AVAILABLE,
        "tracked": n, "open": open_,
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.get_data(as_text=True)
        log.info(f"Webhook in: {raw[:250]}")
        sig = json.loads(raw)
    except Exception as e:
        log.error(f"JSON parse: {e}")
        return jsonify({"error": "invalid JSON"}), 400
    if sig.get("secret") != WEBHOOK_SECRET:
        log.warning("Invalid secret")
        return jsonify({"error": "unauthorized"}), 401
    ok, reason = validate_signal(sig)
    if not ok:
        log.warning(f"Invalid signal: {reason}")
        return jsonify({"error": f"bad signal: {reason}"}), 400
    threading.Thread(target=process_signal_async, args=(sig,), daemon=True).start()
    return jsonify({"status": "accepted"}), 200

@app.route("/telegram", methods=["POST"])
def telegram_update():
    upd = request.get_json(silent=True) or {}
    log.info(f"TG update: {json.dumps(upd)[:300]}")

    if "callback_query" in upd:
        cb     = upd["callback_query"]
        cb_id  = cb.get("id", "")
        data   = cb.get("data", "")
        msg    = cb.get("message", {})
        msg_id = msg.get("message_id", 0)
        action = data.split("|")[0]
        trade_id = str(msg_id)

        def set_mode(new_mode, short_msg):
            """Atomically set mode (once) and re-render the message."""
            with STATE_LOCK:
                t = TRACKING.get(trade_id)
                if not t:
                    return None, "missing"
                if t.get("mode") != "pending":
                    return None, "already_acted"
                t["mode"] = new_mode
                t["action_time"] = datetime.now(timezone.utc).isoformat()
                trade_copy = deepcopy(t)
                _save_state_unlocked()
            answer_callback(cb_id, short_msg)
            try:
                edit_message(msg_id, render_trade(trade_copy), buttons_for_trade(trade_copy))
            except Exception as e:
                log.error(f"Edit after mode set failed: {e}")
            return trade_copy, "ok"

        if action == "confirm":
            res, why = set_mode("live", "✅ Live trade logged")
            if res is None:
                answer_callback(cb_id, "⚠️ Already acted on", alert=True)
        elif action == "paper":
            res, why = set_mode("paper", "📝 Paper trade logged")
            if res is None:
                answer_callback(cb_id, "⚠️ Already acted on", alert=True)
        elif action == "skip":
            res, why = set_mode("skipped", "⏸ Skipped — outcome still tracked")
            if res is None:
                answer_callback(cb_id, "⚠️ Already acted on", alert=True)
        elif action == "today":
            answer_callback(cb_id); send_text(today_stats(), menu_buttons())
        elif action == "journal":
            answer_callback(cb_id); send_text(recent_journal(), menu_buttons())
        elif action == "skipped":
            answer_callback(cb_id); send_text(recent_skipped(), menu_buttons())
        elif action == "outcomes":
            answer_callback(cb_id); send_text(outcomes_summary(), menu_buttons())
        elif action == "status":
            answer_callback(cb_id); send_text(status_text(), menu_buttons())
        elif action == "help":
            answer_callback(cb_id); send_text(help_text(), menu_buttons())
        else:
            answer_callback(cb_id, "Unknown action")
        return jsonify({"ok": True})

    if "message" in upd:
        text = upd["message"].get("text", "").strip().lower()
        if text in ("/menu", "/start"):
            send_text("*📊 Kronus AI — Main Menu*\n\nPick an option:", menu_buttons())
        elif text == "/today":    send_text(today_stats(),      menu_buttons())
        elif text == "/journal":  send_text(recent_journal(),   menu_buttons())
        elif text == "/skipped":  send_text(recent_skipped(),   menu_buttons())
        elif text == "/outcomes": send_text(outcomes_summary(), menu_buttons())
        elif text == "/status":   send_text(status_text(),      menu_buttons())
        elif text == "/help":     send_text(help_text(),        menu_buttons())
        return jsonify({"ok": True})

    return jsonify({"ok": True})

@app.route("/outcomes", methods=["GET"])
def outcomes_json():
    """Dump full state as JSON for external analysis / CSV export."""
    with STATE_LOCK:
        return jsonify({"count": len(TRACKING),
                        "trades": list(TRACKING.values())})

@app.route("/setup_telegram", methods=["GET"])
def setup_telegram():
    if not TELEGRAM_TOKEN:
        return jsonify({"error": "TELEGRAM_TOKEN not set"}), 400
    if not PUBLIC_URL:
        return jsonify({"error": "PUBLIC_URL env var not set"}), 400
    target = f"{PUBLIC_URL.rstrip('/')}/telegram"
    resp = tg_api("setWebhook", {"url": target,
                                 "allowed_updates": ["message", "callback_query"]})
    return jsonify({"target": target, "telegram_response": resp})

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"pong": True, "ts": datetime.now(timezone.utc).isoformat()})

@app.route("/test", methods=["GET"])
def test():
    fake = {
        "secret": WEBHOOK_SECRET, "symbol": "MGC1!", "tf": "15", "session": "NY-AM",
        "tier": "A+", "score": 85, "signal": "LONG", "combo": "2-2 Bull",
        "price": 2650.50, "stop": 2648.00, "target1": 2654.25, "target2": 2656.75,
        "tfc_4h": "BULL", "tfc_1h": "BULL", "tfc_15": "BULL",
        "icc": True, "fvg": True, "near_level": "PDH",
        "cct_open": True, "mins_to_close": 18, "atr": 1.67,
    }
    threading.Thread(target=process_signal_async, args=(fake,), daemon=True).start()
    return jsonify({"test": "dispatched"})

# ── STARTUP ───────────────────────────────────────────────────
load_state()
if OUTCOME_TRACKING_AVAILABLE:
    threading.Thread(target=outcome_tracker_loop, daemon=True).start()
else:
    log.warning("Outcome tracker NOT started (yfinance missing). "
                "Signals still logged; outcomes will stay 'open'.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Kronus AI v5 starting on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
