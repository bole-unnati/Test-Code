"""src/agents/sdlc_agents.py — All 9 SDLC phase agents with real tool execution."""
from __future__ import annotations
import os as _os, sys as _sys
_b = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _b not in _sys.path: _sys.path.insert(0, _b)

import json
import re
import uuid
from src.agents.base import BaseSDLCAgent
from src.graph.state import SDLCState, QualityGate, Requirement, DesignArtifact, WorkItem

import os as _os

# ── Pipeline mode — read from settings (single source of truth) ───────────────
try:
    from config.settings import settings as _settings
    _MODE = _settings.effective_pipeline_mode
    _C    = _settings.pipeline_caps
except Exception:
    _DEMO_ENV = _os.environ.get("DEMO_MODE","").lower() in ("true","1","yes")
    _MODE     = "demo" if _DEMO_ENV else _os.environ.get("PIPELINE_MODE","standard").lower()
    _CAPS     = {
        "demo":     {"reqs":5,  "items":5,  "apis":5,  "models":3,  "files":4,  "push":5},
        "standard": {"reqs":15, "items":15, "apis":15, "models":8,  "files":15, "push":25},
        "full":     {"reqs":50, "items":50, "apis":50, "models":20, "files":50, "push":50},
    }
    _C = _CAPS.get(_MODE, _CAPS["standard"])

_DEMO = (_MODE == "demo")


def _cap(items: list) -> list:
    return items[:_C["reqs"]]

NL = chr(10)


async def _call_tool(tool_map: dict, tool_name: str, args: dict, log_entries: list) -> dict:
    if tool_name not in tool_map:
        print(f"  [TOOL] {tool_name}: NOT IN tool_map", flush=True)
        log_entries.append({"tool": tool_name, "error": "not in tool_map", "raw": ""})
        return {"error": "not in tool_map"}
    try:
        raw = await tool_map[tool_name].ainvoke(args)
        print(f"  [TOOL] {tool_name}: raw={str(raw)[:200]}", flush=True)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict) and parsed.get("error"):
            log_entries.append({"tool": tool_name, "error": parsed["error"], "raw": str(raw)[:300]})
        else:
            url = (parsed.get("url") or parsed.get("repo_url") or parsed.get("pr_url") or
                   parsed.get("release_url") or parsed.get("issue_url") or
                   parsed.get("external_id") or parsed.get("name") or str(parsed)[:80])
            log_entries.append({"tool": tool_name, "result": url, "raw": str(raw)[:300]})
        return parsed
    except Exception as e:
        print(f"  [TOOL] {tool_name}: EXCEPTION {e}", flush=True)
        log_entries.append({"tool": tool_name, "error": str(e), "raw": ""})
        return {"error": str(e)}


