YouTube AI/LLM Intelligence Monitoring

                ┌────────────────────┐
                │   Dashboard Web    │
                │  React / Next.js   │
                └─────────┬──────────┘
                          │
                REST / WebSocket API
                          │
┌──────────────────────────────────────────────────┐
│                  Backend API                     │
│          FastAPI / NestJS / Express              │
└───────┬──────────────┬───────────────┬──────────┘
        │              │               │
        │              │               │
        ▼              ▼               ▼

┌────────────┐  ┌──────────────┐  ┌──────────────┐
│ YouTube API│  │ OpenClaw AI  │  │ Scheduler    │
│ Skill Layer│  │ Agent Layer  │  │ Cron / Queue │
└─────┬──────┘  └──────┬───────┘  └──────┬───────┘
      │                │                 │
      ▼                ▼                 ▼

      ┌────────────────────────────────┐
      │       Data Storage Layer       │
      │ PostgreSQL + Redis + S3        │
      └────────────────────────────────┘

YouTube API
    ↓
OpenClaw Skill
    ↓
Transcript + Metadata Extractor
    ↓
Topic Classifier
    ↓
Database
    ↓
Dashboard Web UI
