//@version=6
strategy(
     title                   = "CCT + ICC + Strat — Paper Simulator",
     shorttitle              = "StratPaper",
     overlay                 = true,
     initial_capital         = 50000,
     default_qty_type        = strategy.fixed,
     default_qty_value       = 1,
     commission_type         = strategy.commission.cash_per_contract,
     commission_value        = 2.50,
     slippage                = 2,
     calc_on_every_tick      = false,
     max_labels_count        = 500,
     max_lines_count         = 400,
     max_boxes_count         = 200,
     process_orders_on_close = true)

// ── INPUTS ────────────────────────────────────────────────────────
g_mode  = "Semi-Auto Mode"
g_strat = "The Strat"
g_icc   = "ICC Settings"
g_cct   = "CCT Settings"
g_risk  = "Risk Management"
g_disp  = "Display"

confirm_bars = input.int  (1,     "Confirmation bars before entry",         group=g_mode, minval=0, maxval=3)
require_all  = input.bool (true,  "Require ALL factors aligned (HPA only)", group=g_mode)
max_trades   = input.int  (1,     "Max concurrent trades",                  group=g_mode, minval=1, maxval=3)

show_nums   = input.bool (true,  "Show candle numbers 1/2U/2D/3", group=g_strat)
show_combos = input.bool (true,  "Show combo labels on chart",    group=g_strat)

icc_mult  = input.float(1.6,  "ICC body size multiplier",            group=g_icc, minval=1.0, step=0.1)
show_fvg  = input.bool (true, "Show Fair Value Gaps",                group=g_icc)
show_pd   = input.bool (true, "Show Prev Day H/L/C lines",           group=g_icc)
show_pw   = input.bool (true, "Show Prev Week H/L lines",            group=g_icc)

cct_mins  = input.int  (20,   "CCT alert window (mins before close)", group=g_cct, minval=5, maxval=60)

stop_ticks  = input.int  (8,     "Stop loss (ticks from entry)",  group=g_risk, minval=1)
rr1         = input.float(1.5,   "Target 1 R:R ratio",            group=g_risk, minval=0.5, step=0.1)
rr2         = input.float(2.5,   "Target 2 R:R ratio",            group=g_risk, minval=1.0, step=0.1)
use_trail   = input.bool (false, "Use trailing stop after T1",     group=g_risk)
trail_ticks = input.int  (6,     "Trail stop distance (ticks)",    group=g_risk, minval=1)

