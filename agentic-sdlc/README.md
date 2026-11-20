# SDLC Automation Platform — Open-Source Edition

> **Groq LLM (llama3-70b) · LangGraph · 100% self-hostable services · React live dashboard**

Upload a requirements document. The platform autonomously runs all 9 SDLC phases with a live WebSocket-powered frontend showing every phase as it executes.

---

## Architecture

```
Browser (React SPA)
    │  upload doc + watch WebSocket
    ▼
FastAPI  (:8000)
    │  asyncio background task
    ▼
LangGraph StateGraph  ← IS the workflow engine (replaces n8n entirely)
    │
    ├─ RequirementsAgent  ──► Zammad (tickets) + Outline (wiki) + Plane (issues)
    ├─ DesignAgent        ──► Penpot (designs) + Outline (architecture docs)
    ├─ PlanningAgent      ──► Plane (issues, cycles) + Outline (sprint docs)
    ├─ DevelopmentAgent   ──► Gitea (repo, branch, push files, PR)
    ├─ QAAgent            ──► Gitea (push tests) + MinIO (test reports)
    ├─ CICDAgent          ──► Gitea Actions (trigger CI) + Mattermost (notify)
    ├─ DeploymentAgent    ──► Gitea (release) + Mattermost (notify)
    ├─ MonitoringAgent    ──► Outline (runbooks) + Mattermost (alerts)
    └─ DocumentationAgent ──► Outline (wiki) + Gitea (README) + MinIO (archive)

All agents use: Groq LLM (llama3-70b-8192) via langchain-groq
```

---

## Why LangGraph instead of n8n?

LangGraph **IS** the workflow engine:

| n8n capability | LangGraph equivalent |
|---|---|
| Sequential node execution | `add_edge(A, B)` |
| Conditional branching | `add_conditional_edges(node, route_fn)` |
| Retry on failure | `error_count` in state + routing back |
| Human approval gate | `interrupt_before=["human_review"]` |
| Parallel execution | `Send()` API for fan-out |
| State persistence | `MemorySaver` / Redis checkpointer |
| Audit trail | Append-only `events` list in `SDLCState` |

Zero external workflow engines. The graph drives everything.

---

## Tech Stack — 100% Open-Source

| Layer | Technology | Licence | Replaces |
|---|---|---|---|
| LLM | Groq (llama3-70b-8192) | Open-weight | Anthropic / OpenAI |
| Orchestration | LangGraph + LangChain | MIT | n8n, Zapier |
| Code hosting | Gitea | MIT | GitHub, Atlassian |
| Project mgmt | Plane | AGPL | Linear, Asana, Monday |
| Wiki / docs | Outline | BSL-1.1 | Notion, Confluence |
| Design | Penpot | MPL-2.0 | Figma |
| Notifications | Mattermost | MIT | Teams, Slack |
| File storage | MinIO | AGPL | Box, SharePoint |
| Support tickets | Zammad | GPL-3 | Intercom, HubSpot |
| API server | FastAPI | MIT | — |
| Frontend | React (Babel, no build) | MIT | — |
| State store | Redis | BSD | — |

---

## Project Structure

```
sdlc-oss/
├── backend/
│   ├── config/
│   │   └── settings.py          # Pydantic Settings — all env vars
│   ├── src/
│   │   ├── tools/
│   │   │   └── oss_tools.py     # All 7 service integrations as @tool functions
│   │   ├── agents/
│   │   │   ├── base.py          # BaseSDLCAgent (Groq + LangGraph ReAct)
│   │   │   └── sdlc_agents.py   # All 9 SDLC phase agents
│   │   ├── graph/
│   │   │   ├── state.py         # SDLCState TypedDict (shared state)
│   │   │   └── workflow.py      # LangGraph graph — nodes, edges, routing
│   │   └── api/
│   │       └── server.py        # FastAPI + WebSocket live streaming
│   ├── tests/
│   │   └── test_oss_platform.py # 34 tests — all passing
│   ├── main.py
│   ├── requirements.txt
│   ├── .env.example
│   ├── Dockerfile
│   └── pyproject.toml
│
├── frontend/
│   └── index.html               # React SPA — no build step needed
│
├── docker/
│   └── nginx.conf               # nginx reverse proxy config
│
└── docker-compose.yml           # All services in one file
```

---

## Quick Start

### 1. Get a Groq API key (free)

Sign up at [console.groq.com](https://console.groq.com) — free tier includes llama3-70b.

### 2. Configure

```bash
cd backend
cp .env.example .env
# Edit .env — set GROQ_API_KEY at minimum
```

### 3. Install and run

```bash
pip install -r requirements.txt
python main.py
# API at http://localhost:8000
# Docs at http://localhost:8000/docs
```

### 4. Open the frontend

Open `frontend/index.html` in your browser — or serve it:

```bash
cd frontend
python3 -m http.server 5173
# Open http://localhost:5173
```

### 5. Run with all services (Docker)

```bash
docker compose up -d
# Frontend: http://localhost:5173
# API:      http://localhost:8000
# MinIO UI: http://localhost:9001  (minioadmin / minioadmin)
# Gitea:    http://localhost:3000
# Outline:  http://localhost:3001
# Mattermost: http://localhost:8065
```

---

## Running Tests

```bash
cd backend
GROQ_API_KEY=test pytest tests/ -v
# 34 passed — all unit tests run without a real Groq key
```

---

## Available Groq Models

Set `GROQ_MODEL` in `.env` to any of:

| Model | Context | Best for |
|---|---|---|
| `llama3-70b-8192` | 8k | Default — best quality |
| `llama3-8b-8192` | 8k | Faster, lower cost |
| `mixtral-8x7b-32768` | 32k | Long requirement docs |
| `gemma2-9b-it` | 8k | Lightweight |

---

## Frontend Features

- **Upload zone** — drag & drop or paste requirements text directly
- **Live phase progress bar** — updates in real time via WebSocket
- **Live event feed** — every phase event streams as it happens, with tool call details
- **Quality gates panel** — scored pass/fail per gate with progress bars
- **Final results panel** — all artifact URLs, stats, downloadable JSON export
- **Run history** — all runs listed with status and current phase

---

## Adding a Service Token

1. Set the token in `.env` (e.g. `GITEA_TOKEN=xxx`)
2. Restart the backend — the service is automatically enabled
3. Check `GET /api/services` to confirm

Services without tokens gracefully degrade — the agent still runs but skips tool calls that require the missing service.
