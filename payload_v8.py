"""
╔══════════════════════════════════════════════════════════════╗
║  payload_v8.py — v7↔v8 Pine webhook compatibility layer     ║
║                                                              ║
║  Drop this file next to main.py. No other deps.             ║
║                                                              ║
║  Handles both old v7 payloads (score 0-100, A+/B/C tiers)   ║
║  and new v8 payloads (conditions_met 0-7, A+/A/B+/B tiers). ║
║                                                              ║
║  Usage:                                                      ║
║    from payload_v8 import (                                  ║
║        normalize_sig,         # unify v7/v8 → canonical dict ║
║        render_signal_block,   # Telegram message body        ║
║        build_claude_context,  # Claude prompt context        ║
║        make_test_payload,     # v8 payload for /test         ║
║    )                                                         ║
╚══════════════════════════════════════════════════════════════╝
"""
from typing import Optional


# ── helpers ──────────────────────────────────────────────────
def _parse_bool(raw, default: bool = False) -> bool:
    """Tolerant bool parse. 'true'/True/1 → True. Missing → default."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    if isinstance(raw, (int, float)):
        return raw != 0
    return default


def _parse_opt_bool(raw) -> Optional[bool]:
    """Like _parse_bool but returns None if raw is missing — keeps 'unknown' distinct from 'false'."""
    if raw is None:
        return None
    return _parse_bool(raw)


def detect_version(sig: dict) -> str:
    """'v8' if payload has the new count field, else 'v7'."""
    return "v8" if "conditions_met" in sig else "v7"


# ── normalizer ───────────────────────────────────────────────
def normalize_sig(sig: dict) -> dict:
    """
    Returns a new dict with canonical _-prefixed fields added.
    Original fields preserved — safe to pass downstream.

    Canonical fields:
        _version        'v7' | 'v8'
        _count          int
        _count_max      int     (7 for v8, 100 for v7)
        _count_label    str     ('5/7' or '72/100')
        _count_display  str     ('Conditions' or 'Score')
        _htf_bias       bool
        _displacement   bool
        _fvg_formed     bool
        _liq_sweep      Optional[bool]   (None on v7)
        _fvg_retrace    Optional[bool]   (None on v7)
        _ltf_confirm    Optional[bool]   (None on v7)
        _liq_target     Optional[bool]   (None on v7)
        _in_session     bool             (default True for v7 back-compat)
        _in_pref_sess   Optional[bool]   (None on v7)
    """
    s = dict(sig)
    version = detect_version(sig)
    s["_version"] = version

    if version == "v8":
        try:
            s["_count"] = int(sig.get("conditions_met", 0))
        except (TypeError, ValueError):
            s["_count"] = 0
        s["_count_max"]      = 7
        s["_count_display"]  = "Conditions"
        s["_count_label"]    = f"{s['_count']}/7"
        s["_htf_bias"]       = _parse_bool(sig.get("cond_htf_bias"))
        s["_displacement"]   = _parse_bool(sig.get("cond_displacement"))
        s["_fvg_formed"]     = _parse_bool(sig.get("cond_fvg_formed"))
        s["_liq_sweep"]      = _parse_opt_bool(sig.get("cond_liq_sweep"))
        s["_fvg_retrace"]    = _parse_opt_bool(sig.get("cond_fvg_retrace"))
        s["_ltf_confirm"]    = _parse_opt_bool(sig.get("cond_ltf_confirm"))
        s["_liq_target"]     = _parse_opt_bool(sig.get("cond_liq_target"))
        s["_in_pref_sess"]   = _parse_opt_bool(sig.get("in_preferred_sess"))
    else:
        # v7 legacy
        try:
            s["_count"] = int(sig.get("score", 0))
        except (TypeError, ValueError):
            s["_count"] = 0
        s["_count_max"]     = 100
        s["_count_display"] = "Score"
        s["_count_label"]   = f"{s['_count']}/100"
        # Derive HTF bias from TFC strings
        t4 = str(sig.get("tfc_4h", "")).upper()
        t1 = str(sig.get("tfc_1h", "")).upper()
        t5 = str(sig.get("tfc_15", "")).upper()
        direction = str(sig.get("signal", "")).upper()
        want = "BULL" if direction == "LONG" else "BEAR"
        aligned = sum(1 for t in (t4, t1, t5) if t == want)
        s["_htf_bias"]     = aligned >= 2
        s["_displacement"] = _parse_bool(sig.get("icc"))
        s["_fvg_formed"]   = _parse_bool(sig.get("fvg"))
        s["_liq_sweep"]    = None
        s["_fvg_retrace"]  = None
        s["_ltf_confirm"]  = None
        s["_liq_target"]   = None
        s["_in_pref_sess"] = None

    # Shared — default in_session True so v7 (which never sent it) works
    s["_in_session"] = _parse_bool(sig.get("in_session"), default=True)
    return s


# ── Telegram renderer ────────────────────────────────────────
def _mark(b) -> str:
    """Checklist marker. None (v7 missing field) → '–'."""
    if b is True:
        return "✅"
    if b is False:
        return "▫️"
    return "–"


def render_signal_block(sig: dict) -> str:
    """
    Returns the Telegram Markdown body for a signal (no Claude analysis).
    Use this as the body, then append Claude's verdict after.

    Safe to call with either v7 or v8 payloads.
    """
    s = normalize_sig(sig)
    dir_emoji = "📈" if s.get("signal") == "LONG" else "📉"
    sess_star = " ⭐" if s["_in_pref_sess"] else ""
    cct_txt   = f"✓ {s.get('mins_to_close')}m to close" if _parse_bool(s.get("cct_open")) else "—"

    if s["_version"] == "v8":
        conds = (
            f"{_mark(s['_htf_bias'])} HTF bias    "
            f"{_mark(s['_liq_sweep'])} Liq sweep\n"
            f"{_mark(s['_displacement'])} Displacement "
            f"{_mark(s['_fvg_formed'])} FVG formed\n"
            f"{_mark(s['_fvg_retrace'])} FVG retrace  "
            f"{_mark(s['_ltf_confirm'])} LTF confirm\n"
            f"{_mark(s['_liq_target'])} Liq target"
        )
    else:
        icc_txt = "✓" if s["_displacement"] else "—"
        fvg_txt = "✓" if s["_fvg_formed"] else "—"
        conds = (
            f"*TFC:* 4H {sig.get('tfc_4h')} / 1H {sig.get('tfc_1h')} / 15m {sig.get('tfc_15')}\n"
            f"*ICC:* {icc_txt}  |  *FVG:* {fvg_txt}"
        )

    return (
        f"{dir_emoji} *{sig.get('symbol')} — {sig.get('signal')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Tier:* {sig.get('tier')}  |  *{s['_count_display']}:* {s['_count_label']}\n"
        f"*Session:* {sig.get('session')}{sess_star}  |  *TF:* {sig.get('tf')}m\n"
        f"*Combo:* {sig.get('combo')}\n\n"
        f"*Entry:* `{sig.get('price')}`\n"
        f"*Stop:*  `{sig.get('stop')}`\n"
        f"*TP1:*   `{sig.get('target1')}`\n"
        f"*TP2:*   `{sig.get('target2')}`\n\n"
        f"{conds}\n"
        f"*Near:* {sig.get('near_level')}  |  *CCT:* {cct_txt}"
    )


# ── Claude context builder ───────────────────────────────────
def build_claude_context(sig: dict) -> str:
    """
    Formatted context block for Claude's analysis prompt.
    Includes the tier-system explanation so Claude reasons about
    the correct grading framework for each payload version.
    """
    s = normalize_sig(sig)

    if s["_version"] == "v8":
        tier_rules = (
            "TIER RULES (v8 — 4-tier count-based):\n"
            "  A+ = 7/7 conditions + preferred session (strongest)\n"
            "  A  = 7/7 non-preferred  OR  6/7 preferred\n"
            "  B+ = 5-6 conditions (decent, size down)\n"
            "  B  = ≤4 conditions (usually skip)\n"
        )
        conds_block = (
            f" • Conditions met: {s['_count']}/7\n"
            f"   1 HTF bias:      {s['_htf_bias']}\n"
            f"   2 Liq sweep:     {s['_liq_sweep']}\n"
            f"   3 Displacement:  {s['_displacement']}\n"
            f"   4 FVG formed:    {s['_fvg_formed']}\n"
            f"   5 FVG retrace:   {s['_fvg_retrace']}\n"
            f"   6 LTF confirm:   {s['_ltf_confirm']}\n"
            f"   7 Liq target:    {s['_liq_target']}\n"
            f" • Preferred session: {s['_in_pref_sess']}\n"
        )
    else:
        tier_rules = (
            "TIER RULES (v7 — legacy score-based):\n"
            "  A+ = 80+/100 | B = 60+/100 | C = <60 (filtered)\n"
        )
        conds_block = (
            f" • Score: {s['_count']}/100\n"
            f" • TFC: 4H {sig.get('tfc_4h')} / 1H {sig.get('tfc_1h')} / 15m {sig.get('tfc_15')}\n"
            f" • ICC: {s['_displacement']} | FVG: {s['_fvg_formed']}\n"
        )

    return (
        f"{tier_rules}\n"
        f"SIGNAL:\n"
        f" • {sig.get('symbol')} {sig.get('signal')} — {sig.get('combo')}\n"
        f" • Tier {sig.get('tier')} | {sig.get('session')} | {sig.get('tf')}m\n"
        f" • Entry {sig.get('price')} | Stop {sig.get('stop')} "
        f"| TP1 {sig.get('target1')} | TP2 {sig.get('target2')}\n"
        f"{conds_block}"
        f" • Near level: {sig.get('near_level')}\n"
        f" • CCT: {sig.get('cct_open')} ({sig.get('mins_to_close')}m to close)\n"
        f" • ATR: {sig.get('atr')}\n"
    )


# ── /test endpoint payload ───────────────────────────────────
def make_test_payload(secret: str) -> dict:
    """v8-format payload for your /test smoke-test endpoint."""
    return {
        "secret": secret,
        "symbol": "MGC1!",
        "tf": "15",
        "session": "NY-AM",
        "tier": "A+",
        "conditions_met": 7,
        "signal": "LONG",
        "combo": "2-2 Bull",
        "price": 2650.50,
        "stop": 2648.00,
        "target1": 2654.25,
        "target2": 2656.75,
        "atr": 1.67,
        "near_level": "PDH",
        "cct_open": True,
        "mins_to_close": 18,
        "in_session": True,
        "in_preferred_sess": True,
        "cond_htf_bias": True,
        "cond_liq_sweep": True,
        "cond_displacement": True,
        "cond_fvg_formed": True,
        "cond_fvg_retrace": True,
        "cond_ltf_confirm": True,
        "cond_liq_target": True,
    }