# ════════════════════════════════════════════════════════════════════════════
# GENERIC FALLBACK SCAFFOLD ENGINE
# Reads tech_stack, data_models, and api_contracts entirely from the design
# state — zero hardcoding of entity names, file counts, or language specifics.
# Supports any language/framework the LLM chose. Falls back gracefully when
# design is incomplete. Adds one file-set per entity, one route per endpoint.
# ════════════════════════════════════════════════════════════════════════════
def _generate_fallback_files(state: dict) -> list[dict]:
    """
    Fully generic scaffold: driven by design state, not hardcoded patterns.
    Works for any language/framework/entity combination the design phase produced.
    """
    # ── Read everything from design state — nothing hardcoded ────────────
    design  = state.get("design") or {}
    stack   = design.get("tech_stack", {})
    lang    = stack.get("language", "Python").lower()
    fw      = stack.get("framework", "").lower()
    db      = stack.get("database", "").lower()
    name    = state.get("project_name", "project")
    apis    = design.get("api_contracts", [])
    models  = design.get("data_models", [])

    # ── Detect language family — order matters; Node checked before Java
    # to prevent "java" substring matching "javascript" ──────────────────
    def _matches(*tokens: str) -> bool:
        combined = f"{lang} {fw}"
        return any(re.search(r'\b' + re.escape(t) + r'\b', combined) or t in combined
                   for t in tokens)

    is_node   = _matches("node", "express", "nestjs", "nest", "typescript", "javascript", "js")
    # Java uses word-boundary match to avoid hitting "javascript"
    is_java   = _matches("spring", "kotlin", "gradle", "maven") 
    #or
               # bool(re.search(r'\bjava\b', f"{lang} {fw}"))
            #)
    is_go     = _matches("golang", "gin", "echo", "fiber") or lang.strip().startswith("go")
    is_rust   = _matches("rust", "actix", "axum", "warp")
    is_dotnet = _matches("c#", "csharp", ".net", "dotnet", "aspnet", "asp.net", "blazor")
    is_ruby   = _matches("ruby", "rails", "sinatra")
    is_php    = _matches("php", "laravel", "symfony", "slim")
    is_python = (not any([is_java, is_node, is_go, is_rust, is_dotnet, is_ruby, is_php])
                 or _matches("python", "fastapi", "flask", "django", "starlette"))

    # ── Helpers shared across all languages ──────────────────────────────

    def _slug(s: str) -> str:
        """lowercase-hyphenated slug"""
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

    def _pascal(s: str) -> str:
        """PascalCase from any string"""
        return re.sub(r"[^a-zA-Z0-9]", " ", s).title().replace(" ", "")

    def _camel(s: str) -> str:
        """camelCase"""
        p = _pascal(s)
        return p[0].lower() + p[1:] if p else s

    def _snake(s: str) -> str:
        """snake_case"""
        return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    def _endpoint_list(indent: str = "") -> str:
        """Human-readable list of all API endpoints for README."""
        if not apis:
            return f"{indent}- GET /health"
        return NL.join(
            f"{indent}- {ep.get('method','GET')} {ep.get('path','')} — {ep.get('description','')}"
            for ep in apis
        )

    def _use_in_memory_db() -> bool:
        """True if no real DB configured or DB is explicitly in-memory."""
        return not db or any(x in db for x in ("h2", "sqlite", "memory", "in-memory", "inmemory"))

    # ── Collect all entities from data_models; fall back to project name ─
    raw_entities = [m.get("entity", "") for m in models if m.get("entity")]
    if not raw_entities:
        raw_entities = [_pascal(name)]  # synthesize one entity from project name

    files: list[dict] = []   # accumulate all files here

    # ════════════════════════════════════════════════════════════════════
    # JAVA / SPRING BOOT  (also Kotlin / Gradle — same file layout)
    # ════════════════════════════════════════════════════════════════════
    if is_java:
        pkg     = "com." + re.sub(r"[^a-z0-9]", "", name.lower())[:20]
        pkg_dir = "src/main/java/" + pkg.replace(".", "/")

        # ── Application entry point (always exactly one) ──────────────
        files.append({"path": f"{pkg_dir}/Application.java", "content": (
            f"package {pkg};" + NL + NL +
            "import org.springframework.boot.SpringApplication;" + NL +
            "import org.springframework.boot.autoconfigure.SpringBootApplication;" + NL + NL +
            "@SpringBootApplication" + NL +
            "public class Application {" + NL +
            "    public static void main(String[] args) {" + NL +
            f"        SpringApplication.run(Application.class, args);" + NL +
            "    }" + NL +
            "}" + NL
        )})

        # ── One Controller + Service + Repository + Entity per entity ─
        for entity_name in raw_entities:
            ent   = _pascal(entity_name)
            evar  = _camel(entity_name)
            etbl  = _snake(entity_name) + "s"
            ebase = f"/api/v1/{_slug(entity_name)}s"

            # Gather API endpoints that belong to this entity
            ent_apis = [
                ep for ep in apis
                if _slug(entity_name) in ep.get("path", "").lower()
                or entity_name.lower() in ep.get("description", "").lower()
            ] or apis  # fallback: all endpoints if none matched

            # Build extra @RequestMapping methods from API contracts
            extra_methods = ""
            seen_paths: set = set()
            for ep in ent_apis[:10]:
                m    = ep.get("method", "GET")
                path = ep.get("path", "")
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                ann  = {"GET": "GetMapping", "POST": "PostMapping",
                        "PUT": "PutMapping", "DELETE": "DeleteMapping"}.get(m, "GetMapping")
                fn   = re.sub(r"[^a-zA-Z0-9]", "_", path.strip("/")) or "root"
                ret  = ("ResponseEntity.ok(\"TODO\")" if m == "GET"
                        else "ResponseEntity.status(201).build()")
                extra_methods += f"    @{ann}(\"{path}\")" + NL
                extra_methods += f"    public ResponseEntity<?> {fn}() {{ return {ret}; }}" + NL + NL

            # Derive field declarations from data_models
            model_def = next((m for m in models if _pascal(m.get("entity","")) == ent), {})
            fields    = model_def.get("fields", [])
            java_fields = ""
            getset      = ""
            for f in fields:
                fname = _camel(f.get("name", "field"))
                if fname == "id":
                    continue
                ftype = _java_type(f.get("type", "String"))
                java_fields += f"    private {ftype} {fname};" + NL
                getset += (
                    f"    public {ftype} get{fname[0].upper()+fname[1:]}() {{ return {fname}; }}" + NL +
                    f"    public void set{fname[0].upper()+fname[1:]}({ftype} {fname}) {{ this.{fname} = {fname}; }}" + NL
                )
            if not java_fields:
                java_fields = "    private String name;" + NL
                getset = (
                    "    public String getName() { return name; }" + NL +
                    "    public void setName(String name) { this.name = name; }" + NL
                )

            # Controller
            files.append({"path": f"{pkg_dir}/controller/{ent}Controller.java", "content": (
                f"package {pkg}.controller;" + NL + NL +
                "import org.springframework.http.ResponseEntity;" + NL +
                "import org.springframework.web.bind.annotation.*;" + NL +
                f"import {pkg}.service.{ent}Service;" + NL +
                f"import {pkg}.model.{ent};" + NL +
                "import org.springframework.beans.factory.annotation.Autowired;" + NL +
                "import java.util.List;" + NL + NL +
                "@RestController" + NL +
                f'@RequestMapping("{ebase}")' + NL +
                f"public class {ent}Controller {{" + NL + NL +
                "    @Autowired" + NL +
                f"    private {ent}Service {evar}Service;" + NL + NL +
                '    @GetMapping("/health")' + NL +
                '    public ResponseEntity<String> health() { return ResponseEntity.ok("{\\"status\\":\\"ok\\"}"); }' + NL + NL +
                "    @GetMapping" + NL +
                f"    public ResponseEntity<List<{ent}>> findAll() {{ return ResponseEntity.ok({evar}Service.findAll()); }}" + NL + NL +
                '    @GetMapping("/{id}")' + NL +
                f"    public ResponseEntity<{ent}> findById(@PathVariable Long id) {{ return ResponseEntity.ok({evar}Service.findById(id)); }}" + NL + NL +
                "    @PostMapping" + NL +
                f"    public ResponseEntity<{ent}> create(@RequestBody {ent} body) {{ return ResponseEntity.status(201).body({evar}Service.create(body)); }}" + NL + NL +
                '    @PutMapping("/{id}")' + NL +
                f"    public ResponseEntity<{ent}> update(@PathVariable Long id, @RequestBody {ent} body) {{ return ResponseEntity.ok({evar}Service.update(id, body)); }}" + NL + NL +
                '    @DeleteMapping("/{id}")' + NL +
                f"    public ResponseEntity<Void> delete(@PathVariable Long id) {{ {evar}Service.delete(id); return ResponseEntity.noContent().build(); }}" + NL +
                "}" + NL
            )})

            # Service
            files.append({"path": f"{pkg_dir}/service/{ent}Service.java", "content": (
                f"package {pkg}.service;" + NL + NL +
                "import org.springframework.stereotype.Service;" + NL +
                "import org.springframework.beans.factory.annotation.Autowired;" + NL +
                f"import {pkg}.repository.{ent}Repository;" + NL +
                f"import {pkg}.model.{ent};" + NL +
                "import java.util.List;" + NL + NL +
                "@Service" + NL +
                f"public class {ent}Service {{" + NL + NL +
                "    @Autowired" + NL +
                f"    private {ent}Repository {evar}Repository;" + NL + NL +
                f"    public List<{ent}> findAll() {{ return {evar}Repository.findAll(); }}" + NL + NL +
                f"    public {ent} findById(Long id) {{" + NL +
                f"        return {evar}Repository.findById(id)" + NL +
                f"            .orElseThrow(() -> new RuntimeException(\"{ent} not found: \" + id));" + NL +
                "    }" + NL + NL +
                f"    public {ent} create({ent} e) {{ return {evar}Repository.save(e); }}" + NL + NL +
                f"    public {ent} update(Long id, {ent} e) {{ findById(id); e.setId(id); return {evar}Repository.save(e); }}" + NL + NL +
                f"    public void delete(Long id) {{ {evar}Repository.deleteById(id); }}" + NL +
                "}" + NL
            )})

            # Repository
            files.append({"path": f"{pkg_dir}/repository/{ent}Repository.java", "content": (
                f"package {pkg}.repository;" + NL + NL +
                "import org.springframework.data.jpa.repository.JpaRepository;" + NL +
                "import org.springframework.stereotype.Repository;" + NL +
                f"import {pkg}.model.{ent};" + NL + NL +
                "@Repository" + NL +
                f"public interface {ent}Repository extends JpaRepository<{ent}, Long> {{" + NL +
                "}" + NL
            )})

            # Entity / Model
            files.append({"path": f"{pkg_dir}/model/{ent}.java", "content": (
                f"package {pkg}.model;" + NL + NL +
                "import jakarta.persistence.*;" + NL +
                "import java.time.LocalDateTime;" + NL + NL +
                "@Entity" + NL +
                f'@Table(name = "{etbl}")' + NL +
                f"public class {ent} {{" + NL + NL +
                "    @Id @GeneratedValue(strategy = GenerationType.IDENTITY)" + NL +
                "    private Long id;" + NL + NL +
                java_fields + NL +
                "    private LocalDateTime createdAt = LocalDateTime.now();" + NL +
                "    private LocalDateTime updatedAt = LocalDateTime.now();" + NL + NL +
                "    @PreUpdate public void preUpdate() { this.updatedAt = LocalDateTime.now(); }" + NL + NL +
                "    public Long getId() { return id; }" + NL +
                "    public void setId(Long id) { this.id = id; }" + NL +
                getset +
                "    public LocalDateTime getCreatedAt() { return createdAt; }" + NL +
                "    public LocalDateTime getUpdatedAt() { return updatedAt; }" + NL +
                "}" + NL
            )})

        # ── application.properties — uses H2 unless design specified real DB ─
        use_h2 = _use_in_memory_db() or "postgresql" not in db and "mysql" not in db and "mongo" not in db
        props  = (
            f"spring.application.name={_slug(name)}" + NL +
            "server.port=8080" + NL
        )
        if use_h2:
            props += (
                "spring.datasource.url=jdbc:h2:mem:testdb;DB_CLOSE_DELAY=-1" + NL +
                "spring.datasource.driver-class-name=org.h2.Driver" + NL +
                "spring.datasource.username=sa" + NL +
                "spring.datasource.password=" + NL +
                "spring.jpa.database-platform=org.hibernate.dialect.H2Dialect" + NL +
                "spring.jpa.hibernate.ddl-auto=create-drop" + NL +
                "spring.h2.console.enabled=true" + NL
            )
        else:
            props += (
                f"spring.datasource.url=jdbc:{db}://localhost:5432/{_slug(name)}" + NL +
                "spring.datasource.username=${DB_USER:sa}" + NL +
                "spring.datasource.password=${DB_PASS:}" + NL +
                "spring.jpa.hibernate.ddl-auto=update" + NL
            )
        props += f"logging.level.{pkg}=DEBUG" + NL
        files.append({"path": "src/main/resources/application.properties", "content": props})

        # ── pom.xml — includes JPA + H2 always (H2 scope=test if real DB) ──
        h2_scope = "runtime" if use_h2 else "test"
        files.append({"path": "pom.xml", "content": (
            '<?xml version="1.0" encoding="UTF-8"?>' + NL +
            '<project xmlns="http://maven.apache.org/POM/4.0.0"' + NL +
            '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' + NL +
            '  xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">' + NL +
            "  <modelVersion>4.0.0</modelVersion>" + NL +
            "  <parent><groupId>org.springframework.boot</groupId>" +
            "<artifactId>spring-boot-starter-parent</artifactId><version>3.2.0</version></parent>" + NL +
            f"  <groupId>{pkg}</groupId>" + NL +
            f"  <artifactId>{_slug(name)}</artifactId>" + NL +
            "  <version>1.0.0</version>" + NL +
            "  <properties><java.version>17</java.version></properties>" + NL +
            "  <dependencies>" + NL +
            "    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>" + NL +
            "    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-data-jpa</artifactId></dependency>" + NL +
            f"    <dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><scope>{h2_scope}</scope></dependency>" + NL +
            "    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency>" + NL +
            "  </dependencies>" + NL +
            "  <build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>" + NL +
            "</project>" + NL
        )})

        # ── Dockerfile ──────────────────────────────────────────────────
        files.append({"path": "Dockerfile", "content": (
            "FROM maven:3.9-eclipse-temurin-17 AS build" + NL +
            "WORKDIR /app" + NL +
            "COPY pom.xml ." + NL +
            "RUN mvn dependency:go-offline -q" + NL +
            "COPY src ./src" + NL +
            "RUN mvn package -DskipTests -q" + NL + NL +
            "FROM eclipse-temurin:17-jre-alpine" + NL +
            "WORKDIR /app" + NL +
            "COPY --from=build /app/target/*.jar app.jar" + NL +
            "EXPOSE 8080" + NL +
            'CMD ["java", "-jar", "app.jar"]' + NL
        )})

        # ── README ──────────────────────────────────────────────────────
        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL +
            f"Spring Boot REST API. Tech: {stack.get('language','Java')} / {stack.get('framework','Spring Boot')} / {db or 'H2'}" + NL + NL +
            "## Quick Start" + NL +
            "```bash" + NL + "mvn spring-boot:run" + NL +
            "# or" + NL + "docker build -t app . && docker run -p 8080:8080 app" + NL + "```" + NL + NL +
            "## API Endpoints" + NL + _endpoint_list() + NL
        )})

        return files

    # ════════════════════════════════════════════════════════════════════
    # NODE.JS / EXPRESS / NESTJS / TYPESCRIPT
    # ════════════════════════════════════════════════════════════════════
    if is_node:
        is_ts   = "typescript" in lang or "ts" == lang or "nest" in fw
        ext     = "ts" if is_ts else "js"
        runtime = "ts-node" if is_ts else "node"

        # ── index / main entrypoint ──────────────────────────────────
        route_mounts = NL.join(
            f"app.use('/api/v1/{_slug(ent)}s', require('./routes/{_camel(ent)}Routes'));"
            for ent in raw_entities
        )
        files.append({"path": f"index.{ext}", "content": (
            "const express = require('express');" + NL +
            "const app = express();" + NL +
            "app.use(express.json());" + NL + NL +
            "app.get('/health', (req, res) => res.json({ status: 'ok', ts: new Date() }));" + NL +
            route_mounts + NL + NL +
            "const PORT = process.env.PORT || 3000;" + NL +
            "const server = app.listen(PORT, () => console.log(`Server running on port ${PORT}`));" + NL +
            "module.exports = { app, server };" + NL
        )})

        # ── One routes + controller + service file per entity ────────
        for entity_name in raw_entities:
            ent  = _pascal(entity_name)
            evar = _camel(entity_name)
            esl  = _slug(entity_name)

            # Collect endpoints for this entity
            ent_apis = [ep for ep in apis if esl in ep.get("path","").lower()] or apis

            files.append({"path": f"routes/{evar}Routes.{ext}", "content": (
                "const express = require('express');" + NL +
                "const router = express.Router();" + NL +
                f"const ctrl = require('../controllers/{evar}Controller');" + NL + NL +
                "router.get('/',      ctrl.findAll);" + NL +
                "router.get('/:id',   ctrl.findById);" + NL +
                "router.post('/',     ctrl.create);" + NL +
                "router.put('/:id',   ctrl.update);" + NL +
                "router.delete('/:id',ctrl.remove);" + NL +
                NL.join(
                    f"router.{ep.get('method','get').lower()}('{ep.get('path','')}', (req,res)=>res.json({{status:'ok'}}) );"
                    for ep in ent_apis[:5]
                ) + NL +
                "module.exports = router;" + NL
            )})

            files.append({"path": f"controllers/{evar}Controller.{ext}", "content": (
                f"const svc = require('../services/{evar}Service');" + NL + NL +
                f"exports.findAll  = async (req,res) => {{ try {{ res.json(await svc.findAll()); }} catch(e){{ res.status(500).json({{error:e.message}}); }} }};" + NL +
                f"exports.findById = async (req,res) => {{ try {{ res.json(await svc.findById(req.params.id)); }} catch(e){{ res.status(404).json({{error:e.message}}); }} }};" + NL +
                f"exports.create   = async (req,res) => {{ try {{ res.status(201).json(await svc.create(req.body)); }} catch(e){{ res.status(400).json({{error:e.message}}); }} }};" + NL +
                f"exports.update   = async (req,res) => {{ try {{ res.json(await svc.update(req.params.id,req.body)); }} catch(e){{ res.status(400).json({{error:e.message}}); }} }};" + NL +
                f"exports.remove   = async (req,res) => {{ try {{ await svc.remove(req.params.id); res.status(204).send(); }} catch(e){{ res.status(404).json({{error:e.message}}); }} }};" + NL
            )})

            files.append({"path": f"services/{evar}Service.{ext}", "content": (
                f"// In-memory store for {ent} — replace with DB layer" + NL +
                f"let _{evar}s = []; let _id = 1;" + NL + NL +
                f"exports.findAll  = async () => _{evar}s;" + NL +
                f"exports.findById = async id => {{ const r = _{evar}s.find(i=>i.id==id); if(!r) throw new Error('{ent} not found'); return r; }};" + NL +
                f"exports.create   = async data => {{ const r={{id:_id++,...data,createdAt:new Date()}}; _{evar}s.push(r); return r; }};" + NL +
                f"exports.update   = async (id,data) => {{ const i=_{evar}s.findIndex(x=>x.id==id); if(i<0) throw new Error('Not found'); _{evar}s[i]={{..._{evar}s[i],...data,updatedAt:new Date()}}; return _{evar}s[i]; }};" + NL +
                f"exports.remove   = async id => {{ _{evar}s=_{evar}s.filter(x=>x.id!=id); }};" + NL
            )})

        # ── package.json ─────────────────────────────────────────────
        deps = {"express": "^4.18.0"}
        dev_deps = {"jest": "^29.0.0", "supertest": "^6.3.0"}
        if is_ts:
            dev_deps.update({"typescript": "^5.0.0", "@types/express": "^4.17.0", "ts-node": "^10.9.0"})
        files.append({"path": "package.json", "content": json.dumps({
            "name": _slug(name), "version": "1.0.0", "main": f"index.{ext}",
            "scripts": {"start": f"{runtime} index.{ext}", "test": "jest --coverage"},
            "dependencies": deps, "devDependencies": dev_deps,
        }, indent=2) + NL})

        # ── Test ─────────────────────────────────────────────────────
        test_cases = NL.join(
            f"  test('GET /api/v1/{_slug(ent)}s returns 200', async () => {{\n"
            f"    const r = await request(app).get('/api/v1/{_slug(ent)}s');\n"
            f"    expect(r.status).toBe(200);\n  }});"
            for ent in raw_entities
        )
        files.append({"path": f"__tests__/app.test.{ext}", "content": (
            "const request = require('supertest');" + NL +
            "const { app, server } = require('../index');" + NL +
            "afterAll(() => server.close());" + NL + NL +
            "test('health check', async () => {" + NL +
            "  const r = await request(app).get('/health');" + NL +
            "  expect(r.status).toBe(200);" + NL +
            "});" + NL + NL +
            test_cases + NL
        )})

        # ── Dockerfile ───────────────────────────────────────────────
        files.append({"path": "Dockerfile", "content": (
            "FROM node:20-alpine" + NL +
            "WORKDIR /app" + NL +
            "COPY package*.json ./" + NL +
            "RUN npm install --omit=dev" + NL +   # ← npm install works without package-lock.json
            "COPY . ." + NL +
            "EXPOSE 3000" + NL +
            f'CMD ["{runtime}", "index.{ext}"]' + NL
        )})

        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL +
            f"{'TypeScript' if is_ts else 'Node.js'} / {stack.get('framework','Express')} REST API." + NL + NL +
            "## Quick Start" + NL +
            "```bash" + NL + "npm install && npm start" + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL
        )})
        return files

    # ════════════════════════════════════════════════════════════════════
    # GO  (Gin, Echo, Fiber, or stdlib net/http)
    # ════════════════════════════════════════════════════════════════════
    if is_go:
        mod  = _slug(name)
        gfw  = "gin" if "gin" in fw else ("echo" if "echo" in fw else ("fiber" if "fiber" in fw else "net/http"))

        # main.go
        route_regs = NL.join(
            f'    r.{ep.get("method","GET").upper()[0]+ep.get("method","GET")[1:].lower()}("{ep.get("path","/api")}", handlers.Handle{_pascal(ep.get("path","/").strip("/").replace("/","_") or "Index")})'
            for ep in apis[:8]
        ) or '    r.GET("/health", handlers.Health)'

        files.append({"path": "main.go", "content": (
            'package main' + NL + NL +
            'import (' + NL +
            f'    "{mod}/handlers"' + NL +
            ('    "github.com/gin-gonic/gin"' if gfw == "gin" else '    "net/http"') + NL +
            ')' + NL + NL +
            'func main() {' + NL +
            ('    r := gin.Default()' if gfw == "gin" else '    mux := http.NewServeMux()') + NL +
            route_regs + NL +
            ('    r.Run(":8080")' if gfw == "gin" else '    http.ListenAndServe(":8080", mux)') + NL +
            '}' + NL
        )})

        # One handler + model file per entity
        for entity_name in raw_entities:
            ent  = _pascal(entity_name)
            evar = entity_name.lower()
            files.append({"path": f"handlers/{evar}.go", "content": (
                'package handlers' + NL + NL +
                'import (' + NL +
                '    "encoding/json"' + NL +
                '    "net/http"' + NL +
                f'    "{mod}/models"' + NL +
                ')' + NL + NL +
                'func Health(w http.ResponseWriter, r *http.Request) {' + NL +
                '    json.NewEncoder(w).Encode(map[string]string{"status":"ok"})' + NL +
                '}' + NL + NL +
                f'var {evar}Store []models.{ent}' + NL + NL +
                f'func List{ent}s(w http.ResponseWriter, r *http.Request) {{' + NL +
                '    w.Header().Set("Content-Type", "application/json")' + NL +
                f'    json.NewEncoder(w).Encode({evar}Store)' + NL +
                '}' + NL + NL +
                f'func Create{ent}(w http.ResponseWriter, r *http.Request) {{' + NL +
                f'    var item models.{ent}' + NL +
                '    json.NewDecoder(r.Body).Decode(&item)' + NL +
                f'    {evar}Store = append({evar}Store, item)' + NL +
                '    w.WriteHeader(http.StatusCreated)' + NL +
                '    json.NewEncoder(w).Encode(item)' + NL +
                '}' + NL
            )})

            model_def = next((m for m in models if _pascal(m.get("entity","")) == ent), {})
            fields    = model_def.get("fields", [])
            go_fields = NL.join(
                f"    {_pascal(f.get('name','Field'))} {_go_type(f.get('type','string'))} `json:\"{f.get('name','field')}\"`"
                for f in fields[:10]
            ) or '    ID   int    `json:"id"`' + NL + '    Name string `json:"name"`'
            files.append({"path": f"models/{evar}.go", "content": (
                'package models' + NL + NL +
                f'type {ent} struct {{' + NL +
                go_fields + NL +
                '}' + NL
            )})

        files.append({"path": "go.mod", "content": (
            f'module {mod}' + NL + NL + 'go 1.21' + NL +
            (f'\nrequire github.com/gin-gonic/gin v1.9.0\n' if gfw == "gin" else "")
        )})
        files.append({"path": "Dockerfile", "content": (
            "FROM golang:1.21-alpine AS build" + NL +
            "WORKDIR /app" + NL + "COPY . ." + NL +
            "RUN go build -o app ." + NL + NL +
            "FROM alpine:latest" + NL +
            "WORKDIR /app" + NL +
            "COPY --from=build /app/app ." + NL +
            "EXPOSE 8080" + NL + 'CMD ["./app"]' + NL
        )})
        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL +
            f"Go / {gfw} REST API." + NL + NL +
            "```bash" + NL + "go run . " + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL
        )})
        return files

    # ════════════════════════════════════════════════════════════════════
    # .NET / ASP.NET CORE (C#)
    # ════════════════════════════════════════════════════════════════════
    if is_dotnet:
        ns = _pascal(name)
        for entity_name in raw_entities:
            ent  = _pascal(entity_name)
            evar = _camel(entity_name)

            model_def = next((m for m in models if _pascal(m.get("entity","")) == ent), {})
            fields    = model_def.get("fields", [])
            cs_props  = NL.join(
                f"    public {_cs_type(f.get('type','string'))} {_pascal(f.get('name','Field'))} {{ get; set; }}"
                for f in fields[:10]
                if f.get("name") and f.get("name").lower() != "id"
            ) or "    public string Name { get; set; } = string.Empty;"

            files.append({"path": f"Models/{ent}.cs", "content": (
                f"namespace {ns}.Models;" + NL + NL +
                f"public class {ent} {{" + NL +
                "    public int Id { get; set; }" + NL +
                cs_props + NL +
                "    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;" + NL +
                "}" + NL
            )})
            files.append({"path": f"Controllers/{ent}Controller.cs", "content": (
                "using Microsoft.AspNetCore.Mvc;" + NL +
                f"using {ns}.Models;" + NL + NL +
                f"namespace {ns}.Controllers;" + NL + NL +
                "[ApiController]" + NL +
                f'[Route("api/v1/{_slug(entity_name)}s")]' + NL +
                f"public class {ent}Controller : ControllerBase {{" + NL +
                f"    private static readonly List<{ent}> _store = new();" + NL +
                "    private static int _nextId = 1;" + NL + NL +
                "    [HttpGet] public IActionResult GetAll() => Ok(_store);" + NL +
                "    [HttpGet(\"{id}\")] public IActionResult Get(int id) {" + NL +
                "        var item = _store.FirstOrDefault(x => x.Id == id);" + NL +
                "        return item is null ? NotFound() : Ok(item);" + NL +
                "    }" + NL +
                f"    [HttpPost] public IActionResult Create([FromBody] {ent} item) {{" + NL +
                "        item.Id = _nextId++; _store.Add(item);" + NL +
                "        return CreatedAtAction(nameof(Get), new { id = item.Id }, item);" + NL +
                "    }" + NL +
                "    [HttpPut(\"{id}\")] public IActionResult Update(int id, [FromBody] " + ent + " item) {" + NL +
                "        var idx = _store.FindIndex(x => x.Id == id);" + NL +
                "        if (idx < 0) return NotFound(); item.Id = id; _store[idx] = item; return Ok(item);" + NL +
                "    }" + NL +
                "    [HttpDelete(\"{id}\")] public IActionResult Delete(int id) {" + NL +
                "        _store.RemoveAll(x => x.Id == id); return NoContent();" + NL +
                "    }" + NL +
                "}" + NL
            )})

        files.append({"path": "Program.cs", "content": (
            "var builder = WebApplication.CreateBuilder(args);" + NL +
            "builder.Services.AddControllers();" + NL +
            "builder.Services.AddEndpointsApiExplorer();" + NL +
            "builder.Services.AddSwaggerGen();" + NL + NL +
            "var app = builder.Build();" + NL +
            "app.UseSwagger(); app.UseSwaggerUI();" + NL +
            "app.MapControllers();" + NL +
            'app.MapGet("/health", () => Results.Ok(new { status = "ok" }));' + NL +
            "app.Run();" + NL
        )})
        files.append({"path": f"{ns}.csproj", "content": (
            '<Project Sdk="Microsoft.NET.Sdk.Web">' + NL +
            "  <PropertyGroup>" + NL +
            "    <TargetFramework>net8.0</TargetFramework>" + NL +
            "    <Nullable>enable</Nullable>" + NL +
            "    <ImplicitUsings>enable</ImplicitUsings>" + NL +
            "  </PropertyGroup>" + NL +
            "  <ItemGroup>" + NL +
            '    <PackageReference Include="Swashbuckle.AspNetCore" Version="6.5.0" />' + NL +
            "  </ItemGroup>" + NL +
            "</Project>" + NL
        )})
        files.append({"path": "Dockerfile", "content": (
            "FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build" + NL +
            "WORKDIR /app" + NL + "COPY . ." + NL +
            "RUN dotnet publish -c Release -o out" + NL + NL +
            "FROM mcr.microsoft.com/dotnet/aspnet:8.0" + NL +
            "WORKDIR /app" + NL + "COPY --from=build /app/out ." + NL +
            f"ENTRYPOINT [\"dotnet\", \"{ns}.dll\"]" + NL
        )})
        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL +
            f"ASP.NET Core 8 REST API." + NL + NL +
            "```bash" + NL + "dotnet run" + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL
        )})
        return files

    # ════════════════════════════════════════════════════════════════════
    # RUBY / RAILS / SINATRA
    # ════════════════════════════════════════════════════════════════════
    if is_ruby:
        for entity_name in raw_entities:
            ent  = _pascal(entity_name)
            evar = _snake(entity_name)
            files.append({"path": f"app/controllers/{evar}s_controller.rb", "content": (
                f"class {ent}sController < ApplicationController" + NL +
                "  before_action :set_item, only: [:show, :update, :destroy]" + NL + NL +
                "  def index; render json: @items; end" + NL +
                "  def show; render json: @item; end" + NL +
                "  def create" + NL +
                f"    @item = {ent}.new(item_params)" + NL +
                "    if @item.save then render json: @item, status: :created" + NL +
                "    else render json: @item.errors, status: :unprocessable_entity; end" + NL +
                "  end" + NL +
                "  def update" + NL +
                "    if @item.update(item_params) then render json: @item" + NL +
                "    else render json: @item.errors, status: :unprocessable_entity; end" + NL +
                "  end" + NL +
                "  def destroy; @item.destroy; head :no_content; end" + NL + NL +
                "  private" + NL +
                f"  def set_item; @item = {ent}.find(params[:id]); end" + NL +
                f"  def item_params; params.require(:{evar}).permit(:name); end" + NL +
                "end" + NL
            )})
            files.append({"path": f"app/models/{evar}.rb", "content": (
                f"class {ent} < ApplicationRecord" + NL +
                "  validates :name, presence: true" + NL +
                "end" + NL
            )})

        files.append({"path": "Gemfile", "content": (
            'source "https://rubygems.org"' + NL +
            'gem "rails", "~> 7.1"' + NL +
            'gem "sqlite3", "~> 1.7"' + NL +
            'gem "puma"' + NL
        )})
        files.append({"path": "Dockerfile", "content": (
            "FROM ruby:3.3-alpine" + NL +
            "WORKDIR /app" + NL + "COPY Gemfile* ./" + NL +
            "RUN bundle install" + NL + "COPY . ." + NL +
            "EXPOSE 3000" + NL + 'CMD ["rails", "server", "-b", "0.0.0.0"]' + NL
        )})
        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL + "Ruby on Rails REST API." + NL + NL +
            "```bash" + NL + "bundle install && rails server" + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL
        )})
        return files

    # ════════════════════════════════════════════════════════════════════
    # PHP / LARAVEL / SYMFONY
    # ════════════════════════════════════════════════════════════════════
    if is_php:
        for entity_name in raw_entities:
            ent  = _pascal(entity_name)
            evar = _camel(entity_name)
            files.append({"path": f"app/Http/Controllers/{ent}Controller.php", "content": (
                "<?php" + NL +
                "namespace App\\Http\\Controllers;" + NL + NL +
                "use Illuminate\\Http\\Request;" + NL +
                f"use App\\Models\\{ent};" + NL + NL +
                f"class {ent}Controller extends Controller {{" + NL +
                f"    public function index() {{ return {ent}::all(); }}" + NL +
                f"    public function show($id) {{ return {ent}::findOrFail($id); }}" + NL +
                "    public function store(Request $request) {" + NL +
                f"        $item = {ent}::create($request->all());" + NL +
                "        return response()->json($item, 201);" + NL +
                "    }" + NL +
                "    public function update(Request $request, $id) {" + NL +
                f"        $item = {ent}::findOrFail($id);" + NL +
                "        $item->update($request->all()); return $item;" + NL +
                "    }" + NL +
                "    public function destroy($id) {" + NL +
                f"        {ent}::findOrFail($id)->delete();" + NL +
                "        return response()->json(null, 204);" + NL +
                "    }" + NL +
                "}" + NL
            )})
            files.append({"path": f"app/Models/{ent}.php", "content": (
                "<?php" + NL +
                "namespace App\\Models;" + NL + NL +
                "use Illuminate\\Database\\Eloquent\\Model;" + NL + NL +
                f"class {ent} extends Model {{" + NL +
                "    protected $guarded = [];" + NL +
                "}" + NL
            )})

        files.append({"path": "Dockerfile", "content": (
            "FROM php:8.3-fpm-alpine" + NL +
            "WORKDIR /var/www" + NL + "COPY . ." + NL +
            "RUN composer install --no-dev" + NL +
            "EXPOSE 9000" + NL + 'CMD ["php-fpm"]' + NL
        )})
        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL + "PHP / Laravel REST API." + NL + NL +
            "```bash" + NL + "composer install && php artisan serve" + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL
        )})
        return files

    # ════════════════════════════════════════════════════════════════════
    # RUST  (Axum / Actix)
    # ════════════════════════════════════════════════════════════════════
    if is_rust:
        rfw = "axum" if "axum" in fw else "actix-web"
        files.append({"path": "src/main.rs", "content": (
            "use std::sync::{Arc, Mutex};" + NL +
            "use axum::{routing::get, Router, Json, extract::State};" + NL +
            "use serde::{Deserialize, Serialize};" + NL + NL +
            "#[derive(Clone, Serialize, Deserialize)]" + NL +
            "pub struct HealthResponse { pub status: String }" + NL + NL +
            "async fn health() -> Json<HealthResponse> {" + NL +
            '    Json(HealthResponse { status: "ok".to_string() })' + NL +
            "}" + NL + NL +
            "#[tokio::main]" + NL +
            "async fn main() {" + NL +
            '    let app = Router::new().route("/health", get(health));' + NL +
            '    let listener = tokio::net::TcpListener::bind("0.0.0.0:8080").await.unwrap();' + NL +
            "    axum::serve(listener, app).await.unwrap();" + NL +
            "}" + NL
        )})
        for entity_name in raw_entities:
            ent  = _pascal(entity_name)
            evar = _snake(entity_name)
            files.append({"path": f"src/{evar}.rs", "content": (
                "use serde::{Deserialize, Serialize};" + NL + NL +
                "#[derive(Clone, Debug, Serialize, Deserialize)]" + NL +
                f"pub struct {ent} {{" + NL +
                "    pub id: u64," + NL +
                "    pub name: String," + NL +
                "}" + NL
            )})

        files.append({"path": "Cargo.toml", "content": (
            f'[package]\nname = "{_slug(name)}"\nversion = "0.1.0"\nedition = "2021"\n\n'
            f'[dependencies]\naxum = "0.7"\ntokio = {{ version = "1", features = ["full"] }}\n'
            'serde = { version = "1", features = ["derive"] }\nserde_json = "1"\n'
        )})
        files.append({"path": "Dockerfile", "content": (
            "FROM rust:1.75-slim AS build" + NL +
            "WORKDIR /app" + NL + "COPY . ." + NL +
            "RUN cargo build --release" + NL + NL +
            "FROM debian:bookworm-slim" + NL +
            "WORKDIR /app" + NL +
            f"COPY --from=build /app/target/release/{_slug(name)} ." + NL +
            "EXPOSE 8080" + NL +
            f'CMD ["./{_slug(name)}"]' + NL
        )})
        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL + f"Rust / {rfw} REST API." + NL + NL +
            "```bash" + NL + "cargo run" + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL
        )})
        return files

    # ════════════════════════════════════════════════════════════════════
    # PYTHON — FastAPI (default) / Flask / Django
    # ════════════════════════════════════════════════════════════════════
    # Determine sub-framework
    is_flask  = "flask" in fw
    is_django = "django" in fw

    if is_django:
        app_name = _slug(name).replace("-", "_")
        for entity_name in raw_entities:
            ent   = _pascal(entity_name)
            evar  = _snake(entity_name)
            model_def = next((m for m in models if _pascal(m.get("entity","")) == ent), {})
            fields    = model_def.get("fields", [])
            dj_fields = NL.join(
                f"    {_snake(f.get('name','field'))} = models.{_django_field(f.get('type','string'))}()"
                for f in fields[:10]
                if f.get("name") and f.get("name").lower() != "id"
            ) or "    name = models.CharField(max_length=255)"

            files.append({"path": f"{app_name}/models.py", "content": (
                "from django.db import models" + NL + NL +
                f"class {ent}(models.Model):" + NL +
                dj_fields + NL +
                "    created_at = models.DateTimeField(auto_now_add=True)" + NL +
                "    updated_at = models.DateTimeField(auto_now=True)" + NL + NL +
                "    class Meta:" + NL +
                f"        db_table = '{evar}s'" + NL
            )})
            files.append({"path": f"{app_name}/views.py", "content": (
                "from django.http import JsonResponse" + NL +
                "from django.views import View" + NL +
                "import json" + NL + NL +
                f"class {ent}View(View):" + NL +
                "    def get(self, request, pk=None):" + NL +
                f"        items = list({ent}.objects.values())" + NL +
                "        return JsonResponse(items, safe=False)" + NL +
                "    def post(self, request):" + NL +
                "        data = json.loads(request.body)" + NL +
                f"        item = {ent}.objects.create(**data)" + NL +
                "        return JsonResponse({'id': item.pk}, status=201)" + NL
            )})

        files.append({"path": "manage.py", "content": (
            "#!/usr/bin/env python" + NL +
            'import os, sys' + NL +
            "def main():" + NL +
            f"    os.environ.setdefault('DJANGO_SETTINGS_MODULE', '{app_name}.settings')" + NL +
            "    from django.core.management import execute_from_command_line" + NL +
            "    execute_from_command_line(sys.argv)" + NL +
            "if __name__ == '__main__': main()" + NL
        )})
        files.append({"path": "requirements.txt", "content": "django>=4.2\ngunicorn\n"})
        files.append({"path": "Dockerfile", "content": (
            "FROM python:3.12-slim" + NL + "WORKDIR /app" + NL +
            "COPY requirements.txt ." + NL + "RUN pip install -r requirements.txt --no-cache-dir" + NL +
            "COPY . ." + NL + "EXPOSE 8000" + NL +
            'CMD ["gunicorn", "--bind", "0.0.0.0:8000", "' + app_name + '.wsgi"]' + NL
        )})
        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL + "Django REST API." + NL + NL +
            "```bash" + NL + "pip install -r requirements.txt" + NL +
            "python manage.py migrate && python manage.py runserver" + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL
        )})
        return files

    if is_flask:
        router_blocks = ""
        for entity_name in raw_entities:
            ent  = _pascal(entity_name)
            evar = _snake(entity_name)
            router_blocks += (
                f"\n# ── {ent} ──\n"
                f"_{evar}s = []; _id_{evar} = 1\n\n"
                f"@app.route('/api/v1/{_slug(entity_name)}s', methods=['GET'])\n"
                f"def list_{evar}s(): return jsonify(_{evar}s)\n\n"
                f"@app.route('/api/v1/{_slug(entity_name)}s', methods=['POST'])\n"
                f"def create_{evar}():\n"
                f"    global _id_{evar}\n"
                f"    item = {{**request.json, 'id': _id_{evar}}}; _id_{evar} += 1\n"
                f"    _{evar}s.append(item); return jsonify(item), 201\n\n"
                f"@app.route('/api/v1/{_slug(entity_name)}s/<int:item_id>', methods=['GET','PUT','DELETE'])\n"
                f"def {evar}(item_id):\n"
                f"    item = next((i for i in _{evar}s if i['id']==item_id), None)\n"
                "    if not item: return jsonify({'error':'not found'}), 404\n"
                "    if request.method=='DELETE':\n"
                f"        _{evar}s[:] = [i for i in _{evar}s if i['id']!=item_id]; return '', 204\n"
                "    if request.method=='PUT': item.update(request.json)\n"
                "    return jsonify(item)\n"
            )
        files.append({"path": "app.py", "content": (
            "from flask import Flask, jsonify, request" + NL +
            "app = Flask(__name__)" + NL + NL +
            "@app.route('/health')" + NL +
            "def health(): return jsonify({'status':'ok'})" + NL +
            router_blocks + NL +
            "if __name__ == '__main__': app.run(debug=True, host='0.0.0.0', port=5000)" + NL
        )})
        files.append({"path": "requirements.txt", "content": "flask>=3.0\ngunicorn\npytest\nhttpx\n"})
        files.append({"path": "tests/test_app.py", "content": (
            "import pytest" + NL +
            "from app import app" + NL + NL +
            "@pytest.fixture" + NL +
            "def client(): app.config['TESTING'] = True; return app.test_client()" + NL + NL +
            "def test_health(client):" + NL +
            "    r = client.get('/health')" + NL +
            "    assert r.status_code == 200" + NL
        )})
        files.append({"path": "Dockerfile", "content": (
            "FROM python:3.12-slim" + NL + "WORKDIR /app" + NL +
            "COPY requirements.txt ." + NL + "RUN pip install -r requirements.txt --no-cache-dir" + NL +
            "COPY . ." + NL + "EXPOSE 5000" + NL +
            'CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]' + NL
        )})
        files.append({"path": "README.md", "content": (
            f"# {name}" + NL + NL + "Flask REST API." + NL + NL +
            "```bash" + NL + "pip install -r requirements.txt && python app.py" + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL
        )})
        return files

    # ── FastAPI (default Python) — one router file per entity ────────
    router_imports  = ""
    routers_include = ""
    router_files_local: list[dict] = []

    for entity_name in raw_entities:
        ent   = _pascal(entity_name)
        evar  = _snake(entity_name)
        esl   = _slug(entity_name)
        model_def = next((m for m in models if _pascal(m.get("entity","")) == ent), {})
        fields    = model_def.get("fields", [])
        field_defs = NL.join(
            f"    {_snake(f.get('name','field'))}: {_py_type(f.get('type','string'))}"
            for f in fields[:10]
            if f.get("name") and f.get("name").lower() != "id"
        ) or "    name: str"

        router_imports  += f"from routers import {evar}" + NL
        routers_include += f'app.include_router({evar}.router, prefix="/api/v1/{esl}s", tags=["{ent}"])' + NL
        router_files_local.append({"path": f"routers/{evar}.py", "content": (
            "from fastapi import APIRouter, HTTPException" + NL +
            "from pydantic import BaseModel" + NL +
            "from typing import List" + NL +
            "from datetime import datetime" + NL + NL +
            f"router = APIRouter()" + NL + NL +
            f"class {ent}Schema(BaseModel):" + NL +
            field_defs + NL + NL +
            f"class {ent}Response({ent}Schema):" + NL +
            "    id: int" + NL +
            "    created_at: datetime = datetime.utcnow()" + NL + NL +
            f"_store: List[dict] = []" + NL +
            f"_next_id = 1" + NL + NL +
            f'@router.get("/", response_model=List[{ent}Response])' + NL +
            f"async def list_{evar}s(): return _store" + NL + NL +
            f'@router.get("/{{item_id}}", response_model={ent}Response)' + NL +
            f"async def get_{evar}(item_id: int):" + NL +
            f"    item = next((i for i in _store if i['id']==item_id), None)" + NL +
            f"    if not item: raise HTTPException(404, '{ent} not found')" + NL +
            f"    return item" + NL + NL +
            f'@router.post("/", response_model={ent}Response, status_code=201)' + NL +
            f"async def create_{evar}(data: {ent}Schema):" + NL +
            "    global _next_id" + NL +
            f"    item = {{**data.model_dump(),'id':_next_id,'created_at':datetime.utcnow()}}" + NL +
            "    _store.append(item); _next_id += 1; return item" + NL + NL +
            f'@router.put("/{{item_id}}", response_model={ent}Response)' + NL +
            f"async def update_{evar}(item_id: int, data: {ent}Schema):" + NL +
            "    for i,item in enumerate(_store):" + NL +
            "        if item['id']==item_id: _store[i]={{**item,**data.model_dump()}}; return _store[i]" + NL +
            f"    raise HTTPException(404, '{ent} not found')" + NL + NL +
            f'@router.delete("/{{item_id}}", status_code=204)' + NL +
            f"async def delete_{evar}(item_id: int):" + NL +
            "    global _store" + NL +
            f"    _store = [i for i in _store if i['id']!=item_id]" + NL
        )})

    main_py = (
        "from fastapi import FastAPI" + NL +
        "from datetime import datetime" + NL +
        router_imports + NL +
        f'app = FastAPI(title="{name}", version="1.0.0")' + NL + NL +
        routers_include + NL +
        '@app.get("/health")' + NL +
        "async def health(): return {'status':'ok','ts':datetime.utcnow().isoformat()}" + NL
    )

    # Build test cases for every entity
    test_cases = NL.join(
        f"def test_{_snake(ent)}_crud(client):" + NL +
        f"    r = client.post('/api/v1/{_slug(ent)}s', json={{'name':'test'}})" + NL +
        f"    assert r.status_code in (201, 422)" + NL
        for ent in raw_entities
    )

    files += [
        {"path": "main.py", "content": main_py},
        *router_files_local,
        {"path": "routers/__init__.py", "content": ""},
        {"path": "requirements.txt", "content": (
            "fastapi>=0.110.0" + NL + "uvicorn[standard]>=0.27.0" + NL +
            "pydantic>=2.0.0" + NL + "pytest>=7.0.0" + NL +
            "pytest-cov>=4.0.0" + NL + "httpx>=0.26.0" + NL
        )},
        {"path": "tests/__init__.py", "content": ""},
        {"path": "tests/test_main.py", "content": (
            "import pytest" + NL +
            "from fastapi.testclient import TestClient" + NL +
            "from main import app" + NL + NL +
            "@pytest.fixture" + NL +
            "def client(): return TestClient(app)" + NL + NL +
            "def test_health(client):" + NL +
            "    r = client.get('/health')" + NL +
            "    assert r.status_code == 200 and r.json()['status'] == 'ok'" + NL + NL +
            test_cases + NL
        )},
        {"path": "Dockerfile", "content": (
            "FROM python:3.12-slim" + NL + "WORKDIR /app" + NL +
            "COPY requirements.txt ." + NL +
            "RUN pip install -r requirements.txt --no-cache-dir" + NL +
            "COPY . ." + NL + "EXPOSE 8000" + NL +
            'CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]' + NL
        )},
        {"path": "README.md", "content": (
            f"# {name}" + NL + NL +
            f"FastAPI REST API — {', '.join(raw_entities)} resource(s)." + NL + NL +
            "```bash" + NL + "pip install -r requirements.txt" + NL +
            "uvicorn main:app --reload" + NL + "```" + NL + NL +
            "## Endpoints" + NL + _endpoint_list() + NL +
            "## Docs" + NL + "Interactive: `http://localhost:8000/docs`" + NL
        )},
    ]
    return files


