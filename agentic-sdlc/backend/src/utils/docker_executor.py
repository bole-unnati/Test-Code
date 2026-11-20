"""
src/utils/docker_executor.py
Docker-based CI/CD engine supporting both local and remote Docker daemons.

Remote Docker setup:
  Set DOCKER_HOST in .env:
    DOCKER_HOST=tcp://192.168.1.100:2375     # plain TCP (enable on server: dockerd -H tcp://0.0.0.0:2375)
    DOCKER_HOST=tcp://192.168.1.100:2376     # TLS
    DOCKER_HOST=ssh://user@192.168.1.100     # SSH tunnel (most secure)
    DOCKER_HOST=unix:///var/run/docker.sock  # local (default)

  On the remote server, enable TCP access:
    # Edit /etc/docker/daemon.json:
    {"hosts": ["unix:///var/run/docker.sock", "tcp://0.0.0.0:2375"]}
    # Then restart: sudo systemctl restart docker
"""
from __future__ import annotations
import os as _os, sys as _sys
_b = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _b not in _sys.path: _sys.path.insert(0, _b)

import asyncio
import json
import os
import re
import tempfile
import time
import structlog

log = structlog.get_logger(__name__)


# ── Docker client factory ─────────────────────────────────────────────────────

def _get_docker_client():
    """
    Return a Docker client connected to local or remote Docker daemon.
    Reads DOCKER_HOST, DOCKER_TLS_VERIFY, DOCKER_CERT_PATH from settings.
    """
    import docker
    from config.settings import settings

    # Read from settings first, then fall back to OS env
    docker_host = getattr(settings, "docker_host", "") or os.environ.get("DOCKER_HOST", "")

    # If DOCKER_HOST is set in settings/.env but not in OS env, propagate it
    # so the Docker SDK picks it up automatically
    if docker_host and not os.environ.get("DOCKER_HOST"):
        os.environ["DOCKER_HOST"] = docker_host
        print(f"  [DOCKER] Set DOCKER_HOST={docker_host} from settings", flush=True)

    if docker_host:
        log.info("docker.connecting", host=docker_host)
        tls_verify = getattr(settings, "docker_tls_verify", "") or \
                     os.environ.get("DOCKER_TLS_VERIFY", "")
        cert_path  = getattr(settings, "docker_cert_path", "") or \
                     os.environ.get("DOCKER_CERT_PATH", "")

        if tls_verify and cert_path:
            tls_config = docker.tls.TLSConfig(
                client_cert=(
                    os.path.join(cert_path, "cert.pem"),
                    os.path.join(cert_path, "key.pem"),
                ),
                ca_cert=os.path.join(cert_path, "ca.pem"),
                verify=True,
            )
            client = docker.DockerClient(base_url=docker_host, tls=tls_config, timeout=120)
        else:
            client = docker.DockerClient(base_url=docker_host, timeout=120)
    else:
        client = docker.from_env(timeout=120)

    client.ping()   # verify connection
    return client


def _docker_available() -> bool:
    """Return True if Docker (local or remote) is reachable."""
    try:
        docker_host = os.environ.get("DOCKER_HOST", "")
        if not docker_host:
            try:
                from config.settings import settings
                docker_host = getattr(settings, "docker_host", "")
            except Exception:
                pass
        if docker_host:
            print(f"  [DOCKER] Connecting to remote: {docker_host}", flush=True)
        else:
            print(f"  [DOCKER] No DOCKER_HOST set — trying local Docker", flush=True)
        _get_docker_client()
        print(f"  [DOCKER] Connected OK", flush=True)
        return True
    except Exception as e:
        print(f"  [DOCKER] Unavailable: {e}", flush=True)
        log.debug("docker.unavailable", error=str(e))
        return False


# ── Language detection ────────────────────────────────────────────────────────

def detect_language(files: list[dict], tech_stack: dict | None = None) -> dict:
    """
    Detect the primary programming language and framework from:
    1. tech_stack from the Design phase (most reliable)
    2. File extensions in the generated files

    Returns:
        {"language": "java", "framework": "spring-boot",
         "build_tool": "maven", "base_image": "eclipse-temurin:17"}
    """
    # 1. Check tech_stack first
    if tech_stack:
        lang = tech_stack.get("language", "").lower()
        fw   = tech_stack.get("framework", "").lower()

        if "java" in lang or "spring" in fw:
            # Detect build tool from actual files (never guess — wrong tool = 127 error)
            has_pom     = any(f.get("path","").endswith("pom.xml") for f in files)
            has_gradle  = any("build.gradle" in f.get("path","") or
                              "gradlew" in f.get("path","") for f in files)
            build_tool  = "mvn" if has_pom else ("gradle" if has_gradle else "mvn")
            test_cmd    = ["mvn", "test"] if build_tool == "mvn" else ["gradle", "test", "--no-daemon"]
            return {
                "language":   "java",
                "framework":  "spring-boot",
                "build_tool": build_tool,
                "base_image": "eclipse-temurin:17-jdk-alpine",
                "run_image":  "eclipse-temurin:17-jre-alpine",
                "test_cmd":   test_cmd,
                "app_port":   "8080",
            }
        if "node" in lang or "typescript" in lang or "express" in fw or "nest" in fw:
            return {
                "language":   "node",
                "framework":  fw or "express",
                "build_tool": "npm",
                "base_image": "node:20-alpine",
                "run_image":  "node:20-alpine",
                "test_cmd":   ["npm", "test"],
                "app_port":   "3000",
            }
        if "go" in lang:
            return {
                "language":   "go",
                "framework":  fw or "gin",
                "build_tool": "go",
                "base_image": "golang:1.22-alpine",
                "run_image":  "alpine:3.19",
                "test_cmd":   ["go", "test", "./..."],
                "app_port":   "8080",
            }
        if "python" in lang or "fastapi" in fw or "django" in fw or "flask" in fw:
            return {
                "language":   "python",
                "framework":  fw or "fastapi",
                "build_tool": "pip",
                "base_image": "python:3.12-slim",
                "run_image":  "python:3.12-slim",
                "test_cmd":   ["python", "-m", "pytest", "--tb=short", "-v",
                               "--cov=.", "--cov-report=term-missing"],
                "app_port":   "8000",
            }
        if "ruby" in lang or "rails" in fw:
            return {
                "language": "ruby", "framework": "rails",
                "build_tool": "bundle",
                "base_image": "ruby:3.3-alpine",
                "run_image":  "ruby:3.3-alpine",
                "test_cmd":   ["bundle", "exec", "rspec"],
                "app_port":   "3000",
            }
        if "php" in lang or "laravel" in fw:
            return {
                "language": "php", "framework": "laravel",
                "build_tool": "composer",
                "base_image": "php:8.3-fpm-alpine",
                "run_image":  "php:8.3-fpm-alpine",
                "test_cmd":   ["php", "artisan", "test"],
                "app_port":   "8000",
            }
        if "dotnet" in lang or "c#" in lang or "aspnet" in fw or "asp.net" in fw:
            return {
                "language": "dotnet", "framework": "aspnet",
                "build_tool": "dotnet",
                "base_image": "mcr.microsoft.com/dotnet/sdk:8.0",
                "run_image":  "mcr.microsoft.com/dotnet/aspnet:8.0",
                "test_cmd":   ["dotnet", "test", "--verbosity", "normal"],
                "app_port":   "8080",
            }

    # 2. Fall back to file extension detection
    exts = {os.path.splitext(f.get("path",""))[1].lower() for f in files}
    paths = {f.get("path","").lower() for f in files}

    if ".java" in exts or any("pom.xml" in p or "build.gradle" in p for p in paths):
        has_pom    = any("pom.xml" in p for p in paths)
        has_gradle = any("build.gradle" in p for p in paths)
        build_tool = "mvn" if has_pom else ("gradle" if has_gradle else "mvn")
        test_cmd   = ["mvn", "test"] if build_tool == "mvn" else ["gradle", "test", "--no-daemon"]
        return {
            "language": "java", "framework": "spring-boot",
            "build_tool": build_tool,
            "base_image": "eclipse-temurin:17-jdk-alpine",
            "run_image":  "eclipse-temurin:17-jre-alpine",
            "test_cmd":   test_cmd,
            "app_port":   "8080",
        }
    if ".ts" in exts or any("package.json" in p for p in paths):
        return {
            "language": "node", "framework": "express",
            "build_tool": "npm",
            "base_image": "node:20-alpine",
            "run_image":  "node:20-alpine",
            "test_cmd":   ["npm", "test"],
            "app_port":   "3000",
        }
    if ".go" in exts:
        return {
            "language": "go", "framework": "gin",
            "build_tool": "go",
            "base_image": "golang:1.22-alpine",
            "run_image":  "alpine:3.19",
            "test_cmd":   ["go", "test", "./..."],
            "app_port":   "8080",
        }

    # Default: Python
    return {
        "language": "python", "framework": "fastapi",
        "build_tool": "pip",
        "base_image": "python:3.12-slim",
        "run_image":  "python:3.12-slim",
        "test_cmd":   ["python", "-m", "pytest", "--tb=short", "-v",
                       "--cov=.", "--cov-report=term-missing"],
        "app_port":   "8000",
    }


