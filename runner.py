#!/usr/bin/env python3
"""task-drain runner — a deterministic due-queue drainer for an AI assistant's scheduled work.

Reads pending.jsonl. For every task that is due (today >= not_before) it runs
precondition -> action -> postcondition as scripts, capturing stdout/stderr/exit
of each phase, and logs a structured line to tasks/<slug>/log.jsonl. No LLM in
the *selection* step; LLM judgment enters only for tasks flagged needs_judgment,
which are handed to a judgment hook (TASK_DRAIN_JUDGMENT_CMD) instead of executed.

Idempotent + safe to re-run:
  - Successful autonomous tasks are moved to completed.jsonl (won't re-run).
  - Skipped/failed tasks stay pending; retrying re-checks the precondition.
  - needs_judgment hand-offs are guarded to fire at most once per day per slug.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

# --- paths ------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
PENDING = BASE / "pending.jsonl"
COMPLETED = BASE / "completed.jsonl"
TASKS_DIR = BASE / "tasks"

AUDIT_FINDINGS = Path(os.environ.get("TASK_DRAIN_FINDINGS", BASE / "findings.md"))
ALERTS_DIR = BASE / "alerts"
# Command that hands a needs_judgment task to a human or an agent session. It is run as
# <cmd...> <slug> <prompt>; split on whitespace. Unset = log the task as awaiting judgment only.
JUDGMENT_CMD = os.environ.get("TASK_DRAIN_JUDGMENT_CMD", "").split()

PHASE_TIMEOUT = 300  # seconds per script phase


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def write_jsonl_atomic(path: Path, objs: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for o in objs:
            f.write(json.dumps(o, separators=(",", ":")) + "\n")
    tmp.replace(path)


def run_phase(script: str, slug: str) -> dict:
    """Run a phase script, capturing exit/stdout/stderr. Never raises."""
    if not script:
        return {"exit": None, "stdout": "", "stderr": "", "note": "no script defined"}
    script_path = Path(script)
    if not script_path.is_absolute():
        script_path = (TASKS_DIR / slug / script).resolve()
    if not script_path.exists():
        return {"exit": 127, "stdout": "", "stderr": f"script not found: {script_path}"}
    try:
        # ensure executable bit is respected; invoke via bash for portability
        proc = subprocess.run(
            ["/bin/bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=PHASE_TIMEOUT,
        )
        return {
            "exit": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as e:
        return {"exit": 124, "stdout": (e.stdout or "")[-4000:] if isinstance(e.stdout, str) else "",
                "stderr": f"timeout after {PHASE_TIMEOUT}s"}
    except Exception as e:  # pragma: no cover - defensive
        return {"exit": 1, "stdout": "", "stderr": f"runner exception: {e!r}"}


def log_task(slug: str, entry: dict) -> None:
    d = TASKS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    append_jsonl(d / "log.jsonl", entry)


def already_handed_off_today(slug: str, today: str) -> bool:
    logf = TASKS_DIR / slug / "log.jsonl"
    if not logf.exists():
        return False
    for line in logf.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("outcome") == "judgment_handoff" and e.get("ts", "").startswith(today):
            return True
    return False


def hand_off(task: dict) -> dict:
    slug = task["slug"]
    desc = task.get("description", slug)
    prompt = (
        f"task-drain: due task '{slug}' needs your judgment. {desc} "
        f"Definition + scripts live in {TASKS_DIR / slug}. "
        f"Review, decide, and either run its action yourself or reschedule/close it."
    )
    if not JUDGMENT_CMD:
        return {"exit": 0, "stdout": "no TASK_DRAIN_JUDGMENT_CMD set; logged only", "stderr": ""}
    try:
        proc = subprocess.run(
            [*JUDGMENT_CMD, slug, prompt],
            capture_output=True, text=True, timeout=30,
        )
        return {"exit": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}
    except Exception as e:  # pragma: no cover
        return {"exit": 1, "stderr": f"hand-off exception: {e!r}"}


def append_audit_note(task: dict, phases: dict) -> None:
    AUDIT_FINDINGS.parent.mkdir(parents=True, exist_ok=True)
    slug = task["slug"]
    header = "# task-drain findings\n\nAppended by runner.py when an `audit`-escalation task fails.\n"
    if not AUDIT_FINDINGS.exists():
        AUDIT_FINDINGS.write_text(header)
    lines = [f"\n## {now_utc()} — {slug}\n",
             f"- description: {task.get('description','')}\n"]
    for name in ("precondition", "action", "postcondition"):
        p = phases.get(name)
        if p is None:
            continue
        lines.append(f"- {name}: exit={p.get('exit')} "
                     f"stderr={(p.get('stderr','') or '').strip()[:300]!r}\n")
    with AUDIT_FINDINGS.open("a") as f:
        f.writelines(lines)


def write_alert_marker(task: dict, phases: dict) -> Path:
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = task["slug"]
    marker = ALERTS_DIR / f"ALERT-{slug}.json"
    marker.write_text(json.dumps({
        "ts": now_utc(),
        "slug": slug,
        "description": task.get("description", ""),
        "escalation": "immediate",
        "phases": phases,
    }, indent=2))
    return marker


def process_task(task: dict, today: str, log) -> bool:
    """Return True if the task should be REMOVED from pending (completed)."""
    slug = task["slug"]
    rules = task.get("rules", {})
    not_before = rules.get("not_before", "1970-01-01")

    # Date gate — skip SILENTLY if not yet due.
    if today < not_before:
        return False

    needs_judgment = bool(rules.get("needs_judgment", False))
    escalation = rules.get("escalation_type", "reschedule")
    phases: dict = {}

    # --- precondition ---
    pre = run_phase(task.get("precondition", ""), slug)
    phases["precondition"] = pre
    if pre.get("exit") != 0:
        log(slug, {"ts": now_utc(), "slug": slug, "outcome": "precondition_failed",
                   "phases": phases})
        print(f"[skip] {slug}: precondition exit={pre.get('exit')}")
        return False

    # --- judgment gate: do NOT execute, hand the task off ---
    if needs_judgment:
        if already_handed_off_today(slug, today):
            print(f"[handoff-skip] {slug}: already handed off today")
            return False
        handoff = hand_off(task)
        phases["handoff"] = handoff
        log(slug, {"ts": now_utc(), "slug": slug, "outcome": "judgment_handoff",
                   "phases": phases})
        print(f"[handoff] {slug}: handed off (exit={handoff.get('exit')})")
        return False

    # --- autonomous: action ---
    act = run_phase(task.get("action", ""), slug)
    phases["action"] = act
    if act.get("exit") != 0:
        return handle_failure(task, phases, escalation, "action_failed", log)

    # --- postcondition PROVES the effect ---
    post = run_phase(task.get("postcondition", ""), slug)
    phases["postcondition"] = post
    if post.get("exit") != 0:
        return handle_failure(task, phases, escalation, "postcondition_failed", log)

    log(slug, {"ts": now_utc(), "slug": slug, "outcome": "success", "phases": phases})
    print(f"[done] {slug}: success")
    return True


def handle_failure(task: dict, phases: dict, escalation: str, outcome: str, log) -> bool:
    slug = task["slug"]
    if escalation == "audit":
        append_audit_note(task, phases)
        log(slug, {"ts": now_utc(), "slug": slug, "outcome": outcome,
                   "escalation": "audit", "phases": phases})
        print(f"[audit] {slug}: {outcome} -> {AUDIT_FINDINGS}")
    elif escalation == "immediate":
        marker = write_alert_marker(task, phases)
        log(slug, {"ts": now_utc(), "slug": slug, "outcome": outcome,
                   "escalation": "immediate", "alert": str(marker), "phases": phases})
        print(f"[ALERT] {slug}: {outcome} -> {marker}", file=sys.stderr)
    else:  # reschedule (default)
        log(slug, {"ts": now_utc(), "slug": slug, "outcome": outcome,
                   "escalation": "reschedule", "phases": phases})
        print(f"[reschedule] {slug}: {outcome} (left pending)")
    return False  # never leaves pending on failure


def main() -> int:
    ap = argparse.ArgumentParser(description="Drain the task queue and execute due tasks.")
    ap.add_argument("--today", help="override date (YYYY-MM-DD) for testing", default=None)
    args = ap.parse_args()

    # Fixed clock source: compute once.
    today = args.today or dt.date.today().isoformat()

    tasks = read_jsonl(PENDING)
    if not tasks:
        print(f"[task-drain] {now_utc()} today={today}: no pending tasks")
        return 0

    print(f"[task-drain] {now_utc()} today={today}: {len(tasks)} pending")
    remaining: list[dict] = []
    completed_now: list[dict] = []
    for task in tasks:
        try:
            done = process_task(task, today, log_task)
        except Exception as e:  # defensive: one bad task never kills the drain
            slug = task.get("slug", "?")
            log_task(slug, {"ts": now_utc(), "slug": slug, "outcome": "runner_error",
                            "error": repr(e)})
            print(f"[error] {slug}: {e!r}", file=sys.stderr)
            done = False
        if done:
            rec = dict(task)
            rec["completed_at"] = now_utc()
            completed_now.append(rec)
        else:
            remaining.append(task)

    # Persist: rewrite pending with survivors, append completions.
    write_jsonl_atomic(PENDING, remaining)
    for rec in completed_now:
        append_jsonl(COMPLETED, rec)

    print(f"[task-drain] done: {len(completed_now)} completed, {len(remaining)} still pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
