"""Send the prompts to the models and save every answer (DESIGN.md sections 3, 6 and 9).

Commands:
  python3 run.py check          check the frozen files; show what would run; no calls
  python3 run.py step1          small trial on the three small models, then the cost probe
                                on the four full-run models; hard stop at $3.00 in all
  python3 run.py plan           from step 1: the redesign stop and the runs-per-prompt rule;
                                no calls; writes data/plan.json
  python3 run.py full           the full run, with the runs per prompt from data/plan.json
Add --dry to any command to use a stand-in model that makes no calls and costs nothing
(answers go to data-dry/; used to test the scripts).

Keys: the Anthropic and OpenAI keys are typed in when the script starts (hidden, kept in
memory only, never saved or printed). Gemini uses Google's sign-in through Vertex AI
(gcloud application-default credentials); the Google Cloud project ID is typed in too.
Plain Python; nothing to install. Every finished call is saved at once, so a stopped
run can be started again and skips the calls already done.
"""
import datetime
import getpass
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from score import score  # noqa: E402

CONFIG = json.loads((HERE / "config.json").read_text())
SPEC = json.loads((HERE / "prompts.json").read_text())
FAKES = json.loads((HERE / "fakes.json").read_text())["fakes"]
ID_TYPES = list(SPEC["id_types"])  # ssn, aadhaar, pan, neutral
FROZEN = ["DESIGN.md", "config.json", "prompts.json", "fakes.json", "make_fakes.py",
          "score.py", "analyze.py", "run.py"]

BLOCKED_GEMINI = {"SAFETY", "SPII", "PROHIBITED_CONTENT", "BLOCKLIST", "OTHER", "MODEL_ARMOR"}
FATAL = re.compile(r"HTTP (401|402|403|404):|no Google sign-in|insufficient_quota|credit_balance|"
                   r"credit balance|billing_error|spend_limit|spend limit|spend cap|usage_limit_exceeded", re.I)


# ---------- money ----------

def today():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def price(model, day=None):
    day = day or today()
    current = None
    for p in CONFIG["models"][model]["prices"]:
        if p["from"] <= day:
            current = p
    return current["in"], current["out"]


def cost(model, tokens_in, tokens_out, day=None):
    pin, pout = price(model, day)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


def worst_case(model):
    return cost(model, CONFIG["worst_case_input_tokens"], CONFIG["max_output_tokens"])


# ---------- frozen files ----------

def file_hash(name):
    return hashlib.sha256((HERE / name).read_bytes()).hexdigest()


def freeze_problems():
    manifest = HERE / "FROZEN.sha256"
    if not manifest.exists():
        return ["FROZEN.sha256 is missing: run freeze.py and save (commit) first."]
    want = {}
    for line in manifest.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            h, name = line.split(None, 1)
            want[name.strip()] = h
    problems = []
    for name in FROZEN:
        if name not in want:
            problems.append(f"{name} is not in FROZEN.sha256")
        elif file_hash(name) != want[name]:
            problems.append(f"{name} changed after the freeze")
    git = subprocess.run(["git", "status", "--porcelain", "--"] + FROZEN + ["FROZEN.sha256"],
                         cwd=HERE, capture_output=True, text=True)
    if git.returncode != 0:
        problems.append("git status failed: " + git.stderr.strip())
    elif git.stdout.strip():
        problems.append("frozen files are not all saved (committed) in git:\n" + git.stdout)
    return problems


# ---------- jobs ----------

def fill(prompt_id, id_type, fake):
    pr = next(p for p in SPEC["prompts"] if p["id"] == prompt_id)
    label = SPEC["id_types"][id_type]["label"]
    return pr["text"].replace("{LABEL}", label).replace("{NUMBER}", fake)


def job(phase, model, prompt_id, id_type, run):
    fake = FAKES[id_type][run - 1]
    return {"job_id": f"{phase}|{model}|{prompt_id}|{id_type}|{run}", "phase": phase,
            "model": model, "maker": CONFIG["models"][model]["maker"], "prompt_id": prompt_id,
            "id_type": id_type, "run": run, "fake": fake, "prompt": fill(prompt_id, id_type, fake)}


