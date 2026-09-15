#!/usr/bin/env python3
"""Build an iCalendar (.ics) feed of one team's games from the Bond Sports public API.

Seasons are discovered automatically: the program's season list is filtered by name,
each season is mapped to its competition and stages, and the schedule, standings and
scoring rules are fetched from there. Finished seasons are cached in the repository so
their results stay in the calendar without being refetched every run.

Each game keeps a stable UID (the Bond Sports event id), so rescheduled games, posted
scores and standings changes show up as updates to existing calendar events rather
than duplicates.

Stdlib only, so it runs anywhere Python 3.9+ is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# A game this far in the future counts as "this week's game" and gets the
# standings/record annotation. A game that started less than FEATURED_GRACE ago
# but has no final score yet keeps the annotation until the score is posted.
FEATURED_WINDOW = timedelta(days=8)
FEATURED_GRACE = timedelta(hours=24)

# A season is fetched live from this long before it starts (the league builds the
# schedule about a week ahead) until this long after it ends (playoff scores and
# late corrections). Outside that window it is served from the cache.
LIVE_BEFORE_START = timedelta(days=14)
LIVE_AFTER_END = timedelta(days=14)

# Every season the team has played stays in the calendar (cached seasons cost no requests).
# Set "keep_seasons" in config.json to a number to keep only that many started seasons
# (the current one counts); upcoming seasons are always kept.
DEFAULT_KEEP_SEASONS: Optional[int] = None

REGULAR_SEASON = "regular_season"
FINAL = "final"
CANCELLED_STATUSES = {"cancelled", "canceled"}

ICS_LINE_LIMIT = 75  # octets, per RFC 5545 section 3.1

# The venue address is kept out of the repository; it comes from this environment
# variable (a GitHub Actions repository variable in CI).
LOCATION_ENV = "GAME_LOCATION"


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Team:
    name: Optional[str]  # None while the league has not assigned a team yet
    score: Optional[int]


@dataclass(frozen=True)
class Game:
    event_id: int
    stage_name: str
    stage_type: str
    status: str
    start: datetime
    end: datetime
    home: Team
    away: Team
    field: Optional[str]
    note: Optional[str]
    overtime: bool
    shootout: bool
    season_id: int = 0
    division_id: Optional[int] = None
    division_name: Optional[str] = None
    counts_for_standings: bool = True

    def involves(self, team: str) -> bool:
        return same_team(self.home.name, team) or same_team(self.away.name, team)

    @property
    def is_placeholder(self) -> bool:
        """True for a scheduled slot whose teams the league has not announced."""
        return self.home.name is None and self.away.name is None

    @property
    def is_final(self) -> bool:
        return self.status == FINAL

    @property
    def is_cancelled(self) -> bool:
        return self.status in CANCELLED_STATUSES

    @property
    def has_result(self) -> bool:
        return self.is_final and self.home.score is not None and self.away.score is not None


@dataclass(frozen=True)
class Standing:
    team: str
    rank: int
    wins: int
    losses: int
    ties: int
    points: int
    goals_for: int
    goals_against: int

    @property
    def record(self) -> str:
        record = f"{self.wins}-{self.losses}"
        return f"{record}-{self.ties}" if self.ties else record

    @property
    def goal_diff(self) -> int:
        return self.goals_for - self.goals_against


@dataclass(frozen=True)
class Standings:
    division: str
    rows: list[Standing]
    division_id: Optional[int] = None

    def for_team(self, team: str) -> Optional[Standing]:
        return next((row for row in self.rows if same_team(row.team, team)), None)


# Applied after the league's published ranking criteria, so teams the rules leave tied are
# still ordered the way readers expect rather than alphabetically.
FALLBACK_CRITERIA = ("point_differential", "points_scored")


@dataclass(frozen=True)
class Ruleset:
    """League points and ranking criteria, as published by the API's stage ruleset."""
    win: int = 3
    tie: int = 1
    loss: int = 0
    criteria: tuple[str, ...] = ("league_points", "head_to_head_record", "points_against")

    @property
    def ranking(self) -> tuple[str, ...]:
        return self.criteria + tuple(c for c in FALLBACK_CRITERIA if c not in self.criteria)


