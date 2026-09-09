import uvicorn

from plex_recommender.config import settings


def main():
    uvicorn.run(
        "plex_recommender.web.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