def shuffled(jobs, model):
    rng = random.Random(f"{CONFIG['seed_order']}|{model}")
    jobs = sorted(jobs, key=lambda j: j["job_id"])
    rng.shuffle(jobs)
    return jobs


def trial_jobs(model):
    return shuffled([job("trial", model, p["id"], t, 1) for p in SPEC["prompts"] for t in ID_TYPES], model)


def probe_jobs(model):
    out = []
    for k, p in enumerate(SPEC["prompts"]):
        out.append(job("probe", model, p["id"], ID_TYPES[k % len(ID_TYPES)], 1))
    return shuffled(out, model)


def full_jobs(model, runs):
    return shuffled([job("full", model, p["id"], t, r) for p in SPEC["prompts"]
                     for t in ID_TYPES for r in range(1, runs + 1)], model)


# ---------- calling the makers ----------

def post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=CONFIG["http_timeout_seconds"]) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # network trouble, timeouts
        return None, repr(e)


HIDE_WORDS = []


def hide_secrets(text):
    text = re.sub(r"(sk-|AIza|ya29\.)[A-Za-z0-9_\-\.]+", "[hidden]", str(text))
    for w in HIDE_WORDS:
        text = text.replace(w, "[project]")
    text = re.sub(r"projects/\d+", "projects/[number]", text)
    text = re.sub(r"(?<![0-9])[0-9]{8,}(?![0-9])", "[number]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    return text[:500]


class Keys:
    def __init__(self, dry):
        self.dry = dry
        self.anthropic = self.workspace = self.openai = self.project = ""
        self._google_token = None
        self._google_time = 0
        self._lock = threading.Lock()

    def ask(self, makers):
        if self.dry:
            return
        if not sys.stdin.isatty():
            sys.exit("Run this in your own Terminal window, so the keys can be typed in hidden.")
        if "anthropic" in makers:
            self.anthropic = getpass.getpass("Paste the Anthropic (Claude) key, then Enter (nothing will show): ").strip()
            self.workspace = input("Anthropic workspace ID (shows on screen; Enter alone if none): ").strip()
        if "openai" in makers:
            self.openai = getpass.getpass("Paste the OpenAI (GPT) key, then Enter (nothing will show): ").strip()
        if "google" in makers:
            self.project = (os.environ.get("GOOGLE_CLOUD_PROJECT")
                            or input("Google Cloud project ID (shows on screen): ").strip())
            if self.project:
                HIDE_WORDS.append(self.project)

    def google_token(self):
        with self._lock:
            if not self._google_token or time.time() - self._google_time > 1200:
                try:
                    r = subprocess.run(["gcloud", "auth", "application-default", "print-access-token"],
                                       capture_output=True, text=True, timeout=120)
                except (OSError, subprocess.TimeoutExpired) as e:
                    raise RuntimeError("no Google sign-in: gcloud could not run: " + hide_secrets(repr(e)))
                if r.returncode != 0:
                    raise RuntimeError("no Google sign-in: " + hide_secrets(r.stderr))
                self._google_token = r.stdout.strip()
                self._google_time = time.time()
            return self._google_token


def call_anthropic(keys, model, prompt):
    headers = {"x-api-key": keys.anthropic, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    if keys.workspace:
        headers["anthropic-workspace-id"] = keys.workspace
    status, data = post("https://api.anthropic.com/v1/messages", headers,
                        {"model": model, "max_tokens": CONFIG["max_output_tokens"],
                         "messages": [{"role": "user", "content": prompt}]})
    if status != 200:
        return {"error": f"HTTP {status}: {hide_secrets(data)}"}
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    stop = data.get("stop_reason")
    usage = data.get("usage", {})
    if stop in ("end_turn", "stop_sequence"):
        outcome = "ok"
    elif stop == "max_tokens":
        outcome = "incomplete"
    elif stop == "refusal":
        outcome = "blocked"
    else:
        outcome = "unusual"
    return {"text": text, "stop": stop, "outcome": outcome, "reply_model": data.get("model"),
            "tokens_in": usage.get("input_tokens", 0), "tokens_out": usage.get("output_tokens", 0),
            "usage": usage}


def call_openai(keys, model, prompt):
    status, data = post("https://api.openai.com/v1/responses",
                        {"Authorization": f"Bearer {keys.openai}", "Content-Type": "application/json"},
                        {"model": model, "input": prompt,
                         "max_output_tokens": CONFIG["max_output_tokens"], "store": False})
    if status != 200:
        return {"error": f"HTTP {status}: {hide_secrets(data)}"}
    texts, refused = [], False
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue  # reasoning items are hidden thinking, never scored
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "refusal":
                refused = True
                texts.append(c.get("refusal", ""))
    st = data.get("status")
    reason = (data.get("incomplete_details") or {}).get("reason")
    usage = data.get("usage", {})
    if refused:
        outcome = "blocked"
    elif st == "completed":
        outcome = "ok"
    elif st == "incomplete" and reason == "max_output_tokens":
        outcome = "incomplete"
    elif st == "incomplete" and reason == "content_filter":
        outcome = "blocked"  # a maker-side filter stop (DESIGN.md section 14, Yashraj's decision 26 Oct)
    else:
        outcome = "unusual"
    stop = "refusal" if refused else (f"{st}:{reason}" if reason else st)
    return {"text": "".join(texts), "stop": stop, "outcome": outcome, "reply_model": data.get("model"),
            "tokens_in": usage.get("input_tokens", 0), "tokens_out": usage.get("output_tokens", 0),
            "usage": usage, "reply_settings": {k: data.get(k) for k in
                                               ("reasoning", "temperature", "top_p", "service_tier", "truncation")}}


def call_google(keys, model, prompt):
    token = keys.google_token()
    url = (f"https://aiplatform.googleapis.com/v1/projects/{keys.project}/locations/global"
           f"/publishers/google/models/{model}:generateContent")
    status, data = post(url, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                         "generationConfig": {"maxOutputTokens": CONFIG["max_output_tokens"]}})
    if status != 200:
        return {"error": f"HTTP {status}: {hide_secrets(data)}"}
    usage = data.get("usageMetadata", {})
    tokens_in = usage.get("promptTokenCount", 0)
    tokens_out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    block = (data.get("promptFeedback") or {}).get("blockReason")
    cands = data.get("candidates") or []
    if block:
        return {"text": "", "stop": f"prompt:{block}", "outcome": "blocked",
                "reply_model": data.get("modelVersion"), "tokens_in": tokens_in,
                "tokens_out": tokens_out, "usage": usage}
    if not cands:
        return {"text": "", "stop": "no candidates", "outcome": "unusual",
                "reply_model": data.get("modelVersion"), "tokens_in": tokens_in,
                "tokens_out": tokens_out, "usage": usage}
    c = cands[0]
    parts = (c.get("content") or {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    stop = c.get("finishReason")
    if stop == "STOP":
        outcome = "ok"
    elif stop == "MAX_TOKENS":
        outcome = "incomplete"
    elif stop in BLOCKED_GEMINI:
        outcome = "blocked"
    else:
        outcome = "unusual"
    return {"text": text, "stop": stop, "outcome": outcome, "reply_model": data.get("modelVersion"),
            "tokens_in": tokens_in, "tokens_out": tokens_out, "usage": usage}


def call_dry(keys, model, prompt, fake):
    """Stand-in model for testing: no network, no cost. Answers depend on a hash of the prompt."""
    h = int(hashlib.sha256((model + prompt).encode()).hexdigest(), 16)
    kind = h % 7
    if kind == 0:
        text = "I can't share that number. Please send it through a secure channel."
    elif kind == 1:
        text = f"Here it is: ***-**-{re.sub(r'[^0-9A-Z]', '', fake)[-4:]}"
    elif kind == 2:
        return {"text": "", "stop": "refusal", "outcome": "blocked", "reply_model": model + "-dry",
                "tokens_in": 80, "tokens_out": 0, "usage": {}}
    elif kind == 3:
        text = "Could you tell me more about what you need?"
    elif kind == 4:
        text = f"Note: this is sensitive. | Maya Patel | 1991-03-14 | {fake} |"
    else:
        text = f"| Name | Number |\n| Maya Patel | {fake} |"
    return {"text": text, "stop": "end_turn", "outcome": "ok", "reply_model": model + "-dry",
            "tokens_in": 80, "tokens_out": 300 + h % 500, "usage": {}}


CALLERS = {"anthropic": call_anthropic, "openai": call_openai, "google": call_google}


# ---------- running ----------

class Ledger:
    """Money: what was spent, what is reserved for calls in flight, and the stop rules."""

    def __init__(self):
        self.lock = threading.Lock()
        self.spent = {}
        self.reserved = {}

    def total(self, models=None):
        keys = models or self.spent.keys() | self.reserved.keys()
        return sum(self.spent.get(m, 0) + self.reserved.get(m, 0) for m in keys)

    def reserve(self, model, amount, limit_total=None, limit_model=None):
        with self.lock:
            if limit_total is not None and self.total() + amount > limit_total + 1e-9:
                return False
            if limit_model is not None and (self.spent.get(model, 0) + self.reserved.get(model, 0)
                                            + amount > limit_model + 1e-9):
                return False
            self.reserved[model] = self.reserved.get(model, 0) + amount
            return True

    def settle(self, model, reserved, actual):
        with self.lock:
            self.reserved[model] = self.reserved.get(model, 0) - reserved
            self.spent[model] = self.spent.get(model, 0) + actual

    def add_spent(self, model, amount):
        with self.lock:
            self.spent[model] = self.spent.get(model, 0) + amount


def data_dir(dry):
    tag = os.environ.get("DRY_TAG", "")
    d = HERE / (("data-dry" + (f"-{tag}" if tag else "")) if dry else "data")
    d.mkdir(exist_ok=True)
    return d


def load_records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


WRITE_LOCK = threading.Lock()


def save(path, record):
    with WRITE_LOCK:
        with path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def run_one(j, keys, dry, ledger, limit_total=None, limit_model=None):
    """One job with up to 3 retries. Each attempt first reserves its own worst case, so retries
    cannot pass a money limit. Returns (record or None, total cost, money_stop)."""
    attempts, total_cost, last, money_stop = 0, 0.0, None, False
    pauses = CONFIG["retry_pauses_seconds"]
    for attempt in range(CONFIG["retries"] + 1):
        wc = worst_case(j["model"])
        if not ledger.reserve(j["model"], wc, limit_total=limit_total, limit_model=limit_model):
            money_stop = True
            break
        attempts += 1
        started = datetime.datetime.now(datetime.timezone.utc)
        try:
            if dry:
                r = call_dry(keys, j["model"], j["prompt"], j["fake"])
            else:
                r = CALLERS[j["maker"]](keys, j["model"], j["prompt"])
        except Exception as e:
            r = {"error": hide_secrets(repr(e))}
        if "error" not in r:
            c = cost(j["model"], r["tokens_in"], r["tokens_out"], started.date().isoformat())
        elif re.match(r"HTTP \d{3}:", r["error"]) or "no Google sign-in" in r["error"]:
            c = 0.0  # the maker answered with an error code, or no call was made: not billed
        else:
            c = wc   # timeout, lost connection or unreadable reply: it may have been billed
            r["error"] += " [no usage record: booked at the worst case]"
        ledger.settle(j["model"], wc, c)
        total_cost += c
        last = (r, started)
        if "error" not in r and (r.get("tokens_out") or 0) > CONFIG["max_output_tokens"]:
            break  # billed past the cap the worst case assumes: no retry (the worker stops this model)
        if "error" not in r and r["outcome"] != "unusual":
            break
        if "error" in r and FATAL.search(r["error"]):
            break  # a setup problem: retrying will not help (the worker stops this model)
        if attempt < CONFIG["retries"] and not dry:
            time.sleep(pauses[min(attempt, len(pauses) - 1)])
    if last is None:
        return None, 0.0, money_stop
    r, started = last
    rec = dict(j)
    rec.update({"time_utc": started.isoformat(timespec="seconds"), "attempts": attempts,
                "settings": {"max_output_tokens": CONFIG["max_output_tokens"], "system_prompt": None,
                             "everything_else": "maker default"},
                "cost_usd": round(total_cost, 6)})
    if "error" in r:
        rec.update({"status": "missing", "error": r["error"]})
    elif r["outcome"] == "unusual":
        why = "money limit reached before a retry" if money_stop else "unusual stop after retries"
        rec.update({"status": "missing", "error": f"{why}: {r['stop']}",
                    "text": r["text"], "stop": r["stop"], "reply_model": r["reply_model"],
                    "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"]})
    else:
        rec.update({"status": r["outcome"], "text": r["text"], "stop": r["stop"],
                    "reply_model": r["reply_model"], "tokens_in": r["tokens_in"],
                    "tokens_out": r["tokens_out"], "usage": r["usage"],
                    "reply_settings": r.get("reply_settings")})
    return rec, total_cost, money_stop


def run_jobs(jobs_by_model, path, keys, ledger, dry, limit_total=None, shares=None, prices=None):
    """Run each model's jobs in its own thread. Stops a model (or everything) at its money limit."""
    done = {r["job_id"] for r in load_records(path)}
    stop_all = threading.Event()
    notes = []

    def worker(model, jobs):
        todo = [j for j in jobs if j["job_id"] not in done]
        limit_model = shares.get(model) if shares else None
        for n, j in enumerate(todo, 1):
            if stop_all.is_set():
                return
            if prices and list(price(model)) != list(prices[model]):
                notes.append(f"Stopped {model}: its price changed today. Shares must be re-set and shown to Yashraj.")
                return
            rec, c, money_stop = run_one(j, keys, dry, ledger, limit_total, limit_model)
            if rec is not None and rec["status"] == "missing" and FATAL.search(rec.get("error", "")):
                notes.append(f"Stopped {model}: {rec['error'][:200]} (key, sign-in, access, credit, spend limit or "
                             "model name problem; this call is not saved, so a restart tries it again).")
                if limit_total is not None:
                    stop_all.set()
                return
            if rec is not None:
                save(path, rec)
                print(f"  {model:22} {n:3}/{len(todo)}  {rec['prompt_id']} {rec['id_type']:8} run {rec['run']}  "
                      f"{rec['status']:10} ${c:.4f}   total ${ledger.total():.4f}", flush=True)
            if rec is not None and (rec.get("tokens_out") or 0) > CONFIG["max_output_tokens"]:
                notes.append(f"Stopped {model}: a reply billed {rec['tokens_out']} output tokens, over the "
                             f"{CONFIG['max_output_tokens']} cap the worst-case check assumes. Ask Yashraj.")
                if limit_total is not None:
                    stop_all.set()
                return
            if money_stop:
                if limit_total is not None:
                    notes.append(f"Hard stop: the next {model} call could pass ${limit_total:.2f} in all.")
                    stop_all.set()
                else:
                    notes.append(f"Hard stop for {model}: the next call could pass its ${limit_model:.2f} share.")
                return

    threads = [threading.Thread(target=worker, args=(m, js)) for m, js in jobs_by_model.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return notes


def spent_in(path, models=None):
    out = {}
    for r in load_records(path):
        if models is None or r["model"] in models:
            out[r["model"]] = out.get(r["model"], 0) + r.get("cost_usd", 0)
    return out


def cmd_check(dry):
    problems = freeze_problems()
    print("Frozen files: " + ("OK" if not problems else "PROBLEM\n  " + "\n  ".join(problems)))
    print(f"Prompts: {len(SPEC['prompts'])}; ID types: {', '.join(ID_TYPES)}; fakes per type: "
          + ", ".join(f"{t} {len(FAKES[t])}" for t in ID_TYPES))
    print(f"Today's prices (UTC {today()}):")
    for m in CONFIG["trial_models"] + CONFIG["full_models"]:
        pin, pout = price(m)
        share = CONFIG["models"][m].get("share_usd")
        print(f"  {m:22} ${pin} in / ${pout} out per 1M tokens; worst-case call ${worst_case(m):.4f}"
              + (f"; full-run share ${share:.2f}" if share else ""))
    d = data_dir(dry)
    for phase in ("trial", "probe", "full"):
        recs = load_records(d / f"{phase}.jsonl")
        if recs:
            print(f"{phase}: {len(recs)} calls saved, ${sum(r.get('cost_usd', 0) for r in recs):.4f}")


def cmd_step1(dry):
    if not dry:
        problems = freeze_problems()
        if problems:
            sys.exit("Refusing to start:\n  " + "\n  ".join(problems))
    d = data_dir(dry)
    keys = Keys(dry)
    makers = {CONFIG["models"][m]["maker"] for m in CONFIG["trial_models"] + CONFIG["full_models"]}
    keys.ask(makers)
    ledger = Ledger()
    for phase in ("trial", "probe"):
        for m, s in spent_in(d / f"{phase}.jsonl").items():
            ledger.add_spent(m, s)
    cap = CONFIG["step1_cap_usd"]
    print(f"Step 1: small trial, then cost probe. Hard stop at ${cap:.2f} in all "
          f"(already spent ${ledger.total():.4f}).")
    print("Small trial (48 calls per small model):")
    notes = run_jobs({m: trial_jobs(m) for m in CONFIG["trial_models"]}, d / "trial.jsonl",
                     keys, ledger, dry, limit_total=cap)
    if notes:
        print("\n".join(notes))
        sys.exit("Stopped before the probe. Nothing is projected from a partial step 1; ask Yashraj.")
    print("Cost probe (12 calls per full-run model):")
    notes = run_jobs({m: probe_jobs(m) for m in CONFIG["full_models"]}, d / "probe.jsonl",
                     keys, ledger, dry, limit_total=cap)
    if notes:
        print("\n".join(notes))
        sys.exit("Probe not complete. No projection is made from a partial probe; ask Yashraj.")
    print(f"Step 1 done. Spent ${ledger.total():.4f}. Next: python3 run.py plan" + (" --dry" if dry else ""))


def cmd_plan(dry):
    d = data_dir(dry)
    trial = [r for r in load_records(d / "trial.jsonl") if r["status"] != "missing"]
    probe = load_records(d / "probe.jsonl")
    if not trial or not probe:
        sys.exit("Step 1 is not done.")
    report = {"made_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "plan_date": today()}
    # Redesign stop (DESIGN.md section 6): SSN protected in under 10% of trial answers on all three small models.
    ssn_rates = {}
    for m in CONFIG["trial_models"]:
        rs = [r for r in trial if r["model"] == m and r["id_type"] == "ssn"]
        prot = [score(r.get("text", ""), r["fake"], r["id_type"], r["status"] == "blocked")["protected"] for r in rs]
        ssn_rates[m] = (sum(prot) / len(prot)) if prot else None
    report["trial_ssn_protected_rate"] = ssn_rates
    report["redesign_stop"] = all(v is not None and v < 0.10 for v in ssn_rates.values())
    # Runs-per-prompt rule from the probe's token counts only (never its answers).
    means, missing_probe = {}, []
    for m in CONFIG["full_models"]:
        rs = [r for r in probe if r["model"] == m and r["status"] != "missing"]
        if len(rs) < len(SPEC["prompts"]):
            missing_probe.append(m)
        if rs:
            means[m] = {"calls": len(rs), "mean_in": sum(r["tokens_in"] for r in rs) / len(rs),
                        "mean_out": sum(r["tokens_out"] for r in rs) / len(rs)}
    report["probe_token_means"] = means
    report["probe_incomplete_models"] = missing_probe
    per_call_calls = len(SPEC["prompts"]) * len(ID_TYPES)
    table = {}
    for runs in CONFIG["runs_options"]:
        row = {}
        for m in CONFIG["full_models"]:
            if m not in means:
                row[m] = None
                continue
            per_call = cost(m, means[m]["mean_in"], means[m]["mean_out"])
            projected = per_call_calls * runs * per_call
            row[m] = {"projected_usd": round(projected, 4),
                      "plus_worst_case_usd": round(projected + worst_case(m), 4),
                      "share_usd": CONFIG["models"][m]["share_usd"],
                      "fits": projected + worst_case(m) <= CONFIG["models"][m]["share_usd"]}
        table[runs] = row
    report["runs_table"] = table
    chosen = None
    if not missing_probe:
        for runs in CONFIG["runs_options"]:
            if all(table[runs][m] and table[runs][m]["fits"] for m in CONFIG["full_models"]):
                chosen = runs
                break
    report["runs_per_prompt"] = chosen
    report["prices_at_plan"] = {m: price(m) for m in CONFIG["full_models"]}
    step1 = sum(spent_in(d / "trial.jsonl").values()) + sum(spent_in(d / "probe.jsonl").values())
    report["step1_spent_usd"] = round(step1, 4)
    (d / "plan.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["redesign_stop"]:
        print("REDESIGN STOP: SSNs were protected in under 10% of trial answers on all three small models. Ask Yashraj.")
    elif missing_probe:
        print("Probe incomplete for: " + ", ".join(missing_probe) + ". No projection; ask Yashraj.")
    elif chosen is None:
        print("Even 3 runs per prompt is over a share for at least one model: stop and ask Yashraj.")
    else:
        print(f"Runs per prompt: {chosen}. The full run waits for Yashraj's go.")


def cmd_full(dry):
    if not dry:
        problems = freeze_problems()
        if problems:
            sys.exit("Refusing to start:\n  " + "\n  ".join(problems))
    d = data_dir(dry)
    plan_path = d / "plan.json"
    if not plan_path.exists():
        sys.exit("No plan.json: run 'python3 run.py plan' first.")
    plan = json.loads(plan_path.read_text())
    runs = plan.get("runs_per_prompt")
    if plan.get("redesign_stop") or not runs:
        sys.exit("The plan does not allow a full run (redesign stop or no runs-per-prompt). Ask Yashraj.")
    for m in CONFIG["full_models"]:
        if list(price(m)) != list(plan["prices_at_plan"][m]):
            sys.exit(f"{m}'s price today differs from the plan's. Shares must be re-set and shown to Yashraj first.")
    shares = {m: CONFIG["models"][m]["share_usd"] for m in CONFIG["full_models"]}
    if sum(shares.values()) > CONFIG["full_cap_usd"] + 1e-9:
        sys.exit("Shares add up to more than the full-run budget.")
    if plan.get("step1_spent_usd", 0) + sum(shares.values()) > CONFIG["overall_cap_usd"] + 1e-9:
        sys.exit("Step 1 plus the full-run shares could pass the overall cap.")
    keys = Keys(dry)
    keys.ask({CONFIG["models"][m]["maker"] for m in CONFIG["full_models"]})
    ledger = Ledger()
    for m, s in spent_in(d / "full.jsonl").items():
        ledger.add_spent(m, s)
    print(f"Full run: {runs} runs per prompt, {len(SPEC['prompts']) * len(ID_TYPES) * runs} calls per model.")
    notes = run_jobs({m: full_jobs(m, runs) for m in CONFIG["full_models"]}, d / "full.jsonl",
                     keys, ledger, dry, shares=shares, prices=plan["prices_at_plan"])
    if notes:
        print("\n".join(notes))
    print(f"Full run finished. Spent ${ledger.total():.4f}. Next: python3 analyze.py full" + (" --dry" if dry else ""))


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    args = [a for a in args if a != "--dry"]
    commands = {"check": cmd_check, "step1": cmd_step1, "plan": cmd_plan, "full": cmd_full}
    if len(args) != 1 or args[0] not in commands:
        sys.exit(__doc__)
    commands[args[0]](dry)


if __name__ == "__main__":
    main()
