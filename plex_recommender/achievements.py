"""Community trophy rules, evaluated from a snapshot rather than stored unlocks."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Optional

from plex_recommender.db._util import normalize_title, parse_iso_to_epoch


RARITY_POINTS = {"bronze": 15, "silver": 35, "gold": 75, "legendary": 150}
CATEGORIES = (
    ("milestones", "Milestones"),
    ("exploration", "Exploration"),
    ("habits", "Habits & Streaks"),
    ("community", "Community"),
    ("curator", "Curator"),
)


@dataclass(frozen=True)
class Achievement:
    id: str
    name: str
    icon: str
    category: str
    rarity: str
    metric: str
    target: int
    description: str
    unit: str = ""
    live: bool = False


ACHIEVEMENT_CATALOG = (
    Achievement("first_steps", "First Steps", "🎟️", "milestones", "bronze", "plays", 1, "Log your first movie or TV play."),
    Achievement("marathon", "Marathoner", "🍿", "milestones", "bronze", "watch_hours", 25, "Log 25 hours of playback.", "h"),
    Achievement("century", "Century Club", "⏱️", "milestones", "silver", "watch_hours", 100, "Log 100 hours of playback.", "h"),
    Achievement("time_lord", "Time Lord", "⌛", "milestones", "legendary", "watch_hours", 500, "Log 500 hours of playback.", "h"),
    Achievement("cinephile", "Film Buff", "🎬", "milestones", "bronze", "movies", 15, "Watch 15 different movies."),
    Achievement("silver_screen", "Silver Screen Legend", "🎞️", "milestones", "gold", "movies", 100, "Watch 100 different movies."),
    Achievement("binge", "Binge Titan", "📺", "milestones", "bronze", "episode_plays", 25, "Log 25 TV episode plays."),
    Achievement("series_regular", "Series Regular", "📡", "milestones", "gold", "episode_plays", 150, "Log 150 TV episode plays."),
    Achievement("comfort_rewatcher", "Comfort Rewatcher", "🔁", "milestones", "silver", "rewatched_titles", 5, "Replay 5 different movies or episodes."),
    Achievement("superfan", "Superfan", "💫", "milestones", "silver", "most_played", 5, "Play the same movie or episode 5 times."),
    Achievement("explorer", "Genre Explorer", "🧭", "exploration", "bronze", "genres", 4, "Explore 4 different genres."),
    Achievement("genre_polymath", "Genre Polymath", "🌈", "exploration", "gold", "genres", 10, "Explore 10 different genres."),
    Achievement("decade_hopper", "Decade Hopper", "🕰️", "exploration", "silver", "decades", 4, "Watch titles from 4 release decades."),
    Achievement("retro_cinephile", "Retro Cinephile", "📼", "exploration", "silver", "classic_movies", 5, "Watch 5 different movies released before 1990."),
    Achievement("auteur_seeker", "Auteur Seeker", "🎥", "exploration", "silver", "most_by_director", 3, "Watch 3 different movies by the same director."),
    Achievement("prestige_watcher", "Prestige Watcher", "⭐", "exploration", "gold", "high_rated", 10, "Watch 10 titles rated at least 8/10 by critics or audiences."),
    Achievement("hat_trick", "Hat Trick", "🔥", "habits", "bronze", "best_streak", 3, "Watch on 3 consecutive days.", " days"),
    Achievement("weekly_ritual", "Weekly Ritual", "📆", "habits", "silver", "best_streak", 7, "Watch on 7 consecutive days.", " days"),
    Achievement("unstoppable", "Unstoppable", "☄️", "habits", "legendary", "best_streak", 30, "Watch on 30 consecutive days.", " days"),
    Achievement("double_feature", "Double Feature", "🎦", "habits", "bronze", "most_movies_in_day", 2, "Watch 2 different movies on the same day."),
    Achievement("binge_day", "Binge Day", "🛋️", "habits", "silver", "most_plays_in_day", 5, "Log 5 plays on the same day."),
    Achievement("weekend_warrior", "Weekend Warrior", "🗓️", "habits", "silver", "weekend_plays", 15, "Log 15 plays on Saturdays or Sundays."),
    Achievement("night_owl", "Night Owl", "🦉", "habits", "silver", "late_plays", 5, "Log 5 plays between midnight and 4 a.m. server time."),
    Achievement("pioneer", "Server Pioneer", "🌟", "community", "bronze", "registered", 1, "Become a Mindarr community member."),
    Achievement("podium", "Podium Finisher", "🏅", "community", "silver", "podium", 1, "Place in the top 3 with at least 3 active watchers.", live=True),
    Achievement("champion", "Server Champion", "👑", "community", "gold", "champion", 1, "Lead the Watcher XP leaderboard with at least one play.", live=True),
    Achievement("trendsetter", "Taste Maker", "🎯", "community", "silver", "trending_overlap", 40, "Watch at least 40% of the current trending titles.", "%", live=True),
    Achievement("trend_leader", "Trendsetter", "🚀", "community", "gold", "trending_overlap", 75, "Watch at least 75% of the current trending titles.", "%", live=True),
    Achievement("indie_maverick", "Indie Maverick", "🎸", "community", "silver", "indie_titles", 20, "Watch 20 titles while having less than 20% overlap with current trends.", live=True),
    Achievement("shared_wavelength", "Shared Wavelength", "🤝", "community", "silver", "shared_titles", 5, "Watch 5 titles also seen by another Mindarr member.", live=True),
    Achievement("active_critic", "Active Critic", "👍", "curator", "bronze", "votes", 5, "Vote on 5 different recommendations.", live=True),
    Achievement("master_curator", "Master Curator", "🎨", "curator", "gold", "votes", 25, "Vote on 25 different recommendations.", live=True),
    Achievement("watchlist_collector", "Watchlist Collector", "📚", "curator", "bronze", "watchlist", 5, "Keep 5 titles on your Mindarr watchlist.", live=True),
)


def evaluate_achievements(metrics: dict) -> list:
    """Return display-ready states. A missing metric is unavailable, not zero."""
    results = []
    for award in ACHIEVEMENT_CATALOG:
        value = metrics.get(award.metric)
        unlocked = value is not None and value >= award.target
        results.append({
            "id": award.id, "name": award.name, "icon": award.icon,
            "category": award.category, "rarity": award.rarity,
            "points": RARITY_POINTS[award.rarity], "desc": award.description,
            "unlocked": unlocked, "available": value is not None,
            "current": int(value) if value is not None else None,
            "target": award.target, "unit": award.unit, "live": award.live,
            "progress_pct": min(100, int(value / award.target * 100)) if value is not None else 0,
        })
    return results


def summarize_achievements(awards: list) -> dict:
    unlocked = sorted((a for a in awards if a["unlocked"]), key=lambda a: -a["points"])
    available = [a for a in awards if a["available"]]
    next_up = sorted(
        (a for a in available if not a["unlocked"] and a["current"] > 0),
        key=lambda a: -a["progress_pct"],
    )[:3]
    return {
        "badges": unlocked,
        "trophy_score": sum(a["points"] for a in unlocked),
        "max_trophy_score": sum(a["points"] for a in awards),
        "completion_pct": int(len(unlocked) / len(awards) * 100),
        "next_up": next_up,
    }


def streaks(days, today) -> tuple:
    """Count distinct consecutive dates; a live streak can end today or yesterday."""
    current = best = run = 0
    previous = None
    for day in sorted({d for d in days if d <= today}):
        run = run + 1 if previous is not None and (day - previous).days == 1 else 1
        best = max(best, run)
        previous = day
    if previous is not None and (today - previous).days <= 1:
        current = run
    return current, best


def _tags(value) -> set:
    return {tag.strip().casefold() for tag in value if isinstance(tag, str) and tag.strip()} if isinstance(value, list) else set()


def _number(value) -> float:
    try:
        number = float(value)
        return number if isfinite(number) else 0
    except (TypeError, ValueError):
        return 0


def _title_key(item: dict) -> Optional[tuple]:
    title = normalize_title(item.get("title") or "")
    if title:
        return item.get("media_type"), title, str(item.get("year") or "")
    return None


def collect_metrics(conn, media: dict, now: Optional[datetime] = None) -> dict:
    """Read registered users' synced playback, title metadata, and current app actions in bulk."""
    now = now or datetime.now().astimezone()
    today = now.astimezone().date()
    keys = [r["user_key"] for r in conn.execute("SELECT user_key FROM users")]
    metrics = {key: Counter() for key in keys}
    plays = defaultdict(Counter)
    days = defaultdict(Counter)
    daily_movies = defaultdict(lambda: defaultdict(set))

    for row in conn.execute("""
        SELECT w.user_key, w.item_id, w.media_type, w.viewed_at, w.duration_watched, w.source
        FROM watch_events w JOIN users u ON u.user_key = w.user_key
        WHERE w.media_type IN ('movie', 'show', 'episode')
    """):
        key, kind, item_id = row["user_key"], row["media_type"], row["item_id"]
        epoch = parse_iso_to_epoch(row["viewed_at"])
        if epoch is not None and epoch > now.timestamp():
            continue
        m = metrics[key]
        m["plays"] += 1
        m["episode_plays"] += kind == "episode"
        duration = max(0, _number(row["duration_watched"]))
        # Tautulli logs seconds; other historical rows use the existing mixed-unit convention.
        m["watch_hours"] += (duration / 1000 if duration > 10000 and row["source"] != "tautulli" else duration)
        if item_id and kind in ("movie", "episode"):
            plays[key][kind, item_id] += 1
        if epoch is not None:
            local = datetime.fromtimestamp(epoch)
            days[key][local.date()] += 1
            m["weekend_plays"] += local.weekday() >= 5
            m["late_plays"] += local.hour < 4
            if kind == "movie" and item_id:
                daily_movies[key][local.date()].add(item_id)

    item_viewers = Counter()
    titles = defaultdict(set)
    title_keys = defaultdict(set)
    genres = defaultdict(set)
    decades = defaultdict(set)
    directors = defaultdict(Counter)
    for row in conn.execute("""
        SELECT um.user_key, um.item_id FROM user_media um JOIN users u ON u.user_key = um.user_key
        WHERE um.view_count > 0
    """):
        key, item_id = row["user_key"], row["item_id"]
        item = media.get(item_id, {})
        # Use the rolled-up movie/series, not individual episode metadata, for breadth and overlap.
        kind = item.get("media_type")
        if kind not in ("movie", "show"):
            continue
        m = metrics[key]
        titles[key].add(item_id)
        name_key = _title_key(item)
        if name_key:
            title_keys[key].add(name_key)
        item_viewers[item_id] += 1
        genres[key].update(_tags(item.get("genres")))
        year = int(_number(item.get("year")))
        if year > 0:
            decades[key].add(year // 10)
        if kind == "movie":
            m["movies"] += 1
            m["classic_movies"] += 0 < year < 1990
            directors[key].update(_tags(item.get("directors")))
        m["high_rated"] += max(_number(item.get("critic_rating")), _number(item.get("audience_rating"))) >= 8

    for row in conn.execute("""
        SELECT user_key, COUNT(*) AS count, 'votes' AS metric FROM user_votes GROUP BY user_key
        UNION ALL
        SELECT user_key, COUNT(*) AS count, 'watchlist' AS metric FROM user_watchlist GROUP BY user_key
    """):
        if row["user_key"] in metrics:
            metrics[row["user_key"]][row["metric"]] = row["count"]

    for key, m in metrics.items():
        # Initialize absent progress counters to zero for registered users.
        for award in ACHIEVEMENT_CATALOG:
            if award.metric not in ("trending_overlap", "indie_titles"):
                m.setdefault(award.metric, 0)
        m["watch_hours"] /= 3600
        m["genres"] = len(genres[key])
        m["decades"] = len(decades[key])
        m["most_by_director"] = max(directors[key].values(), default=0)
        m["rewatched_titles"] = sum(count >= 2 for count in plays[key].values())
        m["most_played"] = max(plays[key].values(), default=0)
        m["most_movies_in_day"] = max((len(items) for items in daily_movies[key].values()), default=0)
        m["most_plays_in_day"] = max(days[key].values(), default=0)
        m["current_streak"], m["best_streak"] = streaks(days[key], today)
        m["shared_titles"] = sum(item_viewers[item_id] > 1 for item_id in titles[key])
        m["title_ids"] = titles[key]
        m["title_keys"] = title_keys[key]
    return metrics


def add_trending_metrics(metrics: dict, popular: list) -> None:
    """Match watched titles (not the recommendation exclusion index) for every member."""
    trending = {}
    for item in popular:
        name_key = _title_key(item)
        item_id = item.get("rating_key")
        if name_key or item_id:
            trending.setdefault(name_key or item_id, set())
            if item_id:
                trending[name_key or item_id].add(item_id)
    if not trending:
        return
    for m in metrics.values():
        matched = sum(key in m["title_keys"] or bool(ids & m["title_ids"]) for key, ids in trending.items())
        pct = 100 * matched / len(trending)
        m["trending_overlap"] = pct
        m["indie_titles"] = len(m["title_ids"]) if pct < 20 else 0
