"""Run the ClaimsRAG API with uvicorn, bound to the configured HOST and PORT: ``python -m app``."""

import uvicorn

from app.config import Settings


def main() -> None:
    settings = Settings()
    # access_log=False: the app logs each request itself, without the query string that uvicorn's access log includes.
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
