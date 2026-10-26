"""Record the fingerprint (SHA-256) of every frozen file in FROZEN.sha256 (DESIGN.md section 9).

Run once, then save (commit) FROZEN.sha256 with the files, before the first paid call.
run.py refuses to make paid calls if any frozen file differs from its fingerprint or is
not saved in git. A change after the freeze is logged in CHANGES.md before re-freezing.
"""
import datetime
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run import FROZEN  # noqa: E402

lines = [f"# Frozen {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')} (UTC)"]
for name in FROZEN:
    lines.append(f"{hashlib.sha256((HERE / name).read_bytes()).hexdigest()}  {name}")
(HERE / "FROZEN.sha256").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
