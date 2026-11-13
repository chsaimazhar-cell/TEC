# TEC — Drift project archive

This repository contains the original **SAIM VIDEO DOWNLODER PROJECT.zip** package. The ZIP contains a project named **Drift — Social Video Downloader**, with a Python backend, a separate HTML/CSS/JavaScript frontend, and a backend integration test.

The original ZIP is preserved under `archive/`, and its source is unpacked into `drift_work/` for easier browsing and maintenance.

**This is a legacy snapshot, not the maintained application.** Use the current [Drift repository](https://github.com/chsaimazhar-cell/drift) for the actively maintained downloader. The archived backend's integration harness passes, but it does not verify/convert every downloaded stream to H.264 MP4 before naming the result `.mp4`. Do not treat this archive as production-ready or expose it publicly without further repair and security review.

> **Responsible use:** Only download media you own or are authorized to save. Follow copyright law and platform terms. Do not use the software to bypass DRM, private accounts, paywalls, or other access controls.

## Project contents

```text
TEC/
├── archive/
│   └── SAIM VIDEO DOWNLODER PROJECT.zip
└── drift_work/
    ├── frontend/
    │   ├── index.html
    │   ├── css/style.css
    │   └── js/app.js
    ├── backend/
    │   ├── app.py
    │   ├── test_backend.py
    │   └── requirements.txt
    ├── .gitignore
    └── README.md
```

## Requirements

- Python 3.10 or newer.
- Node.js (only needed to run the JavaScript syntax check).
- FFmpeg installed and available on PATH.
- Python packages in `drift_work/backend/requirements.txt`.

## Run on Windows

Open PowerShell in the repository folder. First extract the original ZIP if `drift_work/` is not already present:

```powershell
Expand-Archive -LiteralPath ".\archive\SAIM VIDEO DOWNLODER PROJECT.zip" -DestinationPath .
cd .\drift_work\backend
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000 in your browser. Stop the server with Ctrl+C.

## Run on macOS or Linux

```bash
unzip -q "archive/SAIM VIDEO DOWNLODER PROJECT.zip" -d .
cd drift_work/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. Install FFmpeg using your operating system's package manager before starting the app.

## Run tests

From `drift_work/backend`:

```bash
python test_backend.py
python -m py_compile app.py
node --check ../frontend/js/app.js
```

The backend integration test uses a fake extractor so it can test job handling and file streaming without downloading from a real platform. A successful test does not guarantee every third-party platform is currently compatible.

## Automation

GitHub Actions checks ZIP integrity, Python syntax, backend integration tests, and frontend JavaScript syntax. See the Actions tab for the latest run.

## Security and deployment

Before making this service public, review the backend's authentication, rate limiting, CORS, URL validation, storage, and hosting configuration. Do not assume that CORS or a local rate limiter protects a publicly exposed service.

## License

No license is included in the original archive. Reuse and redistribution permissions are not explicitly granted unless the project owner adds a license.
