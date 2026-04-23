"""
╔══════════════════════════════════════════════════════════════╗
║  KRONUS AI — MAIN SERVER  (v4.2 — fast-first alerts)        ║
║                                                              ║
║  CRITICAL CHANGE vs v4.1:                                    ║
║   - Telegram alert fires IMMEDIATELY with raw signal data    ║
║     (tier, entry, stop, targets, TFC, ICC, FVG, CCT).        ║
║     Target latency: 2-5 seconds from bar close.              ║
║   - Claude analysis runs in background AFTER alert sent.     ║
║     When verdict comes back, the message is EDITED to        ║
║     append Claude's take. Typically adds 4-8 seconds.        ║
║   - If Claude is slow (>8s) or fails, the fast alert is      ║
║     already on your phone with everything you need.          ║
║                                                              ║
║  You lose nothing. Alert speed matches ICC/CCT strategy.     ║
╚══════════════════════════════════════════════════════════════╝
"""
import os, json, logging, requests, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "goldstrat2025")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_TIMEOUT    = int(os.environ.get("CLAUDE_TIMEOUT", "8"))   # seconds
CLAUDE_ENABLED    = os.environ.get("CLAUDE_ENABLED", "true").lower() == "true"
PUBLIC_URL        = os.environ.get("PUBLIC_URL", "")

JOURNAL = []
SKIPPED = []
PENDING = {}

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM LOW-LEVEL
# ═══════════════════════════════════════════════════════════════
def tg_api(method: str, payload: dict, timeout: int = 5) -> dict:
    if not TELEGRAM_TOKEN:
        return {}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Telegram {method} error: {e}")
        return {}

def signal_buttons(sig: dict) -> dict:
    sym = sig.get("symbol", "?")
    direction = sig.get("signal", "?")
    return {"inline_keyboard": [
        [
            {"text": f"✅ Take Trade ({direction})", "callback_data": f"confirm|{sym}|{direction}"},
            {"text": "⏸ Skip",                      "callback_data": f"skip|{sym}|{direction}"},
        ],
        [
            {"text": "📊 Today's P&L",  "callback_data": "today"},
            {"text": "📋 Journal",      "callback_data": "journal"},
        ],
        [
            {"text": "🎯 Adjust SL/TP", "callback_data": "adjust"},
            {"text": "📖 Help",         "callback_data": "help"},
        ],
    ]}

def menu_buttons() -> dict:
    return {"inline_keyboard": [
        [
            {"text": "📊 Today's P&L",  "callback_data": "today"},
            {"text": "📋 Journal",      "callback_data": "journal"},
        ],
        [
            {"text": "📈 Positions",    "callback_data": "positions"},
            {"text": "⏸ Skipped",       "callback_data": "skipped"},
        ],
        [
            {"text": "⚙️ Status",       "callback_data": "status"},
            {"text": "📖 Help",         "callback_data": "help"},
        ],
    ]}

# ═══════════════════════════════════════════════════════════════
#  MESSAGE FORMATTERS
# ═══════════════════════════════════════════════════════════════
def format_fast_alert(sig: dict) -> str:
    """Raw signal alert — NO Claude verdict yet. Goes out in 2-3 seconds."""
    dir_emoji = "📈" if sig.get("signal") == "LONG" else "📉"
    cct_txt = f"✓ {sig.get('mins_to_close')}m to close" if sig.get("cct_open") else "—"
    icc_txt = "✓" if sig.get("icc") else "—"
    fvg_txt = "✓" if sig.get("fvg") else "—"

    return (
        f"{dir_emoji} *{sig.get('symbol')} — {sig.get('signal')}*  ⚡\n"
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
        f"*CCT:* {cct_txt}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 _Claude analysis loading..._"
    )

