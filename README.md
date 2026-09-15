# Mai Tai Soccer Calendar

A subscribable calendar of Mai Tai's indoor soccer games, generated from the
[Bond Sports](https://bondsports.co) public API and hosted on GitHub Pages.

**Subscribe:** https://tonisaurus.github.io/mai-tai-calendar/

## What the calendar shows

| Game state | Event title |
|---|---|
| This week's game | `Mai Tai (3rd, 5-2) vs Milan Mujeres (2nd, 6-1)` plus the full division standings in the description |
| Later games | `Mai Tai vs Manchester` |
| Played games | `Mai Tai 7 - 1 Barracuda (W)` plus the standings as they stood at the end of that game day |
| Playoff slots before the bracket is set | `Playoffs: TBD` (one event per slot; slots Mai Tai does not play in disappear once matchups are announced) |

The home team is always listed first. The venue address is not stored in this repository; it is set as the
`GAME_LOCATION` repository variable (Settings > Secrets and variables > Actions > Variables) and
written into each event's location.

## How it works

- [`build_calendar.py`](build_calendar.py) discovers seasons on its own: it lists the program's seasons
  (`programs-seasons/program/<program_id>`), keeps the ones whose name contains `season_name_contains`
  (case-insensitive), maps each to its competition and stages (`program_seasons/<id>/competition`), then
  fetches each stage's schedule and standings plus the scoring ruleset. Seasons the team does not appear in
  are ignored. Python 3.9+, no dependencies.
- A season is fetched live from 45 days before it starts (so the schedule shows up as soon as the league
  publishes it) until 21 days after it ends. Finished seasons are cached as raw API responses in
  [`seasons/`](seasons/) and served from there, so past results stay in the calendar without daily
  refetching, even after the league drops the season from its listing.
- Each event's `UID` is the Bond Sports `eventId`, which is stable even before a game is played, so a
  rescheduled game or a posted score updates the existing calendar entry instead of creating a new one.
- [`state.json`](state.json) remembers a content hash per event so `SEQUENCE` and `LAST-MODIFIED` only
  change when an event actually changes. That also means a run only commits when there is news.
- Standings for past games are rebuilt from results using the league's published ruleset (points per
  win/tie/loss and ranking criteria: league points, then head-to-head record among tied teams, then fewest
  goals against; goal difference and goals for are applied as fallbacks). The most recent game day uses
  the league's table directly, and the build logs a warning if the rebuilt table ever disagrees with it.
- [`.github/workflows/update-calendar.yml`](.github/workflows/update-calendar.yml) runs the script twice a
  day (14:23 and 20:23 UTC; off the hour because GitHub drops :00 schedules under load), and on any change
  to the config or script, then commits the result. GitHub Pages serves the `docs/` folder.
- If the API is unreachable the run retries a few times, then fails without committing, so subscribers keep
  the last good calendar. A run that would publish an empty calendar fails the same way instead of wiping
  everyone's events.

## Alerting

- A failed run opens a GitHub issue labelled `calendar-alert` (or comments on the open one) with a link to
  the run log, and the next successful run closes it.
- Each run pings a [healthchecks.io](https://healthchecks.io) check stored in the `HEALTHCHECK_URL`
  repository secret: success pings `$URL`, failure pings `$URL/fail`. If no ping arrives for a day,
  healthchecks.io emails the owner. This also catches GitHub silently disabling the schedule, which it does
  after 60 days without commits; re-enable it from the Actions tab if that happens.

## Next season

Nothing to do. New seasons appear in the program listing weeks before they start and are picked up
automatically once the league builds the schedule. The only reasons to touch `config.json` are the team
changing its name or league (`team`, `season_name_contains`), or Sports House moving to a new Bond Sports
program (`program_id`, the number in the league page URL).

API requests per run: one for the season listing, plus about five per live season (competition, ruleset,
standings, and one per stage for scores). Nothing is fetched for cached seasons.

## Running locally

```bash
export GAME_LOCATION="Venue name, Street, City, ST 00000"
python3 build_calendar.py --dry-run   # print the calendar
python3 build_calendar.py             # write docs/mai-tai.ics and state.json
python3 -m unittest discover -s tests # run the tests
```
