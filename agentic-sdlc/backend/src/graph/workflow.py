"""
src/graph/workflow.py
LangGraph IS the workflow engine — no n8n, no Zapier.
Conditional edges handle: retry, human review, phase routing, error recovery.
"""
from __future__ import annotations
import os as _os, sys as _sys
_b = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _b not in _sys.path: _sys.path.insert(0, _b)
import asyncio, structlog
from datetime import datetime, timezone
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from src.graph.state import SDLCState, initial_state, make_event
from src.agents.sdlc_agents import (
    RequirementsAgent, DesignAgent, PlanningAgent, DevelopmentAgent,
    QAAgent, CICDAgent, DeploymentAgent, MonitoringAgent, DocumentationAgent,
)
from src.tools.oss_tools import PHASE_TOOLS
from config.settings import settings

log = structlog.get_logger(__name__)

PHASES = ["requirements","design","planning","development","testing","ci_cd","deployment","monitoring","documentation"]
NEXT = {p: PHASES[i+1] for i, p in enumerate(PHASES[:-1])}
NEXT["documentation"] = "done"

_AGENT_CLASSES = {
    "requirements":  RequirementsAgent,
    "design":        DesignAgent,
    "planning":      PlanningAgent,
    "development":   DevelopmentAgent,
    "testing":       QAAgent,
    "ci_cd":         CICDAgent,
    "deployment":    DeploymentAgent,
    "monitoring":    MonitoringAgent,
    "documentation": DocumentationAgent,
}
_agent_cache: dict = {}
def _get_agent(phase: str):
    if phase not in _agent_cache:
        _agent_cache[phase] = _AGENT_CLASSES[phase]()
    return _agent_cache[phase]


# ── Node functions ───────────────────────────────────────────────────────────

