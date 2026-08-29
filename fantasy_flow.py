#!/usr/bin/env python3
"""
Fantasy Quadrant: RSI-style "hot/cold index" plotted against real weekly
performance, with a short trail behind each player showing their last few
weeks' trajectory -- same visual mechanic as moneyflow-update's Equilibrium
quadrant panels, ported from tickers/price-closes to fantasy players/weekly
points.

Three views, all built from the same fetched data:
  1. Hot/cold index x points scored that week (bubble = usage share)
  2. Hot/cold index x points PER TOUCH that week -- an efficiency read
     instead of a volume read, same y-axis, same bubble sizing
  3. A 4-season points-per-week trend grid, one small chart per player,
     covering as much of the last 4 regular seasons as actually exists for
     that player (a rookie just gets however many weeks they have)

All three share a position filter (QB/RB/WR/TE/ALL) built from whichever
positions are actually present in the watchlist.

Meant to run on GitHub Actions on a weekly cron (e.g. Tuesday morning, after
Monday Night Football has posted final stats) or any machine with normal
internet access -- NOT inside a locked-down sandbox with a network
allowlist, since it talks to api.sleeper.app.

Writes one file to the repo each run:
    index.html - self-contained visual (dark, scrubbable quadrant scatter
                 x2 + static trend grid), so the repo always has an
                 up-to-date snapshot you can open directly or serve via
                 GitHub Pages -- no external tooling required to view it.

Data source: the Sleeper API (https://docs.sleeper.com/) -- free, read-only,
no API key required. Endpoints used:
    /v1/state/nfl                          - current season/week
    /v1/players/nfl                        - full player directory (~5MB;
                                              Sleeper's own docs ask that
                                              this be fetched at most once a
                                              day, which a weekly cron
                                              comfortably respects)
    /v1/stats/nfl/regular/{season}/{week}  - one week's box-score stats for
                                              every NFL player who played.
                                              Called once per (season, week)
                                              needed across all three views
                                              -- the quadrant panels' recent
                                              window and the trend grid's
                                              4-season lookback both draw
                                              from the same fetch.

Env vars (all optional):
    FANTASY_SCORING   - "ppr" (default), "half_ppr", or "std". Which Sleeper
                         points field to plot.
    FANTASY_WATCHLIST - comma-separated player full names to override the
                         default WATCHLIST below, e.g.
                         "Christian McCaffrey,Justin Jefferson,..."
    FANTASY_WEEKS     - how many recent weeks the two quadrant panels'
                         sliders cover (default 10), capped by however many
                         weeks have actually been played this season.
    FANTASY_TREND_SEASONS - how many seasons back the trend grid covers,
                         including the current one (default 4).
"""

import html
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

SLEEPER_BASE = "https://api.sleeper.app/v1"

# Default players to track if FANTASY_WATCHLIST isn't set. Names are matched
# case-insensitively against Sleeper's players/nfl directory at runtime (not
# hardcoded player_ids, since those are opaque strings best resolved live
# rather than baked in and risking going stale).
WATCHLIST = [
    "Christian McCaffrey", "Travis Kelce", "Amon-Ra St. Brown", "De'Von Achane",
    "Justin Jefferson", "Rhamondre Stevenson", "George Pickens", "Kyle Pitts",
]

RSI_PERIOD = 4  # shorter window than the stock version's 14 -- a season is only ~17 weeks
DEFAULT_WEEKS_HISTORY = 10
DEFAULT_TREND_SEASONS = 4
TREND_MAX_WEEKS_PER_SEASON = 18  # current NFL regular-season length; harmless if a season had 17
REQUEST_TIMEOUT = 20

SCORING_FIELD = {
    "ppr": "pts_ppr",
    "half_ppr": "pts_half_ppr",
    "std": "pts_std",
}

POSITION_ORDER = ["QB", "RB", "WR", "TE"]  # display order for the filter bar; anything else is appended after


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-quadrant/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_state():
    """Current NFL season/week per Sleeper -- used to know how far back the
    quadrant sliders' weekly history can actually go this season, and which
    4 seasons the trend grid should request."""
    return fetch_json(f"{SLEEPER_BASE}/state/nfl")


def get_players_directory():
    """Full player_id -> {full_name, position, team, ...} directory. Large
    (~5MB) -- Sleeper's docs ask this be fetched at most once a day; a
    weekly cron run comfortably respects that on its own, no extra caching
    needed here."""
    return fetch_json(f"{SLEEPER_BASE}/players/nfl")


_WEEK_STATS_CACHE = {}


def get_week_stats(season, week):
    """One week's stats for every NFL player who recorded a stat line that
    week: player_id -> {pts_ppr, pts_half_ppr, pts_std, rec_tgt, rec,
    rush_att, ...}. Returns {} (not None) if the endpoint has nothing for
    that week yet, so callers can treat an ungenerated future week the same
    as an empty one. Cached in-process per (season, week) since the trend
    grid's 4-season lookback and the quadrant panels' recent window can
    both ask for the current season's most recent weeks."""
    key = (str(season), int(week))
    if key in _WEEK_STATS_CACHE:
        return _WEEK_STATS_CACHE[key]
    try:
        data = fetch_json(f"{SLEEPER_BASE}/stats/nfl/regular/{season}/{week}") or {}
    except Exception as e:
        print(f" [warn] stats fetch failed for {season} week {week}: {e}", file=sys.stderr)
        data = {}
    _WEEK_STATS_CACHE[key] = data
    return data


