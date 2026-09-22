import math
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple
from collections import defaultdict
from plex_recommender.db.watch import get_user_media_items, get_watch_events, get_stats, get_watch_source_breakdown, get_top_watched

def parse_date(date_val) -> datetime:
    """Safely parse date string or datetime, always returning a naive datetime."""
    if not date_val:
        return datetime.min
    if isinstance(date_val, datetime):
        dt = date_val
    else:
        try:
            # Try ISO format
            dt = datetime.fromisoformat(str(date_val).replace("Z", "+00:00"))
        except Exception:
            try:
                dt = datetime.strptime(str(date_val)[:10], "%Y-%m-%d")
            except Exception:
                return datetime.min
    # Normalise to naive (drop tzinfo) so comparisons with datetime.now() are safe
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt

def calculate_time_weight(viewed_at: datetime, reference_date: datetime) -> float:
    """
    Calculate exponential decay weight based on recency.
    Watched today: ~2.0
    Watched 6 months ago: ~1.2
    Watched 1 year ago: ~1.0
    Watched 3+ years ago: ~0.7
    """
    if viewed_at == datetime.min:
        return 1.0
    
    # Handle timezone differences
    if viewed_at.tzinfo is not None:
        viewed_at = viewed_at.replace(tzinfo=None)
    if reference_date.tzinfo is not None:
        reference_date = reference_date.replace(tzinfo=None)

    days_ago = max(0, (reference_date - viewed_at).days)
    # Half-life of 365 days, bounded between 0.6 and 2.0
    decay = math.exp(-days_ago / 365.0)
    return 0.6 + (1.4 * decay)

