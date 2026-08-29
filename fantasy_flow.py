#!/usr/bin/env python3
"""
Fantasy Quadrant: RSI-style "hot/cold index" plotted against real weekly
performance, with a short trail behind each player showing their last few
weeks' trajectory -- same visual mechanic as moneyflow-update's Equilibrium
quadrant panels, ported from tickers/price-closes to fantasy players/weekly
points.

PLAYER SELECTION (this is the part that changed)
------------------------------------------------
The report is no longer a hand-maintained list of 8 names. Each run:

  1. Ranks EVERY player by the fantasy points they scored in THEIR OWN
     MOST RECENT GAME -- for most players that's the latest completed week,
     but a player who was on bye, injured or inactive is ranked on the last
     week they actually played rather than being dropped from the report.
     Filtered to QB/RB/WR/TE.
  2. Takes the top FANTASY_POOL_SIZE (default 300) of them as the
     candidate pool -- that pool is what gets fetched, framed and rendered.
  3. The page's ALL view shows only the top FANTASY_TOP_N (default 40) of
     that pool, so the quadrant scatter stays readable. Selecting a
     POSITION shows EVERY player at that position in the full 300-player
     pool, not just the ones that happened to crack the overall top 40.

Ranking on each player's last game rather than a season total makes this a
"who just did something" report: the pool turns over week to week, which is
the point of a weekly cron. A player's last game is looked for within the
ranking season only, newest week first -- someone who hasn't played at all
this season genuinely has nothing to rank and is left out. How stale each
player's last game is shows on the page (the trend card says "last: wk N"
whenever it isn't the current week) so a week-2 number sitting next to a
week-9 one is never silently compared. FANTASY_RANK_BY=season switches to
season-to-date totals if you'd rather the pool be stable.

"Most recent completed week" is the newest week that actually has a full
slate of stat lines (at least MIN_WEEK_PLAYERS players). A run that lands
mid-slate -- Thursday night, or Sunday afternoon -- steps back to the last
finished week rather than ranking everyone against the handful of players
whose game has kicked off. Setting FANTASY_WATCHLIST bypasses ranking
entirely and uses exactly the names given, as before.

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
one, rather than showing nothing). All three views also share the position
filter described above, built from whichever positions are actually
present in the pool.

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
                                              run -- the ranking pass, the
                                              quadrant panels' per-season
                                              data and the trend grid's
                                              4-season lookback all draw
                                              from the same fetches.

Env vars (all optional):

    FANTASY_SCORING   - "ppr" (default), "half_ppr", or "std". Which Sleeper
                         points field to plot.
    FANTASY_POOL_SIZE - how many top-ranked players make the candidate pool
                         (default 300). This is what the position filter
                         exposes.
    FANTASY_TOP_N     - how many of the pool the ALL view plots
                         (default 40). Position views ignore this.
    FANTASY_RANK_BY   - "last_game" (default) ranks the pool by each
                         player's own most recent game; "season" ranks by
                         season-to-date totals for a pool that barely moves
                         week to week.
    FANTASY_WATCHLIST - comma-separated player full names. If set, ranking
                         is skipped entirely and exactly these players are
                         used, e.g. "Christian McCaffrey,Justin Jefferson".
    FANTASY_POSITIONS - comma-separated positions eligible for the pool
                         (default "QB,RB,WR,TE").
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

RSI_PERIOD = 4  # shorter window than the stock version's 14 -- a season is only ~17 weeks
TRAIL_LEN = 3   # trail runs from (TRAIL_LEN - 1) weeks back to the current week
DEFAULT_TREND_SEASONS = 4
DEFAULT_POOL_SIZE = 300
DEFAULT_TOP_N = 40
MAX_WEEKS_PER_SEASON = 18  # current NFL regular-season length; harmless if a season had 17
MIN_WEEK_PLAYERS = 150     # below this, a week is mid-slate (or garbage) -- step back a week to rank
LABEL_LIMIT = 14           # at most this many name labels drawn per quadrant frame
DEFAULT_RANK_BY = "last_game"  # each player's own latest game; "season" = season-to-date totals
REQUEST_TIMEOUT = 20

SCORING_FIELD = {
    "ppr": "pts_ppr",
    "half_ppr": "pts_half_ppr",
    "std": "pts_std",
}

POSITION_ORDER = ["QB", "RB", "WR", "TE"]  # display order for the filter bar; anything else is appended after


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-quadrant/2.0"})
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
    error. Cached in-process per (season, week) since the ranking pass, the
    quadrant panels' per-season data and the trend grid's 4-season lookback
    all ask for overlapping weeks."""
    key = (str(season), int(week))
    if key in _WEEK_STATS_CACHE:
        return _WEEK_STATS_CACHE[key]
    try:
        data = fetch_json(f"{SLEEPER_BASE}/stats/nfl/regular/{season}/{week}") or {}
    except Exception as e:
        print(f"  [warn] stats fetch failed for {season} week {week}: {e}", file=sys.stderr)
        data = {}
    _WEEK_STATS_CACHE[key] = data
    return data


def season_list(anchor_season, seasons_back):
    """[oldest, ..., newest] season strings, `seasons_back` of them, ending
    at `anchor_season` inclusive. Shared by the quadrant panels' per-season
    fetch and the trend grid so both cover exactly the same span."""
    anchor = int(anchor_season)
    return [str(anchor - i) for i in range(seasons_back - 1, -1, -1)]


def load_season_weeks(season, week_cap=MAX_WEEKS_PER_SEASON):
    """{week: stats_dict} for every week of `season` that has real data.
    Weeks that don't exist yet come back empty from get_week_stats() and are
    simply omitted. Everything is cached, so calling this repeatedly for the
    same season across the ranking pass / panel build / trend build costs
    exactly one set of fetches."""
    out = {}
    for w in range(1, week_cap + 1):
        wk = get_week_stats(season, w)
        if wk:
            out[w] = wk
    return out


# ---------------------------------------------------------------------------
# Player selection: rank the whole league, take the top N as the pool
# ---------------------------------------------------------------------------

def count_scorers(week_stats, scoring_field):
    """How many players have a real points value in one week's payload. Used
    to tell a finished week from one that's still being played."""
    return sum(1 for s in week_stats.values() if s.get(scoring_field) is not None)


def pick_ranking_window(seasons, scoring_field, live_season=None, live_week=None):
    """Returns (default_season, season_weeks, rank_week) where:

      - `default_season` is what the page's season selector opens on, and
        the season the pool is ranked out of: the live season when one is
        genuinely in progress and has any data, otherwise the most recent
        season that has data at all.
      - `season_weeks` is that season's {week: stats} map (cached, so the
        later framing passes re-use it).
      - `rank_week` is the MOST RECENT COMPLETED week in it -- the newest
        week carrying at least MIN_WEEK_PLAYERS stat lines. A run that lands
        mid-slate (Thursday night, Sunday afternoon) sees a thin partial
        week and steps back to the last finished one instead, so the pool is
        never built from the handful of players whose game has already
        kicked off. If no week clears the bar (an odd historical season, or
        a scoring field Sleeper doesn't populate), it falls back to the
        newest week with anything in it.

    Walks seasons newest -> oldest; every fetch it makes is cached."""
    for season in reversed(seasons):  # newest first
        cap = live_week if (live_season is not None and season == str(live_season) and live_week) else MAX_WEEKS_PER_SEASON
        weeks = load_season_weeks(season, cap)
        if not weeks:
            continue
        for w in sorted(weeks.keys(), reverse=True):
            if count_scorers(weeks[w], scoring_field) >= MIN_WEEK_PLAYERS:
                return season, weeks, w
        return season, weeks, max(weeks.keys())
    return None, {}, None


