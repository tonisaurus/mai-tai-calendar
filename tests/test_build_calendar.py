import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_calendar as bc  # noqa: E402

TEAM = "Mai Tai"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def game(event_id=1, home="Mai Tai ", away="Barracuda ", status="scheduled", start=NOW + timedelta(days=1),
         home_score=None, away_score=None, stage="Regular Season", overtime=False, shootout=False, division_id=907,
         season_id=1):
    return bc.Game(
        event_id=event_id, stage_name=stage, stage_type=stage.lower().replace(" ", "_"), status=status, start=start,
        end=start + timedelta(minutes=45), home=bc.Team(home, home_score), away=bc.Team(away, away_score),
        field="Field 1", note=None, overtime=overtime, shootout=shootout, season_id=season_id,
        division_id=division_id, division_name="Div", counts_for_standings=status == "final",
    )


def season(games, standings=None, season_id=1, ruleset=bc.Ruleset()):
    return bc.Season(id=season_id, name="Season", games=games, standings=standings, ruleset=ruleset)


def played(event_id, home, away, home_score, away_score, days_ago):
    return game(event_id, home, away, "final", NOW - timedelta(days=days_ago), home_score, away_score)


STANDINGS = bc.Standings(
    division="7. Monday Women's Jul-Sep 26",
    rows=[
        bc.Standing("Phantom FC", 1, 7, 0, 0, 21, 39, 8),
        bc.Standing("Mai Tai", 3, 5, 2, 0, 15, 34, 14),
        bc.Standing("Barracuda", 8, 1, 6, 0, 3, 10, 48),
        bc.Standing("Serendipity", 5, 3, 3, 1, 10, 20, 21),
    ],
)


class OrdinalTests(unittest.TestCase):
    def test_suffixes(self):
        expected = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th", 21: "21st", 22: "22nd", 111: "111th"}
        for n, text in expected.items():
            self.assertEqual(bc.ordinal(n), text)


class SummaryTests(unittest.TestCase):
    def test_featured_game_shows_rank_and_record_home_first(self):
        self.assertEqual(
            bc.build_summary(game(), TEAM, featured=True, standings=STANDINGS),
            "Mai Tai (3rd, 5-2) vs Barracuda (8th, 1-6)",
        )

    def test_featured_away_game_keeps_home_team_first(self):
        g = game(home="Barracuda ", away="Mai Tai ")
        self.assertEqual(bc.build_summary(g, TEAM, True, STANDINGS), "Barracuda (8th, 1-6) vs Mai Tai (3rd, 5-2)")

    def test_ties_appear_in_record(self):
        g = game(away="Serendipity ")
        self.assertEqual(bc.build_summary(g, TEAM, True, STANDINGS), "Mai Tai (3rd, 5-2) vs Serendipity (5th, 3-3-1)")

    def test_non_featured_upcoming_game_is_plain(self):
        self.assertEqual(bc.build_summary(game(), TEAM, False, STANDINGS), "Mai Tai vs Barracuda")

    def test_final_game_shows_score_and_result(self):
        g = game(status="final", home_score=7, away_score=1)
        self.assertEqual(bc.build_summary(g, TEAM, False, STANDINGS), "Mai Tai 7 - 1 Barracuda (W)")
        g = game(home="Barracuda ", away="Mai Tai ", status="final", home_score=2, away_score=0)
        self.assertEqual(bc.build_summary(g, TEAM, False, STANDINGS), "Barracuda 2 - 0 Mai Tai (L)")
        g = game(status="final", home_score=2, away_score=2, overtime=True)
        self.assertEqual(bc.build_summary(g, TEAM, False, STANDINGS), "Mai Tai 2 - 2 Barracuda (D, OT)")

    def test_final_game_never_shows_standings_even_if_featured(self):
        g = game(status="final", home_score=7, away_score=1)
        self.assertEqual(bc.build_summary(g, TEAM, True, STANDINGS), "Mai Tai 7 - 1 Barracuda (W)")

    def test_playoff_prefix_and_placeholder(self):
        self.assertEqual(bc.build_summary(game(home=None, away=None, stage="Playoffs"), TEAM, False, None), "Playoffs: TBD")
        g = game(stage="Playoffs")
        self.assertEqual(bc.build_summary(g, TEAM, True, STANDINGS), "Playoffs: Mai Tai (3rd, 5-2) vs Barracuda (8th, 1-6)")

    def test_cancelled(self):
        self.assertEqual(bc.build_summary(game(status="cancelled"), TEAM, True, STANDINGS), "CANCELLED: Mai Tai vs Barracuda")

    def test_missing_standings_falls_back_to_plain_names(self):
        self.assertEqual(bc.build_summary(game(), TEAM, True, None), "Mai Tai vs Barracuda")


