#!/usr/bin/env python3
"""
Fantasy Trends: per-player weekly charts for the top slice of the NFL, built
fresh from the Sleeper API on a weekly cron.

Three panels, one shared pool, one shared set of filters:

  1. VOLUME vs EFFICIENCY (scatter) -- points per game on the y-axis against
     snaps per game on the x-axis, over a week range you scrub with a slider.
     Bubble size is that player's CURRENT-SEASON points per game across all
     their games this season, so bubble size is a stable reference that does
     not move as you slide the window. Position is encoded by MARKER SHAPE,
     not colour (see the note on colour below). Median crosshairs split the
     plot into workhorse / efficient / low-usage / struggling quadrants.
  2. WEEKLY POINTS (small multiples) -- fantasy points per game,
     chronological, across as much of the last 6 regular seasons as each
     player actually has. Labelled y-axis, average line, peak and latest
     values called out.
  3. POINTS PER SNAP (small multiples) -- the same weeks divided by
     offensive snaps played: who produces per unit of playing time rather
     than who simply never leaves the field.

On the small multiples every week is a bubble sized by that game's SNAP
COUNT, scaled across every game for every player in the report, so bubble
size means the same thing on both grids and on every card.

A NOTE ON COLOUR
----------------
The scatter needs four categorical identities (QB/RB/WR/TE). No four-hue
palette clears the all-pairs colour-vision floors that a scatter requires --
verified with the data-viz validator against this page's surface, where
every candidate four-hue set failed both the CVD separation and
normal-vision checks (worst pairs came in at dE 1.6-4.8 against a floor of
8). Encoding position as SHAPE at a single hue sidesteps the problem
entirely rather than shipping four colours that a colour-blind reader --
or anyone on a bad monitor -- cannot separate. The two small-multiple grids
each use one hue, which needs no legend.

PLAYER SELECTION
----------------
The report is not a hand-maintained list. Each run:

  1. Ranks EVERY player by the fantasy points they scored in THEIR OWN MOST
     RECENT GAME -- for most players that's the latest completed week, but a
     player who was on bye, injured or inactive is ranked on the last week
     they actually played rather than being dropped from the report.
     Filtered to QB/RB/WR/TE.
  2. Takes the top FANTASY_POOL_SIZE (default 300) of them as the pool.
  3. The ALL view shows only the top FANTASY_TOP_N (default 40) of that
     pool. Selecting a POSITION shows EVERY player at that position in the
     full pool. Searching or pinning specific players overrides the top-N
     cap entirely -- if you asked for a player by name, you get them
     whatever their rank.

A player's last game is looked for within the ranking season only, newest
week first. How stale each player's number is shows on their card ("last:
wk N" whenever it isn't the current week), so a week-2 number is never
silently compared to a week-9 one. FANTASY_RANK_BY=season switches to
season-to-date totals if you'd rather the pool be stable.

"Most recent completed week" is the newest week carrying a full slate of
stat lines (at least MIN_WEEK_PLAYERS players). A run that lands mid-slate
-- Thursday night, or Sunday afternoon -- steps back to the last finished
week rather than ranking everyone against the handful of players whose game
has kicked off. Setting FANTASY_WATCHLIST bypasses ranking entirely.

Meant to run on GitHub Actions on a weekly cron (e.g. Tuesday morning, after
Monday Night Football has posted final stats) or any machine with normal
internet access -- NOT inside a locked-down sandbox with a network
allowlist, since it talks to api.sleeper.app.

Writes one file to the repo each run:

    index.html - self-contained visual, so the repo always has an
                 up-to-date snapshot you can open directly or serve via
                 GitHub Pages -- no external tooling required to view it.

Every chart is drawn in the browser from one compact data payload rather
than pre-rendered as SVG server-side. That keeps a 300-player, 6-season
report under a megabyte instead of several, lets the scatter recompute as
you drag the week slider, and means only the cards actually on screen get
drawn.

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
                                              and cached in-process, so the
                                              ranking pass and the trend
                                              build share the same fetches.

Env vars (all optional):

    FANTASY_SCORING   - "ppr" (default), "half_ppr", or "std".
    FANTASY_POOL_SIZE - how many top-ranked players make the pool
                         (default 300). This is what the position filter
                         and the player search expose.
    FANTASY_TOP_N     - how many of the pool the unfiltered ALL view shows
                         (default 40). Position, search and pins ignore it.
    FANTASY_RANK_BY   - "last_game" (default) ranks by each player's own
                         most recent game; "season" ranks by season-to-date
                         totals for a pool that barely moves week to week.
    FANTASY_WATCHLIST - comma-separated player full names. If set, ranking
                         is skipped and exactly these players are used.
    FANTASY_POSITIONS - comma-separated positions eligible for the pool
                         (default "QB,RB,WR,TE").
    FANTASY_TREND_SEASONS - how many regular seasons the report covers,
                         including the current one (default 6).
"""

import html
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

SLEEPER_BASE = "https://api.sleeper.app/v1"

DEFAULT_TREND_SEASONS = 6
DEFAULT_POOL_SIZE = 300
DEFAULT_TOP_N = 40
MAX_WEEKS_PER_SEASON = 18  # current NFL regular-season length; harmless if a season had 17
MIN_WEEK_PLAYERS = 150     # below this, a week is mid-slate (or garbage) -- step back a week to rank
DEFAULT_RANK_BY = "last_game"  # each player's own latest game; "season" = season-to-date totals
REQUEST_TIMEOUT = 20

SCORING_FIELD = {
    "ppr": "pts_ppr",
    "half_ppr": "pts_half_ppr",
    "std": "pts_std",
}

# Sleeper's offensive-snap field. Kept as a list because it's the one stat
# here that isn't guaranteed to be populated for every historical season --
# if none of these keys is present for a game, that game has no defined
# points-per-snap and is drawn as a gap rather than a zero.
SNAP_FIELDS = ["off_snp"]

POSITION_ORDER = ["QB", "RB", "WR", "TE"]  # display order for the filter bar; anything else appended after


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-trends/4.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_state():
    """Current NFL season/week per Sleeper. Note this can legitimately point
    at a RECENTLY COMPLETED season during the off-season (Sleeper doesn't
    roll `season` over to the new one until it actually starts) -- the
    ranking-window search treats that as "use the most recent season with
    real data" rather than as an error."""
    return fetch_json(f"{SLEEPER_BASE}/state/nfl")


def get_players_directory():
    """Full player_id -> {full_name, position, team, ...} directory. Large
    (~5MB) -- Sleeper's docs ask this be fetched at most once a day; a
    weekly cron run comfortably respects that on its own."""
    return fetch_json(f"{SLEEPER_BASE}/players/nfl")


_WEEK_STATS_CACHE = {}


def get_week_stats(season, week):
    """One week's stats for every NFL player who recorded a stat line that
    week: player_id -> {pts_ppr, off_snp, rec, rush_att, ...}. Returns {}
    (not None) if the endpoint has nothing for that week yet (a future week,
    or a week beyond a shorter historical season), so callers can treat that
    the same as "no data" rather than an error. Cached in-process per
    (season, week) since the ranking pass and the trend build ask for
    overlapping weeks."""
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
    at `anchor_season` inclusive."""
    anchor = int(anchor_season)
    return [str(anchor - i) for i in range(seasons_back - 1, -1, -1)]


def load_season_weeks(season, week_cap=MAX_WEEKS_PER_SEASON):
    """{week: stats_dict} for every week of `season` that has real data.
    Weeks that don't exist yet come back empty and are omitted. Everything
    is cached, so calling this repeatedly for the same season across the
    ranking pass and the trend build costs exactly one set of fetches."""
    out = {}
    for w in range(1, week_cap + 1):
        wk = get_week_stats(season, w)
        if wk:
            out[w] = wk
    return out


def snaps_of(stats):
    """Offensive snaps for one game, or None when Sleeper didn't populate the
    field for that season/player. None is meaningfully different from 0 here:
    0 snaps means "dressed but never on the field", while None means "we
    don't know", and only the former is a real data point."""
    for field in SNAP_FIELDS:
        v = stats.get(field)
        if v is not None:
            return int(v)
    return None


# ---------------------------------------------------------------------------
# Player selection
# ---------------------------------------------------------------------------

def count_scorers(week_stats, scoring_field):
    """How many players have a real points value in one week's payload. Used
    to tell a finished week from one that's still being played."""
    return sum(1 for s in week_stats.values() if s.get(scoring_field) is not None)


