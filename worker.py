#!/usr/bin/env python3
"""
yt-worker - YouTube AI/ML tracker with dynamic web frontend.

Usage:
    python3 worker.py --poll                # poll all channels once (fast mode: --no-summarize)
    python3 worker.py --serve [--port 5000] # start web frontend
    python3 worker.py --poll --serve        # poll + serve together
    python3 worker.py --daemon              # poll every N minutes + serve
"""

import argparse, json, logging, os, re, sqlite3, subprocess, sys, tempfile, time, threading
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

import yaml
from flask import Flask, jsonify, request

# ── paths ──────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent
CONFIG     = BASE_DIR / "config.yml"
DB_PATH    = BASE_DIR / "data" / "state.db"
OUT_DIR    = BASE_DIR / "out"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("yt-worker")

_VENV_YTDLP = BASE_DIR / ".venv" / "bin" / "yt-dlp"
YT_DLP = str(_VENV_YTDLP) if _VENV_YTDLP.is_file() else "yt-dlp"

# ── DB ─────────────────────────────────────────────────────────────────
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            video_id    TEXT PRIMARY KEY,
            channel_id  TEXT NOT NULL,
            channel     TEXT NOT NULL,
            title       TEXT,
            url         TEXT,
            published   TEXT,
            transcript  TEXT,
            summary     TEXT,
            keywords    TEXT,
            processed   TEXT NOT NULL DEFAULT (datetime('now')),
            skipped     INTEGER NOT NULL DEFAULT 0,
            skip_reason TEXT
        )
    """)
    conn.commit()
    return conn

# ── config ─────────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG) as f:
        return yaml.safe_load(f)

# ── yt-dlp helpers ─────────────────────────────────────────────────────
def fetch_channel_videos(channel_id: str, max_videos=30) -> list[dict]:
    cmd = [YT_DLP, "--flat-playlist", "--dump-json",
           "--playlist-end", str(max_videos), "--no-warnings",
           f"https://www.youtube.com/channel/{channel_id}/videos"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    videos = []
    for line in r.stdout.strip().splitlines():
        try:
            videos.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return videos

def fetch_transcript(video_url: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [YT_DLP, "--write-subs", "--write-auto-subs", "--skip-download",
               "--sub-lang", "en", "--output", "subs", "--no-warnings", video_url]
        try:
            subprocess.run(cmd, cwd=tmp, check=True, capture_output=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        vtts = list(Path(tmp).glob("*.vtt"))
        if not vtts:
            return None
        return clean_vtt(vtts[0].read_text(encoding="utf-8"))

def clean_vtt(content: str) -> str:
    lines, out, ts_re = content.splitlines(), [], re.compile(
        r"\d{2}:\d{2}:\d{2}\.\d{3}\s-->\s\d{2}:\d{2}:\d{2}\.\d{3}")
    for line in lines:
        line = line.strip()
        if not line or line == "WEBVTT" or line.isdigit() or ts_re.match(line):
            continue
        if line.startswith(("NOTE", "STYLE")):
            continue
        if out and out[-1] == line:
            continue
        out.append(re.sub(r"<[^>]+>", "", line))
    return "\n".join(out)

# ── relevance ──────────────────────────────────────────────────────────
def matches_keywords(text: str, keywords: list[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(re.search(rf"\b{re.escape(kw.lower())}", t) for kw in keywords)

def is_relevant(video: dict, keywords: list[str]) -> bool:
    return matches_keywords(
        f"{video.get('title','')} {video.get('description','')}", keywords)

# ── summarization ──────────────────────────────────────────────────────
def summarize(transcript: str, title: str, url: str, channel: str, cfg: dict) -> str | None:
    oc = cfg.get("openclaw", {})
    prompt = oc.get("summarize_prompt",
        "Summarize this YouTube transcript. Include main topic, key takeaways, "
        "technical details, and notable claims. Keep under 300 words.\n\n"
        f"Title: {title}\nChannel: {channel}\nURL: {url}\n\nTranscript:\n{{transcript}}")
    prompt = prompt.format(title=title, channel=channel, url=url, transcript=transcript)
    max_chars = oc.get("max_prompt_chars", 24000)
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars] + "\n\n[truncated]"

    timeout = oc.get("timeout_seconds", 180)
    cmd = ["openclaw", "agent", "--session-id", "yt-worker", "--json",
           "--timeout", str(timeout), "--message", prompt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
        payloads = data.get("result", {}).get("payloads", [])
        if payloads:
            return payloads[0].get("text", "").strip()
    except json.JSONDecodeError:
        return r.stdout.strip()

# ── poll logic ─────────────────────────────────────────────────────────
def _trim_if_needed(conn, max_keep: int):
    """Delete oldest videos if count exceeds max_keep, keeping non-skipped first."""
    current = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    if current > max_keep:
        conn.execute(
            "DELETE FROM videos WHERE video_id NOT IN (SELECT video_id FROM videos ORDER BY skipped ASC, processed DESC LIMIT ?)",
            (max_keep,))
        conn.commit()

def poll_channels(config: dict, no_summarize=False):
    channels = config.get("channels", [])
    keywords = config.get("relevance_keywords", [])
    max_new  = config.get("max_new_per_run", 20)
    ch_map   = {c["channel_id"]: c["name"] for c in channels}

    conn = get_db()
    # Cleanup first: keep only max_keep before adding new ones
    max_keep = config.get("max_keep", 20)
    conn.execute(
        "DELETE FROM videos WHERE video_id NOT IN (SELECT video_id FROM videos ORDER BY skipped ASC, processed DESC LIMIT ?)",
        (max_keep,))
    conn.commit()

    total = 0
    total_inserts = 0  # count ALL inserts (including skipped)
    for ch in channels:
        cid   = ch["channel_id"]
        cname = ch.get("name", cid)
        log.info("Polling: %s", cname)
        videos = fetch_channel_videos(cid, max_videos=max_new * 2)
        processed = 0
        for v in videos:
            vid = v.get("id")
            if not vid:
                continue
            if conn.execute("SELECT 1 FROM videos WHERE video_id=?", (vid,)).fetchone():
                continue
            # Hard stop: don't exceed max_new
            if processed >= max_new:
                break
            title = v.get("title", "Untitled")
            url   = f"https://www.youtube.com/watch?v={vid}"
            pub   = v.get("upload_date") or v.get("timestamp") or ""

            if not is_relevant(v, keywords):
                conn.execute(
                    "INSERT OR IGNORE INTO videos(video_id,channel_id,channel,title,url,published,skipped,skip_reason) VALUES(?,?,?,?,?,?,1,?)",
                    (vid, cid, cname, title, url, pub, "irrelevant"))
                conn.commit()
                total_inserts += 1
                _trim_if_needed(conn, max_keep)
                continue

            # Stop if we've already found enough relevant videos
            processed += 1
            log.info("  🆕 %s", title[:80])
            transcript = fetch_transcript(url)
            if not transcript:
                conn.execute(
                    "INSERT OR IGNORE INTO videos(video_id,channel_id,channel,title,url,published,skipped,skip_reason) VALUES(?,?,?,?,?,?,1,?)",
                    (vid, cid, cname, title, url, pub, "no_transcript"))
                conn.commit()
                _trim_if_needed(conn, max_keep)
                continue

            summary = None
            if not no_summarize:
                summary = summarize(transcript, title, url, cname, config)

            conn.execute(
                "INSERT OR IGNORE INTO videos(video_id,channel_id,channel,title,url,published,transcript,summary,skipped) VALUES(?,?,?,?,?,?,?,?,0)",
                (vid, cid, cname, title, url, pub, transcript, summary))
            conn.commit()
            _trim_if_needed(conn, max_keep)
            processed += 1
            if processed >= max_new:
                break

        total += processed
        log.info("  → %d new (%s)", processed, cname)

    # Keep only the most recent max_keep (already defined above)
    conn.execute(
        "DELETE FROM videos WHERE video_id NOT IN (SELECT video_id FROM videos ORDER BY skipped ASC, processed DESC LIMIT ?)",
        (max_keep,))
    conn.commit()
    conn.close()
    log.info("Poll done - %d new videos total (keeping %d)", total, max_keep)

# ── category tagging ───────────────────────────────────────────────────
CATEGORIES = [
    ("LLM Architectures",      "🏗️", [r"transformer", r"attention", r"tokenformer", r"byte latent", r"free transformer"]),
    ("LLM Behavior & Safety",  "🔬", [r"biology of", r"context rot", r"hallucination", r"safety", r"alignment", r"interpretab"]),
    ("LLM Training & Scaling", "📈", [r"scaling", r"test.time compute", r"model parameters", r"pre.training", r"fine.tuning", r"LoRA", r"distill"]),
    ("RL & Search",            "🎮", [r"reinforcement", r"GRPO", r"planning", r"search", r"A\*"]),
    ("AI Agents",              "🤖", [r"agent", r"tool call", r"tool use", r"MCP", r"multi.agent", r"handoff"]),
    ("AI News & Industry",     "📰", [r"ML News", r"open.source", r"GPT.4", r"GPT.5", r"DeepSeek", r"Grok", r"Gemini", r"NVIDIA", r"GTC"]),
    ("AI Apps & Demos",        "🖼️", [r"image", r"video generat", r"diffusion", r"text.to", r"voice", r"game", r"self.driving"]),
    ("DL Fundamentals",        "📐", [r"neural network", r"backprop", r"deep learning", r"intro to", r"spelled.out"]),
    ("AI Tools & Platforms",   "🛠️", [r"LangChain", r"LangSmith", r"Mistral", r"Llama", r"Observab", r"guardrail", r"RAG", r"Copilot"]),
    ("AI/ML",                  "🤔", []),
]

def categorize(title: str):
    t = title.lower()
    for label, emoji, patterns in CATEGORIES:
        for p in patterns:
            if re.search(p, t):
                return emoji, label
    return "🤔", "AI/ML"

def extract_speaker(raw_summary: str) -> str:
    """Extract speaker name from JSON-formatted summaries."""
    if not raw_summary:
        return ""
    try:
        s = raw_summary.strip()
        if s.startswith('```'):
            s = re.sub(r'^```\w*\n?', '', s)
            s = re.sub(r'\n?```$', '', s)
        data = json.loads(s)
        if isinstance(data, dict) and 'speaker' in data:
            name = data['speaker'][:80]
            # Clean parenthetical channel names like "Name (Channel)"
            name = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
            return name
    except (json.JSONDecodeError, TypeError):
        pass
    return ""

def extract_related(raw_summary: str) -> str:
    """Extract related field from JSON summaries."""
    if not raw_summary:
        return ""
    try:
        s = raw_summary.strip()
        if s.startswith('```'):
            s = re.sub(r'^```\w*\n?', '', s)
            s = re.sub(r'\n?```$', '', s)
        data = json.loads(s)
        if isinstance(data, dict) and 'related' in data:
            return str(data['related'])[:100]
    except (json.JSONDecodeError, TypeError):
        pass
    return ""

def extract_summary_text(raw_summary: str) -> str:
    """Extract readable summary text from JSON-formatted summaries."""
    if not raw_summary:
        return ""
    # Try to parse as JSON, extract 'summary' field
    try:
        # Handle markdown code-fenced JSON
        s = raw_summary.strip()
        if s.startswith('```'):
            s = re.sub(r'^```\w*\n?', '', s)
            s = re.sub(r'\n?```$', '', s)
        data = json.loads(s)
        if isinstance(data, dict) and 'summary' in data:
            return data['summary'][:500]
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: clean and truncate raw text
    clean = re.sub(r'##\s*\S+.*', '', raw_summary)
    clean = re.sub(r'```[\s\S]*?```', '', clean)
    clean = re.sub(r'\{[\s\S]*?\}', '', clean)
    return clean.strip()[:500] if clean.strip() else raw_summary[:500]

def extract_topics(raw_summary: str) -> list[str]:
    """Extract topics array from JSON-formatted summaries."""
    if not raw_summary:
        return []
    try:
        s = raw_summary.strip()
        if s.startswith('```'):
            s = re.sub(r'^```\w*\n?', '', s)
            s = re.sub(r'\n?```$', '', s)
        data = json.loads(s)
        if isinstance(data, dict):
            topics = data.get('keywords') or data.get('topics') or data.get('topic') or []
            if isinstance(topics, list):
                return topics
    except (json.JSONDecodeError, TypeError):
        pass
    return []

# ── Flask app ──────────────────────────────────────────────────────────
app = Flask(__name__)

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YT Tracker - AI/ML Index</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:#f0f4f8;color:#1a1a2e;padding:24px;font-size:18px}
h1{font-size:1.4rem;color:#0d47a1;margin-bottom:2px}
.sub{color:#1a1a2e;font-size:.88rem;margin-bottom:20px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.stat{background:#fff;border-radius:10px;padding:12px 18px;min-width:100px;box-shadow:0 1px 3px rgba(0,0,0,.06);text-align:center}
.stat .n{font-size:1.7rem;font-weight:800;color:#1e88e5}
.stat .l{font-size:.68rem;color:#78909c;text-transform:uppercase;letter-spacing:.5px}
.section{background:#fff;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.section h2{font-size:1.1rem;color:#1565c0;margin-bottom:12px}
.filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.filters select,.filters input{background:#fff;border:1.5px solid #cfd8dc;border-radius:8px;padding:6px 12px;font-size:.82rem}
.filters select:focus,.filters input:focus{outline:none;border-color:#1e88e5}
.filters input{width:240px}
table{width:100%;border-collapse:collapse;font-size:.95rem}
th{text-align:left;padding:10px 12px;border-bottom:2px solid #1e88e5;color:#1565c0;font-weight:700;font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:0;background:#fff;z-index:1}
td{padding:8px 12px;border-bottom:1px solid #eceff1}
tr:hover{background:#e3f2fd}
tr:nth-child(even){background:#fafbfc}
tr:nth-child(even):hover{background:#e3f2fd}
.num{width:36px;color:#90a4ae;text-align:right;font-size:.72rem}
.cat{width:150px;white-space:nowrap;font-size:.78rem}
.ch{width:140px;white-space:nowrap;font-weight:600;color:#37474f}
.title a{color:#1565c0;text-decoration:none;font-weight:500}
.title a:hover{text-decoration:underline;color:#0d47a1}
.kp{max-width:440px;font-size:.78rem;color:#37474f;line-height:1.45}
.no-kp{color:#b0bec5;font-style:italic;font-size:.78rem}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.68rem;font-weight:600}
.badge-ok{background:#e8f5e9;color:#2e7d32}
.badge-tx{background:#e3f2fd;color:#1565c0}
.badge-sk{background:#fafafa;color:#9e9e9e}
.hidden{display:none}
.topic-chip{display:inline-block;padding:3px 10px;margin:1px 3px 1px 0;border-radius:8px;font-size:.78rem;font-weight:600;white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis}
.summary-cell{max-width:550px;font-size:.9rem;color:#37474f;line-height:1.55}
.summary-preview{cursor:pointer;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.summary-full{display:none;margin-top:4px}
.summary-full.open{display:block}
.toggle-btn{color:#1e88e5;font-size:.7rem;cursor:pointer;user-select:none;margin-left:2px}
.toggle-btn:hover{text-decoration:underline}
#count{color:#78909c;font-size:.78rem;margin-left:8px}
.matrix-wrap{overflow-x:auto}
.matrix{font-size:.78rem}
.matrix .chn{font-weight:600;color:#1a1a2e;white-space:nowrap}
.matrix .tot{text-align:center;font-weight:700;color:#1e88e5}
.rh{height:100px;white-space:nowrap;vertical-align:bottom}
.rh div{transform:rotate(-45deg);width:20px}
.rh div span{padding:2px 5px;font-size:.62rem}
footer{margin-top:32px;padding-top:14px;border-top:1px solid #cfd8dc;color:#90a4ae;font-size:.72rem}
.loading{text-align:center;padding:40px;color:#90a4ae}
.view-link{font-size:.7rem;margin-left:4px}
.view-link a{color:#90a4ae}
.rel-table{width:100%;border-collapse:collapse;font-size:.82rem}
.rel-table td,.rel-table th{padding:8px 12px;border:1px solid #e0e0e0;text-align:center}
.rel-table th{background:#f5f7fa;font-weight:600;color:#37474f;font-size:.75rem}
.rel-table .ch-name{text-align:left;font-weight:600;white-space:nowrap}
.rel-badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:.7rem;font-weight:600}
.rel-similar{background:#e8f5e9;color:#2e7d32}
.rel-focus{background:#e3f2fd;color:#1565c0}
.rel-complementary{background:#fff3e0;color:#e65100}
.rel-unique{background:#fce4ec;color:#c62828}
</style>
</head>
<body>
<h1>📺 YouTube Tracker - What AI Creators Say About LLMs</h1>
<p class="sub" id="last-update">Loading...</p>

<div id="stats" class="stats loading">Loading...</div>

<div id="relation-section" class="section loading">Loading relations...</div>

<div class="section">
<h2>🎬 Video Index</h2>
<div class="filters">
    <select id="f-ch" onchange="render()"><option value="">📡 All</option></select>
    <select id="f-st" onchange="render()"><option value="">⚡ All</option><option value="summarized">✅ Summarized</option><option value="transcript">📄 Transcript</option><option value="skipped">⏭ Skipped</option></select>
    <input id="f-q" placeholder="🔍 Search..." oninput="render()">
    <span id="count"></span>
</div>
<table>
<thead><tr><th>#</th><th>Speaker</th><th>Creator</th><th>Video</th><th>Topics</th><th>Summary</th></tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>

<footer>yt-worker · OpenClaw</footer>

<script>
let all = [];
let cats = [];
let chans = [];

async function load(){
    const r = await fetch('/api/videos');
    all = await r.json();
    cats = [...new Set(all.map(v=>v.category))].sort();
    chans = [...new Set(all.map(v=>v.channel))].sort();

    // stats
    const ok = all.filter(v=>!v.skipped).length;
    const sum = all.filter(v=>v.summary).length;
    const sk = all.filter(v=>v.skipped).length;
    document.getElementById('stats').innerHTML =
        `<div class="stat"><div class="n">${ok}</div><div class="l">Transcripted</div></div>
         <div class="stat"><div class="n">${sum}</div><div class="l">Summarized</div></div>
         <div class="stat"><div class="n">${sk}</div><div class="l">Skipped</div></div>
         <div class="stat"><div class="n">${all.length}</div><div class="l">Total</div></div>`;

    // channel dropdown
    const sch = document.getElementById('f-ch');
    chans.forEach(c=>{ const o=document.createElement('option'); o.value=c; o.textContent=c; sch.appendChild(o); });

    // relations
    buildRelations();

    // last update
    const now = new Date();
    document.getElementById('last-update').textContent =
        'Data from DB · last updated: ' + now.toLocaleString('en-GB', {timeZone:'Asia/Hong_Kong'});

    render();
}

function toggleSummary(el){
    const cell = el.closest('.summary-cell');
    const preview = cell.querySelector('.summary-preview');
    const full = cell.querySelector('.summary-full');
    const btn = cell.querySelector('.toggle-btn');
    if(full.classList.contains('open')){
        full.classList.remove('open');
        preview.style.display = '';
        btn.textContent = 'more';
    } else {
        full.classList.add('open');
        preview.style.display = 'none';
        btn.textContent = 'less';
    }
}

function buildRelations(){
    // Show per-channel relationship descriptions
    const chans = [...new Set(all.map(v=>v.channel))].sort();
    const seen = {};
    let items = '';
    all.filter(v=>v.related&&!v.skipped).forEach(v=>{
        if(!seen[v.channel] && v.related.length>10){
            seen[v.channel] = true;
            items += `<div style="margin-bottom:12px;font-size:.95rem;line-height:1.55"><strong style="color:#1565c0">${v.channel}</strong>&nbsp;&nbsp;<span style="color:#1a1a2e">${v.related}</span></div>`;
        }
    });
    if(!items){
        document.getElementById('relation-section').innerHTML='';
        return;
    }
    document.getElementById('relation-section').innerHTML =
        `<h2 style="text-align:left">Channel Relations</h2>
         <p style="color:#1a1a2e;font-size:.85rem;margin-bottom:12px;text-align:left">How each channel relates to others on LLM themes</p>
         <div style="text-align:left">${items}</div>`;
}

function render(){
    const fCh  = document.getElementById('f-ch').value;
    const fSt  = document.getElementById('f-st').value;
    const fQ   = document.getElementById('f-q').value.toLowerCase();
    const tbody = document.getElementById('tbody');
    let html = '';
    let n = 0;
    all.forEach((v,i)=>{
        if(fCh  && v.channel!==fCh)   return;
        if(fSt==='summarized' && !v.summary)  return;
        if(fSt==='transcript' && (v.skipped||v.summary)) return;
        if(fSt==='skipped'    && !v.skipped) return;
        if(fQ && !v.title.toLowerCase().includes(fQ) && !(v.summary_text||'').toLowerCase().includes(fQ)) return;
        n++;
        const summary_text = v.summary_text
            ? `<div class="summary-cell"><div class="summary-preview" onclick="toggleSummary(this)">${v.summary_text.slice(0,150)}${v.summary_text.length>150?'...':''}</div><div class="summary-full">${v.summary_text}</div>${v.summary_text.length>150?'<span class="toggle-btn" onclick="toggleSummary(this.parentElement.querySelector(\'.summary-preview\'))">more</span>':''}</div>`
            : '<span class="no-kp">-</span>';
        const topicChips = v.topics && v.topics.length
            ? v.topics.map((t,i)=>`<span class="topic-chip" style="background:${['#e3f2fd','#e8f5e9','#fff3e0','#fce4ec','#f3e5f5','#e0f7fa'][i%6]};color:${['#1565c0','#2e7d32','#e65100','#c62828','#6a1b9a','#00838f'][i%6]}" title="${t.replace(/"/g,'&quot;')}">${t.length>25?t.slice(0,22)+'...':t}</span>`).join('')
            : '<span class="no-kp">-</span>';
        html += `<tr><td class="num">${i+1}</td><td class="cat">${v.speaker||'-'}</td><td class="ch">${v.channel}</td><td class="title"><a href="${v.url}" target="_blank">${v.title}</a><span class="view-link"><a href="/video/${v.video_id}">📄</a></span></td><td class="topic-col">${topicChips}</td><td class="kp-col">${summary_text}</td></tr>`;
    });
    tbody.innerHTML = html || '<tr><td colspan="6" style="text-align:center;padding:24px;color:#90a4ae">No videos match</td></tr>';
    document.getElementById('count').textContent = n + ' / ' + all.length;
}

load();
</script>
</body>
</html>"""

