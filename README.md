# Mai Tai Soccer Calendar

A subscribable calendar of Mai Tai's indoor soccer games, generated from the
[Bond Sports](https://bondsports.co) public API and hosted on GitHub Pages.

**Subscribe:** https://tonisaurus.github.io/mai-tai-calendar/

## What the calendar shows

| Game state | Event title |
|---|---|
| This week's game | `Mai Tai (3rd, 5-2) vs Milan Mujeres (2nd, 6-1)` plus the full division standings in the description |
| Later games | `Mai Tai vs Manchester` |
| Played games | `Mai Tai 7 - 1 Barracuda (W)` |
| Playoff slots before the bracket is set | `Playoffs: TBD` (one event per slot; slots Mai Tai does not play in disappear once matchups are announced) |

The home team is always listed first. The venue address is not stored in this repository; it is set as the
`GAME_LOCATION` repository variable (Settings > Secrets and variables > Actions > Variables) and
written into each event's location.

## How it works

- [`build_calendar.py`](build_calendar.py) fetches `game-scores` and `standings` for every stage in
  [`config.json`](config.json), keeps the games involving the team (plus unassigned placeholder slots),
  and writes [`docs/mai-tai.ics`](docs/mai-tai.ics). Python 3.9+, no dependencies.
- Each event's `UID` is the Bond Sports `eventId`, which is stable even before a game is played, so a
  rescheduled game or a posted score updates the existing calendar entry instead of creating a new one.
- [`state.json`](state.json) remembers a content hash per event so `SEQUENCE` and `LAST-MODIFIED` only
  change when an event actually changes. That also means the daily run only commits when there is news.
- [`.github/workflows/update-calendar.yml`](.github/workflows/update-calendar.yml) runs the script once a
  day (14:00 UTC, a few hours after Monday night games), and on any change to the config or script, then
  commits the result. GitHub Pages serves the `docs/` folder.
- If the API is unreachable the run retries a few times, then fails without committing, so subscribers keep
  the last good calendar. A run that would publish an empty calendar (for example after the league changes
  its ids) fails the same way instead of wiping everyone's events.

## Alerting

- A failed run opens a GitHub issue labelled `calendar-alert` (or comments on the open one) with a link to
  the run log, and the next successful run closes it.
- Each run pings a [healthchecks.io](https://healthchecks.io) check stored in the `HEALTHCHECK_URL`
  repository secret: success pings `$URL`, failure pings `$URL/fail`. If no ping arrives for a day,
  healthchecks.io emails the owner. This also catches GitHub silently disabling the schedule, which it does
  after 60 days without commits; re-enable it from the Actions tab if that happens.

## Next season

Bond Sports gives every season a new competition id and new stage ids. Find them in the network tab of
the league's public schedule page (requests to `api.bondsports.co/v4/competitions/<id>/stages/<n>/...`),
then add a season to `config.json`:

```json
"seasons": [
  { "name": "Jul-Sep 2026", "competition_id": "819c0134-...", "stage_ids": [853, 854] },
  { "name": "Oct-Dec 2026", "competition_id": "<new id>", "stage_ids": [<regular>, <playoffs>] }
]
```

Keep the newest season last: standings are taken from the last season that has them. Old seasons can stay
so past results remain in the calendar, or be removed once their endpoints stop responding.

## Running locally

```bash
export GAME_LOCATION="Venue name, Street, City, ST 00000"
python3 build_calendar.py --dry-run   # print the calendar
python3 build_calendar.py             # write docs/mai-tai.ics and state.json
python3 -m unittest discover -s tests # run the tests
```
