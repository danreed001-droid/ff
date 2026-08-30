#!/usr/bin/env python3
"""
Draft Analyzer: a composite scoring matrix for fantasy draft prep, combining
six real inputs per player:

  1. Production   -- last season's points-per-game (position-relative)
  2. Opportunity   -- last season's snap share / target share / touch share
                       (position-relative) -- the best data-driven proxy for
                       "will this player see the ball a lot" without a paid
                       depth-chart-projection feed
  3. O-line grade  -- PFF's 2026 preseason offensive-line rankings (public
                       article, converted 1-32 rank -> 0-100 score). Weighted
                       heavily for RB, lightly for QB, minimally for WR/TE.
  4. QB quality    -- each team's actual leading-QB PPG from last season,
                       computed directly from real Sleeper stats (not an
                       opinion-based tier list) -- weighted heavily for
                       WR/TE, lightly for RB, not applied to QB itself.
  5. Strength of schedule -- READ FROM sos_config.csv, which you fill in
                       from whichever source you already have (FantasyPros,
                       Sharp Football, RotoWire, etc). This one genuinely
                       needs a data source I can't freely reproduce -- see
                       the "Filling in SOS" section below. Defaults to a
                       neutral 50 for every team until you do.
  6. Workload/durability -- last season's total touches (RB) or targets
                       (WR/TE), penalized past a threshold -- the "heavy
                       workload increases regression/injury risk next year"
                       factor. QB is treated as durability-neutral here.

All six are normalized to comparable 0-100 scales and combined with
position-specific weights (see POSITION_WEIGHTS) into one composite score,
computed separately for Half-PPR and Standard scoring since the "production"
input changes between formats. Outputs the top 150 by composite score for
each format as both a CSV (for Excel/Sheets) and a sortable, filterable
HTML draft board.

Data sources:
  - Sleeper API (https://docs.sleeper.com/) for player stats, snap counts,
    touches, targets -- same free/no-key access used by fantasy_flow.py in
    this repo. This script imports its Sleeper-fetching helpers directly
    rather than duplicating them.
  - PFF's 2026 offensive line rankings (public article, not paywalled
    numbers): https://www.pff.com/news/nfl-offensive-line-rankings-2026
    (TEAM_OLINE_RANK below; re-run PFF's rankings through it periodically
    since these can shift after injuries/lineup changes during the season).
  - sos_config.csv, which YOU fill in -- see "Filling in SOS" below.

Filling in SOS:
  Real per-position strength-of-schedule numbers live behind subscriptions
  (FantasyPros, Sharp Football Analysis, RotoWire, Footballguys all publish
  their own proprietary versions) or require reconstructing opponent
  quality from scratch, which is its own project. Rather than guess or
  reproduce someone else's paywalled table, this script reads
  sos_config.csv (team, sos_score 0-100, 100=easiest) at the repo root. If
  that file is missing or a team isn't listed, that team defaults to a
  neutral 50 (so the whole tool still runs and every other factor still
  applies) -- but the more of these you fill in from a real source, the
  more this factor actually does anything. A starter file with all 32
  teams at 50 is generated automatically the first time you run this if
  sos_config.csv doesn't exist yet.

Env vars (all optional):
    FANTASY_SCORING_POOL - how many top-by-raw-points players to gather
                       before computing composite scores (default 400).
                       Composite ranking can reorder within this pool but
                       won't reach outside it -- this keeps the normalization
                       math meaningful (no 1-game-sample noise) and keeps
                       runtime reasonable.
    DRAFT_TOP_N       - how many players make the final ranked output for
                       each scoring format (default 150).
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

# Reuse this repo's existing Sleeper-fetching helpers rather than duplicating them.
from fantasy_flow import (
    fetch_json, get_state, get_players_directory, get_week_stats,
    SLEEPER_BASE, MAX_WEEKS_PER_SEASON,
)

# --- PFF's 2026 preseason offensive line rankings, all 32 teams (1=best). ---
# Source: https://www.pff.com/news/nfl-offensive-line-rankings-2026 (public
# article, Aug 12 2026). Re-check periodically -- PFF explicitly updates
# these week to week as lineups/injuries change during the season.
TEAM_OLINE_RANK = {
    "DEN": 1, "PHI": 2, "TB": 3, "IND": 4, "CHI": 5, "BUF": 6, "LAC": 7, "KC": 8,
    "ATL": 9, "SF": 10, "LAR": 11, "MIN": 12, "NE": 13, "PIT": 14, "SEA": 15, "NO": 16,
    "DAL": 17, "LV": 18, "DET": 19, "CIN": 20, "NYJ": 21, "ARI": 22, "NYG": 23, "BAL": 24,
    "MIA": 25, "CAR": 26, "HOU": 27, "GB": 28, "TEN": 29, "JAX": 30, "CLE": 31, "WAS": 32,
}

DEFAULT_SCORING_POOL = 400
DEFAULT_DRAFT_TOP_N = 150
MIN_GAMES_PLAYED = 3  # exclude tiny-sample players (a 1-game cameo shouldn't rank)

SCORING_FIELDS = {"half_ppr": "pts_half_ppr", "std": "pts_std"}

RANK_POSITIONS = ("QB", "RB", "WR", "TE")

# Position-specific weights for the six components -- must sum to 1.0 per
# position (verified at the bottom of this file). These encode the request
# directly: O-line matters most for RB, QB quality matters most for
# WR/TE (barely for RB, not at all for QB itself), workload-risk applies
# mainly to RB/WR/TE, SOS and opportunity apply broadly.
POSITION_WEIGHTS = {
    "QB": {"production": 0.45, "opportunity": 0.10, "oline": 0.10, "qb_quality": 0.00, "sos": 0.20, "durability": 0.15},
    "RB": {"production": 0.30, "opportunity": 0.20, "oline": 0.20, "qb_quality": 0.05, "sos": 0.10, "durability": 0.15},
    "WR": {"production": 0.35, "opportunity": 0.20, "oline": 0.05, "qb_quality": 0.20, "sos": 0.10, "durability": 0.10},
    "TE": {"production": 0.35, "opportunity": 0.20, "oline": 0.05, "qb_quality": 0.20, "sos": 0.10, "durability": 0.10},
}
for _pos, _w in POSITION_WEIGHTS.items():
    _total = round(sum(_w.values()), 6)
    assert _total == 1.0, f"POSITION_WEIGHTS[{_pos}] sums to {_total}, not 1.0"

# Workload/durability thresholds -- "risk starts accumulating past this many
# touches/targets last season, and maxes out (durability score bottoms out
# at DURABILITY_FLOOR) past the second number." RB uses touches (rush_att +
# rec); WR/TE use targets; QB is treated as durability-neutral (always 100)
# since pass-attempt volume isn't the same kind of injury-risk signal.
DURABILITY_THRESHOLDS = {
    "RB": (280, 400),   # touches
    "WR": (140, 190),   # targets
    "TE": (100, 140),   # targets
}
DURABILITY_FLOOR = 40  # worst-case durability score (0-100 scale), never fully zeroed out


def load_sos_scores(path="sos_config.csv"):
    """{team: sos_score 0-100, 100=easiest} -- reads sos_config.csv if it
    exists (team,sos_score columns; extra columns ignored), otherwise
    writes a fresh one with every team defaulted to a neutral 50 so the
    file exists for you to edit before the next run. A team present in
    TEAM_OLINE_RANK but missing from the CSV also defaults to 50. See the
    module docstring's "Filling in SOS" section for where to get real
    numbers to put in this file."""
    all_teams = sorted(TEAM_OLINE_RANK.keys())
    scores = {t: 50.0 for t in all_teams}

    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            f.write("# 100 = easiest schedule, 0 = hardest. Fill in from FantasyPros/Sharp\n")
            f.write("# Football/RotoWire/Footballguys SOS rankings -- every team defaults\n")
            f.write("# to 50 (neutral) until you do.\n")
            writer = csv.writer(f)
            writer.writerow(["team", "sos_score"])
            for t in all_teams:
                writer.writerow([t, 50])
        print(f"[info] wrote a fresh {path} with neutral 50s -- fill in real SOS numbers "
              f"from your preferred source before your next run for this factor to do anything.")
        return scores

    with open(path, newline="") as f:
        lines = [line for line in f if not line.lstrip().startswith("#")]
        reader = csv.DictReader(lines)
        for row in reader:
            team = (row.get("team") or "").strip().upper()
            if not team or team not in scores:
                continue
            try:
                scores[team] = float(row["sos_score"])
            except (TypeError, ValueError, KeyError):
                continue
    return scores


def oline_score_for_team(team):
    """PFF O-line rank (1=best, 32=worst) -> a 0-100 score, rank 1 -> 100.0,
    rank 32 -> ~3.1. Unknown/free-agent team defaults to 50 (neutral)."""
    rank = TEAM_OLINE_RANK.get(team)
    if rank is None:
        return 50.0
    return round((33 - rank) / 32 * 100, 1)


def fetch_season_weekly_stats(season, max_week=MAX_WEEKS_PER_SEASON):
    """{week: {player_id: stats}} for every real week found in a season --
    thin wrapper around get_week_stats() so this file doesn't need its own
    caching layer (get_week_stats already caches in-process)."""
    out = {}
    for w in range(1, max_week + 1):
        wk = get_week_stats(season, w)
        if wk:
            out[w] = wk
    return out


def compute_team_qb_scores(weekly_stats_by_week, players_dir, scoring_field):
    """{team: qb_score 0-100} -- each team's BEST (highest-PPG) QB from
    last season, min-max normalized across the whole league. This is a
    real, computed-from-Sleeper-data proxy for "how good is this team's
    QB situation", refreshed every run -- not a static opinion-based tier
    list that goes stale. A team with no qualifying QB (very rare) is
    absent and callers should treat that as unknown -> neutral 50."""
    totals = {}   # pid -> total pts
    games = {}    # pid -> games played
    for wk in weekly_stats_by_week.values():
        for pid, stats in wk.items():
            info = players_dir.get(pid)
            if not info or info.get("position") != "QB":
                continue
            pts = stats.get(scoring_field)
            if pts is None:
                continue
            totals[pid] = totals.get(pid, 0.0) + float(pts)
            games[pid] = games.get(pid, 0) + 1

    team_best_ppg = {}
    for pid, total in totals.items():
        g = games.get(pid, 0)
        if g < MIN_GAMES_PLAYED:
            continue
        info = players_dir.get(pid, {})
        team = info.get("team")
        if not team:
            continue
        ppg = total / g
        if ppg > team_best_ppg.get(team, -1):
            team_best_ppg[team] = ppg

    if not team_best_ppg:
        return {}
    lo, hi = min(team_best_ppg.values()), max(team_best_ppg.values())
    span = (hi - lo) or 1
    return {team: round((ppg - lo) / span * 100, 1) for team, ppg in team_best_ppg.items()}


def compute_team_season_snap_and_opportunity_totals(weekly_stats_by_week, players_dir):
    """{team: total_off_snp} and {team: total_opportunities (rush_att +
    rec_tgt)} summed across every player, every week in the season --
    season-long denominators for each player's snap share / usage share.
    """
    snaps = {}
    opps = {}
    for wk in weekly_stats_by_week.values():
        for pid, stats in wk.items():
            info = players_dir.get(pid)
            if not info:
                continue
            team = info.get("team")
            if not team:
                continue
            snaps[team] = snaps.get(team, 0.0) + (stats.get("off_snp") or 0)
            opps[team] = opps.get(team, 0.0) + (stats.get("rush_att") or 0) + (stats.get("rec_tgt") or 0)
    return snaps, opps


def compute_player_season_stats(weekly_stats_by_week, players_dir, scoring_fields):
    """{player_id: {...}} season aggregates for every skill-position player
    with at least one stat line: points totals (both scoring formats),
    games played, PPG (both formats), total touches/targets/snaps. Doesn't
    filter by minimum games yet -- callers apply MIN_GAMES_PLAYED."""
    out = {}
    for wk in weekly_stats_by_week.values():
        for pid, stats in wk.items():
            info = players_dir.get(pid)
            if not info or info.get("position") not in RANK_POSITIONS:
                continue
            rec = out.setdefault(pid, {
                "name": info.get("full_name") or pid, "pos": info.get("position"),
                "team": info.get("team") or "FA", "games": 0,
                "pts_half_ppr": 0.0, "pts_std": 0.0,
                "touches": 0, "targets": 0, "snaps": 0, "pass_att": 0,
            })
            rec["games"] += 1
            for fmt, field in scoring_fields.items():
                rec[f"pts_{fmt}"] += float(stats.get(field) or 0.0)
            rec["touches"] += (stats.get("rush_att") or 0) + (stats.get("rec") or 0)
            rec["targets"] += stats.get("rec_tgt") or 0
            rec["snaps"] += stats.get("off_snp") or 0
            rec["pass_att"] += stats.get("pass_att") or 0
    return out


def durability_score(pos, touches, targets):
    """0-100, 100 = no workload-risk signal, DURABILITY_FLOOR = maxed-out
    risk. RB uses total touches, WR/TE use total targets, QB is always
    100 (durability-neutral -- see module docstring)."""
    if pos == "RB":
        value = touches
    elif pos in ("WR", "TE"):
        value = targets
    else:
        return 100.0
    lo, hi = DURABILITY_THRESHOLDS[pos]
    if value <= lo:
        return 100.0
    if value >= hi:
        return float(DURABILITY_FLOOR)
    frac = (value - lo) / (hi - lo)
    return round(100.0 - frac * (100.0 - DURABILITY_FLOOR), 1)


def minmax_normalize_within_position(players, value_of):
    """{player_id: 0-100 score}, min-max normalized SEPARATELY within each
    position group (not across positions) -- so a QB's production/
    opportunity is judged against other QBs, not against RBs who play a
    totally different statistical game. `value_of` is either a dict key
    (str) to read from each player dict, or a callable(player_dict) that
    returns the raw value to normalize. Ties/all-equal groups get a flat
    50."""
    getter = (lambda p: p[value_of]) if isinstance(value_of, str) else value_of

    by_pos = {}
    for pid, p in players.items():
        by_pos.setdefault(p["pos"], []).append(pid)

    out = {}
    for pos, pids in by_pos.items():
        vals = [getter(players[pid]) for pid in pids]
        lo, hi = min(vals), max(vals)
        span = hi - lo
        for pid in pids:
            out[pid] = 50.0 if span == 0 else round((getter(players[pid]) - lo) / span * 100, 1)
    return out


def build_draft_board(players, sos_scores, team_qb_scores, scoring_format):
    """Attach every component score + the final composite (0-100, higher
    is better) to each player dict in `players`, for one scoring format
    ("half_ppr" or "std"). Mutates and returns `players`."""
    pts_key = f"pts_{scoring_format}"
    ppg_key = f"ppg_{scoring_format}"
    for p in players.values():
        p[ppg_key] = round(p[pts_key] / p["games"], 2) if p["games"] else 0.0

    # production: PPG, position-relative
    prod_scores = minmax_normalize_within_position(players, ppg_key)

    # opportunity: snap share for pass-catchers/QB, touch share (rush+rec)/
    # (team opportunities) for RB (touches matter more directly for a
    # runner's workload than raw snap count) -- simplified to a single
    # "share of team offense" number per position.
    def _opportunity_value(p):
        return p.get("_touch_share", 0.0) if p["pos"] == "RB" else p.get("_snap_share", 0.0)

    opp_scores = minmax_normalize_within_position(players, _opportunity_value)

    for pid, p in players.items():
        pos = p["pos"]
        w = POSITION_WEIGHTS[pos]
        oline = oline_score_for_team(p["team"])
        qb_q = team_qb_scores.get(p["team"], 50.0)
        sos = sos_scores.get(p["team"], 50.0)
        dur = durability_score(pos, p["touches"], p["targets"])

        composite = (
            w["production"] * prod_scores.get(pid, 50.0)
            + w["opportunity"] * opp_scores.get(pid, 50.0)
            + w["oline"] * oline
            + w["qb_quality"] * qb_q
            + w["sos"] * sos
            + w["durability"] * dur
        )

        p[f"composite_{scoring_format}"] = round(composite, 2)
        p["oline_score"] = oline
        p["qb_quality_score"] = qb_q
        p["sos_score"] = sos
        p["durability_score"] = dur
        p[f"production_score_{scoring_format}"] = prod_scores.get(pid, 50.0)
        p["opportunity_score"] = opp_scores.get(pid, 50.0)

    return players


def write_csv(players_ranked, scoring_format, path):
    fmt_label = "Half-PPR" if scoring_format == "half_ppr" else "Standard"
    fields = [
        "rank", "name", "pos", "team", f"composite_{scoring_format}", f"ppg_{scoring_format}",
        "games", f"production_score_{scoring_format}", "opportunity_score", "oline_score",
        "qb_quality_score", "sos_score", "durability_score", "touches", "targets", "snaps",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for i, p in enumerate(players_ranked, start=1):
            writer.writerow([
                i, p["name"], p["pos"], p["team"], p[f"composite_{scoring_format}"],
                p[f"ppg_{scoring_format}"], p["games"], p[f"production_score_{scoring_format}"],
                p["opportunity_score"], p["oline_score"], p["qb_quality_score"], p["sos_score"],
                p["durability_score"], p["touches"], p["targets"], p["snaps"],
            ])
    print(f"[info] wrote {path} ({fmt_label}, {len(players_ranked)} players)")


DRAFT_BOARD_CSS = """
  :root{
    --bg:#0B0E14; --panel:#111621; --line:#1E2633;
    --ink:#E8E6DE; --dim:#6B7280; --cyan:#4FD8E8;
    --red:#FF5C5C; --green:#3ECF8E; --neutral:#9CA3AF;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  body{ background:var(--bg); color:var(--ink); font-family:'IBM Plex Mono', monospace; padding:26px 20px 60px; }
  .eyebrow{ font-size:11px; letter-spacing:.18em; color:var(--cyan); text-transform:uppercase; }
  h1{ font-family:'Space Grotesk', sans-serif; font-size:22px; margin:4px 0 4px; }
  .sub{ color:var(--dim); font-size:12px; margin-bottom:6px; line-height:1.5; max-width:760px; }
  .sub b{ color:var(--ink); }
  .status{ color:var(--dim); font-size:11px; margin-bottom:18px; }
  .bar{ display:flex; gap:8px; margin:14px 0 18px; flex-wrap:wrap; align-items:center; }
  .bar-label{ font-size:10px; color:var(--dim); letter-spacing:.08em; text-transform:uppercase; margin-right:2px; }
  .btn{
    background:var(--panel); border:1px solid var(--line); color:var(--dim);
    font-family:'IBM Plex Mono', monospace; font-size:11px; padding:7px 13px;
    border-radius:6px; cursor:pointer; transition:all .15s ease;
  }
  .btn:hover{ color:var(--ink); border-color:#39424f; }
  .btn.active{ color:#0B0E14; background:var(--cyan); border-color:var(--cyan); font-weight:600; }
  input#search{
    background:var(--panel); border:1px solid var(--line); color:var(--ink);
    font-family:'IBM Plex Mono', monospace; font-size:12px; padding:7px 12px; border-radius:6px;
    outline:none; min-width:200px;
  }
  table{ width:100%; border-collapse:collapse; font-size:12px; background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  thead th{
    text-align:left; padding:10px 12px; background:#0d1119; color:var(--dim); font-weight:600;
    font-size:10.5px; letter-spacing:.04em; text-transform:uppercase; cursor:pointer; user-select:none;
    border-bottom:1px solid var(--line); white-space:nowrap;
  }
  thead th:hover{ color:var(--ink); }
  thead th.sorted-asc::after{ content:" \\2191"; color:var(--cyan); }
  thead th.sorted-desc::after{ content:" \\2193"; color:var(--cyan); }
  tbody td{ padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
  tbody tr:last-child td{ border-bottom:none; }
  tbody tr:hover{ background:#161b24; }
  .name-cell{ font-weight:600; color:var(--ink); }
  .pos-badge{ font-size:9.5px; color:var(--dim); margin-left:6px; }
  .composite{ font-weight:700; }
  .rank-cell{ color:var(--dim); width:36px; }
  .note{ font-size:11px; color:var(--dim); line-height:1.6; margin-top:18px; max-width:760px; }
  .note b{ color:var(--ink); }
"""


def render_html(players_half, players_std, as_of, pool_size, top_n):
    def rows_json(players_ranked, fmt):
        out = []
        for i, p in enumerate(players_ranked, start=1):
            out.append({
                "rank": i, "name": p["name"], "pos": p["pos"], "team": p["team"],
                "composite": p[f"composite_{fmt}"], "ppg": p[f"ppg_{fmt}"], "games": p["games"],
                "production": p[f"production_score_{fmt}"], "opportunity": p["opportunity_score"],
                "oline": p["oline_score"], "qb_quality": p["qb_quality_score"], "sos": p["sos_score"],
                "durability": p["durability_score"], "touches": p["touches"], "targets": p["targets"],
                "snaps": p["snaps"],
            })
        return out

    data = {"half_ppr": rows_json(players_half, "half_ppr"), "std": rows_json(players_std, "std")}
    data_json = json.dumps(data)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Draft Analyzer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>{DRAFT_BOARD_CSS}</style>
</head>
<body>
<div class="eyebrow">Draft Analyzer</div>
<h1>Composite Draft Board -- Top {top_n}</h1>
<div class="sub">
  One composite score (0-100) per player, blending <b>production</b> (PPG, position-relative),
  <b>opportunity</b> (snap/touch share, position-relative), <b>O-line grade</b> (PFF 2026 preseason
  rankings), <b>QB quality</b> (each team's leading QB's PPG last season, computed from real Sleeper
  data), <b>strength of schedule</b> (from <code>sos_config.csv</code> -- fill this in from your own
  source, defaults to neutral 50), and <b>durability</b> (penalizes last season's heaviest workloads --
  RB touches, WR/TE targets -- for regression/injury risk). Weights are position-specific; see
  <code>POSITION_WEIGHTS</code> in <code>draft_analyzer.py</code> to tune them.
</div>
<div class="status">Pool: top {pool_size} by raw points before re-ranking &middot; generated {html_escape(as_of)}</div>

<div class="bar" id="fmtBar">
  <span class="bar-label">Format</span>
  <button class="btn active" data-fmt="half_ppr">Half-PPR</button>
  <button class="btn" data-fmt="std">Standard</button>
</div>
<div class="bar" id="posBar">
  <span class="bar-label">Position</span>
  <button class="btn active" data-pos="ALL">ALL</button>
  <button class="btn" data-pos="QB">QB</button>
  <button class="btn" data-pos="RB">RB</button>
  <button class="btn" data-pos="WR">WR</button>
  <button class="btn" data-pos="TE">TE</button>
  <input id="search" type="text" placeholder="Search player...">
</div>

<table>
  <thead>
    <tr>
      <th data-key="rank">#</th>
      <th data-key="name">Player</th>
      <th data-key="team">Team</th>
      <th data-key="composite" class="sorted-desc">Score</th>
      <th data-key="ppg">PPG</th>
      <th data-key="games">GP</th>
      <th data-key="production">Prod</th>
      <th data-key="opportunity">Opp</th>
      <th data-key="oline">OL</th>
      <th data-key="qb_quality">QB</th>
      <th data-key="sos">SOS</th>
      <th data-key="durability">Dur</th>
      <th data-key="touches">Tch</th>
      <th data-key="targets">Tgt</th>
      <th data-key="snaps">Snp</th>
    </tr>
  </thead>
  <tbody id="tbody"></tbody>
</table>

<div class="note">
  <b>Prod</b> = production score &middot; <b>Opp</b> = opportunity score &middot; <b>OL</b> = O-line
  grade &middot; <b>QB</b> = team QB-quality score &middot; <b>SOS</b> = strength-of-schedule score
  &middot; <b>Dur</b> = durability score (100 = no workload-risk flag) -- all 0-100, all clickable to
  sort. <b>Tch/Tgt/Snp</b> are raw season totals from last year, not scores. This is a data-driven
  starting point for draft prep, not a guarantee -- injuries, depth-chart battles, and scheme changes
  this preseason aren't reflected unless you've updated <code>sos_config.csv</code> and re-run PFF's
  O-line rankings into <code>TEAM_OLINE_RANK</code>.
</div>

<script>
const DATA = {data_json};
let currentFmt = 'half_ppr';
let currentPos = 'ALL';
let sortKey = 'composite';
let sortDir = 'desc';
let searchTerm = '';

function render(){{
  let rows = DATA[currentFmt].slice();
  if (currentPos !== 'ALL') rows = rows.filter(r => r.pos === currentPos);
  if (searchTerm) rows = rows.filter(r => r.name.toLowerCase().includes(searchTerm));
  rows.sort((a,b) => {{
    const av = a[sortKey], bv = b[sortKey];
    if (typeof av === 'string') return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
    return sortDir === 'asc' ? av - bv : bv - av;
  }});
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map((r, i) => `
    <tr>
      <td class="rank-cell">${{i+1}}</td>
      <td class="name-cell">${{r.name}}<span class="pos-badge">${{r.pos}}</span></td>
      <td>${{r.team}}</td>
      <td class="composite">${{r.composite.toFixed(1)}}</td>
      <td>${{r.ppg.toFixed(1)}}</td>
      <td>${{r.games}}</td>
      <td>${{r.production.toFixed(0)}}</td>
      <td>${{r.opportunity.toFixed(0)}}</td>
      <td>${{r.oline.toFixed(0)}}</td>
      <td>${{r.qb_quality.toFixed(0)}}</td>
      <td>${{r.sos.toFixed(0)}}</td>
      <td>${{r.durability.toFixed(0)}}</td>
      <td>${{r.touches}}</td>
      <td>${{r.targets}}</td>
      <td>${{r.snaps}}</td>
    </tr>`).join('');
}}

document.getElementById('fmtBar').addEventListener('click', e => {{
  const btn = e.target.closest('.btn'); if (!btn) return;
  currentFmt = btn.getAttribute('data-fmt');
  document.querySelectorAll('#fmtBar .btn').forEach(b => b.classList.toggle('active', b === btn));
  render();
}});
document.getElementById('posBar').addEventListener('click', e => {{
  const btn = e.target.closest('.btn'); if (!btn) return;
  currentPos = btn.getAttribute('data-pos');
  document.querySelectorAll('#posBar .btn').forEach(b => b.classList.toggle('active', b === btn));
  render();
}});
document.getElementById('search').addEventListener('input', e => {{
  searchTerm = e.target.value.trim().toLowerCase();
  render();
}});
document.querySelectorAll('thead th').forEach(th => {{
  th.addEventListener('click', () => {{
    const key = th.getAttribute('data-key');
    if (sortKey === key) {{ sortDir = sortDir === 'asc' ? 'desc' : 'asc'; }}
    else {{ sortKey = key; sortDir = 'desc'; }}
    document.querySelectorAll('thead th').forEach(h => h.classList.remove('sorted-asc','sorted-desc'));
    th.classList.add(sortDir === 'asc' ? 'sorted-asc' : 'sorted-desc');
    render();
  }});
}});

render();
</script>
</body>
</html>
"""


def html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def main():
    pool_size = DEFAULT_SCORING_POOL
    if os.environ.get("FANTASY_SCORING_POOL"):
        try:
            pool_size = max(50, int(os.environ["FANTASY_SCORING_POOL"]))
        except ValueError:
            pass

    top_n = DEFAULT_DRAFT_TOP_N
    if os.environ.get("DRAFT_TOP_N"):
        try:
            top_n = max(10, int(os.environ["DRAFT_TOP_N"]))
        except ValueError:
            pass

    print("[info] fetching Sleeper state...")
    state = get_state()
    season = state.get("league_season") or state.get("season")
    current_week = state.get("week") or 0
    season_type = state.get("season_type", "regular")
    is_live = (season_type == "regular" and current_week and current_week >= 1)
    last_completed_season = str(int(season) - 1) if is_live else str(season)
    print(f"[info] current state: season={season} week={current_week} type={season_type} "
          f"-- using last completed season {last_completed_season} for all analysis")

    print("[info] fetching Sleeper players directory (~5MB)...")
    players_dir = get_players_directory()

    print(f"[info] fetching {last_completed_season} weekly stats (up to {MAX_WEEKS_PER_SEASON} weeks)...")
    weekly_stats = fetch_season_weekly_stats(last_completed_season)
    if not weekly_stats:
        print(f"[error] no stats found for {last_completed_season} -- can't build a draft board without "
              f"a completed season to analyze. Exiting.")
        sys.exit(1)

    print("[info] computing team QB-quality scores from real Sleeper data...")
    team_qb_scores = compute_team_qb_scores(weekly_stats, players_dir, "pts_half_ppr")

    print("[info] loading strength-of-schedule config...")
    sos_scores = load_sos_scores()

    print("[info] aggregating season stats for every skill-position player...")
    players = compute_player_season_stats(weekly_stats, players_dir, SCORING_FIELDS)
    players = {pid: p for pid, p in players.items() if p["games"] >= MIN_GAMES_PLAYED}
    print(f"[info] {len(players)} players with >= {MIN_GAMES_PLAYED} games played")

    team_snaps, team_opps = compute_team_season_snap_and_opportunity_totals(weekly_stats, players_dir)
    for p in players.values():
        team_snap_total = team_snaps.get(p["team"], 0.0)
        team_opp_total = team_opps.get(p["team"], 0.0)
        p["_snap_share"] = (p["snaps"] / team_snap_total) if team_snap_total else 0.0
        p["_touch_share"] = (p["touches"] / team_opp_total) if team_opp_total else 0.0

    # Pool by raw half-PPR points first (keeps normalization meaningful,
    # keeps runtime reasonable) -- composite ranking can reorder within
    # this pool but won't reach outside it.
    pool_ids = sorted(players.keys(), key=lambda pid: players[pid]["pts_half_ppr"], reverse=True)[:pool_size]
    pool = {pid: players[pid] for pid in pool_ids}
    print(f"[info] scoring pool: top {len(pool)} players by raw {last_completed_season} points")

    now = datetime.now(timezone.utc)
    as_of = now.strftime("%Y-%m-%d %H:%M UTC")

    for fmt in ("half_ppr", "std"):
        build_draft_board(pool, sos_scores, team_qb_scores, fmt)

    ranked_half = sorted(pool.values(), key=lambda p: p["composite_half_ppr"], reverse=True)[:top_n]
    ranked_std = sorted(pool.values(), key=lambda p: p["composite_std"], reverse=True)[:top_n]

    write_csv(ranked_half, "half_ppr", "draft_board_half_ppr.csv")
    write_csv(ranked_std, "std", "draft_board_standard.csv")

    with open("draft_board.html", "w") as f:
        f.write(render_html(ranked_half, ranked_std, as_of, pool_size, top_n))
    print("[info] wrote draft_board.html")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [f"## Draft Analyzer -- top 15 (Half-PPR), based on {last_completed_season}", ""]
        for i, p in enumerate(ranked_half[:15], start=1):
            lines.append(f"{i}. **{p['name']}** ({p['pos']}, {p['team']}) -- "
                          f"score {p['composite_half_ppr']:.1f}, {p['ppg_half_ppr']:.1f} PPG")
        with open(summary_path, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
