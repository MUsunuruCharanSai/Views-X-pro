# Views X Pro 🎬⚡

An automated AI-powered video processing pipeline and web application that transforms long-form YouTube videos, streams, and podcasts into viral 9:16 vertical shorts (TikTok, YouTube Shorts, Instagram Reels).

Powered by **Google Gemini API** (`gemini-3.5-flash`), **MediaPipe Face Detection**, and **Hardware-Accelerated FFmpeg** (Intel Quick Sync & CPU fallback).

---

## 🌟 Key Features

- **🤖 AI Viral Moment Extraction**:
  - Automatically fetches video transcripts and analyzes pacing, emotional spikes, hooks, and payoffs using Google Gemini.
  - Multi-factor scoring engine (Hook Strength, Emotional Intensity, Unexpectedness, Payoff Quality).
  - Multi-model automatic fallback (`gemini-3.5-flash` &rarr; `gemini-3.1-flash-lite` &rarr; `gemini-3.5-flash-lite`).

- **🎯 Smart 9:16 Video Framing**:
  - **Desktop Streamer Mode (Mode B)**: Preserves full 16:9 gameplay/screen content with letterbox padding.
  - **IRL Speaker Mode (Mode C)**: Panning face-centered 9:16 crop dynamically following the active speaker via MediaPipe Face Detection.
  - **Auto-Calibration & Preset Memory**: Learns streamer facecam positions and caches them per YouTube channel ID.

- **🔤 Animated Karaoke Captions**:
  - Precision ASS subtitle generation with word-by-word active highlighting.
  - Resolution-relative sizing and positioning.

- **⚡ Hardware-Accelerated Encoding**:
  - Intel Quick Sync Video (QSV `h264_qsv`) hardware acceleration with intelligent ICQ quality control.
  - Robust CPU fallback (`libx264`) with CRF 18 visually lossless encoding.
  - Selectable resolution canvas: **1080p** (1080×1920), **1440p** (1440×2560), and **4K** (2160×3840).
  - Pre-upscale denoising (`hqdn3d`) and Lanczos detail enhancement.

- **🎨 Branding & Overlays**:
  - Customizable channel logo watermarks (place any PNG/JPEG logo in `assets/`).
  - Overlay engagement animations (Subscribe & Like call-to-actions).

- **🌐 Modern Web Dashboard**:
  - Real-time progress updates and terminal log streaming via WebSockets.
  - Instant video preview with thumbnail, title, channel info, and duration.
  - Job history with in-browser clip playback and single-click ZIP export.
  - Storage management to view and clear temporary cache files.

---

## 🛠️ Prerequisites

- **Python 3.10+**
- **FFmpeg & FFprobe** installed and accessible on system `PATH`.
  - Windows: `winget install Gyan.FFmpeg` or `choco install ffmpeg`
  - Linux: `sudo apt install ffmpeg`
  - macOS: `brew install ffmpeg`
- **Google Gemini API Key**: Free API key from [Google AI Studio](https://aistudio.google.com/).

---

## 🚀 Quick Start

### 1. Clone the Repository
```bash
git clone https://github.com/<your-username>/<your-repo-name>.git
cd <your-repo-name>
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure API Key
Create a `.env` file or export your Gemini API key:
```bash
cp .env.example .env
```
Edit `.env`:
```env
GEMINI_API_KEY=your_gemini_api_key_here
```
*(Alternatively, enter your API key directly in the web dashboard Settings modal).*

### 4. Run the Server
```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```
Open your browser at **`http://localhost:8000`**.

---

## 📂 Project Architecture

```
.
├── analyzer.py             # Gemini API viral moment detection & scoring
├── app.py                  # End-to-end pipeline orchestrator
├── clipper.py              # FFmpeg video clipper, ASS subtitles & hardware encoding
├── config.py               # Central configuration, resolutions & scoring weights
├── downloader.py           # yt-dlp downloader with smart cache reuse
├── facecam_calibration.py  # Facecam auto-calibration & per-channel preset storage
├── facecam_detector.py     # IRL speaker tracking & face-centered 9:16 timeline
├── face_detector_helper.py # MediaPipe Tasks FaceDetector abstraction
├── history_store.py        # Persistent job history & clip management
├── server.py               # FastAPI backend with REST endpoints & WebSockets
├── settings_store.py       # User settings storage
├── timestamp_refiner.py    # Hook-aware boundary and silence refiner
├── utils.py                # Thread-safe logging and system utilities
├── assets/                 # Brand logos, watermarks, and overlay GIFs
├── models/                 # MediaPipe BlazeFace detection models
└── static/                 # Web dashboard UI (HTML, CSS, JS)
```

---

## ⚙️ Configuration & Tiers

| Resolution Tier | Output Canvas | Max Bitrate | Recommended For |
| :--- | :--- | :--- | :--- |
| **1080p (Default)** | 1080 × 1920 | 20 Mbps | Fast processing, optimal for TikTok / Shorts |
| **1440p (2K)** | 1440 × 2560 | 32 Mbps | High-clarity vertical video |
| **4K (UHD)** | 2160 × 3840 | 50 Mbps | Maximum quality output |

---

## 📄 License
MIT License. Feel free to use, modify, and distribute.