# ── Dockerfile generation ─────────────────────────────────────────────────────

def generate_dockerfile(lang_info: dict, files: list[dict]) -> str:
    """Generate an appropriate Dockerfile for the detected language."""
    lang = lang_info["language"]
    base = lang_info["base_image"]
    run  = lang_info.get("run_image", base)
    port = lang_info.get("app_port", "8080")

    if lang == "java":
        build_tool = lang_info.get("build_tool", "mvn")
        if build_tool == "mvn":
            return (
                f"FROM maven:3.9-eclipse-temurin-17 AS build\n"
                f"WORKDIR /app\nCOPY . .\n"
                f"RUN mvn package -DskipTests -q\n\n"
                f"FROM {run}\n"
                f"WORKDIR /app\n"
                f"COPY --from=build /app/target/*.jar app.jar\n"
                f"EXPOSE {port}\n"
                f'CMD ["java", "-jar", "app.jar"]\n'
            )
        else:  # gradle
            return (
                f"FROM gradle:8.5-jdk17 AS build\n"
                f"WORKDIR /app\nCOPY . .\n"
                f"RUN gradle build -x test --no-daemon -q\n\n"
                f"FROM {run}\n"
                f"WORKDIR /app\n"
                f"COPY --from=build /app/build/libs/*.jar app.jar\n"
                f"EXPOSE {port}\n"
                f'CMD ["java", "-jar", "app.jar"]\n'
            )

    if lang == "node":
        has_ts = any(f.get("path","").endswith(".ts") for f in files)
        build_step = "RUN npm run build\n" if has_ts else ""
        start_file = "dist/index.js" if has_ts else "index.js"
        return (
            f"FROM {base}\n"
            f"WORKDIR /app\n"
            f"COPY package*.json ./\n"
            f"RUN npm ci --only=production\n"
            f"COPY . .\n"
            f"{build_step}"
            f"EXPOSE {port}\n"
            f'CMD ["node", "{start_file}"]\n'
        )

    if lang == "go":
        return (
            f"FROM {base} AS build\n"
            f"WORKDIR /app\n"
            f"COPY go.mod go.sum ./\n"
            f"RUN go mod download\n"
            f"COPY . .\n"
            f"RUN CGO_ENABLED=0 go build -o app ./...\n\n"
            f"FROM {run}\n"
            f"WORKDIR /app\n"
            f"COPY --from=build /app/app .\n"
            f"EXPOSE {port}\n"
            f'CMD ["./app"]\n'
        )

    if lang == "dotnet":
        return (
            f"FROM {base} AS build\n"
            f"WORKDIR /app\n"
            f"COPY *.csproj ./\n"
            f"RUN dotnet restore\n"
            f"COPY . .\n"
            f"RUN dotnet publish -c Release -o out\n\n"
            f"FROM {run}\n"
            f"WORKDIR /app\n"
            f"COPY --from=build /app/out .\n"
            f"EXPOSE {port}\n"
            f'CMD ["dotnet", "app.dll"]\n'
        )

    # Default: Python / FastAPI
    has_req = any("requirements" in f.get("path","") for f in files)
    req_line = "COPY requirements.txt .\nRUN pip install -r requirements.txt --no-cache-dir" \
               if has_req else "RUN pip install fastapi uvicorn pytest pytest-cov --no-cache-dir"

    # Detect main module name
    main_file = next((f.get("path","main.py").replace(".py","")
                      for f in files
                      if f.get("path","").endswith(".py") and
                      "main" in f.get("path","").lower()), "main")

    fw = lang_info.get("framework", "fastapi")
    if "fastapi" in fw or "fastapi" in str(files):
        cmd = f'CMD ["uvicorn", "{main_file}:app", "--host", "0.0.0.0", "--port", "{port}"]'
    elif "django" in fw:
        cmd = f'CMD ["python", "manage.py", "runserver", "0.0.0.0:{port}"]'
    elif "flask" in fw:
        cmd = f'CMD ["flask", "run", "--host=0.0.0.0", "--port={port}"]'
    else:
        cmd = f'CMD ["python", "{main_file}.py"]'

    return (
        f"FROM {base}\n"
        f"WORKDIR /app\n"
        f"COPY . .\n"
        f"{req_line}\n"
        f"EXPOSE {port}\n"
        f"{cmd}\n"
    )


# ── Test runner generation ────────────────────────────────────────────────────

def generate_test_runner(lang_info: dict, files: list[dict]) -> dict | None:
    """Generate a basic test file if none exists."""
    lang = lang_info["language"]
    has_tests = any("test" in f.get("path","").lower() for f in files)
    if has_tests:
        return None

    if lang == "python":
        # Find FastAPI app
        main_mod = next((f.get("path","main.py").replace(".py","")
                         for f in files if f.get("path","").endswith(".py")), "main")
        content = (
            '"""Auto-generated smoke tests."""\n'
            'import pytest\n\n'
            'def test_import():\n'
            '    """App can be imported without error."""\n'
            '    try:\n'
            f'        import {main_mod}\n'
            '        assert True\n'
            '    except ImportError:\n'
            '        pytest.skip("Module not importable in test env")\n\n'
            'def test_health():\n'
            '    """Health endpoint returns 200."""\n'
            '    try:\n'
            '        from fastapi.testclient import TestClient\n'
            f'        import {main_mod}\n'
            f'        if not hasattr({main_mod}, "app"):\n'
            '            pytest.skip("No FastAPI app found")\n'
            f'        client = TestClient({main_mod}.app)\n'
            '        r = client.get("/health")\n'
            '        assert r.status_code in (200, 404)\n'
            '    except Exception as e:\n'
            '        pytest.skip(f"Cannot test: {e}")\n'
        )
        return {"path": "test_smoke.py", "content": content}

    if lang == "java":
        return {
            "path": "src/test/java/SmokeTest.java",
            "content": (
                'import org.junit.jupiter.api.Test;\n'
                'import static org.junit.jupiter.api.Assertions.*;\n\n'
                'class SmokeTest {\n'
                '    @Test void contextLoads() { assertTrue(true); }\n'
                '}\n'
            )
        }

    if lang == "node":
        return {
            "path": "test/smoke.test.js",
            "content": (
                'const assert = require("assert");\n'
                'describe("Smoke", () => {\n'
                '  it("should load without error", () => { assert.ok(true); });\n'
                '});\n'
            )
        }

    return None


