import os
import time
import pytest

from plex_recommender import poster_cache


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "posters"
    monkeypatch.setattr(poster_cache, "CACHE_DIR", d)
    yield d


def test_low_bandwidth_returns_none():
    assert poster_cache.get_cached_poster_url("603", "movie", low_bandwidth=True) is None


def test_missing_tmdb_id_returns_none():
    assert poster_cache.get_cached_poster_url(None, "movie", low_bandwidth=False) is None
    assert poster_cache.get_cached_poster_url("", "movie", low_bandwidth=False) is None


def test_returns_cached_file_without_network(cache_dir, monkeypatch):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_file = cache_dir / "movie_603.jpg"
    cached_file.write_bytes(b"fake-image-bytes")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("network should not be called for a cache hit")

    monkeypatch.setattr(poster_cache.requests, "get", fail_if_called)
    import plex_recommender.discovery.tmdb as tmdb_mod
    monkeypatch.setattr(tmdb_mod.TMDbClient, "_request", lambda self, *a, **k: fail_if_called())

    url = poster_cache.get_cached_poster_url("603", "movie", low_bandwidth=False)
    assert url == "/static/posters/movie_603.jpg"


def test_successful_fetch_writes_file_and_returns_url(monkeypatch):
    import plex_recommender.discovery.tmdb as tmdb_mod

    monkeypatch.setattr(
        tmdb_mod.TMDbClient, "_request",
        lambda self, endpoint, params=None: {"poster_path": "/abc123.jpg"}
    )

    class FakeResp:
        content = b"downloaded-bytes"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(poster_cache.requests, "get", lambda *a, **k: FakeResp())

    url = poster_cache.get_cached_poster_url("603", "movie", low_bandwidth=False)
    assert url == "/static/posters/movie_603.jpg"
    assert (poster_cache.CACHE_DIR / "movie_603.jpg").read_bytes() == b"downloaded-bytes"


def test_failed_fetch_returns_none_and_writes_nothing(monkeypatch):
    import plex_recommender.discovery.tmdb as tmdb_mod

    def boom(self, endpoint, params=None):
        raise Exception("tmdb unavailable")

    monkeypatch.setattr(tmdb_mod.TMDbClient, "_request", boom)

    url = poster_cache.get_cached_poster_url("603", "movie", low_bandwidth=False)
    assert url is None
    assert not (poster_cache.CACHE_DIR / "movie_603.jpg").exists()


def test_cache_hit_refreshes_atime(cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_file = cache_dir / "movie_603.jpg"
    cached_file.write_bytes(b"fake-image-bytes")

    old_time = time.time() - (10 * 86400)
    os.utime(cached_file, (old_time, old_time))
    assert cached_file.stat().st_atime < time.time() - (9 * 86400)

    poster_cache.get_cached_poster_url("603", "movie", low_bandwidth=False)
    assert cached_file.stat().st_atime > time.time() - 60


def test_sweep_deletes_only_stale_files(cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    fresh = cache_dir / "movie_1.jpg"
    recent = cache_dir / "movie_2.jpg"
    stale = cache_dir / "movie_3.jpg"
    for f in (fresh, recent, stale):
        f.write_bytes(b"x")

    now = time.time()
    os.utime(fresh, (now, now))
    os.utime(recent, (now - 3 * 86400, now - 3 * 86400))
    os.utime(stale, (now - 10 * 86400, now - 10 * 86400))

    result = poster_cache.sweep_expired_posters(ttl_days=7)

    assert result["deleted"] == 1
    assert result["kept"] == 2
    assert not stale.exists()
    assert fresh.exists()
    assert recent.exists()


def test_sweep_empty_cache_dir_returns_zero_counts(cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    result = poster_cache.sweep_expired_posters()
    assert result == {"deleted": 0, "kept": 0, "bytes_freed": 0}


def test_sweep_missing_cache_dir_does_not_crash(cache_dir):
    # cache_dir fixture path is set but not created on disk
    assert not cache_dir.exists()
    result = poster_cache.sweep_expired_posters()
    assert result == {"deleted": 0, "kept": 0, "bytes_freed": 0}


# --- get_cached_poster_url_from_source (recommendations page) ---

def test_from_source_low_bandwidth_returns_none():
    assert poster_cache.get_cached_poster_url_from_source(
        "603", "movie", "https://image.tmdb.org/t/p/w342/abc.jpg", low_bandwidth=True
    ) is None


def test_from_source_missing_tmdb_id_or_url_returns_none():
    assert poster_cache.get_cached_poster_url_from_source(None, "movie", "https://x/y.jpg") is None
    assert poster_cache.get_cached_poster_url_from_source("603", "movie", None) is None


def test_from_source_cache_hit_skips_network(cache_dir, monkeypatch):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_file = cache_dir / "movie_603.jpg"
    cached_file.write_bytes(b"fake-image-bytes")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("network should not be called for a cache hit")

    monkeypatch.setattr(poster_cache.requests, "get", fail_if_called)

    url = poster_cache.get_cached_poster_url_from_source(
        "603", "movie", "https://image.tmdb.org/t/p/w342/abc.jpg", low_bandwidth=False
    )
    assert url == "/static/posters/movie_603.jpg"


def test_from_source_downloads_and_caches_on_miss(monkeypatch):
    class FakeResp:
        content = b"downloaded-bytes"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(poster_cache.requests, "get", lambda *a, **k: FakeResp())

    url = poster_cache.get_cached_poster_url_from_source(
        "603", "movie", "https://image.tmdb.org/t/p/w342/abc.jpg", low_bandwidth=False
    )
    assert url == "/static/posters/movie_603.jpg"
    assert (poster_cache.CACHE_DIR / "movie_603.jpg").read_bytes() == b"downloaded-bytes"


def test_from_source_falls_back_to_remote_url_on_download_failure(monkeypatch):
    def boom(*a, **k):
        raise Exception("network unreachable")

    monkeypatch.setattr(poster_cache.requests, "get", boom)

    remote_url = "https://image.tmdb.org/t/p/w342/abc.jpg"
    url = poster_cache.get_cached_poster_url_from_source("603", "movie", remote_url, low_bandwidth=False)
    assert url == remote_url  # graceful degradation, not None
    assert not (poster_cache.CACHE_DIR / "movie_603.jpg").exists()