@dataclass(frozen=True)
class Season:
    id: int
    name: str
    games: list[Game]
    standings: Optional[Standings]  # the league's live table, when the season is current
    ruleset: Ruleset


def same_team(a: Optional[str], b: Optional[str]) -> bool:
    """Bond Sports team names carry trailing whitespace, so compare loosely."""
    if a is None or b is None:
        return False
    return a.strip().casefold() == b.strip().casefold()


def clean_name(name: Optional[str]) -> str:
    return name.strip() if name else "TBD"


# ------------------------------------------------------------------------ fetching


FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 5


def fetch_json(url: str):
    """GET a JSON document, retrying transient failures (network errors, 5xx) with backoff.

    4xx responses are returned to the caller immediately: they mean the URL is wrong
    (e.g. a stale competition id), and retrying will not fix that. An empty body
    (which the API uses for "nothing here yet") comes back as None.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "team-calendar/1.0"})
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
            return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == FETCH_ATTEMPTS:
                raise
            error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == FETCH_ATTEMPTS:
                raise
            error = exc
        delay = FETCH_BACKOFF_SECONDS * attempt
        print(f"warning: {url} failed ({error}); retrying in {delay}s", file=sys.stderr)
        time.sleep(delay)


def parse_datetime(value: str) -> datetime:
    # API returns e.g. "2026-09-15T03:40:00.000Z"; Python 3.9 fromisoformat rejects "Z".
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def parse_games(raw: list, season_id: int, stage_type: str) -> list[Game]:
    games = []
    for item in raw:
        games.append(
            Game(
                event_id=int(item["eventId"]),
                stage_name=item.get("stageName") or "Regular Season",
                stage_type=stage_type,
                status=(item.get("status") or "scheduled").lower(),
                start=parse_datetime(item["startDateTime"]),
                end=parse_datetime(item["endDateTime"]),
                home=Team(item["homeTeam"].get("name"), item["homeTeam"].get("score")),
                away=Team(item["awayTeam"].get("name"), item["awayTeam"].get("score")),
                field=(item.get("space") or {}).get("name"),
                note=item.get("publicNote"),
                overtime=bool(item.get("overtime")),
                shootout=bool(item.get("shootout")),
                season_id=season_id,
                division_id=item["homeTeam"].get("divisionId"),
                division_name=item["homeTeam"].get("divisionName"),
                counts_for_standings=item.get("includedInStandings") is True,
            )
        )
    return games


def parse_standings(raw: Optional[list], team: str) -> Optional[Standings]:
    """Return the standings of the division containing `team`, if any."""
    for division in raw or []:
        rows = []
        for entry in division.get("standings", []):
            played = entry.get("gamesPlayed") or 0
            wins = entry.get("wins") or 0
            losses = entry.get("losses") or 0
            rows.append(
                Standing(
                    team=clean_name(entry["team"]["name"]),
                    rank=int(entry["rank"]),
                    wins=wins,
                    losses=losses,
                    ties=max(played - wins - losses, 0),
                    points=entry.get("points") or 0,
                    goals_for=entry.get("pointsScored") or 0,
                    goals_against=entry.get("pointsAgainst") or 0,
                )
            )
        standings = Standings(
            division=division.get("divisionName", ""),
            rows=sorted(rows, key=lambda r: r.rank),
            division_id=division.get("divisionId"),
        )
        if standings.for_team(team):
            return standings
    return None


def parse_ruleset(raw: Optional[dict]) -> Ruleset:
    if not raw:
        return Ruleset()
    default = Ruleset()
    return Ruleset(
        win=raw.get("pointsForWin", default.win),
        tie=raw.get("pointsForTie", default.tie),
        loss=raw.get("pointsForLoss", default.loss),
        criteria=tuple(raw.get("rankingCriteria") or default.criteria),
    )


# ----------------------------------------------------------------------- discovery


def discover_seasons(api_base: str, program_id: int, name_contains: Optional[str]) -> list[dict]:
    """The program's seasons whose name contains the configured text (case-insensitive)."""
    listing = fetch_json(f"{api_base}/programs-seasons/program/{program_id}") or {}
    seasons = listing.get("data", []) if isinstance(listing, dict) else listing
    needle = (name_contains or "").strip().casefold()
    return [
        {"id": int(s["id"]), "name": s["name"], "startDate": s["startDate"], "endDate": s["endDate"]}
        for s in seasons
        if needle in s["name"].casefold()
    ]


