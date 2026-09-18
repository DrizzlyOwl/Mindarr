import pytest
from unittest.mock import patch
from starlette.testclient import TestClient

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
    service = CommunityService()

    # Rank 1 user with 105 hours, 20 movies, 30 shows, 5 genres
    badges = service._evaluate_badges(
        rank=1,
        plays=50,
        total_hours=105.0,
        movie_count=20,
        show_count=30,
        genre_count=5,
        overlap_pct=50,
        is_registered=True,
    )
    badge_ids = {b["id"] for b in badges}
    assert "champion" in badge_ids
    assert "century" in badge_ids
    assert "cinephile" in badge_ids
    assert "binge" in badge_ids
    assert "explorer" in badge_ids
    assert "trendsetter" in badge_ids
    assert "pioneer" in badge_ids


def test_format_duration():
    service = CommunityService()
    assert service._format_duration(0) == "0m"
    assert service._format_duration(300) == "5m"
    assert service._format_duration(3660) == "1h 1m"
    assert service._format_duration(7200) == "2h"


def test_deterministic_faker_anonymization():
    service = CommunityService()

    # Same seed must yield identical pseudonym
    name1 = service._generate_anonymized_name("user_test_abc")
    name2 = service._generate_anonymized_name("user_test_abc")
    assert name1 == name2
    assert len(name1.split()) == 2
    assert name1.endswith(".")

    # Different seeds should yield different pseudonyms
    name3 = service._generate_anonymized_name("user_test_xyz")
    assert name1 != name3


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
        overlap_pct=60,
    )

    # Candidate 1 (Alice) is #1 but anonymized with Faker
    assert len(leaderboard) == 2
    assert leaderboard[0]["rank"] == 1
    assert leaderboard[0]["is_me"] is False
    assert leaderboard[0]["is_anonymized"] is True
    # Alice's real username/display_name and thumb must be masked
    assert leaderboard[0]["username"] != "alice"
    assert leaderboard[0]["display_name"] != "Alice"
    assert leaderboard[0]["thumb"] is None

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
    assert comparison["leader"]["display_name"] == leaderboard[0]["display_name"]
    assert comparison["plays_behind"] == 40
    assert comparison["next_rank_gap"] == 41
    # Rank message mentions the leader's pseudonym, NOT their real name
    assert "Alice" not in comparison["rank_message"]
    assert leaderboard[0]["display_name"] in comparison["rank_message"]
    assert len(comparison["unlocked_badges"]) > 0
    assert len(comparison["locked_badges"]) > 0

    # Podium
    assert podium["first"]["display_name"] == leaderboard[0]["display_name"]
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
    from plex_recommender.db import create_or_update_user, upsert_user_media, upsert_media_item
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
        # Stream King #1 is anonymized with Faker
        assert leaderboard[0]["rank"] == 1
        assert leaderboard[0]["is_me"] is False
        assert leaderboard[0]["is_anonymized"] is True
        assert leaderboard[0]["display_name"] != "Stream King"
        # Ashley #2 retains authentic display name
        assert leaderboard[1]["username"] == "ash"
        assert leaderboard[1]["display_name"] == "Ashley"
        assert leaderboard[1]["rank"] == 2
        assert leaderboard[1]["is_me"] is True
        assert leaderboard[1]["is_anonymized"] is False

        assert comparison["user_rank"] == 2
        assert comparison["leader"]["display_name"] == leaderboard[0]["display_name"]


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
                "unlocked_badges": [
                    {"id": "pioneer", "name": "Server Pioneer", "icon": "🌟", "desc": "Registered community member"}
                ],
                "locked_badges": [
                    {"id": "champion", "name": "Server Champion", "icon": "👑", "desc": "Rank 1", "hint": "Claim #1 spot"}
                ],
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
            assert "Hot on their heels!" in text
            assert "You vs Server Leader" in text
            assert "Plays" in text
            assert "Watch Time" in text
            assert "Why are other usernames anonymized?" in text
            assert "Faker Pseudonyms Active" in text


