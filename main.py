
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
 
