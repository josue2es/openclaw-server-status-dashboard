import http.server
import socketserver
import json
import subprocess
import os
import time
import shutil
import sqlite3
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

PORT = 8080
DB_PATH = '/home/josue2es/.openclaw/workspace/dashboard.db'
AUTH_PROFILES_PATH = '/home/josue2es/.openclaw/agents/main/agent/auth-profiles.json'
last_speedtest_time = 0
cached_speedtest_result = "Not run yet."
last_apitest_time = 0
APITEST_COOLDOWN = 30  # seconds between API tests

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS metrics
                    (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, 
                     cpu REAL, ram REAL, rx REAL, tx REAL, ping REAL, jitter REAL, loss REAL)''')
    conn.commit()
    conn.close()

def insert_metric(data):
    pass # Deprecated, logic moved to background_worker

def cleanup_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM metrics WHERE timestamp < datetime("now", "-24 hours")')
    conn.commit()
    conn.close()

def background_worker():
    init_db()
    last_rx = None
    last_tx = None
    last_cpu_idle = None
    last_cpu_total = None
    last_time = None
    
    while True:
        try:
            data = get_sys_info()
            now_time = time.time()
            
            if last_rx is not None:
                diff_time = now_time - last_time
                if diff_time > 0:
                    rx_bps = (data.get('net_rx_bytes', 0) - last_rx) / diff_time
                    tx_bps = (data.get('net_tx_bytes', 0) - last_tx) / diff_time
                    
                    idle = data.get('cpu_raw', {}).get('idle', 0)
                    total = data.get('cpu_raw', {}).get('total', 0)
                    idle_diff = idle - last_cpu_idle
                    total_diff = total - last_cpu_total
                    cpu_pct = 0.0
                    if total_diff > 0:
                        cpu_pct = ((total_diff - idle_diff) / total_diff) * 100.0
                    
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute('INSERT INTO metrics (cpu, ram, rx, tx, ping, jitter, loss) VALUES (?, ?, ?, ?, ?, ?, ?)',
                                 (round(cpu_pct, 1),
                                  data.get('ram_percent', 0),
                                  round(rx_bps, 2),
                                  round(tx_bps, 2),
                                  data.get('ping_avg', 0),
                                  data.get('jitter', 0),
                                  data.get('packet_loss', 0)))
                    conn.commit()
                    conn.close()
                    
            last_rx = data.get('net_rx_bytes', 0)
            last_tx = data.get('net_tx_bytes', 0)
            last_cpu_idle = data.get('cpu_raw', {}).get('idle', 0)
            last_cpu_total = data.get('cpu_raw', {}).get('total', 0)
            last_time = now_time
            
            cleanup_db()
        except Exception as e:
            print(f"Background worker error: {e}")
        time.sleep(15)

def get_sys_info():
    info = {}
    
    # CPU Usage
    try:
        with open('/proc/stat', 'r') as f:
            cpu_lines = f.readlines()
            for line in cpu_lines:
                if line.startswith('cpu '):
                    parts = line.split()
                    # user, nice, system, idle, iowait, irq, softirq, steal
                    idle = float(parts[4]) + float(parts[5])
                    non_idle = float(parts[1]) + float(parts[2]) + float(parts[3]) + float(parts[6]) + float(parts[7]) + float(parts[8])
                    total = idle + non_idle
                    info['cpu_raw'] = {'idle': idle, 'total': total}
                    break
    except:
        info['cpu_raw'] = {'idle': 0, 'total': 0}
        
    # RAM Raw for graph
    try:
        with open('/proc/meminfo', 'r') as f:
            mem = {}
            for line in f:
                parts = line.split(':')
                mem[parts[0]] = int(parts[1].strip().split()[0])
            total = mem.get('MemTotal', 1)
            free = mem.get('MemAvailable', mem.get('MemFree', 0))
            used = total - free
            info['ram_percent'] = round((used/total)*100, 1)
            info['ram'] = f"{used/1024/1024:.2f} GB / {total/1024/1024:.2f} GB ({info['ram_percent']:.1f}%)"
    except:
        info['ram'] = "Error"
        info['ram_percent'] = 0

    # Disk Space
    try:
        total, used, free = shutil.disk_usage("/")
        info['disk'] = f"{used/(1024**3):.2f} GB / {total/(1024**3):.2f} GB ({(used/total)*100:.1f}%)"
    except:
        info['disk'] = "Error fetching disk space"

    # Top Apps
    try:
        top = subprocess.check_output(['ps', '-eo', 'pid,user,%cpu,%mem,comm', '--sort=-%cpu'], text=True)
        info['top_apps'] = "\n".join(top.split('\n')[:10])
    except:
        info['top_apps'] = "Error fetching processes"

    # Network (Tx/Rx totals)
    try:
        with open('/proc/net/dev', 'r') as f:
            lines = f.readlines()
            for line in lines:
                if 'eth0:' in line or 'ens3:' in line:
                    parts = line.split()
                    info['net_rx'] = f"{int(parts[1])/1024/1024:.2f} MB"
                    info['net_tx'] = f"{int(parts[9])/1024/1024:.2f} MB"
                    info['net_rx_bytes'] = int(parts[1])
                    info['net_tx_bytes'] = int(parts[9])
                    break
    except:
        info['net_rx'] = "Error"
        info['net_tx'] = "Error"
        info['net_rx_bytes'] = 0
        info['net_tx_bytes'] = 0

    # Ping / Jitter / Packet Loss
    try:
        # Ping Google DNS (8.8.8.8) 5 times
        out = subprocess.check_output(['ping', '-c', '5', '-W', '1', '8.8.8.8'], text=True)
        stats = "Ping failed"
        avg_ping = 0.0
        jitter = 0.0
        packet_loss = 0.0
        
        for line in out.splitlines():
            if 'packets transmitted' in line:
                # 5 packets transmitted, 5 received, 0% packet loss, time 4005ms
                packet_loss = float(line.split(', ')[2].split('%')[0])
            if 'min/avg/max' in line:
                stats = line.strip()
                # rtt min/avg/max/mdev = 3.540/3.600/3.700/0.100 ms
                parts = line.split('=')[1].split('/')
                avg_ping = float(parts[1])
                jitter = float(parts[3].split(' ')[0])
                
        info['ping'] = f"Google DNS (8.8.8.8): {stats}"
        info['ping_avg'] = avg_ping
        info['jitter'] = jitter
        info['packet_loss'] = packet_loss
    except:
        info['ping'] = "Error running ping"
        info['ping_avg'] = 0.0
        info['jitter'] = 0.0
        info['packet_loss'] = 100.0

    # Security (Last Logins)
    try:
        sec = subprocess.check_output(['last', '-n', '5'], text=True)
        info['security'] = sec
    except Exception as e:
        try:
            info['security'] = "Recent active sessions:\n" + subprocess.check_output(['who'], text=True)
        except:
            info['security'] = "Cannot read logs without sudo."

    return info

def test_api(provider, model):
    start = time.time()
    try:
        with open(AUTH_PROFILES_PATH) as f:
            profiles = json.load(f)

        key = None
        for p in profiles.get('profiles', {}).values():
            if p.get('provider') == provider:
                key = p.get('key')
                break

        if not key:
            return {"status": "error", "message": f"No API key configured for provider '{provider}'"}

        if provider == 'anthropic':
            payload = json.dumps({
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Reply with just: OK"}]
            }).encode()
            req = urllib.request.Request(
                'https://api.anthropic.com/v1/messages',
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': key,
                    'anthropic-version': '2023-06-01'
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                reply = data.get('content', [{}])[0].get('text', '').strip()

        elif provider == 'google':
            payload = json.dumps({
                "contents": [{"parts": [{"text": "Reply with just: OK"}]}],
                "generationConfig": {"maxOutputTokens": 10}
            }).encode()
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
            req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                reply = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '').strip()

        elif provider == 'moonshot':
            payload = json.dumps({
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Reply with just: OK"}]
            }).encode()
            req = urllib.request.Request(
                'https://api.moonshot.ai/v1/chat/completions',
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {key}'
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                reply = data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()

        else:
            return {"status": "error", "message": f"Unknown provider: {provider}"}

        latency_ms = round((time.time() - start) * 1000)
        return {"status": "ok", "reply": reply, "latency_ms": latency_ms}

    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            err_data = json.loads(body)
            msg = (err_data.get('error', {}).get('message') or
                   err_data.get('error', {}).get('type') or
                   body[:400])
        except:
            msg = body[:400]
        return {"status": "error", "message": f"HTTP {e.code}: {msg}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_cron_jobs():
    jobs = []
    try:
        with open('/home/josue2es/.openclaw/cron/jobs.json', 'r') as f:
            data = json.load(f)
            tz = ZoneInfo("America/El_Salvador")
            for job in data.get('jobs', []):
                if job.get('enabled', False):
                    next_run = job['state'].get('nextRunAtMs', 0)
                    jobs.append({
                        'name': job['name'],
                        'desc': job['payload'].get('message', 'No description'),
                        'next': datetime.fromtimestamp(next_run/1000, tz=tz).strftime('%Y-%m-%d %H:%M:%S') if next_run else 'N/A'
                    })
    except Exception as e:
        jobs.append({'name': 'Error', 'desc': str(e), 'next': 'N/A'})
    return jobs

def get_memories():
    try:
        with open('/home/josue2es/.openclaw/workspace/memory/2026-04-02.md', 'r') as f:
            lines = [l.strip() for l in f.readlines() if l.strip().startswith('-')]
            return lines[-5:]
    except:
        return ["No recent memories."]

def get_issues():
    issues = {'active': [], 'fixed': []}
    try:
        with open('/home/josue2es/.openclaw/workspace/ISSUES.md', 'r') as f:
            lines = f.readlines()
            section = None
            for line in lines:
                if '## Active Issues' in line: section = 'active'
                elif '## Resolved Issues' in line: section = 'fixed'
                elif line.strip().startswith('-'):
                    if section:
                        issues[section].append(line.strip()[2:])
    except:
        pass
    return issues

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Foxy IT Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
    body { font-family: monospace; background-color: #1e1e1e; color: #00ff00; margin: 40px; }
    h1 { color: #ff9900; }
    .card { background-color: #2d2d2d; border: 1px solid #444; padding: 20px; margin-bottom: 20px; border-radius: 5px; box-shadow: 0 4px 8px rgba(0,0,0,0.5); }
    pre { margin: 0; overflow-x: auto; }
    .metric { font-size: 1.2em; font-weight: bold; color: #fff; }
    .refresh { font-size: 0.8em; color: #888; }
    .btn { background-color: #ff9900; color: #1e1e1e; border: none; padding: 10px 20px; font-weight: bold; cursor: pointer; border-radius: 3px; }
    .btn:hover { background-color: #ffaa33; }
    .btn:disabled { background-color: #666; cursor: not-allowed; }
    .chart-container { position: relative; height: 200px; width: 100%; }
</style>
<script>
    let lastCpuRaw = null;
    let resourceChart = null;
    let netChart = null;
    let pingChart = null;
    let chartLabels = [];
    let cpuData = [];
    let ramData = [];
    let rxData = [];
    let txData = [];
    let pingData = [];
    let jitterData = [];
    let lossData = [];
    let fullHistory = { labels: [], cpu: [], ram: [], rx: [], tx: [], ping: [], jitter: [], loss: [] };
    let lastRx = null;
    let lastTx = null;
    let lastNetTime = null;
    const MAX_DISPLAY_POINTS = 720; // 3 hours (15s * 720 = 10800s = 3h)
    const MAX_HISTORY_POINTS = 5760; // 24 hours (15s * 5760 = 86400s = 24h)

    function initChart() {
        const ctx = document.getElementById('resourceChart').getContext('2d');
        resourceChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: chartLabels,
                datasets: [
                    {
                        label: 'CPU Usage (%)',
                        borderColor: '#ff9900',
                        backgroundColor: 'rgba(255, 153, 0, 0.2)',
                        data: cpuData,
                        tension: 0.4,
                        fill: true
                    },
                    {
                        label: 'RAM Usage (%)',
                        borderColor: '#00ff00',
                        backgroundColor: 'rgba(0, 255, 0, 0.2)',
                        data: ramData,
                        tension: 0.4,
                        fill: true
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100,
                        grid: { color: '#444' },
                        ticks: { color: '#888' }
                    },
                    x: {
                        grid: { color: '#444' },
                        ticks: { color: '#888' }
                    }
                },
                plugins: {
                    legend: { labels: { color: '#fff' } }
                },
                animation: { duration: 0 }
            }
        });

        const netCtx = document.getElementById('netChart').getContext('2d');
        netChart = new Chart(netCtx, {
            type: 'line',
            data: {
                labels: chartLabels,
                datasets: [
                    {
                        label: 'Download',
                        borderColor: '#00ccff',
                        backgroundColor: 'rgba(0, 204, 255, 0.2)',
                        data: rxData,
                        tension: 0.4,
                        fill: true
                    },
                    {
                        label: 'Upload',
                        borderColor: '#ff3366',
                        backgroundColor: 'rgba(255, 51, 102, 0.2)',
                        data: txData,
                        tension: 0.4,
                        fill: true
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: '#444' },
                        ticks: { 
                            color: '#888',
                            callback: function(value) {
                                if (value >= 1048576) return (value / 1048576).toFixed(1) + ' MB/s';
                                if (value >= 1024) return (value / 1024).toFixed(1) + ' KB/s';
                                return value + ' B/s';
                            }
                        }
                    },
                    x: {
                        grid: { color: '#444' },
                        ticks: { color: '#888' }
                    }
                },
                plugins: {
                    legend: { labels: { color: '#fff' } },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                let value = context.parsed.y;
                                if (value >= 1048576) return context.dataset.label + ': ' + (value / 1048576).toFixed(2) + ' MB/s';
                                if (value >= 1024) return context.dataset.label + ': ' + (value / 1024).toFixed(2) + ' KB/s';
                                return context.dataset.label + ': ' + value + ' B/s';
                            }
                        }
                    }
                },
                animation: { duration: 0 }
            }
        });

        const pingCtx = document.getElementById('pingChart').getContext('2d');
        pingChart = new Chart(pingCtx, {
            type: 'line',
            data: {
                labels: chartLabels,
                datasets: [
                    {
                        label: 'Latency (ms)',
                        borderColor: '#ffff00',
                        backgroundColor: 'rgba(255, 255, 0, 0.2)',
                        data: pingData,
                        tension: 0.4,
                        fill: true
                    },
                    {
                        label: 'Jitter (ms)',
                        borderColor: '#00ff00',
                        backgroundColor: 'rgba(0, 255, 0, 0.2)',
                        data: jitterData,
                        tension: 0.4,
                        fill: true
                    },
                    {
                        label: 'Packet Loss (%)',
                        borderColor: '#ff3366',
                        backgroundColor: 'rgba(255, 51, 102, 0.2)',
                        data: lossData,
                        tension: 0.4,
                        fill: true
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: '#444' },
                        ticks: { color: '#888' }
                    },
                    x: {
                        grid: { color: '#444' },
                        ticks: { color: '#888' }
                    }
                },
                plugins: {
                    legend: { labels: { color: '#fff' } }
                },
                animation: { duration: 0 }
            }
        });
    }

    function update() {
        fetch('/api/data').then(r => r.json()).then(data => {
            document.getElementById('ram').innerText = data.ram;
            document.getElementById('disk').innerText = data.disk;
            document.getElementById('net').innerText = "RX: " + data.net_rx + " | TX: " + data.net_tx;
            document.getElementById('ping').innerText = data.ping;
            document.getElementById('top').innerText = data.top_apps;
            document.getElementById('sec').innerText = data.security;
            document.getElementById('ping').innerText = data.ping;


            const nowTime = Date.now();
            const now = new Date();
            const timeStr = now.toLocaleTimeString();
            document.getElementById('time').innerText = timeStr;

            const isFirstCall = lastCpuRaw === null || lastRx === null;

            // Calculate Throughput (Bytes/s)
            let rxBps = 0;
            let txBps = 0;
            if (data.net_rx_bytes !== undefined && lastRx !== null && lastNetTime) {
                const diffTimeSec = (nowTime - lastNetTime) / 1000;
                if (diffTimeSec > 0) {
                    rxBps = (data.net_rx_bytes - lastRx) / diffTimeSec;
                    txBps = (data.net_tx_bytes - lastTx) / diffTimeSec;
                }
            }
            if (data.net_rx_bytes !== undefined) {
                lastRx = data.net_rx_bytes;
                lastTx = data.net_tx_bytes;
                lastNetTime = nowTime;
            }

            // Calculate CPU % from raw stat ticks
            let cpuPercent = 0;
            if (data.cpu_raw && lastCpuRaw) {
                const idleDiff = data.cpu_raw.idle - lastCpuRaw.idle;
                const totalDiff = data.cpu_raw.total - lastCpuRaw.total;
                if (totalDiff > 0) {
                    cpuPercent = ((totalDiff - idleDiff) / totalDiff) * 100;
                }
            }
            if (data.cpu_raw) {
                lastCpuRaw = data.cpu_raw;
            }

            // Skip first call — just prime the baselines, don't push a 0 data point
            if (isFirstCall) {
                updateCharts();
                return;
            }

            // Store and update display
            fullHistory.labels.push(timeStr);
            fullHistory.cpu.push(Math.round(cpuPercent * 10) / 10);
            fullHistory.ram.push(data.ram_percent || 0);
            fullHistory.rx.push(Math.round(rxBps * 10) / 10);
            fullHistory.tx.push(Math.round(txBps * 10) / 10);
            fullHistory.ping.push(data.ping_avg || 0);
            fullHistory.jitter.push(data.jitter || 0);
            fullHistory.loss.push(data.packet_loss || 0);
            
            if (fullHistory.labels.length > MAX_HISTORY_POINTS) {
                fullHistory.labels.shift();
                fullHistory.cpu.shift();
                fullHistory.ram.shift();
                fullHistory.rx.shift();
                fullHistory.tx.shift();
                fullHistory.ping.shift();
                fullHistory.jitter.shift();
                fullHistory.loss.shift();
            }

            updateCharts();
        });
    }

    function updateCharts() {
            // Display last 3 hours (MAX_DISPLAY_POINTS)
            chartLabels = fullHistory.labels.slice(-MAX_DISPLAY_POINTS);
            cpuData = fullHistory.cpu.slice(-MAX_DISPLAY_POINTS);
            ramData = fullHistory.ram.slice(-MAX_DISPLAY_POINTS);
            rxData = fullHistory.rx.slice(-MAX_DISPLAY_POINTS);
            txData = fullHistory.tx.slice(-MAX_DISPLAY_POINTS);
            pingData = fullHistory.ping.slice(-MAX_DISPLAY_POINTS);
            jitterData = fullHistory.jitter.slice(-MAX_DISPLAY_POINTS);
            lossData = fullHistory.loss.slice(-MAX_DISPLAY_POINTS);

            if (resourceChart) {
                resourceChart.data.labels = chartLabels;
                resourceChart.data.datasets[0].data = cpuData;
                resourceChart.data.datasets[1].data = ramData;
                resourceChart.update();
            }
            if (netChart) {
                netChart.data.labels = chartLabels;
                netChart.data.datasets[0].data = rxData;
                netChart.data.datasets[1].data = txData;
                netChart.update();
            }
            if (pingChart) {
                pingChart.data.labels = chartLabels;
                pingChart.data.datasets[0].data = pingData;
                pingChart.data.datasets[1].data = jitterData;
                pingChart.data.datasets[2].data = lossData;
                pingChart.update();
            }
    }
    
    function runSpeedTest() {
        const btn = document.getElementById('speedBtn');
        const res = document.getElementById('speed_result');
        btn.disabled = true;
        res.innerText = "Running speed test... (this takes ~20 seconds)";
        
        fetch('/api/speedtest').then(r => r.json()).then(data => {
            if (data.status === 'rate_limited') {
                const mins = Math.ceil(data.wait / 60);
                res.innerHTML = "<span style='color:#ff5555'>Rate limited. Try again in " + mins + " minutes.</span><br><br><b>Last Result:</b><br>" + data.result;
            } else if (data.status === 'ok') {
                res.innerHTML = "<b>" + data.result + "</b>";
            } else {
                res.innerText = "Error: " + data.message;
            }
            btn.disabled = false;
        }).catch(err => {
            res.innerText = "Failed to run speed test.";
            btn.disabled = false;
        });
    }

    const MODEL_OPTIONS = {
        anthropic: ['claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'claude-opus-4-6'],
        google: ['gemini-3.1-pro-preview', 'gemini-3.1-flash-lite-preview'],
        moonshot: ['kimi-k2.5', 'moonshot-v1-8k', 'moonshot-v1-32k']
    };

    function onProviderChange() {
        const provider = document.getElementById('apiProvider').value;
        const modelSel = document.getElementById('apiModel');
        modelSel.innerHTML = MODEL_OPTIONS[provider].map(m => `<option value="${m}">${m}</option>`).join('');
    }

    function runApiTest() {
        const btn = document.getElementById('apiTestBtn');
        const res = document.getElementById('api_result');
        const provider = document.getElementById('apiProvider').value;
        const model = document.getElementById('apiModel').value;
        btn.disabled = true;
        res.style.color = '#aaa';
        res.innerText = `Testing ${provider} / ${model}...`;

        fetch('/api/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, model })
        }).then(r => r.json()).then(data => {
            if (data.status === 'ok') {
                res.style.color = '#00ff00';
                res.innerText = `OK \u2014 ${data.latency_ms}ms\nReply: "${data.reply}"`;
            } else if (data.status === 'rate_limited') {
                res.style.color = '#ff9900';
                res.innerText = data.message;
            } else {
                res.style.color = '#ff5555';
                res.innerText = `Error\n${data.message}`;
            }
            btn.disabled = false;
        }).catch(err => {
            res.style.color = '#ff5555';
            res.innerText = `Request failed: ${err}`;
            btn.disabled = false;
        });
    }

    function updateMeta() {
        fetch('/api/cron').then(r => r.json()).then(jobs => {
            const tbody = document.getElementById('cron-list');
            tbody.innerHTML = '';
            jobs.forEach(job => {
                const row = `<tr>
                    <td>${job.name}</td>
                    <td style="font-size:0.8em; color:#aaa;">${job.desc}</td>
                    <td>${job.next}</td>
                </tr>`;
                tbody.innerHTML += row;
            });
        });
        fetch('/api/meta').then(r => r.json()).then(data => {
            document.getElementById('mem-list').innerHTML = data.memories.map(m => `<li>${m}</li>`).join('');
            document.getElementById('act-list').innerHTML = data.issues.active.map(i => `<li>${i}</li>`).join('');
            document.getElementById('fix-list').innerHTML = data.issues.fixed.map(i => `<li>${i}</li>`).join('');
        });
    }

    setInterval(update, 15000);
    setInterval(updateMeta, 60000);
    window.onload = () => {
        initChart();
        fetch('/api/history').then(r => r.json()).then(h => {
            fullHistory = {
                labels: h.labels,
                cpu: h.cpu,
                ram: h.ram,
                rx: h.rx,
                tx: h.tx,
                ping: h.ping,
                jitter: h.jitter,
                loss: h.loss
            };
            update();
        }).catch(() => update());
        updateMeta();
    };
</script>
</head>
<body>
    <h1>Foxy Server Monitor v2</h1>
    <p class="refresh">Live updating every 15s. Last refresh: <span id="time"></span></p>

    <!-- 1. Live Resource Usage -->
    <div class="card">
        <h3>Live Resource Usage (CPU &amp; RAM)</h3>
        <div class="chart-container">
            <canvas id="resourceChart"></canvas>
        </div>
    </div>

    <!-- 2. Network Stats (ping/jitter/loss) -->
    <div class="card">
        <h3>Network Stats</h3>
        <div class="chart-container">
            <canvas id="pingChart"></canvas>
        </div>
        <div class="metric" id="ping" style="font-size: 0.9em; margin-top: 10px; color: #aaa;">Loading...</div>
    </div>

    <!-- 3. Network Throughput -->
    <div class="card">
        <h3>Network Throughput (Up/Down)</h3>
        <div class="chart-container">
            <canvas id="netChart"></canvas>
        </div>
        <div class="metric" id="net" style="font-size: 0.9em; margin-top: 10px; color: #aaa;">Loading...</div>
    </div>

    <!-- 4+5+6. API Test, Memory & Disk, Speed Test on same row -->
    <div style="display: flex; gap: 20px; align-items: flex-start;">
        <div class="card" style="flex: 1;">
            <h3>API Test</h3>
            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                <select id="apiProvider" onchange="onProviderChange()" style="background:#1e1e1e; color:#00ff00; border:1px solid #444; padding:8px; font-family:monospace;">
                    <option value="anthropic">Anthropic</option>
                    <option value="google">Google</option>
                    <option value="moonshot">Moonshot</option>
                </select>
                <select id="apiModel" style="background:#1e1e1e; color:#00ff00; border:1px solid #444; padding:8px; font-family:monospace; min-width:200px;">
                    <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
                </select>
                <button id="apiTestBtn" class="btn" onclick="runApiTest()">Test</button>
            </div>
            <div id="api_result" style="margin-top: 15px; color: #aaa; font-family: monospace; white-space: pre-wrap;"></div>
        </div>
        <div class="card" style="flex: 1;">
            <h3>Memory &amp; Disk</h3>
            <div style="margin-bottom: 10px;">
                <div style="color: #888; font-size: 0.85em;">RAM</div>
                <div class="metric" id="ram">Loading...</div>
            </div>
            <div>
                <div style="color: #888; font-size: 0.85em;">Disk (/)</div>
                <div class="metric" id="disk">Loading...</div>
            </div>
        </div>
        <div class="card" style="flex: 1;">
            <h3>Internet Speed Test (Max 1/hour)</h3>
            <button id="speedBtn" class="btn" onclick="runSpeedTest()">Run Speed Test</button>
            <p id="speed_result" style="margin-top: 15px; color: #aaa;">Not run yet.</p>
        </div>
    </div>

    <!-- 7. Top Processes / Security -->
    <div style="display: flex; gap: 20px;">
        <div class="card" style="flex: 1;">
            <h3>Top Processes (CPU)</h3>
            <pre id="top">Loading...</pre>
        </div>
        <div class="card" style="flex: 1;">
            <h3>Security &amp; Logins</h3>
            <pre id="sec">Loading...</pre>
        </div>
    </div>

    <!-- 9. Cron Jobs -->
    <div class="card">
        <h3>Active Cron Jobs</h3>
        <table style="width:100%; color: #fff;">
            <thead>
                <tr style="text-align:left;"><th>Job</th><th>Description</th><th>Next Trigger</th></tr>
            </thead>
            <tbody id="cron-list">
                <tr><td colspan="3">Loading...</td></tr>
            </tbody>
        </table>
    </div>

    <!-- 8. Memories -->
    <div class="card">
        <h3>Recent Memories (Last 5)</h3>
        <ul id="mem-list"></ul>
    </div>

    <!-- 9. Issues -->
    <div style="display: flex; gap: 20px;">
        <div class="card" style="flex: 1;">
            <h3>Current Issues</h3>
            <ul id="act-list"></ul>
        </div>
        <div class="card" style="flex: 1;">
            <h3>Issues Fixed</h3>
            <ul id="fix-list"></ul>
        </div>
    </div>
</body>
</html>
"""

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress access logs

    def do_GET(self):
        global last_speedtest_time, cached_speedtest_result
        
        if self.path == '/':
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
            
        elif self.path == '/api/data':
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(get_sys_info()).encode())

        elif self.path == '/api/meta':
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({'memories': get_memories(), 'issues': get_issues()}).encode())
            
        elif self.path == '/api/cron':
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(get_cron_jobs()).encode())
            
        elif self.path == '/api/history':
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute('SELECT timestamp, cpu, ram, rx, tx, ping, jitter, loss FROM metrics ORDER BY timestamp ASC').fetchall()
            conn.close()
            history = {
                'labels': [r[0] for r in rows],
                'cpu': [r[1] for r in rows],
                'ram': [r[2] for r in rows],
                'rx': [r[3] for r in rows],
                'tx': [r[4] for r in rows],
                'ping': [r[5] for r in rows],
                'jitter': [r[6] for r in rows],
                'loss': [r[7] for r in rows]
            }
            self.wfile.write(json.dumps(history).encode())

        elif self.path == '/api/speedtest':
            now = time.time()
            if now - last_speedtest_time < 3600 and last_speedtest_time != 0:
                # Rate limited
                resp = {
                    "status": "rate_limited", 
                    "wait": 3600 - (now - last_speedtest_time),
                    "result": cached_speedtest_result
                }
            else:
                try:
                    # ~/.local/bin/speedtest-ookla
                    result = subprocess.run(['/home/josue2es/.local/bin/speedtest-ookla', '--accept-license', '--accept-gdpr', '-f', 'json'], capture_output=True, text=True)
                    if result.returncode != 0:
                        raise Exception(result.stderr.strip() or f"Process exited with {result.returncode}")
                    out = result.stdout
                    # Extract just the JSON part, as Ookla CLI sometimes outputs text warnings before the JSON
                    json_str = out[out.find('{'):] if '{' in out else out
                    data = json.loads(json_str)
                    ping = data.get('ping', {}).get('latency', 0)
                    down_mbps = data.get('download', {}).get('bandwidth', 0) * 8 / 1000000
                    up_mbps = data.get('upload', {}).get('bandwidth', 0) * 8 / 1000000
                    server = data.get('server', {}).get('name', 'Unknown')
                    location = data.get('server', {}).get('location', 'Unknown')
                    
                    fmt_result = f"Ping: {ping:.2f} ms<br>Download: {down_mbps:.2f} Mbps<br>Upload: {up_mbps:.2f} Mbps<br>Server: {server} ({location})"
                    cached_speedtest_result = fmt_result
                    last_speedtest_time = now
                    
                    # Store in DB
                    try:
                        conn = sqlite3.connect(DB_PATH)
                        conn.execute('CREATE TABLE IF NOT EXISTS speedtests (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, download REAL, upload REAL, ping REAL)')
                        conn.execute('INSERT INTO speedtests (download, upload, ping) VALUES (?, ?, ?)', (down_mbps, up_mbps, ping))
                        conn.commit()
                        conn.close()
                    except Exception as e_db:
                        print("DB Save Error:", e_db)
                        
                    resp = {"status": "ok", "result": cached_speedtest_result}
                except Exception as e:
                    resp = {"status": "error", "message": f"Speedtest failed: {str(e)}"}
                    
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/api/test':
            global last_apitest_time
            now = time.time()
            wait = APITEST_COOLDOWN - (now - last_apitest_time)
            if wait > 0:
                resp = {"status": "rate_limited", "message": f"Please wait {int(wait)+1}s before testing again."}
            else:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                try:
                    req = json.loads(body)
                    provider = req.get('provider', '')
                    model = req.get('model', '')
                    if not provider or not model:
                        resp = {"status": "error", "message": "Missing provider or model"}
                    else:
                        last_apitest_time = now
                        resp = test_api(provider, model)
                except Exception as e:
                    resp = {"status": "error", "message": str(e)}
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
        else:
            self.send_error(404)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=background_worker, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as httpd:
        print(f"Serving at port {PORT}")
        httpd.serve_forever()