def season_is_live(season: dict, today: date) -> bool:
    start = date.fromisoformat(season["startDate"]) - LIVE_BEFORE_START
    end = date.fromisoformat(season["endDate"]) + LIVE_AFTER_END
    return start <= today <= end


def season_is_over(season: dict, today: date) -> bool:
    return date.fromisoformat(season["endDate"]) + LIVE_AFTER_END < today


def fetch_season(api_base: str, season: dict, team: str, cached: Optional[dict] = None) -> Optional[dict]:
    """Fetch everything about one season as raw API responses, or None if it has no competition yet.

    The ruleset never changes once a season is set up, so it is reused from `cached` when present.
    """
    competition = fetch_json(f"{api_base}/program_seasons/{season['id']}/competition")
    if not competition or not competition.get("uuid"):
        return None
    stages = [
        {"id": int(s["id"]), "name": s.get("name") or "", "stageType": s.get("stageType") or ""}
        for s in competition.get("stages") or []
    ]
    # Regular season first: it is where the standings and the ruleset live.
    stages.sort(key=lambda s: s["stageType"] != REGULAR_SEASON)

    base = f"{api_base}/competitions/{competition['uuid']}/stages"
    games = {str(s["id"]): fetch_json(f"{base}/{s['id']}/game-scores") or [] for s in stages}

    standings: list = []
    for stage in stages:
        raw = fetch_json(f"{base}/{stage['id']}/standings") or []
        if parse_standings(raw, team):
            standings = raw
            break

    ruleset = (cached or {}).get("ruleset")
    if stages and ruleset is None:
        try:
            ruleset = fetch_json(f"{base}/{stages[0]['id']}/ruleset")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise

    return {
        "season": season,
        "competition": {"uuid": competition["uuid"], "stages": stages},
        "ruleset": ruleset,
        "standings": standings,
        "games": games,
    }


def parse_season(bundle: dict, team: str) -> Optional[Season]:
    """Turn a raw bundle into a Season, or None when the team does not take part in it."""
    season_id = int(bundle["season"]["id"])
    games: list[Game] = []
    for stage in bundle["competition"]["stages"]:
        games.extend(parse_games(bundle["games"].get(str(stage["id"]), []), season_id, stage["stageType"]))
    standings = parse_standings(bundle.get("standings"), team)
    if standings is None and not any(g.involves(team) for g in games):
        return None
    return Season(
        id=season_id,
        name=bundle["season"]["name"],
        games=games,
        standings=standings,
        ruleset=parse_ruleset(bundle.get("ruleset")),
    )


def seasons_to_keep(seasons: list[dict], today: date, keep: Optional[int]) -> set[int]:
    """Ids of the `keep` most recently started seasons (all of them when `keep` is None), plus every
    season that has not started yet."""
    started = sorted((s for s in seasons if date.fromisoformat(s["startDate"]) <= today), key=lambda s: s["startDate"])
    if keep is not None:
        started = started[-keep:] if keep > 0 else []
    return {s["id"] for s in started} | {s["id"] for s in seasons if date.fromisoformat(s["startDate"]) > today}


