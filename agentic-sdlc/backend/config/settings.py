"""
config/settings.py
Pydantic Settings for the SDLC OSS Platform.

Searches for .env in these locations (first found wins):
  1. ./backend/.env          (when running: python backend/main.py from project root)
  2. ./.env                  (when running: python main.py from inside backend/)
  3. ../.env                 (one level up — common mistake)
  4. Environment variables   (always checked last — Docker, CI, shell exports)

Common reasons tokens don't load:
  - .env is in the wrong folder  → fixed by multi-path search below
  - Line has spaces: KEY = value  → Pydantic handles this fine
  - Value has quotes: KEY="value" → strip them — Pydantic reads the raw string WITH quotes
  - Windows CRLF line endings    → fixed by encoding='utf-8-sig'
"""
from __future__ import annotations
import os
from pathlib import Path
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file() -> list[str]:
    """
    Return candidate .env paths in search order.
    Pydantic-settings accepts a list and uses the first file that exists.
    """
    here = Path(__file__).parent          # …/backend/config/
    backend = here.parent                  # …/backend/
    project = backend.parent              # …/sdlc-oss/

    candidates = [
        str(backend / ".env"),            # backend/.env  ← correct location
        str(project / ".env"),            # sdlc-oss/.env
        str(project.parent / ".env"),     # one more level up
        ".env",                           # wherever the process cwd is
    ]
    # Always return the list; pydantic-settings skips files that don't exist
    return candidates


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_find_env_file(),
        env_file_encoding="utf-8-sig",   # handles Windows BOM + regular UTF-8
        extra="ignore",
        case_sensitive=False,            # GROQ_API_KEY and groq_api_key both work
    )

    # ── Groq ───────────────────────────────────────────────────────────────
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_model: str   = Field(default="llama3-70b-8192", alias="GROQ_MODEL")
    # Groq free models: llama3-70b-8192 | llama3-8b-8192 | mixtral-8x7b-32768 | gemma2-9b-it

    # ── LLM provider selection ─────────────────────────────────────────────
    # Set LLM_PROVIDER=ollama to use local Ollama (no rate limits, fully private)
    # Set LLM_PROVIDER=groq  to use Groq cloud (faster, needs API key)
    # If LLM_PROVIDER is not set: auto-detects (ollama if running, else groq)
    llm_provider: str = Field(default="auto", alias="LLM_PROVIDER")
    # auto | groq | ollama

    # ── Ollama (local, free, no rate limits) ──────────────────────────────
    ollama_base_url: str = Field(default="http://192.168.74.12:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str    = Field(default="llama3.1", alias="OLLAMA_MODEL")
    # Recommended models (run: ollama pull <model>):
    #   llama3.2        — 3B  — fast on CPU, good quality
    #   llama3.1        — 8B  — better quality, needs ~8GB RAM
    #   qwen2.5-coder   — 7B  — excellent for code generation phases
    #   mistral         — 7B  — balanced speed/quality
    #   llama3.1:70b    — 70B — best quality, needs GPU with 40GB+ VRAM
    #
    # Pull a model: ollama pull llama3.2
    # List models:  ollama list

    # ── Pipeline mode ────────────────────────────────────────────────────────
    # Controls output size across all 9 phases.
    #
    # demo     → 5 reqs · 5 work items · 5 endpoints · 3 models · 4 files · 5 pushes
    #            Use for: quick demos, CI testing, fast pipeline verification
    #
    # standard → 15 reqs · 15 items · 15 endpoints · 8 models · 15 files · 25 pushes
    #            Use for: small-medium projects, prototypes  [DEFAULT]
    #
    # full     → 50 reqs · 50 items · 50 endpoints · 20 models · 50 files · 50 pushes
    #            Use for: production ERP/OMS/CRM — full CRUD, all entities, all endpoints
    #
    pipeline_mode: str     = Field(default="standard", alias="PIPELINE_MODE")
    # Shortcut: DEMO_MODE=true forces pipeline_mode=demo (backward compatible)
    demo_mode: bool        = Field(default=False, alias="DEMO_MODE")

    @property
    def effective_pipeline_mode(self) -> str:
        """Return the actual pipeline mode, respecting DEMO_MODE shortcut."""
        if self.demo_mode:
            return "demo"
        mode = self.pipeline_mode.lower()
        if mode not in ("demo", "standard", "full"):
            return "standard"   # invalid value → fall back to standard
        return mode

    @property
    def pipeline_caps(self) -> dict:
        """Return the cap values for the current pipeline mode."""
        caps = {
            "demo":     {"reqs": 5,  "items": 5,  "apis": 5,  "models": 3,  "files": 4,  "push": 5},
            "standard": {"reqs": 15, "items": 15, "apis": 15, "models": 8,  "files": 15, "push": 25},
            "full":     {"reqs": 50, "items": 50, "apis": 50, "models": 20, "files": 50, "push": 50},
        }
        return caps[self.effective_pipeline_mode]

    # ── Docker (remote server deployment) ───────────────────────────────────
    # Set DOCKER_HOST to connect to a remote Docker daemon
    # Examples:
    #   tcp://192.168.1.100:2375        (plain TCP, no TLS)
    #   tcp://192.168.1.100:2376        (TLS)
    #   ssh://user@192.168.1.100        (SSH tunnel)
    #   unix:///var/run/docker.sock     (local, default)
    docker_host: str       = Field(default="",  alias="DOCKER_HOST")
    docker_tls_verify: str = Field(default="",  alias="DOCKER_TLS_VERIFY")
    docker_cert_path: str  = Field(default="",  alias="DOCKER_CERT_PATH")
    docker_registry: str   = Field(default="",  alias="DOCKER_REGISTRY")
    # Optional: push built images to a registry
    # e.g. DOCKER_REGISTRY=192.168.1.100:5000  (local registry on server)

    # ── Gitea (replaces GitHub + Atlassian) ────────────────────────────────
    gitea_url: str          = Field(default="http://localhost:3000", alias="GITEA_URL")
    gitea_token: str        = Field(default="", alias="GITEA_TOKEN")
    gitea_org: str          = Field(default="sdlc-org",  alias="GITEA_ORG")
    gitea_default_repo: str = Field(default="project",   alias="GITEA_DEFAULT_REPO")

    # ── Plane (replaces Linear + Asana + Monday) ───────────────────────────
    plane_url: str            = Field(default="http://localhost:80", alias="PLANE_URL")
    plane_api_key: str        = Field(default="", alias="PLANE_API_KEY")
    plane_workspace_slug: str = Field(default="myworkspace", alias="PLANE_WORKSPACE_SLUG")
    plane_project_id: str     = Field(default="", alias="PLANE_PROJECT_ID")

    # ── Outline (replaces Notion + Confluence) ─────────────────────────────
    outline_url: str   = Field(default="http://localhost:3001", alias="OUTLINE_URL")
    outline_token: str = Field(default="", alias="OUTLINE_TOKEN")

    # ── Penpot (replaces Figma) ────────────────────────────────────────────
    penpot_url: str          = Field(default="http://localhost:9001", alias="PENPOT_URL")
    penpot_access_token: str = Field(default="", alias="PENPOT_ACCESS_TOKEN")

    # ── Mattermost (replaces Teams + Slack) ───────────────────────────────
    mattermost_url: str             = Field(default="http://localhost:8065", alias="MATTERMOST_URL")
    mattermost_token: str           = Field(default="", alias="MATTERMOST_TOKEN")
    mattermost_default_channel: str = Field(default="sdlc-bot", alias="MATTERMOST_DEFAULT_CHANNEL")

    # ── MinIO (replaces Box + SharePoint) ─────────────────────────────────
    minio_url: str        = Field(default="http://localhost:9000", alias="MINIO_URL")
    minio_access_key: str = Field(default="minioadmin", alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(default="minioadmin", alias="MINIO_SECRET_KEY")
    minio_bucket: str     = Field(default="sdlc-artifacts", alias="MINIO_BUCKET")

    # ── Zammad (replaces Intercom + HubSpot) ──────────────────────────────
    zammad_url: str   = Field(default="http://localhost:8081", alias="ZAMMAD_URL")
    zammad_token: str = Field(default="", alias="ZAMMAD_TOKEN")

    # ── Infrastructure ─────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    database_url: str = Field(default="sqlite:///./sdlc.db", alias="DATABASE_URL")

    # ── Platform ───────────────────────────────────────────────────────────
    log_level: str             = Field(default="INFO",  alias="LOG_LEVEL")
    max_retries: int           = Field(default=3,       alias="MAX_RETRIES")
    enable_human_in_loop: bool = Field(default=False,   alias="ENABLE_HUMAN_IN_LOOP")
    api_host: str              = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int              = Field(default=8000,    alias="API_PORT")
    cors_origins: str          = Field(default="http://localhost:5173,http://localhost:80",
                                       alias="CORS_ORIGINS")

    # ── Post-load validation: strip accidental surrounding quotes ──────────
    @model_validator(mode="after")
    def strip_accidental_quotes(self) -> "Settings":
        """
        Some editors or copy-paste add surrounding quotes to .env values:
            GROQ_API_KEY="gsk_abc123"   ← Pydantic reads the value WITH quotes
            GROQ_API_KEY='gsk_abc123'   ← same problem
        This validator strips them automatically.
        """
        str_fields = [
            "groq_api_key", "groq_model", "llm_provider", "ollama_base_url", "ollama_model",
            "gitea_url", "gitea_token", "gitea_org", "gitea_default_repo",
            "plane_url", "plane_api_key", "plane_workspace_slug", "plane_project_id",
            "outline_url", "outline_token",
            "penpot_url", "penpot_access_token",
            "mattermost_url", "mattermost_token", "mattermost_default_channel",
            "minio_url", "minio_access_key", "minio_secret_key", "minio_bucket",
            "zammad_url", "zammad_token",
            "redis_url", "database_url",
            "log_level", "api_host", "cors_origins",
        ]
        for field_name in str_fields:
            val = getattr(self, field_name, "")
            if isinstance(val, str) and len(val) >= 2:
                if (val.startswith('"') and val.endswith('"')) or \
                   (val.startswith("'") and val.endswith("'")):
                    object.__setattr__(self, field_name, val[1:-1])
        return self

    # ── Derived helpers ────────────────────────────────────────────────────

    @property
    def enabled_services(self) -> list[str]:
        """
        Return services that have a non-empty, non-default credential.
        MinIO is always included if it has any secret key (even the default).
        """
        checks: dict[str, str] = {
            "gitea":      self.gitea_token,
            "plane":      self.plane_api_key,
            "outline":    self.outline_token,
            "penpot":     self.penpot_access_token,
            "mattermost": self.mattermost_token,
            "minio":      self.minio_secret_key,
            "zammad":     self.zammad_token,
        }
        return [name for name, cred in checks.items() if cred.strip()]

    @property
    def env_file_loaded(self) -> str:
        """Return the path of the .env file that was actually found."""
        for path in _find_env_file():
            if os.path.exists(path):
                return os.path.abspath(path)
        return "none — using environment variables only"

    def print_status(self) -> None:
        """Print a human-readable config status table — call at startup."""
        from rich.console import Console
        from rich.table import Table
        console = Console()

        console.print(f"\n[bold cyan]SDLC Platform — Configuration[/bold cyan]")
        console.print(f"[dim].env loaded from: {self.env_file_loaded}[/dim]\n")

        t = Table(show_header=True, header_style="bold")
        t.add_column("Service", style="cyan")
        t.add_column("URL")
        t.add_column("Token / Key")
        t.add_column("Status")

        rows = [
            ("Groq LLM",    "api.groq.com",          self.groq_api_key,        "LLM"),
            ("Ollama",      self.ollama_base_url,    self.ollama_model,        "local LLM"),
            ("Gitea",       self.gitea_url,           self.gitea_token,         "code"),
            ("Plane",       self.plane_url,           self.plane_api_key,       "PM"),
            ("Outline",     self.outline_url,         self.outline_token,       "wiki"),
            ("Penpot",      self.penpot_url,          self.penpot_access_token, "design"),
            ("Mattermost",  self.mattermost_url,      self.mattermost_token,    "notify"),
            ("MinIO",       self.minio_url,           self.minio_secret_key,    "storage"),
            ("Zammad",      self.zammad_url,          self.zammad_token,        "tickets"),
        ]
        for name, url, token, role in rows:
            has = bool(token and token.strip())
            masked = (token[:6] + "…" + token[-3:]) if has and len(token) > 9 else ("set" if has else "")
            status = f"[green]✓ enabled ({role})[/green]" if has else "[dim]— not configured[/dim]"
            t.add_row(name, url or "—", masked or "[dim]not set[/dim]", status)

        console.print(t)
        console.print(f"\n[bold]Active services:[/bold] {self.enabled_services or ['none — Groq only']}\n")


# Singleton
settings = Settings()