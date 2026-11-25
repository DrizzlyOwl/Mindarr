import pytest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from fastapi.testclient import TestClient

from plex_recommender.achievements import evaluate_achievements, streaks, summarize_achievements
from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.community import CommunityService, community_service
from plex_recommender.web.app import app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_community.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    yield


def _login(client: TestClient, is_admin: bool = True):
    with client:
        with client.websocket_connect("/ws-dummy") if False else patch("starlette.middleware.sessions.SessionMiddleware"):
            pass
    # Standard session login pattern used in test_web.py
    with patch("plex_recommender.web.app.get_current_user") as mock_user:
        mock_user.return_value = {
            "user_key": "user_me",
            "username": "ash",
            "title": "Ashley",
            "email": "ash@example.com",
            "is_admin": is_admin,
            "thumb": None,
        }


def test_compute_level_and_xp():
    service = CommunityService()

    # 0 plays, 0 seconds -> Level 1, 0 XP, 0%
    lvl1 = service._compute_level_and_xp(0, 0)
    assert lvl1["level"] == 1
    assert lvl1["xp"] == 0
    assert lvl1["progress_pct"] == 0

    # 10 plays, 2 hours (7200s = 120 mins) -> (10 * 25) + (120 * 0.5) = 250 + 60 = 310 XP
    lvl2 = service._compute_level_and_xp(10, 7200)
    assert lvl2["xp"] == 310
    assert lvl2["level"] >= 2
    assert 0 <= lvl2["progress_pct"] <= 100

    # High hours -> high level
    lvl_high = service._compute_level_and_xp(200, 360000)
    assert lvl_high["level"] > 10


def test_determine_watcher_titles():
    service = CommunityService()

    assert "Server Champion" in service._determine_watcher_title(1, 50, 40.0, 50, 50)
    assert "Binge Prodigy" in service._determine_watcher_title(2, 30, 20.0, 50, 50)
    assert "Silver Screen Ace" in service._determine_watcher_title(3, 25, 15.0, 50, 50)
    assert "Cinephile Extraordinaire" in service._determine_watcher_title(4, 20, 30.0, 70, 30)
    assert "Series Devotee" in service._determine_watcher_title(4, 20, 30.0, 20, 80)
    assert "Marathon Legend" in service._determine_watcher_title(4, 50, 110.0, 50, 50)


def test_evaluate_badges():
    badges = evaluate_achievements({
        "plays": 50, "watch_hours": 105, "movies": 20, "episode_plays": 30,
        "genres": 5, "trending_overlap": 50, "registered": 1, "champion": 1,
    })
    summary = summarize_achievements(badges)
    assert {b["id"] for b in summary["badges"]} == {
        "first_steps", "champion", "marathon", "century", "cinephile", "binge", "explorer", "trendsetter", "pioneer",
    }
    assert summary["trophy_score"] == 235
    assert summary["next_up"][0]["id"] == "trend_leader"


def test_streak_counts_distinct_local_dates_and_allows_yesterday():
    today = date(2026, 11, 25)
    days = [today - timedelta(days=n) for n in (1, 1, 2, 4, 5, 6, 7, -1)]
    assert streaks(days, today) == (2, 4)
    assert streaks(days[:-1], today + timedelta(days=1)) == (0, 4)
    assert streaks([], today) == (0, 0)


