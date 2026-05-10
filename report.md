LLM YouTube landscape tracker system
1. Problem Statement
In today’s fast-paced information landscape, YouTube videos are updated constantly, making it challenging for users to stay informed about the latest developments in their areas of interest. This creates several interrelated problems:
·Efficient retrieval of relevant resources.
·Filtering the most relevant content without watching every video.
·Organizing collected material from multiple perspectives for meaningful categorization.
 To address these challenges, this project aims to build a fully automated system that continuously collects, summarizes, and categorizes video content, thereby removing barriers to timely information access and significantly improving the efficiency with which users acquire up-to-date knowledge.

3. Methodology
2.1 System Architecture
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ YouTube  │───▶│ yt-dlp   │───▶│ Keyword  │───▶│ yt-dlp   │───▶│ OpenClaw │
│ RSS/API  │    │ Metadata │    │ Filter   │    │ Subtitles│    │ Summaries│
└──────────┘    └──────────┘    └────┬─────┘    └──────────┘    └────┬─────┘
                                     │ Irrelevant                    │
                                     ▼                               ▼
                                  ┌──────┐                      ┌──────────┐
                                  │ Skip │                      │ SQLite   │
                                  └──────┘                      │ + Flask  │
                                                                │ Dashboard│
                                                                └──────────┘
The system is implemented as a single Python script (worker.py, ~900 lines), containing four core modules: data collection, relevance filtering, summary generation, and web presentation.

2.2 Data Collection
·Channel polling: Retrieve metadata via yt-dlp --flat-playlist --dump-json, up to 30 videos per channel, process 10 new per poll.
·Subtitle extraction: Download English captions for filtered videos; clean VTT files (remove headers, timestamps, HTML tags, merge duplicates).
·Deduplication: Unique video_id ensures no duplicates in database (INSERT OR IGNORE).

2.3 Relevance Filtering
Keyword matching is applied to video titles and descriptions using 10 configurable LLM/AI keywords:
| Keyword                                         | Type              |
| ----------------------------------------------- | ----------------- |
| LLM, GPT, transformer, large language model     | Core technology   |
| ChatGPT, OpenAI                                 | Product / company |
| machine learning, deep learning, neural network | Fundamental field |
| AI                                              | Broad match       |

Videos failing the filter are marked with skipped=1 and skip_reason='irrelevant', allowing auditing while preventing further processing.

2.4 Summary Generation
Prompt template. Structured prompts enforce JSON output with four fields:
Summarize the following YouTube video transcript concisely. Include:
1. Identify the main speaker
2. Extract 3-5 keywords
3. Describe how this channel relates to others on LLM themes
4. Provide a summary under 300 words

Respond only in JSON:
{"speaker": "...", "keywords": [...], "related": "...", "summary": "..."}

2.5 Storage and Eviction
Database schema:
CREATE TABLE videos (
    video_id    TEXT PRIMARY KEY,
    channel_id  TEXT, channel TEXT,
    title       TEXT, url TEXT, published TEXT,
    transcript  TEXT, summary TEXT,
    skipped     INTEGER DEFAULT 0,
    skip_reason TEXT,
    processed   TEXT DEFAULT (datetime('now'))
);

Automatic eviction. With max_keep=20, after each insert:
Retain videos with transcripts first (ORDER BY skipped ASC)
Then retain the most recently processed
Delete older entries beyond the limit
This ensures the database always holds the latest 20 relevant videos, suitable for tracking recent content.

2.6 Web Presentation
A Flask single-page application provides three endpoints:
| Route         | Purpose                                                                     |
| ------------- | --------------------------------------------------------------------------- |
| `/`           | Dashboard with statistics, channel distribution, and filterable video table |
| `/api/videos` | JSON API with full video data, including category labels                    |
| `/api/stats`  | JSON API with total, processed, summarized counts and per-channel stats     |
The frontend uses inline HTML and native JavaScript with no external dependencies. Users can filter by channel, status (Summarized / Transcript / Skipped), and keywords.

