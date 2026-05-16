#!/usr/bin/env python3
"""Benchmark all available Ollama models for Daleel QSE use."""
import subprocess, time, json, re, sys

MODELS = [
    "gemma3:1b",
    "llama3.2:1b",
    "gemma3:4b",
    "qwen2.5:3b",
    "llama3.2:latest",       # 3b
    "llama3.2-fast:latest",  # 3b
    "qwen2.5:7b",
    "llama3:latest",         # 8b
    "llama3.1:latest",       # 8b
    "llama3.1-fast:latest",  # 8b
    "gemma2:latest",         # 9b
]

PROMPTS = {
    "simple":   "What is the capital of Qatar? Answer in one sentence.",
    "finance":  "If I buy 1000 shares at QAR 15.50 and sell at QAR 18.20 with a 0.0275% commission each way, what is my net profit? Show the calculation.",
    "analysis": "Qatar National Bank (QNBK) just reported 8% year-over-year earnings growth. Briefly explain two key factors that could sustain this growth for a Qatar-focused stock analyst.",
}

def vram_used_mb():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True
        ).strip()
        return int(out.split("\n")[0])
    except Exception:
        return -1

def run_prompt(model, prompt):
    vram_before = vram_used_mb()
    t0 = time.time()
    first_token_time = None
    full_text = []

    try:
        proc = subprocess.Popen(
            ["ollama", "run", model],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        proc.stdin.write(prompt + "\n")
        proc.stdin.close()

        for char in iter(lambda: proc.stdout.read(1), ""):
            if first_token_time is None:
                first_token_time = time.time() - t0
            full_text.append(char)
            # timeout safety
            if time.time() - t0 > 120:
                proc.kill()
                break

        proc.wait(timeout=5)
    except Exception as e:
        return {"error": str(e), "ttft": -1, "total": -1, "vram_delta": -1, "response": ""}

    total_time = time.time() - t0
    vram_after = vram_used_mb()
    response = "".join(full_text).strip()

    return {
        "ttft":       round(first_token_time or total_time, 2),
        "total":      round(total_time, 2),
        "vram_delta": max(0, vram_after - vram_before),
        "response":   response,
    }

def score_finance(response: str) -> int:
    """1-5 score: checks for correct calculation steps."""
    r = response.lower()
    score = 1
    if "commission" in r:           score += 1
    if "net" in r or "profit" in r: score += 1
    # correct net profit ≈ 2668 QAR (rough check)
    if re.search(r'2[56789]\d\d|26[0-9]\d', r): score += 1
    if len(response) > 150:         score += 1
    return min(score, 5)

def score_analysis(response: str) -> int:
    """1-5 score: checks for domain relevance."""
    r = response.lower()
    score = 1
    keywords = ["loan", "deposit", "oil", "gas", "government", "liquidity",
                 "interest", "rate", "dividend", "expansion", "region", "growth"]
    hits = sum(1 for k in keywords if k in r)
    score += min(hits, 3)
    if len(response) > 200: score += 1
    return min(score, 5)

results = {}
print(f"\n{'='*72}")
print(f"  Daleel LLM Benchmark  |  GPU: Quadro P2000 5GB VRAM")
print(f"{'='*72}\n")

for model in MODELS:
    print(f"[{model}]  ", end="", flush=True)
    r = {}

    for key, prompt in PROMPTS.items():
        print(f"{key}.. ", end="", flush=True)
        r[key] = run_prompt(model, prompt)

    results[model] = r
    avg_ttft  = sum(r[k]["ttft"]  for k in PROMPTS) / len(PROMPTS)
    avg_total = sum(r[k]["total"] for k in PROMPTS) / len(PROMPTS)
    vram_used = max(r[k]["vram_delta"] for k in PROMPTS)
    fin_score = score_finance(r["finance"]["response"])
    ana_score = score_analysis(r["analysis"]["response"])
    results[model]["_summary"] = {
        "avg_ttft": round(avg_ttft, 2),
        "avg_total": round(avg_total, 2),
        "vram_mb": vram_used,
        "finance_score": fin_score,
        "analysis_score": ana_score,
    }
    print(f"done  (avg {avg_total:.1f}s, finance {fin_score}/5, analysis {ana_score}/5)")

# Save raw results
out_path = "/home/sadashi/qse-agent/benchmark_results.json"
with open(out_path, "w") as f:
    json.dump(results, f, indent=2)

# ── Print summary table ─────────────────────────────────────────────────────
print(f"\n{'='*100}")
print(f"{'Model':<26} {'Size':>6}  {'TTFT':>6}  {'Total':>6}  {'VRAM':>6}  {'Finance':>8}  {'Analysis':>9}  {'Fits 5GB?':>9}")
print(f"{'-'*100}")

MODEL_SIZES = {
    "gemma3:1b":            ("815MB",  True),
    "llama3.2:1b":          ("1.3GB",  True),
    "gemma3:4b":            ("3.3GB",  True),
    "qwen2.5:3b":           ("1.9GB",  True),
    "llama3.2:latest":      ("2.0GB",  True),
    "llama3.2-fast:latest": ("2.0GB",  True),
    "qwen2.5:7b":           ("4.7GB",  True),
    "llama3:latest":        ("4.7GB",  True),
    "llama3.1:latest":      ("4.9GB",  True),
    "llama3.1-fast:latest": ("4.9GB",  True),
    "gemma2:latest":        ("5.4GB", False),
}

for model in MODELS:
    s = results[model].get("_summary", {})
    size, fits = MODEL_SIZES.get(model, ("?", "?"))
    fits_str = "Yes" if fits else "NO (swap)"
    print(
        f"{model:<26} {size:>6}  "
        f"{s.get('avg_ttft', -1):>5.1f}s  "
        f"{s.get('avg_total', -1):>5.1f}s  "
        f"{s.get('vram_mb', -1):>5}MB  "
        f"{s.get('finance_score', '?'):>7}/5  "
        f"{s.get('analysis_score', '?'):>8}/5  "
        f"{fits_str:>9}"
    )

print(f"{'='*100}")
print(f"\nFull results saved to: {out_path}\n")