class TasteAnalyzer:
    def __init__(self):
        self.now = datetime.now()

    def analyze(self, user_key: str) -> Dict[str, Any]:
        """Run full taste and trend analysis over a user's watched items."""
        items = get_user_media_items(user_key)
        stats = get_stats(user_key)

        if not items:
            return {
                "has_data": False,
                "message": "No watch history available yet. Please run sync first."
            }

        genre_scores_all = defaultdict(float)
        genre_scores_recent = defaultdict(float)
        genre_counts = defaultdict(int)

        keyword_scores = defaultdict(float)
        keyword_counts = defaultdict(int)
        keyword_ids = {}

        director_scores = defaultdict(float)
        actor_scores = defaultdict(float)
        decade_counts = defaultdict(int)

        movies_count = 0
        shows_count = 0
        six_months_ago = self.now - timedelta(days=180)

        for item in items:
            mtype = item.get("media_type")
            if mtype == "movie":
                movies_count += 1
            elif mtype in ("show", "episode"):
                shows_count += 1

            # Determine item's effective view date and count
            view_count = max(1, item.get("view_count") or 1)
            last_viewed = parse_date(item.get("last_viewed_at"))
            is_recent = last_viewed >= six_months_ago

            # Multipliers
            rewatch_mult = 1.0 + 0.3 * (min(view_count, 5) - 1)
            rating_mult = 1.0
            rating = item.get("user_rating") or item.get("audience_rating") or item.get("critic_rating")
            # Graded upward boost from ratings (per-user rating is authoritative).
            # High ratings amplify affinity; low ratings simply don't boost (never penalize).
            if rating:
                if rating >= 9.0:
                    rating_mult = 1.5
                elif rating >= 8.0:
                    rating_mult = 1.25
                elif rating >= 7.0:
                    rating_mult = 1.1

            item_weight = rewatch_mult * rating_mult
            decayed_weight = item_weight * calculate_time_weight(last_viewed, self.now)

            # 1. Genres
            for g in item.get("genres", []):
                clean_g = g.strip().title()
                if not clean_g:
                    continue
                genre_counts[clean_g] += 1
                genre_scores_all[clean_g] += decayed_weight
                if is_recent:
                    genre_scores_recent[clean_g] += item_weight

            # 1b. Keywords (finer-grained taste signal than genres)
            for kw in item.get("keywords", []):
                name = (kw.get("name") if isinstance(kw, dict) else str(kw) or "").strip()
                if not name:
                    continue
                keyword_scores[name] += decayed_weight
                keyword_counts[name] += 1
                if isinstance(kw, dict) and kw.get("id") is not None:
                    keyword_ids[name] = kw["id"]

            # 2. Directors
            for d in item.get("directors", []):
                clean_d = d.strip()
                if clean_d:
                    director_scores[clean_d] += decayed_weight

            # 3. Actors
            for a in item.get("actors", []):
                clean_a = a.strip()
                if clean_a:
                    actor_scores[clean_a] += (decayed_weight * 0.5)

            # 4. Decades
            year = item.get("year")
            if year and 1920 <= year <= self.now.year + 1:
                decade = (year // 10) * 10
                decade_label = f"{decade}s"
                decade_counts[decade_label] += 1

        # Normalize genre scores (0 - 100)
        max_genre_score = max(genre_scores_all.values()) if genre_scores_all else 1.0
        normalized_genres = [
            {
                "genre": g,
                "score": round((score / max_genre_score) * 100, 1),
                "count": genre_counts[g],
                "recent_trending": g in sorted(genre_scores_recent, key=genre_scores_recent.get, reverse=True)[:5]
            }
            for g, score in sorted(genre_scores_all.items(), key=lambda x: x[1], reverse=True)
        ]

        # Top Directors
        top_directors = [
            {"name": d, "score": round(score, 1)}
            for d, score in sorted(director_scores.items(), key=lambda x: x[1], reverse=True)[:10]
        ]

        # Top Actors
        top_actors = [
            {"name": a, "score": round(score, 1)}
            for a, score in sorted(actor_scores.items(), key=lambda x: x[1], reverse=True)[:10]
        ]

        # Decades sorted chronologically
        sorted_decades = [
            {"decade": dec, "count": decade_counts[dec]}
            for dec in sorted(decade_counts.keys())
        ]

        # Top 5 primary genres for queries
        top_5_genres = [g["genre"] for g in normalized_genres[:5]]

        # Top keywords (finer taste dimension); include TMDb id when known.
        max_kw_score = max(keyword_scores.values()) if keyword_scores else 1.0
        top_keywords = [
            {
                "keyword": name,
                "id": keyword_ids.get(name),
                "score": round((score / max_kw_score) * 100, 1),
                "count": keyword_counts[name],
            }
            for name, score in sorted(keyword_scores.items(), key=lambda x: x[1], reverse=True)[:20]
        ]

        summary = self._build_summary(
            normalized_genres=normalized_genres,
            top_keywords=top_keywords,
            sorted_decades=sorted_decades,
            movies_count=movies_count,
            shows_count=shows_count,
            total_items=len(items),
            source_breakdown=get_watch_source_breakdown(user_key),
        )

        top_movies = get_top_watched(user_key, "movie", limit=10)
        top_shows = get_top_watched(user_key, "show", limit=10)

        return {
            "has_data": True,
            "stats": stats,
            "total_items_analyzed": len(items),
            "format_breakdown": {
                "movies": movies_count,
                "shows": shows_count
            },
            "top_genres": normalized_genres[:15],
            "query_genres": top_5_genres,
            "top_keywords": top_keywords,
            "top_movies": top_movies,
            "top_shows": top_shows,
            "decades": sorted_decades,
            "summary": summary,
            "generated_at": self.now.isoformat()
        }

    def _build_summary(
        self,
        normalized_genres,
        sorted_decades,
        movies_count,
        shows_count,
        total_items,
        source_breakdown=None,
        top_keywords=None,
    ) -> Dict[str, Any]:
        """Build a narrative taste-profile summary backed by cited metrics."""
        total_format = movies_count + shows_count

        # Format leaning
        if total_format:
            movie_pct = round(movies_count / total_format * 100)
            show_pct = 100 - movie_pct
        else:
            movie_pct = show_pct = 0
        if movie_pct >= 65:
            format_label = "a dedicated cinephile"
        elif show_pct >= 65:
            format_label = "a serial binger"
        else:
            format_label = "a balanced viewer"

        # Genre identity
        top_genre = normalized_genres[0] if normalized_genres else None
        secondary = normalized_genres[1] if len(normalized_genres) > 1 else None
        trending = [g["genre"] for g in normalized_genres if g.get("recent_trending")][:3]

        # Era leaning: pick the decade with the highest watched count
        favored_decade = None
        if sorted_decades:
            favored_decade = max(sorted_decades, key=lambda d: d["count"])

        # Build a set of narrative highlight sentences, each with citations.
        highlights = []

        if top_genre:
            if secondary:
                highlights.append({
                    "text": f"Your taste centers on {top_genre['genre']} and {secondary['genre']}.",
                    "metric": f"{top_genre['genre']} leads with a {top_genre['score']}% affinity across {top_genre['count']} titles.",
                })
            else:
                highlights.append({
                    "text": f"Your taste centers on {top_genre['genre']}.",
                    "metric": f"{top_genre['score']}% affinity across {top_genre['count']} titles.",
                })

        highlights.append({
            "text": f"You're {format_label}.",
            "metric": f"{movie_pct}% movies vs {show_pct}% TV across {total_items} analyzed titles.",
        })

        # Signature themes (TMDb keywords) — a finer-grained taste signal.
        kws = [k for k in (top_keywords or []) if k.get("count", 0) >= 2][:3]
        if kws:
            names = ", ".join(k["keyword"] for k in kws)
            lead = kws[0]
            highlights.append({
                "text": f"You're drawn to stories about {names}.",
                "metric": f"'{lead['keyword']}' recurs across {lead['count']} watched titles ({lead['score']}% affinity).",
            })

        if favored_decade:
            highlights.append({
                "text": f"You gravitate toward {favored_decade['decade']} releases.",
                "metric": f"{favored_decade['count']} watched titles from the {favored_decade['decade']}.",
            })

        if trending:
            highlights.append({
                "text": f"Recently trending for you: {', '.join(trending)}.",
                "metric": "Based on the last 6 months of watch activity.",
            })

        # One-line headline
        headline_parts = []
        if top_genre:
            headline_parts.append(top_genre["genre"])
        if secondary:
            headline_parts.append(secondary["genre"])
        genre_str = " & ".join(headline_parts) if headline_parts else "Eclectic"
        era_str = f", {favored_decade['decade']}-leaning" if favored_decade else ""
        headline = f"{genre_str} {format_label.split(' ', 1)[-1].title()}{era_str}"

        # Data provenance: how much of the raw watch history came from each source.
        sb = source_breakdown or {"plex": 0, "tautulli": 0, "total": 0}
        src_total = sb.get("total", 0) or 0
        plex_events = sb.get("plex", 0) or 0
        tautulli_events = sb.get("tautulli", 0) or 0
        sources = {
            "plex_events": plex_events,
            "tautulli_events": tautulli_events,
            "total_events": src_total,
            "plex_pct": round(plex_events / src_total * 100) if src_total else 0,
            "tautulli_pct": round(tautulli_events / src_total * 100) if src_total else 0,
        }

        return {
            "headline": headline,
            "highlights": highlights,
            "movie_pct": movie_pct,
            "show_pct": show_pct,
            "sources": sources,
        }


analyzer = TasteAnalyzer()
