"""Main entry point for Views X Pro Web App."""

import uvicorn
from utils import check_ffmpeg, logger


def main():
    """Validate dependencies and launch web interface."""
    logger.info("Initializing Views X Pro Server...")

    if not check_ffmpeg():
        logger.warning("FFmpeg not found on system path. Please ensure FFmpeg is installed.")

    import os
    dev_mode = os.environ.get("APP_ENV", "production") == "development"
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=dev_mode)


if __name__ == "__main__":
    main()