def rank_pool(weeks_by_week, players_dir, scoring_field, pool_size, positions,
              rank_by=DEFAULT_RANK_BY, rank_week=None):
    """Rank players and return the top `pool_size` as
    [{pid, name, pos, team, rank, rank_pts, rank_from_week, weeks_stale,
    games}, ...], best first (rank is 1-based), keeping only `positions`.

    Default (`rank_by="last_game"`): every player is ranked on THEIR OWN
    MOST RECENT GAME. Walk weeks backwards from `rank_week` and take the
    first one where the player has a real points value -- so a player who
    was on bye, inactive or hurt last week is ranked on the last week they
    actually played instead of vanishing from the report. `rank_from_week`
    records which week that number came from and `weeks_stale` how far back
    it is (0 = played the ranking week), which is what the page uses to
    caption a stale number rather than passing it off as current. The search
    stays inside `weeks_by_week`, i.e. the ranking season: a player with no
    game at all this season has nothing to rank and is left out.

    `rank_by="season"` instead totals every week in `weeks_by_week`, for a
    pool that barely moves run to run.

    Ties break on recency of the ranking game (a 20-point week 9 outranks a
    20-point week 3), then games played, then name -- so ordering is stable
    and never rewards staleness. Costs zero extra HTTP: `weeks_by_week` is
    already-cached week stats."""
    if rank_week is None:
        rank_week = max(weeks_by_week.keys()) if weeks_by_week else None

    games = {}
    for wk in weeks_by_week.values():
        for pid, stats in wk.items():
            if stats.get(scoring_field) is not None:
                games[pid] = games.get(pid, 0) + 1

    scored = {}  # pid -> (points, week that number came from)
    if rank_by == "season":
        for w in sorted(k for k in weeks_by_week if rank_week is None or k <= rank_week):
            for pid, stats in weeks_by_week[w].items():
                pts = stats.get(scoring_field)
                if pts is None:
                    continue
                prev = scored.get(pid, (0.0, w))
                scored[pid] = (prev[0] + float(pts), w)
    else:
        # Newest week first; the first hit for a player is their last game,
        # so later (older) weeks never overwrite it.
        for w in sorted((k for k in weeks_by_week if rank_week is None or k <= rank_week), reverse=True):
            for pid, stats in weeks_by_week[w].items():
                if pid in scored:
                    continue
                pts = stats.get(scoring_field)
                if pts is None:
                    continue
                scored[pid] = (float(pts), w)

    rows = []
    for pid, (pts, from_week) in scored.items():
        info = players_dir.get(pid)
        if not info:
            continue
        pos = info.get("position")
        if pos not in positions:
            continue
        name = info.get("full_name")
        if not name:
            continue
        rows.append({
            "pid": pid, "name": name, "pos": pos, "team": info.get("team") or "FA",
            "rank_pts": round(pts, 1), "rank_from_week": from_week,
            "weeks_stale": (rank_week - from_week) if (rank_week is not None and rank_by != "season") else 0,
            "games": games.get(pid, 0),
        })

    rows.sort(key=lambda r: (-r["rank_pts"], r["weeks_stale"], -r["games"], r["name"]))
    rows = rows[:pool_size]
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def resolve_watchlist_ids(players_dir, names):
    """Manual-override path: case-insensitive full_name match against the
    players directory. Returns the same shape rank_pool() does, with rank
    following the order the names were given, so the rest of the pipeline
    doesn't care which path produced the list. A name with no match is
    skipped with a warning rather than silently dropped, so a typo or a
    retirement doesn't fail the whole run."""
    by_name = {}
    for pid, info in players_dir.items():
        name = info.get("full_name")
        if name:
            by_name.setdefault(name.strip().lower(), pid)

    out = []
    for name in names:
        pid = by_name.get(name.strip().lower())
        if pid is None:
            print(f"  [warn] no Sleeper player match for '{name}' -- skipping", file=sys.stderr)
            continue
        info = players_dir[pid]
        out.append({
            "pid": pid, "name": info.get("full_name", name), "pos": info.get("position") or "?",
            "team": info.get("team") or "FA", "rank_pts": None, "games": None,
            "rank_from_week": None, "weeks_stale": 0, "rank": len(out) + 1,
        })
    return out


# ---------------------------------------------------------------------------
# Per-week framing
# ---------------------------------------------------------------------------

def team_opportunity_totals(week_stats, players_dir):
    """{team: total opportunities} for one week, where "opportunities" =
    rush attempts + targets (carries + targets) summed across every player
    on that team who recorded a stat line that week. This is the volume
    proxy that stands in for a stock's dollar volume -- a direct measure of
    how much of the offense's ball-distribution went through that team's
    various backs/receivers, independent of how well any one of them
    performed with it.

    Caveat worth knowing: team membership comes from the CURRENT players
    directory, so a mid-season trade attributes a player's earlier weeks to
    their new team's denominator. It moves usage share for traded players
    only, and never affects points or points-per-touch."""
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


def build_player_frames(pool, weekly_stats_by_week, players_dir, scoring_field):
    """For each player in `pool`, build COLUMN ARRAYS (not a list of per-week
    dicts) covering whichever weeks are present in `weekly_stats_by_week`,
    oldest -> newest:

      - pts:   that week's fantasy points in the configured scoring format
      - ppt:   points PER TOUCH that week (touches = rush attempts +
               receptions, NOT targets). None for a week with zero touches.
      - usage: player's opportunities (rush_att + rec_tgt) as a share of
               their team's total that week -- 0..1, the bubble-size input
               for BOTH quadrant panels
      - rsi:   RSI(RSI_PERIOD) run on this player's own points series so far
               (within this call's week window -- RSI resets per season,
               since a hot/cold streak shouldn't carry across an off-season)

    Column arrays rather than per-week dicts because at 300 players x 18
    weeks x 4 seasons the repeated JSON keys were the single largest thing
    in the output file. Trails are NOT stored: they're two points derived
    from indices i and max(0, i - (TRAIL_LEN - 1)) of these same arrays, so
    the front end computes them on the fly instead of shipping a
    pre-expanded copy of the data.

    A player with NO stat line in ANY week of `weekly_stats_by_week` (never
    appears in any week's dict -- not "appeared and scored 0") is omitted
    entirely, rather than plotted as a flatlined-at-zero bubble -- that's
    the "doesn't exist yet in this season" case (a rookie's earlier
    seasons, a recent addition's earlier weeks)."""
    weeks = sorted(weekly_stats_by_week.keys())
    team_totals_by_week = {w: team_opportunity_totals(weekly_stats_by_week[w], players_dir) for w in weeks}

    players_out = []
    for entry in pool:
        pid, team = entry["pid"], entry["team"]
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
        players_out.append({
            "name": entry["name"], "pos": entry["pos"], "team": team, "rank": entry["rank"],
            "weeks": weeks, "pts": pts_series, "ppt": ppt_series, "usage": usage_series,
            "rsi": [None if v is None else round(v, 1) for v in rsi_vals],
        })
    return players_out