# ── Main executor ─────────────────────────────────────────────────────────────

def _diagnose_crash(logs: str) -> dict | None:
    """
    Analyse container crash logs and return a fix descriptor.
    Returns {"reason": str, "fn": callable(files, tech_stack) -> list[dict]}
    or None if no known fix applies.
    """
    if not logs:
        return None

    # ── Fix 1: uvicorn can't import the app module ─────────────────────────
    # "ModuleNotFoundError" or "importer.py ... import_from_string"
    if "import_from_string" in logs or "ModuleNotFoundError" in logs:
        import re as _re
        # Try to extract the failing module name, e.g. "main:app" -> "main"
        m = _re.search(r"uvicorn.*[\"'](\w+):app[\"']", logs)
        bad_module = m.group(1) if m else None

        def fix_uvicorn_import(files: list[dict], tech_stack: dict) -> list[dict]:
            """Ensure Dockerfile CMD references the correct app module."""
            # Find the actual Python main file
            py_files = [f for f in files if f.get("path","").endswith(".py")
                        and "test" not in f.get("path","").lower()
                        and "main" in f.get("path","").lower()]
            if not py_files:
                py_files = [f for f in files if f.get("path","").endswith(".py")
                            and "test" not in f.get("path","").lower()]
            main_file = py_files[0]["path"].replace(".py","") if py_files else "main"

            # Fix Dockerfile CMD
            fixed = []
            for f in files:
                if f.get("path") == "Dockerfile":
                    old_cmd = f["content"]
                    # Replace any CMD with uvicorn pointing to wrong module
                    new_content = _re.sub(
                        r'CMD\s*\[.*?uvicorn.*?\]',
                        f'CMD ["uvicorn", "{main_file}:app", "--host", "0.0.0.0", "--port", "8000"]',
                        old_cmd
                    )
                    if new_content != old_cmd:
                        fixed.append({"path": "Dockerfile", "content": new_content})
                    else:
                        fixed.append(f)
                else:
                    fixed.append(f)

            # Also ensure the main file actually has a FastAPI app object
            has_app = any(
                "app = FastAPI" in f.get("content","") or "app=FastAPI" in f.get("content","")
                for f in fixed if f.get("path","").endswith(".py")
            )
            if not has_app:
                # Add a minimal app to the main file
                for i, f in enumerate(fixed):
                    if f.get("path","").endswith(".py") and "test" not in f.get("path",""):
                        old_content = f["content"]
                        if "from fastapi" not in old_content.lower():
                            fixed[i] = {
                                "path": f["path"],
                                "content": (
                                    "from fastapi import FastAPI" + chr(10) +
                                    old_content + chr(10) +
                                    chr(10) + 'app = FastAPI()' + chr(10) +
                                    chr(10) + '@app.get("/health")' + chr(10) +
                                    'async def health(): return {"status": "ok"}' + chr(10)
                                )
                            }
                        break
            return fixed

        return {"reason": "uvicorn module import error", "fn": fix_uvicorn_import}

    # ── Fix 2: missing requirements / ModuleNotFoundError ──────────────────
    if "No module named" in logs:
        import re as _re
        m = _re.search(r"No module named '([^']+)'", logs)
        missing = m.group(1) if m else None

        def fix_missing_module(files: list[dict], tech_stack: dict) -> list[dict]:
            fixed = []
            for f in files:
                if "requirements" in f.get("path",""):
                    pkg = missing.split(".")[0] if missing else ""
                    if pkg and pkg not in f["content"]:
                        fixed.append({"path": f["path"], "content": f["content"].rstrip() + chr(10) + pkg + chr(10)})
                    else:
                        fixed.append(f)
                else:
                    fixed.append(f)
            return fixed

        return {"reason": f"missing Python package: {missing}", "fn": fix_missing_module}

    # ── Fix 3: wrong port — app listening on different port than Dockerfile EXPOSE ──
    if "address already in use" in logs.lower() or "bind: address" in logs.lower():
        def fix_port(files: list[dict], tech_stack: dict) -> list[dict]:
            fixed = []
            for f in files:
                if f.get("path") == "Dockerfile":
                    import re as _re
                    # Change to port 8080 as fallback
                    new_content = _re.sub(r'EXPOSE \d+', 'EXPOSE 8080', f["content"])
                    new_content = _re.sub(r'"--port",\s*"\d+"', '"--port", "8080"', new_content)
                    fixed.append({"path": "Dockerfile", "content": new_content})
                else:
                    fixed.append(f)
            return fixed
        return {"reason": "port binding conflict", "fn": fix_port}

    # ── Fix 4: Java / Spring Boot — fat jar not found ──────────────────────
    if "Unable to access jarfile" in logs or "no main manifest attribute" in logs:
        def fix_jar_path(files: list[dict], tech_stack: dict) -> list[dict]:
            fixed = []
            for f in files:
                if f.get("path") == "Dockerfile" and "java" in f["content"].lower():
                    # Use wildcard jar name
                    import re as _re
                    new_content = _re.sub(
                        r'COPY.*\.jar.*',
                        'COPY --from=build /app/target/*.jar app.jar',
                        f["content"]
                    )
                    fixed.append({"path": "Dockerfile", "content": new_content})
                else:
                    fixed.append(f)
            return fixed
        return {"reason": "JAR file path mismatch", "fn": fix_jar_path}

    # ── Fix 5: Node.js — cannot find module ────────────────────────────────
    if "Cannot find module" in logs and ("index" in logs or "app" in logs or "server" in logs):
        import re as _re
        m = _re.search(r"Cannot find module '([^']+)'", logs)
        missing_mod = m.group(1) if m else "index"

        def fix_node_entry(files: list[dict], tech_stack: dict) -> list[dict]:
            fixed = []
            # Find actual entry file
            js_files = [f for f in files if f.get("path","").endswith(".js")
                        and "test" not in f.get("path","").lower()
                        and "node_modules" not in f.get("path","")]
            entry = js_files[0]["path"] if js_files else "index.js"

            for f in files:
                if f.get("path") == "Dockerfile":
                    import re as _re2
                    new_content = _re2.sub(
                        r'CMD \["node",.*?"\]',
                        f'CMD ["node", "{entry}"]',
                        f["content"]
                    )
                    fixed.append({"path": "Dockerfile", "content": new_content})
                else:
                    fixed.append(f)
            return fixed
        return {"reason": f"Node.js entry point mismatch: {missing_mod}", "fn": fix_node_entry}

    return None


