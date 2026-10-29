"""The README's figure: main-run cheating rate by arm, with 95% Wilson intervals.

Reads only data/main_episodes.csv, so it needs no logs or API keys.

Usage: uv run python scripts/plot_cheating_by_arm.py
"""

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from oversight.analysis import CHEATING, wilson

ARMS = [("none", "No line"), ("r00", "0%"), ("r10", "10%"), ("r50", "50%"), ("r100", "100%"),
        ("none_neutral", "No line")]

SURFACE, INK, MUTED, GRID, AXIS, SERIES = "#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#2a78d6"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="data/main_episodes.csv")
    p.add_argument("--out", default="docs/figures/cheating_by_arm.png")
    args = p.parse_args()

    df = pd.read_csv(args.csv)
    df["cheat"] = df["method"].isin(CHEATING)
    g = df.groupby("arm")["cheat"].agg(["sum", "count"])

    # A gap before the neutral-message arm, which tests the mechanism rather than the review rate.
    xs = [0, 1, 2, 3, 4, 5.6]
    fig, ax = plt.subplots(figsize=(7.2, 3.8), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for x, (arm, _) in zip(xs, ARMS):
        k, n = int(g.loc[arm, "sum"]), int(g.loc[arm, "count"])
        lo, hi = wilson(k, n)
        ax.plot([x, x], [100 * lo, 100 * hi], color=SERIES, lw=2, solid_capstyle="round", zorder=2)
        ax.plot(x, 100 * k / n, "o", ms=8, color=SERIES, mec=SURFACE, mew=2, zorder=3)
        ax.annotate(f"{k}/{n}", (x, 100 * hi), xytext=(0, 5), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8.5, color=MUTED)

    ax.set_xticks(xs, [label for _, label in ARMS], fontsize=9, color=INK)
    ax.set_xlim(-0.6, 6.2)
    ax.set_ylim(-1, 41)
    ax.set_yticks(range(0, 41, 10), [f"{v}%" for v in range(0, 41, 10)], fontsize=9, color=MUTED)
    ax.set_ylabel("Episodes that cheated", fontsize=9, color=MUTED)
    ax.set_xlabel("Review rate stated in the task", fontsize=9, color=MUTED, labelpad=8)
    ax.grid(axis="y", color=GRID, lw=1)
    ax.set_axisbelow(True)
    ax.tick_params(length=0, pad=6)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.axvline(4.8, color=AXIS, lw=1)
    for x, header in [(2, "Default continuation message"), (5.6, "Neutral message")]:
        ax.text(x, 38, header, ha="center", va="center", fontsize=9, color=INK)
    ax.set_title("Main run, DeepSeek V4 Pro: cheating by arm (95% Wilson intervals)",
                 fontsize=10, color=INK, loc="left", pad=10)

    fig.tight_layout()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, facecolor=SURFACE, metadata={"Software": None})
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