def build_frames_by_season(pool, seasons, players_dir, scoring_field, live_season=None, live_week=None):
    """{season: [player_column_dicts...]} for each season in `seasons` that
    has any real data -- a season with nothing (further back than Sleeper
    has stats for, or a not-yet-started season) is simply omitted from the
    returned dict rather than included empty, so the front end's season
    selector only ever offers seasons that actually have something to show.

    For `live_season` (only meaningful when the caller knows that season is
    genuinely in progress), only weeks 1..`live_week` are requested since
    later weeks don't exist yet; every other season requests the full range
    (get_week_stats() gracefully returns {} for any week that doesn't
    exist, so this is harmless for a 17-week season). All of it is cached,
    so seasons already pulled during the ranking pass cost nothing here."""
    out = {}
    for season in seasons:
        week_cap = live_week if (live_season is not None and season == str(live_season) and live_week) else MAX_WEEKS_PER_SEASON
        weekly_stats_by_week = load_season_weeks(season, week_cap)
        if not weekly_stats_by_week:
            continue
        frames = build_player_frames(pool, weekly_stats_by_week, players_dir, scoring_field)
        if frames:
            out[season] = frames
    return out


def build_trend_series(pool, seasons, scoring_field):
    """For each player in the pool, a chronological (oldest -> newest) list
    of [season, week, pts, ppt, touches] across every season in `seasons`
    that has real data for them. A week is included only if the player
    actually has a stat line that week -- a rookie or a player who entered
    the league partway through this window simply starts wherever their real
    data starts, rather than being padded with zeros for seasons before they
    existed. `touches` is rush attempts + receptions; `ppt` is points PER
    TOUCH that week (None when touches is 0) -- `ppt` drives that week's
    bubble size in render_trend_svg(). Reuses get_week_stats()'s cache, so
    nothing here re-fetches."""
    series = {e["pid"]: [] for e in pool}
    for season in seasons:
        for week in range(1, MAX_WEEKS_PER_SEASON + 1):
            wk = get_week_stats(season, week)
            if not wk:
                continue
            for entry in pool:
                stats = wk.get(entry["pid"])
                if stats is None:
                    continue
                pts = stats.get(scoring_field)
                if pts is None:
                    continue
                pts = float(pts)
                touches = (stats.get("rush_att") or 0) + (stats.get("rec") or 0)
                ppt = round(pts / touches, 2) if touches > 0 else None
                series[entry["pid"]].append([season, week, round(pts, 1), ppt, touches])
    return series


# ---------------------------------------------------------------------------
# Trend grid rendering
# ---------------------------------------------------------------------------

TREND_BUBBLE_MIN_R = 2.2
TREND_BUBBLE_MAX_R = 7.0
G_SEASON, G_WEEK, G_PTS, G_PPT, G_TOUCHES = 0, 1, 2, 3, 4


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
    scale -- and neither does the top-40/full-pool distinction, so a bubble
    doesn't resize when you switch views."""
    vals = [g[G_PPT] for games in trend_series.values() for g in games if g[G_PPT] is not None]
    if not vals:
        return 0, 1
    lo, hi = min(vals), max(vals)
    if lo == hi:
        hi = lo + 1
    return lo, hi