def pick_ranking_window(seasons, scoring_field, live_season=None, live_week=None):
    """Returns (ranking_season, season_weeks, rank_week):

      - `ranking_season` is the season the pool is ranked out of: the live
        season when one is genuinely in progress and has any data, otherwise
        the most recent season that has data at all.
      - `season_weeks` is that season's {week: stats} map (cached, so the
        trend build re-uses it).
      - `rank_week` is the MOST RECENT COMPLETED week in it -- the newest
        week carrying at least MIN_WEEK_PLAYERS stat lines. A run that lands
        mid-slate sees a thin partial week and steps back to the last
        finished one, so the pool is never built from the handful of players
        whose game has already kicked off. If no week clears the bar, it
        falls back to the newest week with anything in it.

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
    caption a stale number rather than passing it off as current.

    `rank_by="season"` instead totals every week for a pool that barely
    moves run to run.

    Ties break on recency of the ranking game (a 20-point week 9 outranks a
    20-point week 3), then games played, then name -- so ordering is stable
    and never rewards staleness. Costs zero extra HTTP."""
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
    players directory, returning the same shape rank_pool() does so the rest
    of the pipeline doesn't care which path produced the list. A name with no
    match is skipped with a warning rather than silently dropped, so a typo
    or a retirement doesn't fail the whole run."""
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
# Trend data
# ---------------------------------------------------------------------------

G_SEASON, G_WEEK, G_PTS, G_SNAPS, G_TOUCHES = 0, 1, 2, 3, 4


def build_trend_series(pool, seasons, scoring_field):
    """For each player in the pool, a chronological (oldest -> newest) list
    of [season, week, pts, snaps, touches] across every season in `seasons`
    that has real data for them.

    A week is included only if the player actually has a stat line -- a
    rookie, or a player who entered the league partway through this window,
    simply starts wherever their real data starts rather than being padded
    with zeros for seasons before they existed. `snaps` is offensive snaps
    (None when Sleeper has no snap field for that game), and `touches` is
    rush attempts + receptions, carried along for the hover readout.

    This one list feeds all three panels: the small multiples plot it
    directly, and the scatter aggregates whichever slice of it the week
    slider selects. Nothing derived (points per snap, per-game averages) is
    stored -- all of it is arithmetic the browser can do on the fly, and
    shipping it would multiply the payload for no new information.

    Reuses get_week_stats()'s cache, so nothing here re-fetches."""
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
                touches = (stats.get("rush_att") or 0) + (stats.get("rec") or 0)
                series[entry["pid"]].append(
                    [season, week, round(float(pts), 1), snaps_of(stats), touches]
                )
    return series


def snap_coverage(trend_series):
    """(games_with_snaps, games_total) across the whole report. Sleeper's
    snap field isn't guaranteed for older seasons, and both the
    points-per-snap grid and the scatter's x-axis are only as good as this
    ratio -- so it gets printed to the run log and stated on the page rather
    than leaving a half-empty chart unexplained."""
    total = with_snaps = 0
    for games in trend_series.values():
        for g in games:
            total += 1
            if g[G_SNAPS]:
                with_snaps += 1
    return with_snaps, total


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE_CSS = """
  :root{
    --bg:#0B0E14; --panel:#111621; --line:#1E2633;
    --ink:#E8E6DE; --dim:#6B7280; --cyan:#4FD8E8; --amber:#D9A441;
    --violet:#A78BFA;
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
  .sub{ color:var(--dim); font-size:12px; margin-bottom:6px; line-height:1.55; max-width:780px; }
  .sub b{ color:var(--ink); }
  .status{ color:var(--dim); font-size:11px; margin-bottom:4px; line-height:1.6; }
  .status b{ color:var(--ink); }
  .warn{ color:var(--amber); }
  .section{ margin-top:36px; }
  .section-head{ margin-bottom:10px; }
  .filter-bar{ display:flex; gap:8px; margin:10px 0 6px; flex-wrap:wrap; align-items:center; }
  .filter-bar-label{ font-size:10px; color:var(--dim); letter-spacing:.08em; text-transform:uppercase; margin-right:2px; }
  .filter-btn{
    background:var(--panel); border:1px solid var(--line); color:var(--dim);
    font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.03em;
    padding:7px 13px; border-radius:6px; cursor:pointer; transition:all .15s ease;
  }
  .filter-btn:hover{ color:var(--ink); border-color:#39424f; }
  .filter-btn.active{ color:#0B0E14; background:var(--cyan); border-color:var(--cyan); font-weight:600; }
  .filter-btn.small{ padding:5px 10px; font-size:10.5px; }
  .filter-count{ font-size:10.5px; color:var(--dim); margin-left:4px; }
  .checkbox-btn{
    display:inline-flex; align-items:center; gap:6px; background:var(--panel); border:1px solid var(--line);
    color:var(--dim); font-family:'IBM Plex Mono', monospace; font-size:11px; padding:6px 12px;
    border-radius:6px; cursor:pointer; user-select:none;
  }
  .checkbox-btn input{ accent-color:var(--cyan); cursor:pointer; }
  .sticky-controls{
    position:sticky; top:0; z-index:20; background:linear-gradient(180deg, var(--bg) 86%, rgba(11,14,20,0));
    padding-bottom:10px;
  }
  /* player search + pins */
  .search-wrap{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  #playerSearch{
    background:var(--panel); border:1px solid var(--line); color:var(--ink);
    font-family:'IBM Plex Mono', monospace; font-size:11.5px; padding:7px 11px; border-radius:6px;
    min-width:260px; outline:none;
  }
  #playerSearch:focus{ border-color:var(--cyan); }
  #playerSearch::placeholder{ color:var(--dim); }
  .chip{
    display:inline-flex; align-items:center; gap:6px; background:rgba(79,216,232,.12);
    border:1px solid var(--cyan); color:var(--cyan); font-size:10.5px; padding:5px 8px 5px 10px;
    border-radius:999px;
  }
  .chip button{
    background:none; border:none; color:var(--cyan); cursor:pointer; font-size:13px; line-height:1;
    padding:0 2px; font-family:inherit;
  }
  .chip button:hover{ color:var(--ink); }
  .hint{ font-size:10px; color:var(--dim); }
  /* scatter */
  .scatter-row{ display:flex; gap:0; flex-wrap:wrap; border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .scatter-stage{ flex:1 1 640px; min-width:0; background:var(--panel); padding:14px 16px 10px; }
  .scatter-stage svg{ display:block; width:100%; height:auto; }
  aside.scatter-side{
    width:300px; flex-shrink:0; background:#0d1119; border-left:1px solid var(--line);
    padding:16px 16px 18px; display:flex; flex-direction:column; gap:12px;
  }
  .legend-title{ font-size:10px; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); }
  .shape-legend{ display:flex; flex-direction:column; gap:6px; }
  .shape-legend div{ display:flex; align-items:center; gap:8px; font-size:11px; color:var(--ink); }
  .rank-table{ display:flex; flex-direction:column; max-height:44vh; overflow-y:auto; }
  .rank-table::-webkit-scrollbar{ width:6px; }
  .rank-table::-webkit-scrollbar-thumb{ background:var(--line); border-radius:3px; }
  .rank-row{
    display:grid; grid-template-columns:1fr auto auto; gap:8px; font-size:10.5px; padding:5px 0;
    border-bottom:1px solid var(--line); align-items:baseline;
  }
  .rank-row .nm{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .rank-row .v{ color:var(--cyan); font-weight:600; }
  .rank-row .s{ color:var(--dim); }
  .rank-head{ color:var(--dim); border-bottom:1px solid var(--line); }
  .slider-wrap{ display:flex; flex-direction:column; gap:6px; padding:6px 2px 0; }
  .slider-row{ display:flex; align-items:center; gap:10px; }
  .slider-row span.lbl{ font-size:10px; color:var(--dim); width:4ch; }
  input[type=range]{
    flex:1; -webkit-appearance:none; appearance:none; height:3px; background:var(--line);
    border-radius:2px; outline:none;
  }
  input[type=range]::-webkit-slider-thumb{
    -webkit-appearance:none; width:14px; height:14px; border-radius:50%; background:var(--cyan);
    cursor:pointer; border:2px solid var(--bg);
  }
  input[type=range]::-moz-range-thumb{
    width:14px; height:14px; border-radius:50%; background:var(--cyan); cursor:pointer;
    border:2px solid var(--bg);
  }
  .week-label{ font-size:11.5px; color:var(--cyan); text-align:center; }
  .week-label span{ color:var(--dim); }
  select{
    background:var(--panel); border:1px solid var(--line); color:var(--ink);
    font-family:'IBM Plex Mono', monospace; font-size:11px; padding:6px 10px; border-radius:6px;
    cursor:pointer; outline:none;
  }
  /* small multiples */
  .trend-grid{ display:grid; grid-template-columns:repeat(auto-fill, minmax(430px, 1fr)); gap:14px; }
  .trend-card{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px 10px; }
  .trend-card-head{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:2px; gap:8px; }
  .trend-card-head .name{ font-size:12.5px; font-weight:600; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .trend-card-head .meta{ font-size:10px; color:var(--dim); white-space:nowrap; }
  .trend-card-head .stale{ color:var(--amber); }
  .card-stats{ font-size:10px; color:var(--dim); margin-bottom:4px; }
  .card-stats b{ color:var(--ink); font-weight:600; }
  .trend-card svg{ display:block; width:100%; height:auto; }
  .trend-readout{
    font-size:10.5px; color:var(--dim); border-top:1px solid var(--line); margin-top:6px; padding-top:7px;
    min-height:26px;
  }
  .trend-readout.has-data{ color:var(--ink); }
  .trend-readout b{ color:var(--ink); }
  .empty-note{ color:var(--dim); font-size:12px; padding:26px 4px; }
  /* chart internals -- text and rules never intercept a hover meant for a mark */
  svg text, svg line.grid, svg line.axline, svg line.sbound{ pointer-events:none; }
  .ax{ fill:#6B7280; font-size:8.5px; font-family:'IBM Plex Mono', monospace; }
  .axtitle{ fill:#9CA3AF; font-size:10px; letter-spacing:.06em; font-family:'IBM Plex Mono', monospace; }
  .axline{ stroke:rgba(30,38,51,1); stroke-width:1; }
  .grid{ stroke:rgba(107,114,128,0.16); stroke-width:1; }
  .sbound{ stroke:rgba(107,114,128,0.4); stroke-width:1; stroke-dasharray:2,3; }
  .avgline{ stroke:rgba(217,164,65,0.55); stroke-width:1; stroke-dasharray:4,3; }
  .avglabel{ fill:var(--amber); font-size:8.5px; font-family:'IBM Plex Mono', monospace; }
  .tl{ fill:none; stroke-width:1.2; stroke-opacity:.55; }
  .tb{ stroke-width:.8; stroke-opacity:.6; cursor:pointer; }
  .tb.last{ stroke-width:1.4; stroke-opacity:1; }
  .tb:hover{ fill:#E8E6DE; stroke:#E8E6DE; }
  .m-pts .tl{ stroke:var(--cyan); }
  .m-pts .tb{ fill:rgba(79,216,232,0.45); stroke:var(--cyan); }
  .m-pts .tb.last{ fill:var(--cyan); }
  .m-pps .tl{ stroke:var(--violet); }
  .m-pps .tb{ fill:rgba(167,139,250,0.45); stroke:var(--violet); }
  .m-pps .tb.last{ fill:var(--violet); }
  .ptlabel{ fill:var(--ink); font-size:9px; font-family:'IBM Plex Mono', monospace; }
  .peaklabel{ fill:#9CA3AF; font-size:8.5px; font-family:'IBM Plex Mono', monospace; }
  .empty{ fill:#6B7280; font-size:10.5px; font-family:'IBM Plex Mono', monospace; }
  /* scatter marks: ONE hue, position carried by shape (see module docstring) */
  .mk{ fill:rgba(79,216,232,0.30); stroke:var(--cyan); stroke-width:1.5; cursor:pointer;
       transition:transform .25s ease, opacity .2s ease; }
  .mk:hover, .mk.pinned{ fill:rgba(79,216,232,0.75); stroke:#E8E6DE; }
  .mklabel{ fill:var(--ink); font-size:9.5px; font-family:'IBM Plex Mono', monospace; }
  .medline{ stroke:rgba(107,114,128,0.45); stroke-width:1; stroke-dasharray:3,4; }
  .quad{ fill:#6B7280; font-size:9px; letter-spacing:.05em; font-family:'IBM Plex Mono', monospace; opacity:.75; }
  #scatterTip{
    position:fixed; pointer-events:none; z-index:60; background:#0d1119; border:1px solid var(--line);
    border-radius:6px; padding:8px 10px; font-size:10.5px; color:var(--ink); line-height:1.5;
    box-shadow:0 6px 20px rgba(0,0,0,.5); display:none; max-width:260px;
  }
  #scatterTip b{ color:var(--cyan); }
  #scatterTip .d{ color:var(--dim); }
"""