def resolve_watchlist_ids(players_dir, names):
    """Case-insensitive full_name match against the players directory.
    Returns [(player_id, display_name, position, team), ...] in the same
    order as `names`; a name with no match is skipped with a warning rather
    than silently dropped, so a typo/retirement doesn't fail the whole run."""
    by_name = {}
    for pid, info in players_dir.items():
        name = info.get("full_name")
        if name:
            by_name.setdefault(name.strip().lower(), pid)

    out = []
    for name in names:
        pid = by_name.get(name.strip().lower())
        if pid is None:
            print(f" [warn] no Sleeper player match for '{name}' -- skipping", file=sys.stderr)
            continue
        info = players_dir[pid]
        out.append((pid, info.get("full_name", name), info.get("position") or "?", info.get("team") or "FA"))
    return out


def team_opportunity_totals(week_stats, players_dir):
    """{team: total opportunities} for one week, where "opportunities" =
    rush attempts + targets (carries + targets) summed across every player
    on that team who recorded a stat line that week. This is the volume
    proxy that stands in for a stock's dollar volume -- a direct measure of
    how much of the offense's ball-distribution went through that team's
    various backs/receivers, independent of how well any one of them
    performed with it."""
    totals = {}
    for pid, stats in week_stats.items():
        info = players_dir.get(pid)
        if not info:
            continue
        team = info.get("team")
        if not team:
            continue
        opp = (stats.get("rush_att") or 0) + (stats.get("rec_tgt") or 0)
        if opp:
            totals[team] = totals.get(team, 0.0) + opp
    return totals


