#!/usr/bin/env python3
"""Figure 1 of the LazyCode paper: the economics condition.

Input-token cost of one task, in units of its shared context C, as a function of
the number of batch rounds R.

    cached interactive (T turns)        : 0.1 * T                (flat in R)
    uncached batch                      : 0.5 * R
    batch + stacked prompt cache, width N: R * (0.05 + 0.95 / N)

Crossovers: uncached batch beats the cached baseline iff R < T/5.  With discount
stacking across a wave of N prefix-sharing items the crossover moves out to
R < 0.1*T / (0.05 + 0.95/N), which tends to 2T as N grows.

Usage (from docs/paper/latex):
    python scripts/fig_economics.py         # -> figures/economics.pdf
"""

from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

# ---------------------------------------------------------------- style ----
ACCENT = "#1B7878"  # RGB(27,120,120) -- matches \definecolor{accent}
DIMMED = "#787878"  # RGB(120,120,120)
BASE = "#2B2B2B"
WARM = "#A8442A"

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 8.5,
        "axes.labelsize": 9,
        "axes.titlesize": 9,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 7.6,
        "axes.linewidth": 0.7,
        "xtick.major.width": 0.7,
        "ytick.major.width": 0.7,
        "pdf.fonttype": 42,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
    }
)


# ------------------------------------------------------------- formulas ----
def cached_interactive(T: int) -> float:
    """Input cost of a prompt-cached interactive agent over T turns, in C."""
    return 0.1 * T


def batch_uncached(R: np.ndarray) -> np.ndarray:
    """Input cost of R cold-cache batch rounds, in C."""
    return 0.5 * R


def batch_stacked(R: np.ndarray, N: int) -> np.ndarray:
    """Per-item input cost of R batch rounds across waves of width N, in C."""
    return R * (0.05 + 0.95 / N)


def crossover(T: int, N: int | None = None) -> float:
    """R at which batch stops beating the cached interactive baseline."""
    slope = 0.5 if N is None else (0.05 + 0.95 / N)
    return 0.1 * T / slope


# ----------------------------------------------------------------- plot ----
def main() -> None:
    here = pathlib.Path(__file__).resolve().parent.parent
    out = here / "figures" / "economics.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)

    R = np.linspace(0, 12, 400)
    N = 20

    fig, ax = plt.subplots(figsize=(6.1, 3.25))

    x15, x30 = crossover(15), crossover(30)  # 3.0 and 6.0

    # "batch wins" region: left of the T=30 crossover, under that baseline.
    ax.axvspan(0, x30, color=ACCENT, alpha=0.055, lw=0, zorder=0)
    ax.axvspan(0, x15, color=ACCENT, alpha=0.075, lw=0, zorder=0)
    ax.text(
        1.45,
        2.72,
        "batch wins\non input tokens",
        ha="center",
        va="top",
        fontsize=7.6,
        color=ACCENT,
        linespacing=1.35,
    )

    # cached interactive baselines (flat in R)
    for T, y, ls in ((30, cached_interactive(30), "-"), (15, cached_interactive(15), "-")):
        ax.axhline(y, color=BASE, lw=1.15, ls=ls, zorder=3)
        ax.text(
            12.05,
            y,
            f"cached interactive, $T{{=}}{T}$   $({y:.1f}\\,C)$",
            va="center",
            ha="left",
            fontsize=7.4,
            color=BASE,
        )

    # batch, cold caches
    ax.plot(R, batch_uncached(R), color=WARM, lw=1.6, zorder=4)
    ax.annotate(
        "batch, cold cache\n$0.5\\,C$ per round",
        xy=(5.4, 2.70), xytext=(2.55, 3.62),
        fontsize=7.6, color=WARM, ha="left", va="center", linespacing=1.35,
        arrowprops=dict(arrowstyle="-", color=WARM, lw=0.6, shrinkA=2, shrinkB=2),
    )

    # batch, stacked discounts across a wave of N items
    ax.plot(R, batch_stacked(R, N), color=ACCENT, lw=1.7, ls=(0, (5, 2.2)), zorder=4)
    ax.text(
        6.18,
        0.08,
        f"batch + stacked cache, $N{{=}}{N}$: ${0.05 + 0.95 / N:.4f}\\,C$ per round\n"
        f"crossover at $R \\approx T$ (off-chart), $\\to 2T$ as $N \\to \\infty$",
        fontsize=7.6,
        color=ACCENT,
        ha="left",
        va="bottom",
        linespacing=1.35,
    )

    # crossover markers
    for T, xc in ((15, x15), (30, x30)):
        ax.plot([xc, xc], [0, cached_interactive(T)], color=DIMMED, lw=0.8,
                ls=(0, (2, 2)), zorder=2)
        ax.plot([xc], [cached_interactive(T)], "o", ms=4.2, color=BASE,
                mfc="white", mew=1.1, zorder=6)
        ax.text(
            xc + 0.14,
            cached_interactive(T) - 0.14,
            f"$R = T/5 = {xc:.0f}$",
            fontsize=7.4,
            color=DIMMED,
            ha="left",
            va="top",
        )

    ax.set_xlabel("batch rounds $R$ (DAG depth in remote waves)")
    ax.set_ylabel("input-token cost  [units of shared context $C$]")
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 4.2)
    ax.set_xticks(range(0, 13, 2))
    ax.set_yticks([0, 1, 2, 3, 4])
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#CCCCCC", lw=0.45, alpha=0.6, zorder=0)
    ax.set_axisbelow(True)

    fig.savefig(out)
    print(f"wrote {out}")
    print(f"  crossovers: T=15 -> R={x15:.2f}, T=30 -> R={x30:.2f}, "
          f"N={N} vs T=15 -> R={crossover(15, N):.2f}")


if __name__ == "__main__":
    main()