PAGE_JS = r"""
const TREND_GAMES = __TREND_GAMES__;
const POOL = __POOL__;
const SEASONS = __SEASONS__;
const TOP_N = __TOP_N__;
const SNAP_R = __SNAP_BOUNDS__;      // [min,max] snaps across every game -- small-multiple bubble scale
const GLOBAL_MAX = __GLOBAL_MAX__;   // {pts,pps} report-wide maxima for shared y-scaling
const RANK_SEASON = __RANK_SEASON__;
const RANK_WEEK = __RANK_WEEK__;

const G_SEASON = 0, G_WEEK = 1, G_PTS = 2, G_SNAPS = 3, G_TOUCHES = 4;
const BY_PID = {};
POOL.forEach(function(p){ BY_PID[p.pid] = p; });

// ---------------------------------------------------------------------------
// Shared filter state. One predicate drives all three panels, so what you see
// in the scatter is always the same set of players as the cards below it.
// ---------------------------------------------------------------------------
let currentPos = 'ALL';
let searchText = '';
const pinned = new Set();

function matchesSearch(p, q){
  return (p.n + ' ' + p.t + ' ' + p.p).toLowerCase().indexOf(q) !== -1;
}
// Precedence, strongest first:
//   1. PINS win outright. Once you have named a set of players, that IS the
//      set -- a position filter left over from earlier must not silently
//      remove one of them.
//   2. SEARCH narrows within the position filter, and lifts the top-N cap:
//      asking for someone by name and not getting them because they rank
//      214th would be the wrong answer.
//   3. Otherwise the position rule applies, with the cap only on ALL.
function passesFilter(p){
  if (pinned.size) return pinned.has(p.pid);
  if (currentPos !== 'ALL' && p.p !== currentPos) return false;
  if (searchText) return matchesSearch(p, searchText);
  return currentPos !== 'ALL' || p.k <= TOP_N;
}
function visiblePlayers(){ return POOL.filter(passesFilter); }

function esc(s){
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function mean(a){ return a.reduce(function(x, y){ return x + y; }, 0) / a.length; }
function median(a){
  if (!a.length) return 0;
  const s = a.slice().sort(function(x, y){ return x - y; });
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
function niceTicks(hi, n){
  const out = [];
  for (let i = 0; i <= n; i++) out.push(hi * i / n);
  return out;
}

// ===========================================================================
// PANEL 1 -- scatter: points/game (y) vs snaps/game (x) over a week window
// ===========================================================================
const SC = { W: 960, H: 520, ML: 62, MR: 22, MT: 20, MB: 52 };
const MK_MIN = 4, MK_MAX = 17;   // mark "radius" -- area encodes season points/game
let scSeason = RANK_SEASON;
let wkFrom = 1, wkTo = RANK_WEEK || 1;

function weeksAvailable(season){
  let hi = 1;
  for (const pid in TREND_GAMES){
    const gs = TREND_GAMES[pid];
    for (let i = 0; i < gs.length; i++){
      if (gs[i][G_SEASON] === season && gs[i][G_WEEK] > hi) hi = gs[i][G_WEEK];
    }
  }
  return hi;
}

// Season points/game across EVERY game of the selected season -- deliberately
// not the slider window, so the bubble stays a fixed reference you can read a
// player's overall level from while the window slides underneath it.
function seasonPPG(pid, season){
  const gs = TREND_GAMES[pid] || [];
  const pts = [];
  for (let i = 0; i < gs.length; i++) if (gs[i][G_SEASON] === season) pts.push(gs[i][G_PTS]);
  return pts.length ? mean(pts) : null;
}

// Aggregate one player over the selected week window. snaps/game averages only
// over games that HAVE a snap count -- averaging a missing value as zero would
// drag a player left for a data gap rather than for real playing time.
function aggregate(pid, season, from, to){
  const gs = TREND_GAMES[pid] || [];
  const pts = [], snaps = [];
  for (let i = 0; i < gs.length; i++){
    const g = gs[i];
    if (g[G_SEASON] !== season || g[G_WEEK] < from || g[G_WEEK] > to) continue;
    pts.push(g[G_PTS]);
    if (g[G_SNAPS] !== null && g[G_SNAPS] !== undefined) snaps.push(g[G_SNAPS]);
  }
  if (!pts.length || !snaps.length) return null;
  return { games: pts.length, ppg: mean(pts), spg: mean(snaps), snapGames: snaps.length };
}

// Position identity is SHAPE, not colour -- four categorical hues cannot clear
// the all-pairs colour-vision floors a scatter needs (see module docstring).
// All four glyphs are drawn to equal area so size stays comparable across them.
function markSvg(pos, cx, cy, r, pid, isPinned){
  const cls = 'mk' + (isPinned ? ' pinned' : '');
  const tail = ' data-pid="' + esc(pid) + '"/>';
  if (pos === 'QB'){
    return '<circle class="' + cls + '" cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
      '" r="' + r.toFixed(1) + '"' + tail;
  }
  if (pos === 'RB'){
    const s = r * 1.772, h = s / 2;   // square of equal area
    return '<rect class="' + cls + '" x="' + (cx - h).toFixed(1) + '" y="' + (cy - h).toFixed(1) +
      '" width="' + s.toFixed(1) + '" height="' + s.toFixed(1) + '" rx="1.5"' + tail;
  }
  if (pos === 'WR'){
    const a = r * 2.69, hh = a * 0.5774;   // equilateral triangle of equal area
    return '<polygon class="' + cls + '" points="' +
      cx.toFixed(1) + ',' + (cy - hh).toFixed(1) + ' ' +
      (cx - a / 2).toFixed(1) + ',' + (cy + hh / 2).toFixed(1) + ' ' +
      (cx + a / 2).toFixed(1) + ',' + (cy + hh / 2).toFixed(1) + '"' + tail;
  }
  const h2 = r * 2.507 / 2;   // TE: diamond of equal area
  return '<polygon class="' + cls + '" points="' +
    cx.toFixed(1) + ',' + (cy - h2).toFixed(1) + ' ' +
    (cx + h2).toFixed(1) + ',' + cy.toFixed(1) + ' ' +
    cx.toFixed(1) + ',' + (cy + h2).toFixed(1) + ' ' +
    (cx - h2).toFixed(1) + ',' + cy.toFixed(1) + '"' + tail;
}

function renderScatter(){
  const svg = document.getElementById('scatterSvg');
  const players = visiblePlayers();
  const rows = [];
  let noSnapCount = 0;

  players.forEach(function(p){
    const a = aggregate(p.pid, scSeason, wkFrom, wkTo);
    if (!a){ noSnapCount++; return; }
    rows.push({ p: p, ppg: a.ppg, spg: a.spg, games: a.games,
                ppg_season: seasonPPG(p.pid, scSeason) });
  });

  document.getElementById('scatterCount').textContent =
    rows.length + ' plotted' + (noSnapCount ? ' · ' + noSnapCount + ' with no games/snaps in this window' : '');

  if (!rows.length){
    svg.innerHTML = '<text class="empty" x="' + (SC.W / 2) + '" y="' + (SC.H / 2) +
      '" text-anchor="middle">No players with snap data in ' + esc(scSeason) +
      ' weeks ' + wkFrom + '-' + wkTo + '</text>';
    document.getElementById('rankTable').innerHTML = '';
    return;
  }

  const PW = SC.W - SC.ML - SC.MR, PH = SC.H - SC.MT - SC.MB;
  const xHi = Math.max.apply(null, rows.map(function(r){ return r.spg; })) * 1.08 || 1;
  const yHi = Math.max.apply(null, rows.map(function(r){ return r.ppg; })) * 1.10 || 1;
  const xOf = function(v){ return SC.ML + (v / xHi) * PW; };
  const yOf = function(v){ return SC.MT + (1 - v / yHi) * PH; };

  const ppgs = rows.map(function(r){ return r.ppg_season; }).filter(function(v){ return v !== null; });
  const bLo = ppgs.length ? Math.min.apply(null, ppgs) : 0;
  const bHi = ppgs.length ? Math.max.apply(null, ppgs) : 1;
  const bSpan = (bHi - bLo) || 1;
  // Area-proportional, not radius-proportional: radius-scaling a bubble makes a
  // 2x value look 4x bigger.
  const rOf = function(v){
    if (v === null || v === undefined) return MK_MIN;
    const f = Math.max(0, Math.min(1, (v - bLo) / bSpan));
    return Math.sqrt(MK_MIN * MK_MIN + f * (MK_MAX * MK_MAX - MK_MIN * MK_MIN));
  };

  const parts = [];

  // Axes: five labelled ticks a side, plus axis titles with units.
  niceTicks(yHi, 5).forEach(function(t){
    const y = yOf(t);
    parts.push('<line class="grid" x1="' + SC.ML + '" y1="' + y.toFixed(1) + '" x2="' + (SC.W - SC.MR) + '" y2="' + y.toFixed(1) + '"/>');
    parts.push('<text class="ax" x="' + (SC.ML - 7) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + t.toFixed(1) + '</text>');
  });
  niceTicks(xHi, 6).forEach(function(t){
    const x = xOf(t);
    parts.push('<line class="grid" x1="' + x.toFixed(1) + '" y1="' + SC.MT + '" x2="' + x.toFixed(1) + '" y2="' + (SC.H - SC.MB) + '"/>');
    parts.push('<text class="ax" x="' + x.toFixed(1) + '" y="' + (SC.H - SC.MB + 14) + '" text-anchor="middle">' + t.toFixed(0) + '</text>');
  });
  parts.push('<line class="axline" x1="' + SC.ML + '" y1="' + SC.MT + '" x2="' + SC.ML + '" y2="' + (SC.H - SC.MB) + '"/>');
  parts.push('<line class="axline" x1="' + SC.ML + '" y1="' + (SC.H - SC.MB) + '" x2="' + (SC.W - SC.MR) + '" y2="' + (SC.H - SC.MB) + '"/>');
  parts.push('<text class="axtitle" x="' + (SC.ML + PW / 2) + '" y="' + (SC.H - 12) + '" text-anchor="middle">SNAPS PER GAME &rarr;</text>');
  parts.push('<text class="axtitle" transform="translate(15,' + (SC.MT + PH / 2) + ') rotate(-90)" text-anchor="middle">FANTASY POINTS PER GAME &rarr;</text>');

  // Median crosshairs turn the plot into four readable quadrants.
  const mx = median(rows.map(function(r){ return r.spg; }));
  const my = median(rows.map(function(r){ return r.ppg; }));
  parts.push('<line class="medline" x1="' + xOf(mx).toFixed(1) + '" y1="' + SC.MT + '" x2="' + xOf(mx).toFixed(1) + '" y2="' + (SC.H - SC.MB) + '"/>');
  parts.push('<line class="medline" x1="' + SC.ML + '" y1="' + yOf(my).toFixed(1) + '" x2="' + (SC.W - SC.MR) + '" y2="' + yOf(my).toFixed(1) + '"/>');
  parts.push('<text class="ax" x="' + (xOf(mx) + 4).toFixed(1) + '" y="' + (SC.MT + 10) + '">median ' + mx.toFixed(0) + ' snaps</text>');
  parts.push('<text class="ax" x="' + (SC.W - SC.MR - 4) + '" y="' + (yOf(my) - 4).toFixed(1) + '" text-anchor="end">median ' + my.toFixed(1) + ' pt</text>');
  parts.push('<text class="quad" x="' + (SC.ML + 8) + '" y="' + (SC.MT + 24) + '">EFFICIENT &middot; few snaps, high points</text>');
  parts.push('<text class="quad" x="' + (SC.W - SC.MR - 8) + '" y="' + (SC.MT + 24) + '" text-anchor="end">WORKHORSE &middot; heavy snaps, high points</text>');
  parts.push('<text class="quad" x="' + (SC.ML + 8) + '" y="' + (SC.H - SC.MB - 8) + '">LOW USAGE</text>');
  parts.push('<text class="quad" x="' + (SC.W - SC.MR - 8) + '" y="' + (SC.H - SC.MB - 8) + '" text-anchor="end">HIGH SNAPS, LOW RETURN</text>');

  // Biggest marks first so a small one is never buried under a large one.
  rows.sort(function(a, b){ return rOf(b.ppg_season) - rOf(a.ppg_season); });
  rows.forEach(function(r){
    parts.push(markSvg(r.p.p, xOf(r.spg), yOf(r.ppg), rOf(r.ppg_season), r.p.pid, pinned.has(r.p.pid)));
  });

  // Direct labels on the leaders only -- a name on every mark is unreadable.
  // Placed greedily from the top down, and a label that would overlap one
  // already placed (or the quadrant captions) is simply dropped: two names
  // printed on top of each other are worth less than one you can read. The
  // dropped ones are all still in the side table and on hover.
  const taken = [
    { x: SC.ML, y: SC.MT + 14, w: 260, h: 14 },                       // EFFICIENT caption
    { x: SC.W - SC.MR - 260, y: SC.MT + 14, w: 260, h: 14 },          // WORKHORSE caption
  ];
  function fits(box){
    for (let i = 0; i < taken.length; i++){
      const t = taken[i];
      if (box.x < t.x + t.w && box.x + box.w > t.x && box.y < t.y + t.h && box.y + box.h > t.y) return false;
    }
    return true;
  }
  const candidates = rows.slice().sort(function(a, b){ return b.ppg - a.ppg; }).slice(0, 26);
  let placed = 0;
  candidates.forEach(function(r){
    if (placed >= 14) return;
    const x = xOf(r.spg), y = yOf(r.ppg), rad = rOf(r.ppg_season);
    const parts_n = r.p.n.split(' ');
    const short = parts_n.length > 1 ? parts_n[0][0] + '. ' + parts_n.slice(1).join(' ') : r.p.n;
    const w = short.length * 5.9, ly = y - rad - 5;
    const box = { x: x - w / 2, y: ly - 9, w: w, h: 12 };
    if (!fits(box)) return;
    taken.push(box);
    placed++;
    parts.push('<text class="mklabel" x="' + x.toFixed(1) + '" y="' + ly.toFixed(1) +
      '" text-anchor="middle">' + esc(short) + '</text>');
  });

  svg.innerHTML = parts.join('');

  // Side table -- doubles as the non-visual read of the same numbers.
  const table = rows.slice().sort(function(a, b){ return b.ppg - a.ppg; });
  document.getElementById('rankTable').innerHTML =
    '<div class="rank-row rank-head"><span>Player</span><span>pt/g</span><span>snap/g</span></div>' +
    table.map(function(r){
      return '<div class="rank-row" data-pid="' + esc(r.p.pid) + '">' +
        '<span class="nm">' + esc(r.p.n) + ' <span class="s">' + r.p.p + '</span></span>' +
        '<span class="v">' + r.ppg.toFixed(1) + '</span>' +
        '<span class="s">' + r.spg.toFixed(0) + '</span></div>';
    }).join('');
}

// --- scatter hover tooltip ---
const tip = document.getElementById('scatterTip');
document.getElementById('scatterSvg').addEventListener('mousemove', function(e){
  const m = e.target.closest('.mk');
  if (!m){ tip.style.display = 'none'; return; }
  const p = BY_PID[m.getAttribute('data-pid')];
  const a = aggregate(p.pid, scSeason, wkFrom, wkTo);
  if (!a) return;
  const sp = seasonPPG(p.pid, scSeason);
  tip.innerHTML = '<b>' + esc(p.n) + '</b> <span class="d">' + p.p + ' · ' + p.t + ' · #' + p.k + '</span><br>' +
    a.ppg.toFixed(1) + ' pt/game · ' + a.spg.toFixed(1) + ' snaps/game<br>' +
    '<span class="d">' + (a.ppg / a.spg).toFixed(3) + ' pt/snap · ' + a.games + ' games in wk ' +
    wkFrom + '-' + wkTo + '</span><br>' +
    '<span class="d">' + scSeason + ' season: ' + (sp === null ? 'n/a' : sp.toFixed(1) + ' pt/game') +
    ' (bubble size)</span>';
  tip.style.display = 'block';
  tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 275) + 'px';
  tip.style.top = (e.clientY + 14) + 'px';
});
document.getElementById('scatterSvg').addEventListener('mouseleave', function(){ tip.style.display = 'none'; });
// Clicking a mark pins that player across every panel.
document.getElementById('scatterSvg').addEventListener('click', function(e){
  const m = e.target.closest('.mk');
  if (m) togglePin(m.getAttribute('data-pid'));
});
document.getElementById('rankTable').addEventListener('click', function(e){
  const row = e.target.closest('.rank-row[data-pid]');
  if (row) togglePin(row.getAttribute('data-pid'));
});

// --- week window controls ---
function syncWeekUI(){
  const maxW = weeksAvailable(scSeason);
  const f = document.getElementById('wkFrom'), t = document.getElementById('wkTo');
  f.max = maxW; t.max = maxW;
  wkTo = Math.min(wkTo, maxW); wkFrom = Math.min(wkFrom, wkTo);
  f.value = wkFrom; t.value = wkTo;
  const n = wkTo - wkFrom + 1;
  document.getElementById('weekLabel').innerHTML =
    'Weeks <b>' + wkFrom + '</b>–<b>' + wkTo + '</b> <span>(' + n + ' week' + (n === 1 ? '' : 's') +
    ' of ' + esc(scSeason) + ')</span>';
}
function setWindow(from, to){
  const maxW = weeksAvailable(scSeason);
  wkFrom = Math.max(1, Math.min(from, maxW));
  wkTo = Math.max(wkFrom, Math.min(to, maxW));
  syncWeekUI();
  renderScatter();
}
document.getElementById('wkFrom').addEventListener('input', function(){
  setWindow(+this.value, Math.max(+this.value, wkTo));
});
document.getElementById('wkTo').addEventListener('input', function(){
  setWindow(Math.min(wkFrom, +this.value), +this.value);
});
document.getElementById('weekPresets').addEventListener('click', function(e){
  const b = e.target.closest('.filter-btn');
  if (!b) return;
  const maxW = weeksAvailable(scSeason);
  const n = b.getAttribute('data-last');
  setWindow(n === 'all' ? 1 : Math.max(1, maxW - (+n) + 1), maxW);
});
document.getElementById('scatterSeason').addEventListener('change', function(){
  scSeason = this.value;
  const maxW = weeksAvailable(scSeason);
  setWindow(1, maxW);
});

// ===========================================================================
// PANELS 2 & 3 -- small multiples
// ===========================================================================
const GEO = { W: 440, H: 170, ML: 34, MR: 10, MT: 12, MB: 20 };
const R_MIN = 1.8, R_MAX = 5.2;
const scaleMode = { pts: 'auto', pps: 'auto' };

function valueOf(g, metric){
  if (metric === 'pts') return g[G_PTS];
  if (!g[G_SNAPS]) return null;
  return g[G_PTS] / g[G_SNAPS];
}
function fmtVal(v, metric){
  if (v === null || v === undefined) return 'n/a';
  return metric === 'pts' ? v.toFixed(1) : v.toFixed(3);
}
// Every tick on one axis uses the SAME precision -- a "0 / 7.0 / 14" axis reads
// as three different quantities.
function tickDecimals(hi, metric){
  if (metric === 'pts') return hi >= 10 ? 0 : 1;
  return hi >= 1 ? 2 : 3;
}
function radiusOf(snaps){
  if (!snaps) return R_MIN;
  const span = (SNAP_R[1] - SNAP_R[0]) || 1;
  const f = Math.max(0, Math.min(1, (snaps - SNAP_R[0]) / span));
  return R_MIN + f * (R_MAX - R_MIN);
}

function renderChart(svg, gamesIn, metric){
  const W = GEO.W, H = GEO.H, ML = GEO.ML, MR = GEO.MR, MT = GEO.MT, MB = GEO.MB;
  const PW = W - ML - MR, PH = H - MT - MB;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  const games = gamesIn;

  const vals = games.map(function(g){ return valueOf(g, metric); });
  const real = vals.filter(function(v){ return v !== null && v !== undefined; });
  if (real.length < 2){
    svg.innerHTML = '<text class="empty" x="' + (W / 2) + '" y="' + (H / 2) + '" text-anchor="middle">' +
      (metric === 'pps' ? 'no snap data' : 'no data yet') + '</text>';
    return;
  }

  // Both quantities floor at zero, so the axis starts at zero -- starting at
  // the player's own minimum turns ordinary noise into a cliff.
  const lo = 0;
  const hi = scaleMode[metric] === 'shared' ? GLOBAL_MAX[metric] : Math.max.apply(null, real);
  const span = (hi - lo) || 1;
  const xOf = function(i){ return ML + (games.length === 1 ? PW / 2 : (i / (games.length - 1)) * PW); };
  const yOf = function(v){ return MT + (1 - (v - lo) / span) * PH; };

  const parts = [];
  const dec = tickDecimals(hi, metric);
  niceTicks(hi, 4).forEach(function(t){
    const y = yOf(t);
    parts.push('<line class="grid" x1="' + ML + '" y1="' + y.toFixed(1) + '" x2="' + (W - MR) + '" y2="' + y.toFixed(1) + '"/>');
    parts.push('<text class="ax" x="' + (ML - 5) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + t.toFixed(dec) + '</text>');
  });
  parts.push('<line class="axline" x1="' + ML + '" y1="' + MT + '" x2="' + ML + '" y2="' + (H - MB) + '"/>');

  // Career average across the plotted games, drawn and labelled -- the single
  // most useful reference line for "is this week good for HIM".
  const avg = mean(real);
  parts.push('<line class="avgline" x1="' + ML + '" y1="' + yOf(avg).toFixed(1) + '" x2="' + (W - MR) + '" y2="' + yOf(avg).toFixed(1) + '"/>');
  parts.push('<text class="avglabel" x="' + (W - MR - 1) + '" y="' + (yOf(avg) - 3).toFixed(1) +
    '" text-anchor="end">avg ' + fmtVal(avg, metric) + '</text>');

  let last = games[0][G_SEASON];
  parts.push('<text class="ax" x="' + (ML + 2) + '" y="' + (H - 5) + '">' + esc(last) + '</text>');
  for (let i = 1; i < games.length; i++){
    if (games[i][G_SEASON] !== last){
      const gx = (xOf(i - 1) + xOf(i)) / 2;
      parts.push('<line class="sbound" x1="' + gx.toFixed(1) + '" y1="' + MT + '" x2="' + gx.toFixed(1) + '" y2="' + (H - MB) + '"/>');
      parts.push('<text class="ax" x="' + (gx + 3).toFixed(1) + '" y="' + (H - 5) + '">' + esc(games[i][G_SEASON]) + '</text>');
      last = games[i][G_SEASON];
    }
  }

  // One <g> per season for the checkbox toggle; the line breaks wherever the
  // value is null so a stretch with no snap data reads as a gap rather than
  // being bridged by a segment implying data we don't have.
  let i = 0;
  const lastRealIdx = vals.reduce(function(acc, v, idx){ return (v === null || v === undefined) ? acc : idx; }, -1);
  let peakIdx = -1;
  vals.forEach(function(v, idx){ if (v !== null && v !== undefined && (peakIdx < 0 || v > vals[peakIdx])) peakIdx = idx; });

  while (i < games.length){
    const season = games[i][G_SEASON];
    let j = i;
    while (j < games.length && games[j][G_SEASON] === season) j++;
    const g = [];
    let run = [];
    for (let k = i; k <= j; k++){
      const v = (k < j) ? vals[k] : null;
      if (v === null || v === undefined){
        if (run.length > 1){
          g.push('<path class="tl" d="M ' + run.map(function(p){ return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' L ') + '"/>');
        }
        run = [];
      } else {
        run.push([xOf(k), yOf(v)]);
      }
    }
    for (let k = i; k < j; k++){
      const v = vals[k];
      if (v === null || v === undefined) continue;
      g.push('<circle class="tb' + (k === lastRealIdx ? ' last' : '') + '" cx="' + xOf(k).toFixed(1) +
        '" cy="' + yOf(v).toFixed(1) + '" r="' + radiusOf(games[k][G_SNAPS]).toFixed(2) + '" data-i="' + k + '"/>');
    }
    parts.push('<g data-season="' + esc(season) + '">' + g.join('') + '</g>');
    i = j;
  }

  // Called-out values: the peak game and the latest game. The peak label is
  // dropped when it would sit on top of the latest one -- two overlapping
  // numbers are worse than one.
  const labelsWouldCollide = peakIdx >= 0 && lastRealIdx >= 0 &&
    Math.abs(xOf(peakIdx) - xOf(lastRealIdx)) < 46 &&
    Math.abs(yOf(vals[peakIdx]) - yOf(vals[lastRealIdx])) < 12;
  if (peakIdx >= 0 && peakIdx !== lastRealIdx && !labelsWouldCollide){
    parts.push('<text class="peaklabel" x="' + xOf(peakIdx).toFixed(1) + '" y="' +
      Math.max(9, yOf(vals[peakIdx]) - R_MAX - 4).toFixed(1) + '" text-anchor="middle">peak ' +
      fmtVal(vals[peakIdx], metric) + '</text>');
  }
  if (lastRealIdx >= 0){
    parts.push('<text class="ptlabel" x="' + xOf(lastRealIdx).toFixed(1) + '" y="' +
      Math.max(9, yOf(vals[lastRealIdx]) - R_MAX - 4).toFixed(1) + '" text-anchor="end">' +
      fmtVal(vals[lastRealIdx], metric) + '</text>');
  }

  svg.innerHTML = parts.join('');
  applySeasonToggleTo(svg);
}

const drawn = new WeakSet();
function drawCard(card){
  renderChart(card.querySelector('svg'), TREND_GAMES[card.getAttribute('data-pid')] || [],
              card.getAttribute('data-metric'));
  drawn.add(card);
}
const observer = ('IntersectionObserver' in window) ? new IntersectionObserver(function(entries){
  entries.forEach(function(e){ if (e.isIntersecting && !drawn.has(e.target)) drawCard(e.target); });
}, { rootMargin: '300px' }) : null;

function observeAll(){
  document.querySelectorAll('.trend-card').forEach(function(c){
    if (observer) observer.observe(c); else drawCard(c);
  });
}
function redrawMetric(metric){
  document.querySelectorAll('.trend-card[data-metric="' + metric + '"]').forEach(function(c){
    if (drawn.has(c)) drawCard(c);
  });
}

function checkedSeasons(){
  const s = new Set();
  document.querySelectorAll('.season-cb:checked').forEach(function(cb){ s.add(cb.value); });
  return s;
}
function applySeasonToggleTo(svg){
  const on = checkedSeasons();
  svg.querySelectorAll('g[data-season]').forEach(function(g){
    g.style.display = on.has(g.getAttribute('data-season')) ? '' : 'none';
  });
}
function applySeasonToggle(){ document.querySelectorAll('.trend-card svg').forEach(applySeasonToggleTo); }

function showStats(circle){
  const card = circle.closest('.trend-card');
  const games = TREND_GAMES[card.getAttribute('data-pid')];
  if (!games) return;
  const g = games[+circle.getAttribute('data-i')];
  if (!g) return;
  const pps = g[G_SNAPS] ? (g[G_PTS] / g[G_SNAPS]) : null;
  const el = card.querySelector('.trend-readout');
  el.innerHTML = '<b>' + g[G_SEASON] + ' wk' + g[G_WEEK] + ':</b> ' + g[G_PTS].toFixed(1) + ' pt · ' +
    (g[G_SNAPS] === null ? 'snaps n/a' : g[G_SNAPS] + ' snaps') + ' · ' +
    (pps === null ? 'pt/snap n/a' : pps.toFixed(3) + ' pt/snap') + ' · ' + g[G_TOUCHES] + ' touches';
  el.classList.add('has-data');
}
document.querySelectorAll('.trend-grid').forEach(function(grid){
  ['click', 'mouseover'].forEach(function(evt){
    grid.addEventListener(evt, function(e){
      const c = e.target.closest('circle.tb');
      if (c) showStats(c);
    });
  });
});

// ===========================================================================
// Filters -- one entry point, every panel re-reads the same predicate
// ===========================================================================
function applyFilters(){
  const vis = new Set(visiblePlayers().map(function(p){ return p.pid; }));
  document.querySelectorAll('.trend-card').forEach(function(c){
    c.style.display = vis.has(c.getAttribute('data-pid')) ? '' : 'none';
  });
  document.querySelectorAll('#posFilter .filter-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-pos') === currentPos);
  });
  const overridden = pinned.size > 0 || !!searchText;
  document.getElementById('posCount').textContent = overridden
    ? (vis.size + ' shown (top-' + TOP_N + ' cap off while filtering by name)')
    : (currentPos === 'ALL' ? 'top ' + TOP_N + ' of the pool' : vis.size + ' ' + currentPos + 's in the pool');
  document.querySelectorAll('.gridCount').forEach(function(el){ el.textContent = vis.size + ' cards'; });
  renderScatter();
  observeAll();
}

function renderChips(){
  const wrap = document.getElementById('pinChips');
  wrap.innerHTML = Array.from(pinned).map(function(pid){
    const p = BY_PID[pid];
    return '<span class="chip">' + esc(p.n) + ' <span style="opacity:.7">' + p.p + '</span>' +
      '<button data-pid="' + esc(pid) + '" aria-label="Remove ' + esc(p.n) + '">&times;</button></span>';
  }).join('') + (pinned.size ? '<button class="filter-btn small" id="clearPins">Clear all</button>' : '');
}
function togglePin(pid){
  if (pinned.has(pid)) pinned.delete(pid); else pinned.add(pid);
  renderChips();
  applyFilters();
}
document.getElementById('pinChips').addEventListener('click', function(e){
  if (e.target.id === 'clearPins'){ pinned.clear(); renderChips(); applyFilters(); return; }
  const b = e.target.closest('button[data-pid]');
  if (b) togglePin(b.getAttribute('data-pid'));
});

const searchEl = document.getElementById('playerSearch');
searchEl.addEventListener('input', function(){
  searchText = this.value.trim().toLowerCase();
  applyFilters();
});
// Enter (or picking from the autocomplete list) turns the typed name into a
// pin, so you can build up a set of players and then clear the box.
searchEl.addEventListener('change', pinFromInput);
searchEl.addEventListener('keydown', function(e){ if (e.key === 'Enter') pinFromInput(); });
function pinFromInput(){
  const q = searchEl.value.trim().toLowerCase();
  if (!q) return;
  let hit = POOL.find(function(p){ return p.n.toLowerCase() === q; });
  if (!hit){
    const hits = POOL.filter(function(p){ return matchesSearch(p, q); });
    if (hits.length !== 1) return;   // ambiguous -- leave it as a live filter
    hit = hits[0];
  }
  pinned.add(hit.pid);
  searchEl.value = '';
  searchText = '';
  renderChips();
  applyFilters();
}

document.getElementById('posFilter').addEventListener('click', function(e){
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  currentPos = btn.getAttribute('data-pos');
  applyFilters();
});
document.querySelectorAll('.season-cb').forEach(function(cb){
  cb.addEventListener('change', applySeasonToggle);
});
document.querySelectorAll('.scale-toggle').forEach(function(bar){
  bar.addEventListener('click', function(e){
    const btn = e.target.closest('.filter-btn');
    if (!btn) return;
    const metric = bar.getAttribute('data-metric');
    scaleMode[metric] = btn.getAttribute('data-scale');
    bar.querySelectorAll('.filter-btn').forEach(function(b){
      b.classList.toggle('active', b.getAttribute('data-scale') === scaleMode[metric]);
    });
    redrawMetric(metric);
  });
});

// Default window: the most recent 4 weeks of the current season -- a form
// read rather than a season-long average, which is what the slider is for.
(function init(){
  const maxW = weeksAvailable(scSeason);
  wkTo = maxW;
  wkFrom = Math.max(1, maxW - 3);
  syncWeekUI();
  applyFilters();
})();
"""


