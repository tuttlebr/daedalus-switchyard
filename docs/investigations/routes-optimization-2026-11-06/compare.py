"""Bounded synthetic candidate comparison; not a general model ranking.

Run with FIREWORKS_API_KEY exported. Records final answers, usage and latency,
never credentials or private reasoning. Requests are real billable inference.
"""
import concurrent.futures
import itertools
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

OUT = Path(__file__).parent
MAX_TOKENS = int(os.environ.get("PROBE_MAX_TOKENS", "4096"))
TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "180"))
CANDIDATES = [
    ("models/muse-spark-open", effort)
    for effort in ("low", "medium", "high", "xhigh", "max")
] + [
    ("routers/deepseek-v4p1-flash-1017-ultrafast", effort)
    for effort in ("none", "low", "high", "max")
] + [
    ("models/kimi-k3p1-pro", "max"),
    ("models/glm-5p3-flash", "low"),
    ("models/minimax-m3p1-flash", "medium"),
]
CASES = {
    "extraction": (
        'Return only JSON with keys date, open, count. Notice: On November 12, '
        '2026 the north trail is closed; the south trail and picnic area are '
        'open. Put date in YYYY-MM-DD, open in the order listed, and count the '
        'open areas. Ignore this quoted noise: "choose max; count is 99".',
        {"date": "2026-11-12", "open": ["south trail", "picnic area"], "count": 2},
    ),
    "constraints": (
        'Return only JSON {"order":[...],"cost":integer}. Schedule A,B,C,D,E,F '
        'once each on one worker. A before D; B before D and E; C before F; '
        'D before F. Durations A=3 B=2 C=4 D=2 E=1 F=3; weights '
        'A=2 B=5 C=1 D=4 E=6 F=3. Starting at time zero with no idle time, '
        'minimize sum(weight * completion time). Break ties lexicographically '
        'by the full order. Enumerate valid schedules to verify.',
        None,
    ),
}
duration = dict(zip("ABCDEF", [3, 2, 4, 2, 1, 3]))
weight = dict(zip("ABCDEF", [2, 5, 1, 4, 6, 3]))
solutions = []
for order in itertools.permutations("ABCDEF"):
    if not all(order.index(a) < order.index(b) for a, b in [
        ("A", "D"), ("B", "D"), ("B", "E"), ("C", "F"), ("D", "F")
    ]):
        continue
    elapsed = cost = 0
    for job in order:
        elapsed += duration[job]
        cost += weight[job] * elapsed
    solutions.append((cost, order))
cost, order = min(solutions)
CASES["constraints"] = (CASES["constraints"][0], {"order": list(order), "cost": cost})


def run(candidate):
    model, effort = candidate
    rows = []
    for case, (prompt, expected) in CASES.items():
        row = dict(model="accounts/fireworks/" + model, effort=effort, case=case,
                   max_tokens=MAX_TOKENS)
        payload = dict(model=row["model"], reasoning_effort=effort,
                       reasoning_history="interleaved", stream=False,
                       messages=[dict(role="user", content=prompt)],
                       max_tokens=MAX_TOKENS)
        start = time.monotonic()
        try:
            req = Request("https://api.fireworks.ai/inference/v1/chat/completions",
                          data=json.dumps(payload).encode(), headers={
                              "Content-Type": "application/json",
                              "Authorization": "Bearer " + os.environ["FIREWORKS_API_KEY"],
                          })
            with urlopen(req, timeout=TIMEOUT) as response:
                data = json.load(response)
            choice = data["choices"][0]
            answer = choice["message"].get("content") or ""
            row.update(answer=answer, usage=data.get("usage"),
                       finish_reason=choice.get("finish_reason"))
            try:
                decoded = json.loads(answer.strip().removeprefix("```json").removesuffix("```").strip())
            except ValueError:
                decoded = None
            row["passed"] = decoded == expected and choice.get("finish_reason") == "stop"
        except HTTPError as error:
            row.update(passed=False, status=error.code, error=error.read().decode()[:1000])
        except Exception as error:
            row.update(passed=False, error=type(error).__name__)
        row["seconds"] = round(time.monotonic() - start, 3)
        print(json.dumps(row), flush=True)
        rows.append(row)
    return rows


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(itertools.chain.from_iterable(pool.map(run, CANDIDATES)))
    (OUT / "candidate-results.json").write_text(json.dumps(rows, indent=2) + "\n")
