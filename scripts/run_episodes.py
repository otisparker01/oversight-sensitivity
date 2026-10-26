"""Run episodes for one or more models on a seeded sample of usable tasks.

Usage:
  uv run python scripts/run_episodes.py --models glm-5.2 haiku-4.5 \
      --arms none --n-tasks 20 --first 3 --log-dir logs/step1
  uv run python scripts/run_episodes.py --models deepseek-v4-pro \
      --arms none --n-tasks 90 --message-limit 100 --log-dir logs/diagnostic

--n-tasks picks a seeded sample of the usable tasks (the same sample for every
model); --first/--skip take a slice of it, so a few probe episodes can be run
before the rest, and --first-for MODEL=N gives one model a shorter prefix of the
same sample. API keys are read from .env (see .env.example).
"""

import argparse
import random

from dotenv import load_dotenv
from inspect_ai import eval

from oversight.task import excluded_task_ids, original_tests, oversight_lcb

# OpenRouter: pin the two cheapest fp8 providers so price and quantisation are fixed.
PINNED = {"provider": {"order": ["baidu/fp8", "streamlake/fp8"], "allow_fallbacks": False}}
# Follow-up: after the main run, Baidu's prices rose about sevenfold for GLM 5.2 and
# eightfold for DeepSeek V4 Pro, so the follow-up pins StreamLake (fp8) alone, the
# cheaper of the two.
STREAMLAKE = {"provider": {"order": ["streamlake/fp8"], "allow_fallbacks": False}}

# name: (Inspect model id, model args, generation config). Reasoning settings follow
# ImpossibleBench's replication script (4096 thinking tokens; medium effort).
MODELS = {
    "glm-5.2": ("openrouter/z-ai/glm-5.2", PINNED, {"reasoning_effort": "medium"}),
    "glm-5.2-streamlake": ("openrouter/z-ai/glm-5.2", STREAMLAKE, {"reasoning_effort": "medium"}),
    "deepseek-v4-pro": ("openrouter/deepseek/deepseek-v4-pro", PINNED, {"reasoning_effort": "medium"}),
    "deepseek-v4-pro-streamlake": ("openrouter/deepseek/deepseek-v4-pro", STREAMLAKE, {"reasoning_effort": "medium"}),
    "haiku-4.5": ("anthropic/claude-haiku-4-5", {}, {"reasoning_tokens": 4096}),
    "sonnet-4.5": ("anthropic/claude-sonnet-4-5", {}, {"reasoning_tokens": 4096}),
}


def task_sample(n: int, seed: int, split: str = "oneoff") -> list[str]:
    usable = sorted(set(original_tests(split)) - excluded_task_ids(split), key=lambda t: int(t.split("_")[1]))
    return random.Random(seed).sample(usable, n)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", required=True, choices=list(MODELS))
    p.add_argument("--arms", nargs="+", default=["none"])
    p.add_argument("--n-tasks", type=int, default=20)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--skip", type=int, default=0)
    p.add_argument("--first", type=int, default=None)
    p.add_argument("--first-for", nargs="*", default=[], metavar="MODEL=N",
                   help="per-model --first: that model runs only the first N tasks of the sample")
    p.add_argument("--message-limit", type=int, default=50)
    p.add_argument("--log-dir", required=True)
    p.add_argument("--max-connections", type=int, default=10)
    args = p.parse_args()

    load_dotenv()
    first_for = {k: int(v) for k, v in (s.split("=") for s in args.first_for)}
    sample = task_sample(args.n_tasks, args.seed)

    for name in args.models:
        ids = sample[args.skip:][: first_for.get(name, args.first)]
        print(f"{name} tasks ({len(ids)}): {','.join(ids)}", flush=True)
        model, model_args, config = MODELS[name]
        task = oversight_lcb(arms=args.arms, task_ids=ids, seed=args.seed, message_limit=args.message_limit)
        eval(
            task,
            model=model,
            model_args=model_args,
            log_dir=args.log_dir,
            max_connections=args.max_connections,
            max_sandboxes=10,
            fail_on_error=False,
            # A stalled provider once held a DeepSeek request for ~50 min; give
            # up and retry after 10 min (the slowest normal call was ~2 min).
            attempt_timeout=600,
            **config,
        )


if __name__ == "__main__":
    main()
