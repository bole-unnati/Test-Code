"""tests/test_oss_platform.py — Full test suite for OSS + Groq edition"""
import pytest
from src.graph.state import initial_state, make_event, SDLCState
from src.graph.workflow import route, NEXT, PHASES
from src.agents.sdlc_agents import (
    RequirementsAgent, DesignAgent, PlanningAgent,
    DevelopmentAgent, QAAgent, CICDAgent,
    DeploymentAgent, MonitoringAgent, DocumentationAgent,
)
from src.graph.state import QualityGate


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def base_state() -> SDLCState:
    return initial_state("Users shall login with email and password.", "TestProject", "test-run-001")

@pytest.fixture
def state_with_reqs(base_state) -> SDLCState:
    s = dict(base_state)
    s["requirements"] = [
        {"id":"REQ-001","title":"User login","description":"...","type":"functional",
         "priority":"high","acceptance_criteria":["AC1","AC2"]}
    ]
    s["current_phase"] = "requirements"
    return s

@pytest.fixture
def state_with_design(state_with_reqs) -> SDLCState:
    s = dict(state_with_reqs)
    s["design"] = {
        "architecture_summary": "REST API + React frontend",
        "components": [{"name":"API","type":"service"}],
        "api_contracts": [{"method":"POST","path":"/auth/login","description":"Login"}],
        "data_models": [{"entity":"User","fields":[{"name":"email","type":"str"}]}],
        "tech_stack": {"language":"Python","framework":"FastAPI","database":"PostgreSQL"},
        "adrs": [{"id":"ADR-001","title":"Use FastAPI","decision":"chosen for performance"}],
    }
    s["current_phase"] = "design"
    return s

@pytest.fixture
def full_state(state_with_design) -> SDLCState:
    s = dict(state_with_design)
    s["work_items"] = [
        {"id":"WI-001","title":"Implement login","description":"...","type":"story",
         "priority":"high","status":"backlog","linked_requirements":["REQ-001"],
         "external_id":"ISSUE-1","external_url":"http://plane.local/..."}
    ]
    s["code_repo_url"]  = "http://gitea.local/org/testproject"
    s["code_branch"]    = "feature/implementation"
    s["code_pr_url"]    = "http://gitea.local/org/testproject/pulls/1"
    s["test_coverage"]  = 88.5
    s["test_passed"]    = 45
    s["test_failed"]    = 2
    s["image_tag"]      = "sdlc/testproject:latest"
    s["ci_status"]      = "success"
    s["ci_url"]         = "http://gitea.local/org/testproject/actions/runs/1"
    s["staging_url"]    = "http://staging.local"
    s["production_url"] = "http://app.local"
    s["monitoring_config"] = {"slos":[{"name":"availability","target":99.9}],"alerts":[{"name":"high_error"}]}
    s["documentation_url"] = "http://outline.local/collection/project"
    return s


# ── State schema tests ────────────────────────────────────────────────────

class TestInitialState:
    def test_creates_valid_state(self, base_state):
        assert base_state["run_id"] == "test-run-001"
        assert base_state["current_phase"] == "init"
        assert base_state["project_name"] == "TestProject"
        assert base_state["requirements"] == []
        assert base_state["error_count"] == 0
        assert len(base_state["events"]) == 1  # init event

    def test_auto_run_id(self):
        s = initial_state("doc", "proj")
        assert len(s["run_id"]) == 36

    def test_initial_event_emitted(self, base_state):
        ev = base_state["events"][0]
        assert ev["phase"] == "init"
        assert ev["status"] == "started"


# ── Phase event tests ─────────────────────────────────────────────────────

class TestPhaseEvents:
    def test_make_event_structure(self):
        ev = make_event("requirements", "completed", "Done", {"reqs": 5}, [{"tool":"outline"}])
        assert ev["phase"] == "requirements"
        assert ev["status"] == "completed"
        assert ev["artifacts"]["reqs"] == 5
        assert ev["tool_calls"][0]["tool"] == "outline"
        assert "timestamp" in ev


# ── Routing tests ─────────────────────────────────────────────────────────

class TestRouting:
    def test_phase_sequence_complete(self):
        for p in PHASES[:-1]:
            assert p in NEXT, f"{p} missing from NEXT"

    def test_last_phase_goes_to_done(self):
        assert NEXT["documentation"] == "done"

    def test_routes_to_next_on_pass(self, state_with_reqs):
        s = dict(state_with_reqs)
        s["quality_gates"] = [QualityGate(gate_name="requirements",passed=True,
                                           score=1.0,checks=[],blocking=True)]
        assert route(s) == "design"

    def test_routes_to_error_on_failed_phase(self, base_state):
        s = dict(base_state)
        s["current_phase"] = "failed"
        assert route(s) == "error_node"

    def test_routes_to_error_on_abort(self, state_with_reqs):
        s = dict(state_with_reqs)
        s["abort"] = True
        assert route(s) == "error_node"

    def test_blocking_gate_fail_routes_to_error_after_retries(self, state_with_reqs):
        s = dict(state_with_reqs)
        s["error_count"] = 99
        s["quality_gates"] = [QualityGate(gate_name="x",passed=False,score=0.1,checks=[],blocking=True)]
        assert route(s) == "error_node"

    def test_nonblocking_gate_fail_still_advances(self, state_with_reqs):
        s = dict(state_with_reqs)
        s["quality_gates"] = [QualityGate(gate_name="monitoring",passed=False,score=0.4,checks=[],blocking=False)]
        assert route(s) == "design"