def test_achievement_progress_comes_from_synced_data_and_is_consistent_across_viewers(monkeypatch):
    from plex_recommender import sync
    from plex_recommender.db.engagement import add_to_watchlist, record_vote, dismiss_item
    from plex_recommender.db.users import create_or_update_user
    from plex_recommender.db.watch import record_watch_event, upsert_user_media

    service = CommunityService()
    service.clear_cache()
    monkeypatch.setattr("plex_recommender.community.tautulli.is_configured", lambda: False)
    alice = {"user_key": "trophy-alice", "username": "alice"}
    bob = {"user_key": "trophy-bob", "username": "bob"}
    for user in (alice, bob):
        create_or_update_user(user)

    a, b = alice["user_key"], bob["user_key"]
    for i, year in enumerate((1979, 1985, 1996, 2003, 2021)):
        item = {"item_id": f"shared{i}", "title": f"Film {i}", "media_type": "movie", "year": year,
                "genres": ["Action", "Drama", "Sci-Fi", "Adventure", " drama "],
                "directors": ["Director A", " director a "], "critic_rating": 8.5}
        upsert_user_media(a, item)
        upsert_user_media(b, {**item, "view_count": 30})
    upsert_user_media(b, {"item_id": "bob-only", "title": "Bob's Pick", "year": 2022, "media_type": "movie"})
    dismiss_item(a, "1000", title="Bob's Pick", year=2022, reason="not_interested")
    for i in range(25):
        record_vote(a, str(i))
    for i in range(5):
        add_to_watchlist(a, str(i))

    # Play history and app actions are different inputs from the XP leaderboard.
    for i in range(3):
        viewed_at = datetime.combine(date.today() - timedelta(days=i+1), datetime.min.time()).replace(hour=1).isoformat()
        event = {"user_key": a, "item_id": "shared0", "media_type": "movie", "viewed_at": viewed_at}
        record_watch_event({**event, "source": "plex"})
        record_watch_event({**event, "source": "tautulli", "duration_watched": 3600})
        record_watch_event({**event, "user_key": b, "source": "plex"})

    def awards(data):
        return {badge["id"]: badge for category in data["user_comparison"]["trophy_case"]["categories"]
                for badge in category["awards"]}

    before = service.get_community_data(alice)
    badges = awards(before)
    assert {key for key, badge in badges.items() if badge["unlocked"]} >= {
        "hat_trick", "auteur_seeker", "shared_wavelength", "active_critic", "watchlist_collector", "decade_hopper",
    }
    assert badges["night_owl"]["current"] == 3
    assert badges["explorer"]["current"] == 4
    assert badges["trend_leader"]["current"] == 83  # Dismissal isn't a watch.
    assert badges["marathon"]["current"] == 3  # Synced duration, no XP duration estimates.
    assert badges["binge"]["current"] == 0  # Rewatching movies is not episode progress.
    assert before["leaderboard"][0]["user_key"] == b
    assert before["leaderboard"][1]["gamification"]["trophy_score"] > before["leaderboard"][0]["gamification"]["trophy_score"]
    best = next(r for r in before["user_comparison"]["server_records"] if r["name"] == "Longest Streak")
    assert best["holders"] == ["bob", "alice"]
    as_bob = service.get_community_data(bob)
    assert before["leaderboard"][1]["gamification"] == as_bob["leaderboard"][1]["gamification"]

    def plex_sync(user_key, **kwargs):
        record_watch_event({"user_key": user_key, "item_id": "ep1", "media_type": "episode",
                            "viewed_at": datetime.now().isoformat()})
        return 1

    monkeypatch.setattr(sync, "sync_plex_user_history", plex_sync)
    monkeypatch.setattr(sync, "sync_tautulli_user_history", lambda *a, **kw: 0)
    sync.sync_user_history(a)
    assert awards(service.get_community_data(alice))["binge"]["current"] == 1
    assert awards(service.get_community_data(bob))["binge"]["current"] == 0
    service.clear_cache()


def test_unlinked_tautulli_uses_only_local_data_for_trophies(monkeypatch):
    service = CommunityService()
    service.clear_cache()
    monkeypatch.setattr("plex_recommender.community.tautulli.is_configured", lambda: True)
    monkeypatch.setattr("plex_recommender.community.tautulli.monitors_server", lambda _: False)
    with patch.object(service, "_build_from_tautulli") as live:
        data = service.get_community_data({"user_key": "unlinked", "username": "demo"})
    live.assert_not_called()
    assert data["source"] == "db"
    assert not data["user_comparison"]["server_records"]
    service.clear_cache()


def test_format_duration():
    service = CommunityService()
    assert service._format_duration(0) == "0m"
    assert service._format_duration(300) == "5m"
    assert service._format_duration(3660) == "1h 1m"
    assert service._format_duration(7200) == "2h"


