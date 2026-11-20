"""
test_ollama.py — Run this while the pipeline is stuck to diagnose Ollama.

Usage:
    python test_ollama.py
"""
import os, sys, json, time
import urllib.request, urllib.error

_BACKEND = os.path.dirname(os.path.abspath(__file__))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Load .env
for path in [os.path.join(_BACKEND,".env"), os.path.join(os.path.dirname(_BACKEND),".env")]:
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k,_,v = line.partition("="); k=k.strip(); v=v.strip()
                if len(v)>=2 and v[0]==v[-1] and v[0] in ('"',"'"): v=v[1:-1]
                if k not in os.environ: os.environ[k]=v
        print(f"Loaded .env from {path}")
        break

BASE  = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL",    "llama3.1")

print(f"\nOLLAMA_BASE_URL : {BASE}")
print(f"OLLAMA_MODEL    : {MODEL}")
print()

def get(path):
    try:
        r = urllib.request.urlopen(BASE + path, timeout=5)
        return json.loads(r.read()), None
    except urllib.error.URLError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)

def post(path, body, timeout=30):
    try:
        data = json.dumps(body).encode()
        req  = urllib.request.Request(BASE + path, data=data,
               headers={"Content-Type":"application/json"})
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read()), None
    except Exception as e:
        return None, str(e)

# 1. Basic ping
print("── Test 1: Ollama reachable? ──")
data, err = get("/api/tags")
if err:
    print(f"  ✗ FAIL: {err}")
    print(f"  → Is Ollama running?  Run: ollama serve")
    sys.exit(1)
print(f"  ✓ Ollama is running")

# 2. Models installed
print("\n── Test 2: Models installed ──")
models = data.get("models", [])
if not models:
    print(f"  ✗ No models installed")
    print(f"  → Run: ollama pull {MODEL}")
else:
    for m in models:
        name = m.get("name","?")
        size = m.get("size",0) / 1e9
        mark = "◀ THIS" if MODEL in name else ""
        print(f"  • {name:<30} {size:.1f} GB  {mark}")

target = next((m for m in models if MODEL in m.get("name","")), None)
if not target:
    print(f"\n  ✗ Model '{MODEL}' not installed")
    print(f"  → Run: ollama pull {MODEL}")
    sys.exit(1)

# 3. Currently running
print("\n── Test 3: Models in memory (ollama ps) ──")
ps_data, ps_err = get("/api/ps")
if ps_err:
    print(f"  (cannot check — older Ollama version)")
else:
    running = ps_data.get("models", [])
    if running:
        for m in running:
            print(f"  ✓ LOADED: {m.get('name')} — {m.get('size_vram',0)//1e6:.0f} MB VRAM")
    else:
        print(f"  ⚠ No model loaded in memory yet")
        print(f"  → First call will load '{MODEL}' — this causes a slow start (30-60s)")
        print(f"  → Pre-load it: ollama run {MODEL}  (then Ctrl+C after it starts)")

# 4. Quick generation test
print(f"\n── Test 4: Simple generation test (timeout=60s) ──")
print(f"  Sending: {{\"model\":\"{MODEL}\", \"prompt\":\"Reply with: OK\"}}")
print(f"  Waiting...", end="", flush=True)
t0 = time.time()

try:
    data2 = json.dumps({
        "model": MODEL,
        "prompt": 'Reply with exactly the word: OK',
        "stream": False,
        "options": {"num_predict": 5, "temperature": 0}
    }).encode()
    req = urllib.request.Request(
        BASE + "/api/generate", data=data2,
        headers={"Content-Type":"application/json"})
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read())
    elapsed = time.time() - t0
    response_text = result.get("response","").strip()
    print(f"\n  ✓ Got response in {elapsed:.1f}s: {response_text!r}")

    if elapsed > 30:
        print(f"\n  ⚠ Response took {elapsed:.1f}s — model is very slow")
        print(f"  → Try a smaller model: ollama pull llama3.2  (3B, ~4GB)")
        print(f"  → Then set in .env:   OLLAMA_MODEL=llama3.2")
    else:
        print(f"\n  ✓ Model is responding normally")

except urllib.error.URLError as e:
    elapsed = time.time() - t0
    print(f"\n  ✗ FAILED after {elapsed:.1f}s: {e}")
    print(f"\n  DIAGNOSIS:")
    if "timed out" in str(e).lower():
        print(f"  • Ollama received the request but model is not generating")
        print(f"  • Check RAM: llama3.1 needs 8GB free RAM")
        print(f"  • Check GPU/CPU usage: Task Manager → Performance")
        print(f"  • Try smaller model: ollama pull llama3.2")
    else:
        print(f"  • {e}")

# 5. RAM check
print(f"\n── Test 5: System info ──")
try:
    import psutil
    ram   = psutil.virtual_memory()
    total = ram.total / 1e9
    avail = ram.available / 1e9
    used  = ram.percent
    print(f"  RAM total     : {total:.1f} GB")
    print(f"  RAM available : {avail:.1f} GB")
    print(f"  RAM used      : {used:.0f}%")
    if avail < 6:
        print(f"\n  ⚠ LOW RAM — llama3.1 (8B) needs ~8GB free")
        print(f"  → Close other apps or switch to llama3.2 (needs only 4GB)")
        print(f"  → ollama pull llama3.2  then .env: OLLAMA_MODEL=llama3.2")
    else:
        print(f"  ✓ Enough RAM for llama3.1")
except ImportError:
    print(f"  (install psutil for RAM check:  pip install psutil)")

print()
