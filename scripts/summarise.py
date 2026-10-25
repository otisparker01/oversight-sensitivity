"""Print per-model (and per-arm) tables for a log directory.

Usage: uv run python scripts/summarise.py logs/step1 [--by model arm]
"""

import argparse

import pandas as pd

from oversight.analysis import add_judgements, load_episodes, summarise

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("log_dir")
    p.add_argument("--by", nargs="+", default=["model"])
    p.add_argument("--csv", default=None)
    args = p.parse_args()

    df = add_judgements(load_episodes(args.log_dir))
    pd.set_option("display.width", 200, "display.max_columns", 30)
    print(summarise(df, args.by).round(3))
    errors = df[df["error"].notna()]
    if len(errors):
        print(f"\n{len(errors)} errored episodes:\n", errors[["model", "task_id", "error"]])
    if args.csv:
        df.to_csv(args.csv, index=False)