def test_build_leaderboard_and_comparison_multiple_users():
    service = CommunityService()
    current_user = {
        "user_key": "user_2",
        "username": "bob",
        "title": "Bob",
        "email": "bob@example.com",
    }
    candidates = [
        {
            "user_key": "user_1",
            "username": "alice",
            "display_name": "Alice",
            "thumb": "https://example.com/alice.jpg",
            "is_me": False,
            "total_plays": 100,
            "total_duration_seconds": 200000,
            "movie_count": 40,
            "show_count": 60,
            "genres": ["Action", "Sci-Fi", "Drama"],
            "is_registered": True,
        },
        {
            "user_key": "user_2",
            "username": "bob",
            "display_name": "Bob",
            "thumb": "https://example.com/bob.jpg",
            "is_me": True,
            "total_plays": 60,
            "total_duration_seconds": 120000,
            "movie_count": 30,
            "show_count": 30,
            "genres": ["Comedy", "Animation"],
            "is_registered": True,
        },
    ]

    leaderboard, comparison, podium = service._build_leaderboard_and_comparison(
        current_user=current_user,
        candidate_users=candidates,
    )

    # Candidate 1 (Alice) is #1 and shows authentic identity & avatar
    assert len(leaderboard) == 2
    assert leaderboard[0]["rank"] == 1
    assert leaderboard[0]["is_me"] is False
    assert leaderboard[0]["is_anonymized"] is False
    assert leaderboard[0]["username"] == "alice"
    assert leaderboard[0]["display_name"] == "Alice"
    assert leaderboard[0]["thumb"] == "https://example.com/alice.jpg"

    # Candidate 2 (Bob) is #2 and retains authentic identity
    assert leaderboard[1]["rank"] == 2
    assert leaderboard[1]["is_me"] is True
    assert leaderboard[1]["is_anonymized"] is False
    assert leaderboard[1]["username"] == "bob"
    assert leaderboard[1]["display_name"] == "Bob"
    assert leaderboard[1]["thumb"] == "https://example.com/bob.jpg"

    # Comparison metrics
    assert comparison["user_rank"] == 2
    assert comparison["is_leader"] is False
    assert comparison["leader"]["display_name"] == "Alice"
    assert comparison["plays_behind"] == 40
    assert comparison["next_rank_gap"] == 41
    assert "Alice" in comparison["rank_message"]
    assert 0 < comparison["trophy_case"]["unlocked_count"] < comparison["trophy_case"]["total_count"]

    # Podium
    assert podium["first"]["display_name"] == "Alice"
    assert podium["second"]["display_name"] == "Bob"
    assert podium["third"] is None


def test_build_leaderboard_single_user():
    service = CommunityService()
    current_user = {
        "user_key": "user_solo",
        "username": "solo",
        "title": "Solo Viewer",
    }
    candidates = [
        {
            "user_key": "user_solo",
            "username": "solo",
            "display_name": "Solo Viewer",
            "is_me": True,
            "total_plays": 15,
            "total_duration_seconds": 36000,
            "movie_count": 5,
            "show_count": 10,
            "genres": ["Drama"],
            "is_registered": True,
        }
    ]

    leaderboard, comparison, podium = service._build_leaderboard_and_comparison(
        current_user=current_user,
        candidate_users=candidates,
    )

    assert len(leaderboard) == 1
    assert leaderboard[0]["rank"] == 1
    assert comparison["is_leader"] is True
    assert comparison["user_rank"] == 1
    assert comparison["plays_behind"] == 0
    assert "reign supreme" in comparison["rank_message"] or "Pioneer" in comparison["rank_message"]
    assert podium["first"]["username"] == "solo"
    assert podium["second"] is None


