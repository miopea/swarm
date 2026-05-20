# Content System v1 — "No-AI-Slop" Content Orchestration

Status: **specified, not yet implemented**
Date: 2026-05-20
Interview: 4 rounds, 16 decisions captured below
Audience: implementer (almost certainly the same model that wrote this spec)
Predecessor memories: `project_no_ai_slop_content_system`
Predecessor specs: `docs/specs/managed-browser-v1.md` (some posting paths depend on it)
Ship cadence: **4 phases, separate release commits.** Single multi-month commit explicitly rejected during the interview.

---

## Problem

Swarm today is a coding-agent orchestrator. The operator wants to
extend it into a **content orchestration system** that automates
everything around authentic human-recorded content — idea capture,
planning, scripting, post-production handoff, multi-platform
publishing, and analytics feedback — **without generating slop**.
The creator's voice stays human; the orchestration is the AI.

The system targets six platforms simultaneously (YouTube as anchor;
also X / Instagram / TikTok / Pinterest / Facebook), with a
repurposing model where one source idea spawns platform-specific
adaptations.

## Interview decisions (2026-05-20)

| # | Question | Decision |
|---|---|---|
| 1 | Tenancy | **Single creator now, hedge with `creator_id` column from day 1.** Multi-tenant SaaS is explicitly out of v1. |
| 2 | Data model | **New `content_pieces` table**, references existing pipelines + tasks. Plus a new `content_ideas` table for the capture inbox. |
| 3 | v1 scope | **All 7 OpenClaw steps**, ships in **4 phases**. |
| 4 | Voice / style | **Markdown corpus, populated over time.** No corpus on day 1 — the operator drops past scripts into `~/.swarm/content/scripts/*.md` and the corpus warms up. |
| 5 | Content shape | **Source idea → platform-specific adaptations.** One parent `content_piece`, N children — one per target platform. |
| 6 | Posting | **API where available, browser as fallback.** YouTube Data API, Facebook + Instagram Graph API, Pinterest API, TikTok for Business API. X / LinkedIn via browser v2 (or paid API if creds available). |
| 7 | Idea sources | (a) nightly YouTube scrape of competitor channels, (b) new MCP tool `swarm_capture_idea(...)`, (c) email inbox forward to an `ideas@` address. |
| 8 | Cron timing | **Idea capture nightly @ 2am**, **weekly planning Sunday @ 9am**, **analytics daily @ 6am.** All wired through P2's per-pipeline timezone field so "9am" is the creator's morning, not server-local. |
| 9 | Stage machine | **idea → planned → scripted → filming → edited → staged → published → analyzed.** Eight canonical stages enforced as an enum. |
| 10 | Asset handoff | **OneDrive via the existing Microsoft Graph integration** (Swarm is a OneDrive shop). Graph integration extended to cover Drive file upload/download in addition to the existing Outlook coverage. |
| 11 | Analytics feedback | **Daily scrape → snapshots append → Sunday Queen reads them.** The weekly planning Queen step ingests the snapshots + the captured ideas → produces a brief informing next week's content. Closes the loop without a separate analytics DB. |
| 12 | HITL gates | **Dashboard "Content" tab + Queen escalation** for every stage transition that needs operator approval. Reuses the existing escalation surface. |
| 13 | Spec location | `docs/specs/content-system-v1.md` (this file), force-added past `docs/specs/` gitignore. |
| 14 | Ship cadence | **4 phases**, separate release commits. Each phase is usable on its own. |
| 15 | Manual ideas | Operator (or any worker) calls `swarm_capture_idea(title, source, notes, target_platforms)` — new MCP tool. |
| 16 | Email ideas | Operator forwards a tweet / video / article to a configured address; a worker processes the email body into a content_idea row via the existing Outlook integration. |

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────┐
│ Idea sources                                                   │
│  ├─ youtube_scraper (existing) on nightly cron                 │
│  ├─ swarm_capture_idea MCP tool (NEW)                          │
│  └─ Email inbox via Outlook integration (NEW handler)          │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
                  ┌──────────────────┐
                  │ content_ideas    │ ← new table
                  └────────┬─────────┘
                           │ (Sunday 9am)
                           ▼
                  ┌────────────────────┐
                  │ Weekly Queen brief │ ← Queen decision: ideas + analytics → plan
                  └────────┬───────────┘
                           ▼
       ┌───────────────────┴────────────────────┐
       │ Operator approval (dashboard escalation)│
       └───────────────────┬────────────────────┘
                           ▼ (per planned idea)
                  ┌──────────────────┐
                  │ content_pieces   │ ← new table; one parent + N children
                  │  stage=planned   │
                  └────────┬─────────┘
                           ▼ (worker drafts script)
                  ┌──────────────────┐
                  │  stage=scripted  │ ← human refines
                  └────────┬─────────┘
                           ▼ (human films)
                  ┌──────────────────┐
                  │  stage=filming   │ ← assets dropped in OneDrive
                  └────────┬─────────┘
                           ▼ (editor picks up)
                  ┌──────────────────┐
                  │  stage=edited    │
                  └────────┬─────────┘
                           ▼ (per-platform captioning)
                  ┌──────────────────┐
                  │  stage=staged    │ ← operator approves
                  └────────┬─────────┘
                           ▼ (API or browser post)
                  ┌──────────────────┐
                  │  stage=published │
                  └────────┬─────────┘
                           ▼ (daily analytics scrape)
                  ┌──────────────────┐
                  │  stage=analyzed  │ ← feeds back into next Sunday's planning
                  └──────────────────┘
