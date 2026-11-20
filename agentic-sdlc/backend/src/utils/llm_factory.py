"""
src/utils/llm_factory.py

Picks the right LLM and validates it supports tool calling.

Common error:  "does not support tools (status code: 400)"
Root cause:    llama3 (original) does NOT support tools.
Fix:           Use llama3.1, llama3.2, mistral, or qwen2.5 instead.

Ollama models that DO support tool calling:
  llama3.1, llama3.2, llama3.3, mistral, mistral-nemo,
  qwen2.5, qwen2.5-coder, phi4, command-r, firefunction-v2

Ollama models that do NOT support tools (→ status 400):
  llama3, llama3:latest, llama2, gemma, gemma2, codellama
"""
from __future__ import annotations
import os as _os, sys as _sys
_b = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _b not in _sys.path: _sys.path.insert(0, _b)

import asyncio
import urllib.request
import structlog
from langchain_core.language_models import BaseChatModel

log = structlog.get_logger(__name__)

# ── Tool-capable model registry ───────────────────────────────────────────────
# Models confirmed to support Ollama tool calling API
TOOL_CAPABLE_MODELS = {
    "llama3.1", "llama3.1:8b", "llama3.1:70b", "llama3.1:405b",
    "llama3.2", "llama3.2:1b", "llama3.2:3b",
    "llama3.3", "llama3.3:70b",
    "mistral", "mistral:7b", "mistral-nemo", "mistral-small",
    "qwen2.5", "qwen2.5:7b", "qwen2.5:14b", "qwen2.5:32b", "qwen2.5:72b",
    "qwen2.5-coder", "qwen2.5-coder:7b", "qwen2.5-coder:14b", "qwen2.5-coder:32b",
    "phi4", "phi4:14b",
    "command-r", "command-r:35b",
    "firefunction-v2",
    "hermes3", "hermes3:8b",
    "nemotron-mini",
}

# Models known to NOT support tools
NO_TOOL_MODELS = {
    "llama3", "llama3:latest", "llama3:8b", "llama3:70b",
    "llama2", "llama2:7b", "llama2:13b", "llama2:70b",
    "gemma", "gemma:7b", "gemma2", "gemma2:9b", "gemma2:27b",
    "codellama", "codellama:7b", "codellama:13b", "codellama:34b",
    "deepseek-coder", "deepseek-coder:6.7b",
    "dolphin-mistral",
}

# Best default — small, fast, supports tools, runs on most laptops
RECOMMENDED_MODEL = "llama3.2"


def _supports_tools(model: str) -> bool:
    """Return True if the model is known to support tool calling."""
    base = model.split(":")[0].lower()
    full = model.lower()
    # Check exact match first, then base name
    if full in TOOL_CAPABLE_MODELS or base in {m.split(":")[0] for m in TOOL_CAPABLE_MODELS}:
        return True
    if full in NO_TOOL_MODELS or base in {m.split(":")[0] for m in NO_TOOL_MODELS}:
        return False
    # Unknown model — assume it might support tools (try it)
    return True


def _validate_ollama_model(model: str) -> str:
    """
    Check if the model supports tools. If not, warn and suggest a fix.
    Returns the model name (possibly adjusted).
    """
    if not _supports_tools(model):
        log.warning(
            "ollama.model.no_tools",
            model=model,
            fix=f"Run: ollama pull {RECOMMENDED_MODEL}  then set OLLAMA_MODEL={RECOMMENDED_MODEL}"
        )
        print(
            f"\n{'='*60}\n"
            f"  WARNING: '{model}' does not support tool calling.\n"
            f"  This will cause 'status code: 400' errors.\n\n"
            f"  Fix — pull a tool-capable model:\n"
            f"    ollama pull {RECOMMENDED_MODEL}\n\n"
            f"  Then update your .env:\n"
            f"    OLLAMA_MODEL={RECOMMENDED_MODEL}\n"
            f"{'='*60}\n"
        )
    return model


def _ollama_is_running(base_url: str) -> bool:
    """Return True if Ollama is reachable."""
    try:
        urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=2)
        return True
    except Exception:
        return False


def _build_ollama(base_url: str, model: str) -> BaseChatModel:
    from langchain_ollama import ChatOllama
    validated = _validate_ollama_model(model)
    log.info("llm.using", provider="ollama", model=validated, url=base_url)
    return ChatOllama(model=validated, base_url=base_url, temperature=0, num_predict=8192)


def _build_groq(api_key: str, model: str) -> BaseChatModel:
    from langchain_groq import ChatGroq
    log.info("llm.using", provider="groq", model=model)
    return ChatGroq(api_key=api_key, model=model, temperature=0, max_tokens=8192)


# ── Main factory ──────────────────────────────────────────────────────────────