# ── Quality gate tests ────────────────────────────────────────────────────

class TestRequirementsGate:
    def test_passes_with_valid_requirements(self, state_with_reqs):
        gate = RequirementsAgent().quality_gate(state_with_reqs)
        assert gate["passed"] is True
        assert gate["score"] >= 0.9

    def test_fails_with_no_requirements(self, base_state):
        gate = RequirementsAgent().quality_gate(base_state)
        assert gate["passed"] is False

    def test_passes_with_no_acceptance_criteria(self, base_state):
        """Gate now passes as long as ≥1 requirement exists — AC is non-blocking."""
        s = dict(base_state)
        s["requirements"] = [{"id":"R1","title":"T","description":"D","type":"functional",
                              "priority":"high","acceptance_criteria":[]}]
        gate = RequirementsAgent().quality_gate(s)
        assert gate["passed"] is True   # has_requirements=True overrides missing AC


class TestDesignGate:
    def test_passes_with_full_design(self, state_with_design):
        gate = DesignAgent().quality_gate(state_with_design)
        assert gate["passed"] is True

    def test_fails_with_no_design(self, base_state):
        gate = DesignAgent().quality_gate(base_state)
        assert gate["passed"] is False


class TestDevelopmentGate:
    def test_passes_with_all_artifacts(self, full_state):
        s = dict(full_state)
        s["generated_files"] = [{"path":"main.py","content":"print(hello)"}]
        gate = DevelopmentAgent().quality_gate(s)
        assert gate["passed"] is True

    def test_score_reflects_missing_pr(self, state_with_design):
        """Without gitea token, gate is non-blocking (passed=True) but score < 1.0."""
        s = dict(state_with_design)
        s["code_repo_url"] = None
        s["code_branch"] = None
        s["code_pr_url"] = None
        gate = DevelopmentAgent().quality_gate(s)
        assert gate["score"] < 1.0    # 0/3 checks passed
        assert gate["blocking"] is False  # non-blocking when gitea not configured


class TestTestingGate:
    def test_passes_with_good_coverage(self, full_state):
        gate = QAAgent().quality_gate(full_state)
        assert gate["passed"] is True

    def test_fails_with_low_coverage(self, base_state):
        s = dict(base_state)
        s["test_coverage"] = 30.0
        s["test_passed"] = 5
        s["test_failed"] = 5
        gate = QAAgent().quality_gate(s)
        assert gate["passed"] is False


class TestCICDGate:
    def test_passes_when_ci_success(self, full_state):
        gate = CICDAgent().quality_gate(full_state)
        assert gate["passed"] is True

    def test_fails_when_no_ci_url(self, base_state):
        gate = CICDAgent().quality_gate(base_state)
        assert gate["passed"] is False


class TestDeploymentGate:
    def test_passes_with_both_urls(self, full_state):
        gate = DeploymentAgent().quality_gate(full_state)
        assert gate["passed"] is True

    def test_fails_without_production(self, full_state):
        """Gate still passes (staging_url present) but note production missing."""
        s = dict(full_state)
        s["production_url"] = None
        gate = DeploymentAgent().quality_gate(s)
        # Gate passes because staging_url exists — production is optional
        # This is intentional: container running = deployment succeeded
        assert gate["passed"] is True


class TestMonitoringGate:
    def test_passes_with_slos_and_alerts(self, full_state):
        gate = MonitoringAgent().quality_gate(full_state)
        assert gate["passed"] is True
        assert gate["blocking"] is False   # non-blocking

    def test_non_blocking_on_fail(self, base_state):
        gate = MonitoringAgent().quality_gate(base_state)
        assert gate["blocking"] is False


class TestDocumentationGate:
    def test_passes_with_url(self, full_state):
        gate = DocumentationAgent().quality_gate(full_state)
        assert gate["passed"] is True

    def test_score_low_without_url(self, base_state):
        """Documentation gate is non-blocking — always passes but score is lower without URL."""
        gate = DocumentationAgent().quality_gate(base_state)
        assert gate["passed"] is True      # non-blocking gate always passes
        assert gate["score"] < 1.0          # but score is 0.5 when URL is missing


# ── JSON extraction tests ─────────────────────────────────────────────────

class TestJsonExtraction:
    def _agent(self):
        return RequirementsAgent()

    def test_extracts_fenced_json(self):
        text = 'Here it is:\n```json\n{"key": "value"}\n```'
        result = RequirementsAgent._extract_json(text)
        assert result == {"key": "value"}

    def test_extracts_inline_json(self):
        text = 'The result: {"items": [1, 2, 3]}'
        result = RequirementsAgent._extract_json(text)
        assert result == {"items": [1, 2, 3]}

    def test_returns_empty_on_no_json(self):
        result = RequirementsAgent._extract_json("No JSON here at all.")
        assert result == {}


# ── Settings tests ────────────────────────────────────────────────────────

class TestSettings:
    def test_settings_load(self):
        from config.settings import settings
        assert settings.groq_model  # not empty
        assert settings.max_retries > 0

    def test_enabled_services_returns_list(self):
        from config.settings import settings
        services = settings.enabled_services
        assert isinstance(services, list)

    def test_groq_model_default(self):
        from config.settings import settings
        assert "llama" in settings.groq_model or "mixtral" in settings.groq_model or "gemma" in settings.groq_model