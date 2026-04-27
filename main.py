"""
KRONUS AI — main.py v8
Paired with Pine v9.6.
Changes from v7:
  - Version strings updated to v9.6 throughout
  - Entry price now uses directional `entry` field (slippage-adjusted) from Pine
  - Signal card shows enter_ok (ENTER vs SKIP from bonus score)
  - Dual CCT window support — card shows pit vs electronic window
  - Claude prompt updated to v9.6 logic (dual CCT, enter_ok, PD filter off default)
  - Test payload updated with all v9.6 fields
  - Status route reports Pine v9.6
  - Removed unused/dead code and redundant comments
"""
import os, json, logging, threading, time
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)
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
TRADE_TIMEOUT_HRS = int(os.environ.get("TRADE_TIMEOUT_HRS", "4"))
PURGE_DAYS        = int(os.environ.get("PURGE_DAYS", "30"))
FILTER_SESSIONS   = os.environ.get("FILTER_SESSIONS", "true").lower() == "true"
ALLOWED_SESSIONS  = [s.strip().upper() for s in os.environ.get("ALLOWED_SESSIONS", "NY-AM,LONDON").split(",") if s.strip()]

# ── STATE ─────────────────────────────────────────────────────
STATE_LOCK      = threading.RLock()
TRACKING        = {}
CHECKLIST_LOCK  = threading.Lock()
CHECKLIST_STATE = {}

CHECKLIST_ITEMS = [
    ("c0", "Preferred session (London/NY)"),
    ("c1", "HTF bias clear (2+ of 3 TFs)"),
    ("c2", "Liquidity sweep at confirmed pivot"),
    ("c3", "Displacement + volume (>=1.3x avg)"),
    ("c4", "Clean FVG formed (size filtered)"),
    ("c5", "FVG retrace (wick tap, close respects)"),
    ("c6", "LTF Strat confirmation"),
    ("c7", "Clear liquidity target ahead"),
]

# ── PERSISTENCE ───────────────────────────────────────────────
def save_state():
    with STATE_LOCK:
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"tracking": TRACKING}, f, default=str)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log.error(f"Save failed: {e}")

def load_state():
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE) as f:
            data = json.load(f)
        cutoff = datetime.now(timezone.utc) - timedelta(days=PURGE_DAYS)
        kept = {}
        for tid, t in (data.get("tracking") or {}).items():
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
        log.info(f"Loaded {len(kept)} trades")
    except Exception as e:
        log.error(f"Load failed: {e}")

# ── VALIDATION ────────────────────────────────────────────────
def validate_signal(sig):
    if not isinstance(sig, dict):
        return False, "not a dict"
    for f in ["symbol", "signal", "entry", "stop", "target1", "target2"]:
        if sig.get(f) is None:
            return False, f"missing {f}"
    for f in ["entry", "stop", "target1", "target2"]:
        try:
            v = float(sig[f])
            if v != v:
                return False, f"NaN {f}"
        except (TypeError, ValueError):
            return False, f"bad {f}"
    if sig.get("signal") not in ("LONG", "SHORT"):
        return False, "bad direction"
    return True, "ok"

# ── TELEGRAM ──────────────────────────────────────────────────
def tg(method, payload, timeout=5):
    if not TELEGRAM_TOKEN:
        return {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"tg {method}: {e}")
        return {}

def send_text(text, buttons=None):
    p = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if buttons:
        p["reply_markup"] = buttons
    return tg("sendMessage", p).get("result", {}).get("message_id", 0)

def edit_msg(mid, text, buttons=None):
    p = {"chat_id": TELEGRAM_CHAT_ID, "message_id": mid, "text": text, "parse_mode": "Markdown"}
    if buttons is not None:
        p["reply_markup"] = buttons
    tg("editMessageText", p)

def answer_cb(cb_id, text="", alert=False):
    tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text, "show_alert": alert})

# ── BUTTONS ───────────────────────────────────────────────────
def signal_btns(sig):
    s, d = sig.get("symbol", "?"), sig.get("signal", "?")
    return {"inline_keyboard": [
        [{"text": f"✅ Live {d}", "callback_data": f"confirm|{s}|{d}"}],
        [{"text": "📝 Paper", "callback_data": f"paper|{s}|{d}"}, {"text": "⏸ Skip", "callback_data": f"skip|{s}|{d}"}],
        [{"text": "📊 Today", "callback_data": "today"}, {"text": "📈 Stats", "callback_data": "stats"}],
        [{"text": "🔥 Checklist", "callback_data": "open_checklist"}],
    ]}