async def init_node(state: SDLCState) -> dict:
    return {
        "current_phase": "requirements",
        "phase_history": ["init"],
        "services_enabled": settings.enabled_services,
        "events": [make_event("init", "completed", f"Services enabled: {settings.enabled_services or ['mock mode']}",
                              artifacts={"groq_model": settings.groq_model,
                                         "services": settings.enabled_services})],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def make_phase_node(phase_name: str):
    agent = _get_agent(phase_name)
    tools = PHASE_TOOLS.get(phase_name, [])

    async def node(state: SDLCState) -> dict:
        log.info("phase.start", phase=phase_name, run_id=state["run_id"])
        return await agent.run(state, tools)

    node.__name__ = f"{phase_name}_node"
    return node


async def done_node(state: SDLCState) -> dict:
    gates = state.get("quality_gates", [])
    passed = sum(1 for g in gates if g["passed"])
    return {
        "current_phase": "done",
        "phase_history": ["done"],
        "events": [make_event("done", "completed",
                               f"Pipeline complete — {passed}/{len(gates)} quality gates passed",
                               artifacts={"production_url": state.get("production_url"),
                                          "documentation_url": state.get("documentation_url"),
                                          "coverage": state.get("test_coverage", 0)})],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def error_node(state: SDLCState) -> dict:
    return {
        "current_phase": "failed",
        "phase_history": ["failed"],
        "events": [make_event("failed", "failed",
                               f"Pipeline failed after {state.get('error_count',0)} errors",
                               artifacts={"last_phase": state.get("phase_history",["unknown"])[-1]})],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Routing — LangGraph conditional edges replace n8n routing logic ──────────

def route(state: SDLCState) -> str:
    """
    After any phase node, decide: advance | retry | error.
    This IS the workflow automation — LangGraph native, zero n8n.
    """
    current = state.get("current_phase", "failed")
    if current == "failed" or state.get("abort"):
        return "error_node"

    gates = state.get("quality_gates", [])
    if gates:
        last = gates[-1]
        if last.get("blocking") and not last.get("passed"):
            if state.get("error_count", 0) >= settings.max_retries:
                return "error_node"

    return NEXT.get(current, "done")


# ── Build graph ───────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    builder = StateGraph(SDLCState)

    builder.add_node("init", init_node)
    for phase in PHASES:
        builder.add_node(phase, make_phase_node(phase))
    builder.add_node("done", done_node)
    builder.add_node("error_node", error_node)

    builder.add_edge(START, "init")
    builder.add_edge("init", "requirements")

    valid_targets = set(PHASES) | {"done", "error_node"}
    for phase in PHASES:
        builder.add_conditional_edges(phase, route, {t: t for t in valid_targets})

    builder.add_edge("done", END)
    builder.add_edge("error_node", END)

    return builder.compile(checkpointer=MemorySaver())


# ── Runner ────────────────────────────────────────────────────────────────────

GRAPH = build_graph()


async def run_single_phase(
    phase: str,
    state: SDLCState,
    run_id: str,
    on_event=None,
) -> SDLCState:
    """
    Execute one specific phase using an existing state snapshot.
    Used for phase-level re-runs from the frontend without restarting the pipeline.

    The phase node runs, updates the state, and returns the merged result.
    Does NOT advance to the next phase — caller decides what to do next.
    """
    if phase not in _AGENT_CLASSES:
        raise ValueError(f"Unknown phase: {phase}. Valid: {list(_AGENT_CLASSES)}")

    agent = _get_agent(phase)
    tools = PHASE_TOOLS.get(phase, [])

    log.info("phase.rerun", phase=phase, run_id=run_id)

    # Run the agent directly (bypassing the full graph)
    patch = await agent.run(state, tools)

    # Merge patch back into state
    merged = {**state, **patch}
    merged["current_phase"] = phase
    merged["updated_at"]    = datetime.now(timezone.utc).isoformat()

    if on_event:
        await on_event({
            "node":  phase,
            "phase": phase,
            "events": patch.get("events", []),
            "patch_keys": list(patch.keys()),
            "state_snapshot": {
                "current_phase":      phase,
                "requirements_count": len(merged.get("requirements", [])),
                "work_items_count":   len(merged.get("work_items", [])),
                "test_coverage":      merged.get("test_coverage", 0),
                "ci_status":          merged.get("ci_status"),
                "staging_url":        merged.get("staging_url"),
                "production_url":     merged.get("production_url"),
                "documentation_url":  merged.get("documentation_url"),
                "quality_gates":      merged.get("quality_gates", []),
                "services_enabled":   merged.get("services_enabled", []),
            },
        })

    log.info("phase.rerun.done", phase=phase, run_id=run_id)
    return merged


async def run_pipeline(raw_doc: str, project_name: str, run_id: str,
                        on_event=None) -> SDLCState:
    """
    Execute the full SDLC pipeline.
    on_event(patch): called after every node with the state patch —
                     used by the WebSocket broadcaster to push live updates.
    """
    state = initial_state(raw_doc, project_name, run_id)
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 120}

    log.info("pipeline.start", run_id=run_id, project=project_name,
             model=settings.groq_model)

    final = state
    async for patch in GRAPH.astream(state, config=config):
        for node_name, node_output in patch.items():
            final = {**final, **node_output}
            if on_event:
                await on_event({
                    "node": node_name,
                    "phase": node_output.get("current_phase", node_name),
                    "events": node_output.get("events", []),
                    "patch_keys": list(node_output.keys()),
                    "state_snapshot": {
                        "current_phase": final.get("current_phase"),
                        "requirements_count": len(final.get("requirements",[])),
                        "work_items_count": len(final.get("work_items",[])),
                        "test_coverage": final.get("test_coverage",0),
                        "ci_status": final.get("ci_status"),
                        "staging_url": final.get("staging_url"),
                        "production_url": final.get("production_url"),
                        "documentation_url": final.get("documentation_url"),
                        "quality_gates": final.get("quality_gates",[]),
                        "services_enabled": final.get("services_enabled",[]),
                    },
                })

    log.info("pipeline.done", run_id=run_id, phase=final.get("current_phase"))
    return final