def load_seasons(config: dict, cache_dir: Path, today: date) -> list[Season]:
    """The seasons that belong in the calendar: live ones from the API, finished ones from the cache.

    A finished season that is not cached yet is fetched once and cached. Seasons older than the
    retention limit are dropped, along with their cache files; the git history still has them.
    """
    api_base, team = config["api_base"], config["team"]
    cached = {int(p.stem): json.loads(p.read_text(encoding="utf-8")) for p in cache_dir.glob("*.json")}
    listed = {s["id"]: s for s in discover_seasons(api_base, config["program_id"], config.get("season_name_contains"))}
    known = {**{sid: b["season"] for sid, b in cached.items()}, **listed}  # the listing is the fresher source
    kept = seasons_to_keep(list(known.values()), today, config.get("keep_seasons", DEFAULT_KEEP_SEASONS))

    for season_id in cached:
        if season_id not in kept:
            (cache_dir / f"{season_id}.json").unlink()

    bundles: dict[int, dict] = {}
    for season_id, season in known.items():
        if season_id not in kept:
            continue
        if season_id in listed and (season_is_live(season, today) or (season_is_over(season, today) and season_id not in cached)):
            bundle = fetch_season(api_base, season, team, cached.get(season_id))
            if bundle is None:
                continue  # the league has not built this season's schedule yet
            bundles[season_id] = bundle
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / f"{season_id}.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        elif season_id in cached:
            bundles[season_id] = cached[season_id]

    seasons = [parse_season(b, team) for b in bundles.values()]
    return sorted((s for s in seasons if s is not None), key=lambda s: min((g.start for g in s.games), default=datetime.max.replace(tzinfo=timezone.utc)))


# ----------------------------------------------------------------- standings history


@dataclass
class Tally:
    wins: int = 0
    losses: int = 0
    ties: int = 0
    goals_for: int = 0
    goals_against: int = 0

    def points(self, rules: Ruleset) -> int:
        return self.wins * rules.win + self.ties * rules.tie + self.losses * rules.loss


def counted_games(games: list[Game], division_id: Optional[int], tz: ZoneInfo, through: Optional[date] = None) -> list[Game]:
    """Division games that count toward the table, optionally only those played on or before a local date."""
    return [
        g for g in games
        if g.division_id == division_id and g.counts_for_standings and g.has_result
        and (through is None or g.start.astimezone(tz).date() <= through)
    ]


def rank_teams(tally: dict[str, Tally], results: list[Game], rules: Ruleset) -> list[str]:
    """Order teams by the league's ranking criteria, applied in turn to break ties.

    Supported criteria are the ones Bond Sports publishes: league_points, head_to_head_record
    (points earned in games among the tied teams), points_against, point_differential,
    points_scored and wins. Anything still tied after the fallbacks is ordered by name.
    """
    def head_to_head(team: str, group: list[str]) -> int:
        points = 0
        for g in results:
            home, away = clean_name(g.home.name), clean_name(g.away.name)
            if team not in (home, away) or not {home, away} <= set(group):
                continue
            ours, theirs = (g.home.score, g.away.score) if home == team else (g.away.score, g.home.score)
            points += rules.win if ours > theirs else rules.tie if ours == theirs else rules.loss
        return points

    def metric(criterion: str, team: str, group: list[str]) -> int:
        t = tally[team]
        return {
            "league_points": lambda: t.points(rules),
            "head_to_head_record": lambda: head_to_head(team, group),
            "points_against": lambda: -t.goals_against,
            "point_differential": lambda: t.goals_for - t.goals_against,
            "points_scored": lambda: t.goals_for,
            "wins": lambda: t.wins,
        }.get(criterion, lambda: 0)()

    def order(group: list[str], criteria: tuple[str, ...]) -> list[str]:
        if len(group) == 1:
            return group
        if not criteria:
            return sorted(group, key=str.casefold)
        keyed = {team: metric(criteria[0], team, group) for team in group}
        ordered: list[str] = []
        for value in sorted(set(keyed.values()), reverse=True):
            ordered.extend(order([team for team in group if keyed[team] == value], criteria[1:]))
        return ordered

    return order(sorted(tally), rules.ranking)