def render_trend_svg(points, ppt_lo, ppt_hi, width=300, height=140):
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
    an entire season's segment across every card at once via a plain
    display toggle -- x/y positions never move when a season is hidden, so
    nothing needs to be recomputed client-side.

    Each bubble carries only data-i (its index into that card's game list in
    the page's TREND_GAMES map) rather than a full set of data-season/week/
    pts/ppt/touches attributes: at ~21,000 bubbles across a 300-player pool
    those repeated attribute names were about a megabyte of the output on
    their own, and the numbers are already in the page once. Hover or click
    resolves the index through one delegated listener on the grid. Fills and
    strokes come from CSS classes (.tb / .tb.last) for the same reason.
    Returns an empty-state SVG if `points` has fewer than 2 entries."""
    margin_l, margin_r, margin_t, margin_b = 8, 8, 10, 18
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    if len(points) < 2:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Not enough data">'
            f'<text x="{width/2}" y="{height/2}" text-anchor="middle" font-size="11" '
            f'fill="#6B7280" font-family="IBM Plex Mono, monospace">no data yet</text></svg>'
        )

    pts_vals = [p[G_PTS] for p in points]
    lo, hi = min(pts_vals), max(pts_vals)
    hi = max(hi, lo + 1)
    span = hi - lo
    ppt_span = (ppt_hi - ppt_lo) or 1

    def x_of(i):
        return margin_l + (i / (len(points) - 1)) * plot_w

    def y_of(v):
        return margin_t + (1 - (v - lo) / span) * plot_h

    def r_of(ppt):
        if ppt is None:
            return TREND_BUBBLE_MIN_R
        frac = max(0.0, min(1.0, (ppt - ppt_lo) / ppt_span))
        return TREND_BUBBLE_MIN_R + frac * (TREND_BUBBLE_MAX_R - TREND_BUBBLE_MIN_R)

    path_pts = [(x_of(i), y_of(p[G_PTS])) for i, p in enumerate(points)]
    parts = []

    # Season boundary dashed guides + labels -- drawn once, independent of
    # the per-season <g> grouping below (these stay visible regardless of
    # which season checkboxes are ticked, so you can still see where a
    # hidden season's gap sits).
    last_season = points[0][G_SEASON]
    parts.append(
        f'<text x="{margin_l}" y="{height - 4}" font-size="8.5" fill="#6B7280" '
        f'font-family="IBM Plex Mono, monospace">{html.escape(str(last_season))}</text>'
    )
    for i in range(1, len(points)):
        if points[i][G_SEASON] != last_season:
            gx = (x_of(i - 1) + x_of(i)) / 2
            parts.append(
                f'<line x1="{gx:.1f}" y1="{margin_t}" x2="{gx:.1f}" y2="{height - margin_b}" '
                f'stroke="rgba(107,114,128,0.4)" stroke-width="1" stroke-dasharray="2,3"/>'
            )
            parts.append(
                f'<text x="{gx + 3:.1f}" y="{height - 4}" font-size="8.5" fill="#6B7280" '
                f'font-family="IBM Plex Mono, monospace">{html.escape(str(points[i][G_SEASON]))}</text>'
            )
            last_season = points[i][G_SEASON]

    # Group points into contiguous per-season runs (safe since `points` is
    # already chronological, so each season's games are one contiguous
    # block) -- each run gets its own <path> (so a hidden season's line
    # doesn't leave a stray connector) and its own <g data-season="...">
    # wrapper for the checkbox toggle.
    i = 0
    n = len(points)
    while i < n:
        season = points[i][G_SEASON]
        j = i
        while j < n and points[j][G_SEASON] == season:
            j += 1
        run_path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in path_pts[i:j])
        group_parts = [f'<path class="tl" d="{run_path}"/>']
        for k in range(i, j):
            px, py = path_pts[k]
            r = r_of(points[k][G_PPT])
            cls = "tb last" if k == n - 1 else "tb"
            group_parts.append(
                f'<circle class="{cls}" cx="{px:.1f}" cy="{py:.1f}" r="{r:.2f}" data-i="{k}"/>'
            )
        parts.append(f'<g data-season="{html.escape(str(season))}">{"".join(group_parts)}</g>')
        i = j

    last_x, last_y = path_pts[-1]
    parts.append(
        f'<text x="{last_x:.1f}" y="{max(10.0, last_y - TREND_BUBBLE_MAX_R - 5):.1f}" text-anchor="end" '
        f'font-size="9.5" fill="#E8E6DE" font-family="IBM Plex Mono, monospace">{points[-1][G_PTS]:.1f}</text>'
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Weekly points trend across {len(points)} games, bubble size is that '
        f'game\'s points per play. Click any point for exact stats.">{"".join(parts)}</svg>'
    )


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------

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
  .status{ color:var(--dim); font-size:11px; margin-bottom:18px; line-height:1.6; }
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
  .filter-count{ font-size:10.5px; color:var(--dim); margin-left:4px; }
  .checkbox-btn{
    display:inline-flex; align-items:center; gap:6px; background:var(--panel); border:1px solid var(--line);
    color:var(--dim); font-family:'IBM Plex Mono', monospace; font-size:11px; padding:6px 12px;
    border-radius:6px; cursor:pointer; user-select:none;
  }
  .checkbox-btn input{ accent-color:var(--cyan); cursor:pointer; }
  .trend-readout{
    font-size:10.5px; color:var(--dim); border-top:1px solid var(--line); margin-top:8px; padding-top:8px;
    min-height:28px;
  }
  .trend-readout.has-data{ color:var(--ink); }
  .row{ display:flex; gap:0; flex-wrap:wrap; border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .stage-wrap{ flex:1 1 620px; min-width:0; padding:16px; display:flex; flex-direction:column; gap:10px; background:var(--panel); }
  .stage-wrap svg{ display:block; width:100%; height:auto; }
  .eq-bubble{ transition: cx .35s ease, cy .35s ease, r .35s ease, fill .35s ease, stroke .35s ease; fill-opacity:.30; stroke-width:1.6; }
  .eq-label, .eq-rsi{ transition: x .35s ease, y .35s ease, fill .35s ease; }
  .eq-trail{ transition: d .35s ease, stroke .35s ease; fill:none; stroke-width:1.4; stroke-opacity:.55; stroke-linecap:round; }
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
  .legend{ display:flex; flex-direction:column; gap:0; max-height:52vh; overflow-y:auto; }
  .legend::-webkit-scrollbar{ width:6px; }
  .legend::-webkit-scrollbar-thumb{ background:var(--line); border-radius:3px; }
  .leg-row{ display:flex; justify-content:space-between; align-items:center; font-size:11.5px; padding:6px 0; border-bottom:1px solid var(--line); gap:8px; }
  .leg-row .name{ display:flex; align-items:center; gap:7px; min-width:0; }
  .leg-row .nm{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .leg-row .pos{ color:var(--dim); font-size:9.5px; white-space:nowrap; }
  .dot{ width:8px; height:8px; border-radius:50%; display:inline-block; flex-shrink:0; }
  .leg-row .rsiv{ font-weight:600; }
  .leg-row .meta{ color:var(--dim); font-size:10px; margin-left:6px; white-space:nowrap; }
  .note{ font-size:11px; color:var(--dim); line-height:1.6; padding-top:10px; border-top:1px solid var(--line); }
  .note b{ color:var(--ink); }
  .unavailable{ padding:40px 20px; color:var(--dim); font-size:13px; }
  .trend-grid{ display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:14px; }
  .trend-card{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px 10px; }
  .trend-card-head{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px; gap:8px; }
  .trend-card-head .name{ font-size:12.5px; font-weight:600; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .trend-card-head .pos{ font-size:10px; color:var(--dim); white-space:nowrap; }
  .trend-card-head .stale{ color:#D9A441; }
  .trend-card svg{ display:block; width:100%; height:auto; }
  .trend-card .tl{ fill:none; stroke:#4FD8E8; stroke-width:1.2; stroke-opacity:.55; }
  .trend-card .tb{ fill:rgba(79,216,232,0.45); stroke:#4FD8E8; stroke-width:.8; stroke-opacity:.6; cursor:pointer; }
  .trend-card .tb.last{ fill:#4FD8E8; stroke-width:1.4; stroke-opacity:1; }
  .trend-card .tb:hover{ fill:#E8E6DE; stroke:#E8E6DE; }
  @media (max-width: 760px){ aside{ width:100%; border-left:none; border-top:1px solid var(--line); } }
"""

# Kept as a plain (non-f) string with __TOKEN__ placeholders: at this size the
# doubled-brace escaping an f-string would need makes the JS unreadable and
# very easy to break silently.
PAGE_JS = r"""
const SEASON_DATA = __SEASON_DATA__;
const AVAILABLE_SEASONS = __AVAILABLE_SEASONS__;
const TREND_GAMES = __TREND_GAMES__;
const TOP_N = __TOP_N__;
const TRAIL_LEN = __TRAIL_LEN__;
const LABEL_LIMIT = __LABEL_LIMIT__;
let currentSeason = __DEFAULT_SEASON__;
let currentPosFilter = 'ALL';

function getCSS(v){ return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
const C_NEUTRAL = getCSS('--neutral'), C_GREEN = getCSS('--green'), C_RED = getCSS('--red');

function colorFor(rsi){
  if (rsi === null || rsi === undefined) return C_NEUTRAL;
  if (rsi >= 70) return C_GREEN;
  if (rsi <= 30) return C_RED;
  return C_NEUTRAL;
}

// The whole point-selection rule lives here: ALL means "the overall top
// TOP_N of the pool" (so the scatter stays readable), any position means
// "every player at that position in the pool", however many that is.
function isVisible(p){
  return currentPosFilter === 'ALL' ? (p.k <= TOP_N) : (p.p === currentPosFilter);
}

// One shared quadrant-panel controller, parameterized by which column
// drives the x-axis ('v' = points, 'e' = points per touch). Both panels
// share the same y-axis (rsi), the same bubble-size input (usage), and are
// rebuilt from scratch whenever the season selector changes, since a
// different season can have a different set of players/weeks entirely.
//
// Unlike the 8-player version, the x-axis bounds and the bubble radius
// scale are computed over the VISIBLE set, not the whole pool -- otherwise
// a 300-player pool's outliers would squash the top-40 view into the left
// quarter of the panel.
function makeQuadrantController(scope, valKey, unitSuffix){
  const W = 900, H = 380, ML = 50, MR = 20, MT = 26, MB = 34;
  const PW = W - ML - MR, PH = H - MT - MB;

  const svg = document.getElementById('svg-' + scope);
  const slider = document.getElementById('slider-' + scope);
  const scrubLabel = document.getElementById('scrubLabel-' + scope);
  const scrubTs = document.getElementById('scrubTs-' + scope);
  const leftCountEl = document.getElementById('leftCount-' + scope);
  const rightCountEl = document.getElementById('rightCount-' + scope);
  const legendEl = document.getElementById('legend-' + scope);

  let all = [], weeks = [], vis = [], nodes = [];
  let X_MIN = 0, X_MAX = 1;

  function pxOf(v){
    if (v === null || v === undefined) v = X_MIN;
    v = Math.max(X_MIN, Math.min(X_MAX, v));
    return ML + (v - X_MIN) / ((X_MAX - X_MIN) || 1) * PW;
  }
  function pyOf(rsi){
    if (rsi === null || rsi === undefined) rsi = 50;
    rsi = Math.max(0, Math.min(100, rsi));
    return MT + (1 - rsi / 100) * PH;
  }

  function computeBounds(){
    let lo = Infinity, hi = -Infinity;
    for (const p of vis){
      const col = p[valKey];
      for (let i = 0; i < col.length; i++){
        const v = col[i];
        if (v === null || v === undefined) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (lo === Infinity){ lo = 0; hi = 1; }
    const pad = Math.max(0.1, (hi - lo) * 0.1);
    X_MIN = Math.max(0, lo - pad);
    X_MAX = (hi + pad) || 1;
  }

  function initSvg(){
    nodes = [];
    if (!all.length){
      svg.innerHTML = '<text x="' + (W/2) + '" y="' + (H/2) + '" text-anchor="middle" font-size="12" ' +
        'fill="#6B7280" font-family="IBM Plex Mono, monospace">No data for this season</text>';
      return;
    }
    const midY = pyOf(50);
    const parts = [
      '<line x1="'+ML+'" y1="'+midY.toFixed(1)+'" x2="'+(W-MR)+'" y2="'+midY.toFixed(1)+'" stroke="rgba(107,114,128,0.35)" stroke-width="1" stroke-dasharray="3,5"/>',
      '<text x="'+ML+'" y="'+(MT-8)+'" font-size="10" fill="#6B7280">INDEX 100 &middot; hot</text>',
      '<text id="axLo-'+scope+'" x="'+ML+'" y="'+(H-MB+16)+'" font-size="10" fill="#6B7280"></text>',
      '<text id="axHi-'+scope+'" x="'+(W-MR)+'" y="'+(H-MB+16)+'" text-anchor="end" font-size="10" fill="#6B7280"></text>',
      '<text x="'+ML+'" y="'+(H-MB+28)+'" font-size="10" fill="#6B7280">INDEX 0 &middot; cold</text>',
      '<line x1="'+ML+'" y1="'+(H-MB)+'" x2="'+(W-MR)+'" y2="'+(H-MB)+'" stroke="rgba(30,38,51,1)" stroke-width="1"/>'
    ];
    // One set of elements per player in the season, created once. Filtering
    // and scrubbing only ever mutate attributes -- nothing is re-created,
    // which is what keeps a 300-node panel smooth.
    all.forEach(function(p){
      const id = scope + '-' + p.i;
      parts.push('<path class="eq-trail" id="t-'+id+'" d=""/>');
      parts.push('<circle class="eq-bubble" id="b-'+id+'" cx="0" cy="0" r="0"><title></title></circle>');
      parts.push('<text class="eq-label" id="l-'+id+'" x="0" y="0" text-anchor="middle" font-size="11" font-weight="600"></text>');
      parts.push('<text class="eq-rsi" id="r-'+id+'" x="0" y="0" text-anchor="middle" font-size="9.5" fill="rgba(232,230,222,0.55)"></text>');
    });
    svg.innerHTML = parts.join('');
    all.forEach(function(p){
      const id = scope + '-' + p.i;
      nodes.push({
        p: p,
        trail: document.getElementById('t-'+id),
        bubble: document.getElementById('b-'+id),
        title: document.querySelector('#b-'+id+' title'),
        label: document.getElementById('l-'+id),
        rsi: document.getElementById('r-'+id)
      });
    });
  }

  function renderWeek(wi){
    if (!all.length || !weeks.length){
      leftCountEl.textContent = '0'; rightCountEl.textContent = '0';
      scrubTs.textContent = 'Showing: n/a'; scrubLabel.textContent = 'n/a';
      legendEl.innerHTML = '';
      return;
    }
    wi = Math.max(0, Math.min(weeks.length - 1, wi));

    const visIds = new Set(vis.map(function(p){ return p.i; }));

    // Bubble radius scale: usage share across the visible players only.
    let uLo = Infinity, uHi = -Infinity;
    vis.forEach(function(p){
      const u = p.u[wi];
      if (u < uLo) uLo = u;
      if (u > uHi) uHi = u;
    });
    if (uLo === Infinity){ uLo = 0; uHi = 1; }
    const uSpan = (uHi - uLo) || 1;
    // Bubbles shrink as the crowd grows, so a 90-player position view
    // doesn't turn into overlapping discs.
    const rMax = vis.length > 60 ? 14 : (vis.length > 25 ? 18 : 26);
    const rMin = vis.length > 60 ? 3.5 : 6;

    // Only the leaders on the current x-axis get a name label -- 90 labels
    // at once is unreadable. Everything else keeps its hover tooltip.
    const ranked = vis.slice().sort(function(a, b){
      const av = a[valKey][wi], bv = b[valKey][wi];
      return (bv === null || bv === undefined ? -1 : bv) - (av === null || av === undefined ? -1 : av);
    });
    const labelled = new Set(ranked.slice(0, LABEL_LIMIT).map(function(p){ return p.i; }));

    let left = 0, right = 0;
    const legendRows = [];

    nodes.forEach(function(n){
      const p = n.p;
      const on = visIds.has(p.i);
      const disp = on ? '' : 'none';
      n.bubble.style.display = disp;
      n.trail.style.display = disp;
      n.label.style.display = disp;
      n.rsi.style.display = disp;
      if (!on) return;

      const val = p[valKey][wi];
      const hasVal = val !== null && val !== undefined;
      const rsiRaw = p.r[wi];
      const rsi = (rsiRaw === null || rsiRaw === undefined) ? 50 : rsiRaw;
      const color = colorFor(rsiRaw);
      const r = hasVal ? (rMin + ((p.u[wi] - uLo) / uSpan) * (rMax - rMin)) : 0;
      const px = pxOf(val), py = pyOf(rsi);

      n.bubble.setAttribute('cx', px.toFixed(1));
      n.bubble.setAttribute('cy', py.toFixed(1));
      n.bubble.setAttribute('r', r.toFixed(1));
      n.bubble.setAttribute('fill', color);
      n.bubble.setAttribute('stroke', color);
      n.title.textContent = p.n + ' (' + p.p + ' · ' + p.t + ') — ' +
        (hasVal ? val.toFixed(2) + unitSuffix : 'n/a') +
        ', index ' + ((rsiRaw === null || rsiRaw === undefined) ? '—' : Math.round(rsiRaw)) +
        ', usage ' + (p.u[wi] * 100).toFixed(0) + '%';

      const showLabel = hasVal && labelled.has(p.i);
      n.label.style.display = showLabel ? '' : 'none';
      n.rsi.style.display = showLabel ? '' : 'none';
      if (showLabel){
        n.label.setAttribute('x', px.toFixed(1));
        n.label.setAttribute('y', (py - r - 8).toFixed(1));
        n.label.setAttribute('fill', color);
        n.label.textContent = p.n;
        n.rsi.setAttribute('x', px.toFixed(1));
        n.rsi.setAttribute('y', (py + r + 14).toFixed(1));
        n.rsi.textContent = ((rsiRaw === null || rsiRaw === undefined) ? '—' : Math.round(rsiRaw)) +
          ' · ' + val.toFixed(2) + unitSuffix;
      }

      // Trail: two points, from (TRAIL_LEN - 1) weeks back straight to now.
      // Derived here rather than shipped in the JSON.
      const si = Math.max(0, wi - (TRAIL_LEN - 1));
      const sv = p[valKey][si];
      if (si !== wi && hasVal && sv !== null && sv !== undefined){
        const sx = pxOf(sv), sy = pyOf(p.r[si]);
        n.trail.setAttribute('d', 'M ' + sx.toFixed(1) + ',' + sy.toFixed(1) + ' L ' + px.toFixed(1) + ',' + py.toFixed(1));
      } else {
        n.trail.setAttribute('d', '');
      }
      n.trail.setAttribute('stroke', color);

      if (rsi < 49) left++; else if (rsi > 51) right++;
      legendRows.push({ p: p, color: color, rsi: rsi, rsiRaw: rsiRaw, val: val, hasVal: hasVal });
    });

    document.getElementById('axLo-' + scope).textContent = X_MIN.toFixed(1) + unitSuffix;
    document.getElementById('axHi-' + scope).textContent = X_MAX.toFixed(1) + unitSuffix + ' →';
    leftCountEl.textContent = left;
    rightCountEl.textContent = right;
    scrubTs.textContent = 'Showing: week ' + weeks[wi] + ' of ' + currentSeason +
      ' · ' + vis.length + ' players' + (currentPosFilter === 'ALL' ? ' (top ' + TOP_N + ')' : ' (' + currentPosFilter + ')');
    scrubLabel.textContent = 'Week ' + weeks[wi];

    legendRows.sort(function(a, b){ return b.rsi - a.rsi; });
    legendEl.innerHTML = legendRows.map(function(row){
      const rsiDisp = (row.rsiRaw === null || row.rsiRaw === undefined) ? '—' : Math.round(row.rsiRaw);
      const valDisp = row.hasVal ? row.val.toFixed(2) + unitSuffix : 'n/a';
      return '<div class="leg-row">' +
        '<span class="name"><span class="dot" style="background:' + row.color + '"></span>' +
        '<span class="nm">' + row.p.n + '</span><span class="pos">' + row.p.p + ' · ' + row.p.t + '</span></span>' +
        '<span><span class="rsiv" style="color:' + row.color + '">' + rsiDisp + '</span>' +
        '<span class="meta">' + valDisp + ' · ' + (row.p.u[wi] * 100).toFixed(0) + '%</span></span>' +
        '</div>';
    }).join('');
  }

  function refilter(){
    vis = all.filter(isVisible);
    computeBounds();
    renderWeek(+slider.value);
  }

  function loadSeason(season){
    const d = SEASON_DATA[season] || { weeks: [], players: [] };
    weeks = d.weeks;
    all = d.players.map(function(p, i){ p.i = i; return p; });
    vis = all.filter(isVisible);
    computeBounds();
    slider.max = Math.max(0, weeks.length - 1);
    slider.value = Math.max(0, weeks.length - 1);
    initSvg();
    renderWeek(weeks.length - 1);
  }

  slider.addEventListener('input', function(){ renderWeek(+slider.value); });
  return { loadSeason: loadSeason, refilter: refilter };
}

const panel1 = makeQuadrantController('p1', 'v', 'pt');
const panel2 = makeQuadrantController('p2', 'e', 'pt/tch');

function applyPositionFilter(pos){
  currentPosFilter = pos;
  document.querySelectorAll('#posFilter .filter-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-pos') === pos);
  });
  panel1.refilter();
  panel2.refilter();

  let shown = 0;
  document.querySelectorAll('#trendGrid .trend-card').forEach(function(c){
    const ok = pos === 'ALL' ? (+c.getAttribute('data-rank') <= TOP_N) : (c.getAttribute('data-pos') === pos);
    c.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });
  document.getElementById('posCount').textContent =
    pos === 'ALL' ? ('top ' + TOP_N + ' of the pool') : (shown + ' ' + pos + 's in the pool');
  document.getElementById('trendCount').textContent = shown + ' cards';
}

function applySeason(season){
  currentSeason = season;
  panel1.loadSeason(season);
  panel2.loadSeason(season);
  document.querySelectorAll('#seasonFilter .season-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-season') === season);
  });
}

document.getElementById('posFilter').addEventListener('click', function(e){
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  applyPositionFilter(btn.getAttribute('data-pos'));
});
document.getElementById('seasonFilter').addEventListener('click', function(e){
  const btn = e.target.closest('.season-btn');
  if (!btn) return;
  applySeason(btn.getAttribute('data-season'));
});

if (AVAILABLE_SEASONS.length){ applySeason(currentSeason); }
applyPositionFilter('ALL');

// -- 4-season trend grid: season checkboxes + hover/click to inspect --
// Toggling a season checkbox hides/shows every <g data-season="YYYY">
// group across every trend card at once (positions never move -- this is
// a pure visibility toggle, same principle as the position filter).
function applyTrendSeasonToggle(){
  const checked = new Set(
    Array.prototype.slice.call(document.querySelectorAll('.trend-season-cb:checked')).map(function(cb){ return cb.value; })
  );
  document.querySelectorAll('#trendGrid g[data-season]').forEach(function(g){
    g.style.display = checked.has(g.getAttribute('data-season')) ? '' : 'none';
  });
}
document.querySelectorAll('.trend-season-cb').forEach(function(cb){
  cb.addEventListener('change', applyTrendSeasonToggle);
});
applyTrendSeasonToggle();

// One delegated listener for the whole grid rather than an onclick per
// bubble: with a 300-player pool that's ~21,000 bubbles, and per-element
// handlers (and per-element data attributes) were the bulk of the file.
// The bubble carries only its index; the numbers live once in TREND_GAMES.
function showTrendStats(circle){
  const card = circle.closest('.trend-card');
  if (!card) return;
  const games = TREND_GAMES[card.getAttribute('data-pid')];
  if (!games) return;
  const g = games[+circle.getAttribute('data-i')];
  if (!g) return;
  const el = card.querySelector('.trend-readout');
  el.innerHTML = '<b>' + g[0] + ' wk' + g[1] + ':</b> ' + g[2].toFixed(1) + 'pt · ' +
    (g[3] === null ? 'n/a' : g[3].toFixed(2)) + ' pt/play · ' + g[4] + ' touches';
  el.classList.add('has-data');
}
const grid = document.getElementById('trendGrid');
if (grid){
  ['click', 'mouseover'].forEach(function(evt){
    grid.addEventListener(evt, function(e){
      const c = e.target.closest('circle.tb');
      if (c) showTrendStats(c);
    });
  });
}
"""


def render_html(frames_by_season, trend_series, pool, default_season, season, current_week,
                season_type, scoring_label, watchlist_missing, now, trend_seasons_label, trend_seasons,
                ranking_season, rank_week, rank_by, pool_size, top_n, selection_mode):
    """Self-contained HTML page: two scrubbable quadrant panels (points
    scored / points per touch, both x hot-cold index) that share a SEASON
    SELECTOR (any season in `frames_by_season` that actually has data) and
    a position filter, plus a 4-season trend grid -- same visual language
    as the moneyflow-update Equilibrium quadrant panels, fed real Sleeper
    data instead of yfinance OHLC.

    The position filter is the load-bearing control now that the report is
    a 300-player pool: ALL plots the top `top_n` by ranking-season points,
    any position plots every player at that position in the pool."""
    as_of = now.strftime("%Y-%m-%d %H:%M UTC")
    available_seasons = sorted(frames_by_season.keys())  # oldest -> newest

    if not available_seasons and not any(trend_series.values()):
        body = """
<div class="eyebrow">Fantasy Quadrant</div>
<h1>Hot/Cold Index &times; Weekly Performance</h1>
<div class="unavailable">No weekly stats available yet from Sleeper for any of the last few seasons --
check back once games have been played, or verify FANTASY_WATCHLIST names match Sleeper's player
directory.</div>"""
        return _wrap_html(body)

    missing_note = ""
    if watchlist_missing:
        missing_note = (
            "<br><b>Not found on Sleeper this run:</b> "
            + html.escape(", ".join(watchlist_missing))
            + " -- check spelling in FANTASY_WATCHLIST, or they may not be rostered/active."
        )

    all_positions = sorted(
        {p["pos"] for p in pool},
        key=lambda pos: (POSITION_ORDER.index(pos) if pos in POSITION_ORDER else len(POSITION_ORDER), pos),
    )
    pos_counts = {}
    for p in pool:
        pos_counts[p["pos"]] = pos_counts.get(p["pos"], 0) + 1

    filter_buttons = '<button class="filter-btn active" data-pos="ALL">ALL</button>' + "".join(
        f'<button class="filter-btn" data-pos="{html.escape(pos)}">{html.escape(pos)} '
        f'<span style="opacity:.6">{pos_counts.get(pos, 0)}</span></button>'
        for pos in all_positions
    )
    season_buttons = "".join(
        f'<button class="filter-btn season-btn{" active" if s == default_season else ""}" '
        f'data-season="{html.escape(s)}">{html.escape(s)}</button>'
        for s in available_seasons
    )
    trend_season_checkboxes = "".join(
        f'<label class="checkbox-btn"><input type="checkbox" class="trend-season-cb" '
        f'value="{html.escape(s)}" checked> {html.escape(s)}</label>'
        for s in trend_seasons
    )

    # Compact, column-oriented payload. Keys are one letter each and the
    # per-week values are parallel arrays rather than a dict per week --
    # at 300 players x ~18 weeks x 4 seasons the repeated key names were by
    # far the biggest thing in the file.
    season_data = {}
    for s, plist in frames_by_season.items():
        season_data[s] = {
            "weeks": plist[0]["weeks"] if plist else [],
            "players": [
                {"n": p["name"], "p": p["pos"], "t": p["team"], "k": p["rank"],
                 "v": p["pts"], "e": p["ppt"], "u": p["usage"], "r": p["rsi"]}
                for p in plist
            ],
        }

    ppt_lo, ppt_hi = global_ppt_bounds(trend_series)
    trend_cards, trend_games = [], {}
    for entry in pool:
        pid = entry["pid"]
        games = trend_series.get(pid, [])
        if games:
            trend_games[pid] = games
        svg = render_trend_svg(games, ppt_lo, ppt_hi)
        n_games = len(games)
        span_note = f"{n_games} games" if n_games else "no data"
        # Only flagged when the ranking number ISN'T from the current week --
        # otherwise every card would carry a redundant badge.
        stale_note = (f' &middot; <span class="stale">last: wk {entry["rank_from_week"]}</span>'
                      if (entry.get("weeks_stale") or 0) > 0 else "")
        rank_note = f"#{entry['rank']}"
        trend_cards.append(f"""
      <div class="trend-card" data-pid="{html.escape(pid)}" data-pos="{html.escape(entry['pos'])}" data-rank="{entry['rank']}">
        <div class="trend-card-head">
          <span class="name">{rank_note} {html.escape(entry['name'])}</span>
          <span class="pos">{html.escape(entry['pos'])} &middot; {html.escape(entry['team'])} &middot; {span_note}{stale_note}</span>
        </div>
        {svg}
        <div class="trend-readout">Hover or click a point for that game's stats</div>
      </div>""")

    if season_type == "regular" and str(season) in frames_by_season:
        live_note = f" &middot; live season {html.escape(str(season))}, through week {current_week}"
    else:
        live_note = (f" &middot; off-season -- defaulting to the most recent completed season "
                     f"({html.escape(default_season or '?')})")

    stale_count = sum(1 for p in pool if (p.get("weeks_stale") or 0) > 0)
    if selection_mode == "ranked":
        if rank_by != "season":
            basis = (f"the {html.escape(scoring_label)} points in <b>each player's own most recent "
                     f"game</b>, as of <b>{html.escape(str(ranking_season))} week {rank_week}</b>")
            basis_caveat = (
                f" &mdash; a player on bye, hurt or inactive is ranked on the last week they actually "
                f"played, not dropped"
                + (f" ({stale_count} of the {len(pool)} are ranked on an earlier week; their trend cards "
                   f"say <b>last: wk N</b>)" if stale_count else "")
            )
        else:
            basis = (f"season-to-date {html.escape(scoring_label)} points in "
                     f"<b>{html.escape(str(ranking_season))}</b>")
            basis_caveat = ""
        selection_note = (
            f"Pool = top <b>{pool_size}</b> players by {basis}{basis_caveat}. "
            f"<b>ALL</b> plots the top {top_n} of that pool; picking a position plots "
            f"<b>every</b> player at that position in the pool."
        )
    else:
        selection_note = (f"Player list supplied via FANTASY_WATCHLIST ({len(pool)} players) -- ranking skipped. "
                          f"ALL plots the first {top_n}; picking a position plots every one at that position.")

    body = f"""
<div class="eyebrow">Fantasy Quadrant</div>
<h1>Hot/Cold Index &times; Weekly Performance</h1>
<div class="sub">
  <b>Y-axis (both quadrant panels)</b> = a {RSI_PERIOD}-week &quot;hot/cold index&quot; (RSI math run on
  each player's own weekly fantasy points within the selected season, relative to their own recent
  baseline, not other players). <b>Trail</b> = one line from {TRAIL_LEN - 1} weeks ago straight to this
  week -- no simulated motion, every slider position is a real past week's stat line.
</div>
<div class="status">
  {len(pool)} players in report &middot; generated {html.escape(as_of)}{live_note}<br>
  {selection_note}{missing_note}
</div>

<div class="filter-bar" id="posFilter">
  <span class="filter-bar-label">Position</span>{filter_buttons}
  <span class="filter-count" id="posCount"></span>
</div>
<div class="filter-bar" id="seasonFilter"><span class="filter-bar-label">Season</span>{season_buttons}</div>

<div class="section" id="sec-p1">
  <div class="section-head">
    <h2>Hot/Cold Index &times; Points Scored</h2>
    <div class="sub">X-axis = actual points scored that week ({html.escape(scoring_label)}). Bubble size =
    usage share -- opportunities (carries + targets) as a share of the player's own team's total that
    week. Axis range and bubble scale follow whichever players are currently shown, so the top-40 view
    isn't squashed by the full pool's outliers. Only the week's leaders get name labels; hover any
    bubble for the rest.</div>
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
    <h2>4-Season Points Trend ({html.escape(trend_seasons_label)}) <span class="filter-count" id="trendCount"></span></h2>
    <div class="sub">Weekly {html.escape(scoring_label)} points, chronological, across as much of the last
    4 regular seasons as each player actually has -- a rookie or a recent addition just starts wherever
    their real data starts. Each point is a bubble sized by that game's <b>points per play</b> (touches),
    scaled <b>relative to every game, for every player, in this report</b> -- so bubble size never
    changes when you filter. <b>Hover or click any point</b> to see that exact game's stats below its
    card. Dashed lines always mark season boundaries, regardless of which seasons are checked below.
    Cards follow the position filter above, and always cover the same 4-season span independent of the
    season selected in the panels.</div>
  </div>
  <div class="filter-bar" id="trendSeasonToggle"><span class="filter-bar-label">Show seasons</span>{trend_season_checkboxes}</div>
  <div class="trend-grid" id="trendGrid">{"".join(trend_cards)}</div>
</div>

<script>
{_page_js(season_data, available_seasons, trend_games, default_season, top_n)}
</script>
"""
    return _wrap_html(body)


def _page_js(season_data, available_seasons, trend_games, default_season, top_n):
    """Substitute the run's data into PAGE_JS. json.dumps with compact
    separators throughout -- whitespace on a payload this size is measured
    in hundreds of kilobytes."""
    def j(v):
        return json.dumps(v, separators=(",", ":"))

    return (PAGE_JS
            .replace("__SEASON_DATA__", j(season_data))
            .replace("__AVAILABLE_SEASONS__", j(available_seasons))
            .replace("__TREND_GAMES__", j(trend_games))
            .replace("__DEFAULT_SEASON__", j(default_season))
            .replace("__TOP_N__", str(top_n))
            .replace("__TRAIL_LEN__", str(TRAIL_LEN))
            .replace("__LABEL_LIMIT__", str(LABEL_LIMIT)))


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


# ---------------------------------------------------------------------------

def _env_int(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        print(f"  [warn] {name}={raw!r} isn't an integer -- using {default}", file=sys.stderr)
        return default


def main():
    scoring_key = os.environ.get("FANTASY_SCORING", "ppr").strip().lower()
    scoring_field = SCORING_FIELD.get(scoring_key, "pts_ppr")
    scoring_label = {"pts_ppr": "PPR", "pts_half_ppr": "Half-PPR", "pts_std": "Standard"}[scoring_field]

    pool_size = _env_int("FANTASY_POOL_SIZE", DEFAULT_POOL_SIZE)
    top_n = _env_int("FANTASY_TOP_N", DEFAULT_TOP_N)
    trend_seasons_back = _env_int("FANTASY_TREND_SEASONS", DEFAULT_TREND_SEASONS)
    positions = tuple(
        p.strip().upper() for p in os.environ.get("FANTASY_POSITIONS", ",".join(POSITION_ORDER)).split(",")
        if p.strip()
    )
    manual_names = [n.strip() for n in os.environ.get("FANTASY_WATCHLIST", "").split(",") if n.strip()]
    rank_by = os.environ.get("FANTASY_RANK_BY", DEFAULT_RANK_BY).strip().lower()
    if rank_by == "week":  # earlier name for the same thing
        rank_by = "last_game"
    if rank_by not in ("last_game", "season"):
        print(f"  [warn] FANTASY_RANK_BY={rank_by!r} unrecognized -- using {DEFAULT_RANK_BY!r}", file=sys.stderr)
        rank_by = DEFAULT_RANK_BY

    print("[info] fetching Sleeper state...")
    state = get_state()
    season = state.get("league_season") or state.get("season")
    current_week = state.get("week") or 0
    season_type = state.get("season_type", "regular")
    print(f"[info] season={season} week={current_week} season_type={season_type}")

    print("[info] fetching Sleeper players directory (~5MB, this is the slow step)...")
    players_dir = get_players_directory()
    print(f"[info] {len(players_dir)} players in directory")

    now = datetime.now(timezone.utc)
    if not season:
        print("[warn] Sleeper state returned no season -- writing an 'unavailable' placeholder page.")
        with open("index.html", "w") as f:
            f.write(render_html({}, {}, [], None, season, current_week, season_type, scoring_label,
                                [], now, "", [], None, None, rank_by, pool_size, top_n, "ranked"))
        return

    seasons = season_list(season, trend_seasons_back)
    is_live = bool(season_type == "regular" and current_week and current_week >= 1)
    live_season = season if is_live else None
    live_week = current_week if is_live else None

    print(f"[info] resolving ranking window across {seasons} "
          f"({'live -- capping ' + str(season) + ' at week ' + str(current_week) if is_live else 'off-season'})...")
    ranking_season, ranking_weeks, rank_week = pick_ranking_window(seasons, scoring_field, live_season, live_week)
    default_season = ranking_season
    print(f"[info] default season={default_season}, {len(ranking_weeks)} weeks of data, "
          f"ranking on {'season totals' if rank_by == 'season' else 'each player last game as of week ' + str(rank_week)}")

    missing = []
    if manual_names:
        selection_mode = "manual"
        pool = resolve_watchlist_ids(players_dir, manual_names)
        resolved_names = {p["name"] for p in pool}
        missing = [n for n in manual_names if n not in resolved_names]
        print(f"[info] FANTASY_WATCHLIST set -- ranking skipped, {len(pool)} players resolved")
    else:
        selection_mode = "ranked"
        if not ranking_weeks:
            print("[warn] no season with usable stats -- nothing to rank.", file=sys.stderr)
            pool = []
        else:
            pool = rank_pool(ranking_weeks, players_dir, scoring_field, pool_size, positions,
                             rank_by=rank_by, rank_week=rank_week)
            basis = (f"{ranking_season} season totals" if rank_by == "season"
                     else f"last game played through {ranking_season} week {rank_week}")
            print(f"[info] pool = top {len(pool)} ({'/'.join(positions)}) by {scoring_label} points, {basis}")
            if pool:
                stale = sum(1 for p in pool if (p.get("weeks_stale") or 0) > 0)
                head = ", ".join(
                    f"{p['rank']}.{p['name']} ({p['rank_pts']}"
                    + (f", wk{p['rank_from_week']}" if (p.get('weeks_stale') or 0) > 0 else "") + ")"
                    for p in pool[:5]
                )
                print(f"[info] top of pool: {head}")
                if rank_by != "season":
                    print(f"[info] {stale} of {len(pool)} ranked on an earlier week (bye/inactive/injured)")

    if not pool:
        with open("index.html", "w") as f:
            f.write(render_html({}, {}, [], default_season, season, current_week, season_type,
                                scoring_label, missing, now, "", seasons, ranking_season, rank_week,
                                rank_by, pool_size, top_n, selection_mode))
        print("[info] wrote index.html (empty-state)")
        return

    print(f"[info] framing {len(pool)} players across seasons {seasons} "
          f"(cached fetches reused from the ranking pass)...")
    frames_by_season = build_frames_by_season(pool, seasons, players_dir, scoring_field,
                                              live_season=live_season, live_week=live_week)
    if default_season not in frames_by_season and frames_by_season:
        default_season = sorted(frames_by_season.keys())[-1]
    print(f"[info] seasons with data: {sorted(frames_by_season.keys())} -- default={default_season}")

    print(f"[info] building {trend_seasons_back}-season trend history...")
    trend_series = build_trend_series(pool, seasons, scoring_field)
    trend_seasons_label = f"{seasons[0]}–{seasons[-1]}" if seasons else "?"

    with open("index.html", "w") as f:
        f.write(render_html(frames_by_season, trend_series, pool, default_season, season, current_week,
                            season_type, scoring_label, missing, now, trend_seasons_label, seasons,
                            ranking_season, rank_week, rank_by, pool_size, top_n, selection_mode))

    size_mb = os.path.getsize("index.html") / (1024 * 1024)
    print(f"[info] wrote index.html ({size_mb:.1f} MB, {len(pool)} players, "
          f"{sum(len(v) for v in trend_series.values())} game bubbles)")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        basis = (f"**{ranking_season}** season-to-date totals" if rank_by == "season"
                 else f"**each player's most recent game** through {ranking_season} week {rank_week}")
        lines = [
            f"## Fantasy Quadrant — {len(pool)} players, default season {default_season or 'n/a'}",
            "",
            f"Pool ranked on {basis}; ALL view shows the top {top_n}, "
            f"position views show the whole pool at that position.",
            "",
            "| # | Player | Pos | Team | Last game | pt/tch | Index | Usage | Trend games |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        by_name = {p["name"]: p for p in frames_by_season.get(default_season, [])} if default_season else {}
        for entry in pool[:top_n]:
            p = by_name.get(entry["name"])
            n_games = len(trend_series.get(entry["pid"], []))
            # The ranking number and the week it came from, so a stale entry
            # is never read as last week's production.
            stale = (entry.get("weeks_stale") or 0) > 0
            last_game = (f"{entry['rank_pts']:.1f}" if entry["rank_pts"] is not None else "—")
            if stale:
                last_game += f" _(wk {entry['rank_from_week']})_"
            if p and p["weeks"]:
                # Efficiency/index/usage are read from the same week the
                # ranking points came from, not blindly from the last frame.
                try:
                    wi = p["weeks"].index(entry["rank_from_week"])
                except (ValueError, TypeError):
                    wi = -1
                ppt, rsi, usage = p["ppt"][wi], p["rsi"][wi], p["usage"][wi]
                lines.append(
                    f"| {entry['rank']} | {entry['name']} | {entry['pos']} | {entry['team']} | "
                    f"{last_game} | {'n/a' if ppt is None else f'{ppt:.2f}'} | "
                    f"{'n/a' if rsi is None else f'{rsi:.0f}'} | {usage*100:.0f}% | {n_games} |"
                )
            else:
                lines.append(
                    f"| {entry['rank']} | {entry['name']} | {entry['pos']} | {entry['team']} | "
                    f"{last_game} | — | — | — | {n_games} |"
                )
        if missing:
            lines += ["", f"Not found on Sleeper: {', '.join(missing)}"]
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
