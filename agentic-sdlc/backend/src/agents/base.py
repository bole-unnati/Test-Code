"""
src/agents/base.py
Real-time streaming execution — every step logs immediately, nothing waits.
"""
from __future__ import annotations
import os as _os, sys as _sys
_b = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _b not in _sys.path: _sys.path.insert(0, _b)

import asyncio
import json
import re
import time
import structlog
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from langchain_core.messages import SystemMessage, HumanMessage
from config.settings import settings
from src.graph.state import SDLCState, QualityGate, make_event

log = structlog.get_logger(__name__)

# Injected by server.py at startup — maps run_id → async send_log(level,phase,msg,elapsed)
_GLOBAL_LOG_CALLBACK: dict = {}

_FAKE_URL = re.compile(
    r'(localhost|\*\.local|\.local|example\.com|placeholder|your[-_]|<[^>]+>|REPLACE|TODO)',
    re.IGNORECASE,
)


# ── Live logger — prints every step immediately ───────────────────────────────

class LiveLog:
    """Prints timestamped progress to stdout immediately (no buffering)."""

    def __init__(self, phase: str, run_id: str = ""):
        self.phase   = phase.upper().replace("_", " ")
        self._run_id = run_id
        self._t0     = time.time()

    def _ts(self):
        elapsed = time.time() - self._t0
        return f"{elapsed:5.1f}s"

    def _print(self, icon: str, msg: str, level: str = "info"):
        elapsed = time.time() - self._t0
        line = f"  [{self._ts()}] {icon}  {self.phase}: {msg}"
        print(line, flush=True)
        # Push to frontend via WebSocket if a callback is registered
        if self._run_id and _GLOBAL_LOG_CALLBACK.get(self._run_id):
            cb = _GLOBAL_LOG_CALLBACK[self._run_id]
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(cb(level, self.phase.lower().replace(" ","_"), msg, round(elapsed,1)))
            except Exception:
                pass

    def start(self, msg: str):     self._print("▶", msg)
    def step(self, msg: str):      self._print("·", msg)
    def ok(self, msg: str):        self._print("✓", msg, "success")
    def warn(self, msg: str):      self._print("⚠", msg, "warning")
    def error(self, msg: str):     self._print("✗", msg, "error")
    def section(self, msg: str):
        print(f"\n  {'─'*56}", flush=True)
        print(f"  {self.phase} │ {msg}", flush=True)
        print(f"  {'─'*56}", flush=True)

    def raw_response(self, text: str):
        self.section("RAW LLM RESPONSE")
        if not text:
            print("  [EMPTY — no response from model]", flush=True)
        else:
            for line in text[:2500].split("\n"):
                print(f"  {line}", flush=True)
            if len(text) > 2500:
                print(f"\n  ... ({len(text)} total chars, showing first 2500)", flush=True)
        print(f"  {'─'*56}\n", flush=True)

    def parsed_result(self, patch: dict):
        self.section("PARSED OUTPUT")
        skip = {"_llm_response_preview", "_response_length",
                "updated_at", "current_phase", "phase_history", "events", "quality_gates"}
        shown = False
        for k, v in patch.items():
            if k in skip:
                continue
            shown = True
            if isinstance(v, list):
                print(f"  {k}: {len(v)} items", flush=True)
                for item in v[:5]:
                    if isinstance(item, dict):
                        title = (item.get("title") or item.get("name") or
                                 item.get("gate_name") or str(item)[:70])
                        print(f"      • {title}", flush=True)
                if len(v) > 5:
                    print(f"      ... and {len(v)-5} more", flush=True)
            elif isinstance(v, dict):
                print(f"  {k}: {list(v.keys())}", flush=True)
            elif v:
                print(f"  {k}: {str(v)[:100]}", flush=True)
        if not shown:
            print("  (nothing extracted)", flush=True)
        print(flush=True)

    def gate_result(self, gate: QualityGate):
        self.section("QUALITY GATE")
        icon = "✓" if gate["passed"] else "✗"
        print(f"  {icon}  {gate['gate_name']}  score={gate['score']:.0%}  "
              f"{'BLOCKING' if gate.get('blocking') else 'non-blocking'}", flush=True)
        for check in gate.get("checks", []):
            sym = "✓" if check.get("passed") else "✗"
            print(f"  {sym}  {check.get('detail', '')}", flush=True)
        print(flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sanitise_patch(patch: dict) -> dict:
    url_fields = {"code_repo_url", "code_branch", "code_pr_url",
                  "ci_url", "staging_url", "production_url", "documentation_url"}
    result = {}
    for k, v in patch.items():
        if k in url_fields and isinstance(v, str) and _FAKE_URL.search(v):
            log.warning("agent.fake_url", field=k, value=v)
            result[k] = None
        else:
            result[k] = v
    return result


def _service_context() -> str:
    """Plain-text service context added to system prompt (NOT human prompt)."""
    enabled  = settings.enabled_services
    all_svcs = ["gitea","plane","outline","penpot","mattermost","minio","zammad"]
    disabled = [s for s in all_svcs if s not in enabled]
    lines = ["", "AVAILABLE INTEGRATIONS:"]
    if enabled:
        lines.append("  Configured (you may call tools for): " + ", ".join(enabled))
    else:
        lines.append("  None configured — output analysis only, no tool calls needed")
    if disabled:
        lines.append("  Not configured (skip, set URLs to null): " + ", ".join(disabled))
    return "\n".join(lines)


# ── Base agent ────────────────────────────────────────────────────────────────

class BaseSDLCAgent(ABC):
    phase_name: str = "base"

    def __init__(self):
        from src.utils.llm_factory import build_llm
        self.llm = build_llm()

    @property
    @abstractmethod
    def system_prompt(self) -> str: ...

    @abstractmethod
    def human_prompt(self, state: SDLCState) -> str: ...

    @abstractmethod
    def parse_output(self, response: str, state: SDLCState) -> dict: ...

    @abstractmethod
    def quality_gate(self, state: SDLCState) -> QualityGate: ...

    async def execute_tools(self, parsed: dict, state: SDLCState,
                             tools: list) -> list[dict]:
        return []

    # ── Main runner ───────────────────────────────────────────────────────────

    async def run(self, state: SDLCState, tools: list) -> dict:
        ll = LiveLog(self.phase_name, run_id=state.get("run_id",""))

        ll.start(f"Phase started  run_id={state['run_id'][:8]}")
        ll.step(f"Services active: {settings.enabled_services or ['none — analysis only']}")

        start_event = make_event(
            self.phase_name, "started",
            f"Starting {self.phase_name.replace('_',' ')} phase "
            f"(services: {settings.enabled_services or ['none']})",
        )

        # ── Build prompts ─────────────────────────────────────────────────────
        # Service context goes in system prompt (not human prompt) so the LLM
        # does not mistake it for the JSON it should return.
        system_with_ctx = self.system_prompt + _service_context()
        full_prompt     = self.human_prompt(state)
        ll.step(f"Prompt built: {len(system_with_ctx)} sys + {len(full_prompt)} human chars")

        # ── Determine provider ────────────────────────────────────────────────
        provider = getattr(settings, "llm_provider", "auto")
        model    = (getattr(settings, "ollama_model", "llama3.2")
                    if provider in ("ollama", "auto")
                    else getattr(settings, "groq_model", "llama3-70b-8192"))
        ll.step(f"LLM provider={provider}  model={model}")

        # Check Ollama model is actually loaded (not just downloaded)
        if provider in ("ollama", "auto"):
            import urllib.request as _ur, json as _j
            try:
                ollama_url = getattr(settings, "ollama_base_url", "http://localhost:11434")
                resp = _ur.urlopen(ollama_url + "/api/ps", timeout=3)
                running = _j.loads(resp.read()).get("models", [])
                names   = [m.get("name","") for m in running]
                if names:
                    ll.ok(f"Ollama models currently loaded in memory: {names}")
                else:
                    ll.warn(f"No models loaded in Ollama yet — first call will load '{model}' (slow start)")
                    ll.step(f"Tip: run 'ollama run {model}' in another terminal to pre-load it")
            except Exception:
                pass  # /api/ps not available on older Ollama versions

        # ── STEP 1: Direct LLM call ───────────────────────────────────────────
        ll.step("Sending to LLM... (waiting for response)")
        log.info("agent.llm_start", phase=self.phase_name, model=model)

        ai_response = ""
        t_llm = time.time()

        try:
            messages = [
                SystemMessage(content=system_with_ctx),
                HumanMessage(content=full_prompt),
            ]

            # Stream tokens with live output + progress ticker
            ll.step("Waiting for first token from LLM (may take 10-30s to start)...")
            chunks = []
            token_count = 0
            char_count  = 0
            first_token_time = None

            async def _ticker():
                """Print elapsed time every 5s so you know it's not frozen."""
                t = 0
                while True:
                    await asyncio.sleep(5)
                    t += 5
                    if not chunks:
                        print(f"  [{t:3d}s] still waiting for first token...", flush=True)
                    else:
                        print(f"  [{t:3d}s] generating... {token_count} tokens, {char_count} chars so far", flush=True)

            ticker_task = asyncio.create_task(_ticker())

            try:
                async for chunk in self.llm.astream(messages):
                    raw = chunk.content if hasattr(chunk, "content") else str(chunk)
                    if not raw:
                        continue
                    if first_token_time is None:
                        first_token_time = time.time() - t_llm
                        ticker_task.cancel()  # stop ticker once streaming begins
                        print(f"  [first token in {first_token_time:.1f}s — now streaming...]", flush=True)
                    chunks.append(raw)
                    token_count += 1
                    char_count  += len(raw)
                    # Print every token to terminal
                    print(raw, end="", flush=True)
                    # Stream tokens to frontend too
                    if state.get("run_id") and _GLOBAL_LOG_CALLBACK.get(state["run_id"]):
                        cb = _GLOBAL_LOG_CALLBACK[state["run_id"]]
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                asyncio.ensure_future(
                                    cb("token", self.phase_name, raw, round(time.time()-t_llm,1))
                                )
                        except Exception:
                            pass

                ticker_task.cancel()
                print(flush=True)  # newline after streaming
                ai_response = "".join(chunks)

            except asyncio.CancelledError:
                ticker_task.cancel()
                raise
            except Exception as stream_exc:
                ticker_task.cancel()
                ll.warn(f"Streaming error: {stream_exc} — falling back to regular invoke")
                result = await asyncio.wait_for(
                    self.llm.ainvoke(messages), timeout=300.0
                )
                ai_response = result.content if hasattr(result, "content") else str(result)

            llm_time = time.time() - t_llm
            ll.ok(f"LLM responded in {llm_time:.1f}s  ({len(ai_response)} chars)")
            log.info("agent.llm_done", phase=self.phase_name,
                     seconds=round(llm_time, 1),
                     length=len(ai_response),
                     has_json="{" in ai_response)

        except asyncio.TimeoutError:
            ll.error("LLM timed out after 300s")
            return {"current_phase": "failed",
                    "error_count": state.get("error_count", 0) + 1,
                    "events": [start_event, make_event(self.phase_name, "failed",
                                "LLM timed out — try a faster model or shorter document")],
                    "updated_at": datetime.now(timezone.utc).isoformat()}

        except Exception as exc:
            ll.error(f"LLM error: {exc}")
            log.error("agent.llm_error", phase=self.phase_name, error=str(exc))
            return {"current_phase": "failed",
                    "error_count": state.get("error_count", 0) + 1,
                    "events": [start_event, make_event(self.phase_name, "failed", str(exc))],
                    "updated_at": datetime.now(timezone.utc).isoformat()}

        # Show raw response
        ll.raw_response(ai_response)

        # ── STEP 2: Parse JSON ────────────────────────────────────────────────
        ll.step("Parsing JSON from response...")
        state_patch: dict = {}
        tool_calls_log: list[dict] = []

        try:
            raw_patch   = self.parse_output(ai_response, state)
            state_patch = _sanitise_patch(raw_patch)
            ll.ok(f"Parsed successfully: {list(state_patch.keys())}")
        except Exception as exc:
            ll.warn(f"Parse failed: {exc}")
            log.warning("agent.parse_failed", phase=self.phase_name, error=str(exc))

        ll.parsed_result(state_patch)

        # ── STEP 3: Tool calls ────────────────────────────────────────────────
        enabled = settings.enabled_services

        # Build active tool list — pass ALL tools to execute_tools.
        # The old filter (t.name.startswith(svc+"_")) was too strict:
        # it excluded docker_* tools and any tool whose name doesn't match
        # the service prefix exactly. Let execute_tools decide which to call.
        active_tools = tools  # pass all; execute_tools filters internally

        # Only skip if no services configured AND no docker available
        from src.utils.docker_executor import _docker_available
        has_docker = _docker_available()
        has_services = bool(enabled)

        if has_services or has_docker:
            ll.step(f"Calling tools (services={enabled}, docker={has_docker})...")
            try:
                # Longer timeout: Docker build can take 3-5 min, Gitea pushes too
                tool_calls_log = await asyncio.wait_for(
                    self.execute_tools(state_patch, state, active_tools),
                    timeout=600.0,   # 10 min max
                )
                # execute_tools mutates state_patch in-place.
                # Re-sanitise and show what changed.
                state_patch = _sanitise_patch(state_patch)
                ll.ok(f"Tools completed: {len(tool_calls_log)} calls made")
                # Show updated keys so we can verify mutations took effect
                updated = {k: v for k, v in state_patch.items()
                           if v is not None and not k.startswith("_llm")
                           and k not in ("events","quality_gates","phase_history")}
                print(f"  [TOOLS] State after tools: {list(updated.keys())}", flush=True)
                for k, v in updated.items():
                    if isinstance(v, list):
                        print(f"  [TOOLS]   {k}: {len(v)} items", flush=True)
                    elif isinstance(v, str) and len(v) > 0:
                        print(f"  [TOOLS]   {k}: {str(v)[:80]}", flush=True)
                # Log each tool result
                for tc in tool_calls_log:
                    name   = tc.get("tool","?")
                    result = tc.get("result","")
                    error  = tc.get("error","")
                    raw    = tc.get("raw","")
                    if error:
                        ll.warn(f"  {name}: ERROR — {str(error)[:200]}")
                    elif result is None:
                        ll.warn(f"  {name}: returned None — raw={str(raw)[:200]}")
                    elif isinstance(result, dict) and result.get("error"):
                        ll.warn(f"  {name}: tool error — {result['error'][:200]}")
                    elif isinstance(result, str) and result.startswith("{'error"):
                        ll.warn(f"  {name}: tool error — {result[:200]}")
                    else:
                        ll.ok(f"  {name}: {str(result)[:150]}")
            except asyncio.TimeoutError:
                ll.warn("Tools timed out after 600s — continuing without tool results")
            except Exception as exc:
                ll.warn(f"Tool error: {exc}")
                import traceback
                print(traceback.format_exc(), flush=True)
        else:
            ll.step("No services and no Docker — analysis-only mode")

        # ── STEP 4: Quality gate ──────────────────────────────────────────────
        ll.step("Evaluating quality gate...")
        # Merge updated state_patch (which may have been mutated by execute_tools)
        merged = {**state, **state_patch}
        gate   = self.quality_gate(merged)
        ll.gate_result(gate)

        log.info("agent.gate", phase=self.phase_name,
                 passed=gate["passed"], score=gate["score"])

        done_event = make_event(
            self.phase_name,
            "completed" if gate["passed"] else "failed",
            self._summary(state_patch, gate),
            artifacts={
                **state_patch,
                "_llm_response_preview": ai_response[:3000] if ai_response else "[empty]",
                "_response_length": len(ai_response),
            },
            tool_calls=tool_calls_log,
        )

        total_time = time.time() - (ll._t0)
        if gate["passed"]:
            ll.ok(f"Phase complete in {total_time:.1f}s ✓")
        else:
            ll.warn(f"Phase complete in {total_time:.1f}s (gate failed)")

        return {
            **state_patch,
            "current_phase": self.phase_name,
            "phase_history": [self.phase_name],
            "quality_gates": [gate],
            "events": [start_event, done_event],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _summary(self, patch: dict, gate: QualityGate) -> str:
        status  = "passed" if gate["passed"] else f"failed ({gate['score']:.0%})"
        keys    = [k for k in patch if not k.startswith("_") and
                   k not in ("updated_at","current_phase","phase_history")]
        enabled = settings.enabled_services
        svc     = f"services: {enabled}" if enabled else "analysis-only"
        return f"{self.phase_name.replace('_',' ').title()} — gate {status} | {svc} | {keys or 'none'}"

    @staticmethod
    def _extract_json(text: str) -> dict | list:
        if not text or not text.strip():
            log.warning("agent.empty_response")
            return {}

        def _sanitise(s: str) -> str:
            """
            Replace raw control characters inside JSON strings.
            LLMs sometimes emit literal newlines/tabs inside string values
            which makes json.loads raise 'Invalid control character'.
            We replace them with their escaped equivalents ONLY inside strings.
            """
            import re as _re
            result = []
            in_str = False
            escaped = False
            for ch in s:
                if escaped:
                    result.append(ch)
                    escaped = False
                elif ch == "\\":
                    result.append(ch)
                    escaped = True
                elif ch == '"':
                    in_str = not in_str
                    result.append(ch)
                elif in_str and ch == chr(10):   # raw newline inside string
                    result.append("\\n")
                elif in_str and ch == chr(13):   # raw carriage return
                    result.append("\\r")
                elif in_str and ch == chr(9):    # raw tab
                    result.append("\\t")
                elif in_str and ord(ch) < 32:    # other control chars
                    result.append(f"\\u{ord(ch):04x}")
                else:
                    result.append(ch)
            return "".join(result)

        def _try_parse(s: str) -> dict | list | None:
            """Try direct parse, then sanitised parse."""
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
            try:
                return json.loads(_sanitise(s))
            except json.JSONDecodeError:
                return None

        # 1. Fenced block
        m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if m:
            candidate = m.group(1).strip()
            result = _try_parse(candidate)
            if result is not None:
                return result
            text = candidate

        # 2. Find JSON start
        obj = text.find("{")
        arr = text.find("[")
        if obj == -1 and arr == -1:
            log.warning("agent.no_json", preview=text[:150].replace(chr(10)," "))
            return {}

        open_c, close_c, start = ("{","}",obj) if (arr==-1 or (obj!=-1 and obj<arr)) \
                                  else ("[","]",arr)
        jtext = text[start:]

        # 3. Direct parse (with sanitise fallback)
        result = _try_parse(jtext)
        if result is not None:
            return result

        # 4. Balanced walk
        depth = 0
        for i, ch in enumerate(jtext):
            if ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth == 0:
                    result = _try_parse(jtext[:i+1])
                    if result is not None:
                        return result
                    depth = 0

        # 5. Truncation recovery — close any open brackets
        log.warning("agent.json_truncated", length=len(jtext))
        try:
            stack = []
            in_str = False
            esc    = False
            for ch in jtext:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = not in_str
                elif not in_str:
                    if ch in ("{","["):
                        stack.append("}" if ch=="{" else "]")
                    elif ch in ("}","]") and stack and stack[-1]==ch:
                        stack.pop()
            partial = jtext.rstrip().rstrip(",") + "".join(reversed(stack))
            result  = _try_parse(partial)
            if result is not None:
                log.info("agent.json_recovered",
                         keys=list(result.keys()) if isinstance(result,dict) else len(result))
                return result
        except Exception as e:
            log.error("agent.json_unrecoverable", error=str(e))
        return {}