```

## Data model

```python
@dataclass
class ContentIdea:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    creator_id: str = "default"              # multi-tenancy hedge
    title: str = ""
    source: str = ""                          # "youtube_scrape" | "manual" | "email"
    source_url: str = ""
    notes: str = ""
    target_platforms: list[str] = field(default_factory=list)  # e.g. ["youtube", "twitter"]
    captured_at: float = field(default_factory=time.time)
    promoted_to_piece_id: str = ""            # set once an idea is planned
    discarded: bool = False
    discard_reason: str = ""

class ContentStage(Enum):
    IDEA = "idea"
    PLANNED = "planned"
    SCRIPTED = "scripted"
    FILMING = "filming"
    EDITED = "edited"
    STAGED = "staged"           # captioned per platform, awaiting publish
    PUBLISHED = "published"
    ANALYZED = "analyzed"
    DISCARDED = "discarded"     # operator-killed at any stage

class ContentPlatform(Enum):
    YOUTUBE = "youtube"
    X = "x"                     # formerly Twitter
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    PINTEREST = "pinterest"
    FACEBOOK = "facebook"
    LINKEDIN = "linkedin"       # not v1; reserved

@dataclass
class ContentPiece:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    creator_id: str = "default"
    parent_id: str = ""                       # "" for root; child piece points at parent
    platform: ContentPlatform = ContentPlatform.YOUTUBE  # ignored on root, set on children
    title: str = ""
    idea_id: str = ""                         # FK into content_ideas
    pipeline_id: str = ""                     # FK into pipelines (the lifecycle pipeline)
    stage: ContentStage = ContentStage.IDEA
    script_path: str = ""                     # ~/.swarm/content/<id>/script.md
    assets_dir: str = ""                      # OneDrive path or local path
    caption: str = ""                         # platform-ready caption / description
    hashtags: list[str] = field(default_factory=list)
    published_url: str = ""                   # URL after publish
    published_at: float | None = None
    analytics_snapshots: list[dict] = field(default_factory=list)
    # Each snapshot: {ts, views, likes, comments, shares, ctr, retention_pct, ...}
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
```

Tables (additions to v11 schema → bumps to v12):

```sql
CREATE TABLE content_ideas (
  id              TEXT PRIMARY KEY,
  creator_id      TEXT NOT NULL DEFAULT 'default',
  title           TEXT NOT NULL,
  source          TEXT NOT NULL,
  source_url      TEXT,
  notes           TEXT,
  target_platforms TEXT,                       -- JSON array
  captured_at     REAL NOT NULL,
  promoted_to_piece_id TEXT,
  discarded       INTEGER NOT NULL DEFAULT 0,
  discard_reason  TEXT
);
CREATE INDEX idx_content_ideas_captured ON content_ideas(captured_at DESC);
CREATE INDEX idx_content_ideas_promoted ON content_ideas(promoted_to_piece_id);

