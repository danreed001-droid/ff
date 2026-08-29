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
2. **Hot/cold index x points per touch** -- the efficiency read. Same
   y-axis, same season selector, but the x-axis is points scored *per
   touch* (touches = carries + receptions, not targets), so a low-usage
   player who was explosive on limited touches shows up far right even in a
   week they didn't score much in total.
3. **4-season points trend** -- a static small-multiple line chart per
   player, showing weekly points chronologically across as much of the last
   4 regular seasons as they actually have. Every week is a bubble, sized
   by that game's **points per play** (touches) and scaled **relative to
   every game, for every player, in the report** -- so a given points-per-
   play value renders as the same size bubble on every player's card. A
   rookie or a recent addition just starts wherever their real data starts
   (no padding with invented zeros for seasons before they existed). Dashed
   lines mark season boundaries. Always covers the full 4-season span
   regardless of which season is selected in the quadrant panels above --
   it's the one view that isn't affected by the season selector.

   This chart has two extra interactions of its own:
   - **Season checkboxes** above the grid -- uncheck a season and every
     card hides that season's line/bubbles at once (positions never move,
     it's a pure visibility toggle, same principle as the position filter).
   - **Click any point** to see that exact game's season, week, points,
     points-per-play, and touch count in a small readout under that card.

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

By default it tracks 8 sample players (see `WATCHLIST` at the top of
`fantasy_flow.py`). Override this without touching code by setting a repo
secret or workflow env var:

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
- **Two different volume metrics, on purpose.** "Usage share" (bubble size,
  both quadrant panels) is opportunities = carries + *targets* -- it counts
  a look even if the ball wasn't caught, since that's still an opportunity
  the offense gave them. "Points per touch" (panel 2's x-axis) divides by
  touches = carries + *receptions* -- an actual touch, not just a target --
  since an efficiency metric should only count plays where they actually
  had the ball. Mixing these up would understate efficiency for
  high-target/low-catch-rate players.
- **RSI(4), not RSI(14).** A season is only ~17 weeks, so a 14-week window
  would barely ever produce a reading. 4 weeks is short enough to react to a
  real hot/cold streak within a season; tune `RSI_PERIOD` in
  `fantasy_flow.py` if you want it more or less reactive.
- **Byes and injuries show up as a 0-point week** in the quadrant panels
  (which will read as a sharp drop in the hot/cold index, same as a real
  slump would) but are *excluded* from the trend grid (a bye isn't "0
  points," it's "didn't play," so it's left out of that chart rather than
  plotted as a zero).
- **The points-per-touch panel can show fewer bubbles than the points
  panel** in a given week -- a player with zero touches that week (DNP,
  bye, or literally zero touches in a blowout) has no defined points-per-
  touch value and is hidden from that week's frame rather than plotted as 0
  or infinity.
- This is an informational tool, not fantasy advice.