class SelectionTests(unittest.TestCase):
    def test_select_games_keeps_ours_and_upcoming_playoff_placeholders_sorted(self):
        ours = game(1, start=NOW + timedelta(days=3))
        theirs = game(2, home="Phantom FC ", away="Barracuda ")
        placeholder = game(3, home=None, away=None, stage="Playoffs", start=NOW + timedelta(days=10))
        past_placeholder = game(4, home=None, away=None, stage="Playoffs", start=NOW - timedelta(days=60))
        regular_placeholder = game(5, home=None, away=None, start=NOW + timedelta(days=20))
        selected = bc.select_games([placeholder, theirs, ours, past_placeholder, regular_placeholder], TEAM, NOW)
        self.assertEqual([g.event_id for g in selected], [1, 3])

    def test_featured_is_next_unplayed_game_within_window(self):
        played = game(1, status="final", home_score=1, away_score=0, start=NOW - timedelta(days=7))
        upcoming = game(2, start=NOW + timedelta(days=1))
        later = game(3, start=NOW + timedelta(days=8, hours=1))
        self.assertIs(bc.pick_featured([played, upcoming, later], NOW), upcoming)
        self.assertIsNone(bc.pick_featured([played, later], NOW))

    def test_just_played_game_stays_featured_until_score_posts(self):
        recent = game(1, start=NOW - timedelta(hours=12))
        self.assertIs(bc.pick_featured([recent], NOW), recent)
        stale = game(2, start=NOW - timedelta(days=2))
        self.assertIsNone(bc.pick_featured([stale], NOW))

    def test_placeholders_are_never_featured(self):
        self.assertIsNone(bc.pick_featured([game(home=None, away=None, stage="Playoffs")], NOW))


