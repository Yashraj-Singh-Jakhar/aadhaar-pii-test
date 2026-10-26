"""Make the made-up ID numbers (DESIGN.md section 5) and save them to fakes.json.

Every fake breaks a published rule, so, on published sources, none is a real number.
The list is made once from a fixed seed, so anyone can make the same list again.
The script checks every fake and refuses to save a list that fails.

Run:  python3 -I make_fakes.py          (writes fakes.json)
      python3 -I make_fakes.py --check  (remakes the list and checks it matches fakes.json)
"""
import hashlib
import json
import random
import re
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import has_part, plain, unify  # noqa: E402

SEED = 20261009
COUNT = 6
HERE = Path(__file__).resolve().parent

# PAN 4th letter (holder type): official list (OECD India TIN sheet, p. 2) and
# two letters added by third-party sites. The fakes use none of these.
PAN_HOLDER_LETTERS = set("PFCHATBLJG") | set("EK")
PAN_FOURTH = "Q"
PAN_FIFTH = "P"  # first letter of the surname Patel

# SHA-256 fingerprints of the plain form of example numbers printed in the sources,
# so the numbers themselves are not copied here:
# - Kanakia, Nadhamuni and Sarma, "A UID Numbering Scheme" (2010), PDF p. 9, "ID Mapper"
#   image: an Aadhaar-format example and a 9-digit bank routing example.
# - OECD, "India: Information on Tax Identification Numbers", p. 2: a sample PAN.
EXAMPLE_FINGERPRINTS = {
    "84f4db2b12ddb31cb9ba8e9b87f070198f28e62b02231afafdaca579edaab6a1",
    "e255da2d2ece8314cc59929ed0bb38a892abc5b5fc442c51bcb6b2bf11087ed8",
    "7be0a2be12b79bb72924f1d886a634abc261bc305593cfe9c204fbac1c7a2663",
}
BIRTH_DATE_GROUPS = ("1991", "0314", "1403")

# Verhoeff check-digit tables (the published formula).
_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_valid(digits):
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(digits):
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


def make_ssn(rng):
    group = rng.randint(1, 99)
    serial = rng.randint(1, 9999)
    return f"000-{group:02d}-{serial:04d}"


def make_aadhaar(rng):
    body = str(rng.randint(2, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(10))
    good = verhoeff_check_digit(body)
    last = rng.choice([d for d in string.digits if d != good])
    n = body + last
    return f"{n[0:4]} {n[4:8]} {n[8:12]}"


def make_pan(rng):
    first3 = "".join(rng.choice(string.ascii_uppercase) for _ in range(3))
    number = rng.randint(1, 9999)
    last = rng.choice(string.ascii_uppercase)
    return f"{first3}{PAN_FOURTH}{PAN_FIFTH}{number:04d}{last}"


def make_neutral(rng):
    n = str(rng.randint(0, 1)) + "".join(str(rng.randint(0, 9)) for _ in range(11))
    return f"{n[0:4]} {n[4:8]} {n[8:12]}"


MAKERS = {"ssn": make_ssn, "aadhaar": make_aadhaar, "pan": make_pan, "neutral": make_neutral}


def rule_problems(id_type, fake):
    """Return a list of reasons this fake fails its published-rule test (empty if fine)."""
    p = plain(fake)
    problems = []
    if id_type == "ssn":
        m = re.fullmatch(r"(\d{3})-(\d{2})-(\d{4})", fake)
        if not m:
            problems.append("SSN layout")
        elif m.group(1) != "000" or m.group(2) == "00" or m.group(3) == "0000":
            problems.append("SSN must have area 000, group not 00, serial not 0000")
    elif id_type == "aadhaar":
        if not re.fullmatch(r"[2-9]\d{3} \d{4} \d{4}", fake):
            problems.append("Aadhaar layout or first digit")
        elif verhoeff_valid(p):
            problems.append("Aadhaar passes the Verhoeff check; it must fail")
    elif id_type == "pan":
        if not re.fullmatch(r"[A-Z]{3}[A-Z]P\d{4}[A-Z]", fake):
            problems.append("PAN layout")
        elif fake[3] in PAN_HOLDER_LETTERS:
            problems.append("PAN 4th letter is a listed holder type")
        elif fake[5:9] == "0000":
            problems.append("PAN number part must be 0001 to 9999")
    elif id_type == "neutral":
        if not re.fullmatch(r"[01]\d{3} \d{4} \d{4}", fake):
            problems.append("neutral layout or first digit")
    digits = re.sub(r"[^0-9]", "", p)
    for g in BIRTH_DATE_GROUPS:
        if g in digits:
            problems.append(f"contains the birth-date group {g}")
    if hashlib.sha256(p.encode()).hexdigest() in EXAMPLE_FINGERPRINTS:
        problems.append("matches an example number printed in a source")
    return problems


def prompt_problems(fake, prompts, id_types, id_type):
    """The fake's first or last 4 characters must not already appear in the prompt text."""
    label = id_types[id_type]["label"]
    found = []
    for pr in prompts:
        text = pr["text"].replace("{LABEL}", label).replace("{NUMBER}", "")
        if has_part(unify(text), plain(fake)):
            found.append(pr["id"])
    return [f"first or last 4 characters appear in prompt {', '.join(found)}"] if found else []


def build():
    spec = json.loads((HERE / "prompts.json").read_text())
    rng = random.Random(SEED)
    fakes = {}
    for id_type, maker in MAKERS.items():
        chosen = []
        tries = 0
        while len(chosen) < COUNT:
            tries += 1
            if tries > 10000:
                sys.exit(f"Could not make {COUNT} good {id_type} fakes.")
            f = maker(rng)
            if f in chosen:
                continue
            if rule_problems(id_type, f) or prompt_problems(f, spec["prompts"], spec["id_types"], id_type):
                continue
            chosen.append(f)
        fakes[id_type] = chosen
    return fakes


def check_all(fakes):
    spec = json.loads((HERE / "prompts.json").read_text())
    problems = []
    every = [plain(f) for fs in fakes.values() for f in fs]
    if len(set(every)) != len(every):
        problems.append("two fakes are the same")
    for id_type, fs in fakes.items():
        if len(fs) != COUNT:
            problems.append(f"{id_type}: {len(fs)} fakes, not {COUNT}")
        for f in fs:
            for p in rule_problems(id_type, f) + prompt_problems(f, spec["prompts"], spec["id_types"], id_type):
                problems.append(f"{id_type} fake {fs.index(f) + 1}: {p}")
    return problems


def main():
    fakes = build()
    problems = check_all(fakes)
    if problems:
        sys.exit("Refusing to save:\n" + "\n".join(problems))
    out = {"_note": "Made by make_fakes.py (DESIGN.md section 5). Every number is made up and breaks a published rule. Run i of a prompt uses fake i.",
           "seed": SEED, "fakes": fakes}
    path = HERE / "fakes.json"
    if "--check" in sys.argv:
        saved = json.loads(path.read_text())
        if saved.get("fakes") != fakes or saved.get("seed") != SEED:
            sys.exit("fakes.json does NOT match a fresh run of this script.")
        print("fakes.json matches a fresh run; all checks pass.")
        return
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Saved {sum(len(v) for v in fakes.values())} fakes to fakes.json; all checks pass.")


if __name__ == "__main__":
    main()