# ── Type-mapping helpers (used by the scaffold engine above) ─────────────────

def _py_type(t: str) -> str:
    t = (t or "").lower()
    if "int" in t or "long" in t or "number" in t:        return "int"
    if "float" in t or "decimal" in t or "double" in t:   return "float"
    if "bool" in t:                                        return "bool"
    if "date" in t or "time" in t or "timestamp" in t:    return "datetime"
    if "uuid" in t:                                        return "str  # UUID"
    if "enum" in t:
        opts = re.findall(r'\(([^)]+)\)', t)
        return f"str  # enum: {opts[0]}" if opts else "str"
    return "str"

def _java_type(t: str) -> str:
    t = (t or "").lower()
    if "uuid" in t:                                        return "java.util.UUID"
    if "int" in t or "long" in t:                         return "Long"
    if "float" in t or "decimal" in t or "double" in t:   return "Double"
    if "bool" in t:                                        return "Boolean"
    if "date" in t or "timestamp" in t:                   return "java.time.LocalDateTime"
    if "enum" in t:
        opts = re.findall(r'\(([^)]+)\)', t)
        return f"String  /* enum: {opts[0]} */" if opts else "String"
    return "String"

def _go_type(t: str) -> str:
    t = (t or "").lower()
    if "int" in t or "long" in t:                         return "int64"
    if "float" in t or "decimal" in t or "double" in t:   return "float64"
    if "bool" in t:                                        return "bool"
    if "date" in t or "time" in t or "timestamp" in t:    return "time.Time"
    if "uuid" in t:                                        return "string // UUID"
    return "string"

def _cs_type(t: str) -> str:
    t = (t or "").lower()
    if "int" in t or "long" in t:                         return "int"
    if "float" in t or "decimal" in t or "double" in t:   return "decimal"
    if "bool" in t:                                        return "bool"
    if "date" in t or "time" in t or "timestamp" in t:    return "DateTime"
    if "uuid" in t:                                        return "Guid"
    return "string"

def _django_field(t: str) -> str:
    t = (t or "").lower()
    if "int" in t or "long" in t:                         return "IntegerField"
    if "float" in t or "decimal" in t:                    return "DecimalField(max_digits=10, decimal_places=2"
    if "bool" in t:                                        return "BooleanField"
    if "date" in t or "timestamp" in t:                   return "DateTimeField"
    if "uuid" in t:                                        return "UUIDField"
    if "text" in t:                                        return "TextField"
    return "CharField(max_length=255"



# ════════════════════════════════════════════════════════════════════════════
# 1. REQUIREMENTS
# ════════════════════════════════════════════════════════════════════════════
class RequirementsAgent(BaseSDLCAgent):
    phase_name = "requirements"

    @property
    def system_prompt(self):
        return (
            "You are an expert Business Analyst. Read the requirements document carefully.\n\n"
            "OUTPUT FORMAT — respond with exactly this JSON structure:\n"
            '{"requirements":[{"id":"REQ-001","title":"Short title","description":"Full description",'
            '"type":"functional","priority":"high","acceptance_criteria":["Given X, when Y, then Z"]}],'
            '"summary":"2-3 sentence overview",'
            '"wiki_url":null}\n\n'
            "RULES:\n"
            "- type: functional | non_functional | constraint | assumption\n"
            "- priority: critical | high | medium | low\n"
            "- Every requirement needs at least 1 acceptance criterion\n"
            "- wiki_url must be null\n"
            "- Output ONLY the JSON. Start with { and end with }"
        )

    def human_prompt(self, state):
        demo_hint = "\n\nDEMO MODE: Extract exactly 5 most important requirements only." if _DEMO else ""
        return (
            f"Project name: {state['project_name']}{demo_hint}\n\n"
            f"=== REQUIREMENTS DOCUMENT START ===\n"
            f"{state['raw_requirements_doc']}\n"
            f"=== REQUIREMENTS DOCUMENT END ===\n\n"
            f"Extract requirements. Output ONLY JSON starting with {{ and ending with }}"
        )

    def parse_output(self, response, state):
        import structlog as _sl
        _log = _sl.get_logger("requirements_agent")

        print("\n" + "="*70, flush=True)
        print("REQUIREMENTS PARSE_OUTPUT — FULL RAW RESPONSE:", flush=True)
        print(f"Length: {len(response or '')} chars", flush=True)
        print("-"*70, flush=True)
        print(response or "[EMPTY]", flush=True)
        print("="*70 + "\n", flush=True)

        _log.info("req.raw", length=len(response or ""),
                  preview=(response or "")[:300].replace(chr(10), " "))
        try:
            data = self._extract_json(response)
            print(f"  [PARSE] type={type(data).__name__}  keys={list(data.keys()) if isinstance(data,dict) else len(data)}", flush=True)

            raw_reqs = data.get("requirements", []) if isinstance(data, dict) else []

            if not raw_reqs and isinstance(data, list):
                raw_reqs = data

            if not raw_reqs and isinstance(data, dict):
                for key in ("items", "reqs", "req_list", "functional_requirements",
                            "requirements_list", "extracted"):
                    if data.get(key):
                        raw_reqs = data[key]
                        print(f"  [PARSE] found reqs under key '{key}'", flush=True)
                        break

            if not raw_reqs and response:
                import re as _re
                lines = response.split(chr(10))
                text_reqs = []
                for ln in lines:
                    ln = ln.strip()
                    m = _re.match(r'^(?:FR|NFR|REQ|R)?[-\s]?(\d+)[.:)\s]+(.+)$', ln, _re.I)
                    if m and len(m.group(2)) > 5:
                        text_reqs.append({
                            "id": f"REQ-{m.group(1).zfill(3)}",
                            "title": m.group(2)[:80],
                            "description": m.group(2),
                            "type": "functional",
                            "priority": "medium",
                            "acceptance_criteria": [],
                        })
                if text_reqs:
                    raw_reqs = text_reqs
                    print(f"  [PARSE] parsed {len(text_reqs)} reqs from plain text", flush=True)

            raw_reqs = _cap(raw_reqs)
            print(f"  [PARSE] raw_reqs count={len(raw_reqs)}", flush=True)

            reqs = []
            for i, r in enumerate(raw_reqs):
                if not isinstance(r, dict):
                    continue
                reqs.append(Requirement(
                    id=r.get("id") or f"REQ-{str(i+1).zfill(3)}",
                    title=r.get("title") or r.get("name", f"Requirement {i+1}"),
                    description=r.get("description") or r.get("details", ""),
                    type=r.get("type", "functional"),
                    priority=r.get("priority", "medium"),
                    acceptance_criteria=r.get("acceptance_criteria") or r.get("criteria") or [],
                ))
            _log.info("req.parsed", count=len(reqs))
            print(f"  [PARSE] final reqs={len(reqs)}", flush=True)
            return {"requirements": reqs}
        except Exception as exc:
            _sl.get_logger("req").error("req.parse_failed", error=str(exc))
            print(f"  [PARSE] exception: {exc}", flush=True)
            return {"requirements": []}

    async def execute_tools(self, parsed, state, tools):
        log_entries = []
        tool_map = {t.name: t for t in tools}
        reqs = parsed.get("requirements", [])
        if not reqs:
            return log_entries

        collection_id = None
        if "outline_create_collection" in tool_map:
            try:
                r = await tool_map["outline_create_collection"].ainvoke({
                    "name": f"{state['project_name']} — Requirements",
                    "description": f"Requirements for {state['project_name']}",
                })
                d = json.loads(r)
                collection_id = d.get("collection_id")
                log_entries.append({"tool": "outline_create_collection", "result": d.get("url")})
            except Exception as e:
                log_entries.append({"tool": "outline_create_collection", "error": str(e)})

        if "outline_create_document" in tool_map and collection_id:
            try:
                lines = [f"# {state['project_name']} Requirements", ""]
                for r in reqs:
                    lines.append(f"## {r['id']}: {r['title']}")
                    lines.append(f"**Type:** {r.get('type','functional')}  **Priority:** {r.get('priority','medium')}")
                    lines.append("")
                    lines.append(r.get("description", ""))
                    lines.append("")
                    acs = r.get("acceptance_criteria", [])
                    if acs:
                        lines.append("**Acceptance Criteria:**")
                        for ac in acs:
                            lines.append(f"- {ac}")
                    lines.append("")
                r2 = await tool_map["outline_create_document"].ainvoke({
                    "title": f"{state['project_name']} Requirements",
                    "text": NL.join(lines),
                    "collection_id": collection_id,
                })
                d2 = json.loads(r2)
                parsed["wiki_url"] = d2.get("url")
                log_entries.append({"tool": "outline_create_document", "result": d2.get("url")})
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        if "zammad_search_tickets" in tool_map:
            try:
                r3 = await tool_map["zammad_search_tickets"].ainvoke({
                    "query": state["project_name"], "limit": 5,
                })
                tickets = json.loads(r3)
                parsed["related_tickets"] = tickets
                log_entries.append({"tool": "zammad_search_tickets",
                                    "result": f"{len(tickets)} tickets"})
            except Exception as e:
                log_entries.append({"tool": "zammad_search_tickets", "error": str(e)})

        if "plane_create_issue" in tool_map:
            from config.settings import settings
            project_id = getattr(settings, "plane_project_id", "")
            for req in reqs[:5]:
                try:
                    r4 = await tool_map["plane_create_issue"].ainvoke({
                        "project_id": project_id,
                        "title": f"[{req['id']}] {req['title']}",
                        "description": req.get("description", ""),
                        "priority": req.get("priority", "medium"),
                    })
                    print(f"  [PLANE REQ] raw: {r4}", flush=True)
                    d4 = json.loads(r4)
                    url = d4.get("url") or d4.get("external_url") or d4.get("issue_url") or str(d4)
                    log_entries.append({"tool": "plane_create_issue", "result": url})
                except Exception as e:
                    log_entries.append({"tool": "plane_create_issue", "error": str(e)})

        return log_entries

    def quality_gate(self, state):
        reqs = state.get("requirements", [])
        checks = [
            {"name": "has_requirements", "passed": len(reqs) >= 1,
             "detail": f"{len(reqs)} requirements extracted"},
            {"name": "has_acceptance_criteria",
             "passed": any(len(r.get("acceptance_criteria", [])) > 0 for r in reqs),
             "detail": "At least some requirements have acceptance criteria"},
            {"name": "has_priorities",
             "passed": any(r.get("priority") for r in reqs),
             "detail": "At least some requirements have priorities"},
        ]
        score = sum(1 for c in checks if c["passed"]) / len(checks)
        return QualityGate(gate_name="requirements", passed=len(reqs) >= 1,
                           score=score, checks=checks, blocking=True)