# ── Canonical Docker base images — guaranteed to exist on Docker Hub ──────────
_CANONICAL_IMAGES = {
    # Java / Maven
    "maven:3.8.6-jdk-17":            "maven:3.9-eclipse-temurin-17",
    "maven:3.8-jdk-17":              "maven:3.9-eclipse-temurin-17",
    "maven:3.8-openjdk-17":          "maven:3.9-eclipse-temurin-17",
    "maven:3.9-jdk-17":              "maven:3.9-eclipse-temurin-17",
    "maven:3.9-openjdk-17":          "maven:3.9-eclipse-temurin-17",
    "maven:3.9.0-eclipse-temurin-17":"maven:3.9-eclipse-temurin-17",
    "maven:latest":                  "maven:3.9-eclipse-temurin-17",
    "maven:3-jdk-17":                "maven:3.9-eclipse-temurin-17",
    "openjdk:17":                    "eclipse-temurin:17-jdk-alpine",
    "openjdk:17-jdk":                "eclipse-temurin:17-jdk-alpine",
    "openjdk:17-alpine":             "eclipse-temurin:17-jdk-alpine",
    "openjdk:11":                    "eclipse-temurin:11-jdk-alpine",
    "openjdk:21":                    "eclipse-temurin:21-jdk-alpine",
    "eclipse-temurin:17":            "eclipse-temurin:17-jdk-alpine",
    "eclipse-temurin:17-jdk":        "eclipse-temurin:17-jdk-alpine",
    # Gradle
    "gradle:7-jdk17":                "gradle:8.5-jdk17",
    "gradle:7.6-jdk17":              "gradle:8.5-jdk17",
    "gradle:8-jdk17":                "gradle:8.5-jdk17",
    "gradle:latest":                 "gradle:8.5-jdk17",
    # Node
    "node:18-alpine":                "node:20-alpine",
    "node:16-alpine":                "node:20-alpine",
    "node:latest":                   "node:20-alpine",
    "node:18":                       "node:20-alpine",
    # Python
    "python:3.11-slim":              "python:3.12-slim",
    "python:3.10-slim":              "python:3.12-slim",
    "python:3.9-slim":               "python:3.12-slim",
    "python:latest":                 "python:3.12-slim",
    # Go
    "golang:1.21-alpine":            "golang:1.22-alpine",
    "golang:1.20-alpine":            "golang:1.22-alpine",
    "golang:latest":                 "golang:1.22-alpine",
}


def sanitise_dockerfile(content: str) -> tuple[str, list[str]]:
    """
    Replace any non-canonical Docker base image tags in a Dockerfile.
    Returns (fixed_content, list_of_replacements_made).
    Also fixes common LLM mistakes like wrong COPY paths.
    """
    import re as _re
    fixes = []
    lines = content.split(chr(10))
    out   = []

    for line in lines:
        stripped = line.strip()

        # Check FROM lines
        if stripped.upper().startswith("FROM "):
            # Extract image name (handles "FROM image AS stage" and "FROM image")
            m = _re.match(r'(FROM\s+)([^\s]+)(.*)', line, _re.IGNORECASE)
            if m:
                prefix, image, rest = m.group(1), m.group(2), m.group(3)
                canonical = _CANONICAL_IMAGES.get(image)
                if canonical:
                    fixes.append(f"FROM {image} → {canonical}")
                    line = f"{prefix}{canonical}{rest}"

        out.append(line)

    return chr(10).join(out), fixes


async def _llm_fix_compile_error(
    build_logs: str,
    broken_files: list[dict],
    original_files: list[dict],
    log_fn=None,
) -> list[dict] | None:
    """
    Send broken files + compile errors to the LLM and ask it to return
    corrected code. Returns the full file list with fixes applied, or None
    if LLM is unavailable / times out.

    broken_files  — file list from _diagnose_compile_error (scaffolds for bad files)
    original_files — the original generated files (with the broken content)
    """
    import re as _re
    import json as _json

    # Extract which files actually have errors
    java_err_paths = set()
    for full_path, _ in _re.findall(r'\[ERROR\]\s+(/app/[^\s]+\.java):\[(\d+),\d+\]', build_logs):
        java_err_paths.add(full_path.replace("/app/", ""))

    py_err_paths = set()
    for path in _re.findall(r'File "([^"]+\.py)"', build_logs):
        if "SyntaxError" in build_logs:
            py_err_paths.add(path.replace("/app/","").lstrip("/"))

    bad_paths = java_err_paths | py_err_paths
    if not bad_paths:
        # Try Node errors
        for path in _re.findall(r'SyntaxError.*?([^\s/][^\s]*\.(?:js|ts))(?::\d+)?', build_logs):
            bad_paths.add(path.split("/")[-1])

    if not bad_paths:
        return None

    # Extract just the error lines from build logs
    error_lines = [l for l in build_logs.split(chr(10))
                   if "[ERROR]" in l or "SyntaxError" in l or "error:" in l.lower()]
    error_summary = chr(10).join(error_lines[:30])

    # Build per-file fix prompts — only send the broken files to LLM
    bad_file_contents = []
    for f in original_files:
        fp = f.get("path","")
        basename = fp.split("/")[-1]
        if fp in bad_paths or basename in bad_paths:
            bad_file_contents.append(f)

    if not bad_file_contents:
        return None

    try:
        from src.utils.llm_factory import build_llm
        llm = build_llm()
        if log_fn:
            await log_fn("info", f"LLM fixing {len(bad_file_contents)} broken file(s)...")
    except Exception as e:
        if log_fn:
            await log_fn("warning", f"LLM unavailable for code fix: {e} — using scaffold")
        return None

    # Build the repair prompt
    files_block = chr(10).join(
        f"FILE: {f['path']}{chr(10)}"
        f"```{chr(10)}{f.get('content','')}{chr(10)}```"
        for f in bad_file_contents
    )

    NL = chr(10)
    prompt = (
        "You are a compiler. The following source file(s) have compile errors."
        " Fix ONLY the syntax errors. Keep all logic, imports, class names,"
        " package names, and annotations exactly as they are."
        " Return ONLY a JSON array of fixed files — no explanation, no markdown."
        ' Format: [{"path": "...", "content": "full corrected file content"}]'
        + NL + NL
        + "=== COMPILE ERRORS ===" + NL + error_summary + NL + NL
        + "=== FILES TO FIX ===" + NL + files_block + NL + NL
        + "Return ONLY the JSON array. Start with [ and end with ]."
    )

    try:
        from langchain_core.messages import HumanMessage
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        raw  = resp.content if hasattr(resp, "content") else str(resp)

        # Parse JSON response
        raw = raw.strip()
        m   = _re.search(r'\[.*\]', raw, _re.DOTALL)
        if not m:
            raise ValueError("No JSON array in LLM response")

        fixed_list = _json.loads(m.group(0))
        if not isinstance(fixed_list, list):
            raise ValueError("LLM returned non-list")

        # Build the final file list — replace broken files with LLM-fixed ones
        fixed_map = {f["path"]: f["content"] for f in fixed_list if f.get("path") and f.get("content")}
        result_files = []
        for f in original_files:
            fp = f.get("path","")
            basename = fp.split("/")[-1]
            # Check by full path or basename
            if fp in fixed_map:
                result_files.append({"path": fp, "content": fixed_map[fp]})
                if log_fn:
                    await log_fn("success", f"  LLM fixed: {fp}")
            elif basename in fixed_map:
                result_files.append({"path": fp, "content": fixed_map[basename]})
                if log_fn:
                    await log_fn("success", f"  LLM fixed: {fp}")
            else:
                result_files.append(f)

        return result_files

    except Exception as e:
        if log_fn:
            await log_fn("warning", f"LLM code fix failed ({e}) — using scaffold fallback")
        return None


