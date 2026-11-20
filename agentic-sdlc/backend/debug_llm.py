"""
debug_llm.py  —  Test LLM response directly, no pipeline involved.

Usage (run from backend/ folder):
    python debug_llm.py
    python debug_llm.py --doc path/to/your/requirements.txt

This script:
1. Connects to Ollama (or Groq)
2. Sends a minimal requirements extraction prompt
3. Prints the FULL raw response so you can see exactly what the model returns
4. Tries to parse it and shows what was extracted
"""
import os, sys, asyncio, json, re

# Fix path
_BACKEND = os.path.dirname(os.path.abspath(__file__))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Load .env manually before anything else
def _load_env():
    candidates = [
        os.path.join(_BACKEND, ".env"),
        os.path.join(os.path.dirname(_BACKEND), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            print(f"Loading .env from: {path}")
            with open(path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = val_stripped = key.strip()
                    val = val.strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                        val = val[1:-1]
                    # Only set if not already in environment
                    if key not in os.environ:
                        os.environ[key] = val
            return path
    return None

_env_path = _load_env()
if not _env_path:
    print("Warning: no .env file found. Reading from environment variables only.")

# ── Sample doc (used if no --doc arg) ────────────────────────────────────────
SAMPLE_DOC = """
Project: TaskFlow
FR-001: Users shall register with email and password.
FR-002: Users shall log in and receive a JWT token.
FR-003: Authenticated users shall create, update, and delete tasks.
NFR-001: API response time shall be under 200ms at p95.
"""

# ── Minimal prompt ────────────────────────────────────────────────────────────
SYSTEM = """You are a business analyst. Extract requirements from the document.
Return ONLY a JSON object, nothing else. No explanation. No markdown outside the JSON.

Format:
{"requirements":[{"id":"REQ-001","title":"short title","description":"full description","type":"functional","priority":"high","acceptance_criteria":["AC1","AC2"]}]}"""

def build_user_msg(doc: str) -> str:
    return f"""Extract ALL requirements from this document. Return ONLY JSON.

DOCUMENT:
{doc}

Return ONLY a JSON object starting with {{ and ending with }}"""

# ── JSON extractor ────────────────────────────────────────────────────────────
def extract_json(text: str):
    if not text or not text.strip():
        return None, "empty response"

    # Fenced block
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if m:
        try:
            return json.loads(m.group(1).strip()), None
        except Exception as e:
            pass

    # Balanced brace walk
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0: start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(text[start:i+1]), None
                except Exception:
                    start, depth = -1, 0

    return None, "no valid JSON found"

# ── Main test ─────────────────────────────────────────────────────────────────
async def run_test(doc: str):
    # Detect provider
    import urllib.request
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    groq_key   = os.environ.get("GROQ_API_KEY", "")

    # Print what was loaded so we can verify
    print(f"OLLAMA_BASE_URL : {ollama_url}")
    print(f"OLLAMA_MODEL    : {os.environ.get('OLLAMA_MODEL', 'llama3.2 (default)')}")
    print(f"GROQ_API_KEY    : {'set (' + groq_key[:8] + '...)' if groq_key else 'not set'}")

    try:
        urllib.request.urlopen(ollama_url + "/api/tags", timeout=2)
        ollama_running = True
    except Exception:
        ollama_running = False

    print("\n" + "="*60)
    print("LLM DEBUG — Requirements Extraction Test")
    print("="*60)

    if ollama_running:
        model = os.environ.get("OLLAMA_MODEL", "llama3.2")
        print(f"Provider : Ollama  ({ollama_url})")
        print(f"Model    : {model}")
        provider = "ollama"
    elif groq_key:
        model = os.environ.get("GROQ_MODEL", "llama3-70b-8192")
        print(f"Provider : Groq cloud")
        print(f"Model    : {model}")
        provider = "groq"
    else:
        print("ERROR: No LLM available.")
        print("  Start Ollama:  ollama serve")
        print("  Or set:        GROQ_API_KEY=gsk_...")
        return

    print(f"\nDocument preview: {doc[:120].strip()}...")
    print(f"Document length : {len(doc)} chars")
    print("\nSending to LLM...")
    print("-"*60)

    # Build messages
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",   "content": build_user_msg(doc)},
    ]

    raw_response = ""

    if provider == "ollama":
        import httpx
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    f"{ollama_url}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": {"temperature": 0, "num_predict": 2048},
                    }
                )
                r.raise_for_status()
                data = r.json()
                raw_response = data.get("message", {}).get("content", "")
        except Exception as e:
            print(f"ERROR calling Ollama: {e}")
            return

    elif provider == "groq":
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}",
                             "Content-Type": "application/json"},
                    json={"model": model, "messages": messages,
                          "temperature": 0, "max_tokens": 2048},
                )
                r.raise_for_status()
                raw_response = r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"ERROR calling Groq: {e}")
            return

    # Show raw response
    print("\n>>> RAW LLM RESPONSE <<<")
    print("-"*60)
    print(raw_response if raw_response else "[EMPTY — model returned nothing]")
    print("-"*60)
    print(f"Response length: {len(raw_response)} characters")

    # Try to parse
    print("\n>>> PARSING RESULT <<<")
    parsed, err = extract_json(raw_response)

    if err:
        print(f"PARSE FAILED: {err}")
        print("\nDIAGNOSIS:")
        if not raw_response:
            print("  - Model returned empty string")
            print("  - Check if Ollama is running correctly: ollama ps")
            print("  - Try: ollama run " + model + " 'say hello'")
        elif len(raw_response) < 20:
            print(f"  - Response is very short ({len(raw_response)} chars): {raw_response!r}")
            print("  - Model may have hit context limit")
        elif "{" not in raw_response:
            print("  - No JSON braces found in response")
            print("  - Model returned prose instead of JSON")
            print("  - This model may need a different prompt style")
            print(f"  - Try a different model: ollama pull llama3.1")
        else:
            print("  - JSON found but could not be parsed")
            brace_idx = raw_response.find("{")
            print(f"  - First {{ at position {brace_idx}")
            print(f"  - Text around it: {raw_response[max(0,brace_idx-20):brace_idx+100]!r}")
    else:
        reqs = parsed.get("requirements", [])
        print(f"SUCCESS — extracted {len(reqs)} requirements")
        for r in reqs:
            print(f"\n  [{r.get('id','?')}] {r.get('title','?')}")
            print(f"       type={r.get('type','?')}  priority={r.get('priority','?')}")
            acs = r.get('acceptance_criteria', [])
            print(f"       acceptance_criteria: {len(acs)} items")

    # Advice
    print("\n>>> NEXT STEPS <<<")
    if not raw_response:
        print("  1. Check Ollama is running:  ollama ps")
        print("  2. Check model is pulled:    ollama list")
        print(f"  3. Test manually:            ollama run {model} 'reply with: {{\"ok\":true}}'")
    elif parsed and parsed.get("requirements"):
        print("  LLM is working correctly.")
        print("  The pipeline should extract requirements.")
        print("  If pipeline still shows 0: check backend logs for parse errors.")
    else:
        print(f"  The model '{model}' is not returning usable JSON.")
        print("  Try a different model:")
        print("    ollama pull llama3.1    # 8B, better instruction following")
        print("    ollama pull mistral     # 7B, excellent JSON output")
        print("    ollama pull qwen2.5     # 7B, very good at structured output")
        print(f"  Then update .env:  OLLAMA_MODEL=llama3.1")

    print("="*60 + "\n")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", help="Path to requirements doc to test with")
    args = parser.parse_args()

    if args.doc:
        with open(args.doc, encoding="utf-8") as f:
            doc = f.read()
    else:
        doc = SAMPLE_DOC
        print("(No --doc argument given, using built-in sample document)")

    asyncio.run(run_test(doc))
