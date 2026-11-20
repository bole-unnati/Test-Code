"""
test_agent.py — Test individual SDLC phases in isolation.

Run from backend/ folder:
    python test_agent.py requirements
    python test_agent.py requirements --doc path/to/srs.txt
    python test_agent.py design
    python test_agent.py planning
    python test_agent.py development
    python test_agent.py testing
    python test_agent.py ci_cd
    python test_agent.py deployment
    python test_agent.py monitoring
    python test_agent.py documentation
    python test_agent.py all              # run all phases, passing real state between them
    python test_agent.py all --doc path/to/srs.txt

Each phase runs with realistic pre-filled state so it works standalone.
When running 'all', real output from each phase feeds the next.
"""
from __future__ import annotations
import os, sys, asyncio, json, argparse, textwrap, time

_BACKEND = os.path.dirname(os.path.abspath(__file__))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Load .env
def _load_env():
    for path in [
        os.path.join(_BACKEND, ".env"),
        os.path.join(os.path.dirname(_BACKEND), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]:
        if os.path.isfile(path):
            with open(path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    if k not in os.environ:
                        os.environ[k] = v
            return path
    return None

_env = _load_env()

SAMPLE_DOC = """
Project: TaskFlow Pro — Collaborative Task Management SaaS

Functional Requirements:
FR-001: Users shall register and login with email and password.
FR-002: Users shall create projects and invite team members.
FR-003: Users shall create tasks with title, description, assignee, due date, priority.
FR-004: Tasks shall have statuses: Todo, In Progress, Done.
FR-005: Users shall receive real-time notifications for task assignments.
FR-006: The system shall expose a REST API with JWT authentication.

Non-Functional Requirements:
NFR-001: API response time shall be under 200ms at p95.
NFR-002: System shall support 1000 concurrent users.
NFR-003: Availability shall be 99.9% monthly.

Constraints:
C-001: Tech stack: Python, FastAPI, PostgreSQL, Redis, Docker.
C-002: Deploy on Docker Compose for local, Kubernetes for production.
"""

PHASES = [
    "requirements", "design", "planning", "development",
    "testing", "ci_cd", "deployment", "monitoring", "documentation"
]

AGENT_MAP = {
    "requirements":  "RequirementsAgent",
    "design":        "DesignAgent",
    "planning":      "PlanningAgent",
    "development":   "DevelopmentAgent",
    "testing":       "QAAgent",
    "ci_cd":         "CICDAgent",
    "deployment":    "DeploymentAgent",
    "monitoring":    "MonitoringAgent",
    "documentation": "DocumentationAgent",
}


# ── Colours ───────────────────────────────────────────────────────────────────
def _c(t, code):
    if sys.platform == "win32" and "WT_SESSION" not in os.environ:
        return t
    return f"\033[{code}m{t}\033[0m"

green  = lambda s: _c(s, "32")
red    = lambda s: _c(s, "31")
yellow = lambda s: _c(s, "33")
cyan   = lambda s: _c(s, "36")
bold   = lambda s: _c(s, "1")
dim    = lambda s: _c(s, "2")

def sec(title):
    print(f"\n{bold(cyan('─'*60))}", flush=True)
    print(f"  {bold(title)}", flush=True)
    print(cyan('─'*60), flush=True)

def ok(m):   print(f"  {green('✓')}  {m}", flush=True)
def fail(m): print(f"  {red('✗')}  {m}", flush=True)
def info(m): print(f"  {dim('·')}  {m}", flush=True)
def warn(m): print(f"  {yellow('⚠')}  {m}", flush=True)


# ── Build minimal state for a phase ──────────────────────────────────────────
def make_state(phase: str, doc: str, project: str,
               prior_state: dict | None = None,
               image_tag: str | None = None) -> dict:
    """Build state for a phase — uses prior_state if available, else prefills sensibly."""
    from src.graph.state import initial_state
    base = dict(initial_state(doc, project))

    # Merge any real output from prior phases
    if prior_state:
        base.update({k: v for k, v in prior_state.items()
                     if v is not None and k not in ("events", "messages")})
        return base

    # Prefill realistic stub data for standalone runs
    if phase in ("design","planning","development","testing",
                 "ci_cd","deployment","monitoring","documentation"):
        base["requirements"] = [
            {"id":"REQ-001","title":"User Authentication","description":"Login/register",
             "type":"functional","priority":"critical",
             "acceptance_criteria":["JWT token returned on login"]},
            {"id":"REQ-002","title":"Task Management","description":"CRUD tasks",
             "type":"functional","priority":"high",
             "acceptance_criteria":["Tasks have status Todo/In Progress/Done"]},
            {"id":"NFR-001","title":"Performance","description":"p95 < 200ms",
             "type":"non_functional","priority":"high",
             "acceptance_criteria":["p95 latency under 200ms"]},
        ]

    if phase in ("planning","development","testing","ci_cd","deployment","monitoring","documentation"):
        base["design"] = {
            "architecture_summary": "FastAPI microservice with PostgreSQL and Redis",
            "components": [
                {"name":"API","type":"service","tech":"FastAPI","responsibility":"HTTP"},
                {"name":"DB", "type":"database","tech":"PostgreSQL","responsibility":"Storage"},
            ],
            "tech_stack": {"language":"Python 3.12","framework":"FastAPI",
                           "database":"PostgreSQL","infrastructure":"Docker"},
            "api_contracts": [
                {"method":"POST","path":"/api/v1/auth/login","description":"Login","auth":"none"},
                {"method":"GET", "path":"/api/v1/tasks","description":"List tasks","auth":"bearer"},
            ],
            "data_models": [{"entity":"User","fields":[{"name":"id"},{"name":"email"}]}],
            "adrs": [],
        }

    if phase in ("development","testing","ci_cd","deployment","monitoring","documentation"):
        base["work_items"] = [
            {"id":"WI-001","title":"Implement auth","description":"POST /auth/login",
             "type":"story","priority":"critical","status":"backlog",
             "linked_requirements":["REQ-001"],"external_id":"ISSUE-1","external_url":None},
            {"id":"WI-002","title":"Implement task CRUD","description":"GET/POST /tasks",
             "type":"story","priority":"high","status":"backlog",
             "linked_requirements":["REQ-002"],"external_id":"ISSUE-2","external_url":None},
        ]

    if phase in ("testing","ci_cd","deployment","monitoring","documentation"):
        base["code_repo_url"] = None
        base["code_branch"]   = "feature/implementation"
        base["code_pr_url"]   = None
        if image_tag:
            base["image_tag"] = image_tag
            print(f"  Using image: {image_tag}", flush=True)
        # Inject sample generated files so Docker tests have something to build
        from src.agents.sdlc_agents import _generate_fallback_files
        base["generated_files"] = _generate_fallback_files({
            "project_name": project,
            "design": {
                "tech_stack": {"language": "Python 3.12", "framework": "FastAPI"},
                "api_contracts": [
                    {"method": "GET", "path": "/health", "description": "Health check"},
                    {"method": "GET", "path": "/api/tasks", "description": "List tasks"},
                ],
            }
        })

    if phase in ("ci_cd","deployment","monitoring","documentation"):
        base["test_coverage"] = 82.5
        base["test_passed"]   = 8
        base["test_failed"]   = 1
        base["test_output"]   = "8 passed, 1 failed"

    if phase in ("deployment","monitoring","documentation"):
        base["ci_status"] = "success"

    if phase in ("monitoring","documentation"):
        base["staging_url"]    = None
        base["production_url"] = None

    if phase == "documentation":
        base["monitoring_config"] = {
            "slos":   [{"name":"availability","target":99.9}],
            "alerts": [{"name":"high_error_rate","condition":"> 0.5%","severity":"critical"}],
            "health_check_path": "/health",
        }

    return base


# ── Run a single phase ────────────────────────────────────────────────────────
async def run_phase(phase: str, doc: str, project: str,
                    prior_state: dict | None = None,
                    image_tag: str | None = None) -> dict:
    from src.tools.oss_tools import PHASE_TOOLS

    class_name = AGENT_MAP[phase]
    import importlib
    module     = importlib.import_module("src.agents.sdlc_agents")
    AgentClass = getattr(module, class_name)

    print(f"\n{bold('='*60)}", flush=True)
    print(f"  {bold(cyan('PHASE:'))} {bold(phase.upper().replace('_',' '))}"
          f"  ({class_name})", flush=True)
    print(bold('='*60), flush=True)

    # ── LLM check ─────────────────────────────────────────────────────────────
    sec("LLM")
    import urllib.request
    ollama_url   = os.environ.get("OLLAMA_BASE_URL","http://localhost:11434")
    ollama_model = os.environ.get("OLLAMA_MODEL","llama3.2")
    groq_key     = os.environ.get("GROQ_API_KEY","")
    try:
        urllib.request.urlopen(ollama_url+"/api/tags", timeout=2)
        ok(f"Ollama  model={ollama_model}")
    except Exception:
        if groq_key:
            ok(f"Groq  key={groq_key[:8]}...")
        else:
            fail("No LLM — start Ollama or set GROQ_API_KEY")
            return {}

    # ── Services ───────────────────────────────────────────────────────────────
    sec("Services")
    from config.settings import settings
    enabled = settings.enabled_services
    if enabled:
        ok(f"Active: {', '.join(enabled)}")
    else:
        warn("No services — analysis-only mode")
    tools = PHASE_TOOLS.get(phase, [])
    info(f"Phase tools: {[t.name for t in tools]}")

    # ── State ──────────────────────────────────────────────────────────────────
    sec("State")
    state = make_state(phase, doc, project, prior_state, image_tag=image_tag)
    info(f"requirements : {len(state.get('requirements',[]))}")
    info(f"work_items   : {len(state.get('work_items',[]))}")
    info(f"generated_files: {len(state.get('generated_files',[]))}")
    info(f"code_repo    : {state.get('code_repo_url') or 'none'}")
    info(f"test_coverage: {state.get('test_coverage',0):.1f}%")
    info(f"ci_status    : {state.get('ci_status') or 'none'}")

    # ── System prompt preview ──────────────────────────────────────────────────
    sec("System Prompt (preview)")
    agent_obj = AgentClass()
    sp = agent_obj.system_prompt
    print(dim(textwrap.fill(sp[:400], width=58, initial_indent="  ", subsequent_indent="  ")),
          flush=True)
    if len(sp) > 400:
        print(dim(f"  ... ({len(sp)} chars total)"), flush=True)

    # ── Human prompt preview ───────────────────────────────────────────────────
    sec("Human Prompt (preview)")
    hp = agent_obj.human_prompt(state)
    print(dim(textwrap.fill(hp[:500], width=58, initial_indent="  ", subsequent_indent="  ")),
          flush=True)
    if len(hp) > 500:
        print(dim(f"  ... ({len(hp)} chars total)"), flush=True)

    # ── Run agent ──────────────────────────────────────────────────────────────
    sec("Running...")
    t0 = time.time()
    try:
        result = await agent_obj.run(state, tools)
    except Exception as e:
        import traceback
        fail(f"Agent crashed: {e}")
        print(traceback.format_exc(), flush=True)
        return {}
    elapsed = time.time() - t0
    info(f"Completed in {elapsed:.1f}s")

    # ── Results ────────────────────────────────────────────────────────────────
    sec("Output")
    skip = {"events","quality_gates","phase_history","current_phase","updated_at",
            "messages","raw_requirements_doc","_llm_response_preview","_response_length"}
    for k, v in result.items():
        if k in skip: continue
        if isinstance(v, list):
            ok(f"{k}: {len(v)} items")
            for item in (v[:3] if v and isinstance(v[0], dict) else []):
                title = (item.get("title") or item.get("name") or
                         item.get("path") or str(item)[:60])
                print(f"       • {title}", flush=True)
            if len(v) > 3:
                print(f"       ... +{len(v)-3} more", flush=True)
        elif isinstance(v, dict):
            ok(f"{k}: {list(v.keys())}")
        elif v:
            ok(f"{k}: {str(v)[:100]}")

    # ── Quality gate ───────────────────────────────────────────────────────────
    sec("Quality Gate")
    for gate in result.get("quality_gates", []):
        icon = "✓" if gate.get("passed") else "✗"
        fn   = ok if gate.get("passed") else fail
        fn(f"{gate['gate_name']}  {gate['score']:.0%}  "
           f"{'BLOCKING' if gate.get('blocking') else 'non-blocking'}")
        for check in gate.get("checks", []):
            (ok if check.get("passed") else fail)(check.get("detail",""))

    # ── Tool calls ─────────────────────────────────────────────────────────────
    events = result.get("events", [])
    tool_calls = []
    for ev in events:
        tool_calls.extend(ev.get("tool_calls", []))
    if tool_calls:
        sec("Tool Calls")
        for tc in tool_calls:
            name = tc.get("tool","?")
            err  = tc.get("error")
            res  = tc.get("result")
            raw  = tc.get("raw","")
            if err:
                fail(f"{name}: {str(err)[:120]}")
            elif res:
                ok(f"{name}: {str(res)[:120]}")
            else:
                warn(f"{name}: no result  raw={str(raw)[:80]}")

    print(f"\n{'='*60}\n", flush=True)
    return result


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(
        description="Test individual SDLC phases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python test_agent.py requirements
          python test_agent.py requirements --doc docs/srs.txt
          python test_agent.py development --project "MyApp"
          python test_agent.py all
          python test_agent.py all --doc docs/srs.txt
        """)
    )
    parser.add_argument("phase",
        help="Phase to test: " + " | ".join(PHASES) + " | all")
    parser.add_argument("--doc",     help="Path to requirements doc (uses sample if not set)")
    parser.add_argument("--project", default="TaskFlow Pro", help="Project name")
    parser.add_argument("--image",   help="Docker image tag for deployment phase (e.g. sdlc/taskflow-pro:latest)")
    args = parser.parse_args()

    # Load doc
    doc = SAMPLE_DOC
    if args.doc:
        for enc in ("utf-8","utf-8-sig","cp1252","latin-1"):
            try:
                with open(args.doc, encoding=enc) as f:
                    doc = f.read()
                print(f"Loaded: {args.doc}  ({len(doc)} chars, {enc})", flush=True)
                break
            except (UnicodeDecodeError, LookupError):
                continue
    else:
        print(dim("(No --doc given — using built-in sample document)"), flush=True)

    if args.phase == "all":
        print(bold(f"\n{'═'*60}"), flush=True)
        print(bold(f"  Running all {len(PHASES)} phases sequentially"), flush=True)
        print(bold(f"{'═'*60}"), flush=True)

        results  = {}
        state    = None   # carries real output phase → phase
        for phase in PHASES:
            try:
                result = await run_phase(phase, doc, args.project, prior_state=state)
                if result:
                    # Merge into accumulated state for next phase
                    if state is None:
                        state = result
                    else:
                        state = {**state, **{k: v for k, v in result.items()
                                             if v is not None and k != "events"}}
                results[phase] = "✓ passed" if result.get("quality_gates") else "~ done"
            except Exception as e:
                results[phase] = f"✗ error: {e}"
                print(red(f"\n{phase} crashed: {e}\n"), flush=True)

        print(bold(f"\n{'═'*60}"), flush=True)
        print(bold("  RESULTS"), flush=True)
        print(bold(f"{'═'*60}"), flush=True)
        for ph, status in results.items():
            (ok if "✓" in status else (warn if "~" in status else fail))(
                f"{ph:<20} {status}"
            )
        print(flush=True)

    elif args.phase in AGENT_MAP:
        await run_phase(args.phase, doc, args.project, image_tag=getattr(args,"image",None))
    else:
        print(red(f"Unknown phase: {args.phase}"), flush=True)
        print(f"Valid: {' | '.join(PHASES)} | all", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    import textwrap
    asyncio.run(main())