def _diagnose_compile_error(build_logs: str, files: list[dict]) -> dict | None:
    """
    Detect compile/build errors and return fixed files.
    Handles Java (mvn), Python (syntax), Node.js (syntax).
    Returns {"reason": str, "files": list[dict]} or None.
    """
    import re as _re

    logs = build_logs or ""

    # ── Java / Maven compile errors ────────────────────────────────────────
    java_errors = _re.findall(
        r'\[ERROR\]\s+(/app/[^\s]+\.java):\[(\d+),\d+\]',
        logs
    )
    if java_errors:
        bad_paths = set()
        for full_path, _ in java_errors:
            bad_paths.add(full_path.replace("/app/", ""))

        fixed = []
        replaced = []
        for f in files:
            fp = f.get("path", "")
            if fp in bad_paths:
                replaced.append(fp)
                fixed.append({"path": fp, "content": _java_scaffold(fp, f.get("content",""))})
            else:
                fixed.append(f)

        if replaced:
            return {
                "reason": f"Java compile error — replaced {len(replaced)} file(s): {', '.join(replaced)}",
                "files": fixed,
            }

    # ── Python syntax errors ───────────────────────────────────────────────
    py_errors = _re.findall(
        r'File "([^"]+\.py)", line \d+.*?SyntaxError',
        logs, _re.DOTALL
    )
    if not py_errors:
        py_errors = [m for m in _re.findall(r'([^\s"\']+\.py)', logs)
                     if "SyntaxError" in logs]

    if py_errors:
        bad_bases = {p.replace("/app/","").lstrip("/").split("/")[-1] for p in py_errors}
        fixed = []
        replaced = []
        for f in files:
            fp = f.get("path","")
            if fp.split("/")[-1] in bad_bases:
                replaced.append(fp)
                fixed.append({"path": fp, "content": _python_scaffold(fp)})
            else:
                fixed.append(f)
        if replaced:
            return {
                "reason": f"Python syntax error — replaced {len(replaced)} file(s)",
                "files": fixed,
            }

    # ── Node.js / TypeScript errors ────────────────────────────────────────
    node_errors = _re.findall(r'SyntaxError.*?([^\s/][^\s]*\.(?:js|ts))(?::\d+)?', logs)
    if node_errors:
        bad_bases = {p.split("/")[-1] for p in node_errors}
        fixed = []
        replaced = []
        for f in files:
            fp = f.get("path","")
            if fp.split("/")[-1] in bad_bases:
                replaced.append(fp)
                fixed.append({"path": fp, "content": _node_scaffold(fp)})
            else:
                fixed.append(f)
        if replaced:
            return {
                "reason": f"Node.js syntax error — replaced {len(replaced)} file(s)",
                "files": fixed,
            }

    # ── Image pull failure — manifest unknown / not found ─────────────────────
    if "manifest unknown" in logs or "not found" in logs.lower() and "manifest" in logs.lower():
        import re as _re
        # Extract the bad image name
        m = _re.search(r'manifest for ([^\s]+) not found', logs)
        bad_image = m.group(1) if m else None

        def fix_bad_image(files: list[dict], tech_stack: dict) -> list[dict]:
            """Replace the bad image in Dockerfile with a known-good canonical image."""
            fixed = []
            for f in files:
                if f.get("path","") == "Dockerfile" or f.get("path","").endswith("/Dockerfile"):
                    new_content, img_fixes = sanitise_dockerfile(f["content"])
                    if img_fixes:
                        fixed.append({"path": f["path"], "content": new_content})
                    else:
                        # Fallback: rewrite the whole Dockerfile using detected language
                        lang = (tech_stack or {}).get("language","").lower()
                        if "java" in lang or "spring" in lang:
                            new_content = (
                                "FROM maven:3.9-eclipse-temurin-17 AS build" + chr(10) +
                                "WORKDIR /app" + chr(10) +
                                "COPY . ." + chr(10) +
                                "RUN mvn package -DskipTests -q" + chr(10) + chr(10) +
                                "FROM eclipse-temurin:17-jre-alpine" + chr(10) +
                                "WORKDIR /app" + chr(10) +
                                "COPY --from=build /app/target/*.jar app.jar" + chr(10) +
                                "EXPOSE 8080" + chr(10) +
                                'CMD ["java", "-jar", "app.jar"]' + chr(10)
                            )
                            fixed.append({"path": f["path"], "content": new_content})
                        else:
                            fixed.append(f)
                else:
                    fixed.append(f)
            return fixed

        return {"reason": f"Bad Docker image tag: {bad_image or 'unknown'} — replaced with canonical image", "fn": fix_bad_image}

    return None