2.7 Running Modes
| Mode        | Command                 | Purpose                                         |
| ----------- | ----------------------- | ----------------------------------------------- |
| Single poll | `--poll`                | Manual run for experiments or debugging         |
| Web only    | `--serve --port 5000`   | Browse existing data                            |
| Daemon      | `--daemon --port 5000`  | Automatic polling every 30 minutes + web server |
| Fast mode   | `--poll --no-summarize` | Skip summaries, only collect and filter         |

3. Evaluation Dataset
The evaluation dataset was constructed through a structured workflow: videos are first identified via YouTube RSS feeds, followed by metadata retrieval using yt-dlp. The collected videos are then filtered using predefined keywords to ensure relevance to LLM and AI topics. For the remaining videos, English captions are extracted and cleaned, and the OpenClaw agent is used to generate structured summaries.

The dataset draws from 11 curated AI/ML-focused YouTube channels, which can be grouped into four categories based on content focus. 
Academic explanations include 3Blue1Brown, Two Minute Papers, and Yannic Kilcher, providing visualizations of principles and deep dives into research papers. 
Industry leaders such as OpenAI, Google DeepMind, and Andrej Karpathy contribute official releases and hands-on practice insights. 
AI news channels like AI Explained and Lex Fridman offer industry analyses and weekly updates.
Educational platforms, including DeepLearning.
AI, Andrew Ng, and Machine Learning Street Talk, focus on course promotion and academic interviews.

4. Experimental Results
Experimental Setup
| Parameter                               | Value                                                             |
| --------------------------------------- | ----------------------------------------------------------------- |
| Number of monitored channels            | 11                                                                |
| Maximum new videos per poll per channel | 10                                                                |
| Database capacity                       | Retain the latest 20 videos (older entries automatically deleted) |
| Summary engine                          | OpenClaw, 180s timeout per video                                  |
| Relevance filtering                     | 10 LLM/AI keywords                                                |

Overall Results
| Metric                            | Value     |
| --------------------------------- | --------- |
| Total videos collected            | 20        |
| Videos with retrieved transcripts | 20 (100%) |
| Videos with generated summaries   | 14 (70%)  |
| Videos skipped                    | 0         |

Channel Distribution
| Channel           | Videos | Summaries |
| ----------------- | ------ | --------- |
| OpenAI            | 5      | 4         |
| AI Explained      | 5      | 5         |
| Two Minute Papers | 4      | 4         |
| Andrej Karpathy   | 3      | 3         |
| 3Blue1Brown       | 2      | 1         |
| Google DeepMind   | 1      | 1         |

Topic Categorization
| Category            | Number of Videos | Example Videos                                          |
| ------------------- | ---------------- | ------------------------------------------------------- |
|  AI Agents          | 5                | OpenAI Agents SDK series, LangChain end-to-end projects |
|  AI Apps & Demos    | 1                | Image/video generation principles (Welch Labs)          |
|  AI/ML (general)    | 8                | GPT-5.5 release, GPT-2 replication, LLM usage guides    |

5. Discussion
Channel coverage. Out of 11 monitored channels, 6 produced new content during this polling cycle, providing reasonable coverage of the LLM ecosystem. Channels with low update frequency can be compensated for by expanding the monitored list.

Summary speed bottleneck. Each video summary takes approximately 30–45 seconds, with most of the time spent on OpenClaw processing. Implementing batch or asynchronous summarization could significantly reduce this latency.

Categorization accuracy. Current classification relies on regular expression keyword matching, which occasionally places videos into broad AI/ML categories. Introducing LLM-based content classification could improve precision and better differentiate nuanced topics.

Database limitations. With a 20-video retention cap, the system is optimized for tracking recent developments rather than supporting historical analysis. For long-term trend monitoring, additional archival strategies would be necessary.
