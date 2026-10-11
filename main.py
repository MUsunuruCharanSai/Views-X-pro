"""Main entry point for Views X Pro Web App."""

import uvicorn
from utils import check_ffmpeg, logger


def main():
    """Validate dependencies and launch web interface."""
    logger.info("Initializing Views X Pro Server...")

    from utils import ensure_ffmpeg_on_path
    ffmpeg_bin = ensure_ffmpeg_on_path()
    if not ffmpeg_bin:
        logger.warning("FFmpeg not found. Install FFmpeg or set FFMPEG_PATH — downloads will fail.")
    else:
        logger.info(f"FFmpeg ready: {ffmpeg_bin}")

    import os
    dev_mode = os.environ.get("APP_ENV", "production") == "development"
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=dev_mode)


if __name__ == "__main__":
    main()
