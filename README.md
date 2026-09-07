# Reddit Multi-Agent Automation Platform

**Reddit Multi-Agent Automation Platform** is a multi-persona orchestration system for managing authorized Reddit accounts through specialized AI agents. It coordinates Research, Content, and Analysis agents through a LangGraph workflow with human-in-the-loop (HITL) approval gates, and enforces human-like posting behavior through a dedicated behavior-profile/jitter layer — so activity stays clearly non-automated in pattern even though it's agent-driven.

Reddit is the pilot platform. The architecture is designed to extend to additional social platforms later without restructuring the core orchestration layer.

---

## Overview

| Layer | Description |
|-------|-------------|
| **Frontend** | React + Vite dashboard with live persona status, an approval queue, and a websocket-driven activity feed |
| **API** | FastAPI backend — persona management, workflow execution, and approval endpoints |
| **Orchestration** | LangGraph state machine (`HITLRedditOrchestrator`) with checkpointing and an explicit human-approval interrupt |
| **Agents** | Research, Content, and Analysis agents, each scoped per persona |
| **Task execution** | Redis-backed BullMQ queue — workflow runs execute out-of-band, not inline in the request |
| **Memory** | PostgreSQL for approvals/audit logs, Redis for behavior-profile state and the task queue |
| **Integrations** | Reddit API (via asyncpraw) |
| **Compliance** | Per-persona behavior-profile manager enforcing jittered posting intervals, hourly/daily caps, and active-hours windows |

---

## Workflow model

1. **Persona setup** — An authorized Reddit account is registered as a persona, with its own credentials, personality traits, and default subreddit.
2. **Research** — The Research agent gathers subreddit context and synthesizes a research brief. Read-only; rarely requires approval.
3. **Content drafting** — The Content agent drafts a post or comment reply grounded in the research. Any Reddit-facing output from this agent is approval-gated by default.
4. **Analysis** — The Analysis agent reviews the draft against the research for tone, relevance, and risk flags before the checkpoint decides.
5. **HITL checkpoint** — Confidence score, action type, and a live behavior-profile check (jitter/rate-limit/active-hours) jointly decide whether the run proceeds automatically or pauses for a human.
6. **Human approval** — If paused, the run sits at the `approval_gate` interrupt until a human approves or rejects it from the dashboard.
7. **Execution** — On approval, the side-effects step calls the Reddit API and records the action against that persona's behavior profile, so future rate-limit checks reflect real activity.

---

## Features

- **Per-persona orchestration** — each authorized Reddit account gets its own agent set and LangGraph orchestrator instance, since Reddit's OAuth session is single-account per client
- **LangGraph HITL orchestration** with explicit `interrupt_before`/`interrupt_after` nodes, not polling-based approval
- **Behavior-profile compliance layer** — minimum jittered gaps between actions, hourly/daily action caps, and active-hours windows, tracked per persona in Redis
- **Human approval queue** — pending approvals surfaced via REST and pushed live over a websocket bridge
- **Queue-backed execution** — workflow runs are enqueued (BullMQ/Redis) rather than blocking on an open HTTP request, since a run can pause indefinitely awaiting a human
- **Audit logging** — every side-effect execution is logged to PostgreSQL with the acting agent, persona, and payload

---

## Tech Stack

| Category | Technologies |
|----------|--------------|
| Backend | Python 3.9+, FastAPI, Uvicorn, Pydantic v2 |
| Agent framework | LangGraph |
| LLM access | Anthropic / OpenAI (via the existing `llm_service`) |
| Databases | PostgreSQL (approvals, audit logs), Redis (behavior profiles, task queue) |
| Task queue | BullMQ (Python) on Redis |
| Reddit integration | asyncpraw |
| Frontend | React, Vite, Tailwind CSS, lucide-react |
| Real-time | Native WebSocket, bridged from an in-process event bus |

---

## Prerequisites

- **Python** 3.9 or later
- **Node.js** 18+ and npm/pnpm
- **PostgreSQL** (for approvals and audit logs)
- **Redis** (for behavior-profile tracking and the task queue)
- At least one **LLM API key** (Anthropic or OpenAI)
- A **Reddit "script" app** per persona — create at [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) to get a `client_id`/`client_secret`

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/SM41b/Reddit-Multi-Agent-Platform.git
cd Reddit-Multi-Agent-Platform
```

### 2. Configure the backend

```bash
cd backend
cp .env.example .env
```

Edit `.env`:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/reddit_platform
REDIS_URL=redis://localhost:6379
ANTHROPIC_API_KEY=your_key_here
PORT=8000
```