def test_admin_real_username_tooltip_visibility():
    service = CommunityService()
    admin_user = {
        "user_key": "u_admin",
        "username": "admin_ash",
        "title": "Admin Ashley",
        "is_admin": True,
        "onboarded_at": "2024-01-01T00:00:00",
    }
    non_admin_user = {
        "user_key": "u_regular",
        "username": "regular_joe",
        "title": "Joe",
        "is_admin": False,
    }
    candidates = [
        {
            "user_key": "u_other",
            "username": "secret_real_name_99",
            "display_name": "Secret Real Name",
            "is_me": False,
            "total_plays": 50,
            "total_duration_seconds": 100000,
            "movie_count": 20,
            "show_count": 30,
            "genres": ["Action"],
            "is_registered": True,
        },
        {
            "user_key": "u_admin",
            "username": "admin_ash",
            "display_name": "Admin Ashley",
            "is_me": True,
            "total_plays": 20,
            "total_duration_seconds": 40000,
            "movie_count": 10,
            "show_count": 10,
            "genres": ["Sci-Fi"],
            "is_registered": True,
        },
    ]

    # 1. Admin builds leaderboard: real_username is present
    lb_admin, _, _ = service._build_leaderboard_and_comparison(
        current_user=admin_user,
        candidate_users=candidates.copy(),
    )
    other_for_admin = next(c for c in lb_admin if not c["is_me"])
    assert other_for_admin["real_username"] == "secret_real_name_99"

    # 2. Non-admin builds leaderboard: real_username is None
    candidates_copy = [
        dict(candidates[0]),
        {
            "user_key": "u_regular",
            "username": "regular_joe",
            "display_name": "Joe",
            "is_me": True,
            "total_plays": 20,
            "total_duration_seconds": 40000,
            "movie_count": 10,
            "show_count": 10,
            "genres": ["Sci-Fi"],
            "is_registered": True,
        },
    ]
    lb_non_admin, _, _ = service._build_leaderboard_and_comparison(
        current_user=non_admin_user,
        candidate_users=candidates_copy,
    )
    other_for_non_admin = next(c for c in lb_non_admin if not c["is_me"])
    assert other_for_non_admin["real_username"] is None

    # 3. Web rendering: Admin sees tooltip in HTML, Non-admin does NOT
    with patch("plex_recommender.web.app.get_current_user", return_value=admin_user):
        with patch.object(community_service, "get_community_data") as mock_get:
            mock_get.return_value = {
                "leaderboard": lb_admin,
                "user_comparison": {
                    "user_rank": 2,
                    "total_watchers": 2,
                    "percentile": 50,
                    "is_leader": False,
                    "leader": other_for_admin,
                    "current_user": lb_admin[1],
                    "plays_behind": 30,
                    "hours_behind": 16.7,
                    "plays_pct_of_leader": 40,
                    "hours_pct_of_leader": 40,
                    "next_rank_gap": 31,
                    "next_rank_user": other_for_admin["display_name"],
                    "rank_message": f"Behind {other_for_admin['display_name']}",
                    "unlocked_badges": [],
                    "locked_badges": [],
                },
                "podium": {"first": other_for_admin, "second": lb_admin[1], "third": None},
                "benchmarks": {"user_plays": 20, "user_movie_pct": 50, "user_tv_pct": 50, "community_movie_pct": 50, "community_tv_pct": 50, "overlap_count": 0, "overlap_pct": 0},
                "popular_movies": [],
                "popular_tv": [],
                "unseen_recommendations": [],
                "recent_activity": [],
                "is_single_user": False,
                "total_community_members": 2,
            }
            client = TestClient(app)
            resp = client.get("/community/")
            assert resp.status_code == 200
            # Admin sees tooltip with real username
            assert 'title="Real username: @secret_real_name_99"' in resp.text
            assert "Admin View:" in resp.text