def format_enriched_alert(sig: dict, ana: dict) -> str:
    """Same alert, but Claude's verdict has landed — append it."""
    dir_emoji = "📈" if sig.get("signal") == "LONG" else "📉"
    v = ana.get("verdict", "REVIEW")
    v_emoji = {"BUY": "✅", "SELL": "✅", "WAIT": "⏸", "REVIEW": "⚠️"}.get(v, "•")
    cct_txt = f"✓ {sig.get('mins_to_close')}m to close" if sig.get("cct_open") else "—"
    icc_txt = "✓" if sig.get("icc") else "—"
    fvg_txt = "✓" if sig.get("fvg") else "—"

    return (
        f"{dir_emoji} *{sig.get('symbol')} — {sig.get('signal')}*\n"
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
        f"*CCT:* {cct_txt}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{v_emoji} *Claude: {v}* ({ana.get('confidence')})\n"
        f"_{ana.get('key_factor')}_\n\n"
        f"{ana.get('reasoning')}"
    )

def send_fast_alert(sig: dict) -> int:
    """Send the raw signal immediately with buttons. Returns message_id."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram not configured")
        return 0
    resp = tg_api("sendMessage", {
        "chat_id":      TELEGRAM_CHAT_ID,
        "text":         format_fast_alert(sig),
        "parse_mode":   "Markdown",
        "reply_markup": signal_buttons(sig),
    })
    msg_id = resp.get("result", {}).get("message_id", 0)
    if msg_id:
        PENDING[msg_id] = {"sig": sig, "ana": None,
                           "ts": datetime.now(timezone.utc).isoformat()}
    return msg_id

def send_text(text: str, buttons: dict = None) -> int:
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if buttons:
        payload["reply_markup"] = buttons
    resp = tg_api("sendMessage", payload)
    return resp.get("result", {}).get("message_id", 0)

def edit_message(message_id: int, new_text: str, buttons: dict = None):
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text":       new_text,
        "parse_mode": "Markdown",
    }
    if buttons is not None:
        payload["reply_markup"] = buttons
    tg_api("editMessageText", payload)

def answer_callback(callback_id: str, text: str = "", alert: bool = False):
    tg_api("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text":              text,
        "show_alert":        alert,
    })

# ═══════════════════════════════════════════════════════════════
#  CLAUDE — called AFTER fast alert is sent
# ═══════════════════════════════════════════════════════════════
def analyze(sig: dict) -> dict:
    if not CLAUDE_ENABLED or not ANTHROPIC_API_KEY:
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude disabled or no key",
                "reasoning": "Trade the signal on its own merits."}

    prompt = f"""You are reviewing a live futures trade signal from a Strat + ICC + CCT system.

ROLE: LENIENT. Trust the scoring. Approve most A+ and B setups. Only flag WAIT if there's a clear red flag (setup against 3/3 HTF bias with no confluence, or tier C).

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
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json"
            },
            json={"model": CLAUDE_MODEL, "max_tokens": 250,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=CLAUDE_TIMEOUT
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        return json.loads(text)
    except requests.exceptions.Timeout:
        log.warning(f"Claude timeout after {CLAUDE_TIMEOUT}s — using fallback")
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude timed out",
                "reasoning": "Trade the signal on your own read — analysis took too long."}
    except Exception as e:
        log.error(f"Claude error: {e}")
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude API error",
                "reasoning": f"Review manually. ({str(e)[:100]})"}

# ═══════════════════════════════════════════════════════════════
#  BACKGROUND WORKER — fast alert first, Claude enrichment after
# ═══════════════════════════════════════════════════════════════
def process_signal_async(sig: dict):
    """Runs in a thread. Sends fast alert, then calls Claude, then edits."""
    try:
        # Step 1: fast alert out the door (~2s)
        msg_id = send_fast_alert(sig)
        if not msg_id:
            log.error("Fast alert failed to send — aborting Claude enrichment")
            return
        log.info(f"Fast alert sent, message_id={msg_id}")

        # Step 2: call Claude (up to CLAUDE_TIMEOUT seconds)
        ana = analyze(sig)
        log.info(f"Claude verdict: {ana.get('verdict')} ({ana.get('confidence')})")

        # Step 3: enrich the existing message with Claude's take
        if msg_id in PENDING:
            PENDING[msg_id]["ana"] = ana
        edit_message(msg_id, format_enriched_alert(sig, ana), signal_buttons(sig))
        log.info(f"Message {msg_id} enriched with Claude verdict")
    except Exception as e:
        log.error(f"Background processing error: {e}")

