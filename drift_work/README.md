# Drift — Social Video Downloader

Drift is a legacy snapshot of a single-page downloader for **publicly accessible media** from supported social platforms. It uses a Python backend with `yt-dlp` and FFmpeg and a lightweight HTML/CSS/JavaScript frontend.

> This archive is retained for reference. The maintained project is at https://github.com/chsaimazhar-cell/drift. This older backend does not verify or transcode every downloaded stream to H.264 MP4 before naming it `.mp4`; do not use it as a public production service without repairing that behavior and reviewing deployment security.

## What this rebuild fixes

- **Single-command app:** the Python backend serves both the API and frontend; no second web server is required.
- **Persistent job state:** download jobs are stored in SQLite instead of disappearing from memory on page refresh.
- **Refresh recovery:** the browser stores active job IDs and restores their cards/progress after refresh.
- **Real live progress:** backend progress hooks report bytes, percentage, speed and ETA.
- **Large-file safe saving:** modern Chromium/Edge browsers stream the finished MP4 directly to disk with the File System Access API instead of loading the entire file into a Blob.
- **Compatibility fallback:** older browsers hand the file to their native download manager instead of forcing a huge JS-memory allocation.
- **HTTP range support:** finished files support byte ranges for reliable browser/native downloads.
- **Correct Best available:** it now requests `bestvideo+bestaudio/best` instead of silently downgrading to 720p.
- **Dynamic qualities:** extractor-reported video heights from 144p through 2160p are exposed, plus Best available.
- **Cancellation:** cancellation is propagated to the downloader through a thread event and cleaned up safely.
- **Download-size enforcement:** the 500 MB limit is checked during progress and again on the final file.
- **SSRF protection:** only supported public-platform hostnames are accepted by default, and resolved addresses are checked for private/internal ranges.
- **Safer rate limiting:** client identity is not taken from an untrusted `X-Forwarded-For` header.
- **Safer filenames:** downloaded filenames are sanitized before being sent to the browser.
- **Automatic cleanup:** old completed/failed/cancelled jobs and their temporary files are removed after the configured TTL.
- **No authentication bypass:** private content, DRM, login-cookie collection and access-control circumvention are not implemented.

## Supported hosts

By default Drift accepts URLs from:

- YouTube / youtu.be
- TikTok
- Instagram
- Facebook / fb.watch
- X / Twitter

You can extend the allowlist in `backend/app.py` if you intentionally want to support additional public sources.

## Requirements

- Python 3.10+
- FFmpeg
- Internet access to the public source you are downloading from
- `yt-dlp` and `certifi` (installed from `backend/requirements.txt`)

### macOS

```bash
brew install ffmpeg
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
python backend/app.py
```

Then open:

```text
http://127.0.0.1:5000
```

### Windows

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
python backend\app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Configuration

Environment variables are optional:

```text
DRIFT_HOST=127.0.0.1
DRIFT_PORT=5000
DRIFT_MAX_DOWNLOAD_MB=500
DRIFT_JOB_TTL_SECONDS=1800
DRIFT_MAX_ACTIVE_JOBS=2
DRIFT_DOWNLOAD_ROOT=<temporary-directory>/drift-downloads
DRIFT_DB_PATH=<temporary-directory>/drift-downloads/drift.sqlite3
DRIFT_ALLOWED_ORIGIN=*
```

For a same-origin local installation, leave `DRIFT_ALLOWED_ORIGIN=*` as-is. If you deliberately expose the API to another frontend origin, set it to that exact origin.

## API

### `GET /api/health`

Reports backend health, downloader availability, FFmpeg availability and active-job count.

### `POST /api/analyze`

```json
{"url":"https://example.com/video"}
```

Returns title, duration, thumbnail, platform and extractor-reported video qualities.

### `POST /api/downloads`

```json
{"url":"https://example.com/video","quality":"1080p","format":"MP4"}
```

Returns `202` and a persistent `job` object.

`quality` may be `best`, `2160p`, `1440p`, `1080p`, `720p`, `480p` or `360p`. Unsupported requested qualities are safely mapped to Best available.

### `GET /api/downloads/<job_id>`

Returns the current persistent job state.

### `POST /api/downloads/<job_id>/cancel`

Requests cancellation of a queued/downloading/processing job.

### `GET /api/downloads/<job_id>/file`

Streams the finished MP4 and supports HTTP `Range` requests.

## Testing

The repository includes a deterministic backend integration harness that replaces the external extractor with a fake downloader and tests the complete state machine without requiring Internet access:

```bash
cd backend
python test_backend.py
```

The test covers:

- health endpoint
- supported quality discovery
- private/local URL rejection
- job creation
- queued/downloading/processing/ready transitions
- real file streaming
- HTTP range requests
- cancellation
- oversize protection
- 404 job handling
- frontend static serving
- frontend JavaScript delivery

The frontend is also syntax-checked with:

```bash
node --check frontend/js/app.js
```

The backend is syntax-checked with:

```bash
python -m py_compile backend/app.py
```

## Notes about real-world integration testing

The deterministic integration test verifies Drift's job/download machinery independently of a third-party platform. A genuine YouTube/TikTok/etc. download additionally requires Internet access and an installed/current `yt-dlp` version. Drift deliberately does not disable TLS verification or bypass private/authenticated content.
