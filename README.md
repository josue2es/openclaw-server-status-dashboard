# Openclaw Server Status Dashboard

A lightweight self-hosted server monitoring dashboard for servers running [openclaw](https://openclaw.ai). Built with pure Python and vanilla JS — no extra dependencies beyond what's already on your system.

![Dashboard](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Live system stats** — CPU, RAM, disk, network throughput, ping/jitter/packet loss
- **Historical charts** — 3-hour rolling window, 24-hour retention in SQLite
- **Openclaw integration** — displays active cron jobs, agent memories, and issues from your openclaw workspace
- **API health test** — test Anthropic, Google, and Moonshot/Kimi keys directly from the dashboard with latency measurement
- **Internet speed test** — powered by Ookla speedtest CLI, rate-limited to once per hour
- **Zero token usage** — the dashboard itself makes no LLM calls

## Requirements

- Python 3.10+
- A server running [openclaw](https://openclaw.ai)
- Ookla speedtest CLI at `~/.local/bin/speedtest-ookla` (optional, for speed test feature)

## Installation

### 1. Copy the script

```bash
mkdir -p ~/.openclaw/workspace/scripts
cp dashboard.py ~/.openclaw/workspace/scripts/sys_dashboard.py
```

### 2. Edit paths

Open `sys_dashboard.py` and update these variables at the top to match your setup:

```python
DB_PATH = '/home/YOUR_USER/.openclaw/workspace/dashboard.db'
AUTH_PROFILES_PATH = '/home/YOUR_USER/.openclaw/agents/main/agent/auth-profiles.json'
```

Also update the hardcoded paths in `get_cron_jobs()`, `get_memories()`, and `get_issues()` to point to your openclaw workspace.

### 3. Create the systemd user service

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/dashboard.service << EOF
[Unit]
Description=Openclaw Server Status Dashboard
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/YOUR_USER/.openclaw/workspace/scripts/sys_dashboard.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
```

### 4. Enable and start

```bash
systemctl --user daemon-reload
systemctl --user enable --now dashboard
```

The dashboard will be available at `http://localhost:8080`.

### 5. (Optional) Enable linger so the service survives logout

```bash
loginctl enable-linger YOUR_USER
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | Port to listen on |
| `DB_PATH` | See script | Path to SQLite database |
| `AUTH_PROFILES_PATH` | See script | Path to openclaw auth profiles |
| `APITEST_COOLDOWN` | `30` | Seconds between API test requests |

## Dashboard Layout

1. Live Resource Usage (CPU & RAM)
2. Network Stats (latency, jitter, packet loss)
3. Network Throughput
4. API Test / Memory & Disk / Speed Test
5. Top Processes & Security Logins
6. Active Cron Jobs
7. Recent Memories
8. Active & Resolved Issues

## Notes

- The dashboard runs entirely on the server — no external services, no token usage
- System metrics (CPU, RAM, network) update every 15 seconds
- Openclaw data (cron jobs, memories, issues) updates every 60 seconds
- All data is stored locally in SQLite with 24-hour retention

## License

MIT