class StandingsHistoryTests(unittest.TestCase):
    TZ = bc.ZoneInfo("America/Los_Angeles")
    # Week 1 (14 days ago): A beat B 3-1, C drew D 2-2. Week 2 (7 days ago): B beat A 1-0, D beat C 1-0.
    GAMES = [
        played(1, "A ", "B ", 3, 1, 14), played(2, "C ", "D ", 2, 2, 14),
        played(3, "B ", "A ", 1, 0, 7), played(4, "D ", "C ", 1, 0, 7),
        game(5, "A ", "C ", start=NOW + timedelta(days=1)),  # scheduled, must not count
        game(6, "X ", "Y ", "final", NOW - timedelta(days=7), 9, 0, division_id=1),  # another division
    ]

    def table(self, standings):
        return [(r.rank, r.team, r.record, r.points, r.goals_for, r.goals_against) for r in standings.rows]

    def test_records_points_and_tiebreaks(self):
        standings = bc.compute_standings(self.GAMES, 907, self.TZ, bc.Ruleset())
        # A and B are both 1-1 with 3 pts and split their head-to-head; A has GA 2 vs B's GA 3, so A ranks first.
        self.assertEqual(self.table(standings), [
            (1, "D", "1-0-1", 4, 3, 2), (2, "A", "1-1", 3, 3, 2), (3, "B", "1-1", 3, 2, 3), (4, "C", "0-1-1", 1, 2, 3),
        ])
        self.assertEqual(standings.division_id, 907)
        self.assertEqual(standings.division, "Div")

    def test_cutoff_only_counts_games_through_that_day(self):
        week1 = bc.compute_standings(self.GAMES, 907, self.TZ, bc.Ruleset(), through=(NOW - timedelta(days=14)).astimezone(self.TZ).date())
        self.assertEqual(self.table(week1), [
            (1, "A", "1-0", 3, 3, 1), (2, "C", "0-0-1", 1, 2, 2), (3, "D", "0-0-1", 1, 2, 2), (4, "B", "0-1", 0, 1, 3),
        ])

    def test_standings_after_uses_api_table_for_latest_week_and_rebuilds_older_weeks(self):
        api = bc.Standings(division="Div", rows=[bc.Standing("B", 1, 1, 1, 0, 3, 2, 3)], division_id=907)
        self.assertIs(bc.standings_after(self.GAMES[2], season(self.GAMES, api), self.TZ), api)
        older = bc.standings_after(self.GAMES[0], season(self.GAMES, api), self.TZ)
        self.assertIsNot(older, api)
        self.assertEqual(older.for_team("A").record, "1-0")
        other_division = bc.Standings(division="Other", rows=[], division_id=1)
        self.assertIsNot(bc.standings_after(self.GAMES[2], season(self.GAMES, other_division), self.TZ), other_division)

    def test_head_to_head_breaks_ties_before_goals_against(self):
        # P and Q both 2-1 on 6 pts. Q conceded fewer goals overall, but P won their meeting, so P ranks first.
        games = [
            played(1, "P ", "Q ", 1, 0, 21), played(2, "P ", "R ", 0, 5, 14), played(3, "P ", "S ", 2, 0, 7),
            played(4, "Q ", "R ", 3, 0, 14), played(5, "Q ", "S ", 1, 0, 7), played(6, "R ", "S ", 0, 1, 21),
        ]
        rows = bc.compute_standings(games, 907, self.TZ, bc.Ruleset()).rows
        self.assertEqual([(r.team, r.points, r.goals_against) for r in rows[:2]], [("P", 6, 5), ("Q", 6, 1)])

    def test_ruleset_points_and_fallback_criteria(self):
        # Two-point wins, and nothing in the published criteria separates E and F (never met, same GA):
        # the fallback goal-difference criterion does.
        rules = bc.Ruleset(win=2, tie=1, loss=0, criteria=("league_points", "points_against"))
        games = [played(1, "E ", "G ", 4, 1, 7), played(2, "F ", "H ", 2, 1, 7)]
        rows = bc.compute_standings(games, 907, self.TZ, rules).rows
        self.assertEqual([(r.team, r.points) for r in rows], [("E", 2), ("F", 2), ("H", 0), ("G", 0)])
        self.assertEqual(rules.ranking, ("league_points", "points_against", "point_differential", "points_scored"))

    def test_mismatch_detection(self):
        computed = bc.compute_standings(self.GAMES, 907, self.TZ, bc.Ruleset())
        self.assertEqual(bc.standings_mismatch(computed, computed), [])
        swapped = bc.Standings(division="Div", rows=[computed.rows[1], computed.rows[0]] + computed.rows[2:], division_id=907)
        self.assertTrue(bc.standings_mismatch(swapped, computed))


class DescriptionTests(unittest.TestCase):
    def test_played_game_layout(self):
        g = game(status="final", home_score=4, away_score=2, start=NOW - timedelta(days=1))
        text = bc.build_description(g, TEAM, STANDINGS)
        self.assertEqual(text.splitlines()[:5], [
            "Final: Mai Tai 4 - 2 Barracuda (W)", "Field 1", "", "Standings after this game:", "1. Phantom FC 7-0, 21 pts, GD +31",
        ])

    def test_featured_upcoming_game_uses_plain_heading(self):
        self.assertIn("\nStandings:\n1. Phantom FC", bc.build_description(game(), TEAM, STANDINGS))

    def test_upcoming_non_featured_game_has_no_table(self):
        text = bc.build_description(game(), TEAM, None)
        self.assertIn("Standings and records are added the week of the game.", text)
        self.assertNotIn("Standings:", text)