def test_build_from_db_creates_leaderboard():
    from plex_recommender.db.media import upsert_media_item
    from plex_recommender.db.users import create_or_update_user
    from plex_recommender.db.watch import upsert_user_media
    service = CommunityService()

    # Seed 2 users
    create_or_update_user({"user_key": "u1", "username": "alice", "title": "Alice"})
    create_or_update_user({"user_key": "u2", "username": "bob", "title": "Bob"})

    # Seed media item
    upsert_media_item({
        "item_id": "m1", "media_type": "movie", "title": "Great Film", "year": 2023, "genres": ["Action"]
    })
    upsert_user_media("u1", {"item_id": "m1", "view_count": 10})
    upsert_user_media("u2", {"item_id": "m1", "view_count": 3})

    current_user = {"user_key": "u2", "username": "bob", "title": "Bob"}
    local_metrics = {
        "u1": {"media_count": 1, "total_plays": 10, "movie_count": 1, "show_count": 0, "total_duration_seconds": 18000, "genres": ["Action"]},
        "u2": {"media_count": 1, "total_plays": 3, "movie_count": 1, "show_count": 0, "total_duration_seconds": 5400, "genres": ["Action"]},
    }

    data = service._build_from_db(
        current_user=current_user,
        seen_index={"imdb": set(), "tmdb": set(), "tvdb": set(), "title_year": set()},
        user_item_ids={"m1"},
        local_user_stats={"events_count": 3, "movies_count": 1, "shows_count": 0},
        media_meta={},
        local_user_metrics=local_metrics,
    )

    leaderboard = data["leaderboard"]
    comparison = data["user_comparison"]

    assert len(leaderboard) == 2
    assert leaderboard[0]["user_key"] == "u1"
    assert leaderboard[0]["rank"] == 1
    assert leaderboard[1]["user_key"] == "u2"
    assert leaderboard[1]["rank"] == 2
    assert leaderboard[1]["is_me"] is True

    assert comparison["user_rank"] == 2
    assert comparison["is_leader"] is False
    assert comparison["leader"]["user_key"] == "u1"


def test_build_from_tautulli_integrates_top_users():
    service = CommunityService()
    current_user = {"user_key": "u_me", "username": "ash", "title": "Ashley"}

    home_stats = [
        {
            "stat_id": "top_users",
            "rows": [
                {
                    "user": "streamking",
                    "friendly_name": "Stream King",
                    "total_plays": 120,
                    "total_duration": 360000,
                }
            ]
        }
    ]

    with patch("plex_recommender.discovery.tautulli.tautulli.get_home_stats", return_value=home_stats), \
         patch("plex_recommender.discovery.tautulli.tautulli.get_users", return_value=[]), \
         patch("plex_recommender.discovery.tautulli.tautulli.get_history", return_value=[]), \
         patch("plex_recommender.community.get_all_users", return_value=[{"user_key": "u_me", "username": "ash", "title": "Ashley"}]):

        data = service._build_from_tautulli(
            current_user=current_user,
            seen_index={"imdb": set(), "tmdb": set(), "tvdb": set(), "title_year": set()},
            user_item_ids=set(),
            local_user_stats={"events_count": 5, "movies_count": 2, "shows_count": 3},
            media_meta={},
            local_user_metrics={"u_me": {"total_plays": 5, "total_duration_seconds": 9000, "movie_count": 2, "show_count": 3, "genres": ["Drama"]}}
        )

        leaderboard = data["leaderboard"]
        comparison = data["user_comparison"]

        assert len(leaderboard) == 2
        # Stream King #1 shows authentic display name
        assert leaderboard[0]["rank"] == 1
        assert leaderboard[0]["is_me"] is False
        assert leaderboard[0]["is_anonymized"] is False
        assert leaderboard[0]["display_name"] == "Stream King"
        # Ashley #2 retains authentic display name
        assert leaderboard[1]["username"] == "ash"
        assert leaderboard[1]["display_name"] == "Ashley"
        assert leaderboard[1]["rank"] == 2
        assert leaderboard[1]["is_me"] is True
        assert leaderboard[1]["is_anonymized"] is False

        assert comparison["user_rank"] == 2
        assert comparison["leader"]["display_name"] == "Stream King"


