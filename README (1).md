# Fantasy Quadrant

RSI-style "hot/cold index" plotted against real weekly performance, with a
short trail behind each player showing their last few weeks' trajectory --
same visual mechanic as [moneyflow-update](https://github.com/danreed001-droid/moneyflow-update)'s
Equilibrium quadrant panels, ported from tickers/price-closes to fantasy
players/weekly points.

Runs weekly via GitHub Actions and commits `index.html` back into this repo
as static HTML -- no server, no database, no build step for anyone viewing
it.

## What it shows

Three views, all filterable by position (QB/RB/WR/TE/ALL) via the buttons
at the top. The two quadrant panels additionally share a **season
selector** (buttons for whichever of the last 4 regular seasons actually
have data) so you're not stuck looking only at the live season -- pick any
of the last 4 years and its own week slider takes over:

1. **Hot/cold index x points scored** -- the volume/production read. X-axis
   is actual points that week, Y-axis is a 4-week RSI on the player's own
   points **within the selected season** (RSI resets at each season
   boundary, since a hot streak shouldn't carry across an off-season). Same
   math as stock RSI, just relative to their own recent baseline, not other
   players. Bubble size is usage share -- opportunities (carries + targets)
   as a share of their own team's total that week.
2. **Hot/cold index x points per snap** -- the efficiency read. Same
   y-axis, same season selector, but the x-axis is points scored *per
   snap* -- Sleeper's own `off_snp` stat (offensive plays that player was
   actually on the field for), not a derived touches/targets count -- so a
   player who barely leaves the sideline but is quiet with the ball looks
   very different here than a rarely-used player who's explosive in a
   handful of snaps.
3. **4-season points trend** -- a static small-multiple line chart per
   player, showing weekly points chronologically across as much of the last
   4 regular seasons as they actually have. Every week is a bubble, sized
   by that game's **points per snap** and scaled **relative to every game,
   for every player, in the report** -- so a given points-per-snap value
   renders as the same size bubble on every player's card. A rookie or a
   recent addition just starts wherever their real data starts (no padding
   with invented zeros for seasons before they existed). Dashed lines mark
   season boundaries. Always covers the full 4-season span regardless of
   which season is selected in the quadrant panels above -- it's the one
   view that isn't affected by the season selector.

   This chart has two extra interactions of its own:
   - **Season checkboxes** above the grid -- uncheck a season and every
     card hides that season's line/bubbles at once (positions never move,
     it's a pure visibility toggle, same principle as the position filter).
   - **Click any point** to see that exact game's season, week, points,
     points-per-snap, and snap count in a small readout under that card.

Position filtering is a pure visibility toggle across all three sections at
once (every filterable element carries a `data-pos` attribute); it never
changes bubble sizing math. There's no simulation anywhere: every slider
position is a real past week's box score pulled from Sleeper, not invented
or interpolated motion.

**Off-season behavior:** Sleeper's `state/nfl` endpoint reports whichever
season most recently had games -- during the off-season that's the season
that just ended, with `season_type` something other than `"regular"`. This
script treats that correctly: it fetches and shows the last 4 seasons'
worth of quadrant + trend data regardless of whether a season is currently
live, and defaults the season selector to the most recently *completed*
season rather than showing an empty page. Only the live season's window is
capped to the current week (via `season_type == "regular"`); every other
season requests its full 1-18 week range, since `get_week_stats()`
gracefully returns nothing for a week that doesn't exist.

## Data source

The [Sleeper API](https://docs.sleeper.com/) -- free, read-only, no API key
required. Endpoints:

- `GET /v1/state/nfl` -- current season/week
- `GET /v1/players/nfl` -- full player directory (~5MB; fetched fresh each
  run, which comfortably respects Sleeper's own guidance to call this at
  most once a day, since this runs weekly)
- `GET /v1/stats/nfl/regular/{season}/{week}` -- one week's box-score stats
  for every NFL player who played. Called once per (season, week) needed
  across all three views and cached in-process for the run, so the trend
  grid's 4-season lookback doesn't re-fetch weeks the quadrant panels
  already pulled.

Sleeper's docs describe the API as free for non-commercial use; reach out to
them directly if you'd want to use this commercially. Rate limits are
per-IP (roughly 90 requests/minute per third-party reports). A full run with
the default settings makes roughly 70-80 stats requests (mostly from the
4-season trend lookback) -- comfortably under that limit for a once-a-week
job, but worth knowing if you push `FANTASY_TREND_SEASONS` much higher.

## Setup (5 minutes)

1. Push this folder as a repo:

   ```
   cd fantasy-quadrant
   git remote add origin https://github.com/<you>/fantasy-quadrant.git
   git branch -M main
   git add -A
   git commit -m "Initial commit"
   git push -u origin main
   ```

2. The workflow (`.github/workflows/fantasy-flow.yml`) is scheduled for
   Tuesday 09:00 UTC (after Monday Night Football posts final stats). You
   can also trigger it manually any time from the repo's **Actions** tab ->
   "Fantasy Quadrant" -> **Run workflow**. The first run writes `index.html`
   -- until then that file doesn't exist yet in this repo.

3. (Optional) Enable GitHub Pages (Settings -> Pages -> Deploy from branch
   -> main -> / (root)) to view `index.html` at a public URL instead of
   opening the raw file.

## Configuring your watchlist

By default it tracks ~134 players (see `WATCHLIST` at the top of
`fantasy_flow.py`) -- roughly 30+ at each of QB/RB/WR/TE, so the position
filter has a real bench to work with in every bucket, not just a couple of
names per position. It's a best-effort snapshot of notable players; rosters
shift (trades, retirements, breakout rookies) faster than this file does,
so a handful of names may not resolve on any given run -- those are skipped
with a warning in the job log/summary rather than failing the run. Override
the whole list without touching code by setting a repo secret or workflow
env var:

```yaml
env:
  FANTASY_WATCHLIST: "Christian McCaffrey,Justin Jefferson,Bijan Robinson,..."
```

Names are matched case-insensitively against Sleeper's player directory at
runtime (not hardcoded player IDs, since those are opaque strings best
resolved live). If a name doesn't match -- a typo, a retirement, a name
Sleeper spells differently -- it's skipped with a warning in the job log
and job summary, and the rest of the run proceeds normally with whoever did
match.

Other env vars (all optional, set in the workflow file or as repo secrets):

- `FANTASY_SCORING` -- `"ppr"` (default), `"half_ppr"`, or `"std"`. Which
  Sleeper points field drives both quadrant panels' relevant axis and the
  trend grid.
- `FANTASY_TREND_SEASONS` -- how many seasons back BOTH the quadrant
  panels' season selector and the trend grid cover, including the current
  one (default 4).

## Notes

- **Watchlist, not your specific league roster (yet).** This tracks a fixed
  list of named players, the same way moneyflow-update tracks a fixed list
  of tickers -- it doesn't yet pull your actual Sleeper league's rosters.
  That's a natural next step (`/v1/league/{league_id}/rosters` +
  `/v1/user/{username}` to resolve your own team), just not wired up here.
- **Two different volume metrics, on purpose, and neither of them is
  touches.** "Usage share" (bubble size, both quadrant panels) is
  opportunities = carries + *targets* -- it counts a look even if the ball
  wasn't caught, since that's still an opportunity the offense gave them.
  "Points per snap" (panel 2's x-axis, and the trend grid's bubble size) is
  Sleeper's own `off_snp` field -- the count of offensive *plays* that
  player was actually on the field for, whether or not the ball ever came
  their way (a blocking snap, a decoy route, a play the QB threw elsewhere
  on). That's deliberately not the same as touches (carries + catches): a
  player can be on the field for 50 snaps and only touch the ball 8 times,
  and points-per-snap is measuring how much they produced across that
  whole workload, not just the plays where they had it.
- **RSI(4), not RSI(14).** A season is only ~17 weeks, so a 14-week window
  would barely ever produce a reading. 4 weeks is short enough to react to a
  real hot/cold streak within a season; tune `RSI_PERIOD` in
  `fantasy_flow.py` if you want it more or less reactive.
- **Byes and injuries show up as a 0-point week** in the quadrant panels
  (which will read as a sharp drop in the hot/cold index, same as a real
  slump would) but are *excluded* from the trend grid (a bye isn't "0
  points," it's "didn't play," so it's left out of that chart rather than
  plotted as a zero).
- **The points-per-snap panel can show fewer bubbles than the points
  panel** in a given week -- a player with zero recorded offensive snaps
  that week (DNP, bye, or a data gap) has no defined points-per-snap value
  and is hidden from that week's frame rather than plotted as 0 or
  infinity.
- This is an informational tool, not fantasy advice.

## Draft Analyzer (`draft_analyzer.py`)

A separate tool in this repo: a composite scoring matrix for draft prep,
ranking the top 150 players (by default) for both Half-PPR and Standard
scoring. Six inputs per player, all normalized to comparable 0-100 scales
and combined with position-specific weights:

1. **Production** -- last season's PPG, judged against other players at the
   same position (a QB is compared to other QBs, not to RBs).
2. **Opportunity** -- last season's share of team offense: touch share for
   RB, snap share for QB/WR/TE. The best data-driven proxy for "will this
   player see a lot of work" without a paid depth-chart-projection feed.
3. **O-line grade** -- PFF's 2026 preseason offensive-line rankings
   (`TEAM_OLINE_RANK` in the script, sourced from PFF's public rankings
   article), weighted heavily for RB, lightly for QB, minimally for WR/TE.
4. **QB quality** -- each team's actual leading QB's PPG last season,
   computed directly from real Sleeper stats every run -- not a static
   opinion-based tier list that goes stale. Weighted heavily for WR/TE,
   lightly for RB, not applied to QB itself.
5. **Strength of schedule** -- read from `sos_config.csv`, which starts
   with every team at a neutral 50 and does nothing until you fill it in.
   See "Filling in SOS" below for why this one's on you.
6. **Durability** -- penalizes last season's heaviest workloads (RB total
   touches, WR/TE total targets) for regression/injury risk -- the "him
   again? that workload's a warning sign" factor. QB is durability-neutral.

