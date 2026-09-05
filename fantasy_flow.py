#!/usr/bin/env python3
"""
Fantasy Trends: per-player weekly charts for the top slice of the NFL, built
fresh from the Sleeper API on a weekly cron.

Two small-multiple grids plus a season-total leaders table, one shared pool
for the grids, one shared set of filters:

Kickers and team defenses appear in the weekly-points grid alongside
everyone else, but are excluded from the points-per-snap grid: they take no
offensive snaps, so "points per snap" is undefined for them rather than
zero. The page says so where it would otherwise look empty.

  1. WEEKLY POINTS (small multiples) -- fantasy points per game,
     chronological, across as much of the last 6 regular seasons as each
     player actually has. Labelled y-axis, average line, peak and latest
     values called out, and an RSI(5) momentum sub-panel underneath every
     card (period 5, not the usual stock-market 14 -- a game log is much
     shorter than a price history).
  2. POINTS PER SNAP (small multiples) -- the same weeks divided by
     offensive snaps played: who produces per unit of playing time rather
     than who simply never leaves the field. Same RSI(5) sub-panel.
  3. EFFICIENCY LEADERS (table, below the grids) -- season-TOTAL yards per
     unit of volume (yards/carry for RB, yards/catch for WR & TE, yards/attempt
     for QB), each position thresholded to a qualifying volume pool before
     ranking by rate (see LEADER_CFG / build_leader_pool()) so a two-carry,
     38-yard game can never outrank a real workload. Filterable by position
     and by season -- an individual year, or the combined last 2 or 3.

On the small multiples every week is a bubble sized by that game's SNAP
COUNT, scaled across every game for every player in the report, so bubble
size means the same thing on both grids and on every card.

A NOTE ON COLOUR
----------------
Position (QB/RB/WR/TE) carries a validated four-hue categorical palette
everywhere it's used as an identity -- the filter bar, the leaders table's
position picker, and the colour dot next to every player's position label.
Four hues is the hard case for a categorical palette used all-pairs (every
position can sit next to every other one), so the usual off-the-shelf
categorical ramps fail at the fourth slot. Rather than guess, POSITION_COLORS
was found by sweeping the OKLCH gamut against the data-viz validator on this
page's exact surface and maximising the worse of the two colour-vision
floors. The set that won passes all five checks all-pairs (worst CVD dE 9.5,
worst normal-vision dE 18.7). Identity is never colour alone regardless: the
position is always also spelled out as text beside its dot.

PLAYER SELECTION
----------------
The report is not a hand-maintained list. Each run:

  1. Ranks EVERY player by the fantasy points they scored in THEIR OWN MOST
     RECENT GAME -- for most players that's the latest completed week, but a
     player who was on bye, injured or inactive is ranked on the last week
     they actually played rather than being dropped from the report.
     Filtered to QB/RB/WR/TE.
  2. Takes the top FANTASY_POOL_SIZE (default 300) of them as the pool, and
     adds two separately-capped groups ranked the same way: the top
     FANTASY_DEF_COUNT team defenses (default 10) and the top
     FANTASY_K_COUNT kickers (default 20). They are capped separately rather
     than ranked against skill players, which would either bury them or, in
     a week where a kicker outscores the WRs, crowd out the players the
     report is about.
  3. With no filter selected the page shows the top FANTASY_TOP_N (default
     40) of that pool, so it opens usable rather than as 300 cards. Any
     selection lifts that cap: pick a POSITION or a TEAM and you get EVERY
     matching player in the full pool. Position and team are each
     multi-select, OR within themselves and AND with each other ("the RBs
     and TEs on KC or BUF"). Searching or pinning specific players ignores
     the cap too, and pinned players are added on top of whatever else is
     selected -- if you asked for a player by name, you get them whatever
     their rank and whatever else is filtered.

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
report under a megabyte instead of several, and means only the cards
actually on screen get drawn.

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
    /v1/stats/nfl/regular/{season}         - one season's TOTAL stats for
                                              every NFL player who recorded
                                              any -- ONE call per season,
                                              used only by the efficiency
                                              leaders table (see
                                              get_season_stats()).

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
    FANTASY_POSITIONS - comma-separated positions eligible for the main pool
                         (default "QB,RB,WR,TE"). Kickers and defenses have
                         their own counts below.
    FANTASY_DEF_COUNT - how many team defenses to include (default 10).
    FANTASY_K_COUNT   - how many kickers to include (default 20).
    FANTASY_TREND_SEASONS - how many regular seasons the trend grids cover,
                         including the current one (default 6).
    FANTASY_LEADER_SEASONS - how many of the most recent regular seasons the
                         efficiency leaders table fetches season totals for
                         (default 3). The page's year filter offers each of
                         these individually plus a combined "last 2" and
                         "last 3".
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

POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]  # display order for the filter bar

# The skill positions: the ones with offensive snaps and touches, and so the
# only ones the points-per-snap grid can say anything about. Kickers and team
# defenses are carried through the report with their own caps, but they are
# excluded from that panel by construction rather than plotted at zero -- a
# kicker's "points per snap" isn't a small number, it's undefined.
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
SNAPLESS_POSITIONS = ("K", "DEF")
DEFAULT_DEF_COUNT = 10
DEFAULT_K_COUNT = 20

# Categorical hues for the four skill positions, used as identity everywhere
# position needs one (filter bar, card meta, leaders table). NOT picked by
# eye: these are the best-scoring four-hue set found by sweeping the OKLCH
# gamut (hue x chroma x lightness) against the data-viz validator on this
# page's panel surface (#111621), maximising the worse of the two
# colour-vision floors. The winning set passes all five checks on the
# ALL-PAIRS pairlist four simultaneous categorical hues requires -- worst CVD
# dE 9.5 (target >= 8), worst normal-vision dE 18.7 (floor >= 15), every slot
# >= 3:1 against the surface. Hues sit at roughly 250/50/160/330 degrees,
# evenly spread, at near-equal lightness so no position reads as "louder"
# than another.
#
# Colour follows the POSITION, never its rank or the filter state: switching
# which positions are shown must never repaint the survivors.
POSITION_COLORS = {
    "QB": "#1a83db",   # blue
    "RB": "#c85d00",   # orange
    "WR": "#2b9667",   # green
    "TE": "#9b5896",   # purple
    # K and DEF are NOT part of that validated set, and deliberately so: six
    # categorical hues cannot pass the all-pairs floors (checked -- the best
    # six-hue attempt fails CVD separation and the normal-vision floor). They
    # never need to: neither appears in the efficiency-leaders table (only
    # QB/RB/WR/TE have a rate stat to rank there), the only place colour
    # carries identity on its own. These two are UI accents only, always
    # shown as a dot immediately beside the position's own text label, where
    # colour is reinforcement rather than the encoding.
    "K": "#7f8ea3",    # cool grey
    "DEF": "#8d7350",  # warm brown
}
POSITION_FALLBACK_COLOR = "#9CA3AF"

# Efficiency-leaders table: one Sleeper season-TOTAL stat pair per position --
# a volume field the qualifying threshold is measured on, and the yards field
# that, divided by volume, is the rate the table ranks by. top_n is that
# position's qualifying pool size (by volume, per season) -- RB/rush attempts,
# WR & TE/receptions ("catches"), QB/pass attempts, matching what was asked
# for: "top 50 RBs, top 75 WR, top 30 TE, top 30 QB".
LEADER_CFG = {
    "QB": {"vol_field": "pass_att", "yard_field": "pass_yd", "top_n": 30,
           "vol_label": "Pass Att", "yard_label": "Pass Yds", "eff_label": "Yds / Att"},
    "RB": {"vol_field": "rush_att", "yard_field": "rush_yd", "top_n": 50,
           "vol_label": "Rush Att", "yard_label": "Rush Yds", "eff_label": "Yds / Carry"},
    "WR": {"vol_field": "rec", "yard_field": "rec_yd", "top_n": 75,
           "vol_label": "Receptions", "yard_label": "Rec Yds", "eff_label": "Yds / Catch"},
    "TE": {"vol_field": "rec", "yard_field": "rec_yd", "top_n": 30,
           "vol_label": "Receptions", "yard_label": "Rec Yds", "eff_label": "Yds / Catch"},
}
DEFAULT_LEADER_SEASONS = 3


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

def display_name(info, pid):
    """A usable name for any Sleeper entry. Team defenses are the awkward case:
    their player_id IS the team abbreviation and `full_name` is often absent,
    so falling back to first/last and finally to the id keeps a DEF from being
    silently dropped by a `if not name: continue` further down."""
    name = info.get("full_name")
    if name:
        return name.strip()
    parts = [info.get("first_name"), info.get("last_name")]
    joined = " ".join(p.strip() for p in parts if p)
    if joined:
        return joined
    if (info.get("position") or "") == "DEF":
        return f"{pid} Defense"
    return str(pid)


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
              rank_by=DEFAULT_RANK_BY, rank_week=None, group="skill"):
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
        # A team defense's own team is itself; Sleeper doesn't always fill in
        # the `team` field for those entries.
        team = info.get("team") or (pid if pos == "DEF" else None) or "FA"
        rows.append({
            "pid": pid, "name": display_name(info, pid), "pos": pos, "team": team,
            "group": group,
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
            "pid": pid, "name": display_name(info, pid), "pos": info.get("position") or "?",
            "team": info.get("team") or "FA", "group": "skill", "rank_pts": None, "games": None,
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

    This one list feeds both trend grids directly. Nothing derived (points
    per snap, per-game averages, RSI) is stored -- all of it is arithmetic
    the browser can do on the fly, and shipping it would multiply the
    payload for no new information.

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
# Efficiency leaders -- season-total volume vs. yield, thresholded by volume
# ---------------------------------------------------------------------------

_SEASON_STATS_CACHE = {}


def get_season_stats(season):
    """One season's TOTAL stats for every NFL player who recorded any:
    player_id -> {pass_att, pass_yd, rush_att, rush_yd, rec, rec_yd, ...}.
    Sleeper aggregates the whole regular season server-side, so this is ONE
    call per season regardless of how many weeks are in it -- the same
    endpoint shape as get_week_stats() but with no week in the path. Cached
    in-process since nothing else needs a second fetch of the same season."""
    key = str(season)
    if key in _SEASON_STATS_CACHE:
        return _SEASON_STATS_CACHE[key]
    try:
        data = fetch_json(f"{SLEEPER_BASE}/stats/nfl/regular/{season}") or {}
    except Exception as e:
        print(f"  [warn] season-stats fetch failed for {season}: {e}", file=sys.stderr)
        data = {}
    _SEASON_STATS_CACHE[key] = data
    return data


def build_leader_pool(players_dir, leader_seasons):
    """Efficiency-leaders payload for the table under the trend grids.

    For each position in LEADER_CFG and each season in `leader_seasons`,
    ranks every player at that position by that season's VOLUME stat (rush
    attempts for RB, receptions for WR/TE, pass attempts for QB) and keeps
    the top `top_n` -- thresholding on volume rather than on the rate itself,
    so a two-carry, 38-yard game can never crowd out a real workload. A
    player qualifies for the table if they cleared that bar in AT LEAST ONE
    of the fetched seasons; the browser only ever shows a season (or a sum of
    seasons) where a player actually cleared it, via the per-season
    `qualified` flag shipped alongside their raw counting stats.

    Returns a list of {pid, n, p, t, s} where `s` is a list of
    [volume, yards, qualified(0/1)] triples, one per season in
    `leader_seasons`, in that same order (a season with no data for the
    player is [0, 0, 0]) -- everything the browser needs to slice by year or
    sum across years without any further network calls."""
    season_stats = {s: get_season_stats(s) for s in leader_seasons}

    # (season, position) -> set of qualifying player_ids, by that season's volume.
    qualified_by_season_pos = {}
    for season, stats in season_stats.items():
        for pos, cfg in LEADER_CFG.items():
            ranked = []
            for pid, srow in stats.items():
                info = players_dir.get(pid)
                if not info or info.get("position") != pos:
                    continue
                vol = srow.get(cfg["vol_field"]) or 0
                if vol > 0:
                    ranked.append((pid, vol))
            ranked.sort(key=lambda r: -r[1])
            qualified_by_season_pos[(season, pos)] = {pid for pid, _ in ranked[: cfg["top_n"]]}

    union_pids = set()
    for pids in qualified_by_season_pos.values():
        union_pids |= pids

    out = []
    for pid in union_pids:
        info = players_dir.get(pid) or {}
        pos = info.get("position")
        cfg = LEADER_CFG.get(pos)
        if not cfg:
            continue
        per_season = []
        for season in leader_seasons:
            srow = season_stats.get(season, {}).get(pid) or {}
            vol = srow.get(cfg["vol_field"]) or 0
            yards = srow.get(cfg["yard_field"]) or 0
            qualified = pid in qualified_by_season_pos.get((season, pos), set())
            per_season.append([round(float(vol), 1), round(float(yards), 1), 1 if qualified else 0])
        out.append({
            "pid": pid, "n": display_name(info, pid), "p": pos,
            "t": info.get("team") or "FA", "s": per_season,
        })
    return out


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE_CSS = """
  :root{
    --bg:#0B0E14; --panel:#111621; --line:#1E2633;
    --ink:#E8E6DE; --dim:#6B7280; --cyan:#4FD8E8; --amber:#D9A441;
    --violet:#A78BFA; --rsi:#FF8A65;
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
  .filter-btn.tiny{ padding:4px 8px; font-size:10px; letter-spacing:.02em; }
  /* 32 teams would dominate the sticky header, so the bar collapses. The
     summary always states the current selection, so a filter is never
     silently active behind a closed panel. */
  .team-details{ margin:10px 0 6px; }
  .team-details summary{
    display:flex; align-items:center; gap:8px; cursor:pointer; list-style:none;
    padding:2px 0; user-select:none;
  }
  .team-details summary::-webkit-details-marker{ display:none; }
  .team-details summary::before{
    content:'▸'; color:var(--dim); font-size:10px; transition:transform .15s ease; display:inline-block;
  }
  .team-details[open] summary::before{ transform:rotate(90deg); }
  .team-summary{ font-size:11.5px; color:var(--cyan); }
  .team-grid{ margin:8px 0 2px; gap:5px; max-width:1100px; }
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
  .dot{ width:9px; height:9px; border-radius:50%; display:inline-block; flex-shrink:0; }
  /* RSI(5) sub-panel, drawn beneath the main line on every small-multiple card */
  .rsi-line{ fill:none; stroke:var(--rsi); stroke-width:1.1; stroke-opacity:.9; }
  .rsi-band{ stroke:var(--rsi); stroke-opacity:.28; stroke-width:1; stroke-dasharray:1,2; }
  .rsi-title{ fill:var(--rsi); font-size:9px; font-family:'IBM Plex Mono', monospace; letter-spacing:.04em; }
  .rsi-dot{ fill:var(--rsi); }
  .rsi-lastlabel{ fill:var(--rsi); font-size:8.5px; font-family:'IBM Plex Mono', monospace; }
  /* efficiency leaders table */
  .lead-controls{ display:flex; gap:8px; margin:10px 0 6px; flex-wrap:wrap; align-items:center; }
  .lead-table-wrap{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; }
  table.lead-table{ width:100%; border-collapse:collapse; font-size:11.5px; min-width:640px; }
  table.lead-table th, table.lead-table td{ padding:8px 12px; text-align:left; white-space:nowrap; }
  table.lead-table thead th{
    background:var(--panel); color:var(--dim); font-size:10px; letter-spacing:.06em; text-transform:uppercase;
    border-bottom:1px solid var(--line); position:sticky; top:0; cursor:pointer; user-select:none;
  }
  table.lead-table thead th:hover{ color:var(--ink); }
  table.lead-table thead th.sorted{ color:var(--cyan); }
  table.lead-table tbody tr{ border-bottom:1px solid var(--line); cursor:pointer; }
  table.lead-table tbody tr:hover{ background:rgba(255,255,255,.03); }
  table.lead-table tbody tr:focus-visible{ outline:1px solid var(--cyan); outline-offset:-1px; }
  table.lead-table tbody tr.pinned{ background:rgba(79,216,232,.08); box-shadow:inset 2px 0 0 var(--cyan); }
  table.lead-table tbody tr.pinned:hover{ background:rgba(79,216,232,.14); }
  table.lead-table tbody tr.empty-note, table.lead-table tbody tr:has(.empty-note){ cursor:default; }
  table.lead-table td.num{ color:var(--ink); font-weight:600; }
  table.lead-table td.rank{ color:var(--dim); }
  table.lead-table td.eff{ color:var(--cyan); font-weight:700; }
"""

PAGE_JS = r"""
const TREND_GAMES = __TREND_GAMES__;
const POOL = __POOL__;
const SEASONS = __SEASONS__;
const TOP_N = __TOP_N__;
const SNAP_R = __SNAP_BOUNDS__;      // [min,max] snaps across every game -- small-multiple bubble scale
const GLOBAL_MAX = __GLOBAL_MAX__;   // {pts,pps} report-wide maxima for shared y-scaling
const POS_COLORS = __POS_COLORS__;
const POS_FALLBACK = __POS_FALLBACK__;
// Positions with no offensive snaps: they appear in the points grid but are
// excluded from the points-per-snap grid by construction.
const SNAPLESS = new Set(__SNAPLESS__);
function posColor(pos){ return POS_COLORS[pos] || POS_FALLBACK; }
function hasSnaps(pos){ return !SNAPLESS.has(pos); }

const G_SEASON = 0, G_WEEK = 1, G_PTS = 2, G_SNAPS = 3, G_TOUCHES = 4;
const BY_PID = {};
POOL.forEach(function(p){ BY_PID[p.pid] = p; });

// ---------------------------------------------------------------------------
// Shared filter state. One predicate drives both grids, so what you see in
// one is always the same set of players as the other.
// ---------------------------------------------------------------------------
let searchText = '';
const pinned = new Set();         // individually chosen players
const selectedPos = new Set();    // chosen positions; empty means "none chosen"
const selectedTeam = new Set();   // chosen teams; empty means "none chosen"

function matchesSearch(p, q){
  return (p.n + ' ' + p.t + ' ' + p.p).toLowerCase().indexOf(q) !== -1;
}

// Every selector is multi-select. The two ATTRIBUTE selectors -- position and
// team -- are OR within themselves and AND with each other, which is how
// people actually read them: "RB + TE" and "KC + BUF" together means the RBs
// and TEs on those two teams, not every RB in the league plus everyone in
// Kansas City.
function matchesAttrs(p){
  if (selectedPos.size && !selectedPos.has(p.p)) return false;
  if (selectedTeam.size && !selectedTeam.has(p.t)) return false;
  return true;
}

// PINNED PLAYERS are a different kind of choice -- a named individual, not an
// attribute -- so they are added on top rather than intersected. Pin a WR while
// filtering to QBs and you get the quarterbacks AND him; nothing you explicitly
// asked for is ever removed by something else you asked for.
//
//   * nothing chosen           -> the default top-N slice, so the page opens usable
//   * attributes only          -> every player matching them
//   * players only             -> exactly those players
//   * attributes + players     -> the attribute set plus the pinned players
//
// Typing in the search box is a temporary lookup rather than a selection: it
// searches the WHOLE pool (otherwise you could never find a player outside the
// current selection) while keeping pinned players visible so you don't lose the
// set you were assembling.
function passesFilter(p){
  if (searchText) return matchesSearch(p, searchText) || pinned.has(p.pid);
  const hasPins = pinned.size > 0;
  const hasAttrs = selectedPos.size > 0 || selectedTeam.size > 0;
  // The unfiltered default is the top-N SKILL players. Kickers and defenses
  // have their own small caps and are always one click away by position, but
  // they would otherwise take 30 of the 40 default slots in some weeks.
  if (!hasPins && !hasAttrs) return p.g === 'skill' && p.k <= TOP_N;
  return (hasPins && pinned.has(p.pid)) || (hasAttrs && matchesAttrs(p));
}
function visiblePlayers(){ return POOL.filter(passesFilter); }

function esc(s){
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function mean(a){ return a.reduce(function(x, y){ return x + y; }, 0) / a.length; }
function niceTicks(hi, n){
  const out = [];
  for (let i = 0; i <= n; i++) out.push(hi * i / n);
  return out;
}

// ===========================================================================
// PANELS 2 & 3 -- small multiples
// ===========================================================================
const GEO = { W: 440, H: 170, ML: 34, MR: 10, MT: 12, MB: 20 };
const R_MIN = 1.8, R_MAX = 5.2;
const scaleMode = { pts: 'auto', pps: 'auto' };

// RSI(5) -- Wilder's relative strength index, period 5 rather than the usual
// stock-market 14 (a game log is far shorter than a price history). Drawn as
// its own sub-panel underneath the main chart rather than overlaid on a
// second axis: an overlay's "RSI NN" legend and right-axis ticks collided
// with the existing peak/last-value labels on cards where those happened to
// sit near the top of the chart. A dedicated panel with its own 0-100 axis
// has no such collisions.
const RSI_PERIOD = 5;
const RSI_H = 42, RSI_TOP_GAP = 14, RSI_LABEL_GAP = 12, RSI_BOTTOM_PAD = 6;

// Computed only over the REAL (non-gap) values, in the order they actually
// occurred -- a missing week (no snap data, a bye) is skipped rather than
// treated as a zero-change day, which would understate momentum either way.
function computeRSI(vals, period){
  const out = new Array(vals.length).fill(null);
  const idxs = [], series = [];
  vals.forEach(function(v, i){
    if (v !== null && v !== undefined){ idxs.push(i); series.push(v); }
  });
  if (series.length <= period) return out;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++){
    const d = series[i] - series[i - 1];
    if (d >= 0) gain += d; else loss -= d;
  }
  let avgGain = gain / period, avgLoss = loss / period;
  function rsiOf(ag, al){
    if (al === 0) return ag === 0 ? 50 : 100;
    return 100 - 100 / (1 + ag / al);
  }
  out[idxs[period]] = rsiOf(avgGain, avgLoss);
  for (let i = period + 1; i < series.length; i++){
    const d = series[i] - series[i - 1];
    const gn = d > 0 ? d : 0, ls = d < 0 ? -d : 0;
    avgGain = (avgGain * (period - 1) + gn) / period;
    avgLoss = (avgLoss * (period - 1) + ls) / period;
    out[idxs[i]] = rsiOf(avgGain, avgLoss);
  }
  return out;
}

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

function renderChart(svg, gamesIn, metric, pos){
  const W = GEO.W, H = GEO.H, ML = GEO.ML, MR = GEO.MR, MT = GEO.MT, MB = GEO.MB;
  const PW = W - ML - MR, PH = H - MT - MB;
  const games = gamesIn;

  const vals = games.map(function(g){ return valueOf(g, metric); });
  const real = vals.filter(function(v){ return v !== null && v !== undefined; });
  if (real.length < 2){
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    // Distinguish "we have no snap numbers for this player" from "this
    // position doesn't take offensive snaps at all" -- the second isn't a
    // data gap, it's the metric not applying.
    let msg = 'no data yet';
    if (metric === 'pps') msg = SNAPLESS.has(pos)
      ? (pos === 'K' ? 'kickers take no offensive snaps' : 'team defenses take no offensive snaps')
      : 'no snap data';
    svg.innerHTML = '<text class="empty" x="' + (W / 2) + '" y="' + (H / 2) +
      '" text-anchor="middle">' + msg + '</text>';
    return;
  }

  // RSI sub-panel sits below the main plot, in its own band of the same SVG,
  // so the viewBox has to grow to fit both.
  const rTop = H + RSI_TOP_GAP + RSI_LABEL_GAP;
  const rBottom = rTop + RSI_H;
  const totalH = rBottom + RSI_BOTTOM_PAD;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + totalH);

  // Both quantities floor at zero, so the axis starts at zero -- starting at
  // the player's own minimum turns ordinary noise into a cliff.
  const lo = 0;
  const hi = scaleMode[metric] === 'shared' ? GLOBAL_MAX[metric] : Math.max.apply(null, real);
  const span = (hi - lo) || 1;
  const xOf = function(i){ return ML + (games.length === 1 ? PW / 2 : (i / (games.length - 1)) * PW); };
  const yOf = function(v){ return MT + (1 - (v - lo) / span) * PH; };

  const rsiVals = computeRSI(vals, RSI_PERIOD);
  const lastRsiIdx = rsiVals.reduce(function(acc, v, idx){ return v === null ? acc : idx; }, -1);
  const yOfRsi = function(v){ return rTop + (1 - v / 100) * RSI_H; };

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

  // RSI(5) sub-panel furniture: title, 0/30/70/100 reference lines, the two
  // band lines that mark the conventional oversold/overbought thresholds.
  // Skipped entirely (no empty axis drawn) when there isn't enough real data
  // yet for even one RSI reading.
  if (lastRsiIdx >= 0){
    parts.push('<text class="rsi-title" x="' + ML + '" y="' + (rTop - 4) + '">RSI(' + RSI_PERIOD + ')</text>');
    [0, 30, 70, 100].forEach(function(lvl){
      const y = yOfRsi(lvl);
      parts.push('<line class="' + (lvl === 30 || lvl === 70 ? 'rsi-band' : 'grid') + '" x1="' + ML +
        '" y1="' + y.toFixed(1) + '" x2="' + (W - MR) + '" y2="' + y.toFixed(1) + '"/>');
      parts.push('<text class="ax" x="' + (ML - 5) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + lvl + '</text>');
    });
    parts.push('<line class="axline" x1="' + ML + '" y1="' + rTop + '" x2="' + ML + '" y2="' + rBottom + '"/>');
  }

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
    let runR = [];
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
      // Same "force a break at season boundary" trick as the value line above,
      // so the RSI line never bridges across a season gap either.
      const rv = (k < j) ? rsiVals[k] : null;
      if (rv === null || rv === undefined){
        if (runR.length > 1){
          g.push('<path class="rsi-line" d="M ' + runR.map(function(p){ return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' L ') + '"/>');
        }
        runR = [];
      } else {
        runR.push([xOf(k), yOfRsi(rv)]);
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

  if (lastRsiIdx >= 0){
    parts.push('<circle class="rsi-dot" cx="' + xOf(lastRsiIdx).toFixed(1) + '" cy="' +
      yOfRsi(rsiVals[lastRsiIdx]).toFixed(1) + '" r="1.8"/>');
    parts.push('<text class="rsi-lastlabel" x="' + xOf(lastRsiIdx).toFixed(1) + '" y="' +
      Math.max(rTop + 7, yOfRsi(rsiVals[lastRsiIdx]) - 5).toFixed(1) + '" text-anchor="end">' +
      rsiVals[lastRsiIdx].toFixed(0) + '</text>');
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
              card.getAttribute('data-metric'), card.getAttribute('data-pos'));
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
  const idx = +circle.getAttribute('data-i');
  const g = games[idx];
  if (!g) return;
  const pps = g[G_SNAPS] ? (g[G_PTS] / g[G_SNAPS]) : null;
  const metric = card.getAttribute('data-metric');
  const vals = games.map(function(gg){ return valueOf(gg, metric); });
  const rsiVals = computeRSI(vals, RSI_PERIOD);
  const rsi = rsiVals[idx];
  const el = card.querySelector('.trend-readout');
  el.innerHTML = '<b>' + g[G_SEASON] + ' wk' + g[G_WEEK] + ':</b> ' + g[G_PTS].toFixed(1) + ' pt · ' +
    (g[G_SNAPS] === null ? 'snaps n/a' : g[G_SNAPS] + ' snaps') + ' · ' +
    (pps === null ? 'pt/snap n/a' : pps.toFixed(3) + ' pt/snap') + ' · ' + g[G_TOUCHES] + ' touches' +
    (rsi === null || rsi === undefined ? '' : ' · RSI(' + RSI_PERIOD + ') ' + rsi.toFixed(0));
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
    const pos = b.getAttribute('data-pos');
    b.classList.toggle('active', pos === 'ALL' ? selectedPos.size === 0 : selectedPos.has(pos));
  });
  document.querySelectorAll('#teamFilter .filter-btn').forEach(function(b){
    const t = b.getAttribute('data-team');
    b.classList.toggle('active', t === 'ALL' ? selectedTeam.size === 0 : selectedTeam.has(t));
  });
  document.getElementById('teamSummary').textContent = selectedTeam.size
    ? Array.from(selectedTeam).sort().join(', ')
    : 'all teams';

  let note;
  if (searchText) note = vis.size + ' matching "' + searchText + '" (searches the whole pool)';
  else if (!pinned.size && !selectedPos.size && !selectedTeam.size)
    note = 'top ' + TOP_N + ' of the pool — pick a position, team or player to change the set';
  else {
    const attrs = [];
    if (selectedPos.size) attrs.push(Array.from(selectedPos).join('/'));
    if (selectedTeam.size) attrs.push(Array.from(selectedTeam).sort().join('/'));
    const bits = [];
    if (attrs.length) bits.push(attrs.join(' on '));
    if (pinned.size) bits.push(pinned.size + ' pinned player' + (pinned.size === 1 ? '' : 's'));
    note = vis.size + ' shown: ' + bits.join(' + ');
  }
  document.getElementById('posCount').textContent = note;
  document.querySelectorAll('.gridCount').forEach(function(el){ el.textContent = vis.size + ' cards'; });
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
  // The leaders table (defined further down this script) highlights pinned
  // rows and needs to know when a pin changed from anywhere else -- a chip's
  // ×, "Clear all", or a click in the table itself. Declared with `function`,
  // so it's hoisted and safe to call here regardless of source order: by the
  // time a click actually fires this, the whole script has already run once.
  if (typeof renderLeaderTable === 'function') renderLeaderTable();
}
document.getElementById('pinChips').addEventListener('click', function(e){
  if (e.target.id === 'clearPins'){
    pinned.clear();
    renderChips();
    applyFilters();
    if (typeof renderLeaderTable === 'function') renderLeaderTable();
    return;
  }
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

// Positions and teams are both multi-select: each button toggles, ALL clears.
function toggleInto(set, value){
  if (value === 'ALL') set.clear();
  else if (set.has(value)) set.delete(value);
  else set.add(value);
  applyFilters();
}
document.getElementById('posFilter').addEventListener('click', function(e){
  const btn = e.target.closest('.filter-btn');
  if (btn) toggleInto(selectedPos, btn.getAttribute('data-pos'));
});
document.getElementById('teamFilter').addEventListener('click', function(e){
  const btn = e.target.closest('.filter-btn');
  if (btn) toggleInto(selectedTeam, btn.getAttribute('data-team'));
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
  applyFilters();
})();

// ===========================================================================
// EFFICIENCY LEADERS -- season-total volume vs. yield, filterable by
// position and by season. Independent of the pool/filter machinery above:
// this table draws from its own smaller LEADERS payload, not TREND_GAMES.
// ===========================================================================
const LEADER_SEASONS = __LEADER_SEASONS__;   // newest season first
const LEADERS = __LEADER_PLAYERS__;
const LEADER_CFG = __LEADER_CFG__;

let leadPos = 'QB';
let leadYear = 'last2';
let leadSort = 'eff';
let leadSortDir = -1;
const leadTeams = new Set();   // chosen teams; empty means "no filter, show all"
let leadCurrentRows = [];      // exactly what's on screen right now -- copy/download read this

// One row per qualifying player for the chosen (position, year, team) slice.
// An individual year uses that season's own server-computed `qualified` flag
// (an exact top-N-by-volume for that year); "last2"/"last3" sum volume and
// yards across the most recent N seasons for every player who qualified in
// at least one of them, then RE-APPLIES that position's top-N threshold to
// the combined volume -- the same rule, just measured on the combined
// number instead of a single season's. The team filter (multi-select, OR
// within itself) narrows AFTER that threshold is applied -- picking a team
// shows who on that roster made the qualified cut, not a second, looser
// pass restricted to that team.
function leaderRows(pos, year){
  const cfg = LEADER_CFG[pos];
  if (!cfg) return [];
  const players = LEADERS.filter(function(p){ return p.p === pos; });
  let rows = [];
  const idx = LEADER_SEASONS.indexOf(year);
  if (idx !== -1){
    players.forEach(function(p){
      const s = p.s[idx];
      if (!s || !s[2]) return;
      rows.push({ pid: p.pid, n: p.n, t: p.t, vol: s[0], yards: s[1] });
    });
  } else {
    const n = year === 'last3' ? 3 : 2;
    const idxs = [];
    for (let k = 0; k < Math.min(n, LEADER_SEASONS.length); k++) idxs.push(k);
    players.forEach(function(p){
      let vol = 0, yards = 0, any = false;
      idxs.forEach(function(k){
        const s = p.s[k];
        if (!s) return;
        vol += s[0]; yards += s[1];
        if (s[2]) any = true;
      });
      if (any && vol > 0) rows.push({ pid: p.pid, n: p.n, t: p.t, vol: vol, yards: yards });
    });
    rows.sort(function(a, b){ return b.vol - a.vol; });
    rows = rows.slice(0, cfg.top_n);
  }
  rows.forEach(function(r){ r.eff = r.vol ? r.yards / r.vol : 0; });
  if (leadTeams.size) rows = rows.filter(function(r){ return leadTeams.has(r.t); });
  return rows;
}

function renderLeaderTable(){
  const cfg = LEADER_CFG[leadPos];
  const head = document.getElementById('leadHead');
  const body = document.getElementById('leadBody');
  if (!head || !body || !cfg) return;

  const cols = [
    { key: 'rank', label: '#' },
    { key: 'n', label: 'Player' },
    { key: 't', label: 'Team' },
    { key: 'vol', label: cfg.vol_label },
    { key: 'yards', label: cfg.yard_label },
    { key: 'eff', label: cfg.eff_label },
  ];
  head.innerHTML = cols.map(function(c){
    const sorted = leadSort === c.key;
    return '<th data-sort="' + c.key + '" class="' + (sorted ? 'sorted' : '') + '">' + esc(c.label) +
      (sorted ? (leadSortDir === -1 ? ' ↓' : ' ↑') : '') + '</th>';
  }).join('');

  let rows = leaderRows(leadPos, leadYear);
  if (leadSort === 'n' || leadSort === 't'){
    rows.sort(function(a, b){ return leadSortDir * String(a[leadSort]).localeCompare(String(b[leadSort])); });
  } else if (leadSort === 'rank'){
    rows.sort(function(a, b){ return b.eff - a.eff; });
  } else {
    rows.sort(function(a, b){ return leadSortDir * (a[leadSort] - b[leadSort]); });
  }

  leadCurrentRows = rows;

  if (!rows.length){
    const teamNote = leadTeams.size ? ' on ' + Array.from(leadTeams).sort().join('/') : '';
    body.innerHTML = '<tr><td colspan="6" class="empty-note">No qualified ' + esc(leadPos) +
      's' + teamNote + ' for this window yet.</td></tr>';
    return;
  }
  // Rows are clickable: pinning a leader here uses the SAME `pinned` set the
  // search box and pin chips use, so a player picked from this table is
  // filtered into both trend grids above (and stays pinned if you switch
  // position/year/team here, or vice versa). A row only pins if the player
  // has a card to reveal -- see the pool-union note in build_leader_pool's
  // caller in fantasy_flow.py's main(), which folds every leaders-table
  // player into the trend pool for exactly this reason.
  body.innerHTML = rows.map(function(r, idx){
    const isPinned = pinned.has(r.pid);
    return '<tr data-pid="' + esc(r.pid) + '" class="' + (isPinned ? 'pinned' : '') + '" tabindex="0" role="button" ' +
      'aria-pressed="' + isPinned + '" aria-label="' + (isPinned ? 'Unpin ' : 'Pin ') + esc(r.n) + '">' +
      '<td class="rank">' + (idx + 1) + '</td><td>' + esc(r.n) + '</td><td>' + esc(r.t) +
      '</td><td class="num">' + r.vol.toFixed(0) + '</td><td class="num">' + r.yards.toFixed(0) +
      '</td><td class="eff">' + r.eff.toFixed(2) + '</td></tr>';
  }).join('');
}

const leadPosFilterEl = document.getElementById('leadPosFilter');
if (leadPosFilterEl){
  leadPosFilterEl.addEventListener('click', function(e){
    const b = e.target.closest('.filter-btn[data-lead-pos]');
    if (!b) return;
    leadPos = b.getAttribute('data-lead-pos');
    leadSort = 'eff'; leadSortDir = -1;
    leadPosFilterEl.querySelectorAll('.filter-btn').forEach(function(x){ x.classList.toggle('active', x === b); });
    renderLeaderTable();
  });
}
const leadYearFilterEl = document.getElementById('leadYearFilter');
if (leadYearFilterEl){
  leadYearFilterEl.addEventListener('click', function(e){
    const b = e.target.closest('.filter-btn[data-year]');
    if (!b) return;
    leadYear = b.getAttribute('data-year');
    leadYearFilterEl.querySelectorAll('.filter-btn').forEach(function(x){ x.classList.toggle('active', x === b); });
    renderLeaderTable();
  });
}
const leadHeadEl = document.getElementById('leadHead');
if (leadHeadEl){
  leadHeadEl.addEventListener('click', function(e){
    const th = e.target.closest('th[data-sort]');
    if (!th) return;
    const key = th.getAttribute('data-sort');
    if (leadSort === key) leadSortDir *= -1;
    else { leadSort = key; leadSortDir = (key === 'n' || key === 't') ? 1 : -1; }
    renderLeaderTable();
  });
}
const leadBodyEl = document.getElementById('leadBody');
if (leadBodyEl){
  leadBodyEl.addEventListener('click', function(e){
    const row = e.target.closest('tr[data-pid]');
    if (row) togglePin(row.getAttribute('data-pid'));
  });
  // Rows are keyboard-focusable (tabindex + role="button" set when they're
  // drawn) so pinning from the table doesn't require a mouse.
  leadBodyEl.addEventListener('keydown', function(e){
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const row = e.target.closest('tr[data-pid]');
    if (!row) return;
    e.preventDefault();
    togglePin(row.getAttribute('data-pid'));
  });
}
function syncLeadTeamUI(){
  const wrap = document.getElementById('leadTeamFilter');
  const summary = document.getElementById('leadTeamSummary');
  if (!wrap || !summary) return;
  wrap.querySelectorAll('.filter-btn').forEach(function(b){
    const t = b.getAttribute('data-lead-team');
    b.classList.toggle('active', t === 'ALL' ? leadTeams.size === 0 : leadTeams.has(t));
  });
  summary.textContent = leadTeams.size ? Array.from(leadTeams).sort().join(', ') : 'all teams';
}
const leadTeamFilterEl = document.getElementById('leadTeamFilter');
if (leadTeamFilterEl){
  leadTeamFilterEl.addEventListener('click', function(e){
    const b = e.target.closest('.filter-btn[data-lead-team]');
    if (!b) return;
    const t = b.getAttribute('data-lead-team');
    if (t === 'ALL') leadTeams.clear();
    else if (leadTeams.has(t)) leadTeams.delete(t);
    else leadTeams.add(t);
    syncLeadTeamUI();
    renderLeaderTable();
  });
}

// --- copy / download: exactly the rows currently on screen, in the current
// sort order, not a re-query of the full pool -- what you copy is what you see.
function leadExportRows(){
  const cfg = LEADER_CFG[leadPos];
  if (!cfg) return [];
  const header = ['#', 'Player', 'Team', cfg.vol_label, cfg.yard_label, cfg.eff_label];
  const body = leadCurrentRows.map(function(r, idx){
    return [String(idx + 1), r.n, r.t, r.vol.toFixed(0), r.yards.toFixed(0), r.eff.toFixed(2)];
  });
  return [header].concat(body);
}
function leadCSVCell(s){
  s = String(s);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function leadShowStatus(msg){
  const el = document.getElementById('leadCopyStatus');
  if (!el) return;
  el.textContent = msg;
  clearTimeout(leadShowStatus._t);
  leadShowStatus._t = setTimeout(function(){ el.textContent = ''; }, 2200);
}
const leadCopyBtn = document.getElementById('leadCopyBtn');
if (leadCopyBtn){
  leadCopyBtn.addEventListener('click', function(){
    // Tab-separated so a paste into a spreadsheet lands in one cell per
    // column instead of one comma-separated blob in column A.
    const tsv = leadExportRows().map(function(row){ return row.join('\t'); }).join('\n');
    const done = function(){ leadShowStatus('Copied ' + leadCurrentRows.length + ' rows'); };
    const fail = function(){
      const ta = document.createElement('textarea');
      ta.value = tsv;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e){ ok = false; }
      document.body.removeChild(ta);
      leadShowStatus(ok ? 'Copied ' + leadCurrentRows.length + ' rows' : 'Copy failed -- select the table and copy manually');
    };
    if (navigator.clipboard && window.isSecureContext){
      navigator.clipboard.writeText(tsv).then(done, fail);
    } else {
      fail();
    }
  });
}
const leadDownloadBtn = document.getElementById('leadDownloadBtn');
if (leadDownloadBtn){
  leadDownloadBtn.addEventListener('click', function(){
    const csv = leadExportRows().map(function(row){ return row.map(leadCSVCell).join(','); }).join('\r\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'efficiency-leaders-' + leadPos + '-' + leadYear + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  });
}

renderLeaderTable();
"""


def render_html(pool, trend_series, seasons, seasons_label, scoring_label, ranking_season, rank_week,
                rank_by, pool_size, top_n, selection_mode, watchlist_missing, now, snap_cov,
                leader_seasons=(), leader_players=()):
    """Self-contained page: two grids of small-multiple trend charts (each line
    carrying its own RSI(5) sub-panel), all sharing a position filter, a player
    search/pin filter and a hover readout, plus a filterable season-total
    efficiency-leaders table. Every chart is drawn client-side from the one
    TREND_GAMES payload; the leaders table from the smaller LEADERS payload."""
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

    # Each button carries the position's own colour as a dot, so the filter bar
    # and the plot teach the same key.
    filter_buttons = '<button class="filter-btn active" data-pos="ALL">ALL</button>' + "".join(
        f'<button class="filter-btn" data-pos="{html.escape(pos)}">'
        f'<span class="dot" style="background:{POSITION_COLORS.get(pos, POSITION_FALLBACK_COLOR)}"></span> '
        f'{html.escape(pos)} <span style="opacity:.6">{pos_counts.get(pos, 0)}</span></button>'
        for pos in all_positions
    )
    # Teams come from whoever is actually in the pool, not a hardcoded league
    # list -- so the bar never offers a team with nothing behind it, and a
    # relocation or expansion team needs no code change.
    team_counts = {}
    for p in pool:
        team_counts[p["team"]] = team_counts.get(p["team"], 0) + 1
    all_teams = sorted(team_counts)
    team_buttons = "".join(
        f'<button class="filter-btn tiny" data-team="{html.escape(t)}">{html.escape(t)} '
        f'<span style="opacity:.55">{team_counts[t]}</span></button>'
        for t in all_teams
    )
    # Efficiency-leaders position/year controls. Position order follows the same
    # QB/RB/WR/TE convention as the rest of the page; QB opens active so the
    # table never opens empty (every fetched season has QB volume).
    leader_positions = [p for p in POSITION_ORDER if p in LEADER_CFG]
    leader_pos_buttons = "".join(
        f'<button class="filter-btn{" active" if pos == "QB" else ""}" data-lead-pos="{html.escape(pos)}">'
        f'<span class="dot" style="background:{POSITION_COLORS.get(pos, POSITION_FALLBACK_COLOR)}"></span> '
        f'{html.escape(pos)}</button>'
        for pos in leader_positions
    )
    leader_year_buttons = "".join(
        f'<button class="filter-btn" data-year="{html.escape(s)}">{html.escape(s)}</button>'
        for s in leader_seasons
    )
    # Teams come from whoever actually shows up in the leaders pool -- not
    # `all_teams` above, which is scoped to the (differently-sized, possibly
    # differently-filtered) trend-grid pool. Multi-select, OR within itself,
    # same convention as the main team filter: pick two teams and see the
    # qualified leaders from either.
    leader_team_counts = {}
    for p in leader_players:
        leader_team_counts[p["t"]] = leader_team_counts.get(p["t"], 0) + 1
    leader_teams = sorted(leader_team_counts)
    leader_team_buttons = "".join(
        f'<button class="filter-btn tiny" data-lead-team="{html.escape(t)}">{html.escape(t)} '
        f'<span style="opacity:.55">{leader_team_counts[t]}</span></button>'
        for t in leader_teams
    )
    season_checkboxes = "".join(
        f'<label class="checkbox-btn"><input type="checkbox" class="season-cb" value="{html.escape(s)}" checked>'
        f' {html.escape(s)}</label>'
        for s in seasons
    )
    datalist = "".join(f'<option value="{html.escape(p["name"])}">' for p in pool)

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
            # Kickers and defenses are ranked inside their own group, so their
            # rank is labelled with it -- "#3" next to a WR's "#3" would read
            # as the same standing. "leader" entries aren't ranked by
            # anything at all -- they're efficiency-leaders players folded in
            # only so they have a card to reveal when pinned -- so they get
            # no rank badge, just the name.
            grp = entry.get("group", "skill")
            if grp == "leader":
                name_prefix = ""
            elif grp == "skill":
                name_prefix = f"#{entry['rank']} "
            else:
                name_prefix = f"{html.escape(grp)} #{entry['rank']} "
            out.append(f"""
      <div class="trend-card" data-pid="{html.escape(entry['pid'])}" data-pos="{html.escape(entry['pos'])}"
           data-rank="{entry['rank']}" data-metric="{metric}">
        <div class="trend-card-head">
          <span class="name">{name_prefix}{html.escape(entry['name'])}</span>
          <span class="meta"><span class="dot" style="background:{POSITION_COLORS.get(entry['pos'], POSITION_FALLBACK_COLOR)}"></span>
            {html.escape(entry['pos'])} &middot; {html.escape(entry['team'])}{stale_note}</span>
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
                     f'as gaps, never as zeros, on the points-per-snap grid.')
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
        selection_note = (f"Pool = top <b>{pool_size}</b> players by {basis}{caveat}. With nothing selected "
                          f"you see the top {top_n}; any position or team selection shows <b>every</b> "
                          f"matching player in the pool, and pinned players are added on top of that.")
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
  {len(pool)} players, {html.escape(seasons_label)}. One set of filters drives all three panels.
  <b>Position</b> and <b>team</b> are multi-select and narrow each other &mdash; pick RB and TE, pick two
  teams, and you get those positions on those teams. Selecting anything lifts the top-{top_n} cap, so a
  team shows <b>every</b> one of its players in the pool. <b>Pinned players</b> are added on top of
  whatever else is selected. Every chart below follows.
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
      <span class="hint">Enter (or click any mark) to pin &middot; position and team narrow each other, pins add on top</span>
    </div>
  </div>
  <div class="filter-bar" id="pinChips"></div>
  <div class="filter-bar" id="posFilter">
    <span class="filter-bar-label">Position</span>{filter_buttons}
    <span class="filter-count" id="posCount"></span>
  </div>
  <details class="team-details">
    <summary>
      <span class="filter-bar-label">Team</span>
      <span class="team-summary" id="teamSummary">all teams</span>
      <span class="hint">{len(all_teams)} teams in the pool &middot; click to expand</span>
    </summary>
    <div class="filter-bar team-grid" id="teamFilter">
      <button class="filter-btn tiny active" data-team="ALL">ALL</button>{team_buttons}
    </div>
  </details>
  <div class="filter-bar" id="seasonFilter">
    <span class="filter-bar-label">Seasons <span style="opacity:.6">(trend grids)</span></span>{season_checkboxes}
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

<div class="section">
  <div class="section-head">
    <h2>Efficiency Leaders</h2>
    <div class="sub">Season-total volume vs. yield from Sleeper's per-season stats. Each position is
    thresholded to a qualified volume pool before ranking by rate, so the list is never topped by a
    two-catch, 40-yard fluke: top <b>{LEADER_CFG['RB']['top_n']}</b> RBs by rush attempts, top
    <b>{LEADER_CFG['WR']['top_n']}</b> WRs and top <b>{LEADER_CFG['TE']['top_n']}</b> TEs by receptions,
    top <b>{LEADER_CFG['QB']['top_n']}</b> QBs by pass attempts &mdash; each within whichever season(s)
    are selected below. <b>Last 2 Seasons</b> (default) sums volume and yards across the two most recent
    seasons before ranking; an individual year re-applies that position's own threshold to that year
    alone. <b>Team</b> narrows the qualified list to one or more rosters (multi-select). Click a column
    header to sort, or <b>click a player row to pin them into the Weekly Points and Points Per Snap
    grids above</b> &mdash; click again (or the &times; on their chip) to unpin. Pin as many players as
    you like at once.</div>
  </div>
  <div class="lead-controls" id="leadPosFilter">
    <span class="filter-bar-label">Position</span>{leader_pos_buttons}
  </div>
  <div class="lead-controls" id="leadYearFilter">
    <span class="filter-bar-label">Seasons</span>
    <button class="filter-btn active" data-year="last2">Last 2 Seasons</button>
    <button class="filter-btn" data-year="last3">Last 3 Seasons</button>{leader_year_buttons}
  </div>
  <details class="team-details">
    <summary>
      <span class="filter-bar-label">Team</span>
      <span class="team-summary" id="leadTeamSummary">all teams</span>
      <span class="hint">{len(leader_teams)} teams in this table &middot; click to expand</span>
    </summary>
    <div class="lead-controls team-grid" id="leadTeamFilter">
      <button class="filter-btn tiny active" data-lead-team="ALL">ALL</button>{leader_team_buttons}
    </div>
  </details>
  <div class="lead-controls" id="leadExportBar">
    <button class="filter-btn small" id="leadCopyBtn">Copy table</button>
    <button class="filter-btn small" id="leadDownloadBtn">Download CSV</button>
    <span class="hint" id="leadCopyStatus"></span>
  </div>
  <div class="lead-table-wrap">
    <table class="lead-table" id="leadTable">
      <thead><tr id="leadHead"></tr></thead>
      <tbody id="leadBody"></tbody>
    </table>
  </div>
</div>

<script>
{_page_js(trend_series, pool, seasons, top_n, snap_bounds, global_max, ranking_season, rank_week, leader_seasons, leader_players)}
</script>
"""
    return _wrap_html(body)


def _page_js(trend_series, pool, seasons, top_n, snap_bounds, global_max, ranking_season, rank_week,
             leader_seasons=(), leader_players=()):
    """Substitute the run's data into PAGE_JS. Compact separators throughout --
    whitespace on a payload this size is measured in hundreds of KB."""
    def j(v):
        return json.dumps(v, separators=(",", ":"))

    pool_light = [{"pid": p["pid"], "n": p["name"], "p": p["pos"], "t": p["team"],
                   "k": p["rank"], "g": p.get("group", "skill")}
                  for p in pool]
    return (PAGE_JS
            .replace("__TREND_GAMES__", j({k: v for k, v in trend_series.items() if v}))
            .replace("__POOL__", j(pool_light))
            .replace("__SEASONS__", j(seasons))
            .replace("__TOP_N__", str(top_n))
            .replace("__SNAP_BOUNDS__", j(snap_bounds))
            .replace("__GLOBAL_MAX__", j(global_max))
            .replace("__POS_COLORS__", j(POSITION_COLORS))
            .replace("__POS_FALLBACK__", j(POSITION_FALLBACK_COLOR))
            .replace("__SNAPLESS__", j(list(SNAPLESS_POSITIONS)))
            .replace("__LEADER_SEASONS__", j(list(leader_seasons)))
            .replace("__LEADER_PLAYERS__", j(list(leader_players)))
            .replace("__LEADER_CFG__", j(LEADER_CFG)))


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
    leader_seasons_back = _env_int("FANTASY_LEADER_SEASONS", DEFAULT_LEADER_SEASONS)
    positions = tuple(
        p.strip().upper() for p in os.environ.get("FANTASY_POSITIONS", ",".join(SKILL_POSITIONS)).split(",")
        if p.strip()
    )
    def_count = _env_int("FANTASY_DEF_COUNT", DEFAULT_DEF_COUNT)
    k_count = _env_int("FANTASY_K_COUNT", DEFAULT_K_COUNT)
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

    # Efficiency leaders: independent of the trend/ranking pipeline above --
    # just needs the players directory and the current anchor season. Newest
    # season first, so "last 2/3 seasons" on the page is simply "the first
    # 2/3 entries".
    leader_seasons = list(reversed(season_list(season, leader_seasons_back)))
    print(f"[info] fetching {leader_seasons_back}-season totals for efficiency leaders "
          f"({', '.join(leader_seasons)})...")
    leader_players = build_leader_pool(players_dir, leader_seasons)
    print(f"[info] efficiency leaders: {len(leader_players)} players qualified across "
          f"{'/'.join(LEADER_CFG)} in at least one of those seasons")

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
            # Three independently-capped groups. Kickers and defenses are ranked
            # by the same last-game rule but kept in their own buckets, because
            # ranking them against skill players would either bury them (a
            # kicker rarely outscores a WR1) or, in a scoring format where they
            # do, crowd out the players the report is actually about.
            pool = rank_pool(ranking_weeks, players_dir, scoring_field, pool_size, positions,
                             rank_by=rank_by, rank_week=rank_week, group="skill")
            def_pool = rank_pool(ranking_weeks, players_dir, scoring_field, def_count, ("DEF",),
                                 rank_by=rank_by, rank_week=rank_week, group="DEF")
            k_pool = rank_pool(ranking_weeks, players_dir, scoring_field, k_count, ("K",),
                               rank_by=rank_by, rank_week=rank_week, group="K")
            basis = (f"{ranking_season} season totals" if rank_by == "season"
                     else f"last game played through {ranking_season} week {rank_week}")
            print(f"[info] pool = top {len(pool)} ({'/'.join(positions)}) by {scoring_label} points, {basis}")
            print(f"[info]      + top {len(def_pool)} DEF, top {len(k_pool)} K (same ranking rule)")
            if not def_pool:
                print("  [warn] no team defenses found -- check that Sleeper still uses position 'DEF'",
                      file=sys.stderr)
            if not k_pool:
                print("  [warn] no kickers found -- check that Sleeper still uses position 'K'",
                      file=sys.stderr)
            pool = pool + def_pool + k_pool
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
                                rank_by, pool_size, top_n, selection_mode, missing, now, (0, 0),
                                leader_seasons, leader_players))
        print("[info] wrote index.html (empty-state)")
        return

    # Fold in every efficiency-leaders player who ISN'T already in the ranked
    # pool, so clicking a leaders-table row always has a card to reveal: the
    # trend grids only ever show cards for players actually rendered into the
    # page, and cards are rendered from this pool. These extras ride along
    # for free -- build_trend_series() below already walks every cached week
    # of `seasons` regardless of pool size, so adding entries here costs no
    # extra Sleeper fetches, just a few more (already-fetched) lookups per
    # week. group="leader" keeps them out of the default top-N view (same
    # mechanism that already hides DEF/K there) -- they only appear when
    # pinned, searched for, or matched by a position/team filter.
    existing_pids = {p["pid"] for p in pool}
    leader_only_pids = [p["pid"] for p in leader_players if p["pid"] not in existing_pids]
    leader_extras = []
    for i, pid in enumerate(leader_only_pids):
        info = players_dir.get(pid) or {}
        leader_extras.append({
            "pid": pid, "name": display_name(info, pid), "pos": info.get("position") or "?",
            "team": info.get("team") or "FA", "group": "leader",
            "rank_pts": None, "rank_from_week": None, "weeks_stale": 0, "games": None,
            "rank": i + 1,
        })
    render_pool = pool + leader_extras
    if leader_extras:
        print(f"[info] +{len(leader_extras)} efficiency-leaders players folded into the trend pool "
              f"(pinnable, not shown by default)")

    print(f"[info] building {trend_seasons_back}-season history for {len(render_pool)} players "
          f"({seasons_label})...")
    trend_series = build_trend_series(render_pool, seasons, scoring_field)
    snap_cov = snap_coverage(trend_series)
    if snap_cov[1]:
        print(f"[info] snap counts present for {snap_cov[0]:,} of {snap_cov[1]:,} games "
              f"({100.0 * snap_cov[0] / snap_cov[1]:.0f}%)")
    if snap_cov[1] and snap_cov[0] == 0:
        print("[warn] NO snap data at all -- the points-per-snap grid will be "
              f"empty. Check that Sleeper still populates {SNAP_FIELDS} in the stats payload.", file=sys.stderr)

    with open("index.html", "w") as f:
        f.write(render_html(render_pool, trend_series, seasons, seasons_label, scoring_label, ranking_season,
                            rank_week, rank_by, pool_size, top_n, selection_mode, missing, now, snap_cov,
                            leader_seasons, leader_players))

    size_mb = os.path.getsize("index.html") / (1024 * 1024)
    print(f"[info] wrote index.html ({size_mb:.1f} MB, {len(render_pool)} players, {snap_cov[1]:,} games)")

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