def test_web_community_leaderboard_rendering():
    with patch("plex_recommender.web.app.get_current_user") as mock_user:
        mock_user.return_value = {
            "user_key": "user_me",
            "username": "ash",
            "title": "Ashley",
            "email": "ash@example.com",
            "is_admin": True,
            "thumb": None,
            "onboarded_at": "2024-01-01T00:00:00",
        }

        trophy_case = CommunityService()._build_leaderboard_and_comparison(mock_user.return_value, [])[1]["trophy_case"]
        mock_community_data = {
            "is_single_user": False,
            "total_community_members": 2,
            "source": "db",
            "benchmarks": {
                "user_plays": 45,
                "user_movie_pct": 60,
                "user_tv_pct": 40,
                "community_movie_pct": 50,
                "community_tv_pct": 50,
                "overlap_count": 5,
                "overlap_pct": 50,
            },
            "popular_movies": [],
            "popular_tv": [],
            "unseen_recommendations": [],
            "recent_activity": [],
            "leaderboard": [
                {
                    "rank": 1,
                    "user_key": "user_top",
                    "username": "topstreamer",
                    "display_name": "Top Streamer",
                    "thumb": None,
                    "is_me": False,
                    "total_plays": 80,
                    "total_hours": 65.0,
                    "duration_formatted": "65h",
                    "movie_pct": 50,
                    "tv_pct": 50,
                    "top_genre": "Action",
                    "gamification": {
                        "level": 8,
                        "xp": 3500,
                        "progress_pct": 70,
                        "title": "Server Champion 👑",
                        "badges": [
                            {"id": "champion", "name": "Server Champion", "icon": "👑", "desc": "Rank 1"}
                        ],
                    },
                },
                {
                    "rank": 2,
                    "user_key": "user_me",
                    "username": "ash",
                    "display_name": "Ashley",
                    "thumb": None,
                    "is_me": True,
                    "total_plays": 45,
                    "total_hours": 30.0,
                    "duration_formatted": "30h",
                    "movie_pct": 60,
                    "tv_pct": 40,
                    "top_genre": "Sci-Fi",
                    "gamification": {
                        "level": 5,
                        "xp": 1800,
                        "progress_pct": 40,
                        "title": "Binge Prodigy 🥈",
                        "badges": [
                            {"id": "pioneer", "name": "Server Pioneer", "icon": "🌟", "desc": "Pioneer"}
                        ],
                    },
                },
            ],
            "user_comparison": {
                "user_rank": 2,
                "total_watchers": 2,
                "percentile": 50,
                "is_leader": False,
                "leader": {
                    "username": "topstreamer",
                    "display_name": "Top Streamer",
                    "total_plays": 80,
                    "total_hours": 65.0,
                },
                "current_user": {
                    "username": "ash",
                    "display_name": "Ashley",
                    "total_plays": 45,
                    "total_hours": 30.0,
                    "gamification": {
                        "level": 5,
                        "xp": 1800,
                        "progress_pct": 40,
                        "title": "Binge Prodigy 🥈",
                    },
                },
                "plays_behind": 35,
                "hours_behind": 35.0,
                "plays_pct_of_leader": 56,
                "hours_pct_of_leader": 46,
                "next_rank_gap": 36,
                "next_rank_user": "Top Streamer",
                "rank_message": "Hot on their heels! ⚔️ Only 36 more plays to take #1 from Top Streamer!",
                "trophy_case": trophy_case,
                "server_records": [{"name": "Longest Streak", "icon": "🔥", "holders": ["Ashley"], "value": 3, "unit": "days"}],
            },
            "podium": {
                "first": {
                    "username": "topstreamer",
                    "display_name": "Top Streamer",
                    "is_me": False,
                    "total_plays": 80,
                    "total_hours": 65.0,
                    "gamification": {"level": 8, "xp": 3500, "title": "Server Champion 👑"},
                },
                "second": {
                    "username": "ash",
                    "display_name": "Ashley",
                    "is_me": True,
                    "total_plays": 45,
                    "total_hours": 30.0,
                    "gamification": {"level": 5, "xp": 1800, "title": "Binge Prodigy 🥈"},
                },
                "third": None,
            },
        }

        with patch.object(community_service, "get_community_data", return_value=mock_community_data):
            client = TestClient(app)
            resp = client.get("/community/")
            assert resp.status_code == 200
            text = resp.text

            # Check leaderboard elements
            assert "Server Leaderboard" in text
            assert "Top Watchers" in text
            assert "Top Streamer" in text
            assert "Ashley" in text
            assert "Server Champion 👑" in text
            assert "Binge Prodigy 🥈" in text
            assert "Your Trophy Case" in text
            assert "Server Pioneer" in text
            assert "Trophy Score" in text
            assert "Current Streak" in text
            assert "Server Records" in text
            assert 'data-trophy-filter="curator"' in text
            assert "Hot on their heels!" in text
            assert "You vs Server Leader" in text
            assert "Plays" in text
            assert "Watch Time" in text
            assert "Why are other usernames anonymized?" not in text
            assert "Faker Pseudonyms Active" not in text