def resolved_btns():
    return {"inline_keyboard": [[
        {"text": "📊 Today",   "callback_data": "today"},
        {"text": "📈 Stats",   "callback_data": "stats"},
        {"text": "📋 Journal", "callback_data": "journal"},
    ]]}

def menu_btns():
    return {"inline_keyboard": [
        [{"text": "📊 Today",     "callback_data": "today"},   {"text": "📋 Journal", "callback_data": "journal"}],
        [{"text": "📈 Stats",     "callback_data": "stats"},   {"text": "⏸ Skipped",  "callback_data": "skipped"}],
        [{"text": "🔥 Checklist", "callback_data": "open_checklist"}, {"text": "⚙️ Status", "callback_data": "status"}],
    ]}

# ── SIGNAL CARD ───────────────────────────────────────────────
def fmt_card(sig, ana=None):
    d        = "📈" if sig.get("signal") == "LONG" else "📉"
    max_c    = sig.get("max_conditions", 7)
    conds    = sig.get("conditions_met", "?")
    tier     = sig.get("tier", "?")
    raw_tier = sig.get("raw_tier", tier)
    icc_cct  = sig.get("icc_cct_confluence", False)
    enter_ok = sig.get("enter_ok", False)

    tier_line = tier
    if raw_tier != tier and raw_tier == "B+":
        tier_line = f"{tier} _(B+ upgraded via CCT)_"

    # ENTER vs SKIP badge
    enter_badge = "🟢 *ENTER*" if enter_ok else "🟡 *SKIP*"

    try:
        rf = float(sig.get("rvol"))
        rvol_txt = ("✦ " if rf >= 1.5 else "⚠ " if rf < 1.3 else "") + f"{rf:.2f}x"
    except (TypeError, ValueError):
        rvol_txt = "—"

    # CCT — show which window is active
    if sig.get("cct_open"):
        mins = sig.get("mins_to_close", "?")
        cct_line = f"✓ {mins}m to close"
    else:
        cct_line = "—"

    conf_line = "\n⭐ *ICC+CCT CONFLUENCE — Displacement at close*\n" if icc_cct else ""

    mss_active = sig.get("mss_active", False)
    pd_zone    = sig.get("pd_zone", "—")
    atr_exp    = sig.get("atr_expanding")

    mss_txt = "🔵 ACTIVE" if mss_active else "—"
    if pd_zone not in ("—", None):
        correct = (pd_zone == "Discount" and sig.get("signal") == "LONG") or \
                  (pd_zone == "Premium"  and sig.get("signal") == "SHORT")
        pd_txt = ("✅ " if correct else "⚠️ ") + pd_zone
    else:
        pd_txt = "—"
    atr_txt = "📈 Expanding" if atr_exp is True else "📉 Contracting" if atr_exp is False else "—"

    body = (
        f"{d} *{sig.get('symbol')} — {sig.get('signal')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Tier:* {tier_line}  |  *Conds:* {conds}/{max_c}  |  {enter_badge}\n"
        f"*Session:* {sig.get('session')}  |  *TF:* {sig.get('tf')}m\n"
        f"*Combo:* {sig.get('combo')}\n\n"
        f"*Entry:* `{sig.get('entry')}`\n"
        f"*Stop:*  `{sig.get('stop')}`\n"
        f"*TP1:*   `{sig.get('target1')}`\n"
        f"*TP2:*   `{sig.get('target2')}`\n\n"
        f"*RVOL:* {rvol_txt}  |  *Sweep:* {'✓ pivot' if sig.get('cond_liq_sweep') else '—'}  |  *Lvl:* {sig.get('near_level', '—')}\n"
        f"*CCT:* {cct_line}{conf_line}\n"
        f"*MSS:* {mss_txt}  |  *PD:* {pd_txt}  |  *ATR:* {atr_txt}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    if ana:
        v = ana.get("verdict", "REVIEW")
        e = {"BUY": "✅", "SELL": "✅", "WAIT": "⏸", "REVIEW": "⚠️"}.get(v, "•")
        body += f"\n{e} *Claude: {v}* ({ana.get('confidence')})\n_{ana.get('key_factor', '')}_\n{ana.get('reasoning', '')}"
    else:
        body += "\n🧠 _Claude loading..._"
    return body

def fmt_mode(trade):
    mode = trade.get("mode", "pending")
    if mode == "pending":
        return ""
    e  = {"live": "✅", "paper": "📝", "skipped": "⏸"}.get(mode, "•")
    try:
        ts = datetime.fromisoformat(trade.get("action_time", trade["entry_time"])).strftime("%H:%M:%S")
    except Exception:
        ts = "??"
    return f"\n\n{e} *{mode.upper()}* at {ts}"

def fmt_result(trade):
    r = trade.get("result")
    if not r:
        return ""
    e = {"TP1": "🎯", "TP2": "🎯🎯", "STOP": "🛑", "TIMEOUT": "⏱"}.get(r, "•")
    try:
        rt = datetime.fromisoformat(trade["result_time"]).strftime("%m/%d %H:%M UTC")
    except Exception:
        rt = str(trade.get("result_time", ""))
    mfe = trade.get("mfe", 0) or 0
    mae = trade.get("mae", 0) or 0
    return f"\n━━━━━━━━━━━━━━━━━━━━\n{e} *{r}*  |  Resolved: `{rt}`\nMFE: `{mfe:+.2f}`  MAE: `{mae:+.2f}`"

def render(trade):
    return fmt_card(trade["sig"], trade.get("ana")) + fmt_mode(trade) + fmt_result(trade)

def btns_for(trade):
    return signal_btns(trade["sig"]) if trade.get("mode") == "pending" and not trade.get("result") else resolved_btns()

# ── CHECKLIST ─────────────────────────────────────────────────
def autofill(sig):
    return {
        "c0": sig.get("in_preferred_sess",  False),
        "c1": sig.get("cond_htf_bias",      False),
        "c2": sig.get("cond_liq_sweep",     False),
        "c3": sig.get("cond_displacement",  False),
        "c4": sig.get("cond_fvg_formed",    False),
        "c5": sig.get("cond_fvg_retrace",   False),
        "c6": sig.get("cond_ltf_confirm",   False),
        "c7": sig.get("cond_liq_target",    False),
        "_auto": True,
    }

def grade(state):
    s_on  = state.get("c0", False)
    other = sum(1 for k, _ in CHECKLIST_ITEMS[1:] if state.get(k, False))
    if other == 7 and s_on:                  return "A+", "Perfect — TAKE IT", "🔥"
    if other == 7 or (other == 6 and s_on):  return "A",  "Strong trade",       "✅"
    if other >= 5:                           return "B+", "Decent — size down", "⚠️"
    return "B", "Weak — SKIP", "❌"

def cl_header(state):
    n    = sum(1 for k, _ in CHECKLIST_ITEMS if state.get(k, False))
    auto = " _(auto)_" if state.get("_auto") else ""
    return f"🔥 *Setup Checklist* ({n}/8){auto}\n"

def cl_keys(state, mid):
    rows = [[{"text": ("✅" if state.get(k) else "⬜") + f"  {lbl}", "callback_data": f"cl_toggle|{mid}|{k}"}] for k, lbl in CHECKLIST_ITEMS]
    rows.append([
        {"text": "📊 Grade",  "callback_data": f"cl_grade|{mid}"},
        {"text": "🔄 Reset",  "callback_data": f"cl_reset|{mid}"},
    ])
    return {"inline_keyboard": rows}

def send_checklist(sig_or_state, is_state=False):
    state = sig_or_state if is_state else autofill(sig_or_state)
    res   = tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": cl_header(state), "parse_mode": "Markdown", "reply_markup": cl_keys(state, 0)})
    mid   = res.get("result", {}).get("message_id", 0)
    if mid:
        with CHECKLIST_LOCK:
            CHECKLIST_STATE[mid] = state
        edit_msg(mid, cl_header(state), cl_keys(state, mid))
    return mid

# ── CLAUDE ────────────────────────────────────────────────────
def analyze(sig):
    if not CLAUDE_ENABLED or not ANTHROPIC_API_KEY:
        return {"verdict": "REVIEW", "confidence": "N/A", "key_factor": "Claude off", "reasoning": "Trade on own read."}

    try:
        rvol_s = f"{float(sig.get('rvol', 0)):.2f}x"
    except (TypeError, ValueError):
        rvol_s = "N/A"

    tier     = sig.get("tier", "?")
    raw_t    = sig.get("raw_tier", tier)
    upgrade  = f" (upgraded from {raw_t} via ICC+CCT)" if raw_t != tier else ""
    icc_cct  = "YES ⭐" if sig.get("icc_cct_confluence") else "no"
    enter_ok = "YES — bonus conditions met" if sig.get("enter_ok") else "NO — SKIP signal"
    mss_txt  = "YES — structure confirmed flip" if sig.get("mss_active") else "no"
    pd_zone  = sig.get("pd_zone", "unknown")
    atr_txt  = "expanding (trending)" if sig.get("atr_expanding") is True else "contracting (chop risk)" if sig.get("atr_expanding") is False else "unknown"

    prompt = f"""Kronus AI v9.6 futures signal review. Gate logic:
C2 sweep anchored to confirmed pivot (len=2). C3 displacement needs body >1.1x avg AND RVOL>=1.3x. C5 FVG retrace needs wick tap + close respects level. FVGs below 0.1x ATR filtered as noise. B+ upgrades to A when ICC+CCT fires.
Dual CCT windows: metals pit close 12:30 CT and electronic close 15:00 CT.
MSS confirms sweep flipped structure. PD filter is currently OFF by default — pd_zone shown for context only unless filter re-enabled. ATR expansion flags trending vs contracting. enter_ok = bonus score (MSS + PD + ATR) met threshold.
Be lenient on A/A+ — only flag WAIT for a concrete red flag.

{sig.get('symbol')} {sig.get('signal')} | Tier {tier}{upgrade} | {sig.get('conditions_met','?')}/{sig.get('max_conditions',7)} conds | {sig.get('session')} {sig.get('tf')}m
Entry {sig.get('entry')} Stop {sig.get('stop')} TP1 {sig.get('target1')} TP2 {sig.get('target2')} ATR {sig.get('atr')} RVOL {rvol_s}
C1 HTF:{sig.get('cond_htf_bias')} C2 Sweep:{sig.get('cond_liq_sweep')} C3 Disp:{sig.get('cond_displacement')} C4 FVG:{sig.get('cond_fvg_formed')} C5 Retrace:{sig.get('cond_fvg_retrace')} C6 LTF:{sig.get('cond_ltf_confirm')} C7 Target:{sig.get('cond_liq_target')}
ICC+CCT: {icc_cct} | Enter OK: {enter_ok} | Near: {sig.get('near_level','none')} | CCT: {sig.get('cct_open')} ({sig.get('mins_to_close')}m) | Pref session: {sig.get('in_preferred_sess')}
MSS: {mss_txt} | PD zone: {pd_zone} | ATR state: {atr_txt}

Return ONLY JSON: {{"verdict":"BUY|SELL|WAIT","confidence":"HIGH|MEDIUM|LOW","key_factor":"one sentence","reasoning":"2 sentences"}}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
            timeout=CLAUDE_TIMEOUT,
        )
        r.raise_for_status()
        txt = r.json()["content"][0]["text"].strip()
        if txt.startswith("```"):
            txt = txt.split("```")[1].lstrip("json").strip().rstrip("`")
        return json.loads(txt)
    except requests.exceptions.Timeout:
        return {"verdict": "REVIEW", "confidence": "N/A", "key_factor": "Claude timed out", "reasoning": "Trade on own read."}
    except Exception as e:
        log.error(f"Claude: {e}")
        return {"verdict": "REVIEW", "confidence": "N/A", "key_factor": "Claude error", "reasoning": str(e)[:80]}

# ── SIGNAL FLOW ───────────────────────────────────────────────
def process(sig):
    try:
        msg_id = int(tg("sendMessage", {
            "chat_id":      TELEGRAM_CHAT_ID,
            "text":         fmt_card(sig),
            "parse_mode":   "Markdown",
            "reply_markup": signal_btns(sig),
        }).get("result", {}).get("message_id", 0))
        if not msg_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        trade = {
            "trade_id":    str(msg_id),
            "message_id":  msg_id,
            "sig":         sig,
            "ana":         None,
            "mode":        "pending",
            "entry_time":  now,
            "action_time": None,
            "result":      None,
            "result_time": None,
            "mfe":         0.0,
            "mae":         0.0,
        }
        with STATE_LOCK:
            TRACKING[str(msg_id)] = trade
            save_state()
        ana = analyze(sig)
        with STATE_LOCK:
            t = TRACKING.get(str(msg_id))
            if t:
                t["ana"] = ana
                tc = deepcopy(t)
                save_state()
        edit_msg(msg_id, render(tc), btns_for(tc))
        send_checklist(sig)
    except Exception as e:
        log.exception(f"process: {e}")

# ── TIMEOUT RESOLVER ──────────────────────────────────────────
def timeout_loop():
    log.info(f"Timeout loop running (every 60s, {TRADE_TIMEOUT_HRS}h limit)")
    while True:
        try:
            now = datetime.now(timezone.utc)
            with STATE_LOCK:
                open_trades = [deepcopy(t) for t in TRACKING.values() if not t.get("result")]
            for t in open_trades:
                try:
                    age = now - datetime.fromisoformat(t["entry_time"])
                    if age > timedelta(hours=TRADE_TIMEOUT_HRS):
                        with STATE_LOCK:
                            live = TRACKING.get(t["trade_id"])
                            if live and not live.get("result"):
                                live["result"]      = "TIMEOUT"
                                live["result_time"] = now.isoformat()
                                tc = deepcopy(live)
                                save_state()
                        edit_msg(tc["message_id"], render(tc), resolved_btns())
                        log.info(f"Trade {t['trade_id']} timed out")
                except Exception as e:
                    log.error(f"Timeout check {t.get('trade_id')}: {e}")
        except Exception as e:
            log.exception(f"Timeout loop: {e}")
        time.sleep(60)

# ── REPORTS ───────────────────────────────────────────────────
def all_trades():
    with STATE_LOCK:
        return [deepcopy(t) for t in TRACKING.values()]

def _trade_date(t):
    try:
        return datetime.fromisoformat(t["entry_time"]).date()
    except Exception:
        return None

def _fmt_ts(s):
    try:
        return datetime.fromisoformat(s).strftime("%m/%d %H:%M")
    except Exception:
        return "??"

def _wr(arr):
    w = sum(1 for t in arr if t.get("result") in ("TP1", "TP2"))
    l = sum(1 for t in arr if t.get("result") == "STOP")
    return w, l, f"{w / (w + l) * 100:.0f}%" if w + l else "—"

def today_stats():
    today = datetime.now(timezone.utc).date()
    tt    = [t for t in all_trades() if _trade_date(t) == today]
    if not tt:
        return "*📊 Today*\n\nNo signals yet."
    live  = sum(1 for t in tt if t["mode"] == "live")
    paper = sum(1 for t in tt if t["mode"] == "paper")
    skip  = sum(1 for t in tt if t["mode"] == "skipped")
    pend  = sum(1 for t in tt if t["mode"] == "pending")
    w     = sum(1 for t in tt if t.get("result") in ("TP1", "TP2"))
    l     = sum(1 for t in tt if t.get("result") == "STOP")
    lines = [f"*📊 Today* ({today})\n✅ Live: {live}  📝 Paper: {paper}  ⏸ Skip: {skip}  ⏳ Pending: {pend}"]
    if w + l:
        lines.append(f"*W/L:* {w}W / {l}L ({w / (w + l) * 100:.0f}%)")
    return "\n".join(lines)

def journal():
    tt = sorted(
        [t for t in all_trades() if t["mode"] in ("live", "paper")],
        key=lambda t: t.get("entry_time", ""), reverse=True,
    )
    if not tt:
        return "*📋 Journal*\n\nNo confirmed trades."
    lines = ["*📋 Last 10 confirmed*\n"]
    for t in tt[:10]:
        s  = t["sig"]
        e  = "✅" if t["mode"] == "live" else "📝"
        lines.append(f"{e} `{_fmt_ts(t['entry_time'])}` {s.get('symbol')} *{s.get('signal')}* @ {s.get('entry')} → {t.get('result') or 'open'}")
    return "\n".join(lines)

def skipped():
    tt = sorted(
        [t for t in all_trades() if t["mode"] == "skipped"],
        key=lambda t: t.get("entry_time", ""), reverse=True,
    )
    if not tt:
        return "*⏸ Skipped*\n\nNone yet."
    lines = ["*⏸ Skipped trades*\n"]
    for t in tt[:10]:
        s = t["sig"]
        lines.append(f"⏸ `{_fmt_ts(t['entry_time'])}` {s.get('symbol')} *{s.get('signal')}* → would-be: *{t.get('result') or 'open'}*")
    return "\n".join(lines)

def stats():
    resolved = [t for t in all_trades() if t.get("result") in ("TP1", "TP2", "STOP")]
    if not resolved:
        return "*📈 Stats*\n\nNo resolved trades yet."

    lines = [f"*📈 Stats* ({len(resolved)} resolved)\n"]

    lines.append("*By Tier:*")
    for tier in ("A+", "A", "B+", "B"):
        arr = [t for t in resolved if t["sig"].get("tier") == tier]
        if arr:
            w, l, pct = _wr(arr)
            lines.append(f"  {tier}: {w}W/{l}L — {pct} ({len(arr)})")

    lines.append("\n*By Session:*")
    for sess in ("NY-AM", "London", "NY-PM", "Asia"):
        arr = [t for t in resolved if t["sig"].get("session", "").upper() == sess.upper()]
        if arr:
            w, l, pct = _wr(arr)
            lines.append(f"  {sess}: {w}W/{l}L — {pct} ({len(arr)})")

    lines.append("\n*By Combo:*")
    combos = {}
    for t in resolved:
        combos.setdefault(t["sig"].get("combo", "?"), []).append(t)
    for c, arr in sorted(combos.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
        w, l, pct = _wr(arr)
        lines.append(f"  {c}: {w}W/{l}L — {pct} ({len(arr)})")

    lines.append("\n*ICC+CCT Confluence:*")
    for label, fltr in [("⭐ With CCT", True), ("No CCT", False)]:
        arr = [t for t in resolved if bool(t["sig"].get("icc_cct_confluence")) == fltr]
        if arr:
            w, l, pct = _wr(arr)
            lines.append(f"  {label}: {w}W/{l}L — {pct} ({len(arr)})")

    lines.append("\n*Enter OK vs Skip:*")
    for label, fltr in [("🟢 ENTER", True), ("🟡 SKIP", False)]:
        arr = [t for t in resolved if bool(t["sig"].get("enter_ok")) == fltr]
        if arr:
            w, l, pct = _wr(arr)
            lines.append(f"  {label}: {w}W/{l}L — {pct} ({len(arr)})")

    lines.append("\n*MSS Confirmed:*")
    for label, fltr in [("🔵 MSS active", True), ("No MSS", False)]:
        arr = [t for t in resolved if bool(t["sig"].get("mss_active")) == fltr]
        if arr:
            w, l, pct = _wr(arr)
            lines.append(f"  {label}: {w}W/{l}L — {pct} ({len(arr)})")

    lines.append("\n*PD Zone:*")
    for label, pd, sig in [("Longs in Discount", "Discount", "LONG"), ("Longs in Premium ⚠️", "Premium", "LONG"),
                            ("Shorts in Premium", "Premium", "SHORT"), ("Shorts in Discount ⚠️", "Discount", "SHORT")]:
        arr = [t for t in resolved if t["sig"].get("pd_zone") == pd and t["sig"].get("signal") == sig]
        if arr:
            w, l, pct = _wr(arr)
            lines.append(f"  {label}: {w}W/{l}L — {pct} ({len(arr)})")

    lines.append("\n*ATR State:*")
    for label, val in [("📈 Expanding", True), ("📉 Contracting", False)]:
        arr = [t for t in resolved if t["sig"].get("atr_expanding") is val]
        if arr:
            w, l, pct = _wr(arr)
            lines.append(f"  {label}: {w}W/{l}L — {pct} ({len(arr)})")

    lines.append("\n*By Mode:*")
    for mode, e in [("live", "✅"), ("paper", "📝"), ("skipped", "⏸")]:
        arr = [t for t in resolved if t.get("mode") == mode]
        if arr:
            w, l, pct = _wr(arr)
            lines.append(f"  {e} {mode.upper()}: {w}W/{l}L — {pct} ({len(arr)})")

    return "\n".join(lines)

def status():
    with STATE_LOCK:
        n     = len(TRACKING)
        open_ = sum(1 for t in TRACKING.values() if not t.get("result"))
    return (
        f"*⚙️ Kronus AI v8*\n"
        f"Pine: v9.6 | Chart: A/A+ only | Dual CCT + MSS + PD zone + ATR\n"
        f"Claude: {'✅ ' + CLAUDE_MODEL if CLAUDE_ENABLED and ANTHROPIC_API_KEY else '❌'}\n"
        f"Session filter: {'✅ ' + ', '.join(ALLOWED_SESSIONS) if FILTER_SESSIONS else '⛔ off'}\n"
        f"Timeout: {TRADE_TIMEOUT_HRS}h\n"
        f"Trades: {n} total | {open_} open"
    )

# ── ROUTES ────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    with STATE_LOCK:
        n = len(TRACKING)
        o = sum(1 for t in TRACKING.values() if not t.get("result"))
    return jsonify({"status": "running", "version": "v8", "pine": "v9.6", "trades": n, "open": o})

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        sig = json.loads(request.get_data(as_text=True))
    except Exception:
        return jsonify({"error": "bad JSON"}), 400
    if sig.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    if FILTER_SESSIONS:
        sess = str(sig.get("session", "")).strip().upper()
        if sess and sess not in ALLOWED_SESSIONS:
            return jsonify({"status": "skipped", "reason": f"session {sess}"}), 200
    ok, reason = validate_signal(sig)
    if not ok:
        return jsonify({"error": reason}), 400
    threading.Thread(target=process, args=(sig,), daemon=True).start()
    return jsonify({"status": "accepted"}), 200

@app.route("/telegram", methods=["POST"])
def telegram():
    upd = request.get_json(silent=True) or {}

    if "callback_query" in upd:
        cb     = upd["callback_query"]
        cb_id  = cb.get("id", "")
        data   = cb.get("data", "")
        msg_id = cb.get("message", {}).get("message_id", 0)
        parts  = data.split("|")
        action = parts[0]
        tid    = str(msg_id)

        if action == "open_checklist":
            answer_cb(cb_id)
            with STATE_LOCK:
                origin = TRACKING.get(tid)
            send_checklist(origin["sig"] if origin else {})
            return jsonify({"ok": True})

        if action == "cl_toggle" and len(parts) == 3:
            ref, key = int(parts[1]), parts[2]
            with CHECKLIST_LOCK:
                s = CHECKLIST_STATE.get(ref, {})
                s[key] = not s.get(key, False)
                CHECKLIST_STATE[ref] = s
                sc = dict(s)
            edit_msg(msg_id, cl_header(sc), cl_keys(sc, ref))
            answer_cb(cb_id)
            return jsonify({"ok": True})

        if action == "cl_grade" and len(parts) == 2:
            ref = int(parts[1])
            with CHECKLIST_LOCK:
                s = dict(CHECKLIST_STATE.get(ref, {}))
            tier, label, emoji = grade(s)
            n       = sum(1 for k, _ in CHECKLIST_ITEMS if s.get(k))
            missing = [lbl for k, lbl in CHECKLIST_ITEMS if not s.get(k)]
            miss_s  = "\n".join(f"  ⬜ {m}" for m in missing) or "  ✅ All met!"
            answer_cb(cb_id, f"{emoji} {tier} — {label}")
            send_text(f"{emoji} *Grade: {tier}*\n*{label}*\nMet: *{n}/8*\n\n*Missing:*\n{miss_s}", menu_btns())
            return jsonify({"ok": True})

        if action == "cl_reset" and len(parts) == 2:
            ref = int(parts[1])
            with CHECKLIST_LOCK:
                CHECKLIST_STATE[ref] = {}
            edit_msg(msg_id, cl_header({}), cl_keys({}, ref))
            answer_cb(cb_id, "Reset!")
            return jsonify({"ok": True})

        def set_mode(mode, msg):
            with STATE_LOCK:
                t = TRACKING.get(tid)
                if not t or t.get("mode") != "pending":
                    return None
                t["mode"]        = mode
                t["action_time"] = datetime.now(timezone.utc).isoformat()
                tc = deepcopy(t)
                save_state()
            answer_cb(cb_id, msg)
            edit_msg(msg_id, render(tc), btns_for(tc))
            return tc

        if   action == "confirm": set_mode("live",    "✅ Live logged")   or answer_cb(cb_id, "Already acted", alert=True)
        elif action == "paper":   set_mode("paper",   "📝 Paper logged")  or answer_cb(cb_id, "Already acted", alert=True)
        elif action == "skip":    set_mode("skipped", "⏸ Skipped")       or answer_cb(cb_id, "Already acted", alert=True)
        elif action == "today":   answer_cb(cb_id); send_text(today_stats(), menu_btns())
        elif action == "journal": answer_cb(cb_id); send_text(journal(),     menu_btns())
        elif action == "skipped": answer_cb(cb_id); send_text(skipped(),     menu_btns())
        elif action == "stats":   answer_cb(cb_id); send_text(stats(),       menu_btns())
        elif action == "status":  answer_cb(cb_id); send_text(status(),      menu_btns())
        else: answer_cb(cb_id)
        return jsonify({"ok": True})

    if "message" in upd:
        txt = upd["message"].get("text", "").strip().lower()
        if txt in ("/menu", "/start"):
            send_text("*Kronus AI v8*", menu_btns())
        elif txt == "/checklist":
            with STATE_LOCK:
                recent = sorted(TRACKING.values(), key=lambda t: t.get("entry_time", ""), reverse=True)
            send_checklist(recent[0]["sig"] if recent else {})
        elif txt == "/today":   send_text(today_stats(), menu_btns())
        elif txt == "/journal": send_text(journal(),     menu_btns())
        elif txt == "/skipped": send_text(skipped(),     menu_btns())
        elif txt == "/stats":   send_text(stats(),       menu_btns())
        elif txt == "/status":  send_text(status(),      menu_btns())

    return jsonify({"ok": True})

@app.route("/setup_telegram", methods=["GET"])
def setup_telegram():
    if not TELEGRAM_TOKEN or not PUBLIC_URL:
        return jsonify({"error": "TELEGRAM_TOKEN or PUBLIC_URL not set"}), 400
    target = f"{PUBLIC_URL.rstrip('/')}/telegram"
    return jsonify({"target": target, "response": tg("setWebhook", {"url": target, "allowed_updates": ["message", "callback_query"]})})

@app.route("/test", methods=["GET"])
def test():
    fake = {
        "secret":             WEBHOOK_SECRET,
        "symbol":             "MGC1!",
        "tf":                 "1",
        "session":            "NY-AM",
        "tier":               "A+",
        "raw_tier":           "A+",
        "conditions_met":     7,
        "max_conditions":     7,
        "signal":             "LONG",
        "combo":              "2-1-2 Bull",
        "entry":              2650.50,
        "stop":               2648.46,
        "target1":            2652.54,
        "target2":            2654.98,
        "atr":                1.37,
        "rvol":               1.84,
        "icc_cct_confluence": True,
        "near_level":         "PDH",
        "cct_open":           True,
        "mins_to_close":      14,
        "in_session":         True,
        "in_preferred_sess":  True,
        "enter_ok":           True,
        "mss_active":         True,
        "pd_zone":            "Discount",
        "atr_expanding":      True,
        "cond_htf_bias":      True,
        "cond_liq_sweep":     True,
        "cond_displacement":  True,
        "cond_fvg_formed":    True,
        "cond_fvg_retrace":   True,
        "cond_ltf_confirm":   True,
        "cond_liq_target":    True,
    }
    threading.Thread(target=process, args=(fake,), daemon=True).start()
    return jsonify({"status": "dispatched"})

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"pong": True, "ts": datetime.now(timezone.utc).isoformat()})

# ── STARTUP ───────────────────────────────────────────────────
load_state()
threading.Thread(target=timeout_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Kronus AI v8 — Pine v9.6 — port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
