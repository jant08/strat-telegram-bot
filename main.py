"""
KRONUS AI — main.py v7
Fully matched to Pine v9.3.
Changes from v6:
  - Version strings updated to v9.3 throughout
  - Signal card shows mss_active, pd_zone, atr_expanding
  - Claude prompt updated: v9.3 gate logic, MSS, PD zone, ATR expansion
  - Stats: PD zone breakdown + MSS confluence breakdown added
  - Test route payload includes all v9.3 fields
  - Home route reports pine v9.3
  - status() reports pine v9.3
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
    for f in ["symbol", "signal", "price", "stop", "target1", "target2"]:
        if sig.get(f) is None:
            return False, f"missing {f}"
    for f in ["price", "stop", "target1", "target2"]:
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
        {"text": "📊 Today", "callback_data": "today"},
        {"text": "📈 Stats", "callback_data": "stats"},
        {"text": "📋 Journal", "callback_data": "journal"},
    ]]}

def menu_btns():
    return {"inline_keyboard": [
        [{"text": "📊 Today", "callback_data": "today"}, {"text": "📋 Journal", "callback_data": "journal"}],
        [{"text": "📈 Stats", "callback_data": "stats"}, {"text": "⏸ Skipped", "callback_data": "skipped"}],
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

    tier_line = tier
    if raw_tier != tier and raw_tier == "B+":
        tier_line = f"{tier} _(B+ upgraded via CCT)_"

    rvol_raw = sig.get("rvol")
    try:
        rf = float(rvol_raw)
        rvol_txt = ("✦ " if rf >= 1.5 else "⚠ " if rf < 1.3 else "") + f"{rf:.2f}x"
    except (TypeError, ValueError):
        rvol_txt = "—"

    cct_line  = f"✓ {sig.get('mins_to_close')}m to close" if sig.get("cct_open") else "—"
    sweep_txt = "✓ pivot" if sig.get("cond_liq_sweep") else "—"
    conf_line = "\n⭐ *ICC+CCT CONFLUENCE — Displacement at close*\n" if icc_cct else ""

    # ── v9.3 new fields ──────────────────────────────────────
    mss_active   = sig.get("mss_active", False)
    pd_zone      = sig.get("pd_zone", "—")
    atr_exp      = sig.get("atr_expanding")

    mss_txt = "🔵 ACTIVE" if mss_active else "—"
    pd_txt  = ("✅ " if pd_zone == "Discount" and sig.get("signal") == "LONG"
               else "✅ " if pd_zone == "Premium" and sig.get("signal") == "SHORT"
               else "⚠️ ") + pd_zone if pd_zone not in ("—", None) else "—"
    atr_txt = ("📈 Expanding" if atr_exp is True
               else "📉 Contracting" if atr_exp is False
               else "—")

    body = (
        f"{d} *{sig.get('symbol')} — {sig.get('signal')}*{'  ⚡' if not ana else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Tier:* {tier_line}  |  *Conds:* {conds}/{max_c}\n"
        f"*Session:* {sig.get('session')}  |  *TF:* {sig.get('tf')}m\n"
        f"*Combo:* {sig.get('combo')}\n\n"
        f"*Entry:* `{sig.get('price')}`\n"
        f"*Stop:*  `{sig.get('stop')}`\n"
        f"*TP1:*   `{sig.get('target1')}`\n"
        f"*TP2:*   `{sig.get('target2')}`\n\n"
        f"*RVOL:* {rvol_txt}  |  *Sweep:* {sweep_txt}  |  *Lvl:* {sig.get('near_level', '—')}\n"
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
    e = {"live": "✅", "paper": "📝", "skipped": "⏸"}.get(mode, "•")
    l = mode.upper()
    try:
        ts = datetime.fromisoformat(trade.get("action_time", trade["entry_time"])).strftime("%H:%M:%S")
    except Exception:
        ts = "??"
    return f"\n\n{e} *{l}* at {ts}"

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
    if other == 7 and s_on:             return "A+", "Perfect — TAKE IT", "🔥"
    if other == 7 or (other == 6 and s_on): return "A", "Strong trade", "✅"
    if other >= 5:                      return "B+", "Decent — size down", "⚠️"
    return "B", "Weak — SKIP", "❌"

def cl_header(state):
    n    = sum(1 for k, _ in CHECKLIST_ITEMS if state.get(k, False))
    auto = " _(auto)_" if state.get("_auto") else ""
    return f"🔥 *Setup Checklist* ({n}/8){auto}\n"

def cl_keys(state, mid):
    rows = [[{"text": ("✅" if state.get(k) else "⬜") + f"  {lbl}", "callback_data": f"cl_toggle|{mid}|{k}"}] for k, lbl in CHECKLIST_ITEMS]
    rows.append([{"text": "📊 Grade", "callback_data": f"cl_grade|{mid}"}, {"text": "🔄 Reset", "callback_data": f"cl_reset|{mid}"}])
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
        rf = float(sig.get("rvol", 0))
        rvol_s = f"{rf:.2f}x"
    except (TypeError, ValueError):
        rvol_s = "N/A"

    icc_cct  = "YES ⭐" if sig.get("icc_cct_confluence") else "no"
    tier     = sig.get("tier", "?")
    raw_t    = sig.get("raw_tier", tier)
    upgrade  = f" (upgraded from {raw_t} via ICC+CCT)" if raw_t != tier else ""

    # v9.3 extras for Claude context
    mss_active  = sig.get("mss_active", False)
    pd_zone     = sig.get("pd_zone", "unknown")
    atr_exp     = sig.get("atr_expanding", None)
    mss_txt     = "YES — structure confirmed flip" if mss_active else "no"
    atr_txt     = "expanding (trending)" if atr_exp is True else "contracting (chop risk)" if atr_exp is False else "unknown"

    prompt = f"""Kronus AI v9.3 futures signal review. Pine v9.3 gates are strict:
