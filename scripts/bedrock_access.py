import os
import sys
import time
from dotenv import load_dotenv
from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

client = OpenAI(
    api_key=os.environ["BEDROCK_API_KEY"],
    base_url="https://bedrock-runtime.ap-southeast-3.amazonaws.com/openai/v1",
    timeout=120.0,
)

models = [
    "moonshotai.kimi-k2.5",
    "deepseek.v3.2",
    "qwen.qwen3-235b-a22b-2507-v1:0",
    "openai.gpt-oss-120b-1:0",
]

prompt = "Write a 200-word bedtime story about a unicorn."
results = []

for model in models:
    print(f"\n=== {model} ===", flush=True)
    try:
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        elapsed = time.perf_counter() - start
        usage = response.usage
        out_tokens = usage.completion_tokens if usage else 0
        tps = out_tokens / elapsed if elapsed > 0 else 0
        print(response.choices[0].message.content, flush=True)
        print(
            f"\n[time={elapsed:.2f}s, out_tokens={out_tokens}, tok/s={tps:.1f}]",
            flush=True,
        )
        results.append((model, elapsed, out_tokens, tps))
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        results.append((model, None, None, None))

print("\n=== summary ===", flush=True)
print(f"{'model':<40} {'time(s)':>8} {'out_tok':>8} {'tok/s':>8}", flush=True)
for model, elapsed, out_tokens, tps in results:
    if elapsed is None:
        print(f"{model:<40} {'ERR':>8} {'-':>8} {'-':>8}", flush=True)
    else:
        print(f"{model:<40} {elapsed:>8.2f} {out_tokens:>8} {tps:>8.1f}", flush=True)
