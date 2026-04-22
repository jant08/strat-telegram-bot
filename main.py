"""
╔══════════════════════════════════════════════════════════════╗
║  KRONUS AI — MAIN SERVER  (v3)                              ║
║  TradingView Pine v3 → Claude AI (Lenient) → Telegram       ║
║  Gold / Silver Futures (MGC, MSI, GC, SI)                   ║
╚══════════════════════════════════════════════════════════════╝
"""
import os, json, logging, requests
from flask import Flask, request, jsonify

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── CONFIG (all set as Railway env variables) ─────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "goldstrat2025")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# ═══════════════════════════════════════════════════════════════
#  CLAUDE AI — LENIENT ANALYSIS
# ═══════════════════════════════════════════════════════════════
def analyze(sig: dict) -> dict:
    if not ANTHROPIC_API_KEY:
        return {"verdict": "REVIEW", "confidence": "N/A",
                "key_factor": "Claude API key missing",
                "reasoning": "Add ANTHROPIC_API_KEY in Railway env vars."}

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
        # Strip markdown fences if Claude adds them
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
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════
def send_telegram(sig: dict, ana: dict) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram not configured")
        return False

    dir_emoji = "📈" if sig.get("signal") == "LONG" else "📉"
    v = ana.get("verdict", "REVIEW")
    v_emoji = {"BUY": "✅", "SELL": "✅", "WAIT": "⏸", "REVIEW": "⚠️"}.get(v, "•")

    cct_txt = f"✓ {sig.get('mins_to_close')}m to close" if sig.get("cct_open") else "—"
    icc_txt = "✓" if sig.get("icc") else "—"
    fvg_txt = "✓" if sig.get("fvg") else "—"

    msg = (
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

    buttons = {"inline_keyboard": [[
        {"text": f"✅ Confirm {sig.get('signal')}",
         "callback_data": f"confirm_{sig.get('signal')}_{sig.get('symbol')}"},
        {"text": "⏸ Skip", "callback_data": "skip"}
    ]]}

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":      TELEGRAM_CHAT_ID,
                "text":         msg,
                "parse_mode":   "Markdown",
                "reply_markup": buttons
            },
            timeout=10
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram send error: {e}")
        return False

# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status":   "running",
        "bot":      "Kronus AI v3 — Strat + Claude (Lenient)",
        "claude":   "enabled" if ANTHROPIC_API_KEY else "disabled",
        "telegram": "enabled" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "disabled",
        "model":    CLAUDE_MODEL
    })

@app.route("/webhook", methods=["POST"])
def webhook():
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

    # Lenient: send to Telegram on BUY/SELL and REVIEW. Skip silently on WAIT.
    sent = False
    if ana.get("verdict") in ("BUY", "SELL", "REVIEW"):
        sent = send_telegram(sig, ana)

    return jsonify({
        "status":   "processed",
        "verdict":  ana.get("verdict"),
        "telegram": sent
    })

@app.route("/test", methods=["GET"])
def test():
    """Fire a fake signal through the full pipeline to verify Telegram works."""
    fake = {
        "secret":         WEBHOOK_SECRET,
        "symbol":         "MGC1!",
        "tf":             "15",
        "session":        "NY-AM",
        "tier":           "A+",
        "score":          85,
        "signal":         "LONG",
        "combo":          "2-2 Bull",
        "price":          2650.50,
        "stop":           2648.00,
        "target1":        2654.25,
        "target2":        2656.75,
        "tfc_4h":         "BULL",
        "tfc_1h":         "BULL",
        "tfc_15":         "BULL",
        "icc":            True,
        "fvg":            True,
        "near_level":     "PDH",
        "cct_open":       True,
        "mins_to_close":  18,
        "atr":            1.67
    }
    ana = analyze(fake)
    sent = send_telegram(fake, ana)
    return jsonify({"test": "complete", "verdict": ana.get("verdict"), "telegram_sent": sent})

@app.route("/callback", methods=["POST"])
def callback():
    """Placeholder for Telegram button presses (Confirm / Skip)."""
    data = request.get_json(silent=True) or {}
    log.info(f"Callback: {data}")
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Kronus AI server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
