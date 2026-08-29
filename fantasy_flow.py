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
     that player (a rookie just gets however many weeks they have). Every
     week is a bubble sized by that game's touches, scaled relative to
     every game for every player in the report -- not per-player.

Panels 1 and 2 share a SEASON SELECTOR (the last 4 regular seasons,
whichever of them have any data) on top of their own week slider within
whichever season is selected -- so this works both mid-season (defaults to
the live season) and off-season (defaults to the most recently completed
one, rather than showing nothing). All three views also share a position
filter (QB/RB/WR/TE/ALL) built from whichever positions are actually
present in the watchlist.

Meant to run on GitHub Actions on a weekly cron (e.g. Tuesday morning, after
Monday Night Football has posted final stats) or any machine with normal
internet access -- NOT inside a locked-down sandbox with a network
allowlist, since it talks to api.sleeper.app.

Writes one file to the repo each run:
    index.html - self-contained visual (dark, scrubbable quadrant scatter
                 x2 with a season selector + static trend grid), so the
                 repo always has an up-to-date snapshot you can open
                 directly or serve via GitHub Pages -- no external tooling
                 required to view it.

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
                                              and cached in-process for the
                                              run -- the quadrant panels'
                                              per-season data and the trend
                                              grid's 4-season lookback both
                                              draw from the same fetches.

Env vars (all optional):
    FANTASY_SCORING   - "ppr" (default), "half_ppr", or "std". Which Sleeper
                         points field to plot.
    FANTASY_WATCHLIST - comma-separated player full names to override the
                         default WATCHLIST below, e.g.
                         "Christian McCaffrey,Justin Jefferson,..."
    FANTASY_TREND_SEASONS - how many seasons back BOTH the quadrant panels'
                         season selector and the trend grid cover, including
                         the current one (default 4).
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
DEFAULT_TREND_SEASONS = 4
MAX_WEEKS_PER_SEASON = 18  # current NFL regular-season length; harmless if a season had 17
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
    """Current NFL season/week per Sleeper. Note this can legitimately point
    at a RECENTLY COMPLETED season during the off-season (Sleeper doesn't
    roll `season` over to the new one until it actually starts) -- main()
    treats that as "default to the most recent season with real data"
    rather than as an error."""
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
    that week yet (a future week, or a week beyond a shorter historical
    season), so callers can treat that the same as "no data" rather than an
    error. Cached in-process per (season, week) since the quadrant panels'
    per-season data and the trend grid's 4-season lookback ask for
    overlapping weeks."""
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


def season_list(anchor_season, seasons_back):
    """[oldest, ..., newest] season strings, `seasons_back` of them, ending
    at `anchor_season` inclusive. Shared by the quadrant panels' per-season
    fetch and the trend grid so both cover exactly the same span."""
    anchor = int(anchor_season)
    return [str(anchor - i) for i in range(seasons_back - 1, -1, -1)]


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
    per-week frames {week, pts, ppt, usage, rsi, trail_pts, trail_ppt} for
    whichever weeks are present in `weekly_stats_by_week` (oldest -> newest
    by week number), where:
      - pts: that week's fantasy points in the configured scoring format
      - ppt: points PER TOUCH that week (touches = rush attempts +
        receptions, NOT targets). None for a week with zero touches.
      - usage: player's opportunities (rush_att + rec_tgt) as a share of
        their team's total that week -- 0..1, the bubble-size input for
        BOTH quadrant panels
      - rsi: RSI(RSI_PERIOD) run on this player's own points series so far
        (within this call's week window -- RSI resets per season, since a
        hot/cold streak shouldn't carry across an off-season)
      - trail_pts / trail_ppt: [] / [start, current] -- start is
        `trail_len - 1` weeks back (or the earliest available within this
        window), so each panel can draw one straight line from there
        directly to the current point.

    A player with NO stat line in ANY week of `weekly_stats_by_week` (never
    appears in any week's dict -- not "appeared and scored 0") is omitted
    entirely from the returned list, rather than plotted as a flatlined-at-
    zero bubble -- that's the "doesn't exist yet in this season" case (a
    rookie's earlier seasons, a recent addition's earlier weeks)."""
    weeks = sorted(weekly_stats_by_week.keys())
    team_totals_by_week = {w: team_opportunity_totals(weekly_stats_by_week[w], players_dir) for w in weeks}

    players_out = []
    for pid, name, pos, team in watchlist:
        if not any(pid in weekly_stats_by_week[w] for w in weeks):
            continue

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