# ════════════════════════════════════════════════════════════════════════════
# 2. DESIGN
# ════════════════════════════════════════════════════════════════════════════
class DesignAgent(BaseSDLCAgent):
    phase_name = "design"

    @property
    def system_prompt(self):
       return (
        "You are a Principal Software Architect. Design a complete production system.\n\n"
        "🚨 CRITICAL RULE — READ FIRST:\n"
        "   You MUST use the exact language and framework specified in the requirements.\n"
        "   NEVER default to Java or Spring Boot unless the requirements explicitly say so.\n"
        "   The tech_stack in your response MUST match what the requirements document says.\n"
        "   If requirements say Python → use Python/FastAPI (or Flask/Django if specified).\n"
        "   If requirements say Go → use Go/Gin.\n"
        "   If requirements say .NET/C# → use ASP.NET Core.\n\n"
        "Return this exact JSON structure — every field is required:\n"
        '{"architecture_summary":"2-3 paragraph description of the system",'
        '"components":[{"name":"<Service name from requirements>","type":"service",'
        '"tech":"<MUST match requirements language/framework — e.g. FastAPI, Express, Gin, NOT Spring Boot unless required>","responsibility":"<what this component does>"}],'
        '"tech_stack":{'
        '"language":"<EXACT language from requirements — e.g. Python 3.12, Node.js 20, Go 1.21 — NEVER Java unless requirements say Java>",'
        '"framework":"<EXACT framework from requirements — e.g. FastAPI, Flask, Express, NestJS, Gin — NEVER Spring Boot unless requirements say Spring Boot>",'
        '"database":"<database from requirements or PostgreSQL if unspecified>",'
        '"cache":"<cache from requirements or Redis if unspecified>",'
        '"infrastructure":"<infra from requirements or Docker if unspecified>"},'
        '"api_contracts":[{'
        '"method":"POST",'
        '"path":"/api/v1/items",'
        '"description":"Create a new item",'
        '"auth":"Bearer JWT",'
        '"request_body":{"name":"string","description":"string"},'
        '"response_success":{"status":201,"body":{"id":"string (UUID)","name":"string","createdAt":"ISO8601"}},'
        '"response_error":{"status":400,"body":{"error":"Validation failed","details":["name is required"]}},'
        '"path_params":[],'
        '"query_params":[]'
        '}],'
        '"data_models":[{'
        '"entity":"<EntityName from requirements>",'
        '"description":"<what this entity represents>",'
        '"fields":['
        '{"name":"id","type":"UUID","required":true,"description":"Primary key"},'
        '{"name":"name","type":"string","required":true,"description":"Display name"},'
        '{"name":"createdAt","type":"timestamp","required":true,"description":"Creation timestamp"}'
        '],'
        '"relationships":[{"type":"has_many","entity":"<related entity if any>"}]'
        '}],'
        '"adrs":[{'
        '"id":"ADR-001",'
        '"title":"<decision title — e.g. Use FastAPI for Python REST API>",'
        '"status":"accepted",'
        '"context":"<why this decision was needed>",'
        '"decision":"<what was decided — must match requirements tech stack>",'
        '"consequences":"<tradeoffs of this decision>"'
        '}],'
        '"wiki_url":null}\n\n'
        "RULES:\n"
        "- 🚨 tech_stack.language and tech_stack.framework MUST reflect what the requirements say\n"
        "- 🚨 components[].tech MUST use the same language/framework as tech_stack\n"
        "- 🚨 ADR titles and decisions MUST reference the actual chosen technology, not Java/Spring\n"
        "- Every api_contracts entry MUST have request_body, response_success, response_error\n"
        "- Every data_models entry MUST have description and relationships\n"
        "- Generate ALL endpoints implied by the requirements\n"
        "- api_contracts paths should be RESTful and match the entities in data_models\n"
        "- wiki_url must be null\n"
        "- Output ONLY the JSON. Start with { and end with }"
    )


    def human_prompt(self, state):
        tech = _extract_tech_from_doc(state.get("raw_requirements_doc", ""))

        reqs = NL.join(
            f"- [{r['id']}] {r['title']} ({r['priority']})"
            for r in state.get("requirements", [])
        )
        return (
            f"Project: {state['project_name']}\n\n"
            f"🚨 MANDATORY TECHNOLOGY STACK — NON-NEGOTIABLE:\n"
            f"{tech['constraint']}\n\n"
            f"The tech_stack.language in your JSON MUST be: \"{tech['language']}\"\n"
            f"The tech_stack.framework in your JSON MUST be: \"{tech['framework']}\"\n"
            f"All components[].tech MUST use {tech['framework']}.\n\n"
            f"REQUIREMENTS:\n{reqs}\n\n"
            f"Design the complete system architecture. Output ONLY JSON."
        )

    def parse_output(self, response, state):
        try:
            data = self._extract_json(response)
            return {"design": DesignArtifact(
                architecture_summary=data.get("architecture_summary", ""),
                components=_cap(data.get("components", [])),
                api_contracts=_cap(data.get("api_contracts", [])),
                data_models=data.get("data_models", []),
                tech_stack=data.get("tech_stack", {}),
                adrs=data.get("adrs", []),
            )}
        except Exception:
            return {"design": None}

    # ── Diagram generators — pure functions, no LLM call ─────────────────────
    # All diagrams are built directly from the design state data.
    # Output is valid Mermaid syntax that renders in Outline, GitHub, and
    # any Mermaid-compatible viewer. Also stored as .mmd files in MinIO.

    @staticmethod
    def _diagram_system_architecture(design: dict, project: str) -> str:
        """
        C4-style system architecture diagram showing all components,
        their types, technologies, and inter-dependencies.
        Uses Mermaid flowchart (LR = left-to-right).
        """
        comps = design.get("components", [])
        stack = design.get("tech_stack", {})

        if not comps:
            # Synthesise minimal diagram from tech stack alone
            lang  = stack.get("language", "App")
            db    = stack.get("database", "DB")
            cache = stack.get("cache", "")
            lines = [
                "flowchart LR",
                f'    Client["🌐 Client"]',
                f'    API["⚙️ {project}\\n{lang}"]',
                f'    DB[("🗄️ {db}")]',
                '    Client -->|HTTP/REST| API',
                f'    API -->|queries| DB',
            ]
            if cache:
                lines += [f'    Cache[("⚡ {cache}")]', '    API -->|cache| Cache']
            return NL.join(lines)

        # Group components by type
        type_icons = {
            "service":    "⚙️", "api":       "🔌", "gateway":   "🚪",
            "database":   "🗄️", "cache":     "⚡", "queue":     "📨",
            "frontend":   "🌐", "ui":        "🖥️", "worker":    "⚙️",
            "storage":    "📦", "scheduler": "⏰", "auth":      "🔒",
        }

        # Sanitise node IDs (Mermaid requires alphanumeric)
        def _nid(name: str) -> str:
            return re.sub(r"[^a-zA-Z0-9]", "_", name)

        lines = ["flowchart TD", f'    %% {project} — System Architecture']

        # Subgraph per component type
        from collections import defaultdict as _dd
        by_type: dict = _dd(list)
        for c in comps:
            by_type[c.get("type", "service").lower()].append(c)

        # Render components
        for ctype, clist in by_type.items():
            icon = type_icons.get(ctype, "📦")
            lines.append(f'    subgraph {ctype.upper()}["{icon} {ctype.title()} Layer"]')
            for c in clist:
                nid   = _nid(c.get("name", "comp"))
                label = f"{c.get('name', '')}\\n{c.get('tech', '')}"
                # Shape by type
                if ctype in ("database", "cache", "storage"):
                    lines.append(f'        {nid}[("{label}")]')
                elif ctype in ("queue",):
                    lines.append(f'        {nid}>"{label}"]')
                else:
                    lines.append(f'        {nid}["{label}"]')
            lines.append("    end")

        # Draw edges: services → databases, services → caches, etc.
        service_ids = [_nid(c["name"]) for c in comps if c.get("type","") in ("service","api","gateway","worker")]
        db_ids      = [_nid(c["name"]) for c in comps if c.get("type","") in ("database","cache","storage","queue")]
        # Connect first service → each db (simplified — real deps need explicit ADR data)
        if service_ids and db_ids:
            for sid in service_ids[:3]:
                for did in db_ids[:2]:
                    lines.append(f"    {sid} --> {did}")
        # Connect gateway → services
        gw_ids  = [_nid(c["name"]) for c in comps if c.get("type","") in ("gateway","api")]
        svc_ids = [_nid(c["name"]) for c in comps if c.get("type","") == "service"]
        if gw_ids and svc_ids:
            for gid in gw_ids:
                for sid in svc_ids[:3]:
                    lines.append(f"    {gid} -->|routes| {sid}")

        # External client node
        lines.insert(2, '    Client["🌐 External Client"]')
        if gw_ids:
            lines.append(f"    Client -->|HTTPS| {gw_ids[0]}")
        elif service_ids:
            lines.append(f"    Client -->|HTTPS| {service_ids[0]}")

        return NL.join(lines)

    @staticmethod
    def _diagram_erd(design: dict, project: str) -> str:
        """
        Entity-Relationship Diagram from data_models.
        Renders field names, types, and relationships using Mermaid erDiagram.
        """
        models = design.get("data_models", [])
        if not models:
            return (
                "erDiagram\n"
                f'    {re.sub(chr(32), "_", project.upper())} {{\n'
                "        string id PK\n"
                "        string name\n"
                "        timestamp createdAt\n"
                "    }"
            )

        def _erd_type(t: str) -> str:
            t = (t or "").lower()
            if "uuid" in t:                                        return "string"
            if "int" in t or "long" in t:                         return "int"
            if "float" in t or "decimal" in t or "double" in t:   return "float"
            if "bool" in t:                                        return "boolean"
            if "date" in t or "time" in t or "timestamp" in t:    return "datetime"
            if "text" in t:                                        return "string"
            if "enum" in t:                                        return "string"
            return "string"

        def _safe(s: str) -> str:
            return re.sub(r"[^a-zA-Z0-9_]", "_", s or "field")

        lines = ["erDiagram"]

        # Entities with fields
        for m in models:
            ent    = _safe(m.get("entity", "Entity"))
            fields = m.get("fields", [])
            lines.append(f"    {ent} {{")
            # Always add id first if not present
            has_id = any(f.get("name","").lower() == "id" for f in fields)
            if not has_id:
                lines.append(f"        string id PK")
            for f in fields[:12]:  # cap to keep diagram readable
                fname = _safe(f.get("name","field"))
                ftype = _erd_type(f.get("type","string"))
                pk    = " PK" if fname.lower() == "id" else ""
                fk    = " FK" if fname.lower().endswith("id") and fname.lower() != "id" else ""
                lines.append(f"        {ftype} {fname}{pk}{fk}")
            lines.append("    }")

        # Relationships
        for m in models:
            ent  = _safe(m.get("entity","Entity"))
            rels = m.get("relationships", [])
            for rel in rels:
                other     = _safe(rel.get("entity","Other"))
                rel_type  = rel.get("type","has_many").lower()
                # Map to Mermaid ERD relationship syntax
                cardinality = {
                    "has_many":     f'    {ent} ||--o{{ {other}',
                    "belongs_to":   f'    {ent} }}o--|| {other}',
                    "has_one":      f'    {ent} ||--|| {other}',
                    "many_to_many": f'    {ent} }}o--o{{ {other}',
                }.get(rel_type, f'    {ent} ||--o{{ {other}')
                label = rel_type.replace("_"," ")
                lines.append(f'{cardinality} : "{label}"')

        return NL.join(lines)

    @staticmethod
    def _diagram_api_flow(design: dict, project: str) -> str:
        """
        API request-response flow diagram using Mermaid sequenceDiagram.
        Shows Client → API → Service → DB for each unique resource group.
        """
        apis   = design.get("api_contracts", [])
        comps  = design.get("components", [])
        stack  = design.get("tech_stack", {})

        db_name  = next((c.get("name","Database")  for c in comps if c.get("type","") in ("database","cache")), stack.get("database","Database"))
        svc_name = next((c.get("name","Service")   for c in comps if c.get("type","") == "service"),             f"{project} Service")
        api_name = next((c.get("name","API")        for c in comps if c.get("type","") in ("api","gateway")),    f"{project} API")

        lines = [
            "sequenceDiagram",
            f"    autonumber",
            f"    participant C as 🌐 Client",
            f"    participant A as ⚙️ {api_name}",
            f"    participant S as 🔧 {svc_name}",
            f"    participant D as 🗄️ {db_name}",
            "",
        ]

        # Group by resource and show one sequence per resource (cap at 4)
        from collections import defaultdict as _dd2
        grouped: dict = _dd2(list)
        for ep in apis:
            path  = ep.get("path","")
            segs  = [p for p in path.split("/") if p and not p.startswith("{") and p not in ("api","v1","v2")]
            group = segs[0] if segs else "general"
            grouped[group].append(ep)

        for resource, endpoints in list(grouped.items())[:4]:
            lines.append(f"    rect rgb(240, 248, 255)")
            lines.append(f"        Note over C,D: {resource.title()} Operations")
            for ep in endpoints[:3]:
                method = ep.get("method","GET")
                path   = ep.get("path","")
                desc   = ep.get("description","")
                res_ok = ep.get("response_success",{})
                status = res_ok.get("status",200)

                lines.append(f"        C->>+A: {method} {path}")
                lines.append(f"        Note right of A: {desc[:40]}")
                lines.append(f"        A->>+S: {desc[:30]}")
                if method in ("GET",):
                    lines.append(f"        S->>+D: SELECT query")
                    lines.append(f"        D-->>-S: result rows")
                elif method in ("POST","PUT","PATCH"):
                    lines.append(f"        S->>+D: INSERT/UPDATE")
                    lines.append(f"        D-->>-S: affected rows")
                elif method == "DELETE":
                    lines.append(f"        S->>+D: DELETE query")
                    lines.append(f"        D-->>-S: ok")
                lines.append(f"        S-->>-A: processed result")
                lines.append(f"        A-->>-C: {status} response")
            lines.append("    end")
            lines.append("")

        return NL.join(lines)

    @staticmethod
    def _diagram_deployment(design: dict, project: str) -> str:
        """
        Deployment/infrastructure diagram using Mermaid flowchart.
        Shows Docker containers, ports, and network topology.
        """
        comps = design.get("components", [])
        stack = design.get("tech_stack", {})
        infra = stack.get("infrastructure","Docker").lower()

        lang = stack.get("language","App")
        fw   = stack.get("framework","")
        db   = stack.get("database","Database")
        cache= stack.get("cache","")

        lines = [
            "flowchart TB",
            f'    %% {project} — Deployment Architecture',
            "",
        ]

        if "kubernetes" in infra or "k8s" in infra:
            lines += [
                '    subgraph K8S["☸️ Kubernetes Cluster"]',
                '        subgraph NS["📦 Namespace: ' + re.sub(r"[^a-z0-9-]","",project.lower()) + '"]',
                f'            APP["🚀 {project}\\nDeployment\\n({lang}/{fw})\\nreplicas: 2"]',
                f'            SVC["🔌 Service\\nClusterIP:8080"]',
                "        end",
                f'        subgraph DB_NS["💾 Data Layer"]',
                f'            PG["🗄️ {db}\\nStatefulSet"]',
            ]
            if cache:
                lines.append(f'            CACHE["⚡ {cache}\\nDeployment"]')
            lines += [
                "        end",
                "    end",
                '    INGRESS["🌐 Ingress\\nnginx / port 443"]',
                '    CLIENT["👤 Client"]',
                "    CLIENT -->|HTTPS| INGRESS",
                "    INGRESS -->|routes| SVC",
                "    SVC --> APP",
                "    APP --> PG",
            ]
            if cache:
                lines.append("    APP --> CACHE")
        else:
            # Default: Docker Compose topology
            lines += [
                '    subgraph DOCKER["🐳 Docker Compose"]',
                f'        APP["🚀 {project}\\n{lang}/{fw}\\nport: 8080"]',
                f'        DB["🗄️ {db}\\nport: 5432"]',
            ]
            if cache:
                lines.append(f'        CACHE["⚡ {cache}\\nport: 6379"]')
            lines += [
                "    end",
                '    NGINX["🌐 Nginx\\nport: 80/443"]',
                '    CLIENT["👤 Client"]',
                "    CLIENT -->|HTTPS :443| NGINX",
                "    NGINX -->|proxy :8080| APP",
                "    APP -->|:5432| DB",
            ]
            if cache:
                lines.append("    APP -->|:6379| CACHE")

        return NL.join(lines)

    @staticmethod
    def _diagram_component_interaction(design: dict, project: str) -> str:
        """
        Component interaction / dependency diagram using Mermaid classDiagram.
        Shows internal class structure and layer dependencies.
        """
        comps  = design.get("components", [])
        models = design.get("data_models", [])
        stack  = design.get("tech_stack", {})
        lang   = stack.get("language","").lower()

        def _safe(s):
            return re.sub(r"[^a-zA-Z0-9]", "", s or "Component")

        lines = [
            "classDiagram",
            f"    %% {project} — Component Interaction",
            "",
        ]

        if comps:
            # Render each component as a class with its responsibility
            for c in comps[:8]:
                cname = _safe(c.get("name","Component"))
                ctech = c.get("tech","")
                ctype = c.get("type","service")
                cres  = c.get("responsibility","")[:60]
                lines.append(f"    class {cname} {{")
                lines.append(f"        <<{ctype}>>")
                if ctech:
                    lines.append(f"        +tech: {ctech}")
                if cres:
                    lines.append(f"        +{cres}")
                lines.append("    }")

            # Draw layer relationships
            svc_names = [_safe(c["name"]) for c in comps if c.get("type","") == "service"]
            db_names  = [_safe(c["name"]) for c in comps if c.get("type","") in ("database","cache")]
            gw_names  = [_safe(c["name"]) for c in comps if c.get("type","") in ("gateway","api")]
            for gn in gw_names:
                for sn in svc_names[:2]:
                    lines.append(f"    {gn} --> {sn} : delegates")
            for sn in svc_names[:3]:
                for dn in db_names[:2]:
                    lines.append(f"    {sn} --> {dn} : persists")

        elif models:
            # Fallback: show entity classes and their fields
            for m in models[:6]:
                ent    = _safe(m.get("entity","Entity"))
                fields = m.get("fields",[])[:8]
                lines.append(f"    class {ent} {{")
                for f in fields:
                    fname = f.get("name","field")
                    ftype = f.get("type","string").split("(")[0]
                    lines.append(f"        +{ftype} {fname}")
                lines.append("    }")
            # Relationships
            for m in models[:6]:
                ent  = _safe(m.get("entity","Entity"))
                for rel in m.get("relationships",[]):
                    other    = _safe(rel.get("entity","Other"))
                    rel_type = rel.get("type","has_many")
                    arrow = "-->" if "one" in rel_type else "--*"
                    lines.append(f"    {ent} {arrow} {other}")

        else:
            # Minimal fallback
            pname = _safe(project)
            lines += [
                f"    class {pname}API {{",
                "        <<RestController>>",
                "        +handleRequest()",
                "    }",
                f"    class {pname}Service {{",
                "        <<Service>>",
                "        +processRequest()",
                "    }",
                f"    class {pname}Repository {{",
                "        <<Repository>>",
                "        +findAll()",
                "        +save()",
                "    }",
                f"    {pname}API --> {pname}Service : calls",
                f"    {pname}Service --> {pname}Repository : uses",
            ]

        return NL.join(lines)

    async def execute_tools(self, parsed, state, tools):
        log_entries = []
        tool_map = {t.name: t for t in tools}
        design   = parsed.get("design") or {}
        project  = state["project_name"]

        if not design:
            return log_entries

        # ── Step 1: Generate ALL diagrams from design state (no LLM) ──────────
        print(f"  [DESIGN] Generating diagrams for '{project}'...", flush=True)

        diagrams = {
            "architecture": {
                "title":    "System Architecture",
                "mmd":      self._diagram_system_architecture(design, project),
                "filename": "architecture.mmd",
                "desc":     "C4-style component topology — all services, databases, and connections",
            },
            "erd": {
                "title":    "Entity Relationship Diagram",
                "mmd":      self._diagram_erd(design, project),
                "filename": "erd.mmd",
                "desc":     "Database schema — entities, fields, types, and relationships",
            },
            "api_flow": {
                "title":    "API Request Flow",
                "mmd":      self._diagram_api_flow(design, project),
                "filename": "api_flow.mmd",
                "desc":     "Sequence diagram — how each API request flows through the system",
            },
            "deployment": {
                "title":    "Deployment Architecture",
                "mmd":      self._diagram_deployment(design, project),
                "filename": "deployment.mmd",
                "desc":     "Infrastructure layout — Docker/K8s containers, ports, and networking",
            },
            "components": {
                "title":    "Component Interaction",
                "mmd":      self._diagram_component_interaction(design, project),
                "filename": "components.mmd",
                "desc":     "Class/layer diagram — internal component structure and dependencies",
            },
        }

        for key, d in diagrams.items():
            print(f"  [DESIGN] {d['title']}: {len(d['mmd'].splitlines())} lines", flush=True)

        # ── Step 2: Create Outline collection ─────────────────────────────────
        collection_id = None
        if "outline_create_collection" in tool_map:
            try:
                r = await tool_map["outline_create_collection"].ainvoke({
                    "name":        f"{project} — Architecture",
                    "description": "System architecture, diagrams, and technical design",
                })
                d = json.loads(r)
                collection_id = d.get("collection_id")
                log_entries.append({"tool": "outline_create_collection", "result": d.get("url")})
                print(f"  [DESIGN] Outline collection: {d.get('url')}", flush=True)
            except Exception as e:
                log_entries.append({"tool": "outline_create_collection", "error": str(e)})

        # ── Step 3: Architecture Overview doc WITH all diagrams embedded ───────
        if "outline_create_document" in tool_map and collection_id:
            try:
                stack = design.get("tech_stack", {})
                comps = design.get("components", [])
                adrs  = design.get("adrs", [])

                lines = [
                    f"# {project} — Architecture Overview",
                    "",
                    "## Summary",
                    design.get("architecture_summary", ""),
                    "",
                    "## Tech Stack",
                ]
                for k, v in stack.items():
                    lines.append(f"- **{k}**: {v}")

                # Embed System Architecture diagram
                lines += [
                    "",
                    "---",
                    f"## {diagrams['architecture']['title']}",
                    f"_{diagrams['architecture']['desc']}_",
                    "",
                    "```mermaid",
                    diagrams["architecture"]["mmd"],
                    "```",
                    "",
                ]

                # Embed Deployment diagram
                lines += [
                    "---",
                    f"## {diagrams['deployment']['title']}",
                    f"_{diagrams['deployment']['desc']}_",
                    "",
                    "```mermaid",
                    diagrams["deployment"]["mmd"],
                    "```",
                    "",
                ]

                # Embed Component Interaction diagram
                lines += [
                    "---",
                    f"## {diagrams['components']['title']}",
                    f"_{diagrams['components']['desc']}_",
                    "",
                    "```mermaid",
                    diagrams["components"]["mmd"],
                    "```",
                    "",
                ]

                # Components table
                if comps:
                    lines += ["---", "## Components", ""]
                    lines += ["| Component | Type | Technology | Responsibility |",
                               "|-----------|------|------------|----------------|"]
                    for c in comps:
                        lines.append(f"| **{c.get('name','')}** | {c.get('type','')} | {c.get('tech','')} | {c.get('responsibility','')} |")

                # ADRs
                if adrs:
                    lines += ["", "---", "## Architecture Decision Records (ADRs)", ""]
                    for a in adrs:
                        lines += [
                            f"### {a.get('id','ADR')}: {a.get('title','')}",
                            f"**Status:** {a.get('status','proposed')}  |  **Context:** {a.get('context','')}",
                            "",
                            f"**Decision:** {a.get('decision','')}",
                            "",
                            f"**Consequences:** {a.get('consequences','')}",
                            "",
                        ]

                r2 = await tool_map["outline_create_document"].ainvoke({
                    "title":         f"{project} Architecture",
                    "text":          NL.join(lines),
                    "collection_id": collection_id,
                })
                d2 = json.loads(r2)
                parsed["arch_doc_url"] = d2.get("url")
                log_entries.append({"tool": "outline_create_document",
                                    "result": d2.get("url"),
                                    "note": "Architecture doc with 3 embedded diagrams"})
                print(f"  [DESIGN] Architecture doc: {d2.get('url')}", flush=True)
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        # ── Step 4: Data Model doc WITH ERD diagram ────────────────────────────
        if "outline_create_document" in tool_map and collection_id:
            try:
                models = design.get("data_models", [])
                lines  = [
                    f"# {project} — Data Models & ERD",
                    "",
                    "## Entity Relationship Diagram",
                    f"_{diagrams['erd']['desc']}_",
                    "",
                    "```mermaid",
                    diagrams["erd"]["mmd"],
                    "```",
                    "",
                    "---",
                    "## Entity Definitions",
                    "",
                ]
                for m in models:
                    ent    = m.get("entity","")
                    fields = m.get("fields",[])
                    rels   = m.get("relationships",[])
                    lines += [f"### `{ent}`", m.get("description",""), ""]
                    if fields:
                        lines += ["| Field | Type | Required | Description |",
                                   "|-------|------|----------|-------------|"]
                        for f in fields:
                            req = "✓" if f.get("required") else ""
                            lines.append(f"| `{f.get('name','')}` | `{f.get('type','')}` | {req} | {f.get('description','')} |")
                    if rels:
                        lines += ["", "**Relationships:**"]
                        for rel in rels:
                            lines.append(f"- `{rel.get('type','')}` → `{rel.get('entity','')}`")
                    lines.append("")

                r_erd = await tool_map["outline_create_document"].ainvoke({
                    "title":         f"{project} — Data Models",
                    "text":          NL.join(lines),
                    "collection_id": collection_id,
                })
                d_erd = json.loads(r_erd)
                parsed["erd_doc_url"] = d_erd.get("url")
                log_entries.append({"tool": "outline_create_document",
                                    "result": d_erd.get("url"),
                                    "note": "Data model doc with ERD diagram"})
                print(f"  [DESIGN] ERD doc: {d_erd.get('url')}", flush=True)
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        # ── Step 5: API Reference doc WITH sequence diagram ───────────────────
        if "outline_create_document" in tool_map and collection_id:
            try:
                apis  = design.get("api_contracts", [])
                lines = [
                    f"# {project} — API Reference",
                    "",
                    "## API Request Flow",
                    f"_{diagrams['api_flow']['desc']}_",
                    "",
                    "```mermaid",
                    diagrams["api_flow"]["mmd"],
                    "```",
                    "",
                    "---",
                    "## Endpoints",
                    "",
                    f"**Base URL:** `http://localhost:8080`  ",
                    f"**Auth:** `Authorization: Bearer <token>`",
                    "",
                ]

                from collections import defaultdict as _dd3
                grouped: dict = _dd3(list)
                for ep in apis:
                    path  = ep.get("path","/")
                    segs  = [p for p in path.split("/") if p and not p.startswith("{") and p not in ("api","v1","v2")]
                    group = segs[0].title() if segs else "General"
                    grouped[group].append(ep)

                for resource, endpoints in grouped.items():
                    lines += [f"### {resource}", ""]
                    for ep in endpoints:
                        method  = ep.get("method","GET")
                        path    = ep.get("path","")
                        desc    = ep.get("description","")
                        req_b   = ep.get("request_body")
                        res_ok  = ep.get("response_success")
                        res_err = ep.get("response_error")
                        lines  += [f"#### `{method} {path}`", f"_{desc}_", ""]
                        if req_b:
                            lines += ["**Request:**", "```json",
                                      json.dumps(req_b, indent=2), "```", ""]
                        if res_ok:
                            lines += [f"**Response `{res_ok.get('status',200)}`:**", "```json",
                                      json.dumps(res_ok.get("body",{}), indent=2), "```", ""]
                        if res_err:
                            lines += [f"**Error `{res_err.get('status',400)}`:**", "```json",
                                      json.dumps(res_err.get("body",{}), indent=2), "```", ""]
                        lines += ["---", ""]

                r3 = await tool_map["outline_create_document"].ainvoke({
                    "title":         f"{project} — API Reference",
                    "text":          NL.join(lines),
                    "collection_id": collection_id,
                })
                d3 = json.loads(r3)
                log_entries.append({"tool": "outline_create_document",
                                    "result": d3.get("url"),
                                    "note": "API reference with sequence diagram"})
                print(f"  [DESIGN] API doc: {d3.get('url')}", flush=True)
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        # ── Step 6: Store all diagrams as .mmd files in MinIO ────────────────
        # .mmd files can be rendered by any Mermaid-compatible tool:
        # VS Code extension, GitHub markdown, Mermaid Live Editor, Notion, etc.
        if "minio_upload_artifact" in tool_map:
            for key, d in diagrams.items():
                try:
                    r_mmd = tool_map["minio_upload_artifact"].invoke({
                        "object_name":  f"{project}/diagrams/{d['filename']}",
                        "content":      d["mmd"],
                        "content_type": "text/plain",
                    })
                    d_mmd = json.loads(r_mmd)
                    log_entries.append({"tool": "minio_upload_artifact",
                                        "result": d_mmd.get("url"),
                                        "note": f"Mermaid diagram: {d['title']}"})
                    print(f"  [DESIGN] Stored {d['filename']}: {d_mmd.get('url')}", flush=True)
                except Exception as e:
                    log_entries.append({"tool": "minio_upload_artifact",
                                        "error": str(e), "note": d["filename"]})

            # Also store a combined diagrams.md — all 5 diagrams in one file
            try:
                combined = [f"# {project} — All Architecture Diagrams", ""]
                for key, d in diagrams.items():
                    combined += [
                        f"## {d['title']}",
                        f"_{d['desc']}_",
                        "",
                        "```mermaid",
                        d["mmd"],
                        "```",
                        "",
                        "---",
                        "",
                    ]
                r_all = tool_map["minio_upload_artifact"].invoke({
                    "object_name":  f"{project}/diagrams/all_diagrams.md",
                    "content":      NL.join(combined),
                    "content_type": "text/markdown",
                })
                d_all = json.loads(r_all)
                parsed["diagrams_url"] = d_all.get("url")
                log_entries.append({"tool": "minio_upload_artifact",
                                    "result": d_all.get("url"),
                                    "note": "All 5 diagrams combined (markdown)"})
            except Exception as e:
                log_entries.append({"tool": "minio_upload_artifact", "error": str(e)})

        # Store diagram URLs in state for downstream phases (documentation phase uses them)
        parsed["diagram_urls"] = {
            key: f"{project}/diagrams/{d['filename']}"
            for key, d in diagrams.items()
        }
        print(f"  [DESIGN] Generated {len(diagrams)} diagrams, {len(log_entries)} tool calls", flush=True)

        return log_entries

    def quality_gate(self, state):
        d = state.get("design") or {}
        checks = [
            {"name": "has_summary",       "passed": bool(d.get("architecture_summary")), "detail": "Architecture summary written"},
            {"name": "has_components",    "passed": len(d.get("components", [])) >= 1,   "detail": f"{len(d.get('components', []))} components defined"},
            {"name": "has_api_contracts", "passed": len(d.get("api_contracts", [])) >= 1,"detail": f"{len(d.get('api_contracts', []))} API endpoints defined"},
            {"name": "has_tech_stack",    "passed": bool(d.get("tech_stack")),            "detail": "Technology stack defined"},
            {"name": "has_diagrams",      "passed": bool(state.get("diagram_urls")),      "detail": f"{len(state.get('diagram_urls',{}))} diagrams generated"},
        ]
        score = sum(1 for c in checks if c["passed"]) / len(checks)
        critical = bool(d.get("architecture_summary")) or bool(d.get("tech_stack"))
        return QualityGate(gate_name="design", passed=critical,
                           score=score, checks=checks, blocking=True)