VIDEO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;max-width:900px;margin:0 auto;padding:24px;background:#fff;color:#1a1a2e}
h1{color:#0d47a1;font-size:1.4rem}
h2{color:#1565c0;font-size:1rem;margin-top:24px;border-bottom:1.5px solid #e0e0e0;padding-bottom:4px}
a{color:#1565c0}
.meta{color:#546e7a;font-size:.82rem;margin-bottom:18px}
.meta span{margin-right:14px}
.summary-box{background:#f5f8fc;border:1px solid #e0e0e0;border-radius:8px;padding:16px;line-height:1.65;white-space:pre-wrap}
.transcript-box{line-height:1.65;white-space:pre-wrap;font-size:.88rem;color:#455a64}
.back{margin-bottom:14px;font-size:.82rem}
</style>
</head>
<body>
<div class="back"><a href="/">← Back to index</a></div>
<h1>{{ title }}</h1>
<div class="meta">
    <span>📡 {{ channel }}</span>
    <span>🎬 <a href="{{ url }}" target="_blank">Watch on YouTube</a></span>
</div>
{% if summary %}
<h2>Summary</h2>
<div class="summary-box">{{ summary }}</div>
{% endif %}
{% if transcript %}
<h2>Transcript</h2>
<div class="transcript-box">{{ transcript }}</div>
{% endif %}
</body>
</html>"""

@app.route("/")
def index():
    return INDEX_HTML

@app.route("/video/<video_id>")
def video_page(video_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
    conn.close()
    if not row:
        return "Not found", 404
    from flask import render_template_string
    return render_template_string(VIDEO_HTML,
        title=row["title"] or "Untitled",
        channel=row["channel"] or row["channel_id"],
        url=row["url"] or f"https://www.youtube.com/watch?v={video_id}",
        summary=row["summary"] or "",
        transcript=row["transcript"] or "")

@app.route("/api/videos")
def api_videos():
    conn = get_db()
    rows = conn.execute("SELECT * FROM videos ORDER BY channel, published DESC").fetchall()
    conn.close()
    results = []
    for r in rows:
        emoji, cat = categorize(r["title"] or "")
        kp = extract_summary_text(r["summary"] or "")
        topics = extract_topics(r["summary"] or "")
        speaker = extract_speaker(r["summary"] or "")
        related = extract_related(r["summary"] or "")
        results.append({
            "video_id": r["video_id"], "channel": r["channel"], "title": r["title"],
            "url": r["url"], "published": r["published"],
            "summary": r["summary"], "transcript": r["transcript"],
            "skipped": bool(r["skipped"]), "skip_reason": r["skip_reason"] or "",
            "category": cat, "emoji": emoji, "key_point": kp, "summary_text": kp,
            "topics": topics, "speaker": speaker, "related": related,
        })
    return jsonify(results)

@app.route("/api/stats")
def api_stats():
    conn = get_db()
    total = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    processed = conn.execute("SELECT count(*) FROM videos WHERE skipped=0").fetchone()[0]
    summarized = conn.execute("SELECT count(*) FROM videos WHERE summary IS NOT NULL AND summary != ''").fetchone()[0]
    by_channel = [dict(r) for r in conn.execute(
        "SELECT channel, count(*) as cnt, sum(skipped) as sk FROM videos GROUP BY channel ORDER BY cnt DESC")]
    conn.close()
    return jsonify(total=total, processed=processed, summarized=summarized, by_channel=by_channel)

# ── CLI ────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="yt-worker")
    p.add_argument("--poll", action="store_true", help="Poll channels once")
    p.add_argument("--serve", action="store_true", help="Start web frontend")
    p.add_argument("--daemon", action="store_true", help="Poll on interval + serve")
    p.add_argument("--no-summarize", action="store_true", help="Skip summaries (fast)")
    p.add_argument("--port", type=int, default=5000, help="Web port (default 5000)")
    args = p.parse_args()

    config = load_config()

    if args.daemon:
        interval = config.get("poll_interval_minutes", 30)
        def poll_loop():
            while True:
                t0 = time.time()
                try:
                    poll_channels(config, no_summarize=args.no_summarize)
                except Exception as e:
                    log.exception("Poll error: %s", e)
                time.sleep(max(0, interval * 60 - (time.time() - t0)))
        threading.Thread(target=poll_loop, daemon=True).start()
        log.info("Daemon + web on :%d (poll every %d min)", args.port, interval)
        app.run(host="0.0.0.0", port=args.port, debug=False)
    elif args.poll and args.serve:
        poll_channels(config, no_summarize=args.no_summarize)
        log.info("Web on :%d", args.port)
        app.run(host="0.0.0.0", port=args.port, debug=False)
    elif args.poll:
        poll_channels(config, no_summarize=args.no_summarize)
    elif args.serve:
        log.info("Web on :%d", args.port)
        app.run(host="0.0.0.0", port=args.port, debug=False)
    else:
        poll_channels(config, no_summarize=args.no_summarize)

if __name__ == "__main__":
    main()