CREATE TABLE content_pieces (
  id              TEXT PRIMARY KEY,
  creator_id      TEXT NOT NULL DEFAULT 'default',
  parent_id       TEXT,
  platform        TEXT NOT NULL,
  title           TEXT NOT NULL,
  idea_id         TEXT,
  pipeline_id     TEXT,
  stage           TEXT NOT NULL,
  script_path     TEXT,
  assets_dir      TEXT,
  caption         TEXT,
  hashtags        TEXT,                        -- JSON array
  published_url   TEXT,
  published_at    REAL,
  analytics_snapshots TEXT NOT NULL DEFAULT '[]',  -- JSON array
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  FOREIGN KEY (parent_id) REFERENCES content_pieces(id),
  FOREIGN KEY (idea_id) REFERENCES content_ideas(id),
  FOREIGN KEY (pipeline_id) REFERENCES pipelines(id)
);
CREATE INDEX idx_content_pieces_stage ON content_pieces(stage);
CREATE INDEX idx_content_pieces_creator ON content_pieces(creator_id);
CREATE INDEX idx_content_pieces_parent ON content_pieces(parent_id);
```

Persistence rides the existing six-layer config-save chain. `ContentConfig`
dataclass on `HiveConfig` carries the tunables (idea sources, cron schedules,
per-platform API/browser configuration). Mirrors PlaybookConfig wiring (P4b).

## Module layout

```
src/swarm/content/
  __init__.py
  models.py             # ContentIdea, ContentPiece, ContentStage, ContentPlatform, ContentConfig
  ideas_store.py        # CRUD for content_ideas table
  pieces_store.py       # CRUD for content_pieces table; cascade stage transitions
  capture.py            # swarm_capture_idea MCP tool implementation
  scrapers/
    __init__.py
    youtube.py          # repurposes youtube_scraper handler for idea capture
    email.py            # processes forwarded Outlook emails into ideas
  planning.py           # weekly Queen brief: ideas + analytics → next-week plan
  scripting.py          # worker takes brief → drafts script; voice corpus loader
  publishing/
    __init__.py
    youtube.py          # YouTube Data API uploads
    meta.py             # Facebook + Instagram via Graph API
    pinterest.py        # Pinterest API
    tiktok.py           # TikTok for Business API
    x.py                # X API (paid) OR browser-based fallback
  analytics/
    __init__.py
    youtube.py          # YouTube analytics API
    meta.py
    pinterest.py
    tiktok.py
    x.py
  onedrive.py           # extends integrations/microsoft_graph for Drive upload/download

src/swarm/integrations/
  microsoft_graph.py    # extended to cover OneDrive operations

src/swarm/mcp/
  tools.py              # + swarm_capture_idea MCP tool

src/swarm/web/
  templates/dashboard.html  # + new "Content" tab
  static/dashboard.js       # + content rendering, idea capture, stage controls

src/swarm/db/
  schema.py             # v11 → v12 with content_ideas + content_pieces

tests/
  test_content_ideas_store.py
  test_content_pieces_store.py
  test_content_capture_mcp.py
  test_content_planning.py
  test_content_scripting.py
  test_publishing_*.py  # one per platform
  test_analytics_*.py   # one per platform
  test_content_dashboard.py