### Filling in SOS

Real per-position strength-of-schedule numbers live behind subscriptions
(FantasyPros, Sharp Football Analysis, RotoWire, and Footballguys all
publish their own proprietary versions), or require reconstructing
opponent quality from scratch. Rather than guess, or reproduce someone
else's paywalled table, this script reads `sos_config.csv` (columns:
`team`, `sos_score`, 0-100 where 100 = easiest) and defaults every team to
a neutral 50 until you edit it -- the file is created automatically the
first time you run the script if it doesn't exist yet. The more of these
you fill in from a real source, the more this factor actually does
anything; committed automatically by the workflow so your edits persist
across runs.

### Running it

```
python draft_analyzer.py
```

Writes three files: `draft_board.html` (sortable, filterable, click any
column header to sort, position filter, Half-PPR/Standard toggle, search
box) and `draft_board_half_ppr.csv` / `draft_board_standard.csv` (same
data, for Excel/Sheets). The GitHub Actions workflow
(`.github/workflows/draft-analyzer.yml`) runs this weekly during the
summer and commits the results, or trigger it manually from the Actions
tab any time.

Env vars (optional): `FANTASY_SCORING_POOL` (default 400) -- how many
players by raw points get pooled before composite re-ranking; `DRAFT_TOP_N`
(default 150) -- how many make the final output per format.

### Notes

- **Uses last season's completed stats**, not this season's in-progress
  ones -- draft prep happens before/early in a season, so there's no
  meaningful in-season sample to draw from yet. Re-run PFF's O-line
  rankings into `TEAM_OLINE_RANK` periodically since PFF updates those
  through the season as injuries and lineup changes happen.
- **Pool then re-rank.** The script gathers the top `FANTASY_SCORING_POOL`
  players by raw points first, then computes composite scores within that
  pool -- composite ranking can reorder players within the pool (that's
  the whole point) but won't pull someone in from outside it. This keeps
  the position-relative normalization meaningful (a stable ~60-WR peer
  group, not a noisy 3-WR one) and keeps runtime reasonable.
- **Minimum 3 games played** to qualify, so a one-game cameo doesn't rank
  on a tiny, noisy sample.
- **This is a starting point for draft prep, not a finished ranking.** It
  doesn't know about this preseason's injuries, depth-chart battles, or
  scheme changes unless you've updated `sos_config.csv` and refreshed
  `TEAM_OLINE_RANK` -- treat it as one structured input alongside actual
  beat-writer news, not a replacement for it.
- This is an informational tool, not fantasy or betting advice.