class IcsFormattingTests(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(bc.ics_escape("a, b; c\\d\ne"), "a\\, b\\; c\\\\d\\ne")

    def test_fold_respects_75_octets_and_utf8(self):
        line = "DESCRIPTION:" + "é" * 100
        folded = bc.ics_fold(line)
        self.assertTrue(all(len(part.encode("utf-8")) <= 75 for part in folded))
        self.assertTrue(all(part.startswith(" ") for part in folded[1:]))
        self.assertEqual("".join(p[1:] if i else p for i, p in enumerate(folded)), line)

    def test_short_line_not_folded(self):
        self.assertEqual(bc.ics_fold("SUMMARY:short"), ["SUMMARY:short"])


class StateTests(unittest.TestCase):
    def test_sequence_bumps_only_on_change(self):
        first = bc.next_state(None, "aaa", NOW)
        self.assertEqual(first["sequence"], 0)
        same = bc.next_state(first, "aaa", NOW + timedelta(days=1))
        self.assertIs(same, first)
        changed = bc.next_state(first, "bbb", NOW + timedelta(days=1))
        self.assertEqual(changed["sequence"], 1)
        self.assertEqual(changed["last_modified"], (NOW + timedelta(days=1)).isoformat())


class BuildTests(unittest.TestCase):
    CONFIG = {"team": TEAM, "calendar_name": "Mai Tai Soccer", "timezone": "America/Los_Angeles"}
    LOCATION = "Sports House"

    def test_build_is_stable_when_nothing_changes(self):
        games = [game(1, status="final", home_score=3, away_score=1, start=NOW - timedelta(days=7)), game(2)]
        seasons = [season(games, STANDINGS)]
        calendar, state = bc.build(self.CONFIG, seasons, {}, NOW, self.LOCATION)
        again, state2 = bc.build(self.CONFIG, seasons, state, NOW + timedelta(days=1), self.LOCATION)
        self.assertEqual(calendar, again)
        self.assertEqual(state, state2)
        self.assertIn("UID:bondsports-event-2@mai-tai-calendar", calendar)
        self.assertIn("SUMMARY:Mai Tai (3rd\\, 5-2) vs Barracuda (8th\\, 1-6)", calendar)
        self.assertIn("LOCATION:Sports House", calendar)
        self.assertTrue(calendar.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(calendar.endswith("END:VCALENDAR\r\n"))

    def test_rescheduled_game_bumps_sequence(self):
        _, state = bc.build(self.CONFIG, [season([game(1)], STANDINGS)], {}, NOW, self.LOCATION)
        moved = [season([game(1, start=NOW + timedelta(days=2))], STANDINGS)]
        calendar, state2 = bc.build(self.CONFIG, moved, state, NOW + timedelta(hours=1), self.LOCATION)
        self.assertEqual(state2["bondsports-event-1@mai-tai-calendar"]["sequence"], 1)
        self.assertIn("SEQUENCE:1", calendar)


class FetchTests(unittest.TestCase):
    def response(self, payload):
        body = io.BytesIO(json.dumps(payload).encode())
        return mock.MagicMock(__enter__=lambda s: body, __exit__=lambda *a: False)

    def test_retries_transient_errors_then_succeeds(self):
        calls = [urllib.error.URLError("boom"), urllib.error.HTTPError("u", 503, "down", {}, None), self.response({"ok": 1})]
        with mock.patch.object(bc.urllib.request, "urlopen", side_effect=calls) as urlopen, \
                mock.patch.object(bc.time, "sleep") as sleep, redirect_stderr(io.StringIO()):
            self.assertEqual(bc.fetch_json("https://example.test"), {"ok": 1})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])

    def test_empty_body_means_nothing_here_yet(self):
        body = io.BytesIO(b"")
        with mock.patch.object(bc.urllib.request, "urlopen", return_value=mock.MagicMock(__enter__=lambda s: body, __exit__=lambda *a: False)):
            self.assertIsNone(bc.fetch_json("https://example.test"))

    def test_does_not_retry_client_errors(self):
        with mock.patch.object(bc.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 404, "gone", {}, None)) as urlopen, \
                mock.patch.object(bc.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                bc.fetch_json("https://example.test")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_gives_up_after_last_attempt(self):
        with mock.patch.object(bc.urllib.request, "urlopen", side_effect=urllib.error.URLError("boom")) as urlopen, \
                mock.patch.object(bc.time, "sleep"), redirect_stderr(io.StringIO()):
            with self.assertRaises(urllib.error.URLError):
                bc.fetch_json("https://example.test")
        self.assertEqual(urlopen.call_count, bc.FETCH_ATTEMPTS)


class MainTests(unittest.TestCase):
    def test_refuses_to_publish_empty_calendar(self):
        config = {"team": TEAM, "calendar_name": "x", "timezone": "UTC", "api_base": "https://api.test", "output": "out.ics",
                  "state_file": "state.json", "cache_dir": "seasons", "program_id": 1, "season_name_contains": "Monday"}
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(config))
            stderr = io.StringIO()
            with mock.patch.object(bc, "fetch_json", return_value={"data": []}), mock.patch.dict("os.environ", {"GAME_LOCATION": "Venue"}), \
                    redirect_stderr(stderr):
                code = bc.main(["--config", str(config_path)])
            self.assertEqual(code, 1)
            self.assertIn("refusing to publish an empty calendar", stderr.getvalue())
            self.assertFalse((Path(tmp) / "out.ics").exists())


