"""Local runner for all 8 LA/OC utility scrapers (replaces GitHub scrape-all.yml).

Scheduled by Windows Task Scheduler every 15 min via .venv\\Scripts\\pythonw.exe
(no console window), see setup_local_task.ps1.

- Paths are relative to this file; children run with the repo's own .venv python.
  No user-account-specific paths.
- Each utility runs in its own process with a timeout: one failure or hang never
  stops the others.
- One SUCCESS/FAILURE line per utility per run is appended to run_log.txt;
  check_health.ps1 reads those lines.
- Commands mirror scrape-all.yml, so outputs land in the same CSVs, including the
  always-written heartbeat/summary/envelope rows for Glendale, Azusa, Burbank and
  Pasadena.
"""
import datetime as dt
import os
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "run_log.txt"
OUT_DIR = ROOT / "logs"
TIMEOUT = 300  # seconds per scraper process

# Utility name -> list of commands (all must exit 0 for SUCCESS)
JOBS = {
    "SCE": [
        ["sce_oc_outages.py", "--counties", "ORANGE,LOS ANGELES",
         "--out", "la_oc_outages_latest.csv", "--history", "la_oc_outages_history.csv"],
        ["sce_oc_psps.py", "--counties", "ORANGE,LOS ANGELES",
         "--out", "la_oc_psps_latest.csv", "--history", "la_oc_psps_history.csv"],
    ],
    "SDGE": [["sdge/sdge_outages.py"]],
    "LADWP": [["ladwp/ladwp_outages.py", "--region", "la"]],
    "GLENDALE": [["datavoice_outages.py", "--client", "glendale"]],
    "AZUSA": [["datavoice_outages.py", "--client", "azusa"]],
    "BURBANK": [["burbank/burbank_outages.py"]],
    "PASADENA": [["pasadena/pasadena_power.py"]],
    "ANAHEIM": [["anaheim/anaheim_outages.py"]],
}

ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_one(cmd, out_file):
    with open(out_file, "w", encoding="utf-8") as fh:
        try:
            p = subprocess.run([str(PY), *cmd], cwd=ROOT, env=ENV, stdout=fh,
                               stderr=subprocess.STDOUT, timeout=TIMEOUT,
                               creationflags=NO_WINDOW)
            return str(p.returncode)
        except subprocess.TimeoutExpired:
            fh.write(f"\nTIMEOUT after {TIMEOUT}s\n")
            return "timeout"
        except Exception as e:  # launch failure must not stop the other utilities
            fh.write(f"\nLAUNCH ERROR: {e!r}\n")
            return "launch-error"


def main():
    OUT_DIR.mkdir(exist_ok=True)
    for name, cmds in JOBS.items():
        ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        t0 = time.monotonic()
        rcs = [run_one(cmd, OUT_DIR / f"_last_{name.lower()}_{i}.out")
               for i, cmd in enumerate(cmds, 1)]
        ok = all(rc == "0" for rc in rcs)
        line = (f"{ts}  {'SUCCESS' if ok else 'FAILURE'}  [{name}]  "
                f"rc={','.join(rcs)}  {time.monotonic() - t0:.0f}s")
        if not ok:
            line += f"  see logs/_last_{name.lower()}_*.out"
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


if __name__ == "__main__":
    main()