def compute_standings(games: list[Game], division_id: Optional[int], tz: ZoneInfo, rules: Ruleset, through: Optional[date] = None) -> Standings:
    """Rebuild a division table from results, as it stood at the end of `through` (default: now)."""
    division_games = [g for g in games if g.division_id == division_id]
    tally: dict[str, Tally] = {}
    for g in division_games:
        for name in (g.home.name, g.away.name):
            if name:
                tally.setdefault(clean_name(name), Tally())

    results = counted_games(division_games, division_id, tz, through)
    for g in results:
        home, away = tally[clean_name(g.home.name)], tally[clean_name(g.away.name)]
        hs, as_ = g.home.score, g.away.score
        home.goals_for += hs; home.goals_against += as_
        away.goals_for += as_; away.goals_against += hs
        if hs > as_:
            home.wins += 1; away.losses += 1
        elif hs < as_:
            away.wins += 1; home.losses += 1
        else:
            home.ties += 1; away.ties += 1

    rows = [
        Standing(team=name, rank=rank, wins=tally[name].wins, losses=tally[name].losses, ties=tally[name].ties,
                 points=tally[name].points(rules), goals_for=tally[name].goals_for, goals_against=tally[name].goals_against)
        for rank, name in enumerate(rank_teams(tally, results, rules), start=1)
    ]
    division_name = next((g.division_name for g in division_games if g.division_name), "") or ""
    return Standings(division=division_name, rows=rows, division_id=division_id)


def standings_after(game: Game, season: Season, tz: ZoneInfo) -> Standings:
    """The table as it stood at the end of the day `game` was played.

    When that day is the latest with results, the league's own table is current and its ranks are
    authoritative, so prefer it; otherwise rebuild the table from results up to that day.
    """
    through = game.start.astimezone(tz).date()
    api = season.standings
    if api is not None and api.division_id == game.division_id:
        latest = max((g.start.astimezone(tz).date() for g in counted_games(season.games, game.division_id, tz)), default=None)
        if latest is not None and latest <= through:
            return api
    return compute_standings(season.games, game.division_id, tz, season.ruleset, through)


def standings_mismatch(api: Standings, computed: Standings) -> list[str]:
    """Rows where our rebuilt table disagrees with the league's, so a wrong tiebreak or points rule is noticed."""
    def key(rows):
        return [(r.rank, r.team, r.record, r.points) for r in rows]
    if key(api.rows) == key(computed.rows):
        return []
    return [f"api: {r.rank}. {r.team} {r.record} {r.points} pts" for r in api.rows] + \
           [f"computed: {r.rank}. {r.team} {r.record} {r.points} pts" for r in computed.rows]


# ------------------------------------------------------------------------ selection


def select_games(games: list[Game], team: str, now: datetime) -> list[Game]:
    """Our games, plus upcoming unassigned playoff slots.

    Regular-season placeholders are skipped because a freshly created season can be nothing but
    placeholders, and past ones are skipped because a slot that was never assigned is just noise.
    """
    def keep(g: Game) -> bool:
        if g.involves(team):
            return True
        return g.is_placeholder and g.stage_type != REGULAR_SEASON and g.end >= now

    return sorted((g for g in games if keep(g)), key=lambda g: g.start)


def pick_featured(games: list[Game], now: datetime) -> Optional[Game]:
    """The single upcoming game that gets the standings/record annotation."""
    for game in games:  # already sorted by start
        if game.is_final or game.is_cancelled or game.is_placeholder:
            continue
        if now - FEATURED_GRACE <= game.start <= now + FEATURED_WINDOW:
            return game
    return None


# ------------------------------------------------------------------------ rendering


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def annotate(name: Optional[str], standings: Optional[Standings]) -> str:
    """'Mai Tai (3rd, 5-2)' when standings are known, else just the name."""
    label = clean_name(name)
    row = standings.for_team(name) if standings and name else None
    if row is None:
        return label
    return f"{label} ({ordinal(row.rank)}, {row.record})"


def result_letter(game: Game, team: str) -> str:
    ours, theirs = (game.home, game.away) if same_team(game.home.name, team) else (game.away, game.home)
    if ours.score is None or theirs.score is None:
        return ""
    if ours.score > theirs.score:
        return "W"
    if ours.score < theirs.score:
        return "L"
    return "D"


def stage_prefix(game: Game) -> str:
    return "" if game.stage_type == REGULAR_SEASON else f"{game.stage_name}: "


