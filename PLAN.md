# 🎯 NicheFlow Studio - Complete Project Plan

> **Multi-Account Content Automation Platform**  
> Scalable, Intelligent, and Stealth-Ready Social Media Content Management

---

## 📋 Table of Contents

1. [Project Overview](#project-overview)
2. [Core Features](#core-features)
3. [System Architecture](#system-architecture)
4. [Technology Stack](#technology-stack)
5. [Database Schema](#database-schema)
6. [UI/UX Structure](#uiux-structure)
7. [Implementation Roadmap](#implementation-roadmap)
8. [Module Specifications](#module-specifications)
9. [Deployment Strategy](#deployment-strategy)
10. [Risk Mitigation](#risk-mitigation)

---

## 🎯 Project Overview

### Vision

Create a production-grade desktop application that enables multi-account social media content management with intelligent niche verification, automated workflows, and human-like posting behavior.

### Target Platforms

- **Primary:** Instagram Reels
- **Secondary:** TikTok
- **Tertiary:** YouTube Shorts

### Core Value Propositions

1. **Multi-Niche Scalability** - Manage 10+ accounts across different niches
2. **Intelligent Curation** - AI-powered niche consistency verification
3. **Flexible Automation** - Full auto, hybrid, or manual control per account
4. **Stealth Operations** - Human behavior simulation to avoid detection
5. **Portability** - Single-file database, sync across devices

---

## 🚀 Core Features

### 1. Account Management

- Multi-platform account profiles (IG, TikTok, YouTube)
- Per-account niche configuration
- Automation level settings (Full Auto / Hybrid / Manual)
- Session management & health monitoring
- Credential encryption

### 2. Niche Intelligence Engine

- **Niche Signature Builder**
  - Reference video processing (20-30 videos)
  - Semantic embedding generation (sentence-transformers)
  - Emotional profile extraction (LLM-based)
  - Adaptive threshold calculation
- **Multi-Gate Verification**

  - Gate 1: Semantic Similarity (cosine similarity ≥ threshold)
  - Gate 2: Emotional Consistency (tone, energy, persona matching)
  - Gate 3: Quality Score (ASR confidence, length, spam detection)
  - Gate 4: Freshness Score (meme-specific: trend velocity, saturation)

- **Drift Detection**
  - Batch-level variance monitoring
  - Auto-pause on inconsistency
  - Performance-based threshold adjustment

### 3. Smart Scraping Pipeline

- Multi-source support (YouTube, TikTok, Instagram)
- Real-time trend detection (for meme niches)
- Pre-download filtering (metadata-based)
- 3-layer deduplication:
  - URL tracking
  - File hash (SHA-256)
  - Perceptual hash (visual similarity)

### 4. Content Processing

- **Auto-Enhancement**
  - Subtitle generation (Whisper ASR)
  - Audio normalization
  - Format standardization (9:16, 1080x1920)
  - Background music layering
- **Manual Editor**
  - Video preview player
  - Trim/crop tools
  - Text overlay
  - Color grading (LUT application)
  - Blur/masking tools

### 5. Caption Generator

- LLM-based caption variations (3 options)
- Niche-aligned tone matching
- Hashtag rotation (anti-spam)
- Custom templates per account
- Attribution management

### 6. Smart Scheduler

- Human-like posting patterns
  - Randomized delays (45-120 min base)
  - Time-of-day variation
  - Weekend adjustments
  - Avoid exact-hour patterns
- Queue management
  - Drag-and-drop reordering
  - Bulk scheduling (7-day auto-fill)
  - Conflict detection
  - Daily limit enforcement

### 7. Stealth Upload Engine

- Session fingerprinting (unique per account)
- Human behavior simulation:
  - Mouse movement
  - Typing speed variation
  - Random scrolling
  - Pre/post-upload delays
- Platform-specific uploaders:
  - Instagram (instagrapi)
  - TikTok (Selenium-based)
  - YouTube (OAuth API)

### 8. Analytics & Optimization

- Performance tracking (views, ER, growth)
- Top performer analysis
- Niche quality correlation
- AI insights & recommendations
- Shadowban detection
- Auto-optimization (threshold tuning, best time detection)

---

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      PRESENTATION LAYER                      │
│                    (PyQt6 Desktop UI)                        │
├─────────────────────────────────────────────────────────────┤
│  Dashboard │ Accounts │ Niche │ Scraper │ Verify │ Queue   │
│            │          │Builder│         │        │         │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│                     BUSINESS LOGIC LAYER                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │ Niche Engine │  │   Scraper    │  │   Scheduler  │     │
│  │              │  │   Pipeline   │  │   Engine     │     │
│  │ - Signature  │  │              │  │              │     │
│  │ - Gates      │  │ - Discovery  │  │ - Queue Mgmt │     │
│  │ - Drift Det  │  │ - Dedup      │  │ - Human Sim  │     │
│  └──────────────┘  │ - Download   │  └──────────────┘     │
│                     └──────────────┘                        │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │   Content    │  │   Caption    │  │   Uploader   │     │
│  │   Processor  │  │   Generator  │  │   Engine     │     │
│  │              │  │              │  │              │     │
│  │ - FFmpeg     │  │ - LLM API    │  │ - Platform   │     │
│  │ - Whisper    │  │ - Templates  │  │   APIs       │     │
│  │ - Editing    │  │ - Hashtags   │  │ - Session    │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│                         ML/AI LAYER                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │ Whisper ASR  │  │  Sentence    │  │    Ollama    │     │
│  │ (faster-     │  │  Transform.  │  │   (Llama3)   │     │
│  │  whisper)    │  │  Embeddings  │  │              │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│                      DATA LAYER                              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              SQLite Database                          │  │
│  │  (Synced via Dropbox/Google Drive)                   │  │
│  │                                                        │  │
│  │  Tables:                                              │  │
│  │  - accounts                                           │  │
│  │  - account_platforms                                  │  │
│  │  - videos                                             │  │
│  │  - posts                                              │  │
│  │  - content_sources                                    │  │
│  │  - scraping_history                                   │  │
│  │  - batch_metrics                                      │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
│  File Storage:                                               │
│  ~/Dropbox/NicheFlow/                                       │
│  ├── data/                                                   │
│  │   ├── app.db                                             │
│  │   ├── signatures/      (niche embeddings)               │
│  │   └── videos/          (processed content)              │
│  └── logs/                                                   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 🛠️ Technology Stack

### Frontend (UI)

```yaml
Framework: PyQt6 6.6.1
Styling: Qt Stylesheets (QSS) - Dark Theme
Charts: PyQtGraph / Matplotlib
Video Player: QtMultimedia
Icons: Qt Resource System
```

### Backend (Logic)

```yaml
Language: Python 3.11+
Async: asyncio, QThread (for background tasks)
Video Processing: FFmpeg-python, OpenCV
Web Scraping: yt-dlp, BeautifulSoup, Selenium
HTTP Client: requests, httpx
```

### ML/AI Stack

```yaml
Transcription: faster-whisper 0.10.0
Embeddings: sentence-transformers 2.3.1
LLM: ollama (Llama3 local)
Deep Learning: PyTorch 2.1.2
Vision: opencv-python 4.9.0
OCR: easyocr (optional)
```

### Database

```yaml
Primary: SQLite 3.x (via SQLAlchemy 2.0.25)
ORM: SQLAlchemy
Migrations: Alembic 1.13.1
Encryption: cryptography.fernet
```

### Platform APIs

```yaml
Instagram: instagrapi 1.16+
TikTok: TikTokApi / Selenium fallback
YouTube: google-api-python-client
```

### Utilities

```yaml
Config: python-dotenv
Hashing: hashlib (SHA-256)
Image: Pillow 10.2.0
Packaging: PyInstaller 6.3.0
```

---

## 💾 Database Schema

### Core Tables

#### `accounts`

```sql
CREATE TABLE accounts (
    account_id VARCHAR PRIMARY KEY,
    niche_name VARCHAR NOT NULL,

    -- Signature
    signature_path VARCHAR,
    embedding_model VARCHAR,

    -- Emotional DNA
    dominant_emotion VARCHAR,
    energy_level VARCHAR,
    persona_type VARCHAR,

    -- Adaptive thresholds
    similarity_mean FLOAT,
    similarity_std FLOAT,
    k_factor FLOAT DEFAULT 1.0,

    -- Scraping config
    target_channels TEXT,  -- JSON array
    target_hashtags TEXT,  -- JSON array

    -- Stats
    total_videos_scraped INT DEFAULT 0,
    total_videos_approved INT DEFAULT 0,
    total_videos_posted INT DEFAULT 0,

    -- Metadata
    language VARCHAR DEFAULT 'id',
    created_at TIMESTAMP,
    last_calibrated TIMESTAMP,
    last_post_at TIMESTAMP
);
```

#### `account_platforms`

```sql
CREATE TABLE account_platforms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id VARCHAR REFERENCES accounts(account_id),
    platform_id VARCHAR,  -- 'instagram', 'tiktok', 'youtube'

    -- Credentials (encrypted)
    username VARCHAR,
    encrypted_password VARCHAR,
    session_data TEXT,  -- JSON

    -- Automation settings
    automation_level VARCHAR DEFAULT 'hybrid',
    posts_per_day INT DEFAULT 3,
    posting_hours_start INT DEFAULT 8,
    posting_hours_end INT DEFAULT 22,
    min_delay_minutes INT DEFAULT 45,
    max_delay_minutes INT DEFAULT 120,

    -- Health
    last_login TIMESTAMP,
    session_healthy BOOLEAN DEFAULT TRUE,
    shadowban_suspected BOOLEAN DEFAULT FALSE,
    last_health_check TIMESTAMP,

    -- Stats
    total_posts INT DEFAULT 0,
    avg_engagement_rate FLOAT,
    follower_count INT,

    UNIQUE(account_id, platform_id)
);
```

#### `videos`

```sql
CREATE TABLE videos (
    video_id VARCHAR PRIMARY KEY,
    account_id VARCHAR REFERENCES accounts(account_id),

    -- Source
    source_url VARCHAR UNIQUE,
    source_platform VARCHAR,
    content_source_id VARCHAR REFERENCES content_sources(source_id),

    -- Deduplication
    file_hash VARCHAR(64) UNIQUE NOT NULL,
    perceptual_hash VARCHAR,

    -- File info
    file_path VARCHAR,
    duration INT,
    file_size INT,

    -- Status
    status VARCHAR DEFAULT 'scraped',
    -- Values: scraped, transcribed, verified, approved, rejected,
    --         edited, queued, posted

    -- Transcription
    transcript TEXT,
    language VARCHAR,
    asr_confidence FLOAT,

    -- Verification scores
    semantic_score FLOAT,
    semantic_passed BOOLEAN,
    emotional_score FLOAT,
    emotional_passed BOOLEAN,
    quality_score FLOAT,
    quality_passed BOOLEAN,
    freshness_score FLOAT,  -- Meme-specific
    weighted_confidence FLOAT,
    overall_approved BOOLEAN,
    reject_reason TEXT,

    -- Metadata
    scraped_at TIMESTAMP,
    processed_at TIMESTAMP,

    -- Indexes
    INDEX idx_account_status (account_id, status),
    INDEX idx_confidence (weighted_confidence DESC),
    INDEX idx_file_hash (file_hash),
    INDEX idx_perceptual (perceptual_hash)
);
```

#### `posts`

```sql
CREATE TABLE posts (
    post_id VARCHAR PRIMARY KEY,
    video_id VARCHAR REFERENCES videos(video_id),
    account_platform_id INT REFERENCES account_platforms(id),

    -- Post data
    platform_post_id VARCHAR,
    post_url VARCHAR,
    caption TEXT,
    hashtags TEXT,  -- JSON array

    -- Scheduling
    scheduled_time TIMESTAMP,
    posted_at TIMESTAMP,
    status VARCHAR DEFAULT 'scheduled',
    -- Values: scheduled, posting, posted, failed

    -- Performance
    views INT DEFAULT 0,
    likes INT DEFAULT 0,
    comments INT DEFAULT 0,
    shares INT DEFAULT 0,
    saves INT DEFAULT 0,
    engagement_rate FLOAT,

    -- Tracking
    last_metrics_update TIMESTAMP,

    INDEX idx_platform_status (account_platform_id, status),
    INDEX idx_scheduled (scheduled_time),
    INDEX idx_performance (engagement_rate DESC)
);
```

#### `content_sources`

```sql
CREATE TABLE content_sources (
    source_id VARCHAR PRIMARY KEY,
    source_type VARCHAR NOT NULL,
    -- Values: 'original', 'licensed', 'transformative', 'ugc_meme'

    -- For licensed/transformative
    original_creator VARCHAR,
    permission_status VARCHAR,
    license_type VARCHAR,

    -- Legal
    copyright_clear BOOLEAN DEFAULT FALSE,
    attribution_required BOOLEAN DEFAULT TRUE,
    attribution_text TEXT,

    created_at TIMESTAMP
);
```

#### `scraping_history`

```sql
CREATE TABLE scraping_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id VARCHAR REFERENCES accounts(account_id),
    source_url VARCHAR,

    -- Results
    videos_found INT,
    videos_downloaded INT,
    videos_skipped_duplicate INT,
    videos_failed INT,

    scraped_at TIMESTAMP,

    INDEX idx_account_date (account_id, scraped_at DESC)
);
```

#### `batch_metrics`

```sql
CREATE TABLE batch_metrics (
    batch_id VARCHAR PRIMARY KEY,
    account_id VARCHAR REFERENCES accounts(account_id),
    video_count INT,

    -- Drift metrics
    mean_intra_similarity FLOAT,
    std_intra_similarity FLOAT,
    dispersion_score FLOAT,
    drift_detected BOOLEAN,

    processed_at TIMESTAMP,

    INDEX idx_account (account_id)
);
```

---

## 🎨 UI/UX Structure

### Navigation Hierarchy

```
Main Window
├── Dashboard (Home)
├── Accounts Management
│   ├── Account List
│   ├── New Account Dialog
│   ├── Edit Account Dialog
│   └── Platform Connection Dialog
├── Niche Builder
│   ├── Reference Video Upload
│   ├── Processing Progress
│   └── Signature Results
├── Smart Scraper
│   ├── Source Configuration
│   ├── Scraping Progress
│   └── Download History
├── Verification Center
│   ├── Video List (Grid/List view)
│   ├── Video Detail Modal
│   ├── Batch Actions
│   └── Gate Settings
├── Content Editor
│   ├── Video Preview Player
│   ├── Editing Tools Panel
│   ├── Timeline Editor
│   └── Export Settings
├── Caption Generator
│   ├── AI Variations Display
│   ├── Custom Caption Editor
│   └── Hashtag Manager
├── Upload Queue
│   ├── Scheduled Posts List
│   ├── Calendar View
│   ├── Drag-Drop Reordering
│   └── Bulk Scheduler
├── Analytics Dashboard
│   ├── Performance Charts
│   ├── Top Performers
│   ├── AI Insights
│   └── Health Monitor
└── Settings
    ├── General (storage, cleanup)
    ├── Processing (models, devices)
    ├── Scheduling (defaults, timing)
    └── Advanced (debug, logging)
```

### Key UI Components

#### Custom Widgets

```python
# Reusable components
widgets/
├── video_card.py          # Video thumbnail + metadata
├── progress_widget.py     # Custom progress with messages
├── gate_indicator.py      # Visual gate pass/fail display
├── account_card.py        # Account status card
├── timeline_editor.py     # Video editing timeline
├── schedule_calendar.py   # Calendar-based scheduler
└── chart_widgets.py       # Analytics charts
```

#### Dialogs

```python
dialogs/
├── new_account.py         # Multi-step account creation
├── video_detail.py        # Full video analysis view
├── platform_auth.py       # OAuth/login dialogs
├── bulk_edit.py           # Bulk video editing settings
└── error_report.py        # Error handling dialogs
```

---

## 📅 Implementation Roadmap

### **PHASE 1: Foundation** (Weeks 1-2)

**Goal:** Working skeleton with core infrastructure

#### Week 1: Setup & Database

- [ ] Project structure setup
- [ ] PyQt6 boilerplate (main window + navigation)
- [ ] SQLite database setup
- [ ] SQLAlchemy models implementation
- [ ] Alembic migrations setup
- [ ] Config management (settings, paths)
- [ ] Logging system
- [ ] Dark theme QSS implementation

**Deliverable:** Empty UI shell with working database

#### Week 2: Account Management

- [ ] Account CRUD UI (create, read, update, delete)
- [ ] Account list view with cards
- [ ] New account dialog (multi-step wizard)
- [ ] Platform connection UI (credential storage)
- [ ] Password encryption (Fernet)
- [ ] Cloud sync path detection (Dropbox/GDrive)
- [ ] Database lock mechanism

**Deliverable:** Can create/manage accounts, credentials saved securely

---

### **PHASE 2: Niche Intelligence** (Weeks 3-4)

**Goal:** Working niche signature builder + verification

#### Week 3: Niche Signature Builder

- [ ] Reference video upload UI
- [ ] Whisper integration (faster-whisper)
- [ ] Sentence-transformers integration
- [ ] Ollama LLM integration (emotional classification)
- [ ] Background worker (QThread) for processing
- [ ] Progress tracking with real-time updates
- [ ] Centroid calculation & stats
- [ ] Signature file storage (.npz format)
- [ ] Results display UI

**Deliverable:** Can build niche signature from reference videos

#### Week 4: Verification Gates

- [ ] Semantic similarity gate implementation
- [ ] Emotional consistency gate implementation
- [ ] Quality scoring gate implementation
- [ ] Freshness scoring (for meme niches)
- [ ] Master verification orchestrator
- [ ] Verification UI (video detail modal)
- [ ] Batch verification workflow
- [ ] Gate threshold configuration UI

**Deliverable:** Can verify videos against niche signature

---

### **PHASE 3: Content Pipeline** (Weeks 5-6)

**Goal:** Scraping + deduplication + basic editing

#### Week 5: Smart Scraper

- [ ] yt-dlp integration
- [ ] URL discovery (YouTube, TikTok, IG)
- [ ] Pre-filter implementation (metadata checks)
- [ ] 3-layer deduplication:
  - [ ] URL tracking
  - [ ] File hash (SHA-256)
  - [ ] Perceptual hash (OpenCV + imagehash)
- [ ] Scraping UI with progress
- [ ] Download queue management
- [ ] Scraping history tracking

**Deliverable:** Can scrape and deduplicate videos from sources

#### Week 6: Content Editor

- [ ] FFmpeg integration
- [ ] Video player widget (QtMultimedia)
- [ ] Basic editing tools:
  - [ ] Trim/crop
  - [ ] Audio normalization
  - [ ] Subtitle burning
  - [ ] Format conversion (9:16)
- [ ] Bulk edit settings UI
- [ ] Preview & export
- [ ] Processed video storage

**Deliverable:** Can edit videos manually or bulk-apply settings

---

### **PHASE 4: Captioning & Scheduling** (Weeks 7-8)

**Goal:** AI captions + smart scheduler

#### Week 7: Caption Generator

- [ ] LLM caption generation (Ollama)
- [ ] Multiple variation generation (3 options)
- [ ] Niche tone matching
- [ ] Hashtag rotation system
- [ ] Caption templates
- [ ] Custom caption editor UI
- [ ] Attribution manager
- [ ] Caption preview

**Deliverable:** Can generate and customize captions

#### Week 8: Smart Scheduler

- [ ] Upload queue data model
- [ ] Queue UI (list + calendar views)
- [ ] Drag-drop reordering
- [ ] Human-like scheduling algorithm:
  - [ ] Random delays
  - [ ] Time-of-day variation
  - [ ] Pattern avoidance
- [ ] Bulk scheduler (7-day auto-fill)
- [ ] Conflict detection
- [ ] Daily limit enforcement

**Deliverable:** Can schedule posts with human-like patterns

---

### **PHASE 5: Platform Integration** (Weeks 9-10)

**Goal:** Working uploaders for all platforms

#### Week 9: Instagram Uploader

- [ ] instagrapi integration
- [ ] Session management (cookies, tokens)
- [ ] Login with 2FA handling
- [ ] Video upload implementation
- [ ] Caption posting
- [ ] Thumbnail generation
- [ ] Upload retry logic
- [ ] Error handling

**Deliverable:** Can upload to Instagram Reels

#### Week 10: TikTok & YouTube

- [ ] TikTok uploader (Selenium-based)
- [ ] YouTube Shorts uploader (OAuth)
- [ ] Multi-platform posting workflow
- [ ] Platform-specific settings
- [ ] Cross-platform queue management
- [ ] Upload status tracking

**Deliverable:** Can upload to all 3 platforms

---

### **PHASE 6: Stealth & Automation** (Weeks 11-12)

**Goal:** Human behavior simulation + full automation

#### Week 11: Stealth Engine

- [ ] Session fingerprinting
- [ ] Human behavior simulation:
  - [ ] Mouse movement
  - [ ] Typing speed variation
  - [ ] Random scrolling
  - [ ] Pre/post delays
- [ ] Upload behavior randomization
- [ ] User-agent rotation
- [ ] Browser fingerprint spoofing

**Deliverable:** Uploads behave human-like

#### Week 12: Automation Orchestrator

- [ ] Full auto mode workflow
- [ ] Hybrid mode (manual review gates)
- [ ] Per-account automation settings
- [ ] Auto-scraping scheduler (cron-like)
- [ ] Auto-verification pipeline
- [ ] Auto-posting engine
- [ ] Error recovery & retry logic

**Deliverable:** Can run fully autonomous per account

---

### **PHASE 7: Analytics & Optimization** (Weeks 13-14)

**Goal:** Performance tracking + auto-optimization

#### Week 13: Analytics Dashboard

- [ ] Metrics collection (via platform APIs)
- [ ] Performance charts (PyQtGraph)
- [ ] Top performers analysis
- [ ] Engagement trend graphs
- [ ] Niche quality correlation
- [ ] Export reports (CSV/PDF)

**Deliverable:** Can track and visualize performance

#### Week 14: Auto-Optimization

- [ ] Shadowban detector
- [ ] Health monitoring
- [ ] Performance-based threshold tuning
- [ ] Best posting time analyzer
- [ ] AI insights generator
- [ ] Auto-pause on issues
- [ ] Recommendation engine

**Deliverable:** System self-optimizes based on performance

---

### **PHASE 8: Polish & Release** (Weeks 15-16)

**Goal:** Production-ready application

#### Week 15: Testing & Bug Fixes

- [ ] End-to-end testing (all workflows)
- [ ] Edge case handling
- [ ] Performance optimization
- [ ] Memory leak fixes
- [ ] UI/UX polish
- [ ] Error message improvements
- [ ] Logging enhancements

#### Week 16: Packaging & Documentation

- [ ] PyInstaller build script
- [ ] Windows executable generation
- [ ] User manual (markdown)
- [ ] Video tutorials (screen recording)
- [ ] Troubleshooting guide
- [ ] Release notes
- [ ] GitHub repository setup

**Deliverable:** Distributable .exe + documentation

---

## 📦 Module Specifications

### Module 1: Niche Engine (`core/niche_engine.py`)

```python
class NicheSignatureBuilder:
    """
    Build niche signature from reference videos
    """
    def __init__(self, embedding_model='paraphrase-multilingual-MiniLM-L12-v2'):
        self.model = SentenceTransformer(embedding_model)
        self.whisper = WhisperModel("base", device="cpu")

    def build_signature(self, reference_videos: List[str]) -> dict:
        """
        Process reference videos and generate niche signature

        Returns:
            {
                'centroid': np.array,
                'stats': {'mean': float, 'std': float},
                'emotional_profile': dict,
                'reference_count': int
            }
        """
        pass

class VerificationGates:
    """
    Multi-gate verification system
    """
    def semantic_gate(self, video_embedding, niche_signature) -> dict:
        """Returns: {'passed': bool, 'score': float, 'reason': str}"""
        pass

    def emotional_gate(self, transcript, target_profile) -> dict:
        pass

    def quality_gate(self, transcript, video_metadata) -> dict:
        pass

    def freshness_gate(self, video_metadata) -> dict:
        """Meme-specific: trend velocity + saturation"""
        pass

    def verify(self, video_path, niche_signature) -> dict:
        """Master verification orchestrator"""
        pass

class DriftDetector:
    """
    Batch-level niche consistency monitoring
    """
    def detect_drift(self, batch_embeddings, niche_signature) -> dict:
        """
        Returns: {
            'drift_detected': bool,
            'dispersion': float,
            'mean_similarity': float,
            'message': str
        }
        """
        pass
```

### Module 2: Scraper (`core/scraper.py`)

```python
class SmartScraper:
    """
    Multi-source video scraper with deduplication
    """
    def discover_videos(self, source_urls: List[str]) -> List[dict]:
        """Discover videos from YouTube/TikTok/IG"""
        pass

    def pre_filter(self, video_metadata: dict, account_profile: dict) -> bool:
        """Metadata-based filtering before download"""
        pass

    def download_video(self, url: str) -> str:
        """Download with yt-dlp, return local path"""
        pass

    def is_duplicate(self, video_path: str) -> tuple[bool, str]:
        """
        3-layer dedup: URL, file hash, perceptual hash
        Returns: (is_dup, reason)
        """
        pass

    def scrape_batch(self, account_id: str, sources: List[str]) -> dict:
        """
        Full scraping pipeline
        Returns: {'found': int, 'downloaded': int, 'skipped': int}
        """
        pass

class TrendDetector:
    """
    Real-time meme trend detection (optional for meme niches)
    """
    def get_trending_memes(self, timeframe='24h') -> List[dict]:
        """Cross-platform trend aggregation"""
        pass

    def calculate_virality_score(self, meme_metadata) -> float:
        pass
```

### Module 3: Content Processor (`core/content_processor.py`)

```python
class VideoEditor:
    """
    FFmpeg-based video editing
    """
    def normalize_format(self, input_path: str, output_path: str):
        """Convert to 9:16, 1080x1920, 30fps"""
        pass

    def normalize_audio(self, input_path: str, target_lufs=-16):
        """Normalize audio levels"""
        pass

    def add_subtitles(self, video_path: str, transcript: str):
        """Burn subtitles using FFmpeg"""
        pass

    def apply_lut(self, video_path: str, lut_file: str):
        """Color grading"""
        pass

    def trim(self, video_path: str, start: float, end: float):
        pass

    def bulk_process(self, videos: List[str], settings: dict):
        """Apply same edits to multiple videos"""
        pass

class TranscriptionService:
    """
    Whisper ASR wrapper
    """
    def transcribe(self, video_path: str, language='id') -> dict:
        """
        Returns: {
            'transcript': str,
            'confidence': float,
            'segments': List[dict]
        }
        """
        pass
```

### Module 4: Caption Generator (`core/caption_generator.py`)

```python
class CaptionGenerator:
    """
    LLM-based caption generation
    """
    def generate_variations(self, transcript: str, niche_profile: dict, count=3) -> List[str]:
        """Generate multiple caption options"""
        pass

    def apply_template(self, template: str, variables: dict) -> str:
        """Template-based caption generation"""
        pass

    def generate_hashtags(self, niche: str, count=8) -> List[str]:
        """Niche-specific hashtag generation"""
        pass

    def rotate_hashtags(self, account_id: str) -> List[str]:
        """Get next hashtag set (anti-spam rotation)"""
        pass
```

### Module 5: Scheduler (`core/scheduler.py`)

```python
class SmartScheduler:
    """
    Human-like post scheduling
    """
    def calculate_next_post_time(self, account_config: dict, last_post: datetime) -> datetime:
        """
        Calculate next post time with:
        - Random delays
        - Time-of-day variation
        - Pattern avoidance
        """
        pass

    def schedule_batch(self, videos: List[str], account_id: str, days=7):
        """Auto-fill queue for N days"""
        pass

    def check_conflicts(self, scheduled_time: datetime, account_id: str) -> bool:
        """Detect scheduling conflicts"""
        pass

    def enforce_limits(self, account_id: str, date: datetime) -> bool:
        """Check daily post limit"""
        pass
```

### Module 6: Uploader (`core/uploader.py`)

```python
class InstagramUploader:
    """
    Instagram Reels uploader
    """
    def login(self, username: str, password: str) -> bool:
        """Login with session management"""
        pass

    def upload_reel(self, video_path: str, caption: str) -> dict:
        """
        Upload with retry logic
        Returns: {'success': bool, 'post_id': str, 'post_url': str}
        """
        pass

class TikTokUploader:
    """Selenium-based TikTok uploader"""
    pass

class YouTubeUploader:
    """OAuth-based YouTube Shorts uploader"""
    pass

class HumanBehaviorSimulator:
    """
    Simulate human-like interaction
    """
    def type_with_human_speed(self, text: str, wpm=60):
        pass

    def simulate_mouse_movement(self):
        pass

    def random_scroll(self):
        pass

    def pre_upload_delay(self):
        """Random delay before upload (30s-3min)"""
        pass
```

### Module 7: Analytics (`core/analytics.py`)

```python
class PerformanceTracker:
    """
    Track and analyze post performance
    """
    def fetch_metrics(self, post_id: str, platform: str) -> dict:
        """Fetch latest metrics from platform"""
        pass

    def calculate_engagement_rate(self, post: Post) -> float:
        pass

    def get_top_performers(self, account_id: str, limit=10) -> List[Post]:
        pass

    def analyze_trend(self, account_id: str, days=30) -> dict:
        """Engagement trend analysis"""
        pass

class ShadowbanDetector:
    """
    Detect potential shadowban
    """
    def check_health(self, account_id: str) -> dict:
        """
        Returns: {
            'status': 'healthy'|'warning'|'shadowban_suspected',
            'engagement_drop': float,
            'recommendation': str
        }
        """
        pass

class AutoOptimizer:
    """
    Auto-tune system parameters based on performance
    """
    def optimize_threshold(self, account_id: str):
        """Adjust verification threshold based on top performers"""
        pass

    def find_best_posting_times(self, account_id: str) -> List[int]:
        """Analyze when posts perform best"""
        pass
```

---

## 🚀 Deployment Strategy

### Development Environment

```bash
# Virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run in development
python main.py
```

### Production Build (PyInstaller)

```yaml
# build.spec
a = Analysis(
['main.py'],
pathex=[],
binaries=[],
datas=[
('assets', 'assets'),
('ml/models', 'ml/models')
],
hiddenimports=[
'PyQt6',
'faster_whisper',
'sentence_transformers',
'instagrapi'
],
...
)
```

```bash
# Build command
pyinstaller build.spec

# Output: dist/NicheFlowStudio.exe
```

### Distribution

```
NicheFlowStudio-v1.0.0/
├── NicheFlowStudio.exe
├── README.md
├── LICENSE.txt
├── INSTALLATION.md
└── docs/
    ├── USER_GUIDE.md
    ├── TROUBLESHOOTING.md
    └── screenshots/
```

### Cloud Sync Setup

```yaml
Recommended Setup:
1. Install Dropbox/Google Drive
2. Run app first time → creates ~/Dropbox/NicheFlow/
3. Database auto-syncs
4. Lock file prevents concurrent access
```

---

## ⚠️ Risk Mitigation

### Technical Risks

| Risk                  | Impact | Mitigation                         |
| --------------------- | ------ | ---------------------------------- |
| Platform API changes  | High   | Abstraction layer, regular updates |
| Shadowban/account ban | High   | Human behavior sim, rate limiting  |
| Database corruption   | Medium | Lock mechanism, auto-backup        |
| NPU not utilized      | Low    | Graceful fallback to CPU/GPU       |
| Memory leaks          | Medium | Proper cleanup, profiling          |

### Legal/Ethical Risks

| Risk                   | Impact | Mitigation                               |
| ---------------------- | ------ | ---------------------------------------- |
| Copyright infringement | High   | Focus on transformative/licensed content |
| TOS violation          | High   | Stealth features, disclaimers            |
| DMCA takedown          | Medium | Attribution system, quick removal        |

### User Guidance

- **Disclaimer:** Include clear terms that user is responsible for content
- **Best Practices Guide:** Educate on legal content sourcing
- **Built-in Warnings:** Flag high-risk content sources

---

## 📊 Success Metrics

### MVP Success Criteria (Phase 1-4)

- [ ] Can manage 5+ accounts
- [ ] Can build niche signature in <5 minutes
- [ ] Verification accuracy >85%
- [ ] Deduplication catches >95% duplicates
- [ ] Manual workflow fully functional

### V1.0 Success Criteria (Phase 1-8)

- [ ] Full auto mode works for 7+ days unattended
- [ ] Uploads to 3 platforms successfully
- [ ] Human detection rate <5%
- [ ] Analytics track 10+ metrics
- [ ] Zero critical bugs

### Long-term Goals

- [ ] 1000+ users
- [ ] 50+ niches supported
- [ ] Community template library
- [ ] Cloud-hosted option
- [ ] Mobile companion app

---

## 📝 Next Steps

### Immediate Actions (This Week)

1. ✅ Finalize tech stack choices
2. ✅ Create detailed PLAN.md (this document)
3. ⏳ Set up development environment
4. ⏳ Initialize Git repository
5. ⏳ Create project structure skeleton

### Week 1 Sprint

- [ ] PyQt6 main window boilerplate
- [ ] SQLite database + SQLAlchemy models
- [ ] Config management system
- [ ] Dark theme QSS
- [ ] Account management UI (basic CRUD)

### Questions to Resolve

1. **Platform Priority:** Instagram-first, then TikTok/YouTube?
2. **Automation Default:** Hybrid mode as default for new accounts?
3. **Cloud Sync:** Dropbox or Google Drive preferred?
4. **Budget:** Any budget for premium APIs (ElevenLabs TTS, Claude API, etc.)?

---

## 📚 Appendix

### Useful Resources

- **PyQt6 Docs:** https://doc.qt.io/qtforpython-6/
- **instagrapi:** https://github.com/adw0rd/instagrapi
- **faster-whisper:** https://github.com/guillaumekln/faster-whisper
- **sentence-transformers:** https://www.sbert.net/
- **yt-dlp:** https://github.com/yt-dlp/yt-dlp

### Development Tools

- **IDE:** VS Code / PyCharm
- **Design:** Figma (UI mockups)
- **Version Control:** Git + GitHub
- **Testing:** pytest
- **Profiling:** py-spy, memory_profiler

---

**Document Version:** 1.0  
**Last Updated:** 2026-03-13  
**Author:** Development Team  
**Status:** Ready for Implementation ✅
