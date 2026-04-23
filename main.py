"""
╔══════════════════════════════════════════════════════════════╗
║  KRONUS AI — MAIN SERVER  (v4)                              ║
║  TradingView Pine v4 → Claude AI → Telegram (interactive)   ║
║  Gold / Silver Futures (MGC, MSI, GC, SI)                   ║
║                                                              ║
║  NEW in v4:                                                 ║
║   - Functional /callback handler (Confirm / Skip actually   ║
║     do something — journal the trade, update the message)  ║
║   - Two-column button UI with menu commands                 ║
║   - /menu, /positions, /today, /help Telegram commands      ║
║   - In-memory paper journal (Phase 1 of auto-execution)     ║
║   - Telegram webhook setter endpoint (/setup_telegram)      ║
╚══════════════════════════════════════════════════════════════╝
"""
import os, json, logging, requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── CONFIG (Render env variables) ─────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "goldstrat2025")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
PUBLIC_URL        = os.environ.get("PUBLIC_URL", "")  # e.g. https://your-app.onrender.com

# ── IN-MEMORY JOURNAL (Phase 1 tracking) ──────────────────────
# Simple list, resets on restart. For persistence, swap for SQLite or Render KV.
JOURNAL = []            # all confirmed trades
SKIPPED = []            # all skipped signals
PENDING = {}            # signals waiting for button press: message_id -> sig dict

# ═══════════════════════════════════════════════════════════════
#  CLAUDE AI — LENIENT ANALYSIS
# ═══════════════════════════════════════════════════════════════
def analyze(sig: dict) -> dict:
    if not ANTHROPIC_API_KEY:
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude API key missing",
                "reasoning": "Add ANTHROPIC_API_KEY in Render env vars."}

    prompt = f"""You are reviewing a live futures trade signal from a proven Strat + ICC + CCT system.

YOUR ROLE: LENIENT. Trust the scoring. Approve most A+ and B setups. Only flag WAIT if there is a clear red flag (e.g. setup directly against 3/3 higher-timeframe bias with no confluence, or tier C).

SIGNAL:
 • {sig.get('symbol')} {sig.get('signal')} — {sig.get('combo')}
 • Tier {sig.get('tier')} | Score {sig.get('score')}/100 | {sig.get('session')} session | {sig.get('tf')}m
 • Entry {sig.get('price')} | Stop {sig.get('stop')} | TP1 {sig.get('target1')} | TP2 {sig.get('target2')}

CONTEXT:
 • TFC: 4H {sig.get('tfc_4h')} / 1H {sig.get('tfc_1h')} / 15m {sig.get('tfc_15')}
 • ICC: {sig.get('icc')} | FVG: {sig.get('fvg')} | Near: {sig.get('near_level')}
 • CCT window: {sig.get('cct_open')} ({sig.get('mins_to_close')}m to daily close)
 • ATR: {sig.get('atr')}

Return ONLY valid JSON (no markdown, no extra text):
{{"verdict":"BUY|SELL|WAIT","confidence":"HIGH|MEDIUM|LOW","key_factor":"one sentence","reasoning":"2-3 sentences"}}

BUY = approve a LONG. SELL = approve a SHORT. WAIT = skip (use sparingly)."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json"
            },
            json={
                "model":      CLAUDE_MODEL,
                "max_tokens": 400,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=20
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        return json.loads(text)
    except Exception as e:
        log.error(f"Claude error: {e}")
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude API error",
                "reasoning": f"Review manually. ({str(e)[:120]})"}

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM — send / edit / answer callbacks
# ═══════════════════════════════════════════════════════════════
def tg_api(method: str, payload: dict) -> dict:
    """Low-level Telegram API call. Returns JSON or empty dict on failure."""
    if not TELEGRAM_TOKEN:
        return {}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Telegram {method} error: {e}")
        return {}

def signal_buttons(sig: dict) -> dict:
    """Two-column button grid for a fresh signal alert."""
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
    """Main menu grid — sent on /menu command."""
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

def format_signal_msg(sig: dict, ana: dict) -> str:
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

def send_signal(sig: dict, ana: dict) -> int:
    """Send a full signal alert with buttons. Returns message_id or 0."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram not configured")
        return 0
    resp = tg_api("sendMessage", {
        "chat_id":      TELEGRAM_CHAT_ID,
        "text":         format_signal_msg(sig, ana),
        "parse_mode":   "Markdown",
        "reply_markup": signal_buttons(sig),
    })
    msg_id = resp.get("result", {}).get("message_id", 0)
    if msg_id:
        PENDING[msg_id] = {"sig": sig, "ana": ana, "ts": datetime.now(timezone.utc).isoformat()}
    return msg_id

def send_text(text: str, buttons: dict = None) -> int:
    """Send a plain text message, optionally with buttons."""
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if buttons:
        payload["reply_markup"] = buttons
    resp = tg_api("sendMessage", payload)
    return resp.get("result", {}).get("message_id", 0)

def edit_message(message_id: int, new_text: str, buttons: dict = None):
    """Edit an existing message — used to mark Confirm/Skip as done."""
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
    """Acknowledge a button press (removes the 'loading' spinner on the button)."""
    tg_api("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text":              text,
        "show_alert":        alert,
    })

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
        "When a signal fires you'll see a message with buttons:\n"
        "• *✅ Take Trade* — marks the trade as confirmed and logs it to the journal "
        "(paper trade — no broker order is placed yet).\n"
        "• *⏸ Skip* — logs that you passed on this one.\n\n"
        "*Commands you can type:*\n"
        "`/menu` — main menu\n"
        "`/today` — today's activity\n"
        "`/journal` — last 10 confirmed trades\n"
        "`/skipped` — last 10 skipped signals\n"
        "`/status` — server status\n"
        "`/help` — this message\n\n"
        "_Phase 1: manual confirmation + journaling. "
        "Phase 2 (broker auto-execution) will be added after ~50 signals "
        "are validated here._"
    )