def build_summary(game: Game, team: str, featured: bool, standings: Optional[Standings]) -> str:
    home, away = clean_name(game.home.name), clean_name(game.away.name)
    if game.is_cancelled:
        return f"CANCELLED: {stage_prefix(game)}{home} vs {away}"
    if game.is_placeholder:
        return f"{stage_prefix(game)}TBD"
    if game.has_result:
        tags = [t for t in (result_letter(game, team), "OT" if game.overtime else "", "SO" if game.shootout else "") if t]
        suffix = f" ({', '.join(tags)})" if tags else ""
        return f"{stage_prefix(game)}{home} {game.home.score} - {game.away.score} {away}{suffix}"
    if featured:
        return f"{stage_prefix(game)}{annotate(game.home.name, standings)} vs {annotate(game.away.name, standings)}"
    return f"{stage_prefix(game)}{home} vs {away}"


def standings_table(standings: Standings, heading: str) -> list[str]:
    # Calendar apps render descriptions in proportional fonts, so keep rows compact rather than column-aligned.
    lines = [heading]
    for row in standings.rows:
        lines.append(f"{row.rank}. {row.team} {row.record}, {row.points} pts, GD {row.goal_diff:+d}")
    return lines


def build_description(game: Game, team: str, standings: Optional[Standings]) -> str:
    """`standings` is the table to show: pre-game for this week's game, end-of-that-day for played games."""
    lines: list[str] = []
    home, away = clean_name(game.home.name), clean_name(game.away.name)

    if game.is_placeholder:
        lines.append(f"{game.stage_name} slot - teams not yet announced.")
        lines.append(f"This event will update once the league sets the matchups, and will disappear if {team} is not playing in this slot.")
    elif game.is_cancelled:
        lines.append("This game has been cancelled by the league.")
    elif game.has_result:
        tags = [t for t in (result_letter(game, team), "OT" if game.overtime else "", "SO" if game.shootout else "") if t]
        suffix = f" ({', '.join(tags)})" if tags else ""
        lines.append(f"Final: {home} {game.home.score} - {game.away.score} {away}{suffix}")
    else:
        lines.append(f"{home} (home) vs {away} (away)")
        if game.stage_type != REGULAR_SEASON:
            lines.append(game.stage_name)

    if game.field:
        lines.append(game.field)
    if game.note:
        lines.append(f"Note: {game.note.strip()}")

    if standings:
        lines.append("")
        lines.extend(standings_table(standings, "Standings after this game:" if game.has_result else "Standings:"))
    elif not game.is_final and not game.is_placeholder and not game.is_cancelled:
        lines.append("")
        lines.append("Standings and records are added the week of the game.")
    return "\n".join(lines)


def ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line: str) -> list[str]:
    """Fold a content line at 75 octets without splitting a UTF-8 character."""
    encoded = line.encode("utf-8")
    if len(encoded) <= ICS_LINE_LIMIT:
        return [line]
    out: list[str] = []
    limit = ICS_LINE_LIMIT
    start = 0
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:  # inside a multibyte char
            end -= 1
        out.append(("" if start == 0 else " ") + encoded[start:end].decode("utf-8"))
        start = end
        limit = ICS_LINE_LIMIT - 1  # continuation lines start with a space
    return out


def ics_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_uid(game: Game) -> str:
    return f"bondsports-event-{game.event_id}@mai-tai-calendar"


def render_event(game: Game, summary: str, description: str, location: str, sequence: int, modified: datetime) -> list[str]:
    status = "CANCELLED" if game.is_cancelled else "CONFIRMED"
    props = [
        "BEGIN:VEVENT",
        f"UID:{event_uid(game)}",
        f"DTSTAMP:{ics_datetime(modified)}",
        f"LAST-MODIFIED:{ics_datetime(modified)}",
        f"SEQUENCE:{sequence}",
        f"DTSTART:{ics_datetime(game.start)}",
        f"DTEND:{ics_datetime(game.end)}",
        f"SUMMARY:{ics_escape(summary)}",
        f"DESCRIPTION:{ics_escape(description)}",
        f"LOCATION:{ics_escape(location)}",
        f"STATUS:{status}",
        "TRANSP:OPAQUE",
        "END:VEVENT",
    ]
    return [folded for prop in props for folded in ics_fold(prop)]