# ════════════════════════════════════════════════════════════════════════════
# 3. PLANNING
# ════════════════════════════════════════════════════════════════════════════
class PlanningAgent(BaseSDLCAgent):
    phase_name = "planning"

    @property
    def system_prompt(self):
        return (
            "You are an Agile Project Manager. Break requirements into sprint-ready work items.\n\n"
            "Return this exact JSON:\n"
            '{"work_items":[{"id":"WI-001","title":"Implement user registration",'
            '"description":"POST /api/v1/users with email+password validation",'
            '"type":"story","priority":"high","linked_requirements":["REQ-001"],'
            '"external_id":null,"external_url":null}],"sprint_name":"Sprint 1"}\n\n'
            "RULES:\n"
            "- type: epic | story | task | bug\n"
            "- external_id and external_url: always null\n"
            "- Link every work item to at least one requirement\n"
            "- Output ONLY the JSON. Start with { and end with }"
        )

    def human_prompt(self, state):
        reqs = NL.join(
            f"- [{r['id']}] {r['title']} | {r['priority']}"
            for r in state.get("requirements", [])
        )
        design = state.get("design") or {}
        return (
            f"Project: {state['project_name']}\n\n"
            f"REQUIREMENTS:\n{reqs}\n\n"
            f"COMPONENTS: {[c.get('name') for c in design.get('components', [])]}\n\n"
            f"Create work items covering every requirement. Output ONLY JSON."
        )

    def parse_output(self, response, state):
        try:
            data = self._extract_json(response)
            items = [
                WorkItem(
                    id=wi.get("id") or f"WI-{str(uuid.uuid4())[:6]}",
                    title=wi.get("title", ""),
                    description=wi.get("description", ""),
                    type=wi.get("type", "task"),
                    priority=wi.get("priority", "medium"),
                    status="backlog",
                    linked_requirements=wi.get("linked_requirements", []),
                    external_id=wi.get("external_id"),
                    external_url=wi.get("external_url"),
                )
                for wi in _cap(data.get("work_items", []))
            ]
            return {"work_items": items}
        except Exception:
            return {"work_items": []}

    async def execute_tools(self, parsed, state, tools):
        log_entries = []
        tool_map = {t.name: t for t in tools}
        items = parsed.get("work_items", [])
        if not items:
            return log_entries

        from config.settings import settings
        project_id = getattr(settings, "plane_project_id", "")
        print(f"  [PLANNING] PLANE_PROJECT_ID='{project_id}' items={len(items)}", flush=True)

        for wi in items:
            d = await _call_tool(tool_map, "plane_create_issue", {
                "project_id": project_id,
                "title": wi["title"],
                "description": wi.get("description", ""),
                "priority": wi.get("priority", "medium"),
            }, log_entries)
            wi["external_id"]  = d.get("external_id") or d.get("issue_id")
            wi["external_url"] = d.get("url") or d.get("external_url")

        await _call_tool(tool_map, "plane_create_cycle", {
            "project_id": project_id,
            "name": "Sprint 1 — Initial Implementation",
        }, log_entries)

        collection_id = ""
        if "outline_create_collection" in tool_map:
            dc = await _call_tool(tool_map, "outline_create_collection", {
                "name": f"{state['project_name']} — Planning",
                "description": "Sprint planning and work items",
            }, log_entries)
            collection_id = dc.get("collection_id", "")

        if "outline_create_document" in tool_map:
            try:
                lines = [f"# Sprint 1 Planning — {state['project_name']}", "", "## Work Items", ""]
                for wi in items:
                    ext = (f" → [{wi.get('external_id','')}]({wi.get('external_url','')})"
                           if wi.get("external_url") else "")
                    lines.append(f"- **[{wi['id']}]** {wi['title']} ({wi.get('priority','medium')}){ext}")
                    lines.append(f"  Linked: {', '.join(wi.get('linked_requirements', []))}")
                r3 = await tool_map["outline_create_document"].ainvoke({
                    "title": "Sprint 1 Planning",
                    "text": NL.join(lines),
                    "collection_id": collection_id,
                })
                d3 = json.loads(r3)
                log_entries.append({"tool": "outline_create_document",
                                    "result": d3.get("url") or d3.get("document_id")})
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        return log_entries

    def quality_gate(self, state):
        items = state.get("work_items", [])
        req_ids = {r["id"] for r in state.get("requirements", [])}
        covered = {rid for wi in items for rid in wi.get("linked_requirements", [])}
        checks = [
            {"name": "has_items",  "passed": len(items) >= 1,
             "detail": f"{len(items)} work items created"},
            {"name": "coverage",
             "passed": len(req_ids) == 0 or req_ids.issubset(covered | {""}),
             "detail": f"{len(covered)}/{len(req_ids)} requirements covered"},
        ]
        score = sum(1 for c in checks if c["passed"]) / len(checks)
        return QualityGate(gate_name="planning", passed=score >= 0.5,
                           score=score, checks=checks, blocking=True)


