"""Judge every episode in one or more log directories through the Batch API
(skips episodes the current judge model has already judged), wait for the
batch, and append the verdicts to labels/judgements.jsonl.

Usage: uv run python scripts/judge.py logs/step1
       uv run python scripts/judge.py --collect <batch_id>   # resume an earlier batch
"""

import argparse

from dotenv import load_dotenv
from inspect_ai.log import list_eval_logs

from oversight.judges import collect, submit

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("log_dirs", nargs="*")
    p.add_argument("--collect", default=None)
    args = p.parse_args()
    load_dotenv(".env")

    batch_id = args.collect or submit([info.name for d in args.log_dirs for info in list_eval_logs(d)])
    if batch_id is None:
        print("nothing to judge")
    else:
        print(f"batch {batch_id}")
        print(collect(batch_id))