def render_calendar(name: str, tz: str, events: list[list[str]]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//mai-tai-calendar//Bond Sports team calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        f"X-WR-TIMEZONE:{tz}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    for event in events:
        lines.extend(event)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------- state


def content_hash(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def next_state(previous: Optional[dict], digest: str, now: datetime) -> dict:
    """Bump SEQUENCE and LAST-MODIFIED only when an event's rendered content changed."""
    if previous and previous.get("hash") == digest:
        return previous
    sequence = previous["sequence"] + 1 if previous else 0
    return {"hash": digest, "sequence": sequence, "last_modified": now.isoformat()}


# ----------------------------------------------------------------------------- main


def build(config: dict, seasons: list[Season], state: dict, now: datetime, location: str) -> tuple[str, dict]:
    team = config["team"]
    tz = ZoneInfo(config["timezone"])
    by_id = {season.id: season for season in seasons}
    ours = select_games([g for season in seasons for g in season.games], team, now)
    featured = pick_featured(ours, now)

    events = []
    new_state = {}
    for game in ours:
        season = by_id[game.season_id]
        is_featured = game is featured
        summary = build_summary(game, team, is_featured, season.standings)
        if is_featured:
            table = season.standings
        elif game.has_result:
            table = standings_after(game, season, tz)
        else:
            table = None
        description = build_description(game, team, table)
        uid = event_uid(game)
        digest = content_hash(summary, description, game.start, game.end, game.status, location)
        entry = next_state(state.get(uid), digest, now)
        new_state[uid] = entry
        modified = datetime.fromisoformat(entry["last_modified"])
        description += f"\n\nUpdated {modified.astimezone(tz).strftime('%b %-d, %Y %-I:%M %p %Z')}"
        events.append(render_event(game, summary, description, location, entry["sequence"], modified))

    return render_calendar(config["calendar_name"], config["timezone"], events), new_state


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.json", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print the calendar instead of writing files")
    args = parser.parse_args(argv)

    location = os.environ.get(LOCATION_ENV, "").strip()
    if not location:
        print(f"error: set {LOCATION_ENV} to the venue address, e.g. {LOCATION_ENV}='Venue, Street, City'", file=sys.stderr)
        return 2

    root = args.config.resolve().parent
    config = json.loads(args.config.read_text(encoding="utf-8"))
    tz = ZoneInfo(config["timezone"])
    now = datetime.now(timezone.utc).replace(microsecond=0)

    try:
        seasons = load_seasons(config, root / config["cache_dir"], now.astimezone(tz).date())
    except (urllib.error.URLError, ValueError, KeyError) as exc:
        print(f"error: failed to load seasons: {exc}", file=sys.stderr)
        return 1

    for season in seasons:
        if season.standings is not None:
            current = compute_standings(season.games, season.standings.division_id, tz, season.ruleset)
            for line in standings_mismatch(season.standings, current):
                print(f"warning: rebuilt standings for {season.name!r} differ from the API's ({line})", file=sys.stderr)

    state_path = root / config["state_file"]
    calendar, new_state = build(config, seasons, load_state(state_path), now, location)

    # An empty feed would delete every event from every subscriber's calendar, so treat it as an
    # error (most likely a wrong program id or season filter in config.json) rather than publishing it.
    if not new_state:
        print(f"error: found no games for {config['team']!r}; refusing to publish an empty calendar", file=sys.stderr)
        return 1

    if args.dry_run:
        sys.stdout.write(calendar)
        return 0

    output = root / config["output"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(calendar.encode("utf-8"))  # bytes, so CRLF line endings survive on every platform
    state_path.write_text(json.dumps(new_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output.relative_to(root)} with {len(new_state)} events from {len(seasons)} season(s): "
          + ", ".join(s.name for s in seasons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