# ════════════════════════════════════════════════════════════════════════════
# 4. DEVELOPMENT
# ════════════════════════════════════════════════════════════════════════════
class DevelopmentAgent(BaseSDLCAgent):
    phase_name = "development"

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _detect_lang(stack: dict, raw_doc: str = "") -> str:
        """Return canonical language tag — requirements doc takes priority over design output."""

        # ── Requirements doc is the ground truth ────────────────────────────
        if raw_doc:
            tech = _extract_tech_from_doc(raw_doc)
            lang = tech["language"].lower()
            fw   = tech["framework"].lower()

            if "python" in lang:
                if "flask"  in fw: return "flask"
                if "django" in fw: return "django"
                return "fastapi"
            if "node" in lang or "typescript" in lang:
                return "node"
            if "go" in lang:
                return "go"
            if "c#" in lang or ".net" in lang:
                return "dotnet"
            if "ruby" in lang:
                return "ruby"
            if "rust" in lang:
                return "rust"
            if "java" in lang:
                return "java"

        # ── Fallback: read from design state (less reliable) ────────────────
        lang = stack.get("language", "").lower()
        fw   = stack.get("framework", "").lower()
  
        combined = f"{lang} {fw}"
        # Node must be checked before Java to avoid "java" matching "javascript"
        if any(t in combined for t in ("node", "express", "nestjs", "nest", "typescript", "javascript")):
            return "node"
        if re.search(r"\bjava\b", combined) or any(t in combined for t in ("spring", "kotlin", "gradle", "maven")):
            return "java"
        if any(t in combined for t in ("golang", "gin", "echo", "fiber")) or lang.strip().startswith("go"):
            return "go"
        if any(t in combined for t in ("rust", "actix", "axum", "warp")):
            return "rust"
        if any(t in combined for t in ("c#", "csharp", ".net", "dotnet", "aspnet", "asp.net", "blazor")):
            return "dotnet"
        if any(t in combined for t in ("ruby", "rails", "sinatra")):
            return "ruby"
        if any(t in combined for t in ("php", "laravel", "symfony", "slim")):
            return "php"
        if "flask" in combined:
            return "flask"
        if "django" in combined:
            return "django"
        return "fastapi"  # default Python

    @staticmethod
    def _required_file_patterns(lang: str, entities: list[str], pkg: str = "") -> list[str]:
        """
        Return a list of file path patterns that MUST exist for this language.
        Used both in the prompt (as instructions) and in parse_output (to detect gaps).
        Each pattern uses {Entity} and {entity} as placeholder tokens.
        """
        if lang == "java":
            base = [
                f"{pkg}/Application.java",
                f"{pkg}/controller/{{Entity}}Controller.java",
                f"{pkg}/service/{{Entity}}Service.java",
                f"{pkg}/repository/{{Entity}}Repository.java",
                f"{pkg}/model/{{Entity}}.java",
                "src/main/resources/application.properties",
                "pom.xml",
                "Dockerfile",
                "README.md",
            ]
            return base
        if lang == "node":
            return [
                "index.{js|ts}",
                "routes/{entity}Routes.{js|ts}",
                "controllers/{entity}Controller.{js|ts}",
                "services/{entity}Service.{js|ts}",
                "package.json",
                "Dockerfile",
                "README.md",
            ]
        if lang == "go":
            return [
                "main.go",
                "handlers/{entity}.go",
                "models/{entity}.go",
                "go.mod",
                "Dockerfile",
                "README.md",
            ]
        if lang == "dotnet":
            return [
                "Program.cs",
                "Controllers/{Entity}Controller.cs",
                "Models/{Entity}.cs",
                "{ProjectName}.csproj",
                "Dockerfile",
                "README.md",
            ]
        if lang == "ruby":
            return [
                "app/controllers/{entity}s_controller.rb",
                "app/models/{entity}.rb",
                "Gemfile",
                "Dockerfile",
                "README.md",
            ]
        if lang == "php":
            return [
                "app/Http/Controllers/{Entity}Controller.php",
                "app/Models/{Entity}.php",
                "Dockerfile",
                "README.md",
            ]
        if lang == "rust":
            return [
                "src/main.rs",
                "src/{entity}.rs",
                "Cargo.toml",
                "Dockerfile",
                "README.md",
            ]
        if lang == "flask":
            return [
                "app.py",
                "requirements.txt",
                "tests/test_app.py",
                "Dockerfile",
                "README.md",
            ]
        if lang == "django":
            return [
                "manage.py",
                "{app}/models.py",
                "{app}/views.py",
                "requirements.txt",
                "Dockerfile",
                "README.md",
            ]
        # FastAPI default
        return [
            "main.py",
            "routers/{entity}.py",
            "routers/__init__.py",
            "requirements.txt",
            "tests/test_main.py",
            "Dockerfile",
            "README.md",
        ]

    @staticmethod
    def _build_file_checklist(lang: str, entities: list[str], project_name: str) -> str:
        """
        Return a human-readable REQUIRED FILES checklist for the prompt.
        Expands {Entity}/{entity} for every entity in the design.
        """
        import re as _re

        def _pascal(s):
            return _re.sub(r"[^a-zA-Z0-9]", " ", s).title().replace(" ", "")

        def _snake(s):
            return _re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

        def _slug(s):
            return _re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

        pkg = ""
        if lang == "java":
            pkg = "src/main/java/com/" + _re.sub(r"[^a-z0-9]", "", project_name.lower())[:20]

        patterns = DevelopmentAgent._required_file_patterns(lang, entities, pkg)
        lines = []
        for pattern in patterns:
            if "{Entity}" in pattern or "{entity}" in pattern:
                for ent in entities:
                    p = (pattern
                         .replace("{Entity}", _pascal(ent))
                         .replace("{entity}", _snake(ent))
                         .replace("{pkg}", pkg)
                         .replace("{app}", _slug(project_name).replace("-","_")))
                    lines.append(f"  - {p}")
            else:
                p = (pattern
                     .replace("{pkg}", pkg)
                     .replace("{ProjectName}", _pascal(project_name))
                     .replace("{app}", _slug(project_name).replace("-","_")))
                lines.append(f"  - {p}")
        return NL.join(lines)

    @property
    def system_prompt(self):
        return (
            "You are a Senior Software Engineer. Generate a COMPLETE, PRODUCTION-READY codebase.\n\n"
            "═══════════════════════════════════════════════════════════\n"
            "GOLDEN RULE: Generate EVERY file listed in the REQUIRED FILES section of the prompt.\n"
            "Do NOT skip any file. Do NOT merge multiple classes into one file.\n"
            "═══════════════════════════════════════════════════════════\n\n"
            "OUTPUT FORMAT — respond with exactly this JSON structure:\n"
            '{\n'
            '  "project_name": "<name>",\n'
            '  "files": [\n'
            '    {"path": "<relative/file/path>", "content": "<complete file content>"},\n'
            '    ... one entry per file ...\n'
            '  ],\n'
            '  "implementation_notes": "<brief summary>"\n'
            '}\n\n'
            "FILE CONTENT RULES:\n"
            "- Every file must be COMPLETE and RUNNABLE — no TODOs, no ellipsis (...), no stubs\n"
            "- Every endpoint must be implemented — correct HTTP method, path, request/response\n"
            "- Every entity needs full CRUD: list, get-by-id, create, update, delete\n"
            "- Keep each file under 120 lines — concise but complete\n"
            "- content field: escape all newlines as \\n, all quotes as \\\" — valid JSON string\n\n"
            "LANGUAGE-SPECIFIC RULES:\n"
            "Java/Spring Boot:\n"
            "  - Separate file for: Application.java, {Entity}Controller.java, {Entity}Service.java,\n"
            "    {Entity}Repository.java, {Entity}.java (entity), application.properties, pom.xml\n"
            "  - Use H2 in-memory DB (no external DB): spring.datasource.url=jdbc:h2:mem:testdb\n"
            "  - pom.xml MUST include: spring-boot-starter-web, spring-boot-starter-data-jpa, h2\n"
            "Node.js/Express/NestJS:\n"
            "  - Separate file for: index.js, routes/{entity}Routes.js,\n"
            "    controllers/{entity}Controller.js, services/{entity}Service.js, package.json\n"
            "  - Use in-memory array store (no external DB needed for scaffold)\n"
            "Python/FastAPI:\n"
            "  - main.py + routers/{entity}.py (one per entity) + requirements.txt\n"
            "  - Use in-memory list store per router\n"
            "Python/Flask:\n"
            "  - Single app.py with blueprint/route per entity + requirements.txt\n"
            "Go:\n"
            "  - main.go + handlers/{entity}.go + models/{entity}.go + go.mod\n"
            ".NET/C#:\n"
            "  - Program.cs + Controllers/{Entity}Controller.cs + Models/{Entity}.cs + .csproj\n"
            "Ruby/Rails:\n"
            "  - app/controllers/{entity}s_controller.rb + app/models/{entity}.rb + Gemfile\n"
            "ALL languages: always include Dockerfile and README.md\n\n"
            "IMPORTANT: Output ONLY the JSON object. Start with { and end with }"
        )

    def human_prompt(self, state):
        design  = state.get("design") or {}
        stack   = design.get("tech_stack", {})
        apis    = design.get("api_contracts", [])
        models  = design.get("data_models", [])
        reqs    = state.get("requirements", [])[:_C["reqs"]]
        items   = state.get("work_items", [])[:_C["items"]]

        lang  = stack.get("language", "Python")
        fw    = stack.get("framework", "FastAPI")
        db    = stack.get("database", "PostgreSQL")
        infra = stack.get("infrastructure", "Docker")

        # ── Override with requirements doc — don't trust design agent output ──
        tech = _extract_tech_from_doc(state.get("raw_requirements_doc", ""))

        # If design agent still chose wrong stack, correct it here
        actual_lang = stack.get("language", "")
        actual_fw   = stack.get("framework", "")
        wrong_stack = any(r.lower() in actual_lang.lower() or r.lower() in actual_fw.lower()
                        for r in tech["reject"])
        if wrong_stack:
            print(f"  [DEV] Design chose wrong stack ({actual_lang}/{actual_fw}) "
                f"— overriding to {tech['language']}/{tech['framework']}", flush=True)
            stack["language"]  = tech["language"]
            stack["framework"] = tech["framework"]

        # Detect language family for the checklist
        lang_tag  = self._detect_lang(stack)
        entities  = [m.get("entity","") for m in models if m.get("entity")]
        if not entities:
            entities = [re.sub(r"[^a-zA-Z0-9]", "", state["project_name"].title())]

        # Build the mandatory file checklist
        checklist = self._build_file_checklist(lang_tag, entities, state["project_name"])

        # Format API endpoints
        api_lines = NL.join(
            f"  {e.get('method','GET')} {e.get('path','')} — {e.get('description','')}"
            for e in apis[:_C["apis"]]
        ) or "  GET /health\n  GET /api/v1/{entity}s\n  POST /api/v1/{entity}s\n  GET /api/v1/{entity}s/{id}\n  PUT /api/v1/{entity}s/{id}\n  DELETE /api/v1/{entity}s/{id}"

        # Format data models with full field list
        model_lines = NL.join(
            f"  {m.get('entity','')}: fields={[f.get('name') for f in m.get('fields',[])]}"
            for m in models[:_C["models"]]
        ) or f"  {entities[0]}: id, name, createdAt, updatedAt"

        req_lines  = NL.join(f"  [{r.get('id','')}] {r.get('title','')}" for r in reqs)
        item_lines = NL.join(f"  [{wi.get('id','')}] {wi.get('title','')}" for wi in items)

        mode_hint = {
            "demo":     f"DEMO MODE: Generate the 5 most critical files only.",
            "standard": f"STANDARD MODE: Generate all {len(checklist.splitlines())} required files.",
            "full":     f"FULL MODE: Generate complete production codebase — all files, all CRUD.",
        }.get(_MODE, "")

        return (
            f"Project: {state['project_name']}\n"
            f"Language: {lang} / Framework: {fw} / Database: {db} / Infra: {infra}\n\n"
            f"═══ REQUIRED FILES — YOU MUST GENERATE ALL OF THESE ═══\n"
            f"{checklist}\n\n"
            f"═══ API ENDPOINTS TO IMPLEMENT ═══\n{api_lines}\n\n"
            f"═══ DATA MODELS ═══\n{model_lines}\n\n"
            f"═══ REQUIREMENTS ═══\n{req_lines or '  (see work items)'}\n\n"
            f"═══ WORK ITEMS ═══\n{item_lines or '  Implement full CRUD for all entities'}\n\n"
            f"{mode_hint}\n"
            f"TOKEN BUDGET: Keep total JSON under 7000 tokens. Each method: 3-8 lines max.\n\n"
            f"Generate ALL required files above. Output ONLY JSON."
        )

    def parse_output(self, response, state):
        """
        Parse LLM output. Three-stage strategy:
          1. Parse JSON and extract all valid files from LLM response.
          2. If LLM returned partial files (e.g. only Application.java),
             merge with fallback scaffold — LLM files take priority, fallback
             fills in the missing ones.
          3. If JSON parse fails entirely, use full fallback scaffold.
        """
        print(f"\n{'='*70}", flush=True)
        print(f"DEV PARSE_OUTPUT — response length: {len(response or '')} chars", flush=True)
        print(f"First 500 chars:\n{(response or '')[:500]}", flush=True)
        print(f"Last  200 chars:\n{(response or '')[-200:]}", flush=True)
        print(f"{'='*70}\n", flush=True)

        design   = state.get("design") or {}
        stack    = design.get("tech_stack", {})
        lang_tag = self._detect_lang(stack)
        models   = design.get("data_models", [])
        entities = [m.get("entity","") for m in models if m.get("entity")]
        if not entities:
            entities = [re.sub(r"[^a-zA-Z0-9]", "", state["project_name"].title())]

        # ── Minimum file count we expect for this language (per entity) ──
        # java: 4 files per entity + 4 shared = (4*n+4); node: 3 per entity + 4; etc.
        _min_files = {
            "java": max(5, 4 * len(entities) + 4),
            "node": max(5, 3 * len(entities) + 4),
            "go":   max(4, 2 * len(entities) + 3),
            "dotnet": max(4, 2 * len(entities) + 3),
            "ruby": max(4, 2 * len(entities) + 2),
            "php":  max(4, 2 * len(entities) + 2),
            "rust": max(4, len(entities) + 3),
            "fastapi": max(4, len(entities) + 4),
            "flask": 4,
            "django": max(4, len(entities) + 3),
        }
        min_expected = _min_files.get(lang_tag, 4)
        print(f"  [DEV PARSE] lang_tag={lang_tag} entities={entities} min_expected={min_expected}", flush=True)

        llm_files: list[dict] = []
        impl_notes = ""

        try:
            data = self._extract_json(response)
            print(f"  [DEV PARSE] JSON parsed OK. keys={list(data.keys()) if isinstance(data,dict) else type(data)}", flush=True)

            # Find files list under any key the LLM might use
            raw_files = []
            for key in ("files", "source_files", "generated_files", "code_files",
                        "project_files", "artifacts", "sources", "file_list",
                        "output_files", "codebase"):
                if data.get(key):
                    raw_files = data[key]
                    if key != "files":
                        print(f"  [DEV PARSE] found files under alt key '{key}'", flush=True)
                    break

            # Normalise each file entry — LLM uses many different key names
            for f in raw_files:
                if not isinstance(f, dict):
                    continue
                path = (f.get("path") or f.get("filename") or f.get("name") or
                        f.get("file_path") or f.get("filepath") or
                        f.get("file_name") or "").strip().lstrip("/")
                body = (f.get("content") or f.get("code") or f.get("source") or
                        f.get("file_content") or f.get("body") or f.get("text") or "")

                # Skip clearly empty entries
                if not path:
                    print(f"  [DEV PARSE] skip: no path", flush=True)
                    continue
                if not body or not str(body).strip():
                    print(f"  [DEV PARSE] skip {path}: empty content", flush=True)
                    continue
                # Skip placeholder-only content (LLM stub detection)
                body_str = str(body).strip()
                if len(body_str) < 20 or body_str in ("...", "// TODO", "# TODO", "pass"):
                    print(f"  [DEV PARSE] skip {path}: stub content '{body_str[:40]}'", flush=True)
                    continue

                llm_files.append({"path": path, "content": body_str})

            impl_notes = data.get("implementation_notes", "")

        except Exception as exc:
            print(f"  [DEV PARSE] JSON parse FAILED: {exc}", flush=True)

        print(f"  [DEV PARSE] LLM returned {len(llm_files)} valid files:", flush=True)
        for f in llm_files:
            print(f"  [DEV PARSE]   {f['path']} ({len(f['content'])} chars)", flush=True)

        # ── Stage 2: Merge strategy ───────────────────────────────────────
        # Always generate the full fallback scaffold, then overlay with
        # whatever the LLM produced. This guarantees every required file
        # exists — LLM files take priority (they have real content).
        fallback_files = _generate_fallback_files(state)
        print(f"  [DEV PARSE] fallback scaffold has {len(fallback_files)} files", flush=True)

        # Build merged file map: fallback first, LLM overlays on top
        file_map: dict[str, str] = {}
        for f in fallback_files:
            file_map[f["path"]] = f["content"]

        llm_paths_lower = {f["path"].lower() for f in llm_files}
        for f in llm_files:
            if lang_tag == "java":
                pkg = "com." + re.sub(r"[^a-z0-9]", "", state["project_name"].lower())[:20]
                if not _is_valid_java_file(f["path"], f["content"], pkg, entities):
                    print(f"  [DEV PARSE] Rejected LLM file (broken imports): {f['path']}", flush=True)
                    continue  # fallback version stays in file_map
            file_map[f["path"]] = f["content"]

        # Check if LLM covered all required files (log gaps clearly)
        fallback_paths = {f["path"] for f in fallback_files}
        covered_by_llm = {fp for fp in fallback_paths if fp.lower() in llm_paths_lower}
        missing        = fallback_paths - covered_by_llm
        if missing:
            print(f"  [DEV PARSE] LLM missed {len(missing)} files — filled from fallback:", flush=True)
            for m in sorted(missing):
                print(f"  [DEV PARSE]   (fallback) {m}", flush=True)
        else:
            print(f"  [DEV PARSE] LLM covered all required files ✓", flush=True)

        final_files = [{"path": p, "content": c} for p, c in file_map.items()]
        print(f"  [DEV PARSE] FINAL file count: {len(final_files)}", flush=True)

        return {
            "code_repo_url":        None,
            "code_branch":          None,
            "code_pr_url":          None,
            "generated_files":      final_files,
            "implementation_notes": impl_notes or f"Generated {len(final_files)} files ({lang_tag})",
        }

    async def execute_tools(self, parsed, state, tools):
        log_entries = []
        tool_map = {t.name: t for t in tools}

        print(f"  [DEV] available tools: {list(tool_map.keys())}", flush=True)

        files = parsed.get("generated_files", [])
        print(f"  [DEV] files to push: {len(files)} — {[f.get('path') for f in files[:8]]}", flush=True)

        if not files:
            print("  [DEV] No files — using full fallback scaffold", flush=True)
            files = _generate_fallback_files(state)
            parsed["generated_files"] = files
            print(f"  [DEV] Fallback files: {[f.get('path') for f in files]}", flush=True)

        slug   = re.sub(r"[^a-z0-9-]", "-", state["project_name"].lower())[:40].strip("-")
        branch = "feature/implementation"
        print(f"  [DEV] repo slug: {slug}", flush=True)

        d = await _call_tool(tool_map, "gitea_create_repo", {
            "name": slug, "description": state["project_name"]
        }, log_entries)
        parsed["code_repo_url"] = d.get("repo_url") or d.get("html_url")
        if d.get("error"):
            print(f"  [DEV] Repo error: {d['error']} — will still try to push files", flush=True)

        bd = await _call_tool(tool_map, "gitea_create_branch", {
            "repo": slug, "branch": branch
        }, log_entries)
        actual_branch = branch if not bd.get("error") else "main"
        parsed["code_branch"] = actual_branch
        print(f"  [DEV] Using branch: {actual_branch}", flush=True)

        push_limit = _C["push"]
        valid_files = [
            f for f in files
            if f.get("path","").strip() and f.get("content","").strip()
        ]
        skipped = len(files) - len(valid_files)
        if skipped:
            print(f"  [DEV] skipping {skipped} files with empty path/content", flush=True)

        to_push = valid_files[:push_limit]
        print(f"  [DEV] pushing {len(to_push)} of {len(valid_files)} valid files to branch '{actual_branch}'", flush=True)

        for i, f in enumerate(to_push):
            path = f["path"].strip().lstrip("/")
            fc   = f["content"]
            push_branch = "main" if i == 0 and actual_branch != "main" else actual_branch
            print(f"  [DEV] [{i+1}/{len(to_push)}] {path} ({len(fc)} chars) → {push_branch}", flush=True)
            await _call_tool(tool_map, "gitea_push_file", {
                "repo": slug, "branch": push_branch,
                "file_path": path, "content": fc,
                "message": f"{'init: bootstrap' if i == 0 else 'feat: add'} {path}",
            }, log_entries)

        if actual_branch != "main":
            d2 = await _call_tool(tool_map, "gitea_create_pull_request", {
                "repo":  slug,
                "title": f"feat: implement {state['project_name']}",
                "body":  parsed.get("implementation_notes") or "Initial implementation",
                "head":  actual_branch, "base": "main",
            }, log_entries)
            parsed["code_pr_url"] = d2.get("pr_url") or d2.get("html_url")
        else:
            print("  [DEV] Pushed to main — no PR needed", flush=True)

        return log_entries

    def quality_gate(self, state):
        files          = state.get("generated_files", [])
        files_count    = len(files)
        design         = state.get("design") or {}
        stack          = design.get("tech_stack", {})
        models         = design.get("data_models", [])
        entities       = [m.get("entity","") for m in models if m.get("entity")]
        lang_tag       = self._detect_lang(stack)

        # Minimum files expected for this language/entity count
        _min = {
            "java":   max(5, 4 * len(entities) + 4),
            "node":   max(5, 3 * len(entities) + 4),
            "go":     max(4, 2 * len(entities) + 3),
            "dotnet": max(4, 2 * len(entities) + 3),
            "ruby":   max(4, 2 * len(entities) + 2),
            "php":    max(4, 2 * len(entities) + 2),
            "rust":   max(4, len(entities) + 3),
            "fastapi":max(4, len(entities) + 4),
            "flask":  4,
            "django": max(4, len(entities) + 3),
        }
        min_expected = _min.get(lang_tag, 4)

        # Check that critical language-specific files are present
        file_paths_lower = {f.get("path","").lower() for f in files}
        has_entrypoint  = any(
            p in file_paths_lower for p in (
                "main.py","app.py","main.go","index.js","index.ts",
                "program.cs","manage.py","src/main.rs",
            )
        ) or any("application.java" in p for p in file_paths_lower)
        has_build_file  = any(
            p in file_paths_lower for p in (
                "pom.xml","package.json","go.mod","cargo.toml","gemfile","requirements.txt",
            )
        ) or any(p.endswith(".csproj") for p in file_paths_lower)
        has_dockerfile  = "dockerfile" in file_paths_lower

        checks = [
            {"name": "min_file_count",
             "passed": files_count >= min_expected,
             "detail": f"{files_count} files generated (expected ≥{min_expected} for {lang_tag}/{len(entities)} entities)"},
            {"name": "has_entrypoint",
             "passed": has_entrypoint,
             "detail": "Application entry point present" if has_entrypoint else "Missing entry point (main.py/index.js/Application.java etc.)"},
            {"name": "has_build_file",
             "passed": has_build_file,
             "detail": "Build/dependency file present" if has_build_file else "Missing pom.xml/package.json/requirements.txt etc."},
            {"name": "has_dockerfile",
             "passed": has_dockerfile,
             "detail": "Dockerfile present" if has_dockerfile else "Missing Dockerfile"},
            {"name": "repo_created",
             "passed": bool(state.get("code_repo_url")),
             "detail": state.get("code_repo_url") or "repo not created"},
        ]
        score = sum(1 for c in checks if c["passed"]) / len(checks)
        # Only block if we have truly no files at all
        return QualityGate(gate_name="development", passed=files_count >= 1,
                           score=score, checks=checks, blocking=False)


