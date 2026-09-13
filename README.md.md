# Sentinel LoadLab

Sentinel LoadLab is a desktop-based **authorized and bounded web load & performance testing tool** built with Python and CustomTkinter.

It is designed for controlled testing of systems you own or have explicit permission to assess.

## Features

- Premium desktop GUI
- Controlled GET / HEAD testing
- Live latency monitoring
- Achieved RPS tracking
- Success and error rate monitoring
- P50 / P95 / P99 latency metrics
- HTTP 4xx / 5xx tracking
- Live request log
- Response distribution
- Configurable duration, workers, RPS, timeout and warm-up
- Smoke, Baseline, Peak Safe and Max Bounded presets
- TLS verification option
- Redirect handling
- Custom request headers
- CSV export
- JSON export
- HTML performance report
- Save and load configurations
- Keyboard shortcuts
- Authorization confirmation before testing

## Safety Limits

Sentinel LoadLab is intentionally bounded for safe and authorized use.

- Maximum 25 requests per second
- Maximum 20 workers
- Maximum test duration: 5 minutes
- Maximum warm-up: 10 seconds
- GET and HEAD requests only

## Tech Stack

- Python
- CustomTkinter
- Requests
- Matplotlib
- ThreadPoolExecutor

## Installation

Clone the repository:

```bash
git clone https://github.com/omzalavadia/Sentinel-LoadLab.git
```

Open the project directory:

```bash
cd Sentinel-LoadLab
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run

```bash
python sentinel_loadlab.py
```

## Keyboard Shortcuts

- `Ctrl + Enter` — Start test
- `Esc` — Stop test
- `Ctrl + L` — Focus target URL
- `Ctrl + S` — Save configuration
- `Home` — Scroll to top
- `End` — Scroll to bottom

## Project Structure

```text
Sentinel-LoadLab/
├── assets/
│   └── dashboard.png
├── sentinel_loadlab.py
├── README.md
├── requirements.txt
└── .gitignore
```

## Screenshot

After adding your dashboard screenshot to `assets/dashboard.png`, it will appear here:

![Sentinel LoadLab Dashboard](assets/dashboard.png)

## Authorized Use Only

This project is intended only for systems you own or have explicit authorization to test.

Do not use Sentinel LoadLab against third-party systems without permission.

## Author

**Om Zalavadia**

GitHub: [@omzalavadia](https://github.com/omzalavadia)