### 3. Install and run the backend

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
python main.py
```

The API starts at **http://localhost:8000**. Interactive docs at **http://localhost:8000/docs**.

### 4. Install and run the frontend

In a separate terminal:

```bash
cd frontend
npm install
npm run dev
```

The UI is available at **http://localhost:5173**.

### 5. Register a persona

```bash
curl -X POST http://localhost:8000/api/personas \
  -H "Content-Type: application/json" \
  -d '{
    "persona_id": "persona_alpha",
    "reddit_client_id": "...",
    "reddit_client_secret": "...",
    "reddit_username": "...",
    "reddit_password": "...",
    "default_subreddit": "some_subreddit"
  }'
```

---

## Configuration

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL connection string for approvals and audit logs |
| `REDIS_URL` | Redis connection string for behavior-profile tracking and the task queue |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | LLM provider for agent reasoning |

Reddit credentials are supplied per-persona through the `/api/personas` endpoint, not as global environment variables — this is a multi-account platform, not a single-account bot.

---

## API Surface

| Endpoint | Description |
|----------|--------------|
| `POST /api/personas` | Register and authenticate a new persona |
| `GET /api/personas/{persona_id}/status` | Agent statuses for a persona |
| `POST /api/workflow/{persona_id}/start` | Enqueue a Research → Content → Analysis run |
| `GET /api/workflow/{persona_id}/jobs/{job_id}` | Poll a queued run's status/result |
| `GET /api/workflow/{persona_id}/approvals` | List pending approvals for a persona |
| `POST /api/workflow/{persona_id}/approvals/{approval_id}/respond` | Approve or reject a pending action, resuming the run |
| `WS /ws/reddit-dashboard` | Live feed of agent completions and approval-needed events |

---

## Agent Roster

| Agent | Role | Approval bar |
|-------|------|--------------|
| **Research** | Gathers subreddit/topic context, synthesizes a research brief | Low — read-only |
| **Content** | Drafts the Reddit post or comment reply | High — the only agent whose output can trigger a real Reddit side effect |
| **Analysis** | Reviews the draft against research for tone, relevance, and risk flags | Low — advisory to the checkpoint |

The HITL orchestrator (`backend/workflows/hitl_reddit_orchestrator.py`) routes all three through a single `hitl_checkpoint` node, which combines each agent's confidence score with a live behavior-profile check before deciding whether to auto-proceed or pause for a human.

---

## Project Structure

```
Reddit-Multi-Agent-Platform/
├── backend/
│   ├── main.py                             # Primary FastAPI application
│   ├── api/
│   │   ├── reddit_workflow_api.py          # Persona/workflow/approval endpoints
│   │   └── reddit_dashboard_ws.py          # WebSocket bridge to the dashboard
│   ├── agents/
│   │   ├── base_agent.py                   # Lightweight agent base (no memory-manager coupling)
│   │   ├── research_agent.py
│   │   ├── content_agent.py
│   │   ├── analysis_agent.py
│   │   ├── agent_factory.py                # Builds one agent set + orchestrator per persona
│   │   └── behavior_profile_manager.py     # Jitter/rate-limit/active-hours compliance
│   ├── workflows/
│   │   └── hitl_reddit_orchestrator.py     # LangGraph HITL state machine
│   ├── integrations/
│   │   └── reddit_client.py                # Reddit API client (asyncpraw)
│   ├── database/
│   │   └── postgres_approvals.py           # Approval + audit log storage
│   ├── communication/
│   │   └── dashboard_notifier.py           # Publishes to the in-process event bus
│   ├── task_queue/
│   │   ├── queue_manager.py                # Generic BullMQ/Redis queue manager
│   │   └── reddit_workflow_worker.py       # Runs orchestrator jobs off the queue
│   └── core/
│       └── reddit_platform_startup.py      # Wires all of the above together at app startup
├── frontend/
│   └── src/
│       ├── services/redditApi.js           # REST client for the endpoints above
│       ├── components/
│       │   ├── PersonaStatusPanel.jsx
│       │   └── ApprovalQueue.jsx
│       ├── pages/
│       │   └── RedditDashboardPage.jsx     # Composes the panels + live websocket feed
│       └── hooks/useWebSocket.ts           # Generic websocket hook
└── docker-compose.yml                      # PostgreSQL, Redis
```

---

## Design Principles

- **One orchestrator per persona** — Reddit's OAuth session is single-account per client, so each authorized account gets its own fully isolated agent set rather than sharing state across personas.
- **Approval decisions are centralized** — only the orchestrator's `hitl_checkpoint` node makes approval calls; individual agents never gate their own actions, avoiding duplicated or conflicting approval logic.
- **Compliance is structural, not advisory** — the behavior-profile manager can force a human review regardless of how confident an agent is in its own output.
- **Queue-backed, not request-blocking** — a run that pauses for human approval can't sit on an open HTTP connection, so execution always goes through the task queue.

---

## License

License terms have not been specified in this repository. Contact the maintainers before redistribution or commercial use.