class DiscoveryTests(unittest.TestCase):
    LISTING = {"data": [
        {"id": 1, "name": "01. Monday Women's May-July 26", "startDate": "2026-05-18", "endDate": "2026-07-14"},
        {"id": 2, "name": "02. Tuesday Over 30 May-July 26", "startDate": "2026-05-05", "endDate": "2026-06-24"},
        {"id": 3, "name": "13. Monday Women's Sept-Nov 26", "startDate": "2026-09-28", "endDate": "2026-11-23"},
    ]}

    def test_discover_filters_by_name_case_insensitively(self):
        with mock.patch.object(bc, "fetch_json", return_value=self.LISTING):
            found = bc.discover_seasons("https://api.test", 13888, "monday women's")
            self.assertEqual([s["id"] for s in found], [1, 3])
            self.assertEqual(len(bc.discover_seasons("https://api.test", 13888, None)), 3)

    def test_live_window(self):
        upcoming = self.LISTING["data"][2]
        self.assertFalse(bc.season_is_live(upcoming, bc.date(2026, 8, 1)))   # more than 45 days before start
        self.assertTrue(bc.season_is_live(upcoming, bc.date(2026, 9, 15)))
        self.assertTrue(bc.season_is_live(upcoming, bc.date(2026, 12, 10)))  # within 21 days after end
        self.assertFalse(bc.season_is_live(upcoming, bc.date(2026, 12, 20)))
        self.assertTrue(bc.season_is_over(upcoming, bc.date(2026, 12, 20)))

    def bundle(self, season_id, name, home="Mai Tai "):
        return {
            "season": {"id": season_id, "name": name, "startDate": "2026-05-18", "endDate": "2026-07-14"},
            "competition": {"uuid": "u", "stages": [{"id": 10, "name": "Regular Season", "stageType": "regular_season"}]},
            "ruleset": None, "standings": [],
            "games": {"10": [{"eventId": season_id * 100, "stageName": "Regular Season", "status": "scheduled",
                              "startDateTime": "2026-06-01T02:00:00.000Z", "endDateTime": "2026-06-01T02:45:00.000Z",
                              "homeTeam": {"name": home, "score": None, "divisionId": 1, "divisionName": "D"},
                              "awayTeam": {"name": "Other ", "score": None, "divisionId": 1, "divisionName": "D"}}]},
        }

    def test_parse_season_skips_seasons_without_the_team(self):
        self.assertIsNone(bc.parse_season(self.bundle(1, "s", home="Someone Else "), TEAM))
        parsed = bc.parse_season(self.bundle(1, "s"), TEAM)
        self.assertEqual([g.event_id for g in parsed.games], [100])

    def test_load_seasons_fetches_live_and_uncached_seasons_and_keeps_cached_ones(self):
        config = {"api_base": "https://api.test", "team": TEAM, "program_id": 1, "season_name_contains": "Monday"}
        today = bc.date(2026, 9, 15)
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # Season 9 is cached but no longer listed by the API; season 1 is over and uncached; 3 is live.
            (cache / "9.json").write_text(json.dumps(self.bundle(9, "old")))
            fetched = []

            def fake_fetch_season(api_base, season, team):
                fetched.append(season["id"])
                return None if season["id"] == 3 else self.bundle(season["id"], season["name"])

            listed = [self.LISTING["data"][0], self.LISTING["data"][2]]
            with mock.patch.object(bc, "discover_seasons", return_value=listed), \
                    mock.patch.object(bc, "fetch_season", side_effect=fake_fetch_season):
                seasons = bc.load_seasons(config, cache, today)
                self.assertEqual(sorted(fetched), [1, 3])
                self.assertEqual(sorted(s.id for s in seasons), [1, 9])
                self.assertTrue((cache / "1.json").exists())

                # Next run: season 1 is now cached, so only the live season is fetched again.
                fetched.clear()
                seasons = bc.load_seasons(config, cache, today)
                self.assertEqual(fetched, [3])
                self.assertEqual(sorted(s.id for s in seasons), [1, 9])