def build_frames_by_season(watchlist, seasons, players_dir, scoring_field, live_season=None, live_week=None,
                            max_weeks=MAX_WEEKS_PER_SEASON):
    """{season: [player_frame_dicts...]} for each season in `seasons` that
    has any real data -- a season with nothing (further back than Sleeper
    has stats for, or a not-yet-started season) is simply omitted from the
    returned dict rather than included empty, so the front end's season
    selector only ever offers seasons that actually have something to show.

    For `live_season` (only meaningful when the caller knows that season is
    genuinely in progress), only weeks 1..`live_week` are requested since
    later weeks don't exist yet; every other season requests the full
    1..max_weeks range (get_week_stats() gracefully returns {} for any week
    that doesn't exist, so this is harmless for a 17-week season)."""
    out = {}
    for season in seasons:
        week_cap = live_week if (live_season is not None and season == str(live_season) and live_week) else max_weeks
        weekly_stats_by_week = {}
        for w in range(1, week_cap + 1):
            wk = get_week_stats(season, w)
            if wk:
                weekly_stats_by_week[w] = wk
        if not weekly_stats_by_week:
            continue
        frames = build_player_frames(watchlist, weekly_stats_by_week, players_dir, scoring_field)
        if frames:
            out[season] = frames
    return out


def build_trend_series(watchlist, seasons, scoring_field):
    """For each watchlist player, a chronological (oldest -> newest) list of
    {season, week, pts, touches, ppt} across every season in `seasons` that
    has real data for them. A week is included only if the player actually
    has a stat line that week -- a rookie or a player who entered the
    league partway through this window will simply start wherever their
    real data starts, rather than being padded with zeros for seasons
    before they existed. `touches` is rush attempts + receptions; `ppt` is
    points PER TOUCH that week (None when touches is 0) -- `ppt` drives
    that week's bubble size in render_trend_svg(). Reuses
    get_week_stats()'s cache, so weeks already fetched for
    build_frames_by_season() aren't re-fetched."""
    series = {pid: [] for pid, _, _, _ in watchlist}
    for season in seasons:
        for week in range(1, MAX_WEEKS_PER_SEASON + 1):
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
                pts = float(pts)
                touches = (stats.get("rush_att") or 0) + (stats.get("rec") or 0)
                ppt = round(pts / touches, 2) if touches > 0 else None
                series[pid].append({
                    "season": season, "week": week, "pts": round(pts, 1), "touches": touches, "ppt": ppt,
                })
    return series


TREND_BUBBLE_MIN_R = 2.2
TREND_BUBBLE_MAX_R = 7.0


def global_ppt_bounds(trend_series):
    """(lo, hi) points-per-touch across EVERY game, for EVERY player in the
    report -- not just one player's own games (games with undefined ppt,
    i.e. 0 touches, are excluded from the bound calculation but still get
    rendered at TREND_BUBBLE_MIN_R). This is what "relative to all players
    in the report" means for the trend grid's bubble sizing: the same
    points-per-touch value should render as the same-size bubble on every
    player's card, so cards are visually comparable to each other, not just
    internally consistent on their own scale. Position/season filtering
    only changes which cards/points are visible; it never changes this
    scale."""
    vals = [g["ppt"] for games in trend_series.values() for g in games if g["ppt"] is not None]
    if not vals:
        return 0, 1
    lo, hi = min(vals), max(vals)
    if lo == hi:
        hi = lo + 1
    return lo, hi


