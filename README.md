# Views X Pro 🎬

**Views X Pro** is an AI-powered video clipping tool that converts long YouTube videos, streams, and podcasts into short, engaging vertical videos for YouTube Shorts, Instagram Reels, and TikTok.

Just provide a video, and the AI identifies important moments, finds suitable timestamps, and automatically creates short clips with captions and smart framing.

## ✨ Features

- **AI Highlight Detection:** Finds interesting moments, hooks, and important highlights using Google Gemini.
- **Automatic Clip Generation:** Converts selected timestamps into short videos.
- **Smart Video Cropping:** Creates vertical 9:16 videos with face tracking.
- **Animated Captions:** Generates word-by-word highlighted subtitles.
- **High-Quality Encoding:** Uses FFmpeg for video processing and encoding.
- **Custom Branding:** Add logos, watermarks, and Like/Subscribe animations.
- **Web Dashboard:** Track progress, preview clips, view history, and download clips as ZIP files.

## 🛠️ Tech Stack

- Python and FastAPI
- Google Gemini API
- FFmpeg
- MediaPipe Face Detection
- HTML, CSS, and JavaScript
- WebSockets

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- FFmpeg and FFprobe
- Google Gemini API key

### Installation

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
pip install -r requirements.txt
```

### Configure Environment Variables

Create a `.env` file:

```env
GEMINI_API_KEY=your_api_key_here
APP_ENV=production
YTDLP_COOKIES_FILE=cookies.txt
```

yt-dlp uses **default YouTube clients** first (avoids the “confirm you’re not a bot” path that `android`/`ios`/`web` often hit). For Railway hard blocks, add a Netscape `cookies.txt` from a **throwaway** Google account (never commit it; never use your main account).

### Run the Application

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** in your browser.

## 🚂 Deployment

The project supports Railway deployment using the included `Dockerfile` and `railway.toml`.

Configure your `GEMINI_API_KEY` environment variable and generate a public domain in Railway settings.

**Note:** Video processing requires sufficient memory and CPU resources. Temporary files may be lost when the service restarts unless persistent storage is configured.

## 📄 License

This project is licensed under the MIT License.