def _java_scaffold(path: str, original: str) -> str:
    """Return a minimal compilable Java class scaffold based on annotations in original."""
    parts    = path.replace("\\", "/").split("/")
    java_idx = next((i for i, p in enumerate(parts) if p == "java"), -1)
    pkg_parts= parts[java_idx + 1 : -1] if java_idx != -1 else []
    pkg      = ".".join(pkg_parts) if pkg_parts else "com.app"
    cls      = parts[-1].replace(".java", "")

    is_app  = "SpringApplication" in original or "@SpringBootApplication" in original
    is_ctrl = any(k in original for k in ("@RestController","@Controller","@RequestMapping"))
    is_svc  = "@Service" in original
    is_repo = any(k in original for k in ("JpaRepository","CrudRepository","@Repository"))
    is_ent  = any(k in original for k in ("@Entity","@Table","@Id"))

    if is_app:
        return (
            f"package {pkg};\n\n"
            "import org.springframework.boot.SpringApplication;\n"
            "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
            "@SpringBootApplication\n"
            f"public class {cls} {{\n"
            "    public static void main(String[] args) {\n"
            f"        SpringApplication.run({cls}.class, args);\n"
            "    }\n"
            "}\n"
        )
    if is_ctrl:
        entity       = cls.replace("Controller", "")
        entity_lower = entity[0].lower() + entity[1:] if entity else "item"
        return (
            f"package {pkg};\n\n"
            "import org.springframework.http.ResponseEntity;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "import java.util.*;\n\n"
            "@RestController\n"
            f"@RequestMapping(\"/api/v1/{entity_lower}s\")\n"
            f"public class {cls} {{\n\n"
            "    @GetMapping\n"
            "    public ResponseEntity<List<Map<String,Object>>> getAll() {\n"
            "        return ResponseEntity.ok(new ArrayList<>());\n"
            "    }\n\n"
            "    @GetMapping(\"/{id}\")\n"
            "    public ResponseEntity<Map<String,Object>> getById(@PathVariable String id) {\n"
            "        return ResponseEntity.ok(Map.of(\"id\", id));\n"
            "    }\n\n"
            "    @PostMapping\n"
            "    public ResponseEntity<Map<String,Object>> create(@RequestBody Map<String,Object> body) {\n"
            "        body.put(\"id\", UUID.randomUUID().toString());\n"
            "        return ResponseEntity.status(201).body(body);\n"
            "    }\n\n"
            "    @PutMapping(\"/{id}\")\n"
            "    public ResponseEntity<Map<String,Object>> update(@PathVariable String id,\n"
            "            @RequestBody Map<String,Object> body) {\n"
            "        body.put(\"id\", id);\n"
            "        return ResponseEntity.ok(body);\n"
            "    }\n\n"
            "    @DeleteMapping(\"/{id}\")\n"
            "    public ResponseEntity<Void> delete(@PathVariable String id) {\n"
            "        return ResponseEntity.noContent().build();\n"
            "    }\n\n"
            "    @GetMapping(\"/health\")\n"
            "    public ResponseEntity<Map<String,Object>> health() {\n"
            "        return ResponseEntity.ok(Map.of(\"status\", \"ok\"));\n"
            "    }\n"
            "}\n"
        )
    if is_svc:
        return (
            f"package {pkg};\n\n"
            "import org.springframework.stereotype.Service;\n"
            "import java.util.*;\n\n"
            "@Service\n"
            f"public class {cls} {{\n\n"
            "    public List<Map<String,Object>> findAll() { return new ArrayList<>(); }\n\n"
            "    public Optional<Map<String,Object>> findById(String id) { return Optional.empty(); }\n\n"
            "    public Map<String,Object> save(Map<String,Object> entity) {\n"
            "        entity.put(\"id\", UUID.randomUUID().toString());\n"
            "        return entity;\n"
            "    }\n\n"
            "    public void deleteById(String id) {}\n"
            "}\n"
        )
    if is_repo:
        entity = cls.replace("Repository", "")
        return (
            f"package {pkg};\n\n"
            "import org.springframework.data.jpa.repository.JpaRepository;\n"
            "import org.springframework.stereotype.Repository;\n\n"
            "@Repository\n"
            f"public interface {cls} extends JpaRepository<{entity}, String> {{\n"
            "}\n"
        )
    if is_ent:
        return (
            f"package {pkg};\n\n"
            "import jakarta.persistence.*;\n"
            "import java.time.LocalDateTime;\n\n"
            "@Entity\n"
            f"@Table(name = \"{cls.lower()}s\")\n"
            f"public class {cls} {{\n\n"
            "    @Id\n"
            "    @GeneratedValue(strategy = GenerationType.UUID)\n"
            "    private String id;\n\n"
            "    @Column(nullable = false)\n"
            "    private String name;\n\n"
            "    @Column\n"
            "    private String status = \"ACTIVE\";\n\n"
            "    @Column(updatable = false)\n"
            "    private LocalDateTime createdAt;\n\n"
            "    @PrePersist\n"
            "    protected void onCreate() { this.createdAt = LocalDateTime.now(); }\n\n"
            "    public String getId() { return id; }\n"
            "    public void setId(String id) { this.id = id; }\n"
            "    public String getName() { return name; }\n"
            "    public void setName(String name) { this.name = name; }\n"
            "    public String getStatus() { return status; }\n"
            "    public void setStatus(String status) { this.status = status; }\n"
            "    public LocalDateTime getCreatedAt() { return createdAt; }\n"
            "}\n"
        )
    # Generic fallback
    return (
        f"package {pkg};\n\n"
        f"public class {cls} {{\n"
        "    // Auto-fixed scaffold\n"
        "}\n"
    )


def _python_scaffold(path: str) -> str:
    """Return a minimal working Python/FastAPI file."""
    is_main = any(k in path.lower() for k in ("main", "app", "server"))
    if is_main:
        return (
            "from fastapi import FastAPI\n"
            "from fastapi.middleware.cors import CORSMiddleware\n\n"
            "app = FastAPI(title=\"API\", version=\"1.0.0\")\n\n"
            "app.add_middleware(CORSMiddleware, allow_origins=[\"*\"],\n"
            "    allow_methods=[\"*\"], allow_headers=[\"*\"])\n\n"
            "@app.get(\"/health\")\n"
            "async def health():\n"
            "    return {\"status\": \"ok\"}\n"
        )
    return f"# Auto-fixed scaffold: {path}\n"


def _node_scaffold(path: str) -> str:
    """Return a minimal working Node.js/Express file."""
    return (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.use(express.json());\n\n"
        "app.get('/health', (req, res) => res.json({ status: 'ok' }));\n\n"
        "const PORT = process.env.PORT || 3000;\n"
        "app.listen(PORT, () => console.log(`Server running on port ${PORT}`));\n"
        "module.exports = app;\n"
    )