def rsi_series(values, period=RSI_PERIOD):
    """Wilder's RSI over a list of values (oldest -> newest) -- identical
    math to the stock version, just run on weekly fantasy points instead of
    price closes. Returns a same-length list: None before the first full
    `period`-value window, then 0-100 from there on."""
    n = len(values)
    out = [None] * n
    if n < period + 1:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        delta = values[i] - values[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    def rsi_from(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = rsi_from(avg_gain, avg_loss)
    return out


def build_player_frames(watchlist, weekly_stats_by_week, players_dir, scoring_field, trail_len=3):
    """For each (player_id, name, pos, team) in `watchlist`, build a list of
    per-week frames {week, pts, ppt, usage, rsi, trail_pts, trail_ppt},
    where:
      - pts: that week's fantasy points in the configured scoring format
      - ppt: points PER TOUCH that week (touches = rush attempts +
        receptions, NOT targets -- an actual touch, not an opportunity).
        None for a week with zero touches, so the points-per-touch panel
        can skip plotting that point rather than divide by zero.
      - usage: player's opportunities (rush_att + rec_tgt) as a share of
        their team's total that week -- 0..1, the bubble-size input for
        BOTH quadrant panels
      - rsi: RSI(RSI_PERIOD) run on this player's own points series so far
        -- the shared y-axis for both quadrant panels
      - trail_pts / trail_ppt: [] / [start, current] -- start is
        `trail_len - 1` weeks back (or the earliest available), so each
        panel can draw one straight line from there directly to the
        current point, matching the stock quadrant panels' 2-point trail
        (no intermediate dots).

    weekly_stats_by_week is {week_num: {player_id: stats}}; this function
    assumes the caller already only included weeks that have real data."""
    weeks = sorted(weekly_stats_by_week.keys())
    team_totals_by_week = {w: team_opportunity_totals(weekly_stats_by_week[w], players_dir) for w in weeks}

    players_out = []
    for pid, name, pos, team in watchlist:
        pts_series, ppt_series, usage_series = [], [], []
        for w in weeks:
            stats = weekly_stats_by_week[w].get(pid, {})
            pts = float(stats.get(scoring_field) or 0.0)
            touches = (stats.get("rush_att") or 0) + (stats.get("rec") or 0)
            opp = (stats.get("rush_att") or 0) + (stats.get("rec_tgt") or 0)
            team_total = team_totals_by_week[w].get(team, 0.0)
            usage = (opp / team_total) if team_total else 0.0
            pts_series.append(round(pts, 1))
            ppt_series.append(round(pts / touches, 2) if touches > 0 else None)
            usage_series.append(round(usage, 4))

        rsi_vals = rsi_series(pts_series)

        frames = []
        for i, w in enumerate(weeks):
            frames.append({
                "week": w, "pts": pts_series[i], "ppt": ppt_series[i], "usage": usage_series[i],
                "rsi": None if rsi_vals[i] is None else round(rsi_vals[i], 1),
            })

        for i in range(len(frames)):
            start = max(0, i - (trail_len - 1))
            if start == i:
                frames[i]["trail_pts"] = []
                frames[i]["trail_ppt"] = []
            else:
                frames[i]["trail_pts"] = [
                    {"v": frames[start]["pts"], "rsi": frames[start]["rsi"]},
                    {"v": frames[i]["pts"], "rsi": frames[i]["rsi"]},
                ]
                if frames[start]["ppt"] is not None and frames[i]["ppt"] is not None:
                    frames[i]["trail_ppt"] = [
                        {"v": frames[start]["ppt"], "rsi": frames[start]["rsi"]},
                        {"v": frames[i]["ppt"], "rsi": frames[i]["rsi"]},
                    ]
                else:
                    frames[i]["trail_ppt"] = []

        players_out.append({"name": name, "pos": pos, "team": team, "frames": frames})
    return players_out


def build_trend_series(watchlist, current_season, scoring_field, seasons_back=DEFAULT_TREND_SEASONS,
                        weeks_per_season=TREND_MAX_WEEKS_PER_SEASON):
    """For each watchlist player, a chronological (oldest -> newest) list of
    {season, week, pts, touches} covering up to the last `seasons_back`
    regular seasons (including the current one). A week is included only if
    the player actually has a stat line that week -- a rookie or a player
    who entered the league partway through this window will simply start
    wherever their real data starts, rather than being padded with zeros
    for seasons before they existed. `touches` (rush attempts + receptions,
    same definition used everywhere else in this script -- see
    build_player_frames()) drives that week's bubble size in
    render_trend_svg(). Reuses get_week_stats()'s cache, so weeks already
    fetched for the quadrant panels' recent window aren't re-fetched here."""
    try:
        season_int = int(current_season)
    except (TypeError, ValueError):
        return {pid: [] for pid, _, _, _ in watchlist}

    seasons = [str(season_int - i) for i in range(seasons_back - 1, -1, -1)]  # oldest -> newest
    series = {pid: [] for pid, _, _, _ in watchlist}
    for season in seasons:
        for week in range(1, weeks_per_season + 1):
            wk = get_week_stats(season, week)
            if not wk:
                continue
            for pid, _name, _pos, _team in watchlist:
                stats = wk.get(pid)
                if stats is None:
                    continue
                pts = stats.get(scoring_field)
                if pts is None:
                    continue
                touches = (stats.get("rush_att") or 0) + (stats.get("rec") or 0)
                series[pid].append({
                    "season": season, "week": week, "pts": round(float(pts), 1), "touches": touches,
                })
    return series


TREND_BUBBLE_MIN_R = 2.2
TREND_BUBBLE_MAX_R = 7.0


def global_touch_bounds(trend_series):
    """(lo, hi) touches across EVERY game, for EVERY player in the report --
    not just one player's own games. This is what "relative to all players
    in the report" means for the trend grid's bubble sizing: the same
    single-touch count should render as the same-size bubble on every
    player's card, so cards are visually comparable to each other, not just
    internally consistent on their own scale. Position filtering only
    changes which cards are visible (see applyPositionFilter() in the
    rendered JS); it never changes this scale, the same way filtering
    doesn't renormalize the quadrant panels' usage-share bubble sizing."""
    vals = [g["touches"] for games in trend_series.values() for g in games]
    if not vals:
        return 0, 1
    lo, hi = min(vals), max(vals)
    if lo == hi:
        hi = lo + 1
    return lo, hi


def render_trend_svg(points, touch_lo, touch_hi, width=300, height=140):
    """Static (no slider) small-multiple line chart of one player's
    chronological points series across however many seasons of real data
    they have. Every week is a bubble, not just a line vertex -- bubble
    radius is that game's touches (rush attempts + receptions), scaled
    against `touch_lo`/`touch_hi` computed ACROSS THE WHOLE REPORT (see
    global_touch_bounds()), so bubble size is comparable player-to-player,
    not just week-to-week within one player's own card. Vertical dashed
    guides mark season boundaries. Returns an empty-state SVG if `points`
    has fewer than 2 entries."""
    margin_l, margin_r, margin_t, margin_b = 8, 8, 10, 18
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    if len(points) < 2:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Not enough data">'
            f'<text x="{width/2}" y="{height/2}" text-anchor="middle" font-size="11" '
            f'fill="#6B7280" font-family="IBM Plex Mono, monospace">no data yet</text></svg>'
        )

    pts_vals = [p["pts"] for p in points]
    lo, hi = min(pts_vals), min(max(pts_vals), 1e9)
    hi = max(hi, lo + 1)
    span = hi - lo
    touch_span = touch_hi - touch_lo or 1

    def x_of(i):
        return margin_l + (i / (len(points) - 1)) * plot_w

    def y_of(v):
        return margin_t + (1 - (v - lo) / span) * plot_h

    def r_of(touches):
        frac = max(0.0, min(1.0, (touches - touch_lo) / touch_span))
        return TREND_BUBBLE_MIN_R + frac * (TREND_BUBBLE_MAX_R - TREND_BUBBLE_MIN_R)

    path_pts = [(x_of(i), y_of(p["pts"])) for i, p in enumerate(points)]
    path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in path_pts)

    parts = [f'<path d="{path}" fill="none" stroke="#4FD8E8" stroke-width="1.2" stroke-opacity="0.55"/>']

    # Season-boundary dashed guides + labels, drawn where consecutive points cross a season.
    last_season = points[0]["season"]
    parts.append(
        f'<text x="{margin_l}" y="{height - 4}" font-size="8.5" fill="#6B7280" '
        f'font-family="IBM Plex Mono, monospace">{html.escape(str(last_season))}</text>'
    )
    for i in range(1, len(points)):
        if points[i]["season"] != last_season:
            gx = (x_of(i - 1) + x_of(i)) / 2
            parts.append(
                f'<line x1="{gx:.1f}" y1="{margin_t}" x2="{gx:.1f}" y2="{height - margin_b}" '
                f'stroke="rgba(107,114,128,0.4)" stroke-width="1" stroke-dasharray="2,3"/>'
            )
            parts.append(
                f'<text x="{gx + 3:.1f}" y="{height - 4}" font-size="8.5" fill="#6B7280" '
                f'font-family="IBM Plex Mono, monospace">{html.escape(str(points[i]["season"]))}</text>'
            )
            last_season = points[i]["season"]

    # Every point is a bubble sized by that game's touches (report-wide scale) -- the
    # last one gets a brighter fill + its point total labeled above it.
    for i, (px, py) in enumerate(path_pts):
        p = points[i]
        r = r_of(p["touches"])
        is_last = (i == len(path_pts) - 1)
        fill = "#4FD8E8" if is_last else "rgba(79,216,232,0.45)"
        stroke = "#4FD8E8"
        parts.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r:.2f}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{"1.4" if is_last else "0.8"}" '
            f'stroke-opacity="{"1" if is_last else "0.6"}">'
            f'<title>{html.escape(str(p["season"]))} wk{p["week"]}: {p["pts"]:.1f}pt, {p["touches"]} touches</title>'
            f'</circle>'
        )

    last_x, last_y = path_pts[-1]
    parts.append(
        f'<text x="{last_x:.1f}" y="{max(10.0, last_y - TREND_BUBBLE_MAX_R - 5):.1f}" text-anchor="end" '
        f'font-size="9.5" fill="#E8E6DE" font-family="IBM Plex Mono, monospace">{points[-1]["pts"]:.1f}</text>'
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Weekly points trend across {len(points)} games, bubble size is that '
        f'game\'s touches">{"".join(parts)}</svg>'
    )