class ParsingTests(unittest.TestCase):
    def test_parse_games_handles_placeholder(self):
        raw = [{
            "gameId": None, "eventId": 5727653, "stageId": None, "stageName": "Playoffs",
            "homeTeam": {"id": None, "name": None, "score": None}, "awayTeam": {"id": None, "name": None, "score": None},
            "status": "scheduled", "startDateTime": "2026-09-22T02:00:00.000Z", "endDateTime": "2026-09-22T02:45:00.000Z",
            "overtime": None, "shootout": None, "publicNote": None, "space": {"id": 7744, "name": "Field 1"},
        }]
        (g,) = bc.parse_games(raw, season_id=7, stage_type="playoffs")
        self.assertTrue(g.is_placeholder)
        self.assertEqual(g.start, datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc))
        self.assertEqual((g.season_id, g.stage_type), (7, "playoffs"))

    def test_parse_ruleset_reads_points_and_criteria(self):
        rules = bc.parse_ruleset({"pointsForWin": 3, "pointsForTie": 1, "pointsForLoss": 0,
                                  "rankingCriteria": ["league_points", "head_to_head_record", "points_against"]})
        self.assertEqual(rules, bc.Ruleset())
        self.assertEqual(bc.parse_ruleset(None), bc.Ruleset())

    def test_parse_standings_picks_division_with_team(self):
        raw = [
            {"divisionName": "Other", "standings": [{"team": {"name": "X "}, "rank": 1, "wins": 1, "losses": 0, "gamesPlayed": 1, "points": 3, "pointsScored": 1, "pointsAgainst": 0}]},
            {"divisionName": "Ours", "standings": [{"team": {"name": "Mai Tai "}, "rank": 2, "wins": 5, "losses": 2, "gamesPlayed": 8, "points": 16, "pointsScored": 34, "pointsAgainst": 14}]},
        ]
        standings = bc.parse_standings(raw, TEAM)
        self.assertEqual(standings.division, "Ours")
        self.assertEqual(standings.for_team(TEAM).record, "5-2-1")
        self.assertIsNone(bc.parse_standings([], TEAM))


if __name__ == "__main__":
    unittest.main()