def render_trend_svg(points, ppt_lo, ppt_hi, card_id, width=300, height=140):
    """Static-layout (positions never move) small-multiple line chart of one
    player's chronological points series across however many seasons of
    real data they have. Every week is a bubble, not just a line vertex --
    bubble radius is that game's POINTS PER PLAY (touches), scaled against
    `ppt_lo`/`ppt_hi` computed ACROSS THE WHOLE REPORT (see
    global_ppt_bounds()), so bubble size is comparable player-to-player
    (a 2.5 pt/touch week looks the same size on every card), not just
    week-to-week within one player's own card. A game with 0 touches has no
    defined points-per-play and renders at the minimum radius.

    Points are split into one <g data-season="YYYY"> group per season
    (chronological run, since the input is already sorted oldest -> newest)
    so the season checkboxes rendered alongside the trend grid can hide/show
    an entire season's segment across every card at once via a plain CSS
    attribute-selector toggle -- x/y positions never move when a season is
    hidden, so nothing needs to be recomputed client-side, just display:none
    on the matching group. Vertical dashed guides still mark season
    boundaries regardless of which seasons are checked.

    Every bubble also carries data-season/data-week/data-pts/data-ppt/
    data-touches and an onclick calling the page's showTrendStats(this, ...)
    JS function, so clicking any point -- from any season, any week --
    shows that game's exact numbers in a small readout under the card
    (`card_id` names that readout element). Returns an empty-state SVG if
    `points` has fewer than 2 entries."""
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
    ppt_span = ppt_hi - ppt_lo or 1

    def x_of(i):
        return margin_l + (i / (len(points) - 1)) * plot_w

    def y_of(v):
        return margin_t + (1 - (v - lo) / span) * plot_h

    def r_of(ppt):
        if ppt is None:
            return TREND_BUBBLE_MIN_R
        frac = max(0.0, min(1.0, (ppt - ppt_lo) / ppt_span))
        return TREND_BUBBLE_MIN_R + frac * (TREND_BUBBLE_MAX_R - TREND_BUBBLE_MIN_R)

    path_pts = [(x_of(i), y_of(p["pts"])) for i, p in enumerate(points)]

    parts = []

    # Season boundary dashed guides + labels -- drawn once, independent of
    # the per-season <g> grouping below (these stay visible regardless of
    # which season checkboxes are ticked, so you can still see where a
    # hidden season's gap sits).
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

    # Group points into contiguous per-season runs (safe since `points` is
    # already chronological, so each season's games are one contiguous
    # block) -- each run gets its own <path> (so a hidden season's line
    # doesn't leave a stray connector) and its own <g data-season="...">
    # wrapper for the checkbox toggle.
    i = 0
    n = len(points)
    while i < n:
        season = points[i]["season"]
        j = i
        while j < n and points[j]["season"] == season:
            j += 1
        run_path_pts = path_pts[i:j]
        run_path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in run_path_pts)
        group_parts = [f'<path d="{run_path}" fill="none" stroke="#4FD8E8" stroke-width="1.2" stroke-opacity="0.55"/>']
        for k in range(i, j):
            px, py = path_pts[k]
            p = points[k]
            r = r_of(p["ppt"])
            is_last = (k == n - 1)
            fill = "#4FD8E8" if is_last else "rgba(79,216,232,0.45)"
            ppt_str = "n/a" if p["ppt"] is None else f'{p["ppt"]:.2f}'
            group_parts.append(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r:.2f}" fill="{fill}" '
                f'stroke="#4FD8E8" stroke-width="{"1.4" if is_last else "0.8"}" '
                f'stroke-opacity="{"1" if is_last else "0.6"}" '
                f'data-season="{html.escape(str(p["season"]))}" data-week="{p["week"]}" '
                f'data-pts="{p["pts"]}" data-ppt="{ppt_str}" data-touches="{p["touches"]}" '
                f'onclick="showTrendStats(this, \'{html.escape(card_id)}\')" style="cursor:pointer">'
                f'<title>{html.escape(str(p["season"]))} wk{p["week"]}: {p["pts"]:.1f}pt, '
                f'{ppt_str} pt/play ({p["touches"]} touches)</title>'
                f'</circle>'
            )
        parts.append(f'<g data-season="{html.escape(str(season))}">{"".join(group_parts)}</g>')
        i = j

    last_x, last_y = path_pts[-1]
    parts.append(
        f'<text x="{last_x:.1f}" y="{max(10.0, last_y - TREND_BUBBLE_MAX_R - 5):.1f}" text-anchor="end" '
        f'font-size="9.5" fill="#E8E6DE" font-family="IBM Plex Mono, monospace">{points[-1]["pts"]:.1f}</text>'
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Weekly points trend across {len(points)} games, bubble size is that '
        f'game\'s points per play. Click any point for exact stats.">{"".join(parts)}</svg>'
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

  .filter-bar{ display:flex; gap:8px; margin:14px 0 6px; flex-wrap:wrap; align-items:center; }
  .filter-bar-label{ font-size:10px; color:var(--dim); letter-spacing:.08em; text-transform:uppercase; margin-right:2px; }
  .filter-btn{
    background:var(--panel); border:1px solid var(--line); color:var(--dim);
    font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.03em;
    padding:7px 13px; border-radius:6px; cursor:pointer; transition:all .15s ease;
  }
  .filter-btn:hover{ color:var(--ink); border-color:#39424f; }
  .filter-btn.active{ color:#0B0E14; background:var(--cyan); border-color:var(--cyan); font-weight:600; }
  .filter-btn.season-btn.active{ background:var(--green); border-color:var(--green); }
  .checkbox-btn{
    display:inline-flex; align-items:center; gap:6px; background:var(--panel); border:1px solid var(--line);
    color:var(--dim); font-family:'IBM Plex Mono', monospace; font-size:11px; padding:6px 12px;
    border-radius:6px; cursor:pointer; user-select:none;
  }
  .checkbox-btn input{ accent-color:var(--cyan); cursor:pointer; }
  .trend-readout{
    font-size:10.5px; color:var(--dim); border-top:1px solid var(--line); margin-top:8px; padding-top:8px;
  }
  .trend-readout.has-data{ color:var(--ink); }

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
  .empty-season{ padding:40px 20px; color:var(--dim); font-size:12.5px; text-align:center; }

  .trend-grid{ display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:14px; }
  .trend-card{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px 10px; }
  .trend-card-head{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px; }
  .trend-card-head .name{ font-size:12.5px; font-weight:600; color:var(--ink); }
  .trend-card-head .pos{ font-size:10px; color:var(--dim); }
  .trend-card svg{ display:block; width:100%; height:auto; }

  @media (max-width: 760px){ aside{ width:100%; border-left:none; border-top:1px solid var(--line); } }
"""


def render_html(frames_by_season, trend_series, watchlist_meta, default_season, season, current_week,
                 season_type, scoring_label, watchlist_missing, now, trend_seasons_label, trend_seasons):
    """Self-contained HTML page: two scrubbable quadrant panels (points
    scored / points per touch, both x hot-cold index) that share a SEASON
    SELECTOR (any season in `frames_by_season` that actually has data) and
    a position filter bar, plus a static 4-season trend grid -- same visual
    language/JS mechanics as the moneyflow-update Equilibrium quadrant
    panels (dark theme, 2-point trail, market-size bubble sizing), fed real
    Sleeper data instead of yfinance OHLC."""
    as_of = now.strftime("%Y-%m-%d %H:%M UTC")
    available_seasons = sorted(frames_by_season.keys())  # oldest -> newest

    if not available_seasons and not any(trend_series.values()):
        body = f"""
<div class="eyebrow">Fantasy Quadrant</div>
<h1>Hot/Cold Index &times; Weekly Performance</h1>
<div class="unavailable">No weekly stats available yet from Sleeper for any of the last few seasons --
check back once games have been played, or verify FANTASY_WATCHLIST names match Sleeper's player
directory.</div>"""
        return _wrap_html(body)

    missing_note = ""
    if watchlist_missing:
        missing_note = (
            "<br><br><b>Not found on Sleeper this run:</b> "
            + html.escape(", ".join(watchlist_missing))
            + " -- check spelling in FANTASY_WATCHLIST, or they may not be rostered/active."
        )

    all_positions = sorted(
        {p["pos"] for p in watchlist_meta},
        key=lambda pos: (POSITION_ORDER.index(pos) if pos in POSITION_ORDER else len(POSITION_ORDER), pos),
    )
    filter_buttons = '<button class="filter-btn active" data-pos="ALL">ALL</button>' + "".join(
        f'<button class="filter-btn" data-pos="{html.escape(pos)}">{html.escape(pos)}</button>'
        for pos in all_positions
    )
    season_buttons = "".join(
        f'<button class="filter-btn season-btn{" active" if s == default_season else ""}" '
        f'data-season="{html.escape(s)}">{html.escape(s)}</button>'
        for s in available_seasons
    )
    trend_season_checkboxes = "".join(
        f'<label class="checkbox-btn"><input type="checkbox" class="trend-season-cb" value="{html.escape(s)}" checked> {html.escape(s)}</label>'
        for s in trend_seasons
    )

    season_data_json = json.dumps({
        s: {p["name"]: {"pos": p["pos"], "team": p["team"], "frames": p["frames"]} for p in plist}
        for s, plist in frames_by_season.items()
    })

    trend_cards = []
    ppt_lo, ppt_hi = global_ppt_bounds(trend_series)
    for meta in watchlist_meta:
        pts_list = trend_series.get(meta["pid"], [])
        card_id = f"trend-{meta['pid']}"
        svg = render_trend_svg(pts_list, ppt_lo, ppt_hi, card_id)
        n_games = len(pts_list)
        span_note = f"{n_games} games" if n_games else "no data"
        trend_cards.append(f"""
      <div class="trend-card" data-pos="{html.escape(meta['pos'])}">
        <div class="trend-card-head">
          <span class="name">{html.escape(meta['name'])}</span>
          <span class="pos">{html.escape(meta['pos'])} &middot; {html.escape(meta['team'])} &middot; {span_note}</span>
        </div>
        {svg}
        <div class="trend-readout" id="{html.escape(card_id)}">Click a point to see that game's exact stats</div>
      </div>""")

    live_note = ""
    if season_type == "regular" and str(season) in frames_by_season:
        live_note = f" &middot; live season {html.escape(str(season))}, through week {current_week}"
    else:
        live_note = f" &middot; off-season -- defaulting to the most recent completed season ({html.escape(default_season or '?')})"

    body = f"""
<div class="eyebrow">Fantasy Quadrant</div>
<h1>Hot/Cold Index &times; Weekly Performance</h1>
<div class="sub">
  <b>Y-axis (both quadrant panels)</b> = a {RSI_PERIOD}-week &quot;hot/cold index&quot; (RSI math run on
  each player's own weekly fantasy points within the selected season, relative to their own recent
  baseline, not other players). <b>Trail</b> = one line from {RSI_PERIOD - 1} weeks ago straight to this
  week -- no simulated motion, every slider position is a real past week's stat line.
</div>
<div class="status">N = {len(watchlist_meta)} players &middot; generated {html.escape(as_of)}{live_note}{missing_note}</div>

<div class="filter-bar" id="posFilter"><span class="filter-bar-label">Position</span>{filter_buttons}</div>
<div class="filter-bar" id="seasonFilter"><span class="filter-bar-label">Season</span>{season_buttons}</div>

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
          <input type="range" id="slider-p1" min="0" max="0" value="0">
          <span class="scrubLabel" id="scrubLabel-p1">Week 1</span>
        </div>
        <div class="scrubTs" id="scrubTs-p1">Showing: Week 1</div>
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
    read.</div>
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
          <input type="range" id="slider-p2" min="0" max="0" value="0">
          <span class="scrubLabel" id="scrubLabel-p2">Week 1</span>
        </div>
        <div class="scrubTs" id="scrubTs-p2">Showing: Week 1</div>
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
    their real data starts. Each point is a bubble sized by that game's <b>points per play</b> (touches),
    scaled <b>relative to every game, for every player, in this report</b>. <b>Click any point</b> to see
    that exact game's stats below its card. Dashed lines always mark season boundaries, regardless of
    which seasons are checked below. Always covers the same 4-season span, independent of the season
    selected in the panels above.</div>
  </div>
  <div class="filter-bar" id="trendSeasonToggle"><span class="filter-bar-label">Show seasons</span>{trend_season_checkboxes}</div>
  <div class="trend-grid" id="trendGrid">{"".join(trend_cards)}</div>
</div>

<script>
const SEASON_DATA = {season_data_json};
const AVAILABLE_SEASONS = {json.dumps(available_seasons)};
let currentSeason = {json.dumps(default_season)};

function colorFor(rsi){{
  if (rsi === null || rsi === undefined) return getCSS('--neutral');
  if (rsi >= 70) return getCSS('--green');
  if (rsi <= 30) return getCSS('--red');
  return getCSS('--neutral');
}}
function getCSS(v){{ return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }}
function safeId(name){{ return name.replace(/[^a-zA-Z0-9]/g, ''); }}
function playersForSeason(season){{
  const d = SEASON_DATA[season] || {{}};
  return Object.entries(d).map(([name, v]) => ({{ name, pos: v.pos, team: v.team, frames: v.frames }}));
}}

function radiiForWeek(players, weekIdx){{
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

// One shared quadrant-panel controller, parameterized by which field
// drives the x-axis (valueField: 'pts' or 'ppt') and which trail array to
// read (trailField: 'trail_pts' or 'trail_ppt'). Both panels share the
// same y-axis (rsi), the same bubble-size input (usage), and are rebuilt
// from scratch (loadSeason) whenever the season selector changes, since a
// different season can have a different set of players/weeks entirely.
function makeQuadrantController(scope, valueField, trailField, unitSuffix){{
  const W = 900, H = 380, ML = 50, MR = 20, MT = 26, MB = 34;
  const PW = W - ML - MR, PH = H - MT - MB;
  const svg = document.getElementById('svg-' + scope);
  const slider = document.getElementById('slider-' + scope);
  const scrubLabel = document.getElementById('scrubLabel-' + scope);
  const scrubTs = document.getElementById('scrubTs-' + scope);
  const leftCountEl = document.getElementById('leftCount-' + scope);
  const rightCountEl = document.getElementById('rightCount-' + scope);
  const legendEl = document.getElementById('legend-' + scope);

  let players = [];
  let X_MIN = 0, X_MAX = 1, numWeeks = 0;

  function pxOf(v){{
    v = Math.max(X_MIN, Math.min(X_MAX, v === null || v === undefined ? X_MIN : v));
    return ML + (v - X_MIN) / (X_MAX - X_MIN) * PW;
  }}
  function pyOf(rsi){{
    rsi = Math.max(0, Math.min(100, rsi === null || rsi === undefined ? 50 : rsi));
    return MT + (1 - rsi / 100) * PH;
  }}

  function initSvg(){{
    if (!players.length) {{
      svg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`);
      svg.innerHTML = `<text x="${{W/2}}" y="${{H/2}}" text-anchor="middle" font-size="12" fill="#6B7280" font-family="IBM Plex Mono, monospace">No data for this season</text>`;
      return;
    }}
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
    if (!players.length) {{
      leftCountEl.textContent = '0'; rightCountEl.textContent = '0';
      scrubTs.textContent = 'Showing: n/a'; scrubLabel.textContent = 'n/a';
      legendEl.innerHTML = '';
      return;
    }}
    const radii = radiiForWeek(players, weekIdx);
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
      if (!bubble) return;

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
    scrubTs.textContent = `Showing: Week ${{weekIdx+1}} of ${{numWeeks}} (${{currentSeason}})`;
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

  function loadSeason(season){{
    players = playersForSeason(season);
    let vals = [];
    players.forEach(p => p.frames.forEach(f => {{ if (f[valueField] !== null && f[valueField] !== undefined) vals.push(f[valueField]); }}));
    if (!vals.length) vals = [0, 1];
    const rawMin = Math.min(...vals), rawMax = Math.max(...vals);
    const pad = Math.max(0.1, (rawMax - rawMin) * 0.1);
    X_MIN = Math.max(0, rawMin - pad);
    X_MAX = (rawMax + pad) || 1;
    numWeeks = players.length ? players[0].frames.length : 0;

    slider.max = Math.max(0, numWeeks - 1);
    slider.value = Math.max(0, numWeeks - 1);
    initSvg();
    renderWeek(Math.max(0, numWeeks - 1));
  }}

  slider.addEventListener('input', () => renderWeek(+slider.value));
  return {{ loadSeason }};
}}

let currentPosFilter = 'ALL';
function applyPositionFilter(pos){{
  currentPosFilter = pos;
  document.querySelectorAll('[data-pos]').forEach(el => {{
    const match = (pos === 'ALL' || el.getAttribute('data-pos') === pos);
    el.style.display = match ? '' : 'none';
  }});
  document.querySelectorAll('#posFilter .filter-btn').forEach(b => {{
    b.classList.toggle('active', b.getAttribute('data-pos') === pos);
  }});
}}

const panel1 = makeQuadrantController('p1', 'pts', 'trail_pts', 'pt');
const panel2 = makeQuadrantController('p2', 'ppt', 'trail_ppt', 'pt/tch');

function applySeason(season){{
  currentSeason = season;
  panel1.loadSeason(season);
  panel2.loadSeason(season);
  document.querySelectorAll('#seasonFilter .season-btn').forEach(b => {{
    b.classList.toggle('active', b.getAttribute('data-season') === season);
  }});
}}

document.getElementById('posFilter').addEventListener('click', (e) => {{
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  applyPositionFilter(btn.getAttribute('data-pos'));
}});
document.getElementById('seasonFilter').addEventListener('click', (e) => {{
  const btn = e.target.closest('.season-btn');
  if (!btn) return;
  applySeason(btn.getAttribute('data-season'));
}});

if (AVAILABLE_SEASONS.length) {{
  applySeason(currentSeason);
}}
applyPositionFilter('ALL');

// -- 4-season trend grid: season checkboxes + click-to-inspect --
// Toggling a season checkbox hides/shows every <g data-season="YYYY">
// group across every trend card at once (positions never move -- this is
// a pure visibility toggle, same principle as the position filter).
function applyTrendSeasonToggle(){{
  const checked = new Set(
    Array.from(document.querySelectorAll('.trend-season-cb:checked')).map(cb => cb.value)
  );
  document.querySelectorAll('#trendGrid g[data-season]').forEach(g => {{
    g.style.display = checked.has(g.getAttribute('data-season')) ? '' : 'none';
  }});
}}
document.querySelectorAll('.trend-season-cb').forEach(cb => {{
  cb.addEventListener('change', applyTrendSeasonToggle);
}});
applyTrendSeasonToggle();

// Clicking any bubble on the trend grid writes that exact game's stats
// into its card's readout line -- the data attributes are set server-side
// per point (see render_trend_svg()), so this is just a read + format, no
// recomputation.
function showTrendStats(circle, cardId){{
  const season = circle.getAttribute('data-season');
  const week = circle.getAttribute('data-week');
  const pts = circle.getAttribute('data-pts');
  const ppt = circle.getAttribute('data-ppt');
  const touches = circle.getAttribute('data-touches');
  const el = document.getElementById(cardId);
  if (!el) return;
  el.innerHTML = `<b>${{season}} wk${{week}}:</b> ${{pts}}pt &middot; ${{ppt}} pt/play &middot; ${{touches}} touches`;
  el.classList.add('has-data');
}}
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

    trend_seasons = DEFAULT_TREND_SEASONS
    if os.environ.get("FANTASY_TREND_SEASONS"):
        try:
            trend_seasons = max(1, int(os.environ["FANTASY_TREND_SEASONS"]))
        except ValueError:
            pass

    print("[info] fetching Sleeper state...")
    state = get_state()
    season = state.get("league_season") or state.get("season")
    current_week = state.get("week") or 0
    season_type = state.get("season_type", "regular")
    print(f"[info] season={season} week={current_week} season_type={season_type}")

    print("[info] fetching Sleeper players directory (~5MB, this is the slow step)...")
    players_dir = get_players_directory()
    print(f"[info] {len(players_dir)} players in directory")

    resolved = resolve_watchlist_ids(players_dir, watchlist_names)
    resolved_names = {r[1] for r in resolved}
    missing = [n for n in watchlist_names if n not in resolved_names]
    watchlist_meta = [{"pid": pid, "name": name, "pos": pos, "team": team} for pid, name, pos, team in resolved]

    now = datetime.now(timezone.utc)

    if not season:
        print("[warn] Sleeper state returned no season -- writing an 'unavailable' placeholder page.")
        with open("index.html", "w") as f:
            f.write(render_html({}, {pid: [] for pid, _, _, _ in resolved}, watchlist_meta, None,
                                 season, current_week, season_type, scoring_label, missing, now, "", []))
        return

    seasons = season_list(season, trend_seasons)
    is_live = (season_type == "regular" and current_week and current_week >= 1)

    print(f"[info] fetching quadrant-panel data for seasons {seasons} "
          f"({'live -- capping current season at week ' + str(current_week) if is_live else 'all off-season, full 1..' + str(MAX_WEEKS_PER_SEASON) + ' range'})...")
    frames_by_season = build_frames_by_season(
        resolved, seasons, players_dir, scoring_field,
        live_season=season if is_live else None, live_week=current_week if is_live else None,
    )

    default_season = None
    if is_live and str(season) in frames_by_season:
        default_season = str(season)
    elif frames_by_season:
        default_season = sorted(frames_by_season.keys())[-1]  # most recent season with data
    print(f"[info] seasons with data: {sorted(frames_by_season.keys())} -- default={default_season}")

    print(f"[info] fetching {trend_seasons}-season trend history "
          f"(reuses this run's per-season fetches where the windows overlap)...")
    trend_series = build_trend_series(resolved, seasons, scoring_field)
    trend_seasons_label = f"{seasons[0]}\u2013{seasons[-1]}" if seasons else "?"

    with open("index.html", "w") as f:
        f.write(render_html(frames_by_season, trend_series, watchlist_meta, default_season, season,
                             current_week, season_type, scoring_label, missing, now, trend_seasons_label,
                             seasons))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [f"## Fantasy Quadrant — default season {default_season or 'n/a'}", ""]
        current_frames = {p["name"]: p for p in frames_by_season.get(default_season, [])} if default_season else {}
        for meta in watchlist_meta:
            p = current_frames.get(meta["name"])
            n_games = len(trend_series.get(meta["pid"], []))
            if p and p["frames"]:
                last = p["frames"][-1]
                rsi_str = "n/a" if last["rsi"] is None else f'{last["rsi"]:.0f}'
                ppt_str = "n/a" if last["ppt"] is None else f'{last["ppt"]:.2f}'
                lines.append(f"- **{meta['name']}** ({meta['pos']}, {meta['team']}): {last['pts']}pt "
                              f"({ppt_str} pt/touch), index {rsi_str}, usage {last['usage']*100:.0f}%, "
                              f"{n_games} games in {trend_seasons_label} trend")
            else:
                lines.append(f"- **{meta['name']}** ({meta['pos']}, {meta['team']}): no data in default "
                              f"season, {n_games} games in {trend_seasons_label} trend")
        if missing:
            lines.append("")
            lines.append(f"Not found on Sleeper: {', '.join(missing)}")
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")

    print("[info] wrote index.html")


if __name__ == "__main__":
    main()