def render_html(pool, trend_series, seasons, seasons_label, scoring_label, ranking_season, rank_week,
                rank_by, pool_size, top_n, selection_mode, watchlist_missing, now, snap_cov):
    """Self-contained page: a scrubbable points/game vs snaps/game scatter and
    two grids of small-multiple trend charts, all sharing a position filter, a
    player search/pin filter and a hover readout. Every chart is drawn
    client-side from the one TREND_GAMES payload."""
    as_of = now.strftime("%Y-%m-%d %H:%M UTC")

    if not pool or not any(trend_series.values()):
        return _wrap_html("""
<div class="eyebrow">Fantasy Trends</div>
<h1>Volume, Efficiency &amp; Weekly Form</h1>
<div class="empty-note">No weekly stats available from Sleeper for any season in the window -- check back
once games have been played, or verify FANTASY_WATCHLIST names match Sleeper's player directory.</div>""")

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
    season_checkboxes = "".join(
        f'<label class="checkbox-btn"><input type="checkbox" class="season-cb" value="{html.escape(s)}" checked>'
        f' {html.escape(s)}</label>'
        for s in seasons
    )
    datalist = "".join(f'<option value="{html.escape(p["name"])}">' for p in pool)

    # Seasons that actually contain snap data are the only ones the scatter can
    # plot an x-axis for, so the selector offers those rather than every season.
    seasons_with_snaps = sorted({g[G_SEASON] for games in trend_series.values()
                                 for g in games if g[G_SNAPS]}, reverse=True)
    scatter_season_opts = "".join(
        f'<option value="{html.escape(s)}"{" selected" if s == str(ranking_season) else ""}>{html.escape(s)}</option>'
        for s in seasons_with_snaps
    ) or f'<option value="{html.escape(str(ranking_season))}">{html.escape(str(ranking_season))}</option>'

    snap_vals = [g[G_SNAPS] for games in trend_series.values() for g in games if g[G_SNAPS]]
    snap_bounds = [min(snap_vals), max(snap_vals)] if snap_vals else [0, 1]
    if snap_bounds[0] == snap_bounds[1]:
        snap_bounds[1] = snap_bounds[0] + 1
    pts_vals = [g[G_PTS] for games in trend_series.values() for g in games]
    pps_vals = [g[G_PTS] / g[G_SNAPS] for games in trend_series.values() for g in games if g[G_SNAPS]]
    global_max = {
        "pts": round(max(pts_vals), 1) if pts_vals else 1,
        "pps": round(max(pps_vals), 3) if pps_vals else 1,
    }

    def cards_for(metric):
        out = []
        for entry in pool:
            games = trend_series.get(entry["pid"], [])
            vals = [g[G_PTS] for g in games] if metric == "pts" else \
                   [g[G_PTS] / g[G_SNAPS] for g in games if g[G_SNAPS]]
            if vals:
                avg, best = sum(vals) / len(vals), max(vals)
                fmt = "{:.1f}" if metric == "pts" else "{:.3f}"
                stat_line = (f'avg <b>{fmt.format(avg)}</b> &middot; best <b>{fmt.format(best)}</b> '
                             f'&middot; {len(vals)} games')
            else:
                stat_line = "no data"
            stale = (entry.get("weeks_stale") or 0) > 0
            stale_note = (f' &middot; <span class="stale">last: wk {entry["rank_from_week"]}</span>'
                          if stale else "")
            out.append(f"""
      <div class="trend-card" data-pid="{html.escape(entry['pid'])}" data-pos="{html.escape(entry['pos'])}"
           data-rank="{entry['rank']}" data-metric="{metric}">
        <div class="trend-card-head">
          <span class="name">#{entry['rank']} {html.escape(entry['name'])}</span>
          <span class="meta">{html.escape(entry['pos'])} &middot; {html.escape(entry['team'])}{stale_note}</span>
        </div>
        <div class="card-stats">{stat_line}</div>
        <svg class="m-{metric}" viewBox="0 0 440 170" role="img"
             aria-label="{html.escape(entry['name'])} weekly {'points' if metric == 'pts' else 'points per snap'}"></svg>
        <div class="trend-readout">Hover or click a point for that game's numbers</div>
      </div>""")
        return "".join(out)

    with_snaps, total_games = snap_cov
    snap_pct = (100.0 * with_snaps / total_games) if total_games else 0
    if snap_pct < 99:
        snap_note = (f'<span class="warn">Snap counts are present for {snap_pct:.0f}% of games in this '
                     f'window</span> ({with_snaps:,} of {total_games:,}) -- games without them are drawn '
                     f'as gaps, never as zeros, and are left out of the scatter\'s averages.')
    else:
        snap_note = f"Snap counts present for {with_snaps:,} of {total_games:,} games."

    stale_count = sum(1 for p in pool if (p.get("weeks_stale") or 0) > 0)
    if selection_mode == "ranked":
        if rank_by != "season":
            basis = (f"the {html.escape(scoring_label)} points in <b>each player's own most recent game</b>, "
                     f"as of <b>{html.escape(str(ranking_season))} week {rank_week}</b>")
            caveat = (" &mdash; a player on bye, hurt or inactive is ranked on the last week they actually "
                      "played, not dropped"
                      + (f" ({stale_count} of {len(pool)} are ranked on an earlier week; their cards say "
                         f"<b>last: wk N</b>)" if stale_count else ""))
        else:
            basis = (f"season-to-date {html.escape(scoring_label)} points in "
                     f"<b>{html.escape(str(ranking_season))}</b>")
            caveat = ""
        selection_note = (f"Pool = top <b>{pool_size}</b> players by {basis}{caveat}. <b>ALL</b> shows the "
                          f"top {top_n}; a position shows <b>every</b> player at that position; searching "
                          f"or pinning a name overrides the cap entirely.")
    else:
        selection_note = (f"Player list supplied via FANTASY_WATCHLIST ({len(pool)} players) -- ranking "
                          f"skipped.")

    missing_note = ""
    if watchlist_missing:
        missing_note = ('<br><span class="warn">Not found on Sleeper this run:</span> '
                        + html.escape(", ".join(watchlist_missing)))

    def scale_toggle(metric):
        return f"""
  <div class="filter-bar scale-toggle" data-metric="{metric}">
    <span class="filter-bar-label">Y-axis</span>
    <button class="filter-btn active" data-scale="auto">Per player</button>
    <button class="filter-btn" data-scale="shared">Shared across all</button>
  </div>"""

    body = f"""
<div class="eyebrow">Fantasy Trends</div>
<h1>Volume, Efficiency &amp; Weekly Form</h1>
<div class="sub">
  {len(pool)} players, {html.escape(seasons_label)}. One set of filters drives all three panels &mdash;
  search or pin players by name, narrow to a position, and every chart below follows.
</div>
<div class="status">
  {total_games:,} games &middot; generated {html.escape(as_of)}<br>
  {selection_note}{missing_note}<br>
  {snap_note}
</div>

<div class="sticky-controls">
  <div class="filter-bar">
    <span class="filter-bar-label">Player</span>
    <div class="search-wrap">
      <input id="playerSearch" list="playerNames" placeholder="Search a name, team or position…"
             autocomplete="off" aria-label="Search players">
      <datalist id="playerNames">{datalist}</datalist>
      <span class="hint">Enter (or click any mark) to pin &middot; pinned players ignore every other filter</span>
    </div>
  </div>
  <div class="filter-bar" id="pinChips"></div>
  <div class="filter-bar" id="posFilter">
    <span class="filter-bar-label">Position</span>{filter_buttons}
    <span class="filter-count" id="posCount"></span>
  </div>
  <div class="filter-bar" id="seasonFilter">
    <span class="filter-bar-label">Seasons <span style="opacity:.6">(trend grids)</span></span>{season_checkboxes}
  </div>
</div>

<div class="section">
  <div class="section-head">
    <h2>Points Per Game &times; Snaps Per Game <span class="filter-count" id="scatterCount"></span></h2>
    <div class="sub">Each mark is one player over the week window you choose below. <b>Y</b> = fantasy
    points per game, <b>X</b> = offensive snaps per game, both averaged over that window.
    <b>Mark size</b> = that player's points per game across their <b>whole {html.escape(str(ranking_season))}
    season</b> (every game, not just the window) &mdash; a fixed reference that stays put as you slide.
    <b>Shape</b> carries position; dashed crosshairs are the medians of whoever is currently shown.
    Click a mark to pin that player everywhere.</div>
  </div>
  <div class="filter-bar">
    <span class="filter-bar-label">Season</span>
    <select id="scatterSeason">{scatter_season_opts}</select>
    <span class="filter-bar-label" style="margin-left:10px">Window</span>
    <span id="weekPresets" style="display:flex; gap:6px">
      <button class="filter-btn small" data-last="1">Last 1</button>
      <button class="filter-btn small" data-last="3">Last 3</button>
      <button class="filter-btn small" data-last="5">Last 5</button>
      <button class="filter-btn small" data-last="all">Full season</button>
    </span>
  </div>
  <div class="scatter-row">
    <div class="scatter-stage">
      <svg id="scatterSvg" viewBox="0 0 960 520" preserveAspectRatio="xMidYMid meet"
           role="img" aria-label="Points per game against snaps per game"></svg>
      <div class="slider-wrap">
        <div class="slider-row"><span class="lbl">from</span><input type="range" id="wkFrom" min="1" max="18" value="1"
             aria-label="First week in window"></div>
        <div class="slider-row"><span class="lbl">to</span><input type="range" id="wkTo" min="1" max="18" value="18"
             aria-label="Last week in window"></div>
        <div class="week-label" id="weekLabel"></div>
      </div>
    </div>
    <aside class="scatter-side">
      <div>
        <div class="legend-title">Position (shape)</div>
        <div class="shape-legend" style="margin-top:8px">
          <div><svg width="18" height="18" viewBox="0 0 18 18"><circle class="mk" cx="9" cy="9" r="6"/></svg> QB</div>
          <div><svg width="18" height="18" viewBox="0 0 18 18"><rect class="mk" x="3.7" y="3.7" width="10.6" height="10.6" rx="1.5"/></svg> RB</div>
          <div><svg width="18" height="18" viewBox="0 0 18 18"><polygon class="mk" points="9,2.7 1.9,12 16.1,12"/></svg> WR</div>
          <div><svg width="18" height="18" viewBox="0 0 18 18"><polygon class="mk" points="9,1.5 16.5,9 9,16.5 1.5,9"/></svg> TE</div>
        </div>
      </div>
      <div>
        <div class="legend-title">Size = {html.escape(str(ranking_season))} points/game</div>
        <svg width="200" height="52" viewBox="0 0 200 52" style="margin-top:4px">
          <circle class="mk" cx="16" cy="21" r="4"/><text class="ax" x="16" y="46" text-anchor="middle">low</text>
          <circle class="mk" cx="56" cy="21" r="10"/><text class="ax" x="56" y="46" text-anchor="middle">mid</text>
          <circle class="mk" cx="104" cy="21" r="17"/><text class="ax" x="104" y="46" text-anchor="middle">high</text>
        </svg>
      </div>
      <div>
        <div class="legend-title">Shown players &mdash; click to pin</div>
        <div class="rank-table" id="rankTable"></div>
      </div>
    </aside>
  </div>
</div>

<div class="section">
  <div class="section-head">
    <h2>Weekly Points <span class="filter-count gridCount"></span></h2>
    <div class="sub">Y-axis = {html.escape(scoring_label)} points scored that week, starting at zero, with
    five labelled ticks. The amber dashed line is that player's average across every game shown; the
    <b>peak</b> and <b>latest</b> games are labelled with their values. <b>Per player</b> scales each card
    to its own best week; <b>Shared across all</b> puts every card on the report-wide maximum
    ({global_max['pts']:.0f} pt) so cards compare directly.</div>
  </div>
  {scale_toggle('pts')}
  <div class="trend-grid" id="gridPts">{cards_for('pts')}</div>
</div>

<div class="section">
  <div class="section-head">
    <h2>Points Per Snap <span class="filter-count gridCount"></span></h2>
    <div class="sub">Y-axis = that week's points divided by offensive snaps played &mdash; the efficiency
    read, separating a player producing on limited playing time from one accumulating points because he
    never leaves the field. A week with no snap data is a gap, not a zero. Shared maximum is
    {global_max['pps']:.3f} pt/snap.</div>
  </div>
  {scale_toggle('pps')}
  <div class="trend-grid" id="gridPps">{cards_for('pps')}</div>
</div>

<div id="scatterTip" role="tooltip"></div>

<script>
{_page_js(trend_series, pool, seasons, top_n, snap_bounds, global_max, ranking_season, rank_week)}
</script>
"""
    return _wrap_html(body)