# ════════════════════════════════════════════════════════════════════════════
# 5. TESTING (QAAgent)
# ════════════════════════════════════════════════════════════════════════════
class QAAgent(BaseSDLCAgent):
    phase_name = "testing"

    @property
    def system_prompt(self):
        return (
            "You are a Senior QA Engineer. Generate REAL test files for the exact language in the prompt.\n\n"
            "LANGUAGE RULES:\n"
            "- Java/Spring Boot  → JUnit 5 tests (@SpringBootTest, MockMvc, WebMvcTest)\n"
            "- Python/FastAPI    → pytest + httpx or TestClient\n"
            "- Node.js/Express  → Jest + supertest\n"
            "- Go               → Go testing package\n"
            "NEVER generate pytest for a Java project. NEVER generate JUnit for a Python project.\n\n"
            "Return ONLY this JSON:\n"
            '{"test_files":[{"path":"<correct path for language>","content":"<full test code>"}],'
            '"unit_tests_generated":5,"unit_tests_passed":5,"unit_tests_failed":0,'
            '"coverage_percent":75.0}\n\n'
            "Java example path:   src/test/java/com/app/OrderControllerTest.java\n"
            "Python example path: tests/test_main.py\n"
            "Node example path:   __tests__/app.test.js\n\n"
            "RULES:\n"
            "- Each test must be runnable standalone — no external DB needed\n"
            "- Use mocks/stubs for DB and external services\n"
            "- Always test the /health endpoint\n"
            "- Test at least one CRUD operation per entity\n"
            "- Output ONLY the JSON"
        )

    def human_prompt(self, state):
        design  = state.get("design") or {}
        stack   = design.get("tech_stack", {})
        apis    = design.get("api_contracts", [])[:_C["apis"]]
        models  = design.get("data_models", [])[:_C["models"]]
        lang    = stack.get("language", "Python")
        fw      = stack.get("framework", "FastAPI")
        db      = stack.get("database", "PostgreSQL")

        api_lines = NL.join(
            f"  {e.get('method','GET')} {e.get('path','')} — {e.get('description','')}"
            for e in apis
        ) or "  GET /health"

        model_lines = NL.join(
            f"  {m.get('entity','')}: {[f.get('name') for f in m.get('fields',[])[:5]]}"
            for m in models
        ) or "  No models defined"

        return (
            f"Project: {state['project_name']}\n"
            f"Language: {lang} / Framework: {fw} / Database: {db}\n\n"
            f"=== API ENDPOINTS TO TEST ===\n{api_lines}\n\n"
            f"=== DATA MODELS ===\n{model_lines}\n\n"
            f"Generate {lang}/{fw} tests for ALL endpoints above.\n"
            f"Use mocks for {db} — no real DB connection needed.\n"
            f"Output ONLY JSON."
        )

    def parse_output(self, response, state):
        try:
            data = self._extract_json(response)
            return {
                "test_coverage":  float(data.get("coverage_percent", 0)),
                "test_passed":    int(data.get("unit_tests_passed", 0)),
                "test_failed":    int(data.get("unit_tests_failed", 0)),
                "test_files":     data.get("test_files", []),
            }
        except Exception:
            return {"test_coverage": 0.0, "test_passed": 0, "test_failed": 0, "test_files": []}

    async def execute_tools(self, parsed, state, tools):
        import structlog as _sl
        _log = _sl.get_logger("qa_tools")
        log_entries = []
        from src.utils.docker_executor import DockerExecutor, _docker_available

        generated_files = state.get("generated_files", [])
        test_files      = parsed.get("test_files", [])

        print(f"  [QA TOOLS] generated_files={len(generated_files)} test_files={len(test_files)}", flush=True)
        print(f"  [QA TOOLS] docker available: {_docker_available()}", flush=True)

        if not generated_files and not test_files:
            print("  [QA TOOLS] No files — skipping Docker test", flush=True)
            return log_entries

        file_map = {f.get("path",""): f for f in generated_files if f.get("path")}
        for tf in test_files:
            if tf.get("path"):
                file_map[tf["path"]] = tf
        all_files = list(file_map.values())
        print(f"  [QA TOOLS] merged all_files={len(all_files)}", flush=True)

        tool_map = {t.name: t for t in tools}
        slug = state["project_name"].lower().replace(" ", "-")
        if "gitea_push_file" in tool_map and state.get("code_branch"):
            for tf in test_files:
                try:
                    await tool_map["gitea_push_file"].ainvoke({
                        "repo": slug, "branch": state["code_branch"],
                        "file_path": tf.get("path", "test_auto.py"),
                        "content": tf.get("content", ""),
                        "message": f"test: add {tf.get('path','tests')}",
                    })
                    log_entries.append({"tool": "gitea_push_file",
                                        "result": f"Pushed {tf.get('path')}"})
                except Exception as e:
                    log_entries.append({"tool": "gitea_push_file", "error": str(e)})

        if not _docker_available():
            log_entries.append({"tool": "docker_test",
                                 "result": "Docker not available — using LLM estimates"})
            return log_entries

        from src.api.server import _log_callbacks
        cb = _log_callbacks.get(state.get("run_id", ""))

        design     = state.get("design") or {}
        tech_stack = design.get("tech_stack", {})
        executor   = DockerExecutor(log_callback=cb, tech_stack=tech_stack)
        result     = await executor.build_and_test(
            files=all_files, project_name=state["project_name"], run_tests=True,
        )

        if result.get("success"):
            parsed["test_coverage"] = result["coverage_percent"]
            parsed["test_passed"]   = result["test_passed"]
            parsed["test_failed"]   = result["test_failed"]
            parsed["test_output"]  = result["test_output"]
            parsed["image_tag"]    = result["image_tag"]

        # MinIO: store test report as both a human-readable markdown AND
        # the raw test output. Raw stdout is already useful but we add
        # a structured summary for dashboards.
        if "minio_upload_artifact" in tool_map and result.get("test_output"):
            try:
                proj     = state["project_name"]
                passed   = result.get("test_passed", 0)
                failed   = result.get("test_failed", 0)
                coverage = result.get("coverage_percent", 0)
                image    = result.get("image_tag", "")
                total    = passed + failed
                status   = "✅ PASSED" if failed == 0 else "❌ FAILED"

                # 1. Human-readable markdown report
                md_report = (
                    f"# Test Report — {proj}" + NL + NL +
                    f"**Status:** {status}  " + NL +
                    f"**Tests:** {passed} passed / {failed} failed / {total} total  " + NL +
                    f"**Coverage:** {coverage:.1f}%  " + NL +
                    f"**Docker image:** `{image}`  " + NL + NL +
                    "## Raw Output" + NL + NL +
                    "```" + NL +
                    result["test_output"][:8000] +   # cap to avoid huge objects
                    NL + "```" + NL
                )
                r_md = tool_map["minio_upload_artifact"].invoke({
                    "object_name":  f"{proj}/test-report.md",
                    "content":      md_report,
                    "content_type": "text/markdown",
                })
                d_md = json.loads(r_md)
                log_entries.append({"tool": "minio_upload_artifact",
                                    "result": d_md.get("url"),
                                    "note": "Test report (markdown)"})

                # 2. Raw output as plain text (for CI tools that parse stdout)
                r_txt = tool_map["minio_upload_artifact"].invoke({
                    "object_name":  f"{proj}/test-output.txt",
                    "content":      result["test_output"],
                    "content_type": "text/plain",
                })
                d_txt = json.loads(r_txt)
                log_entries.append({"tool": "minio_upload_artifact",
                                    "result": d_txt.get("url"),
                                    "note": "Raw test stdout"})

            except Exception as e:
                log_entries.append({"tool": "minio_upload_artifact", "error": str(e)})

        log_entries.append({"tool": "docker_test", "result": {
            "passed":   result["test_passed"],
            "failed":   result["test_failed"],
            "coverage": result["coverage_percent"],
            "image":    result.get("image_tag"),
        }})
        return log_entries

    def quality_gate(self, state):
        cov   = state.get("test_coverage", 0)
        total = state.get("test_passed", 0) + state.get("test_failed", 0)
        passed = state.get("test_passed", 0)
        failed = state.get("test_failed", 0)
        prate = passed / total if total > 0 else 0
        has_real_results = total > 0
        checks = [
            {"name": "build_succeeded", "passed": bool(state.get("image_tag")),
             "detail": f"Docker image built: {state.get('image_tag','none')}"},
            {"name": "tests_ran",       "passed": has_real_results,
             "detail": f"Tests executed: {total} total"},
            {"name": "pass_rate",       "passed": failed == 0 or prate >= 0.7,
             "detail": f"Pass rate: {prate:.0%} ({passed} passed, {failed} failed)"},
            {"name": "coverage",        "passed": cov >= 50 or not has_real_results,
             "detail": f"Coverage: {cov:.1f}%"},
        ]
        score = sum(1 for c in checks if c["passed"]) / len(checks)
        build_ok = bool(state.get("image_tag"))
        return QualityGate(gate_name="testing", passed=build_ok,
                           score=score, checks=checks, blocking=True)


# ════════════════════════════════════════════════════════════════════════════
# 6. CI/CD
# ════════════════════════════════════════════════════════════════════════════
class CICDAgent(BaseSDLCAgent):
    phase_name = "ci_cd"

    @property
    def system_prompt(self):
        return (
            "You are a DevOps Engineer. Describe the CI/CD pipeline.\n\n"
            "Return this exact JSON:\n"
            '{"ci_status":"success","ci_url":null,'
            '"stages_passed":["lint","test","build","docker"],'
            '"stages_failed":[]}\n\n'
            "RULES:\n"
            "- ci_status: success if tests passed, failure if not\n"
            "- ci_url: always null\n"
            "- Output ONLY the JSON. Start with { and end with }"
        )

    def human_prompt(self, state):
        design = state.get("design") or {}
        return (
            f"Project: {state['project_name']}\n"
            f"Tech: {design.get('tech_stack', {})}\n"
            f"Test coverage: {state.get('test_coverage', 0):.1f}%\n"
            f"Tests: passed={state.get('test_passed',0)} failed={state.get('test_failed',0)}\n\n"
            f"Describe CI pipeline status. Output ONLY JSON."
        )

    def parse_output(self, response, state):
        try:
            data = self._extract_json(response)
            return {
                "ci_status": data.get("ci_status", "running"),
                "ci_url":    data.get("ci_url"),
            }
        except Exception:
            return {"ci_status": "running"}

    async def execute_tools(self, parsed, state, tools):
        log_entries = []
        from src.utils.docker_executor import DockerExecutor, _docker_available

        generated_files = state.get("generated_files", [])
        if not generated_files:
            parsed["ci_status"] = "skipped"
            return log_entries

        tool_map = {t.name: t for t in tools}
        if "gitea_push_file" in tool_map and state.get("code_branch"):
            slug = state["project_name"].lower().replace(" ", "-")
            ci_yaml = (
                "name: CI\n"
                "on: [push, pull_request]\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v3\n"
                "      - uses: actions/setup-python@v4\n"
                "        with:\n"
                "          python-version: '3.12'\n"
                "      - run: pip install -r requirements.txt\n"
                "      - run: pytest --cov=. --cov-report=xml -v\n"
                "  build:\n"
                "    needs: test\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v3\n"
                "      - run: docker build -t app:latest .\n"
            )
            try:
                await tool_map["gitea_push_file"].ainvoke({
                    "repo": slug, "branch": state["code_branch"],
                    "file_path": ".gitea/workflows/ci.yml",
                    "content": ci_yaml,
                    "message": "ci: add Gitea Actions workflow",
                })
                log_entries.append({"tool": "gitea_push_file", "result": ".gitea/workflows/ci.yml"})
            except Exception as e:
                log_entries.append({"tool": "gitea_push_file", "error": str(e)})

        if _docker_available():
            from src.api.server import _log_callbacks
            cb = _log_callbacks.get(state.get("run_id", ""))
            design     = state.get("design") or {}
            tech_stack = design.get("tech_stack", {})
            executor   = DockerExecutor(log_callback=cb, tech_stack=tech_stack)
            result     = await executor.build_and_test(
                files=generated_files,
                project_name=state["project_name"],
                run_tests=False,
            )
            parsed["ci_status"]  = "success" if result.get("success") else "failure"
            parsed["image_tag"] = result.get("image_tag")
            log_entries.append({"tool": "docker_build", "result": {
                "status": parsed["ci_status"],
                "image":  result.get("image_tag"),
            }})

        if "mattermost_notify" in tool_map:
            try:
                icon = "✅" if parsed.get("ci_status") == "success" else "❌"
                await tool_map["mattermost_notify"].ainvoke({
                    "message": (f"{icon} **{state['project_name']}** CI pipeline "
                                f"{parsed.get('ci_status','unknown')}\n"
                                f"Coverage: {state.get('test_coverage',0):.1f}%  "
                                f"Tests: {state.get('test_passed',0)} passed"),
                })
                log_entries.append({"tool": "mattermost_notify", "result": "posted"})
            except Exception as e:
                log_entries.append({"tool": "mattermost_notify", "error": str(e)})

        return log_entries

    def quality_gate(self, state):
        ci  = state.get("ci_status", "unknown")
        ok  = ci in ("success", "skipped")
        checks = [{"name": "ci_status", "passed": ok, "detail": f"CI: {ci}"}]
        return QualityGate(gate_name="ci_cd", passed=ok,
                           score=1.0 if ok else 0.5,
                           checks=checks, blocking=False)


# ════════════════════════════════════════════════════════════════════════════
# 7. DEPLOYMENT
# ════════════════════════════════════════════════════════════════════════════
class DeploymentAgent(BaseSDLCAgent):
    phase_name = "deployment"

    @property
    def system_prompt(self):
        return (
            "You are an SRE. Describe the deployment plan.\n\n"
            "Return this exact JSON:\n"
            '{"staging_url":null,"production_url":null,"release_tag":"v1.0.0",'
            '"release_url":null,"deployment_status":"planned",'
            '"deployment_notes":"Brief deployment description"}\n\n'
            "RULES:\n"
            "- All URL fields: null\n"
            "- Output ONLY the JSON. Start with { and end with }"
        )

    def human_prompt(self, state):
        return (
            f"Project: {state['project_name']}\n"
            f"CI status: {state.get('ci_status', 'unknown')}\n"
            f"Coverage: {state.get('test_coverage', 0):.1f}%\n\n"
            f"Describe the deployment plan. Output ONLY JSON."
        )

    def parse_output(self, response, state):
        try:
            data = self._extract_json(response)
            return {
                "staging_url":    data.get("staging_url"),
                "production_url": data.get("production_url"),
            }
        except Exception:
            return {}

    # ── Port helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_port_free(port: int) -> bool:
        """
        Check if a TCP port is free on localhost using a real socket bind.
        Returns True if the port is available, False if already in use.
        """
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    @staticmethod
    def _get_docker_used_ports() -> set[int]:
        """
        Return the set of host ports already bound by running Docker containers.
        Calls `docker ps` so we catch ports occupied by containers even when
        the local socket appears free (e.g. Docker-in-Docker, remote daemon).
        Returns an empty set if Docker is unavailable or the call fails.
        """
        import subprocess
        used: set[int] = set()
        try:
            out = subprocess.check_output(
                ["docker", "ps", "--format", "{{.Ports}}"],
                timeout=5, stderr=subprocess.DEVNULL,
            ).decode()
            # Ports field looks like: "0.0.0.0:8090->8080/tcp, :::8090->8080/tcp"
            for token in re.findall(r":(\d{4,5})->", out):
                used.add(int(token))
        except Exception:
            pass
        return used

    @staticmethod
    def _find_free_port(start: int = 8090, end: int = 8200) -> int:
        """
        Scan [start, end) for a port that is:
          1. Not bound by any local socket (OS check), AND
          2. Not already used by a running Docker container.

        Returns the first free port found.
        Raises RuntimeError if every port in the range is occupied.
        """
        docker_used = DeploymentAgent._get_docker_used_ports()
        print(f"  [DEPLOY] Docker-occupied ports: {sorted(docker_used)}", flush=True)

        for port in range(start, end):
            if port in docker_used:
                print(f"  [DEPLOY] Port {port}: Docker container already bound — skipping", flush=True)
                continue
            if not DeploymentAgent._is_port_free(port):
                print(f"  [DEPLOY] Port {port}: OS socket busy — skipping", flush=True)
                continue
            print(f"  [DEPLOY] Port {port}: FREE ✓", flush=True)
            return port

        raise RuntimeError(
            f"No free port found in range {start}–{end}. "
            "Stop unused containers with `docker ps` and `docker stop <id>`."
        )

    async def execute_tools(self, parsed, state, tools):
        log_entries = []
        from src.utils.docker_executor import DockerExecutor, _docker_available

        image_tag = state.get("image_tag")
        tool_map  = {t.name: t for t in tools}
        slug      = re.sub(r"[^a-z0-9-]", "-", state["project_name"].lower())[:40].strip("-")

        print(f"  [DEPLOY] image_tag={image_tag}  docker={_docker_available()}", flush=True)

        if image_tag and _docker_available():
            try:
                from src.api.server import _log_callbacks
                cb       = _log_callbacks.get(state.get("run_id",""))
                design   = state.get("design") or {}
                executor = DockerExecutor(log_callback=cb, tech_stack=design.get("tech_stack",{}))

                # ── Dynamic port selection with retry ─────────────────────────
                # Never hardcode port=8090. Scan from 8090 upward, skip any port
                # already bound by the OS or by another Docker container.
                MAX_PORT_RETRIES = 5
                last_error       = None
                used_ports = set()
                for attempt in range(1, MAX_PORT_RETRIES + 1):
                    try:
                        host_port = self._find_free_port(start=8090, end=8200)

                        while host_port in used_ports:
                            host_port += 1
                            if host_port >= 8200:
                                raise RuntimeError("No free port available after filtering retries")
                            
                    except RuntimeError as e:
                        log_entries.append({"tool": "docker_run", "error": str(e)})
                        print(f"  [DEPLOY] {e}", flush=True)
                        break

                    print(f"  [DEPLOY] Attempt {attempt}/{MAX_PORT_RETRIES}: "
                          f"trying port {host_port}...", flush=True)

                    result = await executor.run_container(
                        image_tag=image_tag,
                        project_name=state["project_name"],
                        port=host_port,
                    )

                    # Success
                    if result.get("url"):
                        parsed["staging_url"]    = result["url"]
                        parsed["production_url"] = result["url"]
                        parsed["host_port"]      = host_port
                        log_entries.append({"tool": "docker_run",
                                            "result": result["url"],
                                            "port": host_port})
                        print(f"  [DEPLOY] Container running on port {host_port}: "
                              f"{result['url']}", flush=True)
                        break

                    # Port-in-use error — Docker reported it after we checked
                    err = result.get("error", "")
                    if "already in use" in err.lower() or "port is already" in err.lower() or \
                       "address already in use" in err.lower() or "bind:" in err.lower():
                        print(f"  [DEPLOY] Port {host_port} became occupied between "
                              f"check and bind — retrying (attempt {attempt})", flush=True)
                        used_ports.add(host_port)
                        last_error = err
                        # Mark this port as occupied for the next _find_free_port call
                        # by creating a temporary socket that holds it
                        # (Not ideal, but prevents re-selecting the same port)
                        continue

                    # Any other error — don't retry
                    last_error = err
                    log_entries.append({"tool": "docker_run", "error": err})
                    print(f"  [DEPLOY] Docker run failed (non-port error): {err}", flush=True)
                    break
                else:
                    # All retries exhausted
                    log_entries.append({"tool": "docker_run",
                                        "error": f"All {MAX_PORT_RETRIES} port attempts failed. "
                                                 f"Last error: {last_error}"})
                    print(f"  [DEPLOY] Gave up after {MAX_PORT_RETRIES} attempts", flush=True)

            except Exception as e:
                log_entries.append({"tool": "docker_run", "error": str(e)})
                print(f"  [DEPLOY] Exception in run_container: {e}", flush=True)
        else:
            reason = "no image built" if not image_tag else "Docker not available"
            log_entries.append({"tool": "docker_run", "result": f"skipped — {reason}"})
            print(f"  [DEPLOY] Skipping container start: {reason}", flush=True)

        code_repo = state.get("code_repo_url")
        if code_repo:
            d = await _call_tool(tool_map, "gitea_create_release", {
                "repo": slug, "tag": "v1.0.0",
                "name": f"{state['project_name']} v1.0.0",
                "body": (f"Tests: {state.get('test_passed',0)} passed, "
                         f"{state.get('test_failed',0)} failed  "
                         f"Coverage: {state.get('test_coverage',0):.1f}%  "
                         f"Repo: {code_repo}"),
            }, log_entries)

        url = parsed.get("staging_url") or "Docker not connected"
        await _call_tool(tool_map, "mattermost_phase_update", {
            "phase": "deployment", "status": "completed",
            "details": f"{state['project_name']} — {url}",
        }, log_entries)

        return log_entries

    def quality_gate(self, state):
        has_image = bool(state.get("image_tag"))
        has_url   = bool(state.get("staging_url"))
        has_repo  = bool(state.get("code_repo_url"))
        passed    = has_repo or has_image or has_url
        checks = [
            {"name": "code_repo",   "passed": has_repo,  "detail": state.get("code_repo_url") or "no repo"},
            {"name": "image_built", "passed": has_image, "detail": state.get("image_tag") or "no image"},
            {"name": "deployed",    "passed": has_url,   "detail": state.get("staging_url") or "not running"},
        ]
        score = sum(1 for c in checks if c["passed"]) / len(checks)
        return QualityGate(gate_name="deployment", passed=passed,
                           score=score, checks=checks, blocking=False)


