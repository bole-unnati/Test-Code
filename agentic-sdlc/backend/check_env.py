"""
check_env.py  —  SDLC Platform diagnostics

Completely standalone — imports NOTHING from the project.
Reads .env manually so it works even if settings.py has errors.

Usage (run from anywhere):
    python check_env.py
    python check_env.py --test-connections
    python backend/check_env.py
    python backend\\check_env.py          (Windows)
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sys
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — find the backend/ directory no matter where we are called from
# ─────────────────────────────────────────────────────────────────────────────
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — read .env manually (no pydantic, no imports)
# ─────────────────────────────────────────────────────────────────────────────
def _find_env() -> str | None:
    """Return the path of the first .env file found, or None."""
    candidates = [
        os.path.join(BACKEND_DIR, ".env"),
        os.path.join(os.path.dirname(BACKEND_DIR), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _read_env(path: str) -> dict[str, str]:
    """Parse a .env file into a plain dict. Strips quotes and comments."""
    result: dict[str, str] = {}
    with open(path, encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # strip surrounding quotes
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            result[key] = val
    return result


def _get(env: dict, key: str, default: str = "") -> str:
    """Get from env dict, fall back to os.environ, then default."""
    return env.get(key) or os.environ.get(key) or default


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — helpers
# ─────────────────────────────────────────────────────────────────────────────
def _ollama_running(base_url: str) -> bool:
    try:
        urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=2)
        return True
    except Exception:
        return False


def _colour(text: str, code: str) -> str:
    """ANSI colour — skipped on Windows cmd unless ANSICON/WT is active."""
    if sys.platform == "win32" and "WT_SESSION" not in os.environ:
        return text
    return f"\033[{code}m{text}\033[0m"


def ok(s: str)   -> str: return _colour(s, "32")
def err(s: str)  -> str: return _colour(s, "31")
def warn(s: str) -> str: return _colour(s, "33")
def dim(s: str)  -> str: return _colour(s, "2")
def bold(s: str) -> str: return _colour(s, "1")


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — main check
# ─────────────────────────────────────────────────────────────────────────────
def check_env() -> bool:
    env_path = _find_env()
    env      = _read_env(env_path) if env_path else {}

    print()
    print(bold("SDLC Platform — Configuration Check"))
    print(dim("─" * 50))

    if env_path:
        print(f"  .env file  : {ok(env_path)}")
    else:
        print(f"  .env file  : {err('NOT FOUND')}")

    # ── Read values ──────────────────────────────────────────────────────────
    llm_provider   = _get(env, "LLM_PROVIDER",   "auto")
    groq_key       = _get(env, "GROQ_API_KEY",   "")
    groq_model     = _get(env, "GROQ_MODEL",     "llama3-70b-8192")
    ollama_url     = _get(env, "OLLAMA_BASE_URL","http://localhost:11434")
    ollama_model   = _get(env, "OLLAMA_MODEL",   "llama3.2")
    gitea_url      = _get(env, "GITEA_URL",      "http://localhost:3000")
    gitea_token    = _get(env, "GITEA_TOKEN",    "")
    plane_url      = _get(env, "PLANE_URL",      "http://localhost:80")
    plane_key      = _get(env, "PLANE_API_KEY",  "")
    outline_url    = _get(env, "OUTLINE_URL",    "http://localhost:3001")
    outline_token  = _get(env, "OUTLINE_TOKEN",  "")
    mm_url         = _get(env, "MATTERMOST_URL", "http://localhost:8065")
    mm_token       = _get(env, "MATTERMOST_TOKEN","")
    minio_url      = _get(env, "MINIO_URL",      "http://localhost:9000")
    minio_secret   = _get(env, "MINIO_SECRET_KEY","")
    zammad_url     = _get(env, "ZAMMAD_URL",     "http://localhost:8081")
    zammad_token   = _get(env, "ZAMMAD_TOKEN",   "")

    ollama_up = _ollama_running(ollama_url)

    # ── Print service table ──────────────────────────────────────────────────
    print()
    print(f"  {'Service':<16} {'Credential':<20} Status")
    print(f"  {dim('─'*16)} {dim('─'*20)} {dim('─'*30)}")

    def row(name, cred_val, extra=""):
        masked = (cred_val[:6] + "…") if len(cred_val) > 6 else cred_val
        if cred_val:
            status = ok("✓  set") + (f"  {dim(extra)}" if extra else "")
        else:
            status = dim("—  not set")
        print(f"  {name:<16} {masked:<20} {status}")

    # LLM row
    if ollama_up:
        print(f"  {'Ollama (local)':<16} {ollama_model:<20} {ok('✓  running — no rate limits')}")
    else:
        print(f"  {'Ollama (local)':<16} {ollama_model:<20} {dim('—  not running')}")

    if groq_key:
        print(f"  {'Groq':<16} {(groq_key[:6]+'…'):<20} {ok('✓  set')}  {dim(groq_model)}")
    else:
        print(f"  {'Groq':<16} {'—':<20} {dim('—  not set')}")

    print(f"  {dim('─'*16)} {dim('─'*20)} {dim('─'*30)}")
    row("Gitea",      gitea_token,   gitea_url)
    row("Plane",      plane_key,     plane_url)
    row("Outline",    outline_token, outline_url)
    row("Mattermost", mm_token,      mm_url)
    row("MinIO",      minio_secret,  minio_url)
    row("Zammad",     zammad_token,  zammad_url)

    # ── LLM summary ──────────────────────────────────────────────────────────
    print()
    print(f"  LLM_PROVIDER = {bold(llm_provider)}")
    if llm_provider == "auto":
        if ollama_up:
            print(f"  {ok('→ Will use Ollama')} ({ollama_model} at {ollama_url})")
        elif groq_key:
            print(f"  {warn('→ Ollama not running — will use Groq')} ({groq_model})")
        else:
            print(f"  {err('→ No LLM available')}")
    elif llm_provider == "ollama":
        if ollama_up:
            print(f"  {ok('→ Ollama running')} ({ollama_model})")
        else:
            print(f"  {err('→ Ollama NOT running')} — start with:  ollama serve")
    elif llm_provider == "groq":
        if groq_key:
            print(f"  {ok('→ Groq configured')} ({groq_model})")
        else:
            print(f"  {err('→ GROQ_API_KEY not set')}")

    # ── Problems ─────────────────────────────────────────────────────────────
    problems: list[str] = []
    tips:     list[str] = []

    if not env_path:
        problems.append(".env file not found")
        tips.append(
            "Create it:\n"
            "      cd backend\n"
            "      copy .env.example .env     (Windows)\n"
            "      cp .env.example .env       (Mac/Linux)\n"
            "      # then fill in your keys"
        )

    if not ollama_up and not groq_key:
        problems.append("No LLM configured (need Ollama OR Groq key)")
        tips.append(
            "Option 1 — Ollama local (no rate limits, free):\n"
            "        Install: https://ollama.com\n"
            "        Run:     ollama serve\n"
            "        Pull:    ollama pull llama3.2\n"
            "        .env:    LLM_PROVIDER=ollama\n\n"
            "   Option 2 — Groq cloud (free API key):\n"
            "        Get key: https://console.groq.com\n"
            "        .env:    GROQ_API_KEY=gsk_..."
        )
    elif groq_key and not ollama_up and llm_provider in ("groq", "auto"):
        print()
        print(f"  {warn('⚠  Groq rate limit warning:')}")
        print(f"     Free tier = 100k tokens/day. The 9-phase pipeline uses ~60-90k.")
        print(f"     To avoid 429 errors: install Ollama and set LLM_PROVIDER=ollama")

    print()

    if problems:
        print(err("❌  Problems:"))
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        print()
        print(warn("💡  How to fix:"))
        for t in tips:
            print(f"  {t}")
        print()
        return False

    print(ok("✅  Ready to start:  python main.py"))
    print()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — optional HTTP connectivity tests
# ─────────────────────────────────────────────────────────────────────────────
async def test_connections() -> None:
    try:
        import httpx
    except ImportError:
        print(err("httpx not installed — run: pip install httpx"))
        return

    env_path = _find_env()
    env      = _read_env(env_path) if env_path else {}

    def g(k, d=""): return _get(env, k, d)

    # Each entry: (name, method, url, headers, body, set-of-ok-codes, error-hints)
    cases: list[tuple] = []

    # Ollama — always check, no token needed
    cases.append((
        "Ollama", "GET",
        g("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/tags",
        {}, None, {200},
        {0: "Not running — run:  ollama serve"},
    ))

    if g("GITEA_TOKEN"):
        cases.append((
            "Gitea", "GET",
            g("GITEA_URL", "http://localhost:3000") + "/api/v1/version",
            {"Authorization": f"token {g('GITEA_TOKEN')}"},
            None, {200},
            {401: "Token wrong — Gitea → Settings → Applications → Generate Token",
             404: "GITEA_URL wrong or Gitea not running"},
        ))

    if g("PLANE_API_KEY"):
        base = g("PLANE_URL", "http://localhost:80").rstrip("/")
        if "/api/" in base:
            base = base.split("/api/")[0]
        cases.append((
            "Plane", "GET",
            base + "/api/v1/workspaces/",
            {"x-api-key": g("PLANE_API_KEY")},
            None, {200, 201},
            {401: "API key wrong — Plane → Settings → API Tokens",
             404: "Use PLANE_URL=https://api.plane.so for Plane Cloud"},
        ))

    if g("OUTLINE_TOKEN"):
        # Outline = POST-only (JSON-RPC). GET always returns 405.
        cases.append((
            "Outline", "POST",                      # ← POST, not GET
            g("OUTLINE_URL", "http://localhost:3001").rstrip("/") + "/api/auth.info",
            {"Authorization": f"Bearer {g('OUTLINE_TOKEN')}",
             "Content-Type": "application/json"},
            {}, {200},
            {401: "Token wrong — Outline → Settings → API → New token",
             405: "Check OUTLINE_URL has no trailing path (e.g. /collections)"},
        ))

    if g("MATTERMOST_TOKEN"):
        cases.append((
            "Mattermost", "GET",
            g("MATTERMOST_URL", "http://localhost:8065") + "/api/v4/users/me",
            {"Authorization": f"Bearer {g('MATTERMOST_TOKEN')}"},
            None, {200},
            {401: "Token wrong — Mattermost → Account → Security → Access Tokens",
             403: "Enable tokens: System Console → Integrations → Enable Personal Access Tokens"},
        ))

    if g("MINIO_SECRET_KEY"):
        cases.append((
            "MinIO", "GET",
            g("MINIO_URL", "http://localhost:9000") + "/minio/health/live",
            {}, None, {200, 204},
            {503: "MinIO starting up — wait a few seconds"},
        ))

    if g("ZAMMAD_TOKEN"):
        cases.append((
            "Zammad", "GET",
            g("ZAMMAD_URL", "http://localhost:8081") + "/api/v1/users/me",
            {"Authorization": f"Token token={g('ZAMMAD_TOKEN')}"},
            None, {200},
            {401: "Token wrong — Zammad → Profile → Token Access"},
        ))

    print(bold("\n── Service connectivity ──\n"))
    col = max(len(c[0]) for c in cases) + 2

    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        for name, method, url, headers, body, ok_codes, hints in cases:
            try:
                if method == "GET":
                    r = await client.get(url, headers=headers)
                else:
                    r = await client.post(url, headers=headers, json=body or {})

                code = r.status_code
                if code in ok_codes:
                    print(f"  {ok('✅')}  {name:<{col}} {method}  {code}  Connected")
                elif code in hints:
                    print(f"  {err('❌')}  {name:<{col}} {method}  {code}  {hints[code]}")
                elif code == 405:
                    print(f"  {err('❌')}  {name:<{col}} {method}  {code}  Method Not Allowed — Outline needs POST not GET")
                else:
                    print(f"  {warn('⚠ ')}  {name:<{col}} {method}  {code}  Unexpected")

            except (ConnectionRefusedError, OSError):
                hint = hints.get(0, "Is the service running?")
                print(f"  {err('❌')}  {name:<{col}} {method}  —    {hint}")
            except Exception as exc:
                print(f"  {err('❌')}  {name:<{col}} {method}  —    {exc}")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SDLC Platform — check .env and service connectivity"
    )
    parser.add_argument(
        "--test-connections", action="store_true",
        help="Ping each configured service with a real HTTP request"
    )
    args = parser.parse_args()

    ok_flag = check_env()

    if args.test_connections:
        asyncio.run(test_connections())

    sys.exit(0 if ok_flag else 1)