C2 sweep anchored to confirmed pivot. C3 displacement needs body AND RVOL>=1.3x. C5 FVG retrace needs wick tap + close respects level. FVGs below 0.3x ATR are filtered out as noise. B+ upgrades to A when ICC+CCT fires.
NEW in v9.3: MSS (market structure shift) confirms sweep actually flipped structure. PD zone filters longs to discount (<50% EQ) and shorts to premium (>50% EQ). ATR expansion flags trending vs contracting volatility.
Be lenient on A/A+ — only flag WAIT for a concrete red flag. Flag WAIT if PD zone is wrong side for the direction and MSS is absent.

{sig.get('symbol')} {sig.get('signal')} | Tier {tier}{upgrade} | {sig.get('conditions_met','?')}/{sig.get('max_conditions',7)} conds | {sig.get('session')} {sig.get('tf')}m
Entry {sig.get('price')} Stop {sig.get('stop')} TP1 {sig.get('target1')} TP2 {sig.get('target2')} ATR {sig.get('atr')} RVOL {rvol_s}
C1 HTF:{sig.get('cond_htf_bias')} C2 Sweep:{sig.get('cond_liq_sweep')} C3 Disp+Vol:{sig.get('cond_displacement')} C4 FVG:{sig.get('cond_fvg_formed')} C5 Retrace:{sig.get('cond_fvg_retrace')} C6 LTF:{sig.get('cond_ltf_confirm')} C7 Target:{sig.get('cond_liq_target')}
ICC+CCT confluence: {icc_cct} | Near: {sig.get('near_level','none')} | CCT: {sig.get('cct_open')} ({sig.get('mins_to_close')}m) | Pref session: {sig.get('in_preferred_sess')}
MSS confirmed: {mss_txt} | PD zone: {pd_zone} | ATR state: {atr_txt}

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
            "chat_id": TELEGRAM_CHAT_ID, "text": fmt_card(sig),
            "parse_mode": "Markdown", "reply_markup": signal_btns(sig),
        }).get("result", {}).get("message_id", 0))
        if not msg_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        trade = {
            "trade_id": str(msg_id), "message_id": msg_id, "sig": sig, "ana": None,
            "mode": "pending", "entry_time": now, "action_time": None,
            "result": None, "result_time": None, "mfe": 0.0, "mae": 0.0,
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
        ts = _fmt_ts(t["entry_time"])
        e  = "✅" if t["mode"] == "live" else "📝"
        lines.append(f"{e} `{ts}` {s.get('symbol')} *{s.get('signal')}* @ {s.get('price')} → {t.get('result') or 'open'}")
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

    def wr(arr):
        w = sum(1 for t in arr if t.get("result") in ("TP1", "TP2"))
        l = sum(1 for t in arr if t.get("result") == "STOP")
        return w, l, f"{w / (w + l) * 100:.0f}%" if w + l else "—"

    lines = [f"*📈 Stats* ({len(resolved)} resolved)\n"]

    # By tier
    lines.append("*By Tier:*")
    for tier in ("A+", "A", "B+", "B"):
        arr = [t for t in resolved if t["sig"].get("tier") == tier]
        if arr:
            w, l, pct = wr(arr)
            lines.append(f"  {tier}: {w}W/{l}L — {pct} ({len(arr)} trades)")

    # By session
    lines.append("\n*By Session:*")
    for sess in ("NY-AM", "London", "NY-PM", "Asia"):
        arr = [t for t in resolved if t["sig"].get("session", "").upper() == sess.upper()]
        if arr:
            w, l, pct = wr(arr)
            lines.append(f"  {sess}: {w}W/{l}L — {pct} ({len(arr)})")

    # By combo
    lines.append("\n*By Combo:*")
    combos = {}
    for t in resolved:
        c = t["sig"].get("combo", "?")
        combos.setdefault(c, []).append(t)
    for c, arr in sorted(combos.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
        w, l, pct = wr(arr)
        lines.append(f"  {c}: {w}W/{l}L — {pct} ({len(arr)})")

    # ICC+CCT confluence
    lines.append("\n*ICC+CCT Confluence:*")
    with_conf    = [t for t in resolved if t["sig"].get("icc_cct_confluence")]
    without_conf = [t for t in resolved if not t["sig"].get("icc_cct_confluence")]
    if with_conf:
        w, l, pct = wr(with_conf)
        lines.append(f"  ⭐ With CCT: {w}W/{l}L — {pct} ({len(with_conf)})")
    if without_conf:
        w, l, pct = wr(without_conf)
        lines.append(f"  No CCT: {w}W/{l}L — {pct} ({len(without_conf)})")

    # ── v9.3 additions ───────────────────────────────────────

    # MSS confirmed
    lines.append("\n*MSS Confirmed (v9.3):*")
    with_mss    = [t for t in resolved if t["sig"].get("mss_active")]
    without_mss = [t for t in resolved if not t["sig"].get("mss_active")]
    if with_mss:
        w, l, pct = wr(with_mss)
        lines.append(f"  🔵 MSS active: {w}W/{l}L — {pct} ({len(with_mss)})")
    if without_mss:
        w, l, pct = wr(without_mss)
        lines.append(f"  No MSS: {w}W/{l}L — {pct} ({len(without_mss)})")

    # PD Zone
    lines.append("\n*PD Zone (v9.3):*")
    longs  = [t for t in resolved if t["sig"].get("signal") == "LONG"]
    shorts = [t for t in resolved if t["sig"].get("signal") == "SHORT"]
    l_disc = [t for t in longs  if t["sig"].get("pd_zone") == "Discount"]
    l_prem = [t for t in longs  if t["sig"].get("pd_zone") == "Premium"]
    s_prem = [t for t in shorts if t["sig"].get("pd_zone") == "Premium"]
    s_disc = [t for t in shorts if t["sig"].get("pd_zone") == "Discount"]
    if l_disc:
        w, l, pct = wr(l_disc)
        lines.append(f"  Longs in Discount: {w}W/{l}L — {pct} ({len(l_disc)})")
    if l_prem:
        w, l, pct = wr(l_prem)
        lines.append(f"  Longs in Premium:  {w}W/{l}L — {pct} ({len(l_prem)}) ⚠️")
    if s_prem:
        w, l, pct = wr(s_prem)
        lines.append(f"  Shorts in Premium: {w}W/{l}L — {pct} ({len(s_prem)})")
    if s_disc:
        w, l, pct = wr(s_disc)
        lines.append(f"  Shorts in Discount:{w}W/{l}L — {pct} ({len(s_disc)}) ⚠️")

    # ATR expansion
    lines.append("\n*ATR State (v9.3):*")
    expanding    = [t for t in resolved if t["sig"].get("atr_expanding") is True]
    contracting  = [t for t in resolved if t["sig"].get("atr_expanding") is False]
    if expanding:
        w, l, pct = wr(expanding)
        lines.append(f"  📈 Expanding: {w}W/{l}L — {pct} ({len(expanding)})")
    if contracting:
        w, l, pct = wr(contracting)
        lines.append(f"  📉 Contracting: {w}W/{l}L — {pct} ({len(contracting)})")

    # By mode
    lines.append("\n*By Mode:*")
    for mode in ("live", "paper", "skipped"):
        arr = [t for t in resolved if t.get("mode") == mode]
        if arr:
            w, l, pct = wr(arr)
            e = {"live": "✅", "paper": "📝", "skipped": "⏸"}[mode]
            lines.append(f"  {e} {mode.upper()}: {w}W/{l}L — {pct} ({len(arr)})")

    return "\n".join(lines)

def status():
    with STATE_LOCK:
        n     = len(TRACKING)
        open_ = sum(1 for t in TRACKING.values() if not t.get("result"))
    return (
        f"*⚙️ Kronus AI v7*\n"
        f"Pine: v9.3 | Chart: A/A+ only | ICC+CCT + MSS + PD zone + ATR gate\n"
        f"Claude: {'✅ ' + CLAUDE_MODEL if CLAUDE_ENABLED and ANTHROPIC_API_KEY else '❌'}\n"
        f"Session filter: {'✅ ' + ', '.join(ALLOWED_SESSIONS) if FILTER_SESSIONS else '⛔ off'}\n"
        f"Timeout: {TRADE_TIMEOUT_HRS}h\n"
        f"Trades: {n} total | {open_} open"
    )

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

# ── ROUTES ────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    with STATE_LOCK:
        n = len(TRACKING)
        o = sum(1 for t in TRACKING.values() if not t.get("result"))
    return jsonify({"status": "running", "version": "v7", "pine": "v9.3", "trades": n, "open": o})

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
                if not t:
                    return None
                if t.get("mode") != "pending":
                    return None
                t["mode"]        = mode
                t["action_time"] = datetime.now(timezone.utc).isoformat()
                tc = deepcopy(t)
                save_state()
            answer_cb(cb_id, msg)
            edit_msg(msg_id, render(tc), btns_for(tc))
            return tc

        if action == "confirm":
            if not set_mode("live", "✅ Live logged"):
                answer_cb(cb_id, "Already acted", alert=True)
        elif action == "paper":
            if not set_mode("paper", "📝 Paper logged"):
                answer_cb(cb_id, "Already acted", alert=True)
        elif action == "skip":
            if not set_mode("skipped", "⏸ Skipped"):
                answer_cb(cb_id, "Already acted", alert=True)
        elif action == "today":   answer_cb(cb_id); send_text(today_stats(), menu_btns())
        elif action == "journal": answer_cb(cb_id); send_text(journal(),     menu_btns())
        elif action == "skipped": answer_cb(cb_id); send_text(skipped(),     menu_btns())
        elif action == "stats":   answer_cb(cb_id); send_text(stats(),       menu_btns())
        elif action == "status":  answer_cb(cb_id); send_text(status(),      menu_btns())
        else: answer_cb(cb_id)
        return jsonify({"ok": True})

    if "message" in upd:
        txt = upd["message"].get("text", "").strip().lower()
        if txt in ("/menu", "/start"): send_text("*Kronus AI v7*", menu_btns())
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
    # Full v9.3 payload — matches Pine f_json() output exactly
    fake = {
        "secret":               WEBHOOK_SECRET,
        "symbol":               "MGC1!",
        "tf":                   "15",
        "session":              "NY-AM",
        "tier":                 "A+",
        "raw_tier":             "A+",
        "conditions_met":       7,
        "max_conditions":       7,
        "signal":               "LONG",
        "combo":                "2-1-2 Bull",
        "price":                2650.50,
        "stop":                 2648.00,
        "target1":              2654.25,
        "target2":              2658.50,
        "atr":                  1.67,
        "rvol":                 1.84,
        "icc_cct_confluence":   True,
        "near_level":           "PDH",
        "cct_open":             True,
        "mins_to_close":        18,
        "in_session":           True,
        "in_preferred_sess":    True,
        # v9.3 new fields
        "mss_active":           True,
        "pd_zone":              "Discount",
        "atr_expanding":        True,
        # conditions
        "cond_htf_bias":        True,
        "cond_liq_sweep":       True,
        "cond_displacement":    True,
        "cond_fvg_formed":      True,
        "cond_fvg_retrace":     True,
        "cond_ltf_confirm":     True,
        "cond_liq_target":      True,
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
    log.info(f"Kronus AI v7 — Pine v9.3 — port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