# ════════════════════════════════════════════════════════════════════════════
# 8. MONITORING
# ════════════════════════════════════════════════════════════════════════════
class MonitoringAgent(BaseSDLCAgent):
    phase_name = "monitoring"

    @property
    def system_prompt(self):
        return (
            "You are an SRE. Define monitoring configuration.\n\n"
            "Return this exact JSON:\n"
            '{"monitoring_config":{"slos":['
            '{"name":"availability","target":99.9},'
            '{"name":"latency_p95_ms","target":200},'
            '{"name":"error_rate_pct","target":0.1}],'
            '"alerts":['
            '{"name":"high_error_rate","condition":"error_rate > 0.5%","severity":"critical"},'
            '{"name":"high_latency","condition":"p95 > 200ms","severity":"warning"}],'
            '"health_check_path":"/health","runbook_url":null}}\n\n'
            "RULES:\n"
            "- runbook_url: null\n"
            "- Output ONLY the JSON. Start with { and end with }"
        )

    def human_prompt(self, state):
        return (
            f"Project: {state['project_name']}\n"
            f"Production URL: {state.get('production_url') or 'not deployed'}\n\n"
            f"Configure SLOs and alerts. Output ONLY JSON."
        )

    def parse_output(self, response, state):
        try:
            data = self._extract_json(response)
            return {"monitoring_config": data.get("monitoring_config", {})}
        except Exception:
            return {"monitoring_config": {}}

    async def execute_tools(self, parsed, state, tools):
        log_entries = []
        tool_map = {t.name: t for t in tools}
        mc = parsed.get("monitoring_config") or {}

        if "outline_create_document" in tool_map:
            try:
                slos   = mc.get("slos", [])
                alerts = mc.get("alerts", [])
                lines  = [f"# Monitoring Runbook — {state['project_name']}", "",
                          f"## Service", state.get("production_url", "TBD"), "",
                          "## SLOs"]
                for s in slos:
                    lines.append(f"- **{s.get('name')}**: {s.get('target')}")
                lines += ["", "## Alert Rules"]
                for a in alerts:
                    lines.append(f"- **{a.get('name')}** [{a.get('severity','warning')}]: {a.get('condition','')}")
                lines += ["", "## Health Check",
                           f"Endpoint: `GET {mc.get('health_check_path','/health')}`"]

                coll_id = ""
                if "outline_create_collection" in tool_map:
                    dc = await _call_tool(tool_map, "outline_create_collection", {
                        "name": f"{state['project_name']} — Monitoring",
                        "description": "SLOs, runbooks, and alerts",
                    }, log_entries)
                    coll_id = dc.get("collection_id", "")

                r = await tool_map["outline_create_document"].ainvoke({
                    "title": f"{state['project_name']} Monitoring Runbook",
                    "text":  NL.join(lines),
                    "collection_id": coll_id,
                })
                d = json.loads(r)
                mc["runbook_url"] = d.get("url") or d.get("document_id")
                parsed["monitoring_config"] = mc
                log_entries.append({"tool": "outline_create_document",
                                    "result": d.get("url") or d.get("document_id")})
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        if "mattermost_notify" in tool_map:
            try:
                slo_summary = ", ".join(f"{s.get('name')}={s.get('target')}"
                                        for s in mc.get("slos", []))
                await tool_map["mattermost_notify"].ainvoke({
                    "message": (f"📊 **{state['project_name']}** monitoring configured\n"
                                f"SLOs: {slo_summary}\n"
                                f"Alerts: {len(mc.get('alerts', []))} rules"),
                })
                log_entries.append({"tool": "mattermost_notify", "result": "posted"})
            except Exception as e:
                log_entries.append({"tool": "mattermost_notify", "error": str(e)})

        # MinIO: store Prometheus alert rules as a real YAML file that can be
        # directly mounted into an Alertmanager/Prometheus deployment.
        # JSON blob of SLO config is not actionable — YAML rules are.
        if "minio_upload_artifact" in tool_map:
            try:
                slos   = mc.get("slos", [])
                alerts = mc.get("alerts", [])
                health = mc.get("health_check_path", "/health")
                proj   = state["project_name"]
                url    = state.get("production_url") or state.get("staging_url") or "http://localhost:8080"

                # Build a real Prometheus alert rules YAML
                alert_rules = []
                for a in alerts:
                    severity  = a.get("severity", "warning")
                    condition = a.get("condition", "")
                    alert_rules.append(
                        f"  - alert: {re.sub(r'[^a-zA-Z0-9_]', '_', a.get('name','alert'))}" + NL +
                        f"    expr: {condition}" + NL +
                        f"    for: 1m" + NL +
                        f"    labels:" + NL +
                        f"      severity: {severity}" + NL +
                        f"    annotations:" + NL +
                        f"      summary: \"{a.get('name','')} triggered\"" + NL +
                        f"      description: \"{condition}\""
                    )

                # Build Prometheus scrape config
                prometheus_yaml = (
                    f"# Prometheus config for {proj}" + NL +
                    f"# Generated by SDLC pipeline" + NL + NL +
                    "global:" + NL +
                    "  scrape_interval: 15s" + NL +
                    "  evaluation_interval: 15s" + NL + NL +
                    "rule_files:" + NL +
                    f"  - \"{re.sub(r'[^a-z0-9_]', '_', proj.lower())}_alerts.yml\"" + NL + NL +
                    "scrape_configs:" + NL +
                    f"  - job_name: \"{proj}\"" + NL +
                    f"    static_configs:" + NL +
                    f"      - targets: [\"{url.replace('http://','').replace('https://','')}\"]" + NL +
                    f"    metrics_path: \"{health}\"" + NL
                )

                # Build alert rules file
                alert_slug = re.sub(r'[^a-z0-9_]', '_', proj.lower())
                alert_yaml = (
                    f"# Alert rules for {proj}" + NL +
                    "groups:" + NL +
                    f"  - name: {alert_slug}_alerts" + NL +
                    "    rules:" + NL +
                    (NL.join(alert_rules) if alert_rules else "    []") + NL
                )

                # Build SLO summary as a human-readable markdown (not JSON)
                slo_md = (
                    f"# SLOs — {proj}" + NL + NL +
                    "| SLO | Target |" + NL +
                    "|-----|--------|" + NL +
                    NL.join(f"| {s.get('name','')} | {s.get('target','')} |" for s in slos) + NL + NL +
                    f"Health check: `GET {health}`" + NL +
                    f"Production: {url}" + NL
                )

                # Store all 3 as separate objects
                for obj_name, content, ctype in [
                    (f"{proj}/monitoring/prometheus.yml",          prometheus_yaml, "text/yaml"),
                    (f"{proj}/monitoring/{alert_slug}_alerts.yml", alert_yaml,      "text/yaml"),
                    (f"{proj}/monitoring/slos.md",                 slo_md,          "text/markdown"),
                ]:
                    r2 = tool_map["minio_upload_artifact"].invoke({
                        "object_name":  obj_name,
                        "content":      content,
                        "content_type": ctype,
                    })
                    d2 = json.loads(r2)
                    log_entries.append({"tool": "minio_upload_artifact",
                                        "result": d2.get("url"),
                                        "note": f"Stored: {obj_name}"})

            except Exception as e:
                log_entries.append({"tool": "minio_upload_artifact", "error": str(e)})

        return log_entries

    def quality_gate(self, state):
        mc = state.get("monitoring_config") or {}
        checks = [
            {"name": "has_slos",   "passed": bool(mc.get("slos")),
             "detail": f"{len(mc.get('slos', []))} SLOs defined"},
            {"name": "has_alerts", "passed": bool(mc.get("alerts")),
             "detail": f"{len(mc.get('alerts', []))} alert rules"},
        ]
        score = sum(1 for c in checks if c["passed"]) / len(checks)
        return QualityGate(gate_name="monitoring", passed=score >= 0.5,
                           score=score, checks=checks, blocking=False)


# ════════════════════════════════════════════════════════════════════════════
# 9. DOCUMENTATION
# ════════════════════════════════════════════════════════════════════════════
class DocumentationAgent(BaseSDLCAgent):
    phase_name = "documentation"

    @property
    def system_prompt(self):
        return (
            "You are a Technical Writer. Describe the documentation created.\n\n"
            "Return this exact JSON:\n"
            '{"documentation_url":null,"readme_url":null,"pages_created":5,'
            '"summary":"Description of documentation created"}\n\n'
            "RULES:\n"
            "- documentation_url: null\n"
            "- readme_url: null\n"
            "- Output ONLY the JSON. Start with { and end with }"
        )

    def human_prompt(self, state):
        design = state.get("design") or {}
        return (
            f"Project: {state['project_name']}\n"
            f"Requirements: {len(state.get('requirements', []))}\n"
            f"Work items: {len(state.get('work_items', []))}\n"
            f"Repo: {state.get('code_repo_url') or 'not created'}\n"
            f"Coverage: {state.get('test_coverage', 0):.1f}%\n\n"
            f"Describe the documentation. Output ONLY JSON."
        )

    def parse_output(self, response, state):
        try:
            data = self._extract_json(response)
            return {"documentation_url": data.get("documentation_url")}
        except Exception:
            return {"documentation_url": None}

    async def execute_tools(self, parsed, state, tools):
        log_entries = []
        tool_map = {t.name: t for t in tools}
        reqs   = state.get("requirements", [])
        items  = state.get("work_items", [])
        design = state.get("design") or {}

        collection_id = None
        if "outline_create_collection" in tool_map:
            try:
                r = await tool_map["outline_create_collection"].ainvoke({
                    "name":        f"{state['project_name']} — Project Wiki",
                    "description": f"Complete documentation for {state['project_name']}",
                })
                d = json.loads(r)
                collection_id = d.get("collection_id")
                parsed["documentation_url"] = d.get("url")
                log_entries.append({"tool": "outline_create_collection", "result": d.get("url")})
            except Exception as e:
                log_entries.append({"tool": "outline_create_collection", "error": str(e)})

        if not collection_id:
            collection_id = ""

        if "outline_create_document" in tool_map:
            try:
                lines = [f"# {state['project_name']}", "",
                         "## Overview", design.get("architecture_summary", ""), "",
                         "## Links"]
                if state.get("code_repo_url"):
                    lines.append(f"- **Repository**: {state['code_repo_url']}")
                if state.get("staging_url"):
                    lines.append(f"- **App**: {state['staging_url']}")
                lines += ["", "## Stats",
                           f"- Requirements: {len(reqs)}",
                           f"- Work items: {len(items)}",
                           f"- Test coverage: {state.get('test_coverage',0):.1f}%",
                           f"- CI: {state.get('ci_status','—')}"]
                await tool_map["outline_create_document"].ainvoke({
                    "title": "Home", "text": NL.join(lines),
                    "collection_id": collection_id,
                })
                log_entries.append({"tool": "outline_create_document", "result": "Home page"})
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        if "outline_create_document" in tool_map:
            try:
                apis   = design.get("api_contracts", [])
                models = design.get("data_models", [])
                lines  = [
                    f"# API Reference — {state['project_name']}", "",
                    "> Complete API documentation.", "",
                    f"**Base URL:** `http://localhost:8080`", "",
                    "---", "",
                ]
                from collections import defaultdict as _dd
                grouped = _dd(list)
                for ep in apis:
                    path = ep.get("path", "/")
                    segs = [p for p in path.split("/") if p and not p.startswith("{") and p not in ("api","v1","v2")]
                    resource = segs[0].replace("-","_").title() if segs else "General"
                    grouped[resource].append(ep)

                for resource, endpoints in grouped.items():
                    lines += [f"## {resource} Endpoints", ""]
                    for ep in endpoints:
                        method = ep.get("method","GET")
                        path   = ep.get("path","")
                        desc   = ep.get("description","")
                        req_b  = ep.get("request_body")
                        res_ok = ep.get("response_success")
                        res_err= ep.get("response_error")
                        lines += [f"### `{method} {path}`", f"_{desc}_", ""]
                        if req_b:
                            lines += ["**Request Body:**","```json", json.dumps(req_b, indent=2), "```",""]
                        if res_ok:
                            lines += [f"**✅ Success `{res_ok.get('status',200)}`:**","```json", json.dumps(res_ok.get("body",{}), indent=2), "```",""]
                        if res_err:
                            lines += [f"**❌ Error `{res_err.get('status',400)}`:**","```json", json.dumps(res_err.get("body",{}), indent=2), "```",""]
                        lines += ["---",""]

                if models:
                    lines += ["","## Data Models",""]
                    for m in models:
                        lines += [f"### `{m.get('entity','')}`", m.get("description",""), ""]
                        fields = m.get("fields",[])
                        if fields:
                            lines += ["| Field | Type | Required | Description |","|-------|------|----------|-------------|"]
                            for f in fields:
                                req_f = "✓" if f.get("required") else ""
                                lines.append(f"| `{f.get('name','')}` | `{f.get('type','')}` | {req_f} | {f.get('description','')} |")
                        lines.append("")

                doc_r = await tool_map["outline_create_document"].ainvoke({
                    "title": f"{state['project_name']} — API Reference",
                    "text": NL.join(lines),
                    "collection_id": collection_id,
                })
                doc_d = json.loads(doc_r)
                parsed["api_doc_url"] = doc_d.get("url")
                log_entries.append({"tool": "outline_create_document",
                                    "result": doc_d.get("url") or "API Reference"})
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        if "outline_create_document" in tool_map:
            try:
                lines = [f"# Traceability Matrix — {state['project_name']}", "",
                         "| Requirement | Work Item | Status |",
                         "|---|---|---|"]
                for req in reqs:
                    linked = [wi for wi in items
                              if req["id"] in wi.get("linked_requirements", [])]
                    for wi in linked:
                        lines.append(f"| {req['id']}: {req['title'][:35]} | {wi['id']}: {wi['title'][:35]} | {wi.get('status','backlog')} |")
                    if not linked:
                        lines.append(f"| {req['id']}: {req['title'][:35]} | — | — |")
                await tool_map["outline_create_document"].ainvoke({
                    "title": "Traceability Matrix", "text": NL.join(lines),
                    "collection_id": collection_id,
                })
                log_entries.append({"tool": "outline_create_document", "result": "Traceability matrix"})
            except Exception as e:
                log_entries.append({"tool": "outline_create_document", "error": str(e)})

        slug = state["project_name"].lower().replace(" ", "-")
        if "gitea_push_file" in tool_map and state.get("code_repo_url"):
            try:
                stack = design.get("tech_stack", {})
                lines = [f"# {state['project_name']}", "",
                         design.get("architecture_summary", ""), "",
                         "## Tech Stack"]
                for k, v in stack.items():
                    lines.append(f"- **{k}**: {v}")
                lines += ["", "## Quick Start",
                           "```bash", "docker compose up -d", "```", "",
                           "## Test Results",
                           f"- Passed: {state.get('test_passed',0)}",
                           f"- Failed: {state.get('test_failed',0)}",
                           f"- Coverage: {state.get('test_coverage',0):.1f}%"]
                await tool_map["gitea_push_file"].ainvoke({
                    "repo": slug, "branch": "main",
                    "file_path": "README.md",
                    "content": NL.join(lines),
                    "message": "docs: update README",
                })
                log_entries.append({"tool": "gitea_push_file", "result": "README.md"})
            except Exception as e:
                log_entries.append({"tool": "gitea_push_file", "error": str(e)})

        if "mattermost_phase_update" in tool_map:
            try:
                await tool_map["mattermost_phase_update"].ainvoke({
                    "phase": "documentation", "status": "completed",
                    "details": (f"Wiki published for {state['project_name']}\n"
                                f"Coverage: {state.get('test_coverage',0):.1f}%"),
                })
                log_entries.append({"tool": "mattermost_phase_update", "result": "notified"})
            except Exception as e:
                log_entries.append({"tool": "mattermost_phase_update", "error": str(e)})

        return log_entries

    def quality_gate(self, state):
        has_doc = bool(state.get("documentation_url"))
        return QualityGate(
            gate_name="documentation", passed=True,
            score=1.0 if has_doc else 0.5,
            checks=[{"name": "docs_published", "passed": has_doc,
                     "detail": state.get("documentation_url") or "outline not configured"}],
            blocking=False,
        )
def _is_valid_java_file(path: str, content: str, pkg: str, entities: list[str]) -> bool:
    """
    For Java controller/service/repository files, verify that
    all referenced entity classes are actually imported.
    Returns False if the file looks broken — fallback will be used instead.
    """
    if not path.endswith(".java"):
        return True
    
    lower = path.lower()
    # Only validate files that reference entities
    is_controller  = "controller" in lower
    is_service     = "service" in lower
    is_repository  = "repository" in lower
    
    if not any([is_controller, is_service, is_repository]):
        return True  # model/entity files — don't validate
    
    for entity in entities:
        pascal = re.sub(r"[^a-zA-Z0-9]", " ", entity).title().replace(" ", "")
        # If the file references the entity class name but doesn't import it → broken
        if pascal in content and f"import {pkg}.model.{pascal}" not in content:
            print(f"  [DEV VALIDATE] {path} references {pascal} but missing import → using fallback", flush=True)
            return False
    return True

def _extract_tech_from_doc(raw_doc: str) -> dict:
    """
    Scan the raw requirements document for explicit technology mentions.
    Returns a dict with language, framework, and a human-readable constraint string.
    This is the single source of truth — overrides any LLM assumption.
    """
    doc_lower = raw_doc.lower()

    # ── Language detection — order matters ──────────────────────────────
    lang     = None
    framework = None
    reject   = []

    if "python" in doc_lower:
        lang = "Python 3.12"
        framework = "FastAPI"
        if "flask" in doc_lower:
            framework = "Flask"
        elif "django" in doc_lower:
            framework = "Django"
        reject = ["Java", "Spring Boot", "Node.js", "Express", "NestJS"]

    elif any(x in doc_lower for x in ["node.js", "nodejs", "node js"]):
        lang = "Node.js 20"
        framework = "Express"
        if "nest" in doc_lower:
            framework = "NestJS"
        reject = ["Java", "Spring Boot", "Python", "FastAPI"]

    elif "typescript" in doc_lower and "python" not in doc_lower:
        lang = "TypeScript / Node.js 20"
        framework = "Express"
        if "nest" in doc_lower:
            framework = "NestJS"
        reject = ["Java", "Spring Boot", "Python"]

    elif any(x in doc_lower for x in ["golang", "go lang", " go "]):
        lang = "Go 1.21"
        framework = "Gin"
        reject = ["Java", "Spring Boot", "Python", "Node.js"]

    elif any(x in doc_lower for x in ["c#", "csharp", ".net", "dotnet", "asp.net"]):
        lang = "C# / .NET 8"
        framework = "ASP.NET Core"
        reject = ["Java", "Spring Boot", "Python", "Node.js"]

    elif any(x in doc_lower for x in ["ruby", "rails"]):
        lang = "Ruby 3.3"
        framework = "Rails"
        reject = ["Java", "Spring Boot", "Python", "Node.js"]

    elif any(x in doc_lower for x in ["rust", "actix", "axum"]):
        lang = "Rust"
        framework = "Axum"
        reject = ["Java", "Spring Boot", "Python", "Node.js"]

    elif any(x in doc_lower for x in ["java", "spring"]):
        lang = "Java 17"
        framework = "Spring Boot 3"
        reject = []

    # ── Build constraint string ──────────────────────────────────────────
    if lang:
        reject_str = ""
        if reject:
            reject_str = f"\n   ❌ FORBIDDEN: Do NOT use {', '.join(reject)}"
        constraint = (
            f"   ✅ Language : {lang}\n"
            f"   ✅ Framework: {framework}"
            f"{reject_str}"
        )
    else:
        lang       = "Python 3.12"
        framework  = "FastAPI"
        constraint = (
            f"   ✅ Language : {lang} (defaulted — none specified)\n"
            f"   ✅ Framework: {framework}"
        )

    return {
        "language":   lang,
        "framework":  framework,
        "constraint": constraint,
        "reject":     reject,
    }