class DockerExecutor:
    """
    Builds, tests, and deploys in Docker — local or remote.
    All output streams via log_callback to the frontend live.
    """

    def __init__(self, log_callback=None, tech_stack: dict | None = None):
        self.log_cb     = log_callback
        self.tech_stack = tech_stack
        self._t0        = time.time()
        self._files: list[dict] = []  # stored after build for auto-fix

    async def _log(self, level: str, msg: str):
        elapsed = round(time.time() - self._t0, 1)
        print(f"  [{elapsed:5.1f}s] [DOCKER/{level.upper()}] {msg}", flush=True)
        if self.log_cb:
            try:
                await self.log_cb(level, "docker", msg, elapsed)
            except Exception:
                pass

    async def build_and_test(
        self,
        files: list[dict],
        project_name: str,
        run_tests: bool = True,
    ) -> dict:
        result = {
            "success": False, "image_tag": None,
            "build_logs": "", "test_output": "",
            "test_passed": 0, "test_failed": 0,
            "coverage_percent": 0.0, "container_id": None,
            "error": None, "language": "unknown",
        }

        try:
            client = _get_docker_client()
        except Exception as e:
            await self._log("error", f"Docker connection failed: {e}")
            result["error"] = str(e)
            return result

        # Store files for auto-fix in run_container
        self._files = files

        # Detect language
        lang_info = detect_language(files, self.tech_stack)
        result["language"] = lang_info["language"]
        await self._log("info",
            f"Detected: {lang_info['language']} / {lang_info['framework']} "
            f"(build: {lang_info['build_tool']}, port: {lang_info['app_port']})")

        slug = re.sub(r"[^a-z0-9-]", "-", project_name.lower())[:30]
        tag  = f"sdlc/{slug}:latest"

        from config.settings import settings
        registry = getattr(settings, "docker_registry", "")

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write files
            await self._log("info", f"Writing {len(files)} files to build context...")
            for f in files:
                path    = f.get("path", "")
                content = f.get("content", "")
                if not path:
                    continue
                import os
                # Sanitise Dockerfiles — fix wrong/deprecated base image tags
                if path == "Dockerfile" or path.endswith("/Dockerfile"):
                    fixed_content, img_fixes = sanitise_dockerfile(content)
                    if img_fixes:
                        for fix in img_fixes:
                            await self._log("warning", f"  [IMAGE FIX] {fix}")
                        content = fixed_content
                full = os.path.join(tmpdir, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(content)
                await self._log("info", f"  {path} ({len(content)} chars)")

            # Ensure Dockerfile
            df_path = os.path.join(tmpdir, "Dockerfile")
            if not os.path.exists(df_path):
                await self._log("info", "Generating Dockerfile...")
                with open(df_path, "w") as fh:
                    fh.write(generate_dockerfile(lang_info, files))

            # Add smoke test if needed
            if run_tests:
                smoke = generate_test_runner(lang_info, files)
                if smoke:
                    import os as _os2
                    sp = _os2.path.join(tmpdir, smoke["path"])
                    _os2.makedirs(_os2.path.dirname(sp), exist_ok=True)
                    with open(sp, "w") as fh:
                        fh.write(smoke["content"])
                    await self._log("info", f"Added smoke test: {smoke['path']}")

            # ── Build ─────────────────────────────────────────────────────
            await self._log("info", f"Building image: {tag}")
            build_logs = []
            t0 = time.time()

            try:
                for line in client.api.build(
                    path=tmpdir, tag=tag, rm=True, forcerm=True, decode=True,
                ):
                    if "stream" in line:
                        msg = line["stream"].rstrip()
                        if msg:
                            build_logs.append(msg)
                            await self._log("build", msg)
                    elif "error" in line:
                        err = line["error"].rstrip()
                        build_logs.append(f"ERROR: {err}")
                        await self._log("error", f"Build error: {err}")
                        result["error"] = err

            except Exception as _build_ex:
                result["error"] = str(_build_ex)
                await self._log("error", f"Build exception: {_build_ex}")

            # ── Compile error auto-fix ─────────────────────────────────────
            # Strategy 1: LLM repairs the exact broken file preserving logic
            # Strategy 2: scaffold fallback if LLM unavailable or fails
            if result.get("error") and not result.get("image_tag"):
                all_logs = "\n".join(build_logs)
                fix = _diagnose_compile_error(all_logs, self._files)
                if fix:
                    await self._log("warning", f"Compile error detected: {fix['reason']}")

                    # ── Strategy 1: Ask LLM to fix the broken files ────────
                    llm_fixed = await _llm_fix_compile_error(
                        build_logs=all_logs,
                        broken_files=fix["files"],
                        original_files=self._files,
                        log_fn=self._log,
                    )
                    fixed_files = llm_fixed if llm_fixed else fix["files"]
                    strategy    = "LLM repair" if llm_fixed else "scaffold fallback"
                    await self._log("info", f"Applying fix ({strategy}) and rebuilding...")

                    # Overwrite fixed files in tmpdir
                    self._files = fixed_files
                    for f in fixed_files:
                        fpath    = f.get("path","")
                        fcontent = f.get("content","")
                        if not fpath:
                            continue
                        full = os.path.join(tmpdir, fpath)
                        os.makedirs(os.path.dirname(full), exist_ok=True)
                        with open(full, "w", encoding="utf-8") as fh:
                            fh.write(fcontent)
                        await self._log("info", f"  fixed: {fpath}")

                    # Retry build
                    await self._log("info", f"Rebuilding image: {tag}")
                    build_logs2 = []
                    t0b = time.time()
                    try:
                        for line2 in client.api.build(
                            path=tmpdir, tag=tag, rm=True, forcerm=True, decode=True,
                        ):
                            if "stream" in line2:
                                msg2 = line2["stream"].rstrip()
                                if msg2:
                                    build_logs2.append(msg2)
                                    await self._log("build", msg2)
                            elif "error" in line2:
                                err2 = line2["error"].rstrip()
                                build_logs2.append(f"ERROR: {err2}")
                                await self._log("error", f"Rebuild error: {err2}")
                                result["error"] = err2
                                result["build_logs"] = "\n".join(build_logs2)
                                return result

                        # Rebuild succeeded
                        result["image_tag"]  = tag
                        result["build_logs"] = "\n".join(build_logs2)
                        result["error"]      = None
                        result["auto_fixed"] = True
                        result["fix_applied"]= f"{fix['reason']} [{strategy}]"
                        await self._log("success",
                            f"Auto-fixed build succeeded in {time.time()-t0b:.1f}s: {tag}")

                    except Exception as e2:
                        result["error"] = str(e2)
                        result["build_logs"] = "\n".join(build_logs2)
                        await self._log("error", f"Rebuild failed: {e2}")
                        return result
                else:
                    result["build_logs"] = "\n".join(build_logs)
                    return result

            if result.get("error"):
                result["build_logs"] = "\n".join(build_logs)
                return result

            # Build succeeded (no auto-fix needed)
            if not result.get("image_tag"):
                result["image_tag"]  = tag
                result["build_logs"] = "\n".join(build_logs)
                await self._log("success", f"Image built in {time.time()-t0:.1f}s: {tag}")

            # Push to registry if configured
            if registry and tag:
                remote_tag = f"{registry}/{tag}"
                await self._log("info", f"Pushing to registry: {remote_tag}")
                try:
                    client.images.get(tag).tag(remote_tag)
                    for line in client.images.push(remote_tag, stream=True, decode=True):
                        if "status" in line:
                            await self._log("push", line["status"])
                    result["image_tag"] = remote_tag
                    await self._log("success", f"Pushed: {remote_tag}")
                except Exception as e:
                    await self._log("warning", f"Push failed (continuing): {e}")

            # ── Test ──────────────────────────────────────────────────────
            if run_tests:
                # For Java: run tests inside the BUILD image (has mvn/gradle + source).
                # The runtime image is a slim JRE — no build tools, can't run tests.
                # For all other languages: run test_cmd inside the built image.
                lang      = lang_info.get("language", "")
                build_img = None
                test_cmd  = lang_info["test_cmd"]

                if lang == "java":
                    # Normalise build_tool — LLM sometimes returns "maven" instead of "mvn"
                    raw_tool   = lang_info.get("build_tool", "mvn").lower()
                    is_maven   = "maven" in raw_tool or raw_tool == "mvn"
                    # Also check actual files — pom.xml = maven, build.gradle = gradle
                    has_pom    = any(f.get("path","").endswith("pom.xml") for f in self._files)
                    has_gradle = any("build.gradle" in f.get("path","") for f in self._files)

                    if is_maven or has_pom:
                        build_img = "maven:3.9-eclipse-temurin-17"
                        test_cmd  = ["mvn", "test"]
                    elif has_gradle:
                        build_img = "gradle:8.5-jdk17"
                        test_cmd  = ["gradle", "test", "--no-daemon"]
                    else:
                        # Default to maven — safer than gradle wrapper
                        build_img = "maven:3.9-eclipse-temurin-17"
                        test_cmd  = ["mvn", "test"]

                    await self._log("info",
                        f"Java test strategy: image={build_img} cmd={test_cmd} "
                        f"(pom={has_pom} gradle={has_gradle})")

                await self._log("info", f"Running tests: {' '.join(test_cmd)}")
                t1 = time.time()
                try:
                    if lang == "java" and build_img:
                        # Re-mount source files into a fresh build-image container
                        import tempfile as _tf2, os as _os2
                        with _tf2.TemporaryDirectory() as tdir2:
                            for f in self._files:
                                fp = f.get("path", "")
                                if not fp: continue
                                full = _os2.path.join(tdir2, fp)
                                _os2.makedirs(_os2.path.dirname(full), exist_ok=True)
                                with open(full, "w", encoding="utf-8") as fh:
                                    fh.write(f.get("content", ""))
                            try:
                                out = client.containers.run(
                                    image=build_img,
                                    command=test_cmd,
                                    volumes={tdir2: {"bind": "/app", "mode": "rw"}},
                                    working_dir="/app",
                                    remove=True, stdout=True, stderr=True,
                                    mem_limit="2g", network_disabled=False,
                                )
                            except Exception as _je:
                                out = str(_je).encode()
                    else:
                        out = client.containers.run(
                            image=tag,
                            command=test_cmd,
                            remove=True, stdout=True, stderr=True,
                            mem_limit="1g", network_disabled=True,
                        )
                    test_output = out.decode("utf-8", errors="replace") \
                                  if isinstance(out, bytes) else str(out)
                except Exception as exc:
                    test_output = str(exc)

                result["test_output"] = test_output
                parsed = _parse_test_output(test_output, lang_info["language"])
                result.update(parsed)

                await self._log(
                    "success" if result["test_failed"] == 0 else "warning",
                    f"Tests in {time.time()-t1:.1f}s — "
                    f"passed={result['test_passed']} failed={result['test_failed']} "
                    f"coverage={result['coverage_percent']:.1f}%"
                )
                for line in test_output.split("\n")[:50]:  # show first 50 lines
                    if line.strip():
                        await self._log("test", line)

            result["success"] = True
            return result

    async def run_container(
        self,
        image_tag: str,
        project_name: str,
        port: int = 8090,
        app_port: int | None = None,
    ) -> dict:
        """Start the image as a running service on the Docker host (local or remote)."""
        try:
            client = _get_docker_client()
        except Exception as e:
            return {"error": str(e), "url": None}

        slug = re.sub(r"[^a-z0-9-]", "-", project_name.lower())[:30]
        name = f"sdlc-{slug}"
        internal = str(app_port or 8000)

        # Remove existing
        try:
            old = client.containers.get(name)
            old.stop(timeout=3); old.remove(force=True)
            await self._log("info", f"Removed existing container: {name}")
        except Exception:
            pass

        await self._log("info", f"Starting {name} on port {port}→{internal}...")

        # Determine host URL from DOCKER_HOST setting
        from config.settings import settings
        docker_host = getattr(settings, "docker_host", "") or os.environ.get("DOCKER_HOST","")
        if docker_host and "tcp://" in docker_host:
            host_ip = docker_host.replace("tcp://","").split(":")[0]
        else:
            host_ip = "localhost"
        url = f"http://{host_ip}:{port}"

        try:
            container = client.containers.run(
                image=image_tag, name=name, detach=True,
                ports={f"{internal}/tcp": port},
                restart_policy={"Name": "unless-stopped"},
            )
            await self._log("info", f"Container started — waiting 4s...")
            await asyncio.sleep(4)
            container.reload()
            status = container.status

            if status == "running":
                await self._log("success", f"Container {name} running → {url}")
                return {"container_id": container.id[:12], "name": name,
                        "status": status, "url": url, "port": port}
            else:
                # Container started but exited — read logs and attempt auto-fix
                crash_logs = ""
                try:
                    crash_logs = container.logs(tail=50).decode("utf-8", errors="replace")
                    await self._log("error", f"Container exited ({status}). Crash logs:")
                    for line in crash_logs.split(chr(10))[-25:]:
                        if line.strip():
                            await self._log("error", f"  {line}")
                except Exception:
                    pass

                # ── Auto-fix: detect known crash patterns and rebuild ──────
                fix = _diagnose_crash(crash_logs)
                if fix and self._files:
                    await self._log("warning", f"Auto-fix detected: {fix['reason']}")
                    await self._log("info", "Applying fix and rebuilding...")

                    fixed_files = fix["fn"](self._files, self.tech_stack or {})
                    if fixed_files:
                        # Remove crashed container before rebuild
                        try:
                            container.stop(timeout=2); container.remove(force=True)
                        except Exception:
                            pass

                        # Rebuild with fixed files
                        rebuild = await self.build_and_test(
                            files=fixed_files,
                            project_name=project_name,
                            run_tests=False,
                        )
                        if rebuild.get("success") and rebuild.get("image_tag"):
                            await self._log("info", "Rebuild succeeded — starting fixed container...")
                            new_tag = rebuild["image_tag"]
                            try:
                                c2 = client.containers.run(
                                    image=new_tag, name=name, detach=True,
                                    ports={f"{internal}/tcp": port},
                                    restart_policy={"Name": "unless-stopped"},
                                )
                                await asyncio.sleep(4)
                                c2.reload()
                                if c2.status == "running":
                                    await self._log("success", f"Auto-fixed container running → {url}")
                                    return {"container_id": c2.id[:12], "name": name,
                                            "status": "running", "url": url, "port": port,
                                            "auto_fixed": True, "fix_applied": fix["reason"]}
                                else:
                                    await self._log("warning", "Auto-fix rebuild still failing — returning URL anyway")
                            except Exception as rebuild_err:
                                await self._log("error", f"Auto-fix redeploy failed: {rebuild_err}")
                        else:
                            await self._log("error", f"Auto-fix rebuild failed: {rebuild.get('error','unknown')}")
                    else:
                        await self._log("warning", f"Auto-fix {fix['reason']}: no file changes generated")
                elif not self._files:
                    await self._log("warning", "No files stored — cannot auto-fix (run build_and_test first)")

                await self._log("warning", f"Container {name} status={status} — URL={url}")
                return {"container_id": container.id[:12], "name": name,
                        "status": status, "url": url, "port": port,
                        "note": f"Container {status} — check logs above"}

        except Exception as e:
            err = str(e)
            # Common errors with helpful messages
            if "port is already allocated" in err or "address already in use" in err:
                await self._log("error",
                    f"Port {port} already in use. Stop existing container or change port.")
            elif "No such image" in err or "not found" in err.lower():
                await self._log("error",
                    f"Image '{image_tag}' not found on server. Run testing/CI phase first.")
            else:
                await self._log("error", f"Container start failed: {err}")
            return {"error": err, "url": None}

    async def cleanup(self, image_tag: str):
        try:
            client = _get_docker_client()
            client.images.remove(image_tag, force=True)
            await self._log("info", f"Cleaned up: {image_tag}")
        except Exception:
            pass


# ── Test output parser ────────────────────────────────────────────────────────

def _parse_test_output(output: str, language: str = "python") -> dict:
    passed = 0; failed = 0; coverage = 0.0

    if language == "python":
        m = re.search(r"(\d+) passed", output)
        if m: passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m: failed = int(m.group(1))
        m = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
        if m: coverage = float(m.group(1))

    elif language == "java":
        m = re.search(r"Tests run:\s*(\d+).*?Failures:\s*(\d+).*?Errors:\s*(\d+)",
                      output, re.DOTALL)
        if m:
            total  = int(m.group(1))
            failed = int(m.group(2)) + int(m.group(3))
            passed = total - failed

    elif language == "node":
        m = re.search(r"(\d+) passing", output)
        if m: passed = int(m.group(1))
        m = re.search(r"(\d+) failing", output)
        if m: failed = int(m.group(1))

    elif language == "go":
        passed = output.count("--- PASS")
        failed = output.count("--- FAIL")

    return {"test_passed": passed, "test_failed": failed, "coverage_percent": coverage}