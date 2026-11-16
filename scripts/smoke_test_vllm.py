import time, json, subprocess
from vllm import LLM, SamplingParams

MODEL_PATH = "/home/admin/munimai/models/Qwen2.5-7B-Instruct-AWQ"

print("=== Loading model ===", flush=True)
t0 = time.time()
llm = LLM(
    model=MODEL_PATH,
    quantization="awq",
    gpu_memory_utilization=0.45,   # ~14.4GB of 32GB card, under our 15GB live budget
    max_model_len=8192,
    dtype="float16",
)
load_time = time.time() - t0
print(f"=== Model loaded in {load_time:.1f}s ===", flush=True)

def chat(prompt, max_tokens=200, temperature=0.2):
    messages = [{"role": "user", "content": prompt}]
    text = llm.get_tokenizer().apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    params = SamplingParams(temperature=temperature, max_tokens=max_tokens)
    t0 = time.time()
    out = llm.generate([text], params)
    elapsed = time.time() - t0
    return out[0].outputs[0].text.strip(), elapsed

results = {}

print("\n=== TEST A: English GST question ===", flush=True)
resp, dt = chat("In one sentence, what is an e-way bill used for?")
print(f"[{dt:.2f}s] {resp}", flush=True)
results["A_english"] = {"response": resp, "latency_s": dt}

print("\n=== TEST B: Hindi response ===", flush=True)
resp, dt = chat("Ek chhote vyapari ko GST filing ke baare mein ek saral sawaal ka jawab Hindi mein dijiye: GST return kab file karna chahiye?")
print(f"[{dt:.2f}s] {resp}", flush=True)
results["B_hindi"] = {"response": resp, "latency_s": dt}

print("\n=== TEST C: Structured JSON tool-call ===", flush=True)
schema_prompt = (
    'You are a tool-calling agent. Respond ONLY with valid JSON, no other text, '
    'matching this schema: {"intent": string, "amount": number, "currency": string}. '
    'Input: "Lakshmi ne 5000 rupaye ka bill bheja"'
)
resp, dt = chat(schema_prompt, max_tokens=100, temperature=0.0)
print(f"[{dt:.2f}s] {resp}", flush=True)
json_valid = False
parsed = None
try:
    cleaned = resp.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").replace("json\n", "", 1).strip()
    parsed = json.loads(cleaned)
    json_valid = True
except Exception as e:
    parsed = f"PARSE_ERROR: {e}"
results["C_json_toolcall"] = {"response": resp, "latency_s": dt, "json_valid": json_valid, "parsed": parsed}

print(f"\n=== JSON valid: {json_valid} | parsed: {parsed} ===", flush=True)

print("\n=== nvidia-smi snapshot while model loaded ===", flush=True)
smi = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv"], capture_output=True, text=True)
print(smi.stdout, flush=True)
results["vram_snapshot"] = smi.stdout.strip()
results["load_time_s"] = load_time

with open("/home/admin/munimai/logs/smoke_test_results.json", "w") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print("\n=== DONE. Results written to /home/admin/munimai/logs/smoke_test_results.json ===", flush=True)
