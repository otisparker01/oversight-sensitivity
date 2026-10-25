"""Judge every episode in a log directory (skips episodes already judged).

Usage: uv run python scripts/judge.py logs/step1
"""

import asyncio
import sys

from dotenv import load_dotenv
from inspect_ai.log import list_eval_logs

from oversight.judges import judge_logs

if __name__ == "__main__":
    load_dotenv()
    paths = [info.name for d in sys.argv[1:] for info in list_eval_logs(d)]
    print(asyncio.run(judge_logs(paths)))
