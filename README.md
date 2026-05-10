# yt-worker

Polls 11 AI/ML YouTube channels for new videos, downloads transcripts, generates structured summaries via OpenClaw, and serves a web dashboard. Always keeps the 20 most recent videos.

## Quick Start

```bash
cd ~/projects/yt-worker

# Install dependencies (first run)
./run.sh

# Single poll + summarize
./run.sh --poll

# Start web UI only
./run.sh --serve --port 5000

# Daemon mode — poll every 30 min + web UI
./run.sh --daemon --port 5000

# Fast mode — skip summarization
./run.sh --poll --no-summarize
```

## Web Interface

```
http://localhost:5000
```

### Sections

| Section | Description |
|---------|-------------|
| **Stats** | Transcripted / Summarized / Total counts |
| **Channel Relations** | How each channel relates to others on LLM themes |
| **Video Index** | # / Speaker / Creator / Video / Topics / Summary (foldable) |

### Filters

- **Channel** — filter by creator
- **Status** — Summarized / Transcript / Skipped
- **Search** — keyword search across titles and summaries

## Configuration

Edit `config.yml`:

| Key | Default | Description |
|-----|---------|-------------|
| `poll_interval_minutes` | 30 | Polling interval |
| `max_new_per_run` | 10 | Max new videos per poll |
| `max_keep` | 20 | Max videos in database |
| `channels` | 11 channels | Add or remove YouTube channels |
| `relevance_keywords` | 10 keywords | Relevance filter |
| `openclaw.timeout_seconds` | 180 | Summary generation timeout |
| `openclaw.summarize_prompt` | — | Prompt template for summarization |

### Adding a Channel

```yaml
channels:
  - name: "Channel Name"
    channel_id: "UCxxxxxxxxxxxxxxxxxxxxxx"
```

Find channel IDs at `https://www.youtube.com/channel/<channel_id>`.

## Database

SQLite at `data/state.db`. Table `videos`:

| Column | Type | Description |
|--------|------|-------------|
| `video_id` | TEXT PK | YouTube video ID |
| `channel_id` | TEXT | YouTube channel ID |
| `channel` | TEXT | Channel name |
| `title` | TEXT | Video title |
| `url` | TEXT | YouTube URL |
| `published` | TEXT | Publish date |
| `transcript` | TEXT | Cleaned VTT transcript |
| `summary` | TEXT | JSON summary from OpenClaw |
| `skipped` | INTEGER | 1 if skipped |
| `skip_reason` | TEXT | "irrelevant" or "no_transcript" |

Auto-trim: after any insert, if total exceeds `max_keep`, the oldest non-summarized entries are deleted immediately.

## Summary Format

Each summary is a JSON object:

```json
{
  "speaker": "Name of main speaker",
  "keywords": ["keyword1", "keyword2", "keyword3"],
  "related": "How this channel relates to others on LLM themes...",
  "summary": "Concise summary under 300 words..."
}
```

## Maintenance

```bash
# View logs
tail -f /tmp/yt-worker.log

# Stop daemon
pkill -f 'worker.py'

# Reset and re-fetch
rm data/state.db && ./run.sh --poll
```

## Files

```
.
├── worker.py          # Main application (poll + Flask web)
├── config.yml         # Configuration
├── run.sh             # Launcher (creates venv if needed)
├── requirements.txt   # Python dependencies
├── Dockerfile         # Container build
└── data/
    └── state.db       # SQLite database
```
## Image

webpage webpage.png
terminal log:terminal log.jpg
