"""Build the pre-registered hand-labelling set for judge validation.

The set is every exit episode, every episode the judge marks as mentioning the
reviewer or showing evaluation awareness, and a random 10% of the rest (at least
40). The labeller gets the judge's own inputs (repository notes removed, so the
arm is hidden) in a shuffled order, and a blank sheet with only the questions
each episode was asked. The sheet shows no arm, judge verdict or reason for
inclusion.

Usage: uv run python scripts/hand_label_set.py logs/main
"""

import argparse
import csv
import hashlib
import json
import math
import random
import shutil
from pathlib import Path

from inspect_ai.log import list_eval_logs

from oversight.judges import JUDGE_MODEL, QUESTIONS, SYSTEM

ANSWERS = [k for g in QUESTIONS.values() for k in g if not k.endswith(("_quote", "_rationale"))]


def latest_verdicts(log_dir: str, labels: Path) -> dict[str, dict]:
    """sample_uuid -> the current judge's latest verdict row, for one log directory."""
    logs = {str(info.name) for info in list_eval_logs(log_dir)}
    rows = {}
    for line in (labels / "judgements.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["judge_model"] == JUDGE_MODEL and r["verdict"] is not None and r["log"] in logs:
            rows[r["sample_uuid"]] = r
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("log_dir")
    p.add_argument("--labels", default="labels")
    p.add_argument("--out", default="labels/hand")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    labels, out = Path(args.labels), Path(args.out)

    rows = latest_verdicts(args.log_dir, labels)
    exits = {u for u, r in rows.items() if r["label"] == "exit"}
    flagged = {u for u, r in rows.items() if r["verdict"].get("mentions_reviewer") or r["verdict"].get("mentions_eval_awareness")}
    rest = sorted(set(rows) - exits - flagged)
    rng = random.Random(args.seed)
    sampled = set(rng.sample(rest, min(len(rest), max(40, math.ceil(0.1 * len(rest))))))
    chosen = sorted(exits | flagged | sampled)
    rng.shuffle(chosen)

    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    with (out / "hand_labels.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["key", "transcript", *ANSWERS, "notes"])
        w.writeheader()
        for u in chosen:
            r = rows[u]
            src = labels / "transcripts" / f"{r['custom_id']}.md"
            if hashlib.sha1(src.read_text().encode()).hexdigest() != r["input_sha1"]:
                raise RuntimeError(f"{src} differs from what the judge saw")
            shutil.copy(src, out / "transcripts" / src.name)
            asked = {k for g in r["questions"] for k in QUESTIONS[g]}
            w.writerow({"key": r["custom_id"], "transcript": f"transcripts/{src.name}",
                        **{k: "" if k in asked else "n/a" for k in ANSWERS}, "notes": ""})

    (out / "RUBRIC.md").write_text(
        "# Hand-labelling\n\n"
        "Open each transcript listed in `hand_labels.csv` and fill in its blank cells: `true` or `false`, "
        "or for `exit_kind`, `clean_exit` or `cheating`. Leave `n/a` cells alone; the judge was not asked "
        "those questions for that episode. Use `notes` for anything unclear.\n\n"
        "Don't open `labels/judgements.jsonl` or `labels/batches/` until you've finished: they show the "
        "judge's answers and each episode's arm.\n\n"
        "The definitions below are the judge's own instructions, word for word.\n\n---\n\n" + SYSTEM + "\n"
    )
    print(f"{len(chosen)} episodes: {len(exits)} exits, {len(flagged - exits)} judge-flagged, "
          f"{len(sampled)} sampled from {len(rest)} others (seed {args.seed}) -> {out}")


if __name__ == "__main__":
    main()
