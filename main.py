"""
╔══════════════════════════════════════════════════════════════╗
║   CCT + ICC + STRAT — TELEGRAM ALERT BOT                     ║
║   Receives TradingView webhooks → sends Telegram messages    ║
║   Host free on Railway.app                                   ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, json, requests
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ── CONFIG (set these in Railway environment variables) ────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")    # from BotFather
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # your chat ID
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "stratbot123")

# ── SEND TELEGRAM MESSAGE ──────────────────────────────────────
def send_telegram(message: str):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, data=data, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"Telegram error: {e}")

# ── FORMAT SIGNAL MESSAGE ──────────────────────────────────────
def format_message(data: dict) -> str:
    signal  = data.get("signal",  "?").upper()
    symbol  = data.get("symbol",  "?")
    tf      = data.get("tf",      "?")
    price   = data.get("price",   "?")
    stop    = data.get("stop",    "?")
    target  = data.get("target",  "?")
    combo   = data.get("combo",   "?")
    tfc     = data.get("tfc",     "?")
    cct     = data.get("cct",     "?")
    now     = datetime.now().strftime("%I:%M %p")

    # Direction emoji
    if signal == "LONG":
        arrow = "🟢"
        dir_label = "LONG  ▲"
    elif signal == "SHORT":
        arrow = "🔴"
        dir_label = "SHORT  ▼"
    else:
        arrow = "🟡"
        dir_label = signal

    # CCT status
    cct_status = "✅ OPEN" if str(cct).lower() in ["true", "1", "yes"] else "⏳ Waiting"

    # TFC bar
    tfc_num = int(tfc) if str(tfc).isdigit() else 0
    tfc_bar = "●" * tfc_num + "○" * (3 - tfc_num)

    # R:R calculation
    try:
        rr = abs((float(target) - float(price)) / (float(price) - float(stop)))
        rr_str = f"1 : {rr:.1f}"
    except:
        rr_str = "—"

    msg = (
        f"{arrow} <b>{dir_label} — {symbol}</b>\n"
        f"{'─' * 28}\n"
        f"⏱  <b>Time:</b>        {now}\n"
        f"📊 <b>Timeframe:</b>  {tf}\n"
        f"💰 <b>Entry:</b>       {price}\n"
        f"🛑 <b>Stop:</b>        {stop}\n"
        f"🎯 <b>Target:</b>      {target}\n"
        f"⚖️  <b>R : R:</b>       {rr_str}\n"
        f"{'─' * 28}\n"
        f"📐 <b>Combo:</b>       {combo}\n"
        f"🔗 <b>TFC:</b>         {tfc_bar} ({tfc}/3)\n"
        f"⏰ <b>CCT Window:</b>  {cct_status}\n"
        f"{'─' * 28}\n"
        f"<i>CCT + ICC + Strat System</i>"
    )
    return msg

# ── WEBHOOK ENDPOINT ───────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    # Check secret key
    key = request.args.get("key", "")
    if key != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "bad json"}), 400

    print(f"Signal received: {data}")

    msg = format_message(data)
    send_telegram(msg)

    return jsonify({"status": "sent"})

# ── HEALTH CHECK ───────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({
        "status": "running",
        "bot":    "CCT + ICC + Strat Telegram Bot",
        "time":   datetime.now().isoformat()
    })

# ── MAIN ───────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Bot running on port {port}")
    app.run(host="0.0.0.0", port=port)
