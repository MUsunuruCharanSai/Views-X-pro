// Views X Pro — Frontend Controller v9
// Fixes: alert() → toasts, score bars, timestamps, API key warning,
//        FALLBACK REASON log coloring, skeleton loaders, lazy video loading

document.addEventListener("DOMContentLoaded", () => {

    let lastPreviewUrl = "";
    let currentSocket = null;

    // ── Element References ────────────────────────────────────
    const urlInput        = document.getElementById("youtube-url");
    const generateBtn     = document.getElementById("generate-btn");
    const apiKeyInput     = document.getElementById("api-key-input");
    const systemStatus    = document.getElementById("system-status");
    const apiKeyWarning   = document.getElementById("api-key-warning");
    const apiKeyWarningLink = document.getElementById("api-key-warning-link");
    const cookiesWarning  = document.getElementById("cookies-warning");
    const cookiesWarningLink = document.getElementById("cookies-warning-link");
    const ytCookiesInput  = document.getElementById("yt-cookies-input");
    const ytCookiesStatus = document.getElementById("yt-cookies-status");
    const ytCookiesSaveBtn = document.getElementById("yt-cookies-save-btn");

    const videoPreviewCard = document.getElementById("video-preview-card");
    const previewThumb     = document.getElementById("preview-thumb");
    const previewTitle     = document.getElementById("preview-title");
    const previewUploader  = document.getElementById("preview-uploader");
    const previewDuration  = document.getElementById("preview-duration");

    const tierRadios       = document.querySelectorAll('input[name="resolution_tier"]');


    const progressSection  = document.getElementById("progress-section");
    const resultsSection   = document.getElementById("results-section");
    const overallPercent   = document.getElementById("overall-percent");
    const logTerminal      = document.getElementById("log-terminal");
    const queueStatusBanner = document.getElementById("queue-status-banner");

    const analysisBanner   = document.getElementById("analysis-banner");
    const partialSuccessNote = document.getElementById("partial-success-note");
    const clipsCountBadge  = document.getElementById("clips-count-badge");
    const downloadZipBtn   = document.getElementById("download-zip-btn");
    const clipsGrid        = document.getElementById("clips-grid");
    const clipsSkeleton    = document.getElementById("clips-skeleton");
    const emptyState       = document.getElementById("empty-state");

    const settingsForm     = document.getElementById("settings-form");
    const settingsDetails  = document.getElementById("settings-details");
    const setGeminiKey     = document.getElementById("set-gemini-key");
    const setClipCount     = document.getElementById("set-clip-count");
    const setConfidence    = document.getElementById("set-confidence");
    const confidenceVal    = document.getElementById("confidence-val");
    const setSpacing       = document.getElementById("set-spacing");
    const spacingHelper    = document.getElementById("spacing-helper");
    const setMinDur        = document.getElementById("set-min-dur");
    const setMaxDur        = document.getElementById("set-max-dur");
    const setFlashForward  = document.getElementById("set-flash-forward");
    const settingsToast    = document.getElementById("settings-saved-toast");

    const storageTotalBadge = document.getElementById("storage-total-badge");
    const statDownloads    = document.getElementById("stat-downloads");
    const statClips        = document.getElementById("stat-clips");
    const statTemp         = document.getElementById("stat-temp");
    const statLogs         = document.getElementById("stat-logs");
    const clearStorageBtn  = document.getElementById("clear-storage-btn");

    const historyCountBadge = document.getElementById("history-count-badge");
    const historyList       = document.getElementById("history-list");

    // Mode Selector elements
    const modeIrlBtn          = document.getElementById("mode-irl-btn");
    const modeDesktopBtn      = document.getElementById("mode-desktop-btn");
    const modeStatusBadge     = document.getElementById("mode-status-badge");

    // Facecam Calibration elements
    const calibrateUrlInput   = document.getElementById("calibrate-url-input");
    const calibrateBtn        = document.getElementById("calibrate-btn");
    const refreshPresetsBtn   = document.getElementById("refresh-presets-btn");
    const presetsList         = document.getElementById("presets-list");
    const facecamDetails      = document.getElementById("facecam-details");

    let currentMode = "desktop";       // "desktop" (default Full 16:9 + Blurred BG) | "irl"

    // ── Mode Selector ────────────────────────────────────────
    function setMode(mode) {
        currentMode = mode;
        const isDesktop = mode === "desktop";

        if (modeIrlBtn) {
            modeIrlBtn.classList.toggle("mode-tile-active", !isDesktop);
            modeIrlBtn.setAttribute("aria-selected", !isDesktop ? "true" : "false");
        }
        if (modeDesktopBtn) {
            modeDesktopBtn.classList.toggle("mode-tile-active", isDesktop);
            modeDesktopBtn.setAttribute("aria-selected", isDesktop ? "true" : "false");
        }

        if (modeStatusBadge) {
            if (!isDesktop) {
                modeStatusBadge.textContent = "IRL / Smart Face Crop";
                modeStatusBadge.className = "status-chip chip-purple";
            } else {
                modeStatusBadge.textContent = "Desktop Streamer";
                modeStatusBadge.className = "status-chip chip-cyan";
            }
        }
    }

    if (modeIrlBtn) {
        modeIrlBtn.addEventListener("click", e => {
            e.preventDefault();
            e.stopPropagation();
            setMode("irl");
            toast("Switched to IRL / Smart Face Crop Mode", "info", 2000);
        });
    }

    if (modeDesktopBtn) {
        modeDesktopBtn.addEventListener("click", e => {
            e.preventDefault();
            e.stopPropagation();
            setMode("desktop");
            toast("Switched to Desktop Streamer Mode", "info", 2000);
        });
    }

    // ── Channel Brand Logo Selector ──────────────────────────
    const logoStatusBadge    = document.getElementById("logo-status-badge");
    const logoSelectorGrid   = document.getElementById("logo-selector-grid");
    let selectedLogo         = localStorage.getItem("selected_logo") || "logo.png";

    function updateLogoBadge(logoId, logoTitle) {
        if (!logoStatusBadge) return;
        if (logoId === "none") {
            logoStatusBadge.textContent = "No Logo";
            logoStatusBadge.className = "status-chip";
            logoStatusBadge.style.cssText = "background:rgba(255,255,255,0.06);color:var(--text-muted);border-color:rgba(255,255,255,0.1);";
        } else {
            logoStatusBadge.textContent = logoTitle || logoId.replace(/\.[^/.]+$/, "");
            logoStatusBadge.className = "status-chip chip-cyan";
            logoStatusBadge.style.cssText = "";
        }
    }

    function selectLogo(logoId, logoTitle) {
        selectedLogo = logoId;
        try { localStorage.setItem("selected_logo", logoId); } catch(e) {}

        if (logoSelectorGrid) {
            logoSelectorGrid.querySelectorAll(".logo-tile").forEach(el => {
                el.classList.toggle("active", el.dataset.logo === logoId);
            });
        }
        updateLogoBadge(logoId, logoTitle);
    }

    async function fetchAvailableLogos() {
        try {
            const res = await fetch("/api/logos");
            if (!res.ok) return;
            const data = await res.json();
            const logos = data.logos || [];
            if (!logoSelectorGrid || logos.length === 0) return;

            logoSelectorGrid.innerHTML = "";
            let matched = false;

            logos.forEach((logo, idx) => {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "logo-tile";
                btn.dataset.logo = logo.id;

                const isSelected = selectedLogo === logo.id || (!matched && !selectedLogo && idx === 0);
                if (isSelected) {
                    btn.classList.add("active");
                    matched = true;
                    updateLogoBadge(logo.id, logo.name);
                }

                btn.innerHTML = `
                    <div class="logo-tile-thumb">
                        <img src="${escapeHtml(logo.url)}" alt="${escapeHtml(logo.name)}">
                    </div>
                    <div class="logo-tile-info">
                        <span class="logo-tile-name">${escapeHtml(logo.name)}</span>
                        <span class="logo-tile-tag">Profile ${idx + 1}</span>
                    </div>
                    <span class="logo-tile-check">&#10003;</span>
                `;

                btn.addEventListener("click", e => {
                    e.preventDefault();
                    e.stopPropagation();
                    selectLogo(logo.id, logo.name);
                    toast(`Selected: ${logo.name}`, "info", 1600);
                });

                logoSelectorGrid.appendChild(btn);
            });

            // Always add "No Logo" option
            const noneBtn = document.createElement("button");
            noneBtn.type = "button";
            noneBtn.className = "logo-tile logo-tile-none" + (selectedLogo === "none" ? " active" : "");
            noneBtn.dataset.logo = "none";
            if (selectedLogo === "none") updateLogoBadge("none", "No Logo");

            noneBtn.innerHTML = `
                <div class="logo-tile-thumb logo-tile-thumb-none"><span>&#8212;</span></div>
                <div class="logo-tile-info">
                    <span class="logo-tile-name">No Logo</span>
                    <span class="logo-tile-tag">Clean bar</span>
                </div>
                <span class="logo-tile-check">&#10003;</span>
            `;
            noneBtn.addEventListener("click", e => {
                e.preventDefault();
                e.stopPropagation();
                selectLogo("none", "No Logo");
                toast("Brand logo disabled", "info", 1600);
            });
            logoSelectorGrid.appendChild(noneBtn);

            if (!matched && selectedLogo !== "none" && logos.length > 0) {
                selectLogo(logos[0].id, logos[0].name);
            }
        } catch (err) {
            console.warn("Could not fetch available logos:", err);
            // Fallback: wire static HTML logo-tile click handlers
            if (logoSelectorGrid) {
                logoSelectorGrid.querySelectorAll(".logo-tile").forEach(item => {
                    item.addEventListener("click", e => {
                        e.preventDefault();
                        const id = item.dataset.logo;
                        const title = item.querySelector(".logo-tile-name")?.textContent || id;
                        selectLogo(id, title);
                    });
                });
                selectLogo(selectedLogo, selectedLogo.replace(/\.[^/.]+$/, ""));
            }
        }
    }





    // ── Toast Notification System ─────────────────────────────
    // Replaces all alert() calls with non-blocking toasts
    function toast(message, type = "info", duration = 4000) {
        const container = document.getElementById("toast-container");
        const el = document.createElement("div");
        el.className = `toast toast-${type}`;

        const icon = { success: "✅", error: "❌", warning: "⚠️", info: "ℹ️" }[type] || "ℹ️";
        el.innerHTML = `<span>${icon}</span><span>${message}</span>`;
        container.appendChild(el);

        setTimeout(() => {
            el.classList.add("out");
            setTimeout(() => el.remove(), 300);
        }, duration);
    }

    // ── API Key Warning Banner ────────────────────────────────
    function updateApiKeyWarning(settings) {
        const hasKey = !!(settings.gemini_api_key);

        if (!hasKey) {
            apiKeyWarning.classList.add("visible");
        } else {
            apiKeyWarning.classList.remove("visible");
        }
    }

    apiKeyWarningLink.addEventListener("click", () => {
        settingsDetails.open = true;
        settingsDetails.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    async function refreshCookiesStatus() {
        if (!ytCookiesStatus && !cookiesWarning) return;
        try {
            const res = await fetch("/api/youtube-cookies/status");
            if (!res.ok) return;
            const data = await res.json();
            const auth = data.auth || "unknown";
            if (ytCookiesStatus) {
                const insp = data.inspection;
                const login = insp?.login_cookies?.length
                    ? ` login=[${insp.login_cookies.join(", ")}]`
                    : "";
                ytCookiesStatus.textContent = data.configured
                    ? `Status: configured (${auth})${login}`
                    : `Status: not configured (${auth})`;
            }
            if (cookiesWarning) {
                if (data.cookies_recommended && !data.configured) {
                    cookiesWarning.classList.add("visible");
                } else {
                    cookiesWarning.classList.remove("visible");
                }
            }
        } catch (err) {
            console.warn("Cookies status failed:", err);
        }
    }

    cookiesWarningLink?.addEventListener("click", (e) => {
        e.preventDefault();
        settingsDetails.open = true;
        ytCookiesInput?.focus();
        settingsDetails.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    ytCookiesSaveBtn?.addEventListener("click", async () => {
        const text = (ytCookiesInput?.value || "").trim();
        if (!text) {
            toast("Paste Netscape cookies.txt contents first.", "error");
            return;
        }
        ytCookiesSaveBtn.disabled = true;
        try {
            const looksB64 = /^[A-Za-z0-9+/=\s]+$/.test(text)
                && !text.includes("\t")
                && text.length > 80
                && !text.includes("# Netscape");
            const payload = looksB64
                ? { cookies_base64: text.replace(/\s+/g, "") }
                : { cookies: text };
            const res = await fetch("/api/youtube-cookies", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || "Failed to save cookies");
            toast(data.message || "YouTube cookies saved.", "success", 7000);
            if (ytCookiesInput) ytCookiesInput.value = "";
            await refreshCookiesStatus();
        } catch (err) {
            toast(`Cookies error: ${err.message}`, "error", 8000);
        } finally {
            ytCookiesSaveBtn.disabled = false;
        }
    });

    // ── Resolution Tier Radio Selector ────────────────────────
    document.querySelectorAll(".tier-option").forEach(label => {
        label.addEventListener("click", () => {
            const radio = label.querySelector('input[name="resolution_tier"]');
            if (radio && !radio.checked) {
                radio.checked = true;
                radio.dispatchEvent(new Event("change", { bubbles: true }));
            }
        });
    });

    tierRadios.forEach(radio => {
        radio.addEventListener("change", () => {
            document.querySelectorAll(".tier-option").forEach(l => l.classList.remove("active"));
            const activeLabel = radio.closest(".tier-option");
            if (activeLabel) activeLabel.classList.add("active");
        });
    });

    function getSelectedResolutionTier() {
        const checked = document.querySelector('input[name="resolution_tier"]:checked');
        return checked ? checked.value : "1440p";
    }

    function setSelectedResolutionTier(tier) {
        tierRadios.forEach(r => {
            r.checked = (r.value === tier);
            if (r.checked) r.dispatchEvent(new Event("change", { bubbles: true }));
        });
    }

    // ── YouTube URL Validation / Normalize ────────────────────
    function normalizeYouTubeUrl(raw) {
        let url = (raw || "").trim();
        // Strip wrapping quotes and markdown links
        url = url.replace(/^['"]+|['"]+$/g, "");
        const md = url.match(/^\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)$/);
        if (md) url = md[2].trim();
        const m = url.match(/(?:youtube\.com\/(?:watch\?v=|shorts\/|embed\/|live\/)|youtu\.be\/)([0-9A-Za-z_-]{11})/);
        if (m) return `https://www.youtube.com/watch?v=${m[1]}`;
        return url;
    }

    function isValidYouTubeUrl(url) {
        if (!url) return false;
        return /(?:youtube\.com\/(?:watch\?v=|shorts\/|embed\/|live\/)|youtu\.be\/)([0-9A-Za-z_-]{11})/.test(normalizeYouTubeUrl(url));
    }

    // ── Video Preview Fetcher ─────────────────────────────────
    async function fetchVideoPreview(rawUrl) {
        const url = normalizeYouTubeUrl(rawUrl);
        if (!url || url === lastPreviewUrl || !isValidYouTubeUrl(url)) return;

        // Write cleaned URL back into the input so Process uses the same value
        if (urlInput.value.trim() !== url) urlInput.value = url;

        previewTitle.textContent = "Fetching video info…";
        previewUploader.textContent = "";
        previewDuration.textContent = "";
        videoPreviewCard.classList.remove("hidden");

        const controller = new AbortController();
        // YouTube metadata often takes 20–45s when bot-checks / retries run
        const timeoutId = setTimeout(() => controller.abort(), 60000);

        try {
            const res = await fetch("/api/preview-info", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url }),
                signal: controller.signal
            });
            clearTimeout(timeoutId);
            if (!res.ok) {
                let detail = `Preview failed (${res.status})`;
                try {
                    const errBody = await res.json();
                    detail = errBody.detail || detail;
                } catch (_) { /* ignore */ }
                previewTitle.textContent = "Could not load preview";
                previewUploader.textContent = String(detail).slice(0, 180);
                toast(detail, "warning");
                return;
            }
            const data = await res.json();
            lastPreviewUrl = url;

            previewThumb.src = data.thumbnail;
            previewTitle.textContent = data.title;
            previewUploader.textContent = `📺 ${data.uploader}`;
            const d = data.duration || 0;
            previewDuration.textContent = `⏱ ${Math.floor(d / 60)}:${String(d % 60).padStart(2, "0")}`;
        } catch (err) {
            clearTimeout(timeoutId);
            console.warn("Preview fetch failed or timed out:", err);
            previewTitle.textContent = "Preview timed out / failed";
            previewUploader.textContent = "Try again — YouTube can be slow. Processing may still work.";
            toast("Video preview timed out. You can still click Generate.", "warning");
        }
    }

    urlInput.addEventListener("blur", () => {
        const url = normalizeYouTubeUrl(urlInput.value);
        if (url && url !== urlInput.value.trim()) urlInput.value = url;
        fetchVideoPreview(url);
    });

    urlInput.addEventListener("paste", () => {
        setTimeout(() => {
            const url = normalizeYouTubeUrl(urlInput.value);
            if (url) urlInput.value = url;
            fetchVideoPreview(url);
        }, 150);
    });

    function formatSecs(seconds) {
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return m > 0 ? `${m}m ${s}s` : `${s}s`;
    }

    function formatTimestamp(seconds) {
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
    }

    // ── Initial Data Load ─────────────────────────────────────
    fetchSettings();
    refreshCookiesStatus();
    fetchStorageStats();
    fetchJobHistory();
    fetchAvailableLogos();

    // ── Settings Handlers ─────────────────────────────────────
    setConfidence.addEventListener("input", () => {
        confidenceVal.textContent = `${Math.round(parseFloat(setConfidence.value) * 100)}%`;
    });

    setSpacing.addEventListener("input", () => {
        const sec = parseInt(setSpacing.value) || 0;
        const m = Math.floor(sec / 60);
        const s = sec % 60;
        spacingHelper.textContent = m > 0 ? `${m}m ${s}s gap between clips` : `${s}s gap between clips`;
    });

    settingsForm.addEventListener("submit", async e => {
        e.preventDefault();
        const payload = {
            llm_provider: "gemini",
            resolution_tier: getSelectedResolutionTier(),
            gemini_api_key: setGeminiKey.value.trim(),
            target_clip_count: parseInt(setClipCount.value) || 4,
            min_confidence_score: parseFloat(setConfidence.value) || 0.6,
            min_clip_spacing_seconds: parseInt(setSpacing.value) || 180,
            min_clip_duration_sec: parseFloat(setMinDur.value) || 15.0,
            max_clip_duration_sec: parseFloat(setMaxDur.value) || 90.0,
            flash_forward_hook: setFlashForward ? setFlashForward.checked : true,
        };

        try {
            const res = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || "Failed to save settings.");
            }
            settingsToast.classList.remove("hidden");
            setTimeout(() => settingsToast.classList.add("hidden"), 2500);
            toast("Settings saved successfully!", "success");
            updateApiKeyWarning(payload);
        } catch (err) {
            toast(`Settings error: ${err.message}`, "error", 6000);
        }
    });

    async function fetchSettings() {
        try {
            const res = await fetch("/api/settings");
            if (!res.ok) return;
            const data = await res.json();

            setSelectedResolutionTier(data.resolution_tier || "1440p");
            setGeminiKey.value = data.gemini_api_key || "";
            setClipCount.value = data.target_clip_count || 4;
            setConfidence.value = data.min_confidence_score || 0.6;
            confidenceVal.textContent = `${Math.round((data.min_confidence_score || 0.6) * 100)}%`;
            setSpacing.value = data.min_clip_spacing_seconds || 180;
            const m = Math.floor((data.min_clip_spacing_seconds || 180) / 60);
            spacingHelper.textContent = `${m}m gap between clips`;
            setMinDur.value = data.min_clip_duration_sec || 15.0;
            setMaxDur.value = data.max_clip_duration_sec || 90.0;
            if (setFlashForward) {
                setFlashForward.checked = data.flash_forward_hook !== undefined ? Boolean(data.flash_forward_hook) : true;
            }

            updateApiKeyWarning(data);
        } catch (err) {
            console.warn("Could not fetch settings:", err);
        }
    }

    // ── Storage Handlers ──────────────────────────────────────
    async function fetchStorageStats() {
        try {
            const res = await fetch("/api/storage");
            if (!res.ok) return;
            const data = await res.json();
            storageTotalBadge.textContent = `${data.total_size_mb} MB · ${data.total_files} files`;
            const dirs = data.directories || {};
            statDownloads.textContent = `${dirs.downloads?.size_mb || 0} MB`;
            statClips.textContent     = `${dirs.clips?.size_mb || 0} MB`;
            statTemp.textContent      = `${dirs.temp?.size_mb || 0} MB`;
            statLogs.textContent      = `${dirs.logs?.size_mb || 0} MB`;
        } catch (err) {
            console.warn("Storage stats failed:", err);
        }
    }

    clearStorageBtn.addEventListener("click", async () => {
        if (!confirm("Clear all downloaded videos, clips, and temp files?")) return;
        clearStorageBtn.disabled = true;
        try {
            const res = await fetch("/api/storage/clear", { method: "POST" });
            if (!res.ok) { const e = await res.json(); throw new Error(e.detail); }
            await fetchStorageStats();
            await fetchJobHistory();
            toast("Storage cleared successfully!", "success");
        } catch (err) {
            toast(`Error clearing storage: ${err.message}`, "error");
        } finally {
            clearStorageBtn.disabled = false;
        }
    });

    // ── Job History ───────────────────────────────────────────
    async function fetchJobHistory() {
        try {
            const res = await fetch("/api/history");
            if (!res.ok) return;
            renderJobHistory(await res.json());
        } catch (err) {
            console.warn("History fetch failed:", err);
        }
    }

    function renderJobHistory(history) {
        historyCountBadge.textContent = `${history.length} Run${history.length === 1 ? "" : "s"}`;
        historyList.innerHTML = "";

        if (history.length === 0) {
            historyList.innerHTML = `<div class="log-line muted" style="text-align:center; padding:20px;">No past jobs yet.</div>`;
            return;
        }

        history.forEach(job => {
            const item = document.createElement("div");
            item.className = "history-item";
            const clips = job.clips || [];
            const dateStr = new Date(job.timestamp).toLocaleString();
            const methodTag = job.analysis_method?.includes("gemini") ? "✨ Gemini AI"
                : "ℹ Heuristic";
            const tierTag = job.resolution_tier || "1440p";
            const statusColor = job.status === "completed" ? "var(--green)" : "var(--red)";

            item.innerHTML = `
                <div class="history-header">
                    <div style="min-width:0; flex:1;">
                        <div class="history-title">${escapeHtml(job.video_title || "YouTube Video")}</div>
                        <div class="history-sub">
                            <span style="color:${statusColor}; font-weight:700;">● ${job.status}</span>
                            <span>${dateStr}</span>
                            <span class="count-badge" style="font-size:10px; padding:2px 7px;">${methodTag}</span>
                            <span class="count-badge" style="font-size:10px; padding:2px 7px; background:rgba(0,229,255,0.1); color:var(--cyan);">${tierTag}</span>
                            <span>${clips.length} clip${clips.length !== 1 ? "s" : ""}</span>
                        </div>
                    </div>
                    <div class="history-actions">
                        <button class="btn btn-ghost btn-sm rerun-btn" data-url="${escapeHtml(job.youtube_url)}">🔄 Re-Run</button>
                        ${clips.length > 0 ? `<a href="/api/jobs/${job.job_id}/download-zip" class="btn btn-secondary btn-sm">📦 ZIP</a>` : ""}
                        <button class="btn btn-danger btn-sm delete-history-btn" data-jobid="${job.job_id}">🗑</button>
                    </div>
                </div>
            `;

            historyList.appendChild(item);
        });

        document.querySelectorAll(".rerun-btn").forEach(btn => {
            btn.addEventListener("click", e => {
                const url = e.currentTarget.dataset.url;
                if (url) {
                    urlInput.value = url;
                    fetchVideoPreview(url);
                    window.scrollTo({ top: 0, behavior: "smooth" });
                    settingsDetails.open = true;
                }
            });
        });

        document.querySelectorAll(".delete-history-btn").forEach(btn => {
            btn.addEventListener("click", async e => {
                const jobId = e.currentTarget.dataset.jobid;
                if (!confirm(`Delete job ${jobId} and its clip files?`)) return;
                try {
                    const res = await fetch(`/api/history/${jobId}`, { method: "DELETE" });
                    if (!res.ok) { const err = await res.json(); throw new Error(err.detail); }
                    await fetchJobHistory();
                    await fetchStorageStats();
                    toast("Job deleted.", "success");
                } catch (err) {
                    toast(`Delete failed: ${err.message}`, "error");
                }
            });
        });
    }

    // ── Core Pipeline & WebSocket ─────────────────────────────
    generateBtn.addEventListener("click", async () => {
        const url = normalizeYouTubeUrl(urlInput.value);
        urlInput.value = url;
        if (!url) { toast("Please paste a YouTube URL.", "warning"); return; }
        if (!isValidYouTubeUrl(url)) { toast("Invalid YouTube URL format. Check the URL and try again.", "warning"); return; }

        const selectedTier = getSelectedResolutionTier();
        const apiKey = apiKeyInput.value.trim();

        generateBtn.disabled = true;
        systemStatus.textContent = "Pipeline Running…";

        progressSection.classList.remove("hidden");
        resultsSection.classList.remove("hidden");
        analysisBanner.classList.add("hidden");
        partialSuccessNote.classList.add("hidden");
        queueStatusBanner.classList.add("hidden");
        emptyState.classList.add("hidden");
        clipsGrid.classList.add("hidden");
        clipsGrid.innerHTML = "";
        clipsSkeleton.classList.remove("hidden");
        logTerminal.innerHTML = "";
        resetProgressBars();

        appendLog(`Starting pipeline — ${selectedTier} resolution | Mode: ${currentMode === "desktop" ? "Desktop Streamer (Full-Frame 16:9 + Black Letterbox)" : "IRL / Smart Face Tracking"} | Logo: ${selectedLogo === "none" ? "None" : selectedLogo}`, "info");

        // Send explicit mode flags so the backend respects user choice, overriding auto-calibration.
        // IRL mode: {is_irl: true} → app.py forces smart face crop, ignores any saved channel presets.
        // Desktop mode: {force_desktop: true} → app.py forces Desktop Streamer Full-Frame 16:9,
        //   ignoring auto-calibration that would otherwise override the user's Desktop selection with IRL.
        const activeRegion = (currentMode === "desktop") ? { force_desktop: true } : { is_irl: true };

        const payload = {
            url,
            api_key: apiKey,
            resolution_tier: selectedTier,
            facecam_region: activeRegion,
            selected_logo: selectedLogo,
        };


        try {
            const res = await fetch("/api/process", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (!res.ok) { const e = await res.json(); throw new Error(e.detail || "Failed to start pipeline."); }
            const data = await res.json();
            appendLog(`Job created: ${data.job_id}`, "success");
            connectWebSocket(data.job_id);
        } catch (err) {
            toast(`Pipeline start failed: ${err.message}`, "error", 8000);
            generateBtn.disabled = false;
            systemStatus.textContent = "Error";
            appendLog(`[ERROR] ${err.message}`, "error");
            clipsSkeleton.classList.add("hidden");
        }
    });

    let statusPollTimer = null;
    let activeJobId = null;

    function stopStatusPolling() {
        if (statusPollTimer) {
            clearInterval(statusPollTimer);
            statusPollTimer = null;
        }
    }

    function startStatusPolling(jobId) {
        if (statusPollTimer) return;
        statusPollTimer = setInterval(async () => {
            if (!activeJobId || activeJobId !== jobId) {
                stopStatusPolling();
                return;
            }
            try {
                const res = await fetch(`/api/status/${jobId}`);
                if (!res.ok) return;
                const data = await res.json();
                if (data.status === "completed") {
                    stopStatusPolling();
                    handlePipelineEvent({ type: "completed", ...data, job_id: jobId });
                } else if (data.status === "failed") {
                    stopStatusPolling();
                    handlePipelineEvent({ type: "failed", ...data, job_id: jobId });
                } else if (data.status === "processing" || data.status === "queued") {
                    handlePipelineEvent({
                        type: "progress",
                        stage: data.stage || "Processing...",
                        progress: data.progress || 0,
                        status: data.stage || ""
                    });
                }
            } catch (pollErr) {
                console.debug("Status poll check:", pollErr);
            }
        }, 2500);
    }

    function connectWebSocket(jobId) {
        activeJobId = jobId;
        stopStatusPolling();

        if (currentSocket) {
            try {
                currentSocket.onclose = null;
                currentSocket.onerror = null;
                currentSocket.close();
            } catch (_) {}
            currentSocket = null;
        }

        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${proto}//${window.location.host}/ws/${jobId}`);
        currentSocket = ws;

        ws.onopen = () => appendLog("WebSocket connected — live pipeline stream active.", "success");
        ws.onmessage = e => {
            try {
                const parsed = JSON.parse(e.data);
                handlePipelineEvent(parsed);
            } catch (err) {
                console.warn("WebSocket parse error:", err);
            }
        };
        ws.onerror = () => {
            appendLog("WebSocket connection interrupted. Switching to live polling...", "warning");
            startStatusPolling(jobId);
        };
        ws.onclose = () => {
            if (activeJobId === jobId) {
                appendLog("WebSocket stream closed. Checking live status polling...", "muted");
                startStatusPolling(jobId);
            }
        };
    }

    function handlePipelineEvent(data) {
        if (!data) return;

        if (data.type === "state") {
            if (data.progress !== undefined) overallPercent.textContent = `${Math.round(data.progress)}%`;
            if (data.stage) updateStageProgress(data.stage, data.progress || 0, data.status || "");

            if (Array.isArray(data.logs) && data.logs.length > 0) {
                logTerminal.innerHTML = "";
                data.logs.forEach(l => appendLog(l, classifyLog(l)));
            }
        }

        if (data.type === "progress") {
            const stage    = data.stage || "";
            const progress = data.progress !== undefined ? data.progress : 0;
            const status   = data.status || "";

            overallPercent.textContent = `${Math.round(progress)}%`;

            if (stage.includes("Waiting for previous job")) {
                queueStatusBanner.classList.remove("hidden");
            } else {
                queueStatusBanner.classList.add("hidden");
            }

            if (data.log) {
                appendLog(data.log, classifyLog(data.log));
            }

            updateStageProgress(stage, progress, status);
        }

        if (data.type === "log") {
            if (data.log) {
                appendLog(data.log, classifyLog(data.log));
            }
        }

        if (data.type === "completed" || (data.type === "state" && data.status === "completed")) {
            stopStatusPolling();
            activeJobId = null;

            systemStatus.textContent = "Ready";
            generateBtn.disabled = false;
            overallPercent.textContent = "100%";
            queueStatusBanner.classList.add("hidden");
            updateStageProgress("Finalizing...", 100, "Done");
            appendLog("✓ Pipeline complete!", "success");
            clipsSkeleton.classList.add("hidden");

            const clips = data.clips || [];
            renderResultsSection(clips, data.analysis_method, data.attempted_clips, data.generated_clips, data.job_id, data.resolution_tier);
            fetchStorageStats();
            fetchJobHistory();
        }

        if (data.type === "failed") {
            stopStatusPolling();
            activeJobId = null;

            systemStatus.textContent = "Error";
            generateBtn.disabled = false;
            queueStatusBanner.classList.add("hidden");
            clipsSkeleton.classList.add("hidden");
            appendLog(`[FATAL] ${data.error}`, "error");
            toast(`Pipeline failed: ${data.error}`, "error", 10000);
            fetchJobHistory();
        }
    }

    // Classify log lines for color coding
    function classifyLog(msg) {
        if (!msg) return "muted";
        const m = msg.toUpperCase();
        if (m.includes("[FALLBACK REASON]") || m.includes("[FALLBACK]") || m.includes("[ERROR]") || m.includes("FATAL") || m.includes("FAILED")) return "error";
        if (m.includes("WARNING") || m.includes("WARN") || m.includes("HEURISTIC") || m.includes("RETRY") || m.includes("⚠")) return "warning";
        if (m.includes("✓") || m.includes("SUCCESS") || m.includes("COMPLETE") || m.includes("DETECTED") || m.includes("EXTRACTED") || m.includes("CLIP") || m.includes("✨") || m.includes("✅")) return "success";
        if (m.includes("[DOWNLOAD") || m.includes("[TRANSCRIPT") || m.includes("[ANALYSIS") || m.includes("[CUTTING") || m.includes("ℹ") || m.includes("[CALIBRATION]") || m.includes("[LLM]") || m.includes("[CROP") || m.includes("[PAN]")) return "info";
        return "";
    }

    // ── Stage Progress ────────────────────────────────────────
    function updateStageProgress(stage, percent, status) {
        if (!stage) return;
        const s = stage.toLowerCase();

        if (s.includes("download")) {
            setStageFill("download", percent, status);
        } else if (s.includes("extract")) {
            setStageFill("download", 100, "Done ✓");
            setStageFill("extract", percent, status);
        } else if (s.includes("transcript")) {
            setStageFill("download", 100, "Done ✓");
            setStageFill("extract", 100, "Done ✓");
            setStageFill("transcript", percent, status);
        } else if (s.includes("analyz") || s.includes("hook") || s.includes("llm")) {
            setStageFill("download", 100, "Done ✓");
            setStageFill("extract", 100, "Done ✓");
            setStageFill("transcript", 100, "Done ✓");
            setStageFill("analyze", percent, status);
        } else if (s.includes("cut") || s.includes("finaliz") || s.includes("render")) {
            setStageFill("download", 100, "Done ✓");
            setStageFill("extract", 100, "Done ✓");
            setStageFill("transcript", 100, "Done ✓");
            setStageFill("analyze", 100, "Done ✓");
            setStageFill("cut", percent, status);
        }
    }

    function setStageFill(id, pct, status) {
        const fill = document.getElementById(`fill-${id}`);
        const stat = document.getElementById(`status-${id}`);
        if (fill) fill.style.width = `${pct}%`;
        if (stat) stat.textContent = `${Math.round(pct)}% — ${status}`;
    }

    function resetProgressBars() {
        ["download", "extract", "transcript", "analyze", "cut"].forEach(id => {
            const fill = document.getElementById(`fill-${id}`);
            const stat = document.getElementById(`status-${id}`);
            if (fill) fill.style.width = "0%";
            if (stat) stat.textContent = "Pending";
        });
        overallPercent.textContent = "0%";
    }

    function appendLog(msg, cls = "") {
        if (!msg) return;
        // Prevent duplicate consecutive log entries
        const lastLine = logTerminal.lastElementChild;
        if (lastLine && lastLine.textContent.includes(msg)) return;

        const line = document.createElement("div");
        line.className = `log-line${cls ? " " + cls : ""}`;
        const t = new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
        line.textContent = `[${t}] ${msg}`;
        logTerminal.appendChild(line);
        logTerminal.scrollTop = logTerminal.scrollHeight;
    }

    // ── Results Renderer ──────────────────────────────────────
    function renderResultsSection(clips, analysisMethod, attempted, generated, jobId, tier) {
        resultsSection.classList.remove("hidden");
        clipsGrid.classList.remove("hidden");
        clipsCountBadge.textContent = `${clips.length} Clip${clips.length === 1 ? "" : "s"}`;

        downloadZipBtn.href = clips.length > 0 && jobId ? `/api/jobs/${jobId}/download-zip` : "#";
        clips.length > 0 && jobId ? downloadZipBtn.classList.remove("hidden") : downloadZipBtn.classList.add("hidden");

        // Analysis banner
        analysisBanner.className = "banner";
        const method = analysisMethod || "";
        if (method.includes("uniform_fallback")) {
            analysisBanner.classList.add("banner-warning");
            analysisBanner.innerHTML = `<span class="banner-icon">⚠️</span><span>No captions/transcript found for this video — clips are <strong>evenly spaced</strong>. AI selection needs YouTube captions (or auto-captions). Your Gemini API key is only used <em>after</em> captions load.</span>`;
            analysisBanner.classList.remove("hidden");
        } else if (method.includes("heuristic_transcript")) {
            analysisBanner.classList.add("banner-warning");
            analysisBanner.innerHTML = `<span class="banner-icon">ℹ️</span><span>Captions found, but clips used <strong>heuristic analysis</strong> (no Gemini key active). <a href="#" id="banner-settings-link" style="color:var(--cyan); text-decoration:underline;">Add your Gemini API key in Settings</a> for AI-powered selection.</span>`;
            analysisBanner.classList.remove("hidden");
            document.getElementById("banner-settings-link")?.addEventListener("click", e => {
                e.preventDefault();
                settingsDetails.open = true;
                settingsDetails.scrollIntoView({ behavior: "smooth" });
            });
        } else if (method.includes("gemini")) {
            const provider = "Gemini AI";
            const faceMsg  = method.includes("_facecam") ? " + Adaptive Facecam" : "";
            analysisBanner.classList.add("banner-success");
            analysisBanner.innerHTML = `<span class="banner-icon">✨</span><span>Moments selected by <strong>${provider}</strong> multi-factor scoring${faceMsg} · ${tier}</span>`;
            analysisBanner.classList.remove("hidden");
        }

        if (attempted > 0 && generated < attempted) {
            partialSuccessNote.classList.add("banner-warning");
            partialSuccessNote.innerHTML = `<span class="banner-icon">⚠️</span><span>${generated} of ${attempted} planned clips succeeded.</span>`;
            partialSuccessNote.classList.remove("hidden");
        } else {
            partialSuccessNote.classList.add("hidden");
        }

        clipsGrid.innerHTML = "";

        if (clips.length === 0) {
            clipsGrid.classList.add("hidden");
            emptyState.classList.remove("hidden");
            return;
        }

        emptyState.classList.add("hidden");

        clips.forEach((clip, idx) => {
            const card = document.createElement("div");
            card.className = "clip-card";

            const hook       = clip.hook_text || `Clip #${idx + 1}`;
            const confPct    = Math.round((clip.confidence_score || 0.7) * 100);
            const hookPct    = Math.round((clip.hook_strength   || 0.7) * 100);
            const emoPct     = Math.round((clip.emotional_intensity || 0.7) * 100);
            const unexpPct   = Math.round((clip.unexpectedness  || 0.7) * 100);
            const payoffPct  = Math.round((clip.payoff_quality  || 0.7) * 100);
            const startTs    = formatTimestamp(clip.start_time || 0);
            const dur        = formatSecs(clip.duration || 30);
            const clipId     = `clip-${idx}-${Date.now()}`;

            card.innerHTML = `
                <video class="clip-player" controls preload="none" id="${clipId}">
                    <source src="${clip.url}" type="video/mp4">
                </video>

                <div class="clip-body">
                    <div class="clip-hook-text">${escapeHtml(hook)}</div>

                    <div class="clip-meta-row">
                        <span class="clip-timestamps">⏱ ${startTs} · ${dur}</span>
                        <span class="clip-confidence-badge">★ ${confPct}%</span>
                    </div>

                    <!-- Score Bars (always visible) -->
                    <div class="score-bars">
                        <div class="score-bar-row">
                            <span class="score-bar-label">🪝 Hook</span>
                            <div class="score-bar-track"><div class="score-bar-fill bar-hook" style="width:${hookPct}%"></div></div>
                            <span class="score-bar-pct">${hookPct}%</span>
                        </div>
                        <div class="score-bar-row">
                            <span class="score-bar-label">🔥 Emotion</span>
                            <div class="score-bar-track"><div class="score-bar-fill bar-emotion" style="width:${emoPct}%"></div></div>
                            <span class="score-bar-pct">${emoPct}%</span>
                        </div>
                        <div class="score-bar-row">
                            <span class="score-bar-label">⚡ Surprise</span>
                            <div class="score-bar-track"><div class="score-bar-fill bar-surprise" style="width:${unexpPct}%"></div></div>
                            <span class="score-bar-pct">${unexpPct}%</span>
                        </div>
                        <div class="score-bar-row">
                            <span class="score-bar-label">🎉 Payoff</span>
                            <div class="score-bar-track"><div class="score-bar-fill bar-payoff" style="width:${payoffPct}%"></div></div>
                            <span class="score-bar-pct">${payoffPct}%</span>
                        </div>
                    </div>

                    ${clip.explanation ? `<div class="clip-explanation">"${escapeHtml(clip.explanation)}"</div>` : ""}

                    <div class="clip-actions">
                        <button class="copy-hook-btn" data-hook="${escapeHtml(hook)}" title="Copy hook caption to clipboard">📋 Copy Hook</button>
                        <a href="${clip.url}" download="${clip.name}" class="download-btn">⬇ Download</a>
                    </div>
                </div>
            `;
            clipsGrid.appendChild(card);
        });

        // Copy hook text handlers
        document.querySelectorAll(".copy-hook-btn").forEach(btn => {
            btn.addEventListener("click", async e => {
                const hook = e.currentTarget.dataset.hook;
                try {
                    await navigator.clipboard.writeText(hook);
                    btn.textContent = "✅ Copied!";
                    btn.classList.add("copied");
                    setTimeout(() => {
                        btn.textContent = "📋 Copy Hook";
                        btn.classList.remove("copied");
                    }, 2000);
                } catch {
                    toast("Could not copy to clipboard.", "error");
                }
            });
        });
    }

    // XSS safety
    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    // ── Facecam Calibration ───────────────────────────────────

    async function loadPresets() {
        if (!presetsList) return;
        try {
            const res = await fetch("/api/presets");
            const data = await res.json();
            const presets = data.presets || [];

            if (presets.length === 0) {
                presetsList.innerHTML = '<div class="presets-empty">No presets saved yet. Calibrate a channel to get started.</div>';
                return;
            }

            presetsList.innerHTML = presets.map(p => `
                <div class="preset-card" data-channel-id="${escapeHtml(p.channel_id)}">
                    <div class="preset-card-info">
                        <div class="preset-channel-id">${escapeHtml(p.channel_id)}</div>
                        <div class="preset-region">x=${p.x} y=${p.y} w=${p.w} h=${p.h}</div>
                    </div>
                    <div class="preset-meta">
                        <span class="preset-confidence">${Math.round((p.confidence || 0) * 100)}% conf</span>
                        ${escapeHtml(p.calibrated_at || '')}
                    </div>
                    <button class="btn-delete-preset" data-channel-id="${escapeHtml(p.channel_id)}" title="Delete preset">🗑</button>
                </div>
            `).join('');

            // Wire delete buttons
            presetsList.querySelectorAll('.btn-delete-preset').forEach(btn => {
                btn.addEventListener('click', () => handleDeletePreset(btn.dataset.channelId));
            });

        } catch (e) {
            presetsList.innerHTML = '<div class="presets-empty">Could not load presets.</div>';
        }
    }

    async function handleDeletePreset(channelId) {
        if (!confirm(`Delete preset for channel "${channelId}"?`)) return;
        try {
            const res = await fetch(`/api/presets/${encodeURIComponent(channelId)}`, { method: 'DELETE' });
            const data = await res.json();
            if (res.ok) {
                toast(`Preset for '${channelId}' deleted.`, 'success');
                loadPresets();
            } else {
                toast(data.detail || 'Delete failed.', 'error');
            }
        } catch (e) {
            toast('Could not delete preset.', 'error');
        }
    }

    async function handleCalibrate() {
        const url = (calibrateUrlInput?.value || urlInput?.value || '').trim();
        if (!url) {
            toast('Paste a YouTube URL from the channel to calibrate.', 'warning');
            return;
        }

        calibrateBtn.disabled = true;
        calibrateBtn.innerHTML = '<span class="calibrating-spinner"></span>Calibrating...';

        try {
            const res = await fetch('/api/calibrate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ youtube_url: url, force: false })
            });
            const data = await res.json();

            if (!res.ok) {
                toast(data.detail || 'Calibration request failed.', 'error');
                return;
            }

            if (data.status === 'success') {
                const r = data.region;
                toast(
                    `✅ Facecam calibrated for "${data.uploader || data.channel_id}"! Region: x=${r.x} y=${r.y} w=${r.w} h=${r.h}`,
                    'success', 6000
                );
                loadPresets();
            } else if (data.status === 'exists') {
                toast(
                    `ℹ️ Preset already saved for channel "${data.channel_id}". Future videos will auto-use it.`,
                    'info', 5000
                );
            } else if (data.status === 'no_facecam') {
                toast(
                    `⚠️ No consistent facecam found in "${data.uploader || data.channel_id}". Streamer may not use a webcam overlay.`,
                    'warning', 6000
                );
            } else {
                toast(data.message || 'Calibration returned unknown status.', 'warning');
            }
        } catch (e) {
            toast('Calibration failed: ' + e.message, 'error');
        } finally {
            calibrateBtn.disabled = false;
            calibrateBtn.innerHTML = '🎯 Calibrate';
        }
    }

    if (calibrateBtn) calibrateBtn.addEventListener('click', handleCalibrate);
    if (refreshPresetsBtn) refreshPresetsBtn.addEventListener('click', loadPresets);

    // Pre-fill calibrate URL from main input when facecam panel opens
    if (facecamDetails && calibrateUrlInput) {
        facecamDetails.addEventListener('toggle', () => {
            if (facecamDetails.open && !calibrateUrlInput.value && urlInput?.value) {
                calibrateUrlInput.value = urlInput.value;
            }
            if (facecamDetails.open) loadPresets();
        });
    }

    // Load presets on page init
    loadPresets();

});
