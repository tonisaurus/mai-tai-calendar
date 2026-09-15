import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_calendar as bc  # noqa: E402

TEAM = "Mai Tai"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def game(event_id=1, home="Mai Tai ", away="Barracuda ", status="scheduled", start=NOW + timedelta(days=1),
         home_score=None, away_score=None, stage="Regular Season", overtime=False, shootout=False):
    return bc.Game(
        event_id=event_id, stage_name=stage, status=status, start=start, end=start + timedelta(minutes=45),
        home=bc.Team(home, home_score), away=bc.Team(away, away_score), field="Field 1", note=None,
        overtime=overtime, shootout=shootout,
    )


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
    def test_select_games_keeps_ours_and_placeholders_sorted(self):
        ours = game(1, start=NOW + timedelta(days=3))
        theirs = game(2, home="Phantom FC ", away="Barracuda ")
        placeholder = game(3, home=None, away=None, stage="Playoffs", start=NOW + timedelta(days=10))
        self.assertEqual([g.event_id for g in bc.select_games([placeholder, theirs, ours], TEAM)], [1, 3])

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
        calendar, state = bc.build(self.CONFIG, games, STANDINGS, {}, NOW, self.LOCATION)
        again, state2 = bc.build(self.CONFIG, games, STANDINGS, state, NOW + timedelta(days=1), self.LOCATION)
        self.assertEqual(calendar, again)
        self.assertEqual(state, state2)
        self.assertIn("UID:bondsports-event-2@mai-tai-calendar", calendar)
        self.assertIn("SUMMARY:Mai Tai (3rd\\, 5-2) vs Barracuda (8th\\, 1-6)", calendar)
        self.assertIn("LOCATION:Sports House", calendar)
        self.assertTrue(calendar.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(calendar.endswith("END:VCALENDAR\r\n"))

    def test_rescheduled_game_bumps_sequence(self):
        games = [game(1)]
        _, state = bc.build(self.CONFIG, games, STANDINGS, {}, NOW, self.LOCATION)
        moved = [game(1, start=NOW + timedelta(days=2))]
        calendar, state2 = bc.build(self.CONFIG, moved, STANDINGS, state, NOW + timedelta(hours=1), self.LOCATION)
        self.assertEqual(state2["bondsports-event-1@mai-tai-calendar"]["sequence"], 1)
        self.assertIn("SEQUENCE:1", calendar)


class ParsingTests(unittest.TestCase):
    def test_parse_games_handles_placeholder(self):
        raw = [{
            "gameId": None, "eventId": 5727653, "stageId": None, "stageName": "Playoffs",
            "homeTeam": {"id": None, "name": None, "score": None}, "awayTeam": {"id": None, "name": None, "score": None},
            "status": "scheduled", "startDateTime": "2026-09-22T02:00:00.000Z", "endDateTime": "2026-09-22T02:45:00.000Z",
            "overtime": None, "shootout": None, "publicNote": None, "space": {"id": 7744, "name": "Field 1"},
        }]
        (g,) = bc.parse_games(raw)
        self.assertTrue(g.is_placeholder)
        self.assertEqual(g.start, datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc))

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
