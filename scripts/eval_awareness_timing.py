"""Exploratory, not pre-registered: for each main-run episode the judge marks as
showing evaluation awareness, was the awareness first voiced before or after the
first authorising exchange (a flag written as text, answered by the default
continuation message)?

The position used is the first message containing the judge's quote. The judge
quotes one passage, which need not be the first time the agent voiced awareness,
so "after" can hide an earlier, unquoted mention; "before" cannot be wrong that way.

It also prints, by arm, how many assistant turns followed the first answered flag
(either continuation message), since episodes that end soon after the reply have
little room to voice awareness late.

Usage: uv run python scripts/eval_awareness_timing.py logs/main
"""

import argparse
import re
from collections import Counter

import pandas as pd
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_ai.model import ContentReasoning

from oversight.analysis import CONTINUE_MESSAGES, FLAG, add_judgements, load_episodes
from oversight.judges import _render_message
from oversight.task import DEFAULT_CONTINUE

ARMS = ["none", "r00", "r10", "r50", "r100", "none_neutral"]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def message_text(m) -> str:
    """One message as the judge saw it (the judge's own rendering), plus the raw
    text and arguments, since quotes of tool calls may drop the JSON escaping."""
    raw = [m.text or ""] + [v for c in getattr(m, "tool_calls", None) or [] for v in map(str, c.arguments.values())]
    if not isinstance(m.content, str):
        raw += [c.reasoning or c.summary or "" for c in m.content if isinstance(c, ContentReasoning)]
    return norm(" ".join(_render_message(m) + raw))


def first_exchange(messages, replies=(DEFAULT_CONTINUE,)) -> int | None:
    """Index of the first continuation message (by default, the default one) that
    answers a text flag."""
    for i, (prev, m) in enumerate(zip(messages, messages[1:]), 1):
        if (prev.role == "assistant" and not prev.tool_calls and FLAG in (prev.text or "")
                and m.role == "user" and m.text in replies):
            return i
    return None


def quote_position(messages, quote: str) -> int | None:
    """Index of the first message containing the quote (or, if the quote was
    shortened with an ellipsis, its longest piece)."""
    pieces = sorted((norm(p) for p in re.split(r"\.\.\.|…|\n", quote)), key=len, reverse=True)
    texts = [message_text(m) for m in messages]
    for needle in [norm(quote), *pieces[:1], *(pieces[0][k:k + 40] for k in (0, len(pieces[0]) // 2) if pieces)]:
        if len(needle) >= 15:
            hit = next((i for i, t in enumerate(texts) if needle in t), None)
            if hit is not None:
                return hit
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("log_dir")
    args = p.parse_args()

    df = add_judgements(load_episodes(args.log_dir))
    aware = df[df["j_mentions_eval_awareness"] == True]  # noqa: E712
    samples = {s.uuid: s for info in list_eval_logs(args.log_dir) for s in read_eval_log(info).samples or []}
    rows = []
    for _, r in aware.iterrows():
        msgs = samples[r["sample_uuid"]].messages
        ex, q = first_exchange(msgs), quote_position(msgs, r["j_mentions_eval_awareness_quote"] or "")
        when = ("no exchange" if ex is None else "quote not located" if q is None
                else "before the exchange" if q < ex else "after the exchange")
        rows.append({"arm": r["arm"], "task_id": r["task_id"], "cheat": r["method"] in ("test_edit", "special_casing", "other"),
                     "when": when})
    out = pd.DataFrame(rows)
    print(f"{len(out)} eval-aware episodes (judge)\n")
    print(pd.crosstab(out["arm"], out["when"], margins=True).reindex([*[a for a in ARMS if a in set(out["arm"])], "All"]))
    print("\nby whether the episode cheated:\n", pd.crosstab(out["cheat"], out["when"], margins=True))
    print("\nwhen, overall:", dict(Counter(out["when"])))

    arms = dict(zip(df["sample_uuid"], df["arm"]))
    after = pd.DataFrame([{"arm": arms[u], "turns": sum(m.role == "assistant" for m in s.messages[ex + 1:])}
                          for u, s in samples.items()
                          if u in arms and (ex := first_exchange(s.messages, CONTINUE_MESSAGES)) is not None])
    print("\nassistant turns after the first answered flag (either message), episodes with one:\n",
          after.groupby("arm")["turns"].describe()[["count", "50%", "min", "max"]].reindex(ARMS))


if __name__ == "__main__":
    main()