# ═══════════════════════════════════════════════════════════════
#  JOURNAL HELPERS
# ═══════════════════════════════════════════════════════════════
def today_stats() -> str:
    today = datetime.now(timezone.utc).date()
    confirmed_today = [j for j in JOURNAL
                       if datetime.fromisoformat(j["ts"]).date() == today]
    skipped_today   = [s for s in SKIPPED
                       if datetime.fromisoformat(s["ts"]).date() == today]
    if not confirmed_today and not skipped_today:
        return "*📊 Today's activity*\n\nNo signals yet today."
    lines = [f"*📊 Today's activity* ({today.isoformat()})\n"]
    lines.append(f"✅ Confirmed: {len(confirmed_today)}")
    lines.append(f"⏸ Skipped:   {len(skipped_today)}")
    if confirmed_today:
        lines.append("\n*Confirmed trades:*")
        for j in confirmed_today[-10:]:
            s = j["sig"]
            lines.append(f"  • {s.get('symbol')} {s.get('signal')} @ `{s.get('price')}` ({s.get('tier')})")
    return "\n".join(lines)

def recent_journal() -> str:
    if not JOURNAL:
        return "*📋 Journal*\n\nNo confirmed trades yet."
    lines = ["*📋 Recent confirmed trades* (last 10)\n"]
    for j in JOURNAL[-10:]:
        s = j["sig"]
        ts = datetime.fromisoformat(j["ts"]).strftime("%m/%d %H:%M")
        lines.append(f"`{ts}` {s.get('symbol')} *{s.get('signal')}* "
                     f"@ {s.get('price')} | {s.get('tier')} ({s.get('score')})")
    return "\n".join(lines)

def recent_skipped() -> str:
    if not SKIPPED:
        return "*⏸ Skipped signals*\n\nNone yet."
    lines = ["*⏸ Recent skipped signals* (last 10)\n"]
    for k in SKIPPED[-10:]:
        s = k["sig"]
        ts = datetime.fromisoformat(k["ts"]).strftime("%m/%d %H:%M")
        lines.append(f"`{ts}` {s.get('symbol')} *{s.get('signal')}* "
                     f"@ {s.get('price')} | {s.get('tier')}")
    return "\n".join(lines)

def help_text() -> str:
    return (
        "*📖 Kronus AI — Help*\n\n"
        "Every alert fires in ~3 seconds with full signal data. "
        "Claude's take updates the message a few seconds later (⚡ icon = still loading).\n\n"
        "• *✅ Take Trade* — logs to journal.\n"
        "• *⏸ Skip* — logs the pass.\n\n"
        "*Commands:*\n"
        "`/menu` `/today` `/journal` `/skipped` `/status` `/help`\n\n"
        "_Phase 1: manual confirm + journaling. "
        "Phase 2 (broker auto-execution) after ~50 validated signals._"
    )

def status_text() -> str:
    return (
        "*⚙️ Kronus AI — Status*\n\n"
        f"Version: *v4.2 (fast-first)*\n"
        f"Claude: {'✅ ' + CLAUDE_MODEL if (CLAUDE_ENABLED and ANTHROPIC_API_KEY) else '❌ disabled'}\n"
        f"Claude timeout: {CLAUDE_TIMEOUT}s\n"
        f"Telegram: {'✅ enabled' if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else '❌ disabled'}\n\n"
        f"Pending: *{len(PENDING)}*  |  Journal: *{len(JOURNAL)}*  |  Skipped: *{len(SKIPPED)}*"
    )

# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status":         "running",
        "bot":            "Kronus AI v4.2 — fast-first alerts",
        "claude":         "enabled" if (CLAUDE_ENABLED and ANTHROPIC_API_KEY) else "disabled",
        "claude_model":   CLAUDE_MODEL,
        "claude_timeout": CLAUDE_TIMEOUT,
        "telegram":       "enabled" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "disabled",
        "journal":        len(JOURNAL),
        "pending":        len(PENDING),
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    """TradingView → Kronus. Responds instantly, processes async."""
    try:
        raw = request.get_data(as_text=True)
        log.info(f"Webhook in: {raw[:250]}")
        sig = json.loads(raw)
    except Exception as e:
        log.error(f"JSON parse error: {e}")
        return jsonify({"error": "invalid JSON"}), 400

    if sig.get("secret") != WEBHOOK_SECRET:
        log.warning("Invalid secret")
        return jsonify({"error": "unauthorized"}), 401

    thread = threading.Thread(target=process_signal_async, args=(sig,), daemon=True)
    thread.start()
    return jsonify({"status": "accepted"}), 200

@app.route("/telegram", methods=["POST"])
def telegram_update():
    upd = request.get_json(silent=True) or {}
    log.info(f"Telegram update: {json.dumps(upd)[:300]}")

    if "callback_query" in upd:
        cb      = upd["callback_query"]
        cb_id   = cb.get("id", "")
        data    = cb.get("data", "")
        msg     = cb.get("message", {})
        msg_id  = msg.get("message_id", 0)

        parts = data.split("|")
        action = parts[0]

        if action == "confirm":
            pending = PENDING.pop(msg_id, None)
            if pending:
                JOURNAL.append(pending)
                sig = pending["sig"]
                answer_callback(cb_id, f"✅ Trade logged: {sig.get('symbol')} {sig.get('signal')}")
                new_text = msg.get("text", "") + f"\n\n✅ *CONFIRMED* at {datetime.now().strftime('%H:%M:%S')}"
                edit_message(msg_id, new_text, buttons={"inline_keyboard": [[
                    {"text": "📊 Today", "callback_data": "today"},
                    {"text": "📋 Journal", "callback_data": "journal"},
                ]]})
            else:
                answer_callback(cb_id, "⚠️ Already acted on", alert=True)

        elif action == "skip":
            pending = PENDING.pop(msg_id, None)
            if pending:
                SKIPPED.append(pending)
                answer_callback(cb_id, "⏸ Skipped")
                new_text = msg.get("text", "") + f"\n\n⏸ *SKIPPED* at {datetime.now().strftime('%H:%M:%S')}"
                edit_message(msg_id, new_text, buttons=None)
            else:
                answer_callback(cb_id, "⚠️ Already acted on", alert=True)

        elif action == "today":
            answer_callback(cb_id); send_text(today_stats(), menu_buttons())
        elif action == "journal":
            answer_callback(cb_id); send_text(recent_journal(), menu_buttons())
        elif action == "skipped":
            answer_callback(cb_id); send_text(recent_skipped(), menu_buttons())
        elif action in ("status", "positions"):
            answer_callback(cb_id); send_text(status_text(), menu_buttons())
        elif action == "help":
            answer_callback(cb_id); send_text(help_text(), menu_buttons())
        elif action == "adjust":
            answer_callback(cb_id, "🎯 Phase 2 feature", alert=True)
        else:
            answer_callback(cb_id, "Unknown action")
        return jsonify({"ok": True})

    if "message" in upd:
        text = upd["message"].get("text", "").strip().lower()
        if text in ("/menu", "/start"):
            send_text("*📊 Kronus AI — Main Menu*\n\nPick an option:", menu_buttons())
        elif text == "/today":
            send_text(today_stats(), menu_buttons())
        elif text == "/journal":
            send_text(recent_journal(), menu_buttons())
        elif text == "/skipped":
            send_text(recent_skipped(), menu_buttons())
        elif text == "/status":
            send_text(status_text(), menu_buttons())
        elif text == "/help":
            send_text(help_text(), menu_buttons())
        return jsonify({"ok": True})

    return jsonify({"ok": True})

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
    """Keep-alive — point UptimeRobot here every 5 min to avoid sleep."""
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
    thread = threading.Thread(target=process_signal_async, args=(fake,), daemon=True)
    thread.start()
    return jsonify({"test": "dispatched", "note": "check Telegram — fast alert first, Claude verdict enriches in a few seconds"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Kronus AI v4.2 (fast-first) starting on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