def status_text() -> str:
    return (
        "*⚙️ Kronus AI — Status*\n\n"
        f"Version: *v4*\n"
        f"Claude: {'✅ enabled' if ANTHROPIC_API_KEY else '❌ disabled'}\n"
        f"Telegram: {'✅ enabled' if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else '❌ disabled'}\n"
        f"Model: `{CLAUDE_MODEL}`\n\n"
        f"Signals pending decision: *{len(PENDING)}*\n"
        f"Journal entries: *{len(JOURNAL)}*\n"
        f"Skipped: *{len(SKIPPED)}*\n"
    )

# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status":   "running",
        "bot":      "Kronus AI v4 — Strat + Claude + interactive UI",
        "claude":   "enabled" if ANTHROPIC_API_KEY else "disabled",
        "telegram": "enabled" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "disabled",
        "model":    CLAUDE_MODEL,
        "journal":  len(JOURNAL),
        "pending":  len(PENDING),
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    """TradingView → Kronus. Receives a signal, analyzes, sends Telegram."""
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

    ana = analyze(sig)
    log.info(f"Claude verdict: {ana.get('verdict')} ({ana.get('confidence')})")

    msg_id = 0
    if ana.get("verdict") in ("BUY", "SELL", "REVIEW"):
        msg_id = send_signal(sig, ana)

    return jsonify({
        "status":     "processed",
        "verdict":    ana.get("verdict"),
        "message_id": msg_id,
    })

@app.route("/telegram", methods=["POST"])
def telegram_update():
    """Receives updates from Telegram — button presses AND slash commands."""
    upd = request.get_json(silent=True) or {}
    log.info(f"Telegram update: {json.dumps(upd)[:300]}")

    # ── Button press (callback_query) ─────────────────────────
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
                # Update the original message to show it was confirmed
                new_text = msg.get("text", "") + f"\n\n✅ *CONFIRMED* at {datetime.now().strftime('%H:%M:%S')}"
                edit_message(msg_id, new_text, buttons={"inline_keyboard": [[
                    {"text": "📊 Today", "callback_data": "today"},
                    {"text": "📋 Journal", "callback_data": "journal"},
                ]]})
            else:
                answer_callback(cb_id, "⚠️ Signal no longer pending (already acted on?)", alert=True)

        elif action == "skip":
            pending = PENDING.pop(msg_id, None)
            if pending:
                SKIPPED.append(pending)
                answer_callback(cb_id, "⏸ Skipped")
                new_text = msg.get("text", "") + f"\n\n⏸ *SKIPPED* at {datetime.now().strftime('%H:%M:%S')}"
                edit_message(msg_id, new_text, buttons=None)
            else:
                answer_callback(cb_id, "⚠️ Signal no longer pending", alert=True)

        elif action == "today":
            answer_callback(cb_id)
            send_text(today_stats(), menu_buttons())
        elif action == "journal":
            answer_callback(cb_id)
            send_text(recent_journal(), menu_buttons())
        elif action == "skipped":
            answer_callback(cb_id)
            send_text(recent_skipped(), menu_buttons())
        elif action == "status" or action == "positions":
            answer_callback(cb_id)
            send_text(status_text(), menu_buttons())
        elif action == "help":
            answer_callback(cb_id)
            send_text(help_text(), menu_buttons())
        elif action == "adjust":
            answer_callback(cb_id, "🎯 SL/TP adjustment is a Phase 2 feature", alert=True)
        else:
            answer_callback(cb_id, "Unknown action")
        return jsonify({"ok": True})

    # ── Slash command (message) ───────────────────────────────
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
    """One-time: register this Render app as the Telegram webhook target.
       Visit this URL once after deploy: https://your-app.onrender.com/setup_telegram"""
    if not TELEGRAM_TOKEN:
        return jsonify({"error": "TELEGRAM_TOKEN not set"}), 400
    if not PUBLIC_URL:
        return jsonify({"error": "PUBLIC_URL env var not set (e.g. https://your-app.onrender.com)"}), 400
    target = f"{PUBLIC_URL.rstrip('/')}/telegram"
    resp = tg_api("setWebhook", {"url": target,
                                 "allowed_updates": ["message", "callback_query"]})
    return jsonify({"target": target, "telegram_response": resp})

@app.route("/test", methods=["GET"])
def test():
    """Fire a fake A+ signal through the full pipeline."""
    fake = {
        "secret": WEBHOOK_SECRET, "symbol": "MGC1!", "tf": "15", "session": "NY-AM",
        "tier": "A+", "score": 85, "signal": "LONG", "combo": "2-2 Bull",
        "price": 2650.50, "stop": 2648.00, "target1": 2654.25, "target2": 2656.75,
        "tfc_4h": "BULL", "tfc_1h": "BULL", "tfc_15": "BULL",
        "icc": True, "fvg": True, "near_level": "PDH",
        "cct_open": True, "mins_to_close": 18, "atr": 1.67,
    }
    ana = analyze(fake)
    msg_id = send_signal(fake, ana)
    return jsonify({"test": "complete", "verdict": ana.get("verdict"), "message_id": msg_id})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Kronus AI v4 starting on port {port}")
    app.run(host="0.0.0.0", port=port)