EQUILIBRIUM_CSS = """
  :root{
    --bg:#0B0E14; --panel:#111621; --line:#1E2633;
    --ink:#E8E6DE; --dim:#6B7280; --cyan:#4FD8E8;
    --red:#FF5C5C; --green:#3ECF8E; --neutral:#9CA3AF;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  body{
    background:var(--bg); color:var(--ink);
    font-family:'IBM Plex Mono', monospace;
    padding:26px 20px 60px;
  }
  .eyebrow{ font-size:11px; letter-spacing:.18em; color:var(--cyan); text-transform:uppercase; }
  h1{ font-family:'Space Grotesk', sans-serif; font-size:22px; margin:4px 0 4px; }
  h2{ font-family:'Space Grotesk', sans-serif; font-size:15px; margin:0 0 4px; }
  .sub{ color:var(--dim); font-size:12px; margin-bottom:6px; line-height:1.5; max-width:680px; }
  .sub b{ color:var(--ink); }
  .status{ color:var(--dim); font-size:11px; margin-bottom:18px; }
  .section{ margin-top:34px; }
  .section-head{ margin-bottom:12px; }

  .filter-bar{ display:flex; gap:8px; margin:14px 0 26px; flex-wrap:wrap; }
  .filter-btn{
    background:var(--panel); border:1px solid var(--line); color:var(--dim);
    font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.03em;
    padding:7px 13px; border-radius:6px; cursor:pointer; transition:all .15s ease;
  }
  .filter-btn:hover{ color:var(--ink); border-color:#39424f; }
  .filter-btn.active{ color:#0B0E14; background:var(--cyan); border-color:var(--cyan); font-weight:600; }

  .row{ display:flex; gap:0; flex-wrap:wrap; border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .stage-wrap{ flex:1 1 620px; min-width:0; padding:16px; display:flex; flex-direction:column; gap:10px; background:var(--panel); }
  .stage-wrap svg{ display:block; width:100%; height:auto; }
  .eq-bubble{ transition: cx .35s ease, cy .35s ease, r .35s ease, fill .35s ease, stroke .35s ease; }
  .eq-label, .eq-rsi{ transition: x .35s ease, y .35s ease, fill .35s ease; }
  .eq-trail{ transition: d .35s ease, stroke .35s ease; }

  .hud{ display:flex; justify-content:space-between; padding:0 8px; font-size:11px; color:var(--dim); }
  .side-label{ display:flex; flex-direction:column; gap:2px; }
  .side-label .n{ font-size:22px; font-weight:600; }
  .side-label.left{ color:var(--red); }
  .side-label.right{ text-align:right; color:var(--green); }

  .scrubber{ padding:4px 12px 0; display:flex; flex-direction:column; gap:6px; }
  .scrub-row{ display:flex; align-items:center; gap:12px; }
  input[type=range]{ flex:1; -webkit-appearance:none; height:3px; background:var(--line); border-radius:2px; outline:none; }
  input[type=range]::-webkit-slider-thumb{ -webkit-appearance:none; width:14px; height:14px; border-radius:50%; background:var(--cyan); cursor:pointer; border:2px solid var(--bg); }
  input[type=range]::-moz-range-thumb{ width:14px; height:14px; border-radius:50%; background:var(--cyan); cursor:pointer; border:2px solid var(--bg); }
  .scrubLabel{ font-size:11px; color:var(--cyan); white-space:nowrap; min-width:8ch; text-align:right; }
  .scrubTs{ font-size:10.5px; color:var(--dim); text-align:center; }

  aside{ width:280px; flex-shrink:0; background:#0d1119; border-left:1px solid var(--line); padding:22px 20px; display:flex; flex-direction:column; gap:14px; }
  .legend{ display:flex; flex-direction:column; gap:0; }
  .leg-row{ display:flex; justify-content:space-between; align-items:center; font-size:11.5px; padding:6px 0; border-bottom:1px solid var(--line); }
  .leg-row .name{ display:flex; align-items:center; gap:7px; }
  .leg-row .pos{ color:var(--dim); font-size:9.5px; }
  .dot{ width:8px; height:8px; border-radius:50%; display:inline-block; flex-shrink:0; }
  .leg-row .rsiv{ font-weight:600; }
  .leg-row .meta{ color:var(--dim); font-size:10px; margin-left:6px; }
  .note{ font-size:11px; color:var(--dim); line-height:1.6; padding-top:10px; border-top:1px solid var(--line); }
  .note b{ color:var(--ink); }
  .unavailable{ padding:40px 20px; color:var(--dim); font-size:13px; }

  .trend-grid{ display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:14px; }
  .trend-card{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px 10px; }
  .trend-card-head{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px; }
  .trend-card-head .name{ font-size:12.5px; font-weight:600; color:var(--ink); }
  .trend-card-head .pos{ font-size:10px; color:var(--dim); }
  .trend-card svg{ display:block; width:100%; height:auto; }

  @media (max-width: 760px){ aside{ width:100%; border-left:none; border-top:1px solid var(--line); } }
"""