def _page_js(trend_series, pool, seasons, top_n, snap_bounds, global_max, ranking_season, rank_week):
    """Substitute the run's data into PAGE_JS. Compact separators throughout --
    whitespace on a payload this size is measured in hundreds of KB."""
    def j(v):
        return json.dumps(v, separators=(",", ":"))

    pool_light = [{"pid": p["pid"], "n": p["name"], "p": p["pos"], "t": p["team"], "k": p["rank"]}
                  for p in pool]
    return (PAGE_JS
            .replace("__TREND_GAMES__", j({k: v for k, v in trend_series.items() if v}))
            .replace("__POOL__", j(pool_light))
            .replace("__SEASONS__", j(seasons))
            .replace("__TOP_N__", str(top_n))
            .replace("__SNAP_BOUNDS__", j(snap_bounds))
            .replace("__GLOBAL_MAX__", j(global_max))
            .replace("__RANK_SEASON__", j(str(ranking_season)))
            .replace("__RANK_WEEK__", str(rank_week or 1)))


def _wrap_html(body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Fantasy Trends</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>{PAGE_CSS}</style>
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
            f.write(render_html([], {}, [], "", scoring_label, None, None, rank_by, pool_size, top_n,
                                "ranked", [], now, (0, 0)))
        return

    seasons = season_list(season, trend_seasons_back)
    seasons_label = f"{seasons[0]}–{seasons[-1]}"
    is_live = bool(season_type == "regular" and current_week and current_week >= 1)
    live_season = season if is_live else None
    live_week = current_week if is_live else None

    print(f"[info] resolving ranking window across {seasons} "
          f"({'live -- capping ' + str(season) + ' at week ' + str(current_week) if is_live else 'off-season'})...")
    ranking_season, ranking_weeks, rank_week = pick_ranking_window(seasons, scoring_field, live_season, live_week)
    print(f"[info] ranking season={ranking_season}, {len(ranking_weeks)} weeks of data, "
          f"ranking on {'season totals' if rank_by == 'season' else 'each player last game as of week ' + str(rank_week)}")

    missing = []
    if manual_names:
        selection_mode = "manual"
        pool = resolve_watchlist_ids(players_dir, manual_names)
        resolved = {p["name"] for p in pool}
        missing = [n for n in manual_names if n not in resolved]
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
                head = ", ".join(
                    f"{p['rank']}.{p['name']} ({p['rank_pts']}"
                    + (f", wk{p['rank_from_week']}" if (p.get('weeks_stale') or 0) > 0 else "") + ")"
                    for p in pool[:5]
                )
                print(f"[info] top of pool: {head}")
                if rank_by != "season":
                    stale = sum(1 for p in pool if (p.get("weeks_stale") or 0) > 0)
                    print(f"[info] {stale} of {len(pool)} ranked on an earlier week (bye/inactive/injured)")

    if not pool:
        with open("index.html", "w") as f:
            f.write(render_html([], {}, seasons, seasons_label, scoring_label, ranking_season, rank_week,
                                rank_by, pool_size, top_n, selection_mode, missing, now, (0, 0)))
        print("[info] wrote index.html (empty-state)")
        return

    print(f"[info] building {trend_seasons_back}-season history for {len(pool)} players ({seasons_label})...")
    trend_series = build_trend_series(pool, seasons, scoring_field)
    snap_cov = snap_coverage(trend_series)
    if snap_cov[1]:
        print(f"[info] snap counts present for {snap_cov[0]:,} of {snap_cov[1]:,} games "
              f"({100.0 * snap_cov[0] / snap_cov[1]:.0f}%)")
    if snap_cov[1] and snap_cov[0] == 0:
        print("[warn] NO snap data at all -- the points-per-snap grid and the scatter's x-axis will be "
              f"empty. Check that Sleeper still populates {SNAP_FIELDS} in the stats payload.", file=sys.stderr)

    with open("index.html", "w") as f:
        f.write(render_html(pool, trend_series, seasons, seasons_label, scoring_label, ranking_season,
                            rank_week, rank_by, pool_size, top_n, selection_mode, missing, now, snap_cov))

    size_mb = os.path.getsize("index.html") / (1024 * 1024)
    print(f"[info] wrote index.html ({size_mb:.1f} MB, {len(pool)} players, {snap_cov[1]:,} games)")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        basis = (f"**{ranking_season}** season-to-date totals" if rank_by == "season"
                 else f"**each player's most recent game** through {ranking_season} week {rank_week}")
        lines = [
            f"## Fantasy Trends — {len(pool)} players, {seasons_label}",
            "",
            f"Pool ranked on {basis}; ALL view shows the top {top_n}, position views show the whole "
            f"pool at that position, search/pin overrides both. Snap counts present for "
            f"{snap_cov[0]:,} of {snap_cov[1]:,} games.",
            "",
            "| # | Player | Pos | Team | Last game | Snaps | pt/snap | Season pt/g | Games |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        rs = str(ranking_season)
        for entry in pool[:top_n]:
            games = trend_series.get(entry["pid"], [])
            last = None
            for g in games:
                if entry["rank_from_week"] is None or (g[G_SEASON] == rs and g[G_WEEK] == entry["rank_from_week"]):
                    last = g
            last = last or (games[-1] if games else None)
            season_pts = [g[G_PTS] for g in games if g[G_SEASON] == rs]
            ppg = f"{sum(season_pts) / len(season_pts):.1f}" if season_pts else "—"
            pts_cell = f"{entry['rank_pts']:.1f}" if entry["rank_pts"] is not None else "—"
            if (entry.get("weeks_stale") or 0) > 0:
                pts_cell += f" _(wk {entry['rank_from_week']})_"
            if last and last[G_SNAPS]:
                snaps_cell, pps_cell = str(last[G_SNAPS]), f"{last[G_PTS] / last[G_SNAPS]:.3f}"
            else:
                snaps_cell, pps_cell = "—", "—"
            lines.append(
                f"| {entry['rank']} | {entry['name']} | {entry['pos']} | {entry['team']} | "
                f"{pts_cell} | {snaps_cell} | {pps_cell} | {ppg} | {len(games)} |"
            )
        if missing:
            lines += ["", f"Not found on Sleeper: {', '.join(missing)}"]
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