def test_community_cache_ttl_is_per_user(monkeypatch):
    import plex_recommender.community as comm_mod

    comm_mod._CACHE.clear()
    service = CommunityService()
    calls = []

    def fake_build_db(self, current_user, *args, **kwargs):
        calls.append(current_user["user_key"])
        return {"user_key": current_user["user_key"], "call": len(calls)}

    monkeypatch.setattr(CommunityService, "_build_from_db", fake_build_db)
    monkeypatch.setattr(comm_mod.tautulli, "is_configured", lambda: False)

    t = [1000.0]
    monkeypatch.setattr(comm_mod.time, "time", lambda: t[0])

    u1 = {"user_key": "u1", "username": "alice"}
    u2 = {"user_key": "u2", "username": "bob"}

    # u1 populates cache at t=1000
    assert service.get_community_data(u1)["call"] == 1
    # u2 populates cache at t=1500 (should NOT refresh u1's TTL)
    t[0] = 1500.0
    assert service.get_community_data(u2)["call"] == 2
    # At t=1700, u1 is expired (700s > 600s) while u2 is still fresh (200s < 600s)
    t[0] = 1700.0
    assert service.get_community_data(u1)["call"] == 3
    assert service.get_community_data(u2)["call"] == 2
    comm_mod._CACHE.clear()


def test_community_shows_real_names_and_activity_for_all_users():
    from plex_recommender.db.media import upsert_media_item
    from plex_recommender.db.users import create_or_update_user, mark_onboarded
    from plex_recommender.db.watch import upsert_user_media, record_watch_event
    import base64, json, itsdangerous

    community_service.clear_cache()
    create_or_update_user({
        "user_key": "adm_1", "username": "admin_owner", "title": "Admin Owner",
        "email": "adm@example.com", "is_admin": True,
    })
    mark_onboarded("adm_1")
    create_or_update_user({
        "user_key": "peer_2", "username": "plex_peer", "title": "Real Peer Name",
        "email": "peer@example.com", "thumb": "https://img.example/peer.jpg", "is_admin": False,
    })
    mark_onboarded("peer_2")

    upsert_media_item({"item_id": "m_lb", "media_type": "movie", "title": "Hit Film", "year": 2024, "genres": ["Action"]})
    upsert_user_media("peer_2", {"item_id": "m_lb", "view_count": 25})
    upsert_user_media("adm_1", {"item_id": "m_lb", "view_count": 5})
    record_watch_event({
        "user_key": "adm_1", "item_id": "m_lb", "title": "Hit Film",
        "media_type": "movie", "viewed_at": "2026-01-10T20:00:00", "source": "plex",
    })

    def _cookie(uk):
        signer = itsdangerous.TimestampSigner(settings.session_secret)
        return signer.sign(base64.b64encode(json.dumps({"user_key": uk}).encode())).decode()

    with patch("plex_recommender.discovery.tautulli.tautulli.is_configured", return_value=False):
        with TestClient(app) as client:
            client.cookies.set("session", _cookie("peer_2"))
            resp = client.get("/community/")
            assert resp.status_code == 200
            assert "Admin Owner" in resp.text
            assert "Watched by Admin Owner" in resp.text
            assert "Why are other usernames anonymized?" not in resp.text
            assert "Reveal Plex Users" not in resp.text

            client.cookies.set("session", _cookie("adm_1"))
            resp_adm = client.get("/community/")
            assert resp_adm.status_code == 200
            assert "Real Peer Name" in resp_adm.text
            assert "https://img.example/peer.jpg" in resp_adm.text

    community_service.clear_cache()
