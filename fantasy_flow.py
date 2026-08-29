diff --git a/money_flow.py b/money_flow.py
index 7169497..327f020 100644
--- a/money_flow.py
+++ b/money_flow.py
@@ -1820,6 +1820,121 @@ def build_equilibrium_history_daily(assets, days=EQUILIBRIUM_HISTORY_DAYS, sizin
     return _build_equilibrium_frames(assets, "daily_ohlc", days, id_prefix, "%Y-%m-%d", sizing=sizing)
 
 
+def _build_quadrant_frames(assets, ohlc_key, periods, id_prefix, ts_format, size_lookback=20, trail_len=3):
+    """Real candle-by-candle frames for the RSI x %-change quadrant-scatter
+    panels (see render_quadrant_svg()) -- same 6 EQUILIBRIUM_TICKERS, same
+    real-data-only replay contract as _build_equilibrium_frames() (no
+    simulated/invented values anywhere), but each frame_asset carries a
+    1-candle %-change ("pct1", the x-axis) alongside its RSI (the y-axis),
+    plus a short "trail" of up to `trail_len` recent (rsi, pct1) points
+    (oldest -> newest, ending at the current point) so the panel can draw a
+    short path leading into the current bubble -- e.g. trail_len=3 draws
+    the path from 2-candles-ago -> 1-candle-ago -> now.
+
+    Bubble size is always market size (trailing average dollar volume,
+    same convention/scope as the "volume" sizing equilibrium panels) --
+    there's no legacy |%change| sizing option here since %change is
+    already the x-axis.
+
+    Returns {"frames": [...], "x_domain": max_abs_pct1} (frames oldest ->
+    newest; x_domain is a single symmetric bound, padded 10%, computed once
+    across every frame/ticker so the x-axis doesn't rescale as you scrub),
+    or None under the same insufficient-history conditions as
+    _build_equilibrium_frames()."""
+    by_ticker = {a["ticker"]: a for a in assets if not a.get("unavailable")}
+    per_ticker_records = {}
+    for ticker, _display_name in EQUILIBRIUM_TICKERS:
+        a = by_ticker.get(ticker)
+        series = a.get(ohlc_key) if a else None
+        if not series or len(series) < RSI_PERIOD + 1 + 1:
+            return None
+        mult = contract_multiplier_for(ticker)
+        closes = [c["close"] for c in series]
+        rsi_vals = rsi_series(closes)
+        records = []  # oldest -> newest; only candles where RSI and a 1-candle change are defined
+        for idx in range(1, len(closes)):
+            if rsi_vals[idx] is None:
+                continue
+            prev = closes[idx - 1]
+            pct1 = ((closes[idx] - prev) / prev * 100) if prev else None
+            dollar_vol = avg_dollar_volume(series[:idx + 1], lookback=size_lookback, multiplier=mult)
+            records.append({
+                "ts": series[idx].get("ts"), "rsi": rsi_vals[idx], "pct1": pct1,
+                "dollar_vol": dollar_vol,
+            })
+        if len(records) < 2:
+            return None
+        per_ticker_records[ticker] = records
+
+    frame_count = min(min(len(v) for v in per_ticker_records.values()), periods + 1)
+    frames = []
+    max_abs_pct1 = 0.0
+    for offset in range(frame_count):
+        pos_from_end = frame_count - 1 - offset  # 0 = most recent candle, larger = further back
+        raw_rows = {t: per_ticker_records[t][-(pos_from_end + 1)] for t, _ in EQUILIBRIUM_TICKERS}
+
+        frame_sizes = {t: (raw_rows[t]["dollar_vol"] or 0.0) for t, _ in EQUILIBRIUM_TICKERS}
+        radii = compute_relative_bubble_radii_from_dollar_volume(frame_sizes)
+
+        frame_assets = []
+        ts_candidates = []
+        for ticker, display_name in EQUILIBRIUM_TICKERS:
+            recs = per_ticker_records[ticker]
+            idx_in_recs = len(recs) - 1 - pos_from_end
+            trail_start = max(0, idx_in_recs - (trail_len - 1))
+            trail_slice = recs[trail_start:idx_in_recs + 1]  # oldest -> newest, ends at current
+            if len(trail_slice) >= 2:
+                # Only draw the endpoints -- one straight line from the
+                # oldest point in the window (e.g. 2 candles back, when
+                # trail_len=3) directly to the current point, skipping the
+                # intermediate candle(s) rather than a multi-segment path.
+                trail_slice = [trail_slice[0], trail_slice[-1]]
+            trail = [
+                {"rsi": round(r["rsi"], 2), "pct1": None if r["pct1"] is None else round(r["pct1"], 3)}
+                for r in trail_slice
+            ]
+            for r in trail:
+                if r["pct1"] is not None:
+                    max_abs_pct1 = max(max_abs_pct1, abs(r["pct1"]))
+            rec = recs[idx_in_recs]
+            x_for_color = max(-1.0, min(1.0, (rec["rsi"] - 50.0) / 50.0))
+            frame_assets.append({
+                "id": f"{id_prefix}{_eq_dom_id(ticker)}",
+                "ticker": ticker,
+                "name": display_name,
+                "rsi": round(rec["rsi"], 1),
+                "pct1": None if rec["pct1"] is None else round(rec["pct1"], 3),
+                "dollar_vol": rec["dollar_vol"],
+                "bubble_r": round(radii.get(ticker, RSI_BUBBLE_MIN_R), 3),
+                "color": equilibrium_color_for_x(x_for_color),
+                "trail": trail,
+            })
+            if rec["ts"] is not None:
+                ts_candidates.append(rec["ts"])
+        frame_ts = max(ts_candidates) if ts_candidates else None
+        frames.append({
+            "ts": frame_ts.strftime(ts_format) if frame_ts else None,
+            "assets": frame_assets,
+        })
+
+    x_domain = round(max(0.5, max_abs_pct1 * 1.1), 3)  # never collapse to 0; 10% padding
+    return {"frames": frames, "x_domain": x_domain}
+
+
+def build_quadrant_history(assets, hours=EQUILIBRIUM_HISTORY_HOURS, id_prefix="qh-"):
+    """Hourly RSI x %-change quadrant replay -- see _build_quadrant_frames()
+    for the shared logic. 1-hour %-change on the x-axis, market-size bubble
+    sizing, a 3-candle trail behind each bubble."""
+    return _build_quadrant_frames(assets, "hourly_ohlc", hours, id_prefix, "%Y-%m-%d %H:%M UTC")
+
+
+def build_quadrant_history_daily(assets, days=EQUILIBRIUM_HISTORY_DAYS, id_prefix="qd-"):
+    """Daily RSI x %-change quadrant replay -- see _build_quadrant_frames()
+    for the shared logic. 1-day %-change on the x-axis, market-size bubble
+    sizing, a 3-candle trail behind each bubble."""
+    return _build_quadrant_frames(assets, "daily_ohlc", days, id_prefix, "%Y-%m-%d")
+
+
 def _eq_well_y(x_norm, height, base_frac=0.28, amp_frac=0.42):
     """Same potential-well shape used both server-side (initial paint) and
     client-side (JS port in EQUILIBRIUM_SLIDER_SCRIPT): lowest (largest y
@@ -1895,6 +2010,77 @@ def render_equilibrium_svg(frame_assets, width=EQUILIBRIUM_SVG_WIDTH, height=EQU
     )
 
 
+def render_quadrant_svg(frame_assets, x_domain, width=EQUILIBRIUM_SVG_WIDTH, height=EQUILIBRIUM_SVG_HEIGHT,
+                         lookback_desc="recent history"):
+    """SVG scene for one quadrant-scatter frame -- RSI on the y-axis (100 at
+    top, 0 at bottom), %-change on the x-axis (symmetric around 0, bounded
+    by `x_domain`). Each asset draws as a short trail (its last few
+    (rsi, pct1) points, oldest -> newest) ending in a bubble at its current
+    reading, sized by market size -- mirrors render_equilibrium_svg()'s
+    id-per-element convention (already scoped by panel, e.g. "qh-ZNF") so
+    the slider's JS can move everything for other frames without
+    re-rendering the SVG."""
+    margin_l, margin_r = 46, 20
+    margin_t, margin_b = 28, 34
+    plot_w = width - margin_l - margin_r
+    plot_h = height - margin_t - margin_b
+
+    def px_of(pct):
+        pct = max(-x_domain, min(x_domain, pct if pct is not None else 0.0))
+        return margin_l + (pct + x_domain) / (2 * x_domain) * plot_w
+
+    def py_of(rsi):
+        rsi = max(0.0, min(100.0, rsi))
+        return margin_t + (1 - rsi / 100.0) * plot_h
+
+    mid_x, mid_y = px_of(0), py_of(50)
+
+    parts = [
+        f'<line x1="{margin_l}" y1="{mid_y:.1f}" x2="{width - margin_r}" y2="{mid_y:.1f}" '
+        f'stroke="rgba(107,114,128,0.35)" stroke-width="1" stroke-dasharray="3,5"/>',
+        f'<line x1="{mid_x:.1f}" y1="{margin_t}" x2="{mid_x:.1f}" y2="{height - margin_b}" '
+        f'stroke="rgba(79,216,232,0.30)" stroke-width="1" stroke-dasharray="3,5"/>',
+        f'<text x="{margin_l}" y="{margin_t - 10}" font-size="10" '
+        f'font-family="IBM Plex Mono, monospace" fill="#6B7280">RSI 100 &middot; overbought</text>',
+        f'<text x="{margin_l}" y="{height - margin_b + 16}" font-size="10" '
+        f'font-family="IBM Plex Mono, monospace" fill="#6B7280">RSI 0 &middot; oversold</text>',
+        f'<text x="{width - margin_r}" y="{mid_y - 6:.1f}" text-anchor="end" font-size="10" '
+        f'font-family="IBM Plex Mono, monospace" fill="#3ECF8E">+{x_domain:.2f}% &rarr;</text>',
+        f'<text x="{margin_l}" y="{mid_y - 6:.1f}" font-size="10" '
+        f'font-family="IBM Plex Mono, monospace" fill="#FF5C5C">&larr; -{x_domain:.2f}%</text>',
+    ]
+
+    for a in frame_assets:
+        dom_id, color, r = a["id"], a["color"], a["bubble_r"]
+        trail_pts = [(px_of(t["pct1"]), py_of(t["rsi"])) for t in a["trail"] if t["pct1"] is not None]
+        trail_path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in trail_pts) if len(trail_pts) >= 2 else ""
+        px, py = px_of(a["pct1"]), py_of(a["rsi"])
+        parts.append(
+            f'<path id="trail-{dom_id}" d="{trail_path}" fill="none" stroke="{color}" '
+            f'stroke-width="1.4" stroke-opacity="0.55" stroke-linecap="round" stroke-linejoin="round" class="eq-trail"/>'
+        )
+        parts.append(
+            f'<circle id="b-{dom_id}" cx="{px:.1f}" cy="{py:.1f}" r="{r:.2f}" fill="{color}" '
+            f'fill-opacity="0.30" stroke="{color}" stroke-width="1.6" class="eq-bubble"/>'
+        )
+        parts.append(
+            f'<text id="l-{dom_id}" x="{px:.1f}" y="{py - r - 8:.1f}" text-anchor="middle" font-size="11" '
+            f'font-weight="600" font-family="IBM Plex Mono, monospace" fill="{color}" class="eq-label">{html.escape(a["name"])}</text>'
+        )
+        pct_str = "n/a" if a["pct1"] is None else f'{a["pct1"]:+.2f}%'
+        parts.append(
+            f'<text id="r-{dom_id}" x="{px:.1f}" y="{py + r + 14:.1f}" text-anchor="middle" font-size="9.5" '
+            f'font-family="IBM Plex Mono, monospace" fill="rgba(232,230,222,0.55)" class="eq-rsi">'
+            f'{a["rsi"]:.0f} &middot; {pct_str}</text>'
+        )
+
+    return (
+        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" role="img" '
+        f'aria-label="RSI versus percent-change quadrant, scrub the slider to replay {html.escape(lookback_desc)}">'
+        f'{"".join(parts)}</svg>'
+    )
+
+
 EQUILIBRIUM_CSS = """
   #app{
     --bg:#0B0E14; --well:#3A4A5C; --neutral:#9CA3AF; --cyan:#4FD8E8;
@@ -1924,6 +2110,7 @@ EQUILIBRIUM_CSS = """
   #app .stage-wrap svg .eq-bubble{ transition: cx 0.35s ease, cy 0.35s ease, r 0.35s ease, fill 0.35s ease, stroke 0.35s ease; }
   #app .stage-wrap svg .eq-label,
   #app .stage-wrap svg .eq-rsi{ transition: x 0.35s ease, y 0.35s ease, fill 0.35s ease; }
+  #app .stage-wrap svg .eq-trail{ transition: d 0.35s ease, stroke 0.35s ease; }
   #app .hud{ display:flex; justify-content:space-between; padding:0 8px;
     font-family:'IBM Plex Mono',monospace; font-size:11px; color:var(--dim); }
   #app .side-label{display:flex; flex-direction:column; gap:2px;}
@@ -2098,6 +2285,162 @@ def _render_equilibrium_panel(history, *, scope, section_title, section_note, cu
 <script>{script}</script>"""
 
 
+def _quadrant_slider_script(frames, dom_ids, *, scope, width, height, x_domain, current_label, ago_suffix, pct_suffix):
+    """JS twin of _equilibrium_slider_script() for the quadrant-scatter
+    panels: replays real precomputed frames (rsi, pct1, trail, dollar_vol)
+    as the slider moves -- no physics, no invented values. Also redraws
+    each asset's trail path to the frame's (up to 3) trail points. Wrapped
+    in an IIFE and scoped by `scope` ("qh"/"qd") so this can sit alongside
+    the well panels' scripts on the same page without id/const collisions."""
+    frames_json = json.dumps(frames)
+    ids_json = json.dumps(dom_ids)
+    current_label_json = json.dumps(current_label)
+    ago_suffix_json = json.dumps(ago_suffix)
+    pct_suffix_json = json.dumps(pct_suffix)
+    return f"""
+(function() {{
+  const EQ_FRAMES = {frames_json};
+  const EQ_IDS = {ids_json};
+  const EQ_W = {width}, EQ_H = {height}, EQ_XDOM = {x_domain};
+  const EQ_ML = 46, EQ_MR = 20, EQ_MT = 28, EQ_MB = 34;
+  const EQ_PW = EQ_W - EQ_ML - EQ_MR, EQ_PH = EQ_H - EQ_MT - EQ_MB;
+  function eqPxOf(pct){{
+    pct = Math.max(-EQ_XDOM, Math.min(EQ_XDOM, (pct === null || pct === undefined) ? 0 : pct));
+    return EQ_ML + (pct + EQ_XDOM) / (2 * EQ_XDOM) * EQ_PW;
+  }}
+  function eqPyOf(rsi){{
+    rsi = Math.max(0, Math.min(100, rsi));
+    return EQ_MT + (1 - rsi / 100) * EQ_PH;
+  }}
+  function eqFmtDollar(v){{
+    if (v === null || v === undefined) return 'n/a';
+    v = Math.abs(v);
+    const units = [[1e12,'T'],[1e9,'B'],[1e6,'M'],[1e3,'K']];
+    for (const [t, s] of units) {{ if (v >= t) return '$' + (v/t).toFixed(1) + s; }}
+    return '$' + v.toFixed(0);
+  }}
+
+  const slider = document.getElementById('slider-{scope}');
+  const scrubLabel = document.getElementById('scrubLabel-{scope}');
+  const scrubTs = document.getElementById('scrubTs-{scope}');
+  const leftCountEl = document.getElementById('leftCount-{scope}');
+  const rightCountEl = document.getElementById('rightCount-{scope}');
+  const legendEl = document.getElementById('legend-{scope}');
+  if (!slider) return;
+
+  function renderFrame(frameIdx){{
+    const frame = EQ_FRAMES[frameIdx];
+    let left = 0, right = 0;
+    frame.assets.forEach(a => {{
+      const px = eqPxOf(a.pct1), py = eqPyOf(a.rsi);
+      const bubble = document.getElementById('b-' + a.id);
+      const label = document.getElementById('l-' + a.id);
+      const rsiText = document.getElementById('r-' + a.id);
+      const trail = document.getElementById('trail-' + a.id);
+      if (bubble) {{
+        bubble.setAttribute('cx', px); bubble.setAttribute('cy', py); bubble.setAttribute('r', a.bubble_r);
+        bubble.setAttribute('fill', a.color); bubble.setAttribute('stroke', a.color);
+      }}
+      if (label) {{ label.setAttribute('x', px); label.setAttribute('y', py - a.bubble_r - 8); label.setAttribute('fill', a.color); }}
+      if (rsiText) {{
+        const pctStr = (a.pct1 === null || a.pct1 === undefined) ? 'n/a' : (a.pct1 >= 0 ? '+' : '') + a.pct1.toFixed(2) + '%';
+        rsiText.setAttribute('x', px); rsiText.setAttribute('y', py + a.bubble_r + 14);
+        rsiText.textContent = Math.round(a.rsi) + ' \\u00b7 ' + pctStr;
+      }}
+      if (trail) {{
+        const pts = (a.trail || []).filter(t => t.pct1 !== null && t.pct1 !== undefined)
+          .map(t => eqPxOf(t.pct1).toFixed(1) + ',' + eqPyOf(t.rsi).toFixed(1));
+        trail.setAttribute('d', pts.length >= 2 ? ('M ' + pts.join(' L ')) : '');
+        trail.setAttribute('stroke', a.color);
+      }}
+      if (a.rsi < 49) left++; else if (a.rsi > 51) right++;
+    }});
+    leftCountEl.textContent = left;
+    rightCountEl.textContent = right;
+    scrubTs.textContent = frame.ts ? ('Showing: ' + frame.ts) : 'Showing: n/a';
+    const periodsAgo = EQ_FRAMES.length - 1 - frameIdx;
+    scrubLabel.textContent = periodsAgo === 0 ? {current_label_json} : (periodsAgo + {ago_suffix_json});
+    const rows = frame.assets.slice().sort((a, b) => b.rsi - a.rsi).map(a => {{
+      const pctStr = (a.pct1 === null || a.pct1 === undefined) ? 'n/a' : (a.pct1 >= 0 ? '+' : '') + a.pct1.toFixed(2) + {pct_suffix_json};
+      const volStr = eqFmtDollar(a.dollar_vol);
+      return '<div class="leg-row"><span><span class="dot" style="background:' + a.color + '"></span>' + a.name +
+        '</span><span><span class="rsi" style="color:' + a.color + '">' + Math.round(a.rsi) +
+        '</span><span class="pct">' + volStr + ' \\u00b7 ' + pctStr + '</span></span></div>';
+    }});
+    legendEl.innerHTML = rows.join('');
+  }}
+
+  slider.addEventListener('input', () => renderFrame(+slider.value));
+  renderFrame(+slider.value);
+}})();
+"""
+
+
+def _render_quadrant_panel(history, *, scope, section_title, section_note, current_label,
+                            ago_suffix, pct_suffix, lookback_word, unavailable_msg):
+    """Build one scrubbable RSI x %-change quadrant panel (section heading +
+    stage/slider + legend/note, plus its own IIFE-wrapped <script>) --
+    twin of _render_equilibrium_panel() for the quadrant-scatter layout.
+    `scope` ("qh"/"qd") keeps every element id in this panel distinct from
+    the other panels sharing the same #app."""
+    if history is None:
+        return f"""
+    <div class="eq-section-head"><h2>{html.escape(section_title)}</h2></div>
+    <div class="eq-unavailable">{html.escape(unavailable_msg)}</div>"""
+
+    frames = history["frames"]
+    x_domain = history["x_domain"]
+    current = frames[-1]
+    max_back = len(frames) - 1
+    dom_ids = [a["id"] for a in current["assets"]]
+    lookback_desc = f"the last {max_back} {lookback_word}{'s' if max_back != 1 else ''}"
+    svg = render_quadrant_svg(current["assets"], x_domain, lookback_desc=lookback_desc)
+
+    def _leg_row(a):
+        pct_str = "n/a" if a["pct1"] is None else f'{a["pct1"]:+.2f}{pct_suffix}'
+        vol_str = format_dollar_compact(a["dollar_vol"])
+        return (
+            f'<div class="leg-row"><span><span class="dot" style="background:{a["color"]}"></span>'
+            f'{html.escape(a["name"])}</span>'
+            f'<span><span class="rsi" style="color:{a["color"]}">{a["rsi"]:.0f}</span>'
+            f'<span class="pct">{vol_str} · {pct_str}</span></span></div>'
+        )
+
+    legend_html = "".join(
+        _leg_row(a) for a in sorted(current["assets"], key=lambda a: a["rsi"], reverse=True)
+    )
+    left_count = sum(1 for a in current["assets"] if a["rsi"] < 49)
+    right_count = sum(1 for a in current["assets"] if a["rsi"] > 51)
+    script = _quadrant_slider_script(
+        frames, dom_ids, scope=scope, width=EQUILIBRIUM_SVG_WIDTH, height=EQUILIBRIUM_SVG_HEIGHT,
+        x_domain=x_domain, current_label=current_label, ago_suffix=ago_suffix, pct_suffix=pct_suffix,
+    )
+
+    return f"""
+    <div class="eq-section-head"><h2>{html.escape(section_title)}</h2></div>
+    <div class="eq-row">
+      <div class="stage-wrap">
+        {svg}
+        <div class="hud">
+          <div class="side-label left">OVERSOLD (RSI &lt; 50)<div class="n" id="leftCount-{scope}">{left_count}</div></div>
+          <div class="side-label right">OVERBOUGHT (RSI &gt; 50)<div class="n" id="rightCount-{scope}">{right_count}</div></div>
+        </div>
+        <div class="scrubber">
+          <div class="scrub-row">
+            <input type="range" id="slider-{scope}" min="0" max="{max_back}" value="{max_back}">
+            <span class="scrubLabel" id="scrubLabel-{scope}">{html.escape(current_label)}</span>
+          </div>
+          <div class="scrubTs" id="scrubTs-{scope}">Showing: {html.escape(current["ts"] or "n/a")}</div>
+        </div>
+      </div>
+      <aside>
+        <div class="legend" id="legend-{scope}">{legend_html}</div>
+        <div class="note">{section_note}</div>
+      </aside>
+    </div>
+<script>{script}</script>"""
+
+
 def render_equilibrium_app(assets, now, embedded=False):
     """Build the `<div id="app">...</div>` markup for the Equilibrium -- RSI
     Reversion view: FOUR real-data scrubbable panels stacked in the same
@@ -2232,6 +2575,55 @@ def render_equilibrium_app(assets, now, embedded=False):
         unavailable_msg=_unavailable_msg("daily"),
     )
 
+    # -- Quadrant-scatter panels: RSI (y) x %-change (x) instead of the
+    # potential well, with a short 3-candle trail leading into each bubble.
+    # Bubble size is always market size here (see _build_quadrant_frames()).
+    quadrant_hourly_history = build_quadrant_history(assets)
+    quadrant_hourly_back = (
+        (len(quadrant_hourly_history["frames"]) - 1) if quadrant_hourly_history else EQUILIBRIUM_HISTORY_HOURS
+    )
+    quadrant_hourly_note = (
+        "Same six assets, plotted on <b>RSI (y-axis)</b> versus <b>1-hour "
+        f"% change (x-axis)</b> instead of the potential well. Drag the "
+        f"slider to replay the last {quadrant_hourly_back} hours — every "
+        "position is an actual past reading, not invented or simulated "
+        "motion."
+        "<br><br>"
+        "Each bubble is sized by <b>market size</b> — trailing average "
+        "dollar volume, relative to the other five at that same hour — the "
+        "same convention as the market-size well panels above. The short "
+        "trail behind each bubble connects its last 3 hourly readings, so "
+        "you can see which way it arrived at its current spot, not just "
+        "where it is right now."
+    )
+    quadrant_hourly_panel = _render_quadrant_panel(
+        quadrant_hourly_history, scope="qh", section_title="Hourly — RSI × % change (quadrant)",
+        section_note=quadrant_hourly_note, current_label="Current hour", ago_suffix="h ago",
+        pct_suffix="% 1h", lookback_word="hour", unavailable_msg=_unavailable_msg("hourly"),
+    )
+
+    quadrant_daily_history = build_quadrant_history_daily(assets)
+    quadrant_daily_back = (
+        (len(quadrant_daily_history["frames"]) - 1) if quadrant_daily_history else EQUILIBRIUM_HISTORY_DAYS
+    )
+    quadrant_daily_note = (
+        "Same six assets, same <b>RSI × % change</b> layout, on <b>daily</b> "
+        f"closes instead of hourly ones. Drag the slider to replay the last "
+        f"{quadrant_daily_back} trading days — every position is an actual "
+        "past reading pulled from real daily closes."
+        "<br><br>"
+        "Bubble size is market size — trailing average dollar volume over "
+        "the last 20 trading days, relative to the other five that day — a "
+        "separate normalization from every other panel/section in this "
+        "report. The trail connects each asset's last 3 trading days' "
+        "readings."
+    )
+    quadrant_daily_panel = _render_quadrant_panel(
+        quadrant_daily_history, scope="qd", section_title="Daily — RSI × % change (quadrant)",
+        section_note=quadrant_daily_note, current_label="Today", ago_suffix="d ago",
+        pct_suffix="% 1d", lookback_word="day", unavailable_msg=_unavailable_msg("daily"),
+    )
+
     return f"""<div id="app" style="min-height:{min_height};">
   <header>
     <div>
@@ -2247,6 +2639,8 @@ def render_equilibrium_app(assets, now, embedded=False):
     <div class="eq-divider"></div>{daily_legacy_panel}
     <div class="eq-divider"></div>{hourly_panel}
     <div class="eq-divider"></div>{daily_panel}
+    <div class="eq-divider"></div>{quadrant_hourly_panel}
+    <div class="eq-divider"></div>{quadrant_daily_panel}
   </main>
 </div>"""
