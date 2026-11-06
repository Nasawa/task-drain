# task-drain

A tiny, deterministic due-queue for scheduled work that an AI assistant (or a person) has
already approved: **"do X on or after date Y, but only if Z is true, and prove it worked."**

It exists because asking an LLM to *pick* what to do from a backlog is fuzzy. A nightly
"look at the backlog and choose something" job will sometimes skip an item that was
explicitly approved, because the model found a cleaner target. task-drain splits the two
needs: no model is involved in **selection**. Every task that is due and whose precondition
passes runs, every time. Judgment only comes in for tasks you explicitly flag as needing it,
and those are handed off rather than executed.

One Python file, standard library only, driven by a systemd user timer.

## How a task works

Each task is three small scripts plus one line of JSON:

```
tasks/<slug>/
  precondition.sh    # exit 0 => the world is ready
  action.sh          # does the work
  postcondition.sh   # exit 0 => PROVES the effect landed (success)
  log.jsonl          # written by the runner: one structured line per run
```

```json
{"slug": "unique-id",
 "description": "human summary",
 "precondition": "precondition.sh", "action": "action.sh", "postcondition": "postcondition.sh",
 "rules": {"not_before": "YYYY-MM-DD", "needs_judgment": false,
           "escalation_type": "reschedule|audit|immediate"},
 "approved_by": "who-and-when", "created_at": "YYYY-MM-DD"}
```

Script paths are relative to `tasks/<slug>/` (absolute paths work too). Each runs under
`/bin/bash` with a 300 s timeout; the runner captures exit code, stdout and stderr.

## What a run does

For each line in `pending.jsonl` where `today >= not_before` (earlier tasks are skipped
silently):

1. **precondition**: non-zero means skip, stay pending, log `precondition_failed`.
2. **`needs_judgment: true`**: don't execute. Run `TASK_DRAIN_JUDGMENT_CMD <slug> <prompt>`
   (at most once per task per day) so a person or an agent session can decide. With the
   variable unset, the task is only logged as awaiting judgment.
3. **Otherwise**: run **action**, then **postcondition**.
   - postcondition exits 0: the task moves to `completed.jsonl`, logged `success`.
   - action or postcondition fails: handled by `escalation_type`:
     - `reschedule`: stay pending and try again next run;
     - `audit`: stay pending and append a note to `findings.md` (or `TASK_DRAIN_FINDINGS`);
     - `immediate`: also write `alerts/ALERT-<slug>.json` and log loudly to stderr.

Runs are idempotent and safe to repeat: the clock is read once per run, successes leave the
queue, and anything skipped or failed is re-checked next time. `--today YYYY-MM-DD` overrides
the date for testing.

The postcondition is the point. A task is not "done" because its action exited 0; it is done
when a separate check proves the effect is real.

## Try it

```bash
git clone https://github.com/Nasawa/task-drain ~/task-drain && cd ~/task-drain
cp examples/pending.jsonl . && cp -r examples/tasks .
python3 runner.py            # runs the example task, moves it to completed.jsonl
cat tasks/hello-after-date/log.jsonl
```

## Run it nightly

```bash
mkdir -p ~/.config/systemd/user
cp systemd/task-drain.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now task-drain.timer
```

The timer fires at 04:20 local with `Persistent=true`, so a missed run catches up at the next
boot. Output goes to the journal (`journalctl --user -u task-drain`).

## Wiring up judgment

`TASK_DRAIN_JUDGMENT_CMD` is split on whitespace and called with the task's slug and a prompt
as two extra arguments. Point it at whatever reaches the decider: a notification script, a
chat bot, or something that injects the prompt into a running agent session. It is the only
place a model gets involved, and only for tasks you flagged.

## Status

This was built for one homelab assistant and is published as-is because the shape is useful,
not because it is a product. In practice our own assistant ended up using its live-session
wake mechanism for most "do X later" work, so this queue sees little traffic. If you have
scheduled work that must not depend on a model choosing it, it may suit you well.

MIT licensed.
