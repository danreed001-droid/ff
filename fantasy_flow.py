#!/usr/bin/env python3
"""
Fantasy Trends: per-player weekly trend charts for the top slice of the
NFL, built fresh from the Sleeper API on a weekly cron.

Two grids of small-multiple charts, one card per player, sharing a pool, a
position filter and a season filter:

  1. WEEKLY POINTS -- fantasy points per game, chronological, across as much
     of the last 6 regular seasons as each player actually has. Labeled
     y-axis, so you can read the point value off the chart.
  2. POINTS PER SNAP -- the same weeks, divided by offensive snaps played.
     The efficiency read: who produces the most per unit of playing time,
     rather than who is simply on the field the most.

Every week is a bubble sized by that game's SNAP COUNT, scaled across every
game for every player in the report -- so bubble size means the same thing
on both grids and on every card. Hover or click any bubble for that game's
exact numbers.

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
     full pool, not just the ones that cracked the overall top 40.

A player's last game is looked for within the ranking season only, newest
week first -- someone who hasn't played at all this season has nothing to
rank and is left out. How stale each player's number is shows on their card
("last: wk N" whenever it isn't the current week), so a week-2 number is
never silently compared to a week-9 one. FANTASY_RANK_BY=season switches to
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

The charts are drawn in the browser from a compact data payload rather than
being pre-rendered as SVG server-side. That keeps a 300-player, 6-season
report under a megabyte instead of ~4MB of markup, lets the y-axis switch
between per-player and shared scaling without a rebuild, and means only the
cards actually on screen ever get drawn.

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
                         exposes.
    FANTASY_TOP_N     - how many of the pool the ALL view shows
                         (default 40). Position views ignore this.
    FANTASY_RANK_BY   - "last_game" (default) ranks by each player's own
                         most recent game; "season" ranks by season-to-date
                         totals for a pool that barely moves week to week.
    FANTASY_WATCHLIST - comma-separated player full names. If set, ranking
                         is skipped and exactly these players are used.
    FANTASY_POSITIONS - comma-separated positions eligible for the pool
                         (default "QB,RB,WR,TE").
    FANTASY_TREND_SEASONS - how many regular seasons the grids cover,
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
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-trends/3.0"})
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
    caption a stale number rather than passing it off as current. The search
    stays inside `weeks_by_week`, i.e. the ranking season.

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
    rush attempts + receptions, carried along for the hover readout. Points
    per snap is NOT stored -- it's pts/snaps, computed in the browser, and
    shipping it would be a third of the payload for no new information.

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
    snap field isn't guaranteed for older seasons, and the points-per-snap
    grid is only as good as this ratio -- so it gets printed to the run log
    and stated on the page rather than leaving a half-empty chart
    unexplained."""
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
  .sub{ color:var(--dim); font-size:12px; margin-bottom:6px; line-height:1.55; max-width:760px; }
  .sub b{ color:var(--ink); }
  .status{ color:var(--dim); font-size:11px; margin-bottom:4px; line-height:1.6; }
  .status b{ color:var(--ink); }
  .warn{ color:var(--amber); }
  .section{ margin-top:36px; }
  .section-head{ margin-bottom:10px; }
  .filter-bar{ display:flex; gap:8px; margin:12px 0 6px; flex-wrap:wrap; align-items:center; }
  .filter-bar-label{ font-size:10px; color:var(--dim); letter-spacing:.08em; text-transform:uppercase; margin-right:2px; }
  .filter-btn{
    background:var(--panel); border:1px solid var(--line); color:var(--dim);
    font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.03em;
    padding:7px 13px; border-radius:6px; cursor:pointer; transition:all .15s ease;
  }
  .filter-btn:hover{ color:var(--ink); border-color:#39424f; }
  .filter-btn.active{ color:#0B0E14; background:var(--cyan); border-color:var(--cyan); font-weight:600; }
  .filter-count{ font-size:10.5px; color:var(--dim); margin-left:4px; }
  .checkbox-btn{
    display:inline-flex; align-items:center; gap:6px; background:var(--panel); border:1px solid var(--line);
    color:var(--dim); font-family:'IBM Plex Mono', monospace; font-size:11px; padding:6px 12px;
    border-radius:6px; cursor:pointer; user-select:none;
  }
  .checkbox-btn input{ accent-color:var(--cyan); cursor:pointer; }
  .sticky-controls{
    position:sticky; top:0; z-index:20; background:linear-gradient(180deg, var(--bg) 82%, rgba(11,14,20,0));
    padding-bottom:10px;
  }
  /* 6 seasons is ~85 games per card; narrow cards turn that into an
     unreadable scribble, so cards are wide and the grid holds fewer
     columns. */
  .trend-grid{ display:grid; grid-template-columns:repeat(auto-fill, minmax(430px, 1fr)); gap:14px; }
  .trend-card{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px 10px; }
  .trend-card-head{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; gap:8px; }
  .trend-card-head .name{ font-size:12.5px; font-weight:600; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .trend-card-head .meta{ font-size:10px; color:var(--dim); white-space:nowrap; }
  .trend-card-head .stale{ color:var(--amber); }
  .trend-card svg{ display:block; width:100%; height:auto; }
  .trend-readout{
    font-size:10.5px; color:var(--dim); border-top:1px solid var(--line); margin-top:6px; padding-top:7px;
    min-height:26px;
  }
  .trend-readout.has-data{ color:var(--ink); }
  .trend-readout b{ color:var(--ink); }
  /* chart internals */
  /* Text and gridlines must never swallow a hover/click meant for a bubble --
     the last-value label sits directly on top of the most recent game, which
     is the point you most want to inspect. */
  .trend-card svg text, .trend-card svg line{ pointer-events:none; }
  .ax{ fill:#6B7280; font-size:8.5px; font-family:'IBM Plex Mono', monospace; }
  .axline{ stroke:rgba(30,38,51,1); stroke-width:1; }
  .grid{ stroke:rgba(107,114,128,0.16); stroke-width:1; }
  .sbound{ stroke:rgba(107,114,128,0.4); stroke-width:1; stroke-dasharray:2,3; }
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
  .lastval{ fill:var(--ink); font-size:9.5px; font-family:'IBM Plex Mono', monospace; }
  .empty{ fill:#6B7280; font-size:10.5px; font-family:'IBM Plex Mono', monospace; }
"""

PAGE_JS = r"""
const TREND_GAMES = __TREND_GAMES__;
const POOL = __POOL__;
const SEASONS = __SEASONS__;
const TOP_N = __TOP_N__;
const SNAP_R = __SNAP_BOUNDS__;   // [min, max] snaps across every game in the report
const GLOBAL_MAX = __GLOBAL_MAX__; // {pts: n, pps: n} report-wide maxima for shared scaling

// game tuple indices
const G_SEASON = 0, G_WEEK = 1, G_PTS = 2, G_SNAPS = 3, G_TOUCHES = 4;

const GEO = { W: 440, H: 170, ML: 34, MR: 10, MT: 12, MB: 20 };
// Max radius stays modest: at ~85 games per card a large bubble overlaps
// its neighbours and hides the line it sits on.
const R_MIN = 1.8, R_MAX = 5.2;

let currentPos = 'ALL';
const scaleMode = { pts: 'auto', pps: 'auto' };   // 'auto' = per player, 'shared' = whole report

// The value a card plots, per metric. Points per snap is derived here rather
// than shipped: a game with no snap data (Sleeper doesn't populate the field
// for every historical season) has no defined efficiency and returns null,
// which the renderer draws as a gap in the line rather than a zero.
function valueOf(g, metric){
  if (metric === 'pts') return g[G_PTS];
  if (!g[G_SNAPS]) return null;
  return g[G_PTS] / g[G_SNAPS];
}
function fmt(v, metric){
  if (v === null || v === undefined) return 'n/a';
  return metric === 'pts' ? v.toFixed(1) : v.toFixed(3);
}
// Axis ticks want fewer significant digits than the readout does, and every
// tick on one axis must use the SAME number of them -- a "0 / 7.0 / 14" axis
// reads as three different quantities. Precision is chosen from the axis
// maximum, then applied to all three ticks.
function tickDecimals(hi, metric){
  if (metric === 'pts') return hi >= 10 ? 0 : 1;
  return hi >= 1 ? 2 : 3;
}
function fmtTick(v, decimals){
  return v.toFixed(decimals);
}

function radiusOf(snaps){
  if (!snaps) return R_MIN;
  const span = (SNAP_R[1] - SNAP_R[0]) || 1;
  const f = Math.max(0, Math.min(1, (snaps - SNAP_R[0]) / span));
  return R_MIN + f * (R_MAX - R_MIN);
}

function esc(s){
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Draws one card's chart. Everything is positioned from the FULL game list,
// so hiding a season with the checkboxes is a pure visibility toggle -- no
// point ever moves, which is what makes the grid readable while you filter.
function renderChart(svg, games, metric){
  const W = GEO.W, H = GEO.H, ML = GEO.ML, MR = GEO.MR, MT = GEO.MT, MB = GEO.MB;
  const PW = W - ML - MR, PH = H - MT - MB;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);

  const vals = games.map(function(g){ return valueOf(g, metric); });
  const real = vals.filter(function(v){ return v !== null && v !== undefined; });
  if (real.length < 2){
    svg.innerHTML = '<text class="empty" x="' + (W / 2) + '" y="' + (H / 2) + '" text-anchor="middle">' +
      (metric === 'pps' ? 'no snap data' : 'no data yet') + '</text>';
    return;
  }

  // Points and points-per-snap are both floor-at-zero quantities, so the
  // axis always starts at 0 -- a chart that starts at the player's own
  // minimum exaggerates ordinary week-to-week noise into a cliff.
  const lo = 0;
  const hi = scaleMode[metric] === 'shared' ? GLOBAL_MAX[metric] : Math.max.apply(null, real);
  const span = (hi - lo) || 1;

  const xOf = function(i){ return ML + (games.length === 1 ? PW / 2 : (i / (games.length - 1)) * PW); };
  const yOf = function(v){ return MT + (1 - (v - lo) / span) * PH; };

  const parts = [];

  // y-axis: three labeled ticks with faint gridlines, so a value can be read
  // off the chart instead of only via hover.
  const dec = tickDecimals(hi, metric);
  [lo, lo + span / 2, hi].forEach(function(t){
    const y = yOf(t);
    parts.push('<line class="grid" x1="' + ML + '" y1="' + y.toFixed(1) + '" x2="' + (W - MR) + '" y2="' + y.toFixed(1) + '"/>');
    parts.push('<text class="ax" x="' + (ML - 5) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' +
      fmtTick(t, dec) + '</text>');
  });
  parts.push('<line class="axline" x1="' + ML + '" y1="' + MT + '" x2="' + ML + '" y2="' + (H - MB) + '"/>');

  // Season boundary guides + labels, drawn once and always visible so a
  // hidden season still reads as a gap rather than a squeeze.
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

  // One <g> per season for the checkbox toggle. Within a season the line is
  // broken wherever the value is null, so a stretch with no snap data reads
  // as a gap instead of being bridged by a straight line that implies data
  // we don't have.
  let i = 0;
  const lastRealIdx = vals.reduce(function(acc, v, idx){ return (v === null || v === undefined) ? acc : idx; }, -1);
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

  if (lastRealIdx >= 0){
    const lx = xOf(lastRealIdx), ly = yOf(vals[lastRealIdx]);
    parts.push('<text class="lastval" x="' + lx.toFixed(1) + '" y="' + Math.max(10, ly - R_MAX - 5).toFixed(1) +
      '" text-anchor="end">' + fmt(vals[lastRealIdx], metric) + '</text>');
  }

  svg.innerHTML = parts.join('');
  applySeasonToggleTo(svg);
}

// Cards are drawn only once they scroll into view. With a 300-player pool
// across two grids that's 600 charts; drawing them all up front would stall
// the page for seconds, and most are never looked at.
const drawn = new WeakSet();
function drawCard(card){
  const svg = card.querySelector('svg');
  const games = TREND_GAMES[card.getAttribute('data-pid')] || [];
  renderChart(svg, games, card.getAttribute('data-metric'));
  drawn.add(card);
}
const observer = ('IntersectionObserver' in window) ? new IntersectionObserver(function(entries){
  entries.forEach(function(e){
    if (e.isIntersecting && !drawn.has(e.target)) drawCard(e.target);
  });
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

// --- filters --------------------------------------------------------------
// ALL = the overall top TOP_N of the pool, so the grid stays scannable.
// A position = every player at that position in the pool, however many.
function applyPositionFilter(pos){
  currentPos = pos;
  document.querySelectorAll('#posFilter .filter-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-pos') === pos);
  });
  let shown = 0;
  document.querySelectorAll('.trend-card').forEach(function(c){
    const ok = pos === 'ALL' ? (+c.getAttribute('data-rank') <= TOP_N) : (c.getAttribute('data-pos') === pos);
    c.style.display = ok ? '' : 'none';
    if (ok && c.getAttribute('data-metric') === 'pts') shown++;
  });
  document.getElementById('posCount').textContent =
    pos === 'ALL' ? ('top ' + TOP_N + ' of the pool') : (shown + ' ' + pos + 's in the pool');
  document.querySelectorAll('.gridCount').forEach(function(el){ el.textContent = shown + ' cards'; });
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
function applySeasonToggle(){
  document.querySelectorAll('.trend-card svg').forEach(applySeasonToggleTo);
}

// --- readout --------------------------------------------------------------
// One delegated listener per grid rather than a handler per bubble: at 300
// players x 2 grids that's tens of thousands of points, and the numbers
// already live once in TREND_GAMES.
function showStats(circle){
  const card = circle.closest('.trend-card');
  const games = TREND_GAMES[card.getAttribute('data-pid')];
  if (!games) return;
  const g = games[+circle.getAttribute('data-i')];
  if (!g) return;
  const pps = g[G_SNAPS] ? (g[G_PTS] / g[G_SNAPS]) : null;
  card.querySelector('.trend-readout').innerHTML =
    '<b>' + g[G_SEASON] + ' wk' + g[G_WEEK] + ':</b> ' + g[G_PTS].toFixed(1) + ' pt · ' +
    (g[G_SNAPS] === null ? 'snaps n/a' : g[G_SNAPS] + ' snaps') + ' · ' +
    (pps === null ? 'pt/snap n/a' : pps.toFixed(3) + ' pt/snap') + ' · ' + g[G_TOUCHES] + ' touches';
  card.querySelector('.trend-readout').classList.add('has-data');
}

document.querySelectorAll('.trend-grid').forEach(function(grid){
  ['click', 'mouseover'].forEach(function(evt){
    grid.addEventListener(evt, function(e){
      const c = e.target.closest('circle.tb');
      if (c) showStats(c);
    });
  });
});

document.getElementById('posFilter').addEventListener('click', function(e){
  const btn = e.target.closest('.filter-btn');
  if (btn) applyPositionFilter(btn.getAttribute('data-pos'));
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

applyPositionFilter('ALL');
observeAll();
"""


def render_html(pool, trend_series, seasons, seasons_label, scoring_label, ranking_season, rank_week,
                rank_by, pool_size, top_n, selection_mode, watchlist_missing, now, snap_cov):
    """Self-contained page: two grids of small-multiple trend charts (weekly
    points, and points per snap), sharing a position filter, a season filter
    and a hover readout. Charts are drawn client-side from TREND_GAMES."""
    as_of = now.strftime("%Y-%m-%d %H:%M UTC")

    if not pool or not any(trend_series.values()):
        return _wrap_html("""
<div class="eyebrow">Fantasy Trends</div>
<h1>Weekly Points &amp; Points Per Snap</h1>
<div class="sub" style="padding:30px 0">No weekly stats available from Sleeper for any season in the
window -- check back once games have been played, or verify FANTASY_WATCHLIST names match Sleeper's
player directory.</div>""")

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

    # Bubble-size scale and the shared-y maxima are computed once, across
    # every game of every player in the report -- so a bubble of a given size
    # means the same snap count on every card and in both grids, and shared
    # scaling doesn't shift when you filter.
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
            n = len(games)
            stale = (entry.get("weeks_stale") or 0) > 0
            stale_note = (f' &middot; <span class="stale">last: wk {entry["rank_from_week"]}</span>'
                          if stale else "")
            out.append(f"""
      <div class="trend-card" data-pid="{html.escape(entry['pid'])}" data-pos="{html.escape(entry['pos'])}"
           data-rank="{entry['rank']}" data-metric="{metric}">
        <div class="trend-card-head">
          <span class="name">#{entry['rank']} {html.escape(entry['name'])}</span>
          <span class="meta">{html.escape(entry['pos'])} &middot; {html.escape(entry['team'])} &middot;
            {n} games{stale_note}</span>
        </div>
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
                     f'as gaps in the points-per-snap grid, never as zeros.')
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
                          f"top {top_n}; picking a position shows <b>every</b> player at that position in "
                          f"the pool.")
    else:
        selection_note = (f"Player list supplied via FANTASY_WATCHLIST ({len(pool)} players) -- ranking "
                          f"skipped. ALL shows the first {top_n}; picking a position shows every one at "
                          f"that position.")

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
<h1>Weekly Points &amp; Points Per Snap</h1>
<div class="sub">
  One card per player, {html.escape(seasons_label)}. Every week is a bubble <b>sized by that game's snap
  count</b>, scaled across every game for every player in the report -- so bubble size means the same
  thing on both grids and never changes when you filter. Dashed lines mark season boundaries.
  <b>Hover or click any point</b> for that game's exact numbers.
</div>
<div class="status">
  {len(pool)} players &middot; {total_games:,} games &middot; generated {html.escape(as_of)}<br>
  {selection_note}{missing_note}<br>
  {snap_note}
</div>

<div class="sticky-controls">
  <div class="filter-bar" id="posFilter">
    <span class="filter-bar-label">Position</span>{filter_buttons}
    <span class="filter-count" id="posCount"></span>
  </div>
  <div class="filter-bar" id="seasonFilter"><span class="filter-bar-label">Seasons</span>{season_checkboxes}</div>
</div>

<div class="section">
  <div class="section-head">
    <h2>Weekly Points <span class="filter-count gridCount"></span></h2>
    <div class="sub">Y-axis = {html.escape(scoring_label)} fantasy points scored that week, starting at
    zero. <b>Per player</b> scales each card to its own best week -- best for reading one player's shape.
    <b>Shared across all</b> puts every card on the report-wide maximum ({global_max['pts']:.0f} pt), which
    makes cards directly comparable but flattens the lower-scoring ones.</div>
  </div>
  {scale_toggle('pts')}
  <div class="trend-grid" id="gridPts">{cards_for('pts')}</div>
</div>

<div class="section">
  <div class="section-head">
    <h2>Points Per Snap <span class="filter-count gridCount"></span></h2>
    <div class="sub">Y-axis = that week's points divided by offensive snaps played. This is the
    efficiency read: it separates a player producing on limited playing time from one accumulating
    points because he never leaves the field. A week with no snap data is a gap, not a zero. Shared
    maximum is {global_max['pps']:.3f} pt/snap.</div>
  </div>
  {scale_toggle('pps')}
  <div class="trend-grid" id="gridPps">{cards_for('pps')}</div>
</div>

<script>
{_page_js(trend_series, pool, seasons, top_n, snap_bounds, global_max)}
</script>
"""
    return _wrap_html(body)


def _page_js(trend_series, pool, seasons, top_n, snap_bounds, global_max):
    """Substitute the run's data into PAGE_JS. Compact separators throughout
    -- whitespace on a payload this size is measured in hundreds of KB."""
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
            .replace("__GLOBAL_MAX__", j(global_max)))


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

    print(f"[info] building {trend_seasons_back}-season trend history for {len(pool)} players "
          f"({seasons_label})...")
    trend_series = build_trend_series(pool, seasons, scoring_field)
    snap_cov = snap_coverage(trend_series)
    print(f"[info] snap counts present for {snap_cov[0]:,} of {snap_cov[1]:,} games "
          f"({100.0 * snap_cov[0] / snap_cov[1]:.0f}%)" if snap_cov[1] else "[info] no games")
    if snap_cov[1] and snap_cov[0] == 0:
        print("[warn] NO snap data at all -- the points-per-snap grid will be empty. Check that "
              f"Sleeper still populates {SNAP_FIELDS} in the stats payload.", file=sys.stderr)

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
            f"pool at that position. Snap counts present for {snap_cov[0]:,} of {snap_cov[1]:,} games.",
            "",
            "| # | Player | Pos | Team | Last game | Snaps | pt/snap | Games |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for entry in pool[:top_n]:
            games = trend_series.get(entry["pid"], [])
            last = None
            for g in games:
                if entry["rank_from_week"] is None or (g[G_SEASON] == str(ranking_season)
                                                       and g[G_WEEK] == entry["rank_from_week"]):
                    last = g
            last = last or (games[-1] if games else None)
            pts_cell = f"{entry['rank_pts']:.1f}" if entry["rank_pts"] is not None else "—"
            if (entry.get("weeks_stale") or 0) > 0:
                pts_cell += f" _(wk {entry['rank_from_week']})_"
            if last and last[G_SNAPS]:
                snaps_cell = str(last[G_SNAPS])
                pps_cell = f"{last[G_PTS] / last[G_SNAPS]:.3f}"
            else:
                snaps_cell, pps_cell = "—", "—"
            lines.append(
                f"| {entry['rank']} | {entry['name']} | {entry['pos']} | {entry['team']} | "
                f"{pts_cell} | {snaps_cell} | {pps_cell} | {len(games)} |"
            )
        if missing:
            lines += ["", f"Not found on Sleeper: {', '.join(missing)}"]
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