def render_html(players, trend_series, season, current_week, scoring_label, watchlist_missing, now,
                 trend_seasons_label):
    """Self-contained HTML page with three sections: two scrubbable
    quadrant panels (points scored / points per touch, both x hot-cold
    index) sharing a position filter bar, and a static 4-season trend grid
    -- same visual language/JS mechanics as the moneyflow-update
    Equilibrium quadrant panels (dark theme, 2-point trail, market-size
    bubble sizing), just fed real Sleeper data instead of yfinance OHLC."""
    as_of = now.strftime("%Y-%m-%d %H:%M UTC")

    if not players or not players[0]["frames"]:
        body = f"""
<div class="eyebrow">Fantasy Quadrant</div>
<h1>Hot/Cold Index &times; Weekly Performance</h1>
<div class="unavailable">No weekly stats available yet for {html.escape(str(season))} -- check back once
the season has a few weeks of games played.</div>"""
        return _wrap_html(body)

    num_weeks = len(players[0]["frames"])
    missing_note = ""
    if watchlist_missing:
        missing_note = (
            "<br><br><b>Not found on Sleeper this run:</b> "
            + html.escape(", ".join(watchlist_missing))
            + " -- check spelling in FANTASY_WATCHLIST, or they may not be rostered/active."
        )

    positions_present = sorted(
        {p["pos"] for p in players},
        key=lambda pos: (POSITION_ORDER.index(pos) if pos in POSITION_ORDER else len(POSITION_ORDER), pos),
    )
    filter_buttons = '<button class="filter-btn active" data-pos="ALL">ALL</button>' + "".join(
        f'<button class="filter-btn" data-pos="{html.escape(pos)}">{html.escape(pos)}</button>'
        for pos in positions_present
    )

    data_json = json.dumps({
        p["name"]: {"pos": p["pos"], "team": p["team"], "frames": p["frames"]}
        for p in players
    })

    trend_cards = []
    touch_lo, touch_hi = global_touch_bounds(trend_series if isinstance(trend_series, dict) else {})
    for p in players:
        pts_list = trend_series.get(p.get("_pid"), []) if isinstance(trend_series, dict) else []
        svg = render_trend_svg(pts_list, touch_lo, touch_hi)
        n_games = len(pts_list)
        span_note = f"{n_games} games" if n_games else "no data"
        trend_cards.append(f"""
      <div class="trend-card" data-pos="{html.escape(p['pos'])}">
        <div class="trend-card-head">
          <span class="name">{html.escape(p['name'])}</span>
          <span class="pos">{html.escape(p['pos'])} &middot; {html.escape(p['team'])} &middot; {span_note}</span>
        </div>
        {svg}
      </div>""")

    body = f"""
<div class="eyebrow">Fantasy Quadrant</div>
<h1>Hot/Cold Index &times; Weekly Performance</h1>
<div class="sub">
  <b>Y-axis (both quadrant panels)</b> = a {RSI_PERIOD}-week &quot;hot/cold index&quot; (RSI math run on
  each player's own weekly fantasy points, relative to their own recent baseline, not other players).
  <b>Trail</b> = one line from {RSI_PERIOD - 1} weeks ago straight to this week -- no simulated motion,
  every slider position is a real past week's stat line.
</div>
<div class="status">N = {len(players)} players &middot; {html.escape(str(season))} season, through week
{current_week} &middot; generated {html.escape(as_of)}{missing_note}</div>

<div class="filter-bar" id="posFilter">{filter_buttons}</div>

<div class="section" id="sec-p1">
  <div class="section-head">
    <h2>Hot/Cold Index &times; Points Scored</h2>
    <div class="sub">X-axis = actual points scored that week ({html.escape(scoring_label)}). Bubble size =
    usage share -- opportunities (carries + targets) as a share of the player's own team's total that
    week.</div>
  </div>
  <div class="row">
    <div class="stage-wrap">
      <svg id="svg-p1" viewBox="0 0 900 380" preserveAspectRatio="xMidYMid meet"></svg>
      <div class="hud">
        <div class="side-label left">COLD (INDEX &lt; 50)<div class="n" id="leftCount-p1">0</div></div>
        <div class="side-label right">HOT (INDEX &gt; 50)<div class="n" id="rightCount-p1">0</div></div>
      </div>
      <div class="scrubber">
        <div class="scrub-row">
          <input type="range" id="slider-p1" min="0" max="{num_weeks - 1}" value="{num_weeks - 1}">
          <span class="scrubLabel" id="scrubLabel-p1">Week {num_weeks}</span>
        </div>
        <div class="scrubTs" id="scrubTs-p1">Showing: Week {num_weeks} of {num_weeks}</div>
      </div>
    </div>
    <aside>
      <div class="legend" id="legend-p1"></div>
      <div class="note"><b>Points scored</b> is the volume/production read -- who actually put up the
      most points, regardless of how efficiently.</div>
    </aside>
  </div>
</div>

<div class="section" id="sec-p2">
  <div class="section-head">
    <h2>Hot/Cold Index &times; Points Per Touch</h2>
    <div class="sub">X-axis = points scored PER TOUCH that week (touches = carries + receptions, not
    targets). Same bubble sizing (usage share). This is the efficiency read instead of the volume
    read -- a low-usage player can still show up far right here if they were explosive on their limited
    touches.</div>
  </div>
  <div class="row">
    <div class="stage-wrap">
      <svg id="svg-p2" viewBox="0 0 900 380" preserveAspectRatio="xMidYMid meet"></svg>
      <div class="hud">
        <div class="side-label left">COLD (INDEX &lt; 50)<div class="n" id="leftCount-p2">0</div></div>
        <div class="side-label right">HOT (INDEX &gt; 50)<div class="n" id="rightCount-p2">0</div></div>
      </div>
      <div class="scrubber">
        <div class="scrub-row">
          <input type="range" id="slider-p2" min="0" max="{num_weeks - 1}" value="{num_weeks - 1}">
          <span class="scrubLabel" id="scrubLabel-p2">Week {num_weeks}</span>
        </div>
        <div class="scrubTs" id="scrubTs-p2">Showing: Week {num_weeks} of {num_weeks}</div>
      </div>
    </div>
    <aside>
      <div class="legend" id="legend-p2"></div>
      <div class="note"><b>Points per touch</b> strips out volume -- a bell-cow back getting 20 mediocre
      touches and a change-of-pace back getting 5 explosive ones can land in very different spots here
      even in a week they scored the same total points.</div>
    </aside>
  </div>
</div>

<div class="section" id="sec-trend">
  <div class="section-head">
    <h2>4-Season Points Trend ({html.escape(trend_seasons_label)})</h2>
    <div class="sub">Weekly {html.escape(scoring_label)} points, chronological, across as much of the last
    4 regular seasons as each player actually has -- a rookie or a recent addition just starts wherever
    their real data starts. Each point is a bubble sized by that game's touches (carries + receptions),
    scaled <b>relative to every game, for every player, in this report</b> -- so a given touch count is the
    same size bubble on every player's card, not just consistent within one card. Dashed lines mark season
    boundaries. Static (no slider) -- this is the full span, not a scrub.</div>
  </div>
  <div class="trend-grid" id="trendGrid">{"".join(trend_cards)}</div>
</div>

<script>
const DATA = {data_json};
const players = Object.entries(DATA).map(([name, d]) => ({{ name, pos: d.pos, team: d.team, frames: d.frames }}));
const numWeeks = players.length ? players[0].frames.length : 0;

function colorFor(rsi){{
  if (rsi === null || rsi === undefined) return getCSS('--neutral');
  if (rsi >= 70) return getCSS('--green');
  if (rsi <= 30) return getCSS('--red');
  return getCSS('--neutral');
}}
function getCSS(v){{ return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }}
function safeId(name){{ return name.replace(/[^a-zA-Z0-9]/g, ''); }}

function radiiForWeek(weekIdx){{
  const vals = players.map(p => p.frames[weekIdx].usage);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = hi - lo || 1;
  const out = {{}};
  players.forEach(p => {{
    const u = p.frames[weekIdx].usage;
    out[p.name] = 6 + ((u - lo) / span) * 20;
  }});
  return out;
}}

// One shared quadrant-panel builder, parameterized by which field drives
// the x-axis (valueField: 'pts' or 'ppt') and which trail array to read
// (trailField: 'trail_pts' or 'trail_ppt'). Both panels share the same
// y-axis (rsi) and the same bubble-size input (usage).
function makeQuadrantPanel(scope, valueField, trailField, unitSuffix){{
  const W = 900, H = 380, ML = 50, MR = 20, MT = 26, MB = 34;
  const PW = W - ML - MR, PH = H - MT - MB;

  let vals = [];
  players.forEach(p => p.frames.forEach(f => {{ if (f[valueField] !== null && f[valueField] !== undefined) vals.push(f[valueField]); }}));
  if (!vals.length) vals = [0, 1];
  const rawMin = Math.min(...vals), rawMax = Math.max(...vals);
  const pad = Math.max(0.1, (rawMax - rawMin) * 0.1);
  const X_MIN = Math.max(0, rawMin - pad);
  const X_MAX = rawMax + pad || 1;

  function pxOf(v){{
    v = Math.max(X_MIN, Math.min(X_MAX, v === null || v === undefined ? X_MIN : v));
    return ML + (v - X_MIN) / (X_MAX - X_MIN) * PW;
  }}
  function pyOf(rsi){{
    rsi = Math.max(0, Math.min(100, rsi === null || rsi === undefined ? 50 : rsi));
    return MT + (1 - rsi / 100) * PH;
  }}

  const svg = document.getElementById('svg-' + scope);
  const slider = document.getElementById('slider-' + scope);
  const scrubLabel = document.getElementById('scrubLabel-' + scope);
  const scrubTs = document.getElementById('scrubTs-' + scope);
  const leftCountEl = document.getElementById('leftCount-' + scope);
  const rightCountEl = document.getElementById('rightCount-' + scope);
  const legendEl = document.getElementById('legend-' + scope);
  if (!svg || !slider) return null;

  function initSvg(){{
    const midY = pyOf(50);
    let parts = [
      `<line x1="${{ML}}" y1="${{midY.toFixed(1)}}" x2="${{W-MR}}" y2="${{midY.toFixed(1)}}" stroke="rgba(107,114,128,0.35)" stroke-width="1" stroke-dasharray="3,5"/>`,
      `<text x="${{ML}}" y="${{MT-8}}" font-size="10" fill="#6B7280">INDEX 100 &middot; hot</text>`,
      `<text x="${{ML}}" y="${{H-MB+16}}" font-size="10" fill="#6B7280">INDEX 0 &middot; cold</text>`,
      `<text x="${{W-MR}}" y="${{H-MB+16}}" text-anchor="end" font-size="10" fill="#6B7280">${{X_MAX.toFixed(1)}}${{unitSuffix}} &rarr;</text>`,
      `<text x="${{ML}}" y="${{H-MB+16}}" font-size="10" fill="#6B7280">${{X_MIN.toFixed(1)}}${{unitSuffix}}</text>`,
      `<line x1="${{ML}}" y1="${{H-MB}}" x2="${{W-MR}}" y2="${{H-MB}}" stroke="rgba(30,38,51,1)" stroke-width="1"/>`,
    ];
    players.forEach(p => {{
      const id = safeId(p.name);
      parts.push(`<path id="trail-${{scope}}-${{id}}" data-pos="${{p.pos}}" d="" fill="none" stroke="${{getCSS('--neutral')}}" stroke-width="1.4" stroke-opacity="0.55" stroke-linecap="round" class="eq-trail"/>`);
      parts.push(`<circle id="b-${{scope}}-${{id}}" data-pos="${{p.pos}}" cx="0" cy="0" r="6" fill="${{getCSS('--neutral')}}" fill-opacity="0.30" stroke="${{getCSS('--neutral')}}" stroke-width="1.6" class="eq-bubble"/>`);
      parts.push(`<text id="l-${{scope}}-${{id}}" data-pos="${{p.pos}}" x="0" y="0" text-anchor="middle" font-size="11" font-weight="600" fill="${{getCSS('--neutral')}}" class="eq-label">${{p.name}}</text>`);
      parts.push(`<text id="r-${{scope}}-${{id}}" data-pos="${{p.pos}}" x="0" y="0" text-anchor="middle" font-size="9.5" fill="rgba(232,230,222,0.55)" class="eq-rsi"></text>`);
    }});
    svg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`);
    svg.innerHTML = parts.join('');
  }}

  function renderWeek(weekIdx){{
    const radii = radiiForWeek(weekIdx);
    let left = 0, right = 0;
    const legendRows = [];

    players.forEach(p => {{
      const id = safeId(p.name);
      const f = p.frames[weekIdx];
      const rsi = f.rsi === null || f.rsi === undefined ? 50 : f.rsi;
      const color = colorFor(f.rsi);
      const r = radii[p.name];
      const val = f[valueField];
      const px = pxOf(val), py = pyOf(rsi);

      const bubble = document.getElementById(`b-${{scope}}-${{id}}`);
      const label = document.getElementById(`l-${{scope}}-${{id}}`);
      const rsiText = document.getElementById(`r-${{scope}}-${{id}}`);
      const trail = document.getElementById(`trail-${{scope}}-${{id}}`);

      const hasVal = val !== null && val !== undefined;
      bubble.setAttribute('cx', px); bubble.setAttribute('cy', py); bubble.setAttribute('r', hasVal ? r : 0);
      bubble.setAttribute('fill', color); bubble.setAttribute('stroke', color);
      label.setAttribute('x', px); label.setAttribute('y', py - r - 8); label.setAttribute('fill', color);
      label.style.opacity = hasVal ? '1' : '0';
      rsiText.setAttribute('x', px); rsiText.setAttribute('y', py + r + 14);
      rsiText.textContent = hasVal ? ((f.rsi === null || f.rsi === undefined ? '\\u2014' : Math.round(f.rsi)) + ' \\u00b7 ' + val.toFixed(2) + unitSuffix) : '';

      const tr = f[trailField];
      if (tr && tr.length === 2) {{
        const s = tr[0], c = tr[1];
        const sx = pxOf(s.v), sy = pyOf(s.rsi === null ? 50 : s.rsi);
        const cx = pxOf(c.v), cy = pyOf(c.rsi === null ? 50 : c.rsi);
        trail.setAttribute('d', `M ${{sx.toFixed(1)}},${{sy.toFixed(1)}} L ${{cx.toFixed(1)}},${{cy.toFixed(1)}}`);
      }} else {{
        trail.setAttribute('d', '');
      }}
      trail.setAttribute('stroke', color);

      if (rsi < 49) left++; else if (rsi > 51) right++;
      legendRows.push({{ p, f, color, rsi, val, hasVal }});
    }});

    leftCountEl.textContent = left;
    rightCountEl.textContent = right;
    scrubTs.textContent = `Showing: Week ${{weekIdx+1}} of ${{numWeeks}}`;
    scrubLabel.textContent = `Week ${{weekIdx+1}}`;

    legendRows.sort((a,b) => b.rsi - a.rsi);
    legendEl.innerHTML = legendRows.map(({{p,f,color,rsi,val,hasVal}}) => {{
      const rsiDisp = (f.rsi===null||f.rsi===undefined) ? '\\u2014' : Math.round(f.rsi);
      const valDisp = hasVal ? val.toFixed(2) + unitSuffix : 'n/a';
      return `<div class="leg-row" data-pos="${{p.pos}}">
        <span class="name"><span class="dot" style="background:${{color}}"></span>${{p.name}}<span class="pos">${{p.pos}} &middot; ${{p.team}}</span></span>
        <span><span class="rsiv" style="color:${{color}}">${{rsiDisp}}</span><span class="meta">${{valDisp}} &middot; ${{(f.usage*100).toFixed(0)}}%</span></span>
      </div>`;
    }}).join('');

    applyPositionFilter(currentPosFilter);
  }}

  initSvg();
  slider.addEventListener('input', () => renderWeek(+slider.value));
  renderWeek(numWeeks - 1);
  return {{ renderCurrent: () => renderWeek(+slider.value) }};
}}

let currentPosFilter = 'ALL';
function applyPositionFilter(pos){{
  currentPosFilter = pos;
  document.querySelectorAll('[data-pos]').forEach(el => {{
    const match = (pos === 'ALL' || el.getAttribute('data-pos') === pos);
    el.style.display = match ? '' : 'none';
  }});
  document.querySelectorAll('.filter-btn').forEach(b => {{
    b.classList.toggle('active', b.getAttribute('data-pos') === pos);
  }});
}}

document.getElementById('posFilter').addEventListener('click', (e) => {{
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  applyPositionFilter(btn.getAttribute('data-pos'));
}});

if (numWeeks > 0) {{
  makeQuadrantPanel('p1', 'pts', 'trail_pts', 'pt');
  makeQuadrantPanel('p2', 'ppt', 'trail_ppt', 'pt/tch');
}}
applyPositionFilter('ALL');
</script>
"""
    return _wrap_html(body)