```

---

## Phases (4 ships, in order)

### Phase A — Data model + idea capture

Stages: **idea → planned**

- v12 schema migration: `content_ideas` + `content_pieces` tables.
- `ContentConfig` dataclass + six-layer config-save-chain wiring.
- `IdeasStore` + `PiecesStore` with CRUD methods.
- `swarm_capture_idea(title, source, notes, target_platforms)` MCP tool.
- YouTube competitor scraper pipeline template (uses existing
  `youtube_scraper` handler, scheduled nightly via P2 cron).
- Email idea processor: Outlook integration watches `ideas@` mailbox;
  parses each new mail's subject+body into a content_idea row. (Reuses
  the existing Graph OAuth setup.)
- Dashboard: new "Content" tab with two sub-views:
  - **Ideas inbox** (idea stage): list of captured ideas with
    promote / discard / edit controls.
  - **Pieces board** (planned and beyond — stays empty until Phase B): a
    kanban-style stage column view.
- Round-trip tests + comprehensive MCP tool test.

**Ship target:** ~2 weeks. Single release commit.

### Phase B — Weekly planning + scripting

Stages: **planned → scripted**

- Weekly planning pipeline template, scheduled Sunday 9am via P2 cron.
- Planning step calls headless Queen with prompt: "Here are last week's
  captured ideas + the most recent analytics snapshots. Produce a brief
  recommending N content pieces for next week, with target platforms,
  rationale, and angle suggestions." Output: a structured proposal
  that surfaces as a single Queen escalation.
- Operator approves the brief in the dashboard. On approval, the
  planned ideas get promoted to `content_pieces` rows
  (stage=planned), one parent per idea + children per target platform.
- Scripting worker (existing PTY worker): receives a task for each
  parent piece. Reads any matching scripts from
  `~/.swarm/content/scripts/*.md` (cold-start: none; warms up as the
  operator drops past scripts in). Drafts a script; writes to
  `~/.swarm/content/<piece_id>/script.md`. Flips stage to scripted.
  Surfaces a "review-and-refine" task on the dashboard.
- Operator refines the script in-place. Marks reviewed → triggers
  Phase C (handoff to filming).

**Ship target:** ~2-3 weeks. Single release commit.

### Phase C — Filming → editing → staging → publishing

Stages: **filming → edited → staged → published**

This is the biggest phase by LOC and risk. Breaks down further:

- **Filming sub-step.** Pipeline marks stage=filming; creates a
  OneDrive folder under `assets_dir`; emails / notifies the operator
  with the folder URL. Operator drops raw footage. Operator clicks
  "filming done" in the dashboard → stage=edited (if self-edit) or
  stays filming until editor signals done (if external editor).
- **OneDrive integration.** Extends `integrations/microsoft_graph.py`
  with Drive API: create folder, list contents, upload file,
  generate share link. Auth reuses the existing Outlook
  OAuth token (Graph permissions: add `Files.ReadWrite.All`).
- **Editor handoff.** When stage transitions filming → edited (via
  operator marking done or an external editor uploading a final file
  named `final.mp4`), a notification pipeline sends an email to the
  configured editor address with the OneDrive share link. Optional;
  skipped if `editor_email` is empty in ContentConfig.
- **Staging.** For each child content piece (one per platform), a
  worker generates the platform-ready caption + hashtags from the
  edited script. Stage → staged. Operator reviews per platform; can
  edit caption/hashtags in the dashboard.
- **Publishing.** Per-platform publisher:
  - YouTube → Data API v3 upload (own channel).
  - Facebook + Instagram → Graph API publish (Instagram Graph
    requires a creator/business account).
  - Pinterest → Pinterest API pin create.
  - TikTok → TikTok for Business API. (If the operator has no
    business account: browser fallback flagged here, not built.)
  - X / Twitter → API if paid creds present; **browser v2 fallback**
    otherwise. The browser v2 spec (`managed-browser-v1.md`) is
    foundational here — at minimum need the click + fill_form
    actions. Per-platform browser profile (`x`, `linkedin`, ...).
  - LinkedIn → reserved enum slot; not built in v1.
- Publishing fires only after operator approval at stage=staged
  (via Queen escalation). Per-platform sub-step can be skipped if
  the operator decides not to publish to that platform.
- Once published: stage=published, `published_url` + `published_at`
  recorded. Notifies operator with the live URLs.

**Ship target:** ~6-8 weeks. Probably 2-3 sub-releases as YouTube +
OneDrive land first, then Meta APIs, then Pinterest + TikTok + X.

### Phase D — Analytics + feedback loop

Stages: **published → analyzed**

- Daily analytics scrape pipeline (cron 6am):
  - For each content piece in stage=published (within the last N days,
    e.g. 90), pull the platform's analytics API (or browser-scrape if
    no API).
  - Append a timestamped snapshot to
    `content_pieces.analytics_snapshots`.
- Daily snapshot summary fed back to the dashboard's Content tab.
- Sunday's weekly-planning Queen call (Phase B) is updated to
  ingest the last 7 days of snapshots in its prompt. Brief includes
  performance learnings: "Hook style X overperformed last week; try
  similar."
- "Analyzed" stage transition: a content piece flips to
  stage=analyzed once it has at least 7 days of post-publish
  snapshots and the next weekly planning has incorporated it. Mostly
  for filtering / reporting purposes.

**Ship target:** ~2 weeks. Single release commit.

---

## Per-platform decision matrix (Phase C reference)

| Platform | Post via | Analytics via | OAuth required? | Notes |
|---|---|---|---|---|
| YouTube | Data API v3 (own channel) | Data API v3 + Analytics API | Yes (Google) | Anchor platform. Most polished path. |
| Facebook | Graph API | Graph API | Yes (Meta) | Reuses Meta OAuth across FB + IG. |
| Instagram | Graph API (Instagram Graph) | Instagram Graph API | Yes (Meta) | Requires creator/business account. |
| Pinterest | Pinterest API | Pinterest API | Yes (Pinterest) | Surface: pin creation + image upload. |
| TikTok | TikTok for Business API | TikTok for Business API | Yes (TikTok dev) | Requires business account; otherwise out. |
| X / Twitter | API (paid) OR browser v2 | API (paid) OR browser v2 scrape | Paid creds OR browser profile | Browser fallback flagged for build-when-needed. |
| LinkedIn | API (limited) OR browser v2 | Limited API; browser scrape | Yes | Reserved enum slot; not v1. |

---

## Dashboard UI

New top-level **Content** tab in the bottom panel (next to Tasks /
Decisions / Pipelines / Playbooks / Activity). Three sub-tabs:

1. **Ideas** — captured idea inbox. Each idea card shows source, age,
   title, notes. Actions: Promote → spawn a parent ContentPiece +
   children for selected platforms. Discard with reason. Edit.
2. **Pieces** — kanban by stage. Each card: title, platform (or
   "parent" badge for root), stage, age, latest analytics row if
   published. Click → side panel with full detail + script preview +
   stage controls.
3. **Analytics** — per-platform performance summary across all
   published pieces in the last 30 days. Sparkline per piece. Top 5
   over/underperformers. Feeds the operator's mental model.

Stage transitions that require approval fire Queen escalations exactly
like existing approval rules. Operator approves/rejects/edits from
either the Decisions tab (existing) or directly from the Content tab
card.

---

## Out of scope for v1

- **Multi-tenant / SaaS deployment.** Single-creator only. The
  `creator_id` column is the hedge but no UI / RBAC for multi-creator.
- **Voice generation / lipsync / avatars.** Slop. Skip.
- **Auto-generated thumbnails.** Operator handles.
- **A/B testing of captions.** Phase E if ever.
- **Cross-creator analytics benchmarking.** Phase F if ever.
- **LinkedIn full pipeline.** Reserved enum, no implementation.
- **Live-streaming workflows.** Different shape entirely.
- **Comment moderation / DM auto-reply.** Different surface.

---

## Risks (named explicitly)

- **Multi-platform API breakage.** Every platform changes its API
  surface, rate limits, ToS. v1 builds six integrations; expected
  maintenance load is non-trivial. **Mitigation:** all platforms
  behind a `Publisher` protocol so one platform breaking doesn't
  cascade; per-platform tests + status badges in the dashboard.
- **OneDrive token expiry.** Same hazard as Outlook today. **Mitigation:**
  the existing Graph OAuth refresh handles this; surface token-expiry
  as a dashboard banner.
- **Voice match quality on first draft.** No corpus on day 1 means
  scripts are written from scratch and the operator edits heavily.
  **Mitigation:** explicit "first 10 pieces are rough" expectation
  in the docs; corpus auto-warms as the operator confirms scripts.
- **Browser-based posting brittleness.** Platform UI changes break
  the click drivers. **Mitigation:** prefer APIs everywhere; treat
  browser as a fallback flagged with each call.
- **Operator-approval bottleneck.** Every stage transition that
  matters routes through Queen escalation. If the operator's not
  watching, content stalls. **Mitigation:** escalations age into the
  Activity log with a clear "still waiting" badge; dashboard surfaces
  the count.
- **Content posted before review.** Worst-case: a wedged worker
  publishes a draft. **Mitigation:** publish stage is **always** behind
  an explicit Queen escalation; no auto-publish path exists for v1.
- **Subscription cost explosion.** The weekly-planning Queen call +
  the per-script worker prompts (with attached corpus + analytics
  snapshots) could rack up usage if not bounded. **Mitigation:**
  cron-driven so call frequency is bounded; corpus attachment capped
  at top-N scripts.

---

## Open questions

- **Editor workflow specifics.** Is the editor a real person we
  notify via email? An automated editor service like Submagic /
  Descript? A worker? The spec assumes a real person + email
  notification, but the operator may have a specific tool in mind.
- **Per-platform content adaptation.** "Repurpose for X" means
  different things — YouTube long-form → TikTok 30s = a full re-cut;
  YouTube → Twitter thread = a script rewrite. Phase B's scripting
  step needs platform-specific prompts; the spec defers the prompt
  library to implementation time.
- **Analytics scrape vs platform-native dashboards.** Some operators
  prefer to keep analytics in YouTube Studio / Meta Business Suite
  rather than ingesting into Swarm. v1 ingests; if operator wants to
  opt out, configure `analytics: enabled=false` and the feedback
  loop degrades to "manual review."
- **X / Twitter API access cost.** If the operator has paid X API
  creds, use them; if not, browser fallback. The decision lives in
  config; the spec leaves the choice to the operator.