bull_c = input.color(color.new(#26a69a, 0), "Bull color",    group=g_disp)
bear_c = input.color(color.new(#ef5350, 0), "Bear color",    group=g_disp)
neut_c = input.color(color.new(#90a4ae, 0), "Neutral color", group=g_disp)
warn_c = color.new(#ff9800, 0)
purp_c = color.new(#9c27b0, 0)

// ── STRAT CANDLE CLASSIFICATION ───────────────────────────────────
is_1  = high < high[1] and low  > low[1]
is_2u = high > high[1] and low >= low[1]
is_2d = low  < low[1]  and high <= high[1]
is_3  = high > high[1] and low  < low[1]
ctype = is_1 ? 1 : is_2u ? 2 : is_2d ? -2 : is_3 ? 3 : 0

if show_nums
    lbl = is_1 ? "1" : is_2u ? "2U" : is_2d ? "2D" : is_3 ? "3" : ""
    if lbl != ""
        _y   = (is_2u or is_3) ? yloc.abovebar : yloc.belowbar
        _col = is_2u ? bull_c : is_2d ? bear_c : neut_c
        label.new(bar_index, is_2u or is_3 ? high : low, lbl,
             yloc=_y, color=color.new(color.white, 100),
             textcolor=_col, style=label.style_none, size=size.small)

// ── STRAT COMBO DETECTION ─────────────────────────────────────────
rng_pct    = (close - low) / math.max(high - low, 0.0001)
shooter    = (is_2u or is_3) and rng_pct >= 0.75 and close > open
hammer     = (is_2d or is_3) and rng_pct <= 0.25 and close < open
c123_bull  = ctype[2] == 1  and ctype[1] == 2  and is_3 and close > high[1]
c123_bear  = ctype[2] == 1  and ctype[1] == -2 and is_3 and close < low[1]
c22_bull   = ctype[1] == -2 and is_2u
c22_bear   = ctype[1] == 2  and is_2d
broad_bull = is_3 and rng_pct >= 0.6
broad_bear = is_3 and rng_pct <= 0.4

combo_bull = c123_bull or c22_bull or shooter or broad_bull
combo_bear = c123_bear or c22_bear or hammer  or broad_bear
combo_name = c123_bull or c123_bear ? "1-2-3" : c22_bull or c22_bear ? "2-2 Rev" : shooter ? "Shooter" : hammer ? "Hammer" : broad_bull or broad_bear ? "Outside" : "—"

if show_combos
    if c123_bull
        label.new(bar_index, low,  "1-2-3 ▲", yloc=yloc.belowbar, color=bull_c,                textcolor=color.white, style=label.style_label_up,   size=size.normal)
    if c123_bear
        label.new(bar_index, high, "1-2-3 ▼", yloc=yloc.abovebar, color=bear_c,                textcolor=color.white, style=label.style_label_down, size=size.normal)
    if c22_bull
        label.new(bar_index, low,  "2-2 ▲",   yloc=yloc.belowbar, color=color.new(bull_c, 25), textcolor=color.white, style=label.style_label_up,   size=size.small)
    if c22_bear
        label.new(bar_index, high, "2-2 ▼",   yloc=yloc.abovebar, color=color.new(bear_c, 25), textcolor=color.white, style=label.style_label_down, size=size.small)
    if shooter
        label.new(bar_index, high, "SHOOT ▲", yloc=yloc.abovebar, color=bull_c,                textcolor=color.white, style=label.style_label_down, size=size.small)
    if hammer
        label.new(bar_index, low,  "HAMMER ▼",yloc=yloc.belowbar, color=bear_c,                textcolor=color.white, style=label.style_label_up,   size=size.small)

// ── ICC — INSTITUTIONAL CANDLE ────────────────────────────────────
avg_body = ta.sma(math.abs(close - open), 20)
cur_body = math.abs(close - open)
icc_bull = cur_body > avg_body * icc_mult and close > open
icc_bear = cur_body > avg_body * icc_mult and close < open

if icc_bull
    box.new(bar_index, close, bar_index, open,
         bgcolor=color.new(#ff9800, 78), border_color=color.new(#ff9800, 20), border_width=1)
if icc_bear
    box.new(bar_index, open, bar_index, close,
         bgcolor=color.new(#ff9800, 78), border_color=color.new(#ff9800, 20), border_width=1)

// ── FAIR VALUE GAPS ───────────────────────────────────────────────
fvg_bull = show_fvg and low  > high[2]
fvg_bear = show_fvg and high < low[2]

if fvg_bull
    box.new(bar_index - 2, low,    bar_index, high[2],
         bgcolor=color.new(#26a69a, 85), border_color=color.new(#26a69a, 50), border_width=1)
if fvg_bear
    box.new(bar_index - 2, low[2], bar_index, high,
         bgcolor=color.new(#ef5350, 85), border_color=color.new(#ef5350, 50), border_width=1)

// ── KEY LEVELS — PDH / PDL / PDC / PWH / PWL ─────────────────────
// v6: lookahead_on removed — use [1] indexing on D/W request instead
pdh = request.security(syminfo.tickerid, "D", high[1],   lookahead=barmerge.lookahead_off)
pdl = request.security(syminfo.tickerid, "D", low[1],    lookahead=barmerge.lookahead_off)
pdc = request.security(syminfo.tickerid, "D", close[1],  lookahead=barmerge.lookahead_off)
pwh = request.security(syminfo.tickerid, "W", high[1],   lookahead=barmerge.lookahead_off)
pwl = request.security(syminfo.tickerid, "W", low[1],    lookahead=barmerge.lookahead_off)

near_pdh   = math.abs(close - pdh) / close < 0.0015
near_pdl   = math.abs(close - pdl) / close < 0.0015
near_pwh   = math.abs(close - pwh) / close < 0.0015
near_pwl   = math.abs(close - pwl) / close < 0.0015
near_level = near_pdh or near_pdl or near_pwh or near_pwl

if show_pd
    line.new(bar_index[1], pdh, bar_index, pdh, color=color.new(bull_c, 40), style=line.style_dashed, width=1)
    line.new(bar_index[1], pdl, bar_index, pdl, color=color.new(bear_c, 40), style=line.style_dashed, width=1)
    line.new(bar_index[1], pdc, bar_index, pdc, color=color.new(neut_c, 50), style=line.style_dotted, width=1)
if show_pd and barstate.islast
    label.new(bar_index, pdh, "PDH", yloc=yloc.price, color=color.new(color.white, 100), textcolor=bull_c, style=label.style_none, size=size.small)
    label.new(bar_index, pdl, "PDL", yloc=yloc.price, color=color.new(color.white, 100), textcolor=bear_c, style=label.style_none, size=size.small)
    label.new(bar_index, pdc, "PDC", yloc=yloc.price, color=color.new(color.white, 100), textcolor=neut_c, style=label.style_none, size=size.small)
if show_pw
    line.new(bar_index[1], pwh, bar_index, pwh, color=color.new(bull_c, 20), style=line.style_dashed, width=2)
    line.new(bar_index[1], pwl, bar_index, pwl, color=color.new(bear_c, 20), style=line.style_dashed, width=2)
if show_pw and barstate.islast
    label.new(bar_index, pwh, "PWH", yloc=yloc.price, color=color.new(color.white, 100), textcolor=color.new(bull_c, 0), style=label.style_none, size=size.small)
    label.new(bar_index, pwl, "PWL", yloc=yloc.price, color=color.new(color.white, 100), textcolor=color.new(bear_c, 0), style=label.style_none, size=size.small)

// ── CCT — CANDLE CLOSE TIME ───────────────────────────────────────
tf_d_secs    = timeframe.in_seconds("D")
tf_w_secs    = timeframe.in_seconds("W")
epoch_s      = time / 1000
warn_s       = cct_mins * 60
near_d_close = (epoch_s % tf_d_secs) >= (tf_d_secs - warn_s)
near_w_close = (epoch_s % tf_w_secs) >= (tf_w_secs - warn_s)
mins_to_d    = math.round((tf_d_secs - epoch_s % tf_d_secs) / 60)
mins_to_w    = math.round((tf_w_secs - epoch_s % tf_w_secs) / 60)
near_cct     = near_d_close or near_w_close

bgcolor(near_d_close ? color.new(#ff9800, 93) : na, title="CCT Daily window")
bgcolor(near_w_close ? color.new(#9c27b0, 93) : na, title="CCT Weekly window")

// ── TFC — TIME FRAME CONTINUITY ───────────────────────────────────
d_c = request.security(syminfo.tickerid, "D", close, lookahead=barmerge.lookahead_off)
d_o = request.security(syminfo.tickerid, "D", open,  lookahead=barmerge.lookahead_off)
w_c = request.security(syminfo.tickerid, "W", close, lookahead=barmerge.lookahead_off)
w_o = request.security(syminfo.tickerid, "W", open,  lookahead=barmerge.lookahead_off)
m_c = request.security(syminfo.tickerid, "M", close, lookahead=barmerge.lookahead_off)
m_o = request.security(syminfo.tickerid, "M", open,  lookahead=barmerge.lookahead_off)

d_bull    = d_c > d_o
w_bull    = w_c > w_o
m_bull    = m_c > m_o
tfc_score = (d_bull ? 1 : 0) + (w_bull ? 1 : 0) + (m_bull ? 1 : 0)
tfc_bull  = tfc_score >= 2
tfc_bear  = tfc_score <= 1

// ── HPA SIGNAL ────────────────────────────────────────────────────
hpa_long      = combo_bull and icc_bull and near_cct and tfc_bull
hpa_short     = combo_bear and icc_bear and near_cct and tfc_bear
partial_long  = combo_bull and tfc_bull and not hpa_long
partial_short = combo_bear and tfc_bear and not hpa_short

plotshape(series=hpa_long,      title="HPA Long",      style=shape.triangleup,   location=location.belowbar, color=bull_c,                size=size.large, text="HPA ▲")
plotshape(series=hpa_short,     title="HPA Short",     style=shape.triangledown, location=location.abovebar, color=bear_c,                size=size.large, text="HPA ▼")
plotshape(series=partial_long,  title="Partial Long",  style=shape.triangleup,   location=location.belowbar, color=color.new(bull_c, 50), size=size.small)
plotshape(series=partial_short, title="Partial Short", style=shape.triangledown, location=location.abovebar, color=color.new(bear_c, 50), size=size.small)

// ── RISK LEVELS ───────────────────────────────────────────────────
tick      = syminfo.mintick
stop_dist = stop_ticks * tick
tgt1_dist = stop_dist * rr1
tgt2_dist = stop_dist * rr2

// ── ENTRY LOGIC ───────────────────────────────────────────────────
enter_long  = require_all ? hpa_long[confirm_bars]  : (combo_bull[confirm_bars] and tfc_bull[confirm_bars])
enter_short = require_all ? hpa_short[confirm_bars] : (combo_bear[confirm_bars] and tfc_bear[confirm_bars])
can_enter   = math.abs(strategy.opentrades) < max_trades

if enter_long and can_enter and strategy.position_size == 0
    strategy.entry("Long", strategy.long, comment="▲ " + combo_name)
    strategy.exit("Long Exit", "Long",
         stop         = strategy.position_avg_price - stop_dist,
         limit        = strategy.position_avg_price + tgt1_dist,
         trail_price  = use_trail ? strategy.position_avg_price + tgt1_dist : na,
         trail_offset = use_trail ? trail_ticks * tick : na)

if enter_short and can_enter and strategy.position_size == 0
    strategy.entry("Short", strategy.short, comment="▼ " + combo_name)
    strategy.exit("Short Exit", "Short",
         stop         = strategy.position_avg_price + stop_dist,
         limit        = strategy.position_avg_price - tgt1_dist,
         trail_price  = use_trail ? strategy.position_avg_price - tgt1_dist : na,
         trail_offset = use_trail ? trail_ticks * tick : na)

// ── TRADE LEVEL LINES ─────────────────────────────────────────────
in_long  = strategy.position_size > 0
in_short = strategy.position_size < 0

if in_long and barstate.islast
    ep = strategy.position_avg_price
    line.new(bar_index - 5, ep,             bar_index, ep,             color=neut_c,                width=2)
    line.new(bar_index - 5, ep - stop_dist, bar_index, ep - stop_dist, color=bear_c,                width=1, style=line.style_dashed)
    line.new(bar_index - 5, ep + tgt1_dist, bar_index, ep + tgt1_dist, color=bull_c,                width=1, style=line.style_dashed)
    line.new(bar_index - 5, ep + tgt2_dist, bar_index, ep + tgt2_dist, color=color.new(bull_c, 40), width=1, style=line.style_dotted)
    label.new(bar_index, ep - stop_dist, "STOP " + str.tostring(math.round(ep - stop_dist, 2)), yloc=yloc.price, color=color.new(bear_c, 10), textcolor=color.white, style=label.style_label_left, size=size.small)
    label.new(bar_index, ep + tgt1_dist, "T1 "   + str.tostring(math.round(ep + tgt1_dist, 2)), yloc=yloc.price, color=color.new(bull_c, 10), textcolor=color.white, style=label.style_label_left, size=size.small)
    label.new(bar_index, ep + tgt2_dist, "T2 "   + str.tostring(math.round(ep + tgt2_dist, 2)), yloc=yloc.price, color=color.new(bull_c, 40), textcolor=color.white, style=label.style_label_left, size=size.small)

if in_short and barstate.islast
    ep = strategy.position_avg_price
    line.new(bar_index - 5, ep,             bar_index, ep,             color=neut_c,                width=2)
    line.new(bar_index - 5, ep + stop_dist, bar_index, ep + stop_dist, color=bear_c,                width=1, style=line.style_dashed)
    line.new(bar_index - 5, ep - tgt1_dist, bar_index, ep - tgt1_dist, color=bull_c,                width=1, style=line.style_dashed)
    line.new(bar_index - 5, ep - tgt2_dist, bar_index, ep - tgt2_dist, color=color.new(bull_c, 40), width=1, style=line.style_dotted)
    label.new(bar_index, ep + stop_dist, "STOP " + str.tostring(math.round(ep + stop_dist, 2)), yloc=yloc.price, color=color.new(bear_c, 10), textcolor=color.white, style=label.style_label_left, size=size.small)
    label.new(bar_index, ep - tgt1_dist, "T1 "   + str.tostring(math.round(ep - tgt1_dist, 2)), yloc=yloc.price, color=color.new(bull_c, 10), textcolor=color.white, style=label.style_label_left, size=size.small)
    label.new(bar_index, ep - tgt2_dist, "T2 "   + str.tostring(math.round(ep - tgt2_dist, 2)), yloc=yloc.price, color=color.new(bull_c, 40), textcolor=color.white, style=label.style_label_left, size=size.small)

// ── DASHBOARD TABLE ───────────────────────────────────────────────
var table dash = table.new(position.top_right, 2, 13,
     bgcolor=color.new(#0d1117, 5), border_color=color.new(color.white, 75),
     border_width=1, frame_color=color.new(color.white, 65), frame_width=1)

f_cell(col, row, txt, tc, bg) =>
    table.cell(dash, col, row, txt, text_color=tc, text_size=size.small, bgcolor=bg)

if barstate.islast
    bg0   = color.new(#161b22, 0)
    bg1   = color.new(#0d1117, 0)
    off   = color.new(#30363d, 0)
    hdr_c = color.new(color.white, 35)

    f_cell(0, 0, "FACTOR", hdr_c, bg0)
    f_cell(1, 0, "STATUS", hdr_c, bg0)

    ct_s = ctype == 1 ? "1  Inside" : ctype == 2 ? "2U  Up" : ctype == -2 ? "2D  Down" : ctype == 3 ? "3  Outside" : "—"
    ct_c = ctype == 2 ? bull_c : ctype == -2 ? bear_c : neut_c
    f_cell(0, 1, "Strat candle",  color.white, bg1)
    f_cell(1, 1, ct_s,            ct_c,        bg1)

    cb_s = combo_bull ? "▲ " + combo_name : combo_bear ? "▼ " + combo_name : "None"
    cb_c = combo_bull ? bull_c : combo_bear ? bear_c : off
    f_cell(0, 2, "Combo pattern", color.white, bg1)
    f_cell(1, 2, cb_s,            cb_c,        bg1)

    ic_s = icc_bull ? "Bull displacement" : icc_bear ? "Bear displacement" : "Normal"
    ic_c = icc_bull ? bull_c : icc_bear ? bear_c : off
    f_cell(0, 3, "ICC candle",    color.white, bg1)
    f_cell(1, 3, ic_s,            ic_c,        bg1)

    fv_s = fvg_bull ? "Bull FVG" : fvg_bear ? "Bear FVG" : "None"
    fv_c = fvg_bull ? bull_c : fvg_bear ? bear_c : off
    f_cell(0, 4, "Fair Value Gap", color.white, bg1)
    f_cell(1, 4, fv_s,             fv_c,        bg1)

    tf_s = str.tostring(tfc_score) + "/3  " + (m_bull ? "M▲" : "M▼") + "  " + (w_bull ? "W▲" : "W▼") + "  " + (d_bull ? "D▲" : "D▼")
    tf_c = tfc_bull ? bull_c : tfc_bear ? bear_c : neut_c
    f_cell(0, 5, "TFC score",  color.white, bg1)
    f_cell(1, 5, tf_s,         tf_c,        bg1)

    cd_s = near_d_close ? "OPEN  " + str.tostring(mins_to_d) + "m left" : "Waiting  " + str.tostring(mins_to_d) + "m"
    cd_c = near_d_close ? warn_c : off
    f_cell(0, 6, "CCT Daily",  color.white, bg1)
    f_cell(1, 6, cd_s,         cd_c,        bg1)

    cw_s = near_w_close ? "OPEN  " + str.tostring(mins_to_w) + "m left" : "Waiting"
    cw_c = near_w_close ? purp_c : off
    f_cell(0, 7, "CCT Weekly", color.white, bg1)
    f_cell(1, 7, cw_s,         cw_c,        bg1)

    lv_s = near_pdh ? "At PDH" : near_pdl ? "At PDL" : near_pwh ? "At PWH" : near_pwl ? "At PWL" : "None nearby"
    lv_c = near_level ? warn_c : off
    f_cell(0, 8, "Key level",  color.white, bg1)
    f_cell(1, 8, lv_s,         lv_c,        bg1)

    f_cell(0, 9, "Confirm bars", color.white, bg1)
    f_cell(1, 9, str.tostring(confirm_bars) + (confirm_bars == 1 ? " bar delay" : " bars delay"), neut_c, bg1)

    f_cell(0, 10, "---------", off, bg0)
    f_cell(1, 10, "---------", off, bg0)

    hp_s = hpa_long ? "▲  LONG" : hpa_short ? "▼  SHORT" : partial_long ? "~ Partial ▲" : partial_short ? "~ Partial ▼" : "No signal"
    hp_c = hpa_long ? bull_c : hpa_short ? bear_c : partial_long ? color.new(bull_c, 40) : partial_short ? color.new(bear_c, 40) : off
    f_cell(0, 11, "HPA Signal", color.white, bg0)
    f_cell(1, 11, hp_s,         hp_c,        bg0)

    pos_s = in_long ? "▲ LONG @ " + str.tostring(math.round(strategy.position_avg_price, 2)) : in_short ? "▼ SHORT @ " + str.tostring(math.round(strategy.position_avg_price, 2)) : "Flat"
    pos_c = in_long ? bull_c : in_short ? bear_c : neut_c
    f_cell(0, 12, "Position", color.white, bg0)
    f_cell(1, 12, pos_s,       pos_c,      bg0)

// ── P&L SCOREBOARD ────────────────────────────────────────────────
var table score = table.new(position.bottom_left, 2, 6,
     bgcolor=color.new(#0d1117, 5), border_color=color.new(color.white, 80),
     border_width=1, frame_color=color.new(color.white, 70), frame_width=1)

if barstate.islast
    bg0      = color.new(#161b22, 0)
    bg1      = color.new(#0d1117, 0)
    hdr      = color.new(color.white, 35)
    net_pnl  = strategy.netprofit
    win_r    = strategy.wintrades
    loss_r   = strategy.losstrades
    total_t  = strategy.closedtrades
    win_pct  = total_t > 0 ? math.round(win_r / total_t * 100) : 0
    avg_win  = strategy.grossprofit / math.max(win_r,  1)
    avg_loss = strategy.grossloss   / math.max(loss_r, 1)
    pf       = strategy.grossloss != 0 ? math.abs(strategy.grossprofit / strategy.grossloss) : 0
    pnl_c    = net_pnl >= 0 ? color.new(#26a69a, 0) : color.new(#ef5350, 0)

    table.cell(score, 0, 0, "PAPER P&L",    text_color=hdr,         text_size=size.small, bgcolor=bg0)
    table.cell(score, 1, 0, "VALUE",         text_color=hdr,         text_size=size.small, bgcolor=bg0)
    table.cell(score, 0, 1, "Net P&L",       text_color=color.white, text_size=size.small, bgcolor=bg1)
    table.cell(score, 1, 1, "$" + str.tostring(math.round(net_pnl)), text_color=pnl_c, text_size=size.small, bgcolor=bg1)
    table.cell(score, 0, 2, "Win rate",      text_color=color.white, text_size=size.small, bgcolor=bg1)
    table.cell(score, 1, 2, str.tostring(win_pct) + "% (" + str.tostring(win_r) + "/" + str.tostring(total_t) + ")", text_color=win_pct >= 50 ? color.new(#26a69a, 0) : color.new(#ef5350, 0), text_size=size.small, bgcolor=bg1)
    table.cell(score, 0, 3, "Profit factor", text_color=color.white, text_size=size.small, bgcolor=bg1)
    table.cell(score, 1, 3, str.tostring(math.round(pf, 2)), text_color=pf >= 1.5 ? color.new(#26a69a, 0) : color.new(#ef5350, 0), text_size=size.small, bgcolor=bg1)
    table.cell(score, 0, 4, "Avg win/loss",  text_color=color.white, text_size=size.small, bgcolor=bg1)
    table.cell(score, 1, 4, "$" + str.tostring(math.round(avg_win)) + " / $" + str.tostring(math.round(math.abs(avg_loss))), text_color=color.new(#90a4ae, 0), text_size=size.small, bgcolor=bg1)
    table.cell(score, 0, 5, "Open trades",   text_color=color.white, text_size=size.small, bgcolor=bg0)
    table.cell(score, 1, 5, str.tostring(strategy.opentrades), text_color=color.new(#90a4ae, 0), text_size=size.small, bgcolor=bg0)

// ── ALERTS ────────────────────────────────────────────────────────
alertcondition(hpa_long,                "HPA LONG — Full confluence",  "HPA LONG: Strat+ICC+CCT+TFC aligned on {{ticker}} {{interval}} @ {{close}}")
alertcondition(hpa_short,               "HPA SHORT — Full confluence", "HPA SHORT: Strat+ICC+CCT+TFC aligned on {{ticker}} {{interval}} @ {{close}}")
alertcondition(partial_long,            "Partial LONG setup",          "Partial Long: combo+TFC on {{ticker}} {{interval}}")
alertcondition(partial_short,           "Partial SHORT setup",         "Partial Short: combo+TFC on {{ticker}} {{interval}}")
alertcondition(c123_bull,               "1-2-3 Bullish combo",         "1-2-3 BULL on {{ticker}} {{interval}} @ {{close}}")
alertcondition(c123_bear,               "1-2-3 Bearish combo",         "1-2-3 BEAR on {{ticker}} {{interval}} @ {{close}}")
alertcondition(c22_bull,                "2-2 Reversal Long",           "2-2 Reversal LONG on {{ticker}} {{interval}}")
alertcondition(c22_bear,                "2-2 Reversal Short",          "2-2 Reversal SHORT on {{ticker}} {{interval}}")
alertcondition(near_d_close,            "CCT Daily window open",       "CCT Daily window open on {{ticker}} {{interval}}")
alertcondition(near_cct and combo_bull, "CCT + Bull combo",            "CCT window + Bull combo on {{ticker}} {{interval}}")
alertcondition(near_cct and combo_bear, "CCT + Bear combo",            "CCT window + Bear combo on {{ticker}} {{interval}}")
alertcondition(icc_bull and combo_bull, "ICC + Bull combo",            "ICC Bull displacement + combo on {{ticker}}")
alertcondition(icc_bear and combo_bear, "ICC + Bear combo",            "ICC Bear displacement + combo on {{ticker}}")
alertcondition(near_level and hpa_long,  "HPA Long at key level",      "HPA LONG at key level on {{ticker}}")
alertcondition(near_level and hpa_short, "HPA Short at key level",     "HPA SHORT at key level on {{ticker}}")