def build_llm() -> BaseChatModel:
    """
    Return the best available LLM.
    Uses getattr() with safe defaults — works even on older settings.py
    that don't have llm_provider / ollama_* fields yet.
    """
    from config.settings import settings

    provider     = getattr(settings, "llm_provider",    "auto").lower().strip()
    ollama_url   = getattr(settings, "ollama_base_url", "http://localhost:11434")
    ollama_model = getattr(settings, "ollama_model",    RECOMMENDED_MODEL)
    groq_key     = getattr(settings, "groq_api_key",    "")
    groq_model   = getattr(settings, "groq_model",      "llama3-70b-8192")

    if provider == "ollama":
        if not _ollama_is_running(ollama_url):
            raise RuntimeError(
                "LLM_PROVIDER=ollama but Ollama is not running at " + ollama_url + ".\n"
                "Start:  ollama serve\n"
                "Pull:   ollama pull " + ollama_model
            )
        return _build_ollama(ollama_url, ollama_model)

    if provider == "groq":
        if not groq_key.strip():
            raise RuntimeError(
                "LLM_PROVIDER=groq but GROQ_API_KEY is not set.\n"
                "Get a free key: https://console.groq.com"
            )
        return _build_groq(groq_key, groq_model)

    # auto — prefer Ollama (no rate limits), fall back to Groq
    if _ollama_is_running(ollama_url):
        log.info("llm.auto", chosen="ollama", model=ollama_model)
        return _build_ollama(ollama_url, ollama_model)

    if groq_key.strip():
        log.info("llm.auto", chosen="groq", model=groq_model)
        return _build_groq(groq_key, groq_model)

    raise RuntimeError(
        "No LLM available.\n"
        "Option 1 — Ollama local (no rate limits, free):\n"
        "  Install: https://ollama.com\n"
        "  Run:     ollama serve\n"
        "  Pull:    ollama pull " + RECOMMENDED_MODEL + "\n"
        "  .env:    LLM_PROVIDER=ollama\n"
        "           OLLAMA_MODEL=" + RECOMMENDED_MODEL + "\n"
        "Option 2 — Groq cloud (free key, 100k tokens/day):\n"
        "  Get key: https://console.groq.com\n"
        "  .env:    GROQ_API_KEY=gsk_..."
    )


# ── Rate-limit handler (for Groq 429 errors) ─────────────────────────────────

class RateLimitHandler:
    """
    Wraps agent.ainvoke() with retry + Ollama fallback for Groq 429 errors.
    """

    def __init__(self, max_retries: int = 4):
        self.max_retries = max_retries

    async def ainvoke_with_retry(self, agent, messages: dict, config: dict) -> dict:
        last_exc = None

        for attempt in range(1, self.max_retries + 1):
            try:
                return await agent.ainvoke(messages, config=config)

            except Exception as exc:
                err_str = str(exc)

                # Tool-not-supported error — give a clear message
                if "does not support tools" in err_str or (
                    "status code: 400" in err_str and "tool" in err_str.lower()
                ):
                    raise RuntimeError(
                        "Model does not support tool calling.\n\n"
                        "Your current model: check OLLAMA_MODEL in .env\n\n"
                        "Fix:\n"
                        "  1. ollama pull " + RECOMMENDED_MODEL + "\n"
                        "  2. Add to .env:  OLLAMA_MODEL=" + RECOMMENDED_MODEL + "\n"
                        "  3. Restart:      python main.py\n\n"
                        "Tool-capable models: llama3.1, llama3.2, llama3.3,\n"
                        "  mistral, qwen2.5, qwen2.5-coder, phi4\n\n"
                        "Original error: " + err_str
                    )

                # Groq 429 rate-limit
                if "429" in err_str or "rate_limit_exceeded" in err_str:
                    wait = self._parse_wait(err_str) or min(30 * (2 ** (attempt - 1)), 300)
                    log.warning("llm.rate_limit", attempt=attempt, wait_seconds=int(wait),
                                tip="Switch to Ollama: LLM_PROVIDER=ollama OLLAMA_MODEL=" + RECOMMENDED_MODEL)

                    if attempt == self.max_retries:
                        # Try Ollama as emergency fallback
                        from config.settings import settings
                        ou = getattr(settings, "ollama_base_url", "http://localhost:11434")
                        om = getattr(settings, "ollama_model", RECOMMENDED_MODEL)
                        if _ollama_is_running(ou) and _supports_tools(om):
                            log.info("llm.rate_limit.fallback", to="ollama")
                            from langgraph.prebuilt import create_react_agent
                            fb = create_react_agent(model=_build_ollama(ou, om), tools=[])
                            return await fb.ainvoke(messages, config=config)
                        last_exc = exc
                        break

                    log.info("llm.waiting", seconds=int(wait))
                    await asyncio.sleep(wait)
                    last_exc = exc

                elif "connection" in err_str.lower() or "refused" in err_str.lower():
                    raise RuntimeError(
                        "Cannot connect to LLM.\n"
                        "If using Ollama: run 'ollama serve'\n"
                        "Error: " + err_str
                    )
                else:
                    raise

        raise last_exc or RuntimeError("All LLM retry attempts exhausted")

    @staticmethod
    def _parse_wait(error_str: str) -> float | None:
        import re
        m = re.search(r"try again in\s+(?:(\d+)m)?(\d+(?:\.\d+)?)s", error_str)
        if m:
            return int(m.group(1) or 0) * 60 + float(m.group(2) or 0) + 2
        return None


rate_limit_handler = RateLimitHandler()