def _wrap_html(body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Fantasy Quadrant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>{EQUILIBRIUM_CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


def main():
    scoring_key = os.environ.get("FANTASY_SCORING", "ppr").strip().lower()
    scoring_field = SCORING_FIELD.get(scoring_key, "pts_ppr")
    scoring_label = {"pts_ppr": "PPR", "pts_half_ppr": "Half-PPR", "pts_std": "Standard"}[scoring_field]

    watchlist_names = WATCHLIST
    if os.environ.get("FANTASY_WATCHLIST"):
        watchlist_names = [n.strip() for n in os.environ["FANTASY_WATCHLIST"].split(",") if n.strip()]

    weeks_history = DEFAULT_WEEKS_HISTORY
    if os.environ.get("FANTASY_WEEKS"):
        try:
            weeks_history = max(2, int(os.environ["FANTASY_WEEKS"]))
        except ValueError:
            pass

    trend_seasons = DEFAULT_TREND_SEASONS
    if os.environ.get("FANTASY_TREND_SEASONS"):
        try:
            trend_seasons = max(1, int(os.environ["FANTASY_TREND_SEASONS"]))
        except ValueError:
            pass

    print("[info] fetching Sleeper state...")
    state = get_state()
    season = state.get("league_season") or state.get("season")
    current_week = state.get("week") or 1
    season_type = state.get("season_type", "regular")
    print(f"[info] season={season} week={current_week} season_type={season_type}")

    print("[info] fetching Sleeper players directory (~5MB, this is the slow step)...")
    players_dir = get_players_directory()
    print(f"[info] {len(players_dir)} players in directory")

    resolved = resolve_watchlist_ids(players_dir, watchlist_names)
    resolved_names = {r[1] for r in resolved}
    missing = [n for n in watchlist_names if n not in resolved_names]

    now = datetime.now(timezone.utc)

    if season_type != "regular" or not current_week or current_week < 1:
        print("[info] not currently in-season -- writing an 'unavailable' placeholder page.")
        with open("index.html", "w") as f:
            f.write(render_html([], {}, season, current_week or 0, scoring_label, missing, now, ""))
        return

    first_week = max(1, current_week - weeks_history + 1)
    weekly_stats_by_week = {}
    for w in range(first_week, current_week + 1):
        print(f"[info] fetching {season} week {w} stats...")
        wk = get_week_stats(season, w)
        if wk:
            weekly_stats_by_week[w] = wk
        else:
            print(f" [warn] no stats returned for week {w} -- skipping from this run's window")

    if not weekly_stats_by_week:
        print("[warn] no weekly stats available at all this run.")

    players = build_player_frames(resolved, weekly_stats_by_week, players_dir, scoring_field)
    # stash player_id on each player dict so render_html can key into trend_series by pid
    for p, (pid, _name, _pos, _team) in zip(players, resolved):
        p["_pid"] = pid

    print(f"[info] fetching {trend_seasons}-season trend history "
          f"({trend_seasons * TREND_MAX_WEEKS_PER_SEASON} week-requests, reuses this run's current-season fetches)...")
    trend_series = build_trend_series(resolved, season, scoring_field, seasons_back=trend_seasons)
    try:
        earliest_season = str(int(season) - trend_seasons + 1)
    except (TypeError, ValueError):
        earliest_season = "?"
    trend_seasons_label = f"{earliest_season}\u2013{season}"

    with open("index.html", "w") as f:
        f.write(render_html(players, trend_series, season, current_week, scoring_label, missing, now,
                             trend_seasons_label))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [f"## Fantasy Quadrant — {season} week {current_week}", ""]
        for p in players:
            last = p["frames"][-1] if p["frames"] else None
            if last:
                rsi_str = "n/a" if last["rsi"] is None else f'{last["rsi"]:.0f}'
                ppt_str = "n/a" if last["ppt"] is None else f'{last["ppt"]:.2f}'
                n_games = len(trend_series.get(p.get("_pid"), []))
                lines.append(f"- **{p['name']}** ({p['pos']}, {p['team']}): {last['pts']}pt "
                              f"({ppt_str} pt/touch), index {rsi_str}, usage {last['usage']*100:.0f}%, "
                              f"{n_games} games in {trend_seasons_label} trend")
        if missing:
            lines.append("")
            lines.append(f"Not found on Sleeper: {', '.join(missing)}")
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")

    print("[info] wrote index.html")


if __name__ == "__main__":
    main()
