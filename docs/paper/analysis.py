"""Recompute every number and figure of the workshop paper (docs/paper/index.md) from the pilot's records.

Inputs, read only:
    results/2026-10-haiku-pilot/results.jsonl.gz   the 251 records of the October 2026 pilot
    results/2026-10-haiku-pilot/gold.jsonl         reference-patch validation of the 70 sampled tasks
    results/2026-10-haiku-pilot/meta.json          run settings and task ids
    results/2026-10-haiku-pilot/artifacts.tar.gz   each final pair's runs.json: the full final messages
                                                   (the records keep the first 1,000 characters)
    docs/paper/labels.csv                          values the published files do not hold, each with source and date

Outputs:
    docs/paper/numbers.json                        every number under a stable key, with its source tag
    docs/paper/figures/*.svg                       Figures 1 and 2 and Appendix Figures A1 to A3

Usage, from the repository root:
    python3 -B docs/paper/analysis.py              recompute, write the outputs, print every number
    python3 -B docs/paper/analysis.py --check      recompute; exit 1 if any output on disk differs

Standard library only; no network; deterministic (two runs give byte-identical outputs).

Definitions
    Final record: the latest record of a (task, arm) pair in file order; it must have `error` null.
    The other records are superseded records. Arms: solo, review, best (best of 3).
    Grading rules: current = `resolved`; first published = `eval.patch_applied and eval.report.resolved`;
    missing-test = current and no listed PASS_TO_PASS test missing from the log; both strict = first
    published and missing-test.
    Differences are team minus solo in percentage points; wins are tasks the team resolves and solo
    does not, losses the reverse.
    Base prices: per run, uncached input x $0.125 + cache reads x $0.01 + output x $0.50 per million
    tokens, uncached input = input_tokens - cached_input_tokens (the vendor's schedule for prompts up
    to 100,000 tokens). As billed: the record's `usage.cost_usd`, Claude Code's own total_cost_usd.
    Task bootstrap: percentile intervals; SEED and BOOT below; one fixed set of resampled indices per
    number of resampled tasks, shared by every statistic on that set (48 tasks; 31 tasks for the
    unflagged AUCs); of the BOOT = 200,000 sorted values the 5,001st and the 195,000th are the 95%
    limits. These limits are Monte Carlo estimates: another seed can move a printed limit.
    Repository bootstrap: exact over all resamples of the 23 repositories (no simulation), with the
    limits those ranks tend to as the resample count grows.
    The McNemar and sign tests, the coverage study and the power are exact enumerations, not
    simulations. The exact unconditional test enumerates its tail probability exactly and maximises it
    over the nuisance parameter numerically (a grid, then golden-section search); its bounds invert the
    test on grids, then by bisection to 1e-7. Tango's score test and the Newcombe and Tango intervals
    are asymptotic.
    Tasks for 80% power: the smallest n, searched upward from 6, whose power is at least 0.80. Smallest
    gain for 80% power: the smallest gain on a 0.1-point grid whose power is at least 0.80.

Rounding rules for numbers.json
    Integers, booleans and strings are stored as they are. Every other number is stored rounded to six
    significant digits. Percentages, shares and differences in points are stored in percent (keys
    ending in `_pct` or `_pts`); p-values, AUCs, kappas, correlations and ratios as plain numbers;
    money in US dollars. An interval is a two-element list [lower, upper]. The paper rounds these half
    up (one decimal for percentages and points, two for ratios in tables) and never from other printed
    values.

Tags (numbers.json "_tags")
    REC from the published records; LAB from labels.csv (alone or joined to the records); PRICE uses
    the vendor's price schedule; ARITH arithmetic on other outputs.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import json
import math
import random
import re
import statistics
import sys
import tarfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "results" / "2026-10-haiku-pilot"
HERE = ROOT / "docs" / "paper"
FIGS = HERE / "figures"

SEED = 20261029  # the date of the pilot's first invocation
BOOT = 200_000
Z95 = 1.959963984540054
Z90 = statistics.NormalDist().inv_cdf(0.95)
ARMS = ("solo", "review", "best")
TEAMS = ("review", "best")
ARM_LABEL = {"solo": "solo", "review": "review", "best": "best of 3"}
LANG_LABEL = {"java": "Java", "cs": "C#", "c": "C", "rust": "Rust", "js": "JavaScript", "cpp": "C++"}
PRICE = {"uncached": 0.125, "cache_read": 0.01, "output": 0.50}  # USD per million tokens, Claude Haiku 5.5
SANDBOX_USD_PER_HOUR = 0.668  # the repository's figure for a 2-core, 16 GiB Modal sandbox
RULES = ("current", "first", "missing", "both")
VERDICT_SCORE = re.compile(r'"score"\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)')
VERDICT_PASSED = re.compile(r'"passed"\s*:\s*(true|false)')
DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)\r?$", re.M)
APPLY_CHECK = re.compile(r"^Checking patch (.+?)\.\.\.\r?$")
APPLY_CLEAN = re.compile(r"^Applied patch (.+?) cleanly\.\r?$")
APPLY_REJECTS = re.compile(r"^Applying patch (.+?) with (\d+) rejects?\.\.\.\r?$")
HUNK_OK = re.compile(r"^Hunk #(\d+) applied cleanly\.\r?$")
# keywords for a verdict that gives the no-modification instruction as its reason for not building or testing
NOWRITE = re.compile(r"would write|writ\w* (?:build )?(?:output|bin/obj|artifacts)? ?in(?:to|side) (?:the repo|/testbed)|"
                     r"not allowed to modify|did not write to it|avoid (?:building|writing|modifying)|"
                     r"build tree lives inside the repo|since building would|to avoid building inside", re.I)
# a final message that names the repository's agent instruction file
INSTRUCTION_FILE = re.compile(r"\b(AGENTS|CLAUDE)\.md\b")


# =============================================================================================== output store
class Store:
    """Holds every number with its tag and the expected values it is checked against."""

    def __init__(self):
        self.values = {}
        self.tags = {}
        self.expect_log = []

    def put(self, key, value, tag):
        """Store one number under a new key with its source tag."""
        if key in self.values:
            raise KeyError("duplicate key " + key)
        self.values[key] = value
        self.tags[key] = tag
        return value

    def expect(self, key, expected, nd=None, soft=False):
        """Compare a stored value with its expected value at the printed precision (nd decimals, half up)."""
        got = self.values[key]
        shown = rounded(got, nd)
        ok = shown == expected
        self.expect_log.append((key, shown, expected, ok, soft))
        return ok


def half_up(x, nd):
    """Round half up to nd decimals (the paper's rule), from the decimal form of the float."""
    q = Decimal(1).scaleb(-nd)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


def rounded(v, nd):
    """half_up applied to a number or a list; nd None leaves the value as it is."""
    if nd is None:
        return v
    if isinstance(v, (list, tuple)):
        return [rounded(x, nd) for x in v]
    return half_up(v, nd)


def sig6(v):
    """The storage rule of numbers.json: six significant digits for every non-integer number."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        if v == 0 or not math.isfinite(v):
            return 0.0 if v == 0 else v
        return float(f"{v:.6g}")
    if isinstance(v, (list, tuple)):
        return [sig6(x) for x in v]
    if isinstance(v, dict):
        return {k: sig6(x) for k, x in v.items()}
    raise TypeError(type(v))


# =============================================================================================== statistics
def wilson(k, n, z=Z95):
    """Wilson score interval of k of n, as fractions."""
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(wins, losses):
    """Exact conditional two-sided McNemar p: min(1, 2 P[Bin(w + l, 1/2) <= min(w, l)])."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(Fraction(math.comb(n, i), 2**n) for i in range(min(wins, losses) + 1))
    return float(min(Fraction(1), 2 * tail))


def mcnemar_midp(wins, losses):
    """Mid-p version: twice the tail minus the probability of the observed count."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(Fraction(math.comb(n, i), 2**n) for i in range(k + 1))
    return float(min(Fraction(1), 2 * tail - Fraction(math.comb(n, k), 2**n)))


def newcombe10(both, wins, losses, neither, z=Z95):
    """Newcombe (1998) method 10 for a paired difference, team minus baseline (as fractions)."""
    n = both + wins + losses + neither
    p1, p2 = (both + wins) / n, (both + losses) / n
    l1, u1 = wilson(both + wins, n, z)
    l2, u2 = wilson(both + losses, n, z)
    margins = (both + wins) * (losses + neither) * (both + losses) * (wins + neither)
    num = both * neither - wins * losses
    if num > 0:
        num = max(num - n / 2, 0.0)
    phi = num / math.sqrt(margins) if margins else 0.0
    d = p1 - p2
    lo = d - math.sqrt(max(0.0, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2))
    hi = d + math.sqrt(max(0.0, (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2))
    return max(-1.0, lo), min(1.0, hi)


def tango_p01(wins, losses, n, d0):
    """Restricted maximum-likelihood loss probability under delta = d0 (Tango 1998)."""
    a = 2 * n
    b = -wins - losses + (2 * n - wins + losses) * d0
    c = -losses * d0 * (1 - d0)
    return (math.sqrt(max(0.0, b * b - 4 * a * c)) - b) / (2 * a)


def tango_z(wins, losses, n, d0):
    """Tango's score statistic for H0: delta = d0; equals McNemar's (b - c) / sqrt(b + c) at d0 = 0."""
    num = wins - losses - n * d0
    var = n * (2 * tango_p01(wins, losses, n, d0) + d0 * (1 - d0))
    if var <= 1e-15:
        return 0.0 if abs(num) < 1e-12 else math.copysign(math.inf, num)
    return num / math.sqrt(var)


def tango_interval(wins, losses, n, z=Z95):
    """Asymptotic score interval of Tango (1998): every d0 with |z(d0)| <= z."""
    d = (wins - losses) / n

    def solve(target, lo, hi):
        for _ in range(80):
            mid = (lo + hi) / 2
            if tango_z(wins, losses, n, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    eps = 1e-12
    return solve(z, -1 + eps, d), solve(-z, d, 1 - eps)


def kappa(both, a_only, b_only, neither):
    """Cohen's kappa of two arms' verdicts (Appendix B agreement table)."""
    n = both + a_only + b_only + neither
    po = (both + neither) / n
    pa, pb = (both + a_only) / n, (both + b_only) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return (po - pe) / (1 - pe)


def auc(pairs):
    """Mann-Whitney AUC of (score, outcome) pairs: P(score of a positive > a negative) + P(tie) / 2."""
    ordered = sorted(pairs, key=lambda x: x[0])
    ranks = [0.0] * len(ordered)
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    npos = sum(1 for _, o in ordered if o)
    nneg = len(ordered) - npos
    if npos == 0 or nneg == 0:
        return None
    rsum = sum(r for r, (_, o) in zip(ranks, ordered) if o)
    return (rsum - npos * (npos + 1) / 2) / (npos * nneg)


def quantile7(xs, q):
    """Linear interpolation between order statistics (type 7)."""
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def pearson(xs, ys):
    """Pearson correlation (Appendix F: reviewer's first score against the task's mean judge score)."""
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))


def betainc(a, b, x):
    """Regularised incomplete beta function I_x(a, b) (continued fraction, modified Lentz)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x > (a + 1) / (a + b + 2):
        return 1.0 - betainc(b, a, 1 - x)
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((a - 1 + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (a + b + m) * x / ((a + m2) * (a + 1 + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(lbt) * h / a


def t_quantile(p, df):
    """Student t quantile by bisection on the CDF."""

    def cdf(t):
        tail = 0.5 * betainc(df / 2, 0.5, df / (df + t * t))
        return 1 - tail if t > 0 else tail

    lo, hi = -60.0, 60.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def geomean_t(ratios, level=0.95):
    """Geometric mean of per-task ratios with a Student t interval on the log scale (n - 1 df)."""
    logs = [math.log(r) for r in ratios]
    n = len(logs)
    m = statistics.fmean(logs)
    se = statistics.stdev(logs) / math.sqrt(n)
    tq = t_quantile(1 - (1 - level) / 2, n - 1)
    return math.exp(m), [math.exp(m - tq * se), math.exp(m + tq * se)]


def sign_test(pos, neg):
    """Exact two-sided sign test (no ties)."""
    n = pos + neg
    tail = sum(Fraction(math.comb(n, i), 2**n) for i in range(min(pos, neg) + 1))
    return float(min(Fraction(1), 2 * tail))


def cluster_robust(diffs_by_cluster, level):
    """CR1 sandwich interval of a mean paired difference, t with G - 1 df."""
    allv = [d for v in diffs_by_cluster.values() for d in v]
    n, g = len(allv), len(diffs_by_cluster)
    mean = sum(allv) / n
    ss = sum(sum(d - mean for d in v) ** 2 for v in diffs_by_cluster.values())
    se = math.sqrt(g / (g - 1) * ss) / n
    tq = t_quantile(1 - (1 - level) / 2, g - 1)
    return mean, [mean - tq * se, mean + tq * se]


_BOOT_CACHE = {}


def boot_indices(n):
    """BOOT resamples of n indices with replacement, from random.Random(SEED); one set per n, shared.
    Each resample is held as bytes (indices below 256), which iterate as the same ints in less memory."""
    assert n <= 256
    if n not in _BOOT_CACHE:
        rng = random.Random(SEED)
        _BOOT_CACHE[n] = [bytes(rng.randrange(n) for _ in range(n)) for _ in range(BOOT)]
    return _BOOT_CACHE[n]


def percentile95(values):
    """The 5,001st and 195,000th of 200,000 sorted values (the same ranks scaled for fewer valid values)."""
    s = sorted(values)
    m = len(s)
    return [s[int(math.floor(0.025 * m))], s[int(math.ceil(0.975 * m)) - 1]]


def boot_ratio_of_totals(num, den):
    """Task bootstrap of sum(num) / sum(den), the same resampled tasks for numerator and denominator."""
    out = []
    for idx in boot_indices(len(num)):
        out.append(sum(num[i] for i in idx) / sum(den[i] for i in idx))
    return percentile95(out)


def boot_auc(pairs):
    """Task bootstrap of the AUC (Table 2); resamples without both outcomes are skipped and counted."""
    out = []
    for idx in boot_indices(len(pairs)):
        a = auc([pairs[i] for i in idx])
        if a is not None:
            out.append(a)
    return percentile95(out), len(out)


def cluster_boot_exact(clusters):
    """The exact bootstrap distribution (all resamples, no simulation) of a ratio of sums over clusters.
    G clusters, each (sum, size), are drawn G times with replacement, so the draw counts follow the
    multinomial law with G trials and equal shares; a dynamic programme over the clusters carries (draws so
    far, sum, size) with integer multinomial weights. Returns [(100 * sum / size, probability)], sorted, as
    Fractions."""
    g = len(clusters)
    states = {(0, 0, 0): 1}
    for i, (s, n) in enumerate(clusters):
        nxt = defaultdict(int)
        for (m, ss, nn), w in states.items():
            for k in ((g - m,) if i == g - 1 else range(g - m + 1)):
                nxt[(m + k, ss + k * s, nn + k * n)] += w * math.comb(m + k, k)
        states = nxt
    dist = defaultdict(int)
    for (_, ss, nn), w in states.items():
        dist[Fraction(100 * ss, nn)] += w
    assert sum(dist.values()) == g**g
    return [(x, Fraction(w, g**g)) for x, w in sorted(dist.items())]


def exact_limits(dist, alpha):
    """Percentile limits of an exact bootstrap distribution, the ranks of percentile95 as the resample count
    grows: the smallest value whose cumulative probability exceeds alpha, and the smallest whose cumulative
    probability reaches 1 - alpha."""
    cum, lo, hi = Fraction(0), None, None
    for x, pr in dist:
        cum += pr
        if lo is None and cum > alpha:
            lo = x
        if hi is None and cum >= 1 - alpha:
            hi = x
    return [float(lo), float(hi)]


# --------------------------------------------------------------------- exact unconditional (trinomial) test
class Trinomial:
    """Exact unconditional one-sided tests of delta = p10 - p01 for n independent tasks, each a win (p10),
    a loss (p01) or a tie. Outcomes are ordered by Tango's score statistic; the tail probability is
    enumerated exactly, and the p-value is its supremum over the nuisance p01, found numerically."""

    def __init__(self, n):
        self.n = n
        lf = [math.lgamma(i + 1) for i in range(n + 1)]
        self.cells = [(b, c) for b in range(n + 1) for c in range(n + 1 - b)]
        self.logc = {(b, c): lf[n] - lf[b] - lf[c] - lf[n - b - c] for b, c in self.cells}

    def prob(self, region, p10, p01):
        """P[(wins, losses) in region] for cell probabilities p10, p01 and 1 - p10 - p01."""
        n = self.n
        p00 = max(0.0, 1.0 - p10 - p01)
        l10 = math.log(p10) if p10 > 0 else None
        l01 = math.log(p01) if p01 > 0 else None
        l00 = math.log(p00) if p00 > 0 else None
        tot = 0.0
        for b, c in region:
            t = n - b - c
            if (b and l10 is None) or (c and l01 is None) or (t and l00 is None):
                continue
            x = self.logc[(b, c)]
            if b:
                x += b * l10
            if c:
                x += c * l01
            if t:
                x += t * l00
            tot += math.exp(x)
        return tot

    def region(self, wins, losses, d0, side):
        """Outcomes at least as extreme as the observed one under the score ordering at d0."""
        n = self.n
        z_obs = tango_z(wins, losses, n, d0)
        if side == "upper":  # evidence that delta < d0
            return [(b, c) for b, c in self.cells if tango_z(b, c, n, d0) <= z_obs + 1e-9]
        return [(b, c) for b, c in self.cells if tango_z(b, c, n, d0) >= z_obs - 1e-9]

    def sup(self, region, d0, lo, hi):
        """Largest probability of the region over the nuisance p01 in [lo, hi]: grid, then golden section."""
        def f(q):
            return self.prob(region, q + d0, q)

        if hi - lo < 1e-12:
            return f(lo)
        pts = sorted({lo + (hi - lo) * (i / 80) ** 2 for i in range(81)} | {lo + (hi - lo) * i / 20 for i in range(21)})
        vals = [f(q) for q in pts]
        j = max(range(len(pts)), key=vals.__getitem__)
        best = vals[j]
        a, b = pts[max(0, j - 1)], pts[min(len(pts) - 1, j + 1)]
        g = (math.sqrt(5) - 1) / 2
        x1, x2 = b - g * (b - a), a + g * (b - a)
        f1, f2 = f(x1), f(x2)
        for _ in range(25):
            if f1 < f2:
                a, x1, f1 = x1, x2, f2
                x2 = a + g * (b - a)
                f2 = f(x2)
            else:
                b, x2, f2 = x2, x1, f1
                x1 = b - g * (b - a)
                f1 = f(x1)
        return max(best, f1, f2)

    def pvalue(self, wins, losses, d0, side):
        """Exact unconditional one-sided p of delta = d0 (upper: evidence that delta < d0)."""
        lo, hi = max(0.0, -d0), (1.0 - d0) / 2.0
        return min(1.0, self.sup(self.region(wins, losses, d0, side), d0, lo, hi))

    def bound(self, wins, losses, alpha, side):
        """Confidence bound by inverting the test: grids of 0.005 and 0.0005, then bisection inside the
        step after the last accepted point."""
        d = (wins - losses) / self.n
        sgn = 1 if side == "upper" else -1
        stop = 1.0 - 1e-9 if side == "upper" else -1.0 + 1e-9
        last_ok, x, run = d, d, 0
        while (x - stop) * sgn < 0:
            if self.pvalue(wins, losses, x, side) > alpha:
                last_ok, run = x, 0
            else:
                run += 1
                if run >= 12:
                    break
            x += sgn * 0.005
        fine_last, k = last_ok, 0
        while k <= 11:
            x = last_ok + sgn * 0.0005 * k
            if (x - stop) * sgn >= 0:
                break
            if self.pvalue(wins, losses, x, side) > alpha:
                fine_last = x
            k += 1
        inside, outside = fine_last, fine_last + sgn * 0.0005
        if (outside - stop) * sgn > 0:
            outside = stop
        for _ in range(13):  # 0.0005 / 2^13 < 1e-7
            mid = (inside + outside) / 2
            if self.pvalue(wins, losses, mid, side) > alpha:
                inside = mid
            else:
                outside = mid
        return (inside + outside) / 2


def clopper_pearson(k, n, alpha):
    """Two-sided Clopper-Pearson interval of level 1 - alpha (bisection on binomial tails)."""

    def cdf(kk, p):
        return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(kk + 1)) if kk >= 0 else 0.0

    def solve(f, lo, hi):
        for _ in range(100):
            mid = (lo + hi) / 2
            if f(mid):
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(lambda p: 1 - cdf(k - 1, p) < alpha / 2, 0.0, 1.0)
    upper = 1.0 if k == n else solve(lambda p: cdf(k, p) > alpha / 2, 0.0, 1.0)
    return lower, upper


# --------------------------------------------------------------------- exact power of the McNemar test
class McNemarPower:
    """Exact power of a two-sided McNemar test at 0.05, counting rejections in the team's favour: the
    exact conditional test, or with test="midp" its mid-p version. Model: each task is a win with
    probability p10 and a loss with probability p01, independently."""

    def __init__(self, nmax, test="exact"):
        self.test = test
        self.crit = [None] * (nmax + 1)  # smallest number of wins that rejects among D discordant tasks
        for big_d in range(1, nmax + 1):
            total, cum, comb, best, k = 2**big_d, 0, 1, None, 0
            while 2 * k < big_d:
                cum += comb
                stat = 2 * cum if test == "exact" else 2 * cum - comb  # 2^D times the two-sided p
                if 20 * stat <= total:  # p <= 0.05
                    best = k
                else:
                    break
                k += 1
                comb = comb * (big_d - k + 1) // k
            self.crit[big_d] = None if best is None else big_d - best
        self._tails = {}

    def tails(self, q, nmax):
        """P(rejection in the team's favour | D discordant tasks) for D up to nmax, win share q."""
        key = (q, nmax)
        if key not in self._tails:
            out = [0.0] * (nmax + 1)
            for big_d in range(1, nmax + 1):
                w = self.crit[big_d]
                if w is not None:
                    out[big_d] = 1.0 if q >= 1.0 else betainc(w, big_d - w + 1, q)
            self._tails[key] = out
        return self._tails[key]

    def power(self, n, p10, p01):
        """Power at n tasks (Table 3, Figure A2)."""
        psi = p10 + p01
        if psi <= 0:
            return 0.0
        q = p10 / psi
        tails = self.tails(q, len(self.crit) - 1)
        sd = math.sqrt(n * psi * (1 - psi)) if psi < 1 else 0.0
        lo = max(1, int(n * psi - 15 * sd - 5))
        hi = min(n, int(n * psi + 15 * sd + 5))
        lgn = math.lgamma(n + 1)
        lp, lq = math.log(psi), (math.log1p(-psi) if psi < 1 else None)
        tot = 0.0
        for big_d in range(lo, hi + 1):
            if not tails[big_d]:
                continue
            if lq is None and big_d < n:
                continue
            lpmf = lgn - math.lgamma(big_d + 1) - math.lgamma(n - big_d + 1) + big_d * lp + ((n - big_d) * lq if big_d < n else 0.0)
            tot += math.exp(lpmf) * tails[big_d]
        return tot

    def n_for(self, p10, p01, target=0.80, nmax=None):
        """Smallest n with power at least the target (Table 3)."""
        nmax = nmax or (len(self.crit) - 1)
        for n in range(6, nmax + 1):
            if self.power(n, p10, p01) >= target:
                return n
        return None


def self_checks():
    """Methods checks behind Table 1 rows 1-4 and Figure 2: the re-implemented intervals against the
    repository's test value and the values a statistics package prints, and two identities of Tango's
    statistic."""
    # Newcombe method 10: the repository's test value (Armitage and Berry's 2x2 table: 20, 12, 2, 16)
    lo, hi = newcombe10(20, 12, 2, 16)
    assert abs(lo - 0.056156) < 1e-6 and abs(hi - 0.329207) < 1e-6
    # the paired table (cells 1, 1, 7, 12) of the vignette "Paired contrasts" of the R package ratesci 1.1.1, which
    # prints MOVER-NW (Newcombe method 10) -0.5069 to -0.0256 and AS(Tango) -0.5172 to -0.0260 for it
    lo, hi = newcombe10(1, 1, 7, 12)
    assert abs(lo + 0.5069) < 5e-5 and abs(hi + 0.0256) < 5e-5
    lo, hi = tango_interval(1, 7, 21)
    assert abs(lo + 0.5172) < 5e-5 and abs(hi + 0.0260) < 5e-5
    # Tango's statistic at zero is McNemar's; with no losses its upper limit is the Wilson upper limit of wins / n
    assert abs(tango_z(2, 0, 48, 0.0) - 2 / math.sqrt(2)) < 1e-12
    assert abs(tango_interval(2, 0, 48, Z90)[1] - wilson(2, 48, Z90)[1]) < 1e-9
    # exact McNemar and its mid-p on the pilot's two tables
    assert mcnemar_exact(2, 0) == 0.5 and mcnemar_exact(2, 1) == 1.0 and mcnemar_midp(2, 0) == 0.25 and mcnemar_midp(2, 1) == 0.625


# =============================================================================================== loading
def load_full_messages(final):
    """Full final message of every run of the 144 final pairs, from artifacts.tar.gz: {arm: {task: [(role,
    message)]}}. Each pair's runs.json must list the same session ids, in the same order, as its record."""
    arm_of = {r["team"].replace(":", "_", 1): a for a in ARMS for r in final[a].values()}
    out = {a: {} for a in ARMS}
    with tarfile.open(DATA / "artifacts.tar.gz", "r:gz") as tf:
        for member in tf:
            if not (member.isfile() and member.name.endswith("/runs.json")):
                continue
            parts = member.name.split("/")
            arm, task = arm_of[parts[-3]], parts[-2]
            runs = json.load(tf.extractfile(member))["runs"]
            assert [x["session_id"] for x in runs] == [x["session_id"] for x in final[arm][task]["runs"]], (arm, task)
            out[arm][task] = [(x["role"], x["final_message"] or "") for x in runs]
    assert all(sorted(out[a]) == sorted(final[a]) for a in ARMS)
    return out


def compact_apply_log(log):
    """The lines of a `git apply` log that apply_log_files reads, each run of other lines kept as one empty
    line. apply_log_files reads only lines matching its four patterns and treats a run of other lines as
    it treats one, so its result is unchanged; the 285 MB of logs need not stay in memory."""
    out, gap = [], False
    for line in log.splitlines():
        if APPLY_CHECK.match(line) or APPLY_CLEAN.match(line) or APPLY_REJECTS.match(line) or HUNK_OK.match(line):
            out.append(line)
            gap = False
        elif not gap:
            out.append("")
            gap = True
    return "\n".join(out)


def load_pilot():
    """Read the published files and select the final record of every pair."""
    records, names = [], {}
    with gzip.open(DATA / "results.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                ev = r.get("eval")
                if isinstance(ev, dict) and isinstance(ev.get("apply_log"), str):
                    ev["apply_log"] = compact_apply_log(ev["apply_log"])
                # one shared copy of each test id: the same ids recur in every record of a task
                for group in ((ev or {}).get("report") or {}).values():
                    if isinstance(group, dict):
                        for k, ids in group.items():
                            if isinstance(ids, list):
                                group[k] = [names.setdefault(x, x) if isinstance(x, str) else x for x in ids]
                records.append(r)
    gold = [json.loads(line) for line in (DATA / "gold.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    latest = {}
    for i, r in enumerate(records):
        latest[(r["task_id"], r["team"].split(":", 1)[0])] = i
    final = {a: {} for a in ARMS}
    for (task, arm), i in latest.items():
        assert records[i]["error"] is None, (task, arm)
        final[arm][task] = records[i]
    final_ids = set(latest.values())
    superseded = [r for i, r in enumerate(records) if i not in final_ids]
    tasks = sorted(final["solo"])
    assert all(sorted(final[a]) == tasks for a in ARMS) and len(tasks) == 48
    return {"records": records, "final": final, "superseded": superseded, "tasks": tasks, "gold": gold, "meta": meta,
            "full": load_full_messages(final)}


def load_labels():
    """Read docs/paper/labels.csv into {key: {task_id: value}} (task_id '' for one-row values)."""
    out = defaultdict(dict)
    with open(HERE / "labels.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            assert row["task_id"] not in out[row["key"]], row
            out[row["key"]][row["task_id"]] = row["value"]
    return out


def base_cost(usage):
    """Model cost of one run at base prices (Table 1 row 6), in US dollars."""
    unc = usage["input_tokens"] - usage["cached_input_tokens"]
    return (unc * PRICE["uncached"] + usage["cached_input_tokens"] * PRICE["cache_read"] + usage["output_tokens"] * PRICE["output"]) / 1e6


def parse_verdict(msg):
    """(score, passed) of a reviewer or judge verdict; the stored message is cut at 1,000 characters,
    and both fields come first in the verdict's JSON."""
    s, p = VERDICT_SCORE.search(msg or ""), VERDICT_PASSED.search(msg or "")
    if not s or not p:
        return None
    return float(s.group(1)), p.group(1) == "true"


def verdict_feedback(msg):
    """Feedback of the last JSON object with score, passed and feedback in a full verdict message."""
    dec = json.JSONDecoder()
    found, i = None, 0
    while True:
        start = msg.find("{", i)
        if start < 0:
            return found
        try:
            obj, end = dec.raw_decode(msg, start)
        except ValueError:
            i = start + 1
            continue
        i = end
        if isinstance(obj, dict) and {"score", "passed", "feedback"} <= obj.keys():
            found = obj["feedback"]


def apply_log_files(log):
    """Hidden test patch files and the candidate's per-file apply status, from `git apply --reject` output."""
    lines = log.splitlines()
    i = 0
    while i < len(lines) and not APPLY_CHECK.match(lines[i]):
        i += 1
    checked = []
    while i < len(lines) and APPLY_CHECK.match(lines[i]):
        checked.append(APPLY_CHECK.match(lines[i]).group(1))
        i += 1
    hidden = []
    while i < len(lines) and len(hidden) < len(checked):
        m = APPLY_CLEAN.match(lines[i])
        if m:
            hidden.append(m.group(1))
        elif APPLY_CHECK.match(lines[i]):
            break
        i += 1
    assert sorted(hidden) == sorted(checked) and hidden, "hidden test patch block not clean"
    cand, cur = {}, None
    for line in lines[i:]:
        m = APPLY_CLEAN.match(line)
        if m:
            cand[m.group(1)] = {"clean": True, "ok": 0}
            cur = None
            continue
        m = APPLY_REJECTS.match(line)
        if m:
            cur = m.group(1)
            cand[cur] = {"clean": False, "ok": 0}
            continue
        if cur and HUNK_OK.match(line):
            cand[cur]["ok"] += 1
    return set(hidden), cand


def derive(p, lab):
    """Per-pair outcomes under every rule, costs, verdicts and task facts used by every later section."""
    final, tasks = p["final"], p["tasks"]
    d = {"tasks": tasks}
    d["repo"] = {t: final["solo"][t]["repo"] for t in tasks}
    d["lang"] = {t: final["solo"][t]["language"] for t in tasks}
    res = {rule: {a: {} for a in ARMS} for rule in RULES}
    for a in ARMS:
        for t in tasks:
            r = final[a][t]
            ev = r["eval"]
            rep = ev["report"]
            cur = bool(r["resolved"]) and r["error"] is None
            first = bool(ev["patch_applied"]) and bool(rep["resolved"])
            assert first == (cur and bool(ev["patch_applied"]))
            missing = cur and not rep["PASS_TO_PASS"]["missing"]
            res["current"][a][t] = cur
            res["first"][a][t] = first
            res["missing"][a][t] = missing
            res["both"][a][t] = first and missing
    d["res"] = res
    cls = lab["class"]
    d["cls"] = {t: (cls[t] if cls[t] != "none" else None) for t in tasks}
    assert len(cls) == 48
    # costs per record and per run
    d["billed"] = {a: [final[a][t]["usage"]["cost_usd"] for t in tasks] for a in ARMS}
    d["base"] = {a: [sum(base_cost(x["usage"]) for x in final[a][t]["runs"]) for t in tasks] for a in ARMS}
    for a in ARMS:
        for t in tasks:
            r = final[a][t]
            assert abs(sum(x["usage"]["cost_usd"] for x in r["runs"]) - r["usage"]["cost_usd"]) < 1e-9
            for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
                assert sum(x["usage"][k] for x in r["runs"]) == r["usage"][k]
    # verdicts
    review_v, judge_v = {}, {}
    for t in tasks:
        r = final["review"][t]
        vs = [parse_verdict(x.get("final_message")) for x in r["runs"] if x["role"] == "reviewer"]
        assert all(v is not None for v in vs) and [v[0] for v in vs] == r["meta"]["scores"]
        review_v[t] = vs
        b = final["best"][t]
        jv = []
        for cand in b["meta"]["candidates"]:
            w_i, j_i = cand["runs"]
            assert b["runs"][w_i]["role"] == "worker" and b["runs"][j_i]["role"] == "judge"
            v = parse_verdict(b["runs"][j_i].get("final_message"))
            assert v is not None and v[0] == cand["score"]
            jv.append(v)
        assert [v[0] for v in jv] == b["meta"]["scores"]
        judge_v[t] = jv
    d["review_v"], d["judge_v"] = review_v, judge_v
    return d


# =============================================================================================== records and sample
def records_and_sample(p, d, lab, S):
    """Data and code, Appendix H and §8: records, superseded records, runs, tasks, repositories, languages, gold file."""
    recs, sup, final, tasks = p["records"], p["superseded"], p["final"], d["tasks"]
    S.put("records.total", len(recs), "REC")
    S.put("records.final", sum(len(final[a]) for a in ARMS), "REC")
    S.put("records.superseded", len(sup), "REC")
    S.put("superseded.no_model", sum("never reached the model" in (r["error"] or "") for r in sup), "REC")
    S.put("superseded.mid_run", sum("mid-run" in (r["error"] or "") for r in sup), "REC")
    assert all(r["error"] for r in sup) and all(r["stage"] == "solve" for r in sup)
    costed = [r for r in sup if (r.get("usage") or {}).get("cost_usd")]
    S.put("superseded.with_cost", len(costed), "REC")
    S.put("superseded.cost_usd", sum(r["usage"]["cost_usd"] for r in costed), "REC")
    S.put("superseded.graded_resolved", sum(bool((r.get("eval") or {}).get("resolved")) for r in sup), "REC")
    swap = {a: dict((t, d["res"]["current"][a][t]) for t in tasks) for a in ARMS}
    for r in sup:
        ev = r.get("eval") or {}
        if ev.get("report"):
            swap[r["team"].split(":", 1)[0]][r["task_id"]] = bool(ev["resolved"])
    for a in ARMS:
        S.put(f"superseded.swap_resolved.{a}", sum(swap[a].values()), "REC")
    graded = [r for r in sup if (r.get("eval") or {}).get("report")]
    S.put("superseded.graded", len(graded), "REC")
    S.put("superseded.graded_same_verdict", sum(1 for r in graded if bool(r["eval"]["resolved"]) == d["res"]["current"][r["team"].split(":", 1)[0]][r["task_id"]]), "REC")
    days = Counter(final[a][t]["started_at"][:10] for a in ARMS for t in tasks)
    S.put("final.started_day1", days["2026-10-29"], "REC")
    S.put("final.started_day2", days["2026-10-31"], "REC")
    roles = Counter()
    exit0 = 0
    for a in ARMS:
        for t in tasks:
            for x in final[a][t]["runs"]:
                roles[(a, x["role"])] += 1
                exit0 += x["exit_code"] == 0
    for (a, role), k in sorted(roles.items()):
        S.put(f"runs.by_role.{a}_{role}", k, "REC")
    S.put("runs.total", sum(roles.values()), "REC")
    S.put("runs.exit0", exit0, "REC")
    S.put("tasks.n", len(tasks), "REC")
    sizes = Counter(d["repo"].values())
    S.put("repos.n", len(sizes), "REC")
    top = sizes.most_common(2)
    S.put("repos.top2", [top[0][1], top[1][1]], "REC")
    S.put("repos.top2_names", [top[0][0], top[1][0]], "REC")
    S.put("repos.top2_share_pct", 100 * (top[0][1] + top[1][1]) / len(tasks), "REC")
    S.put("repos.eff_clusters", len(tasks) ** 2 / sum(m * m for m in sizes.values()), "REC")
    langs = Counter(d["lang"].values())
    for code, k in langs.items():
        S.put(f"tasks.languages.{LANG_LABEL[code]}", k, "REC")
    gold = p["gold"]
    S.put("gold.sampled", len(gold), "REC")
    S.put("gold.valid", sum(1 for g in gold if g["valid"]), "REC")
    S.put("gold.invalid", sum(1 for g in gold if not g["valid"] and g["error"] is None), "REC")
    S.put("gold.error", sum(1 for g in gold if g["error"] is not None), "REC")
    assert sorted(g["task_id"] for g in gold if g["valid"]) == tasks
    assert all(sorted(run["task_ids"]) == tasks for run in p["meta"]["runs"])
    inval = [g for g in gold if not g["valid"] and g["error"] is None]
    S.put("gold.invalid_by_failure.f2p_failed", sum(1 for g in inval if g["fail_to_pass_failed"] > 0), "REC")
    S.put("gold.invalid_by_failure.p2p_only", sum(1 for g in inval if g["fail_to_pass_failed"] == 0 and g["pass_to_pass_failed"] > 0), "REC")
    S.put("gold.invalid_by_failure.none", sum(1 for g in inval if g["fail_to_pass_failed"] == 0 and g["pass_to_pass_failed"] == 0), "REC")
    S.put("review.patch_over_40k", sum(1 for t in tasks if len(final["review"][t]["patch"] or "") > 40000), "REC")
    nverd = sum(len(d["review_v"][t]) + len(d["judge_v"][t]) for t in tasks)
    S.put("verdicts.parsed", nverd, "REC")
    stored = [x.get("final_message") or "" for t in tasks for a in ("review", "best") for x in final[a][t]["runs"] if x["role"] in ("reviewer", "judge")]
    S.put("verdicts.nowrite_cited_stored", sum(1 for m in stored if NOWRITE.search(m)), "REC")
    S.put("verdicts.stored_messages_cut", sum(1 for m in stored if len(m) >= 1000), "REC")
    # the same keywords over the verdict feedback of the full messages (a keyword count, so a lower bound)
    fb = [(role, verdict_feedback(m)) for a in ("review", "best") for t in tasks for role, m in p["full"][a][t]
          if role in ("reviewer", "judge")]
    assert len(fb) == nverd and all(isinstance(f, str) for _, f in fb)
    S.put("verdicts.nowrite_keywords", NOWRITE.pattern, "REC")
    S.put("verdicts.nowrite_cited", sum(1 for _, f in fb if NOWRITE.search(f)), "REC")
    S.put("verdicts.nowrite_cited_by_role", [sum(1 for r, f in fb if r == role and NOWRITE.search(f)) for role in ("reviewer", "judge")], "REC")
    S.put("redaction.flagged", sum(bool(final[a][t].get("redacted")) for a in ARMS for t in tasks), "REC")
    S.put("redaction.patch_redacted", sum(bool(final[a][t].get("patch_redacted")) for a in ARMS for t in tasks), "REC")
    S.put("run.invocations", [r["started_at"][:19] + "Z" for r in p["meta"]["runs"]], "REC")
    S.put("run.seed", p["meta"]["runs"][0]["seed"], "REC")
    S.put("run.concurrency", p["meta"]["runs"][0]["concurrency"], "REC")
    S.put("run.solve_timeout_s", p["meta"]["runs"][0]["solve_timeout_s"], "REC")
    S.put("run.dataset_revision", p["meta"]["datasets"]["multilang"]["revision"][:12], "REC")
    # task description (Appendix C)
    created = sorted(final["solo"][t]["created_at"] for t in tasks)
    S.put("tasks.created_range", [created[0][:10], created[-1][:10]], "REC")
    ages = [(datetime.fromisoformat(final["solo"][t]["started_at"]) - datetime.fromisoformat(final["solo"][t]["created_at"])).total_seconds() / 86400 for t in tasks]
    S.put("tasks.age_days_range", [min(ages), max(ages)], "REC")
    S.put("tasks.age_days_median", statistics.median(ages), "REC")
    f2p = [sum(len(final["solo"][t]["eval"]["report"]["FAIL_TO_PASS"][k]) for k in ("success", "failure", "missing")) for t in tasks]
    p2p = [sum(len(final["solo"][t]["eval"]["report"]["PASS_TO_PASS"][k]) for k in ("success", "failure", "missing")) for t in tasks]
    for a in ARMS:  # the listed tests are the same for every arm of a task
        for i, t in enumerate(tasks):
            assert sum(len(final[a][t]["eval"]["report"]["FAIL_TO_PASS"][k]) for k in ("success", "failure", "missing")) == f2p[i]
    S.put("tasks.f2p_median", statistics.median(f2p), "REC")
    S.put("tasks.f2p_one", sum(1 for x in f2p if x == 1), "REC")
    S.put("tasks.f2p_over100", sum(1 for x in f2p if x > 100), "REC")
    S.put("tasks.f2p_max", max(f2p), "REC")
    S.put("tasks.p2p_median", statistics.median(p2p), "REC")
    S.put("tasks.p2p_range", [min(p2p), max(p2p)], "REC")
    sampled_repos = defaultdict(set)
    for g in gold:
        owner, rest = g["task_id"].split("__", 1)
        sampled_repos[owner + "/" + rest.rsplit("-", 1)[0]].add(bool(g["valid"]))
    S.put("selection.repos_sampled", len(sampled_repos), "REC")
    S.put("selection.repos_mixed", sum(1 for v in sampled_repos.values() if v == {True, False}), "REC")
    d["f2p_sizes"] = f2p


# =============================================================================================== outcomes
def table22(res, team, base, tasks):
    """Paired 2x2 table (both, wins, losses, neither) of one comparison on one task set."""
    both = sum(1 for t in tasks if res[team][t] and res[base][t])
    wins = sum(1 for t in tasks if res[team][t] and not res[base][t])
    losses = sum(1 for t in tasks if res[base][t] and not res[team][t])
    return both, wins, losses, len(tasks) - both - wins - losses


def paired(res, team, base, tasks):
    """All paired statistics of one comparison on one task set."""
    both, wins, losses, neither = table22(res, team, base, tasks)
    n = len(tasks)
    out = {"both": both, "wins": wins, "losses": losses, "neither": neither, "n": n,
           "diff_pts": 100 * (wins - losses) / n,
           "newcombe95_pts": [100 * x for x in newcombe10(both, wins, losses, neither, Z95)],
           "newcombe90_pts": [100 * x for x in newcombe10(both, wins, losses, neither, Z90)],
           "tango95_pts": [100 * x for x in tango_interval(wins, losses, n, Z95)],
           "tango90_pts": [100 * x for x in tango_interval(wins, losses, n, Z90)],
           "mcnemar_exact": mcnemar_exact(wins, losses), "mcnemar_midp": mcnemar_midp(wins, losses)}
    return out


def put_paired(S, prefix, pr, tag):
    """Store every statistic of paired() under one prefix."""
    for k, v in pr.items():
        S.put(f"{prefix}.{k}", v, tag)


def outcomes(p, d, lab, S):
    """Table 1 rows 1-4 and §4: resolve rates, paired tests, overlap, agreement, languages."""
    tasks, res = d["tasks"], d["res"]["current"]
    for a in ARMS:
        k = sum(res[a].values())
        S.put(f"resolved.{a}.count", k, "REC")
        S.put(f"resolved.{a}.pct", 100 * k / 48, "REC")
        S.put(f"resolved.{a}.wilson95_pct", [100 * x for x in wilson(k, 48)], "REC")
    for team in TEAMS:
        put_paired(S, f"pair.{team}", paired(res, team, "solo", tasks), "REC")
    put_paired(S, "pair.review_vs_best", paired(res, "review", "best", tasks), "REC")
    pats = Counter((res["solo"][t], res["review"][t], res["best"][t]) for t in tasks)
    S.put("overlap.all3", pats[(True, True, True)], "REC")
    S.put("overlap.none", pats[(False, False, False)], "REC")
    S.put("overlap.review_best_only", pats[(False, True, True)], "REC")
    S.put("overlap.solo_review_only", pats[(True, True, False)], "REC")
    S.put("overlap.other_patterns", sum(v for k, v in pats.items() if k not in {(True, True, True), (False, False, False), (False, True, True), (True, True, False)}), "REC")
    union = {t for t in tasks if any(res[a][t] for a in ARMS)}
    S.put("overlap.union", len(union), "REC")
    S.put("overlap.union_equals_review", union == {t for t in tasks if res["review"][t]}, "REC")
    disc = sorted(t for t in tasks if any(res[team][t] != res["solo"][t] for team in TEAMS))
    S.put("discordant.ids", disc, "REC")
    S.put("discordant.review_wins", sorted(t for t in tasks if res["review"][t] and not res["solo"][t]), "REC")
    S.put("discordant.best_wins", sorted(t for t in tasks if res["best"][t] and not res["solo"][t]), "REC")
    S.put("discordant.best_losses", sorted(t for t in tasks if res["solo"][t] and not res["best"][t]), "REC")
    d["discordant"] = disc
    for x, y in (("solo", "review"), ("solo", "best"), ("review", "best")):
        both, a_only, b_only, neither = table22(res, x, y, tasks)
        S.put(f"agreement.{x}_{y}_pct", 100 * (both + neither) / 48, "REC")
        S.put(f"kappa.{x}_{y}", kappa(both, a_only, b_only, neither), "REC")
    for code in sorted(set(d["lang"].values())):
        sub = [t for t in tasks if d["lang"][t] == code]
        S.put(f"bylang.{LANG_LABEL[code]}", [len(sub)] + [sum(res[a][t] for t in sub) for a in ARMS], "REC")
        S.put(f"bylang_first.{LANG_LABEL[code]}", [sum(d["res"]["first"][a][t] for t in sub) for a in ARMS], "REC")


# =============================================================================================== grading
def task_sets(p, d, lab):
    """The task subsets of Figure 2 and Appendix B (labels decide the flagged ones and the issue dates)."""
    tasks, cls = d["tasks"], d["cls"]
    issue = lab["issue_created"]
    sets = {
        "all48": tasks,
        "noBC": [t for t in tasks if cls[t] not in ("B", "C")],
        "noflag": [t for t in tasks if cls[t] is None],
        "issue41": [t for t in tasks if issue[t] >= "2026-07-01"],
        "nolangchain": [t for t in tasks if d["repo"][t] != "langchain4j/langchain4j"],
    }
    return sets


def grading(p, d, lab, S):
    """Table 1 row 5, §5.1, Figure 2 rows and Table B1."""
    final, tasks, res = p["final"], d["tasks"], d["res"]
    sets = task_sets(p, d, lab)
    d["sets"] = sets
    for name, sub in sets.items():
        S.put(f"set.{name}.n", len(sub), "LAB" if name in ("noBC", "noflag", "issue41") else "REC")
    signs = Counter()
    for rule in RULES:
        for name, sub in sets.items():
            tag = "LAB" if name in ("noBC", "noflag", "issue41") else "REC"
            S.put(f"rule.{rule}.{name}.resolved", [sum(res[rule][a][t] for t in sub) for a in ARMS], tag)
            for team in TEAMS:
                pr = paired(res[rule], team, "solo", sub)
                put_paired(S, f"rule.{rule}.{name}.{team}", pr, tag)
                signs["positive" if pr["wins"] > pr["losses"] else ("negative" if pr["wins"] < pr["losses"] else "zero")] += 1
    S.put("sens.sign_count.appB", [signs["positive"], signs["zero"], signs["negative"]], "LAB")
    S.put("sens.min_exact_p.appB", min(S.values[f"rule.{r}.{s}.{t}.mcnemar_exact"] for r in RULES for s in sets for t in TEAMS), "LAB")
    # short names used by the paper's tables
    for a, k in zip(ARMS, S.values["rule.first.all48.resolved"]):
        S.put(f"resolved.first_rule.{a}", k, "REC")
    for a, k in zip(ARMS, S.values["rule.missing.all48.resolved"]):
        S.put(f"resolved.missing_test.{a}", k, "REC")
    for a, k in zip(ARMS, S.values["rule.both.all48.resolved"]):
        S.put(f"resolved.both_strict.{a}", k, "REC")
    # Figure 2 rows
    rows = [("all48", "current"), ("all48", "first"), ("all48", "missing"), ("all48", "both"),
            ("noBC", "current"), ("noflag", "current"), ("nolangchain", "current")]
    fig2 = []
    for name, rule in rows:
        fig2.append({team: {k: S.values[f"rule.{rule}.{name}.{team}.{k}"] for k in ("wins", "losses", "diff_pts", "newcombe95_pts", "tango95_pts", "mcnemar_exact")} for team in TEAMS})
    d["fig2"] = fig2
    S.put("fig2.min_exact_p", min(r[t]["mcnemar_exact"] for r in fig2 for t in TEAMS), "LAB")
    S.put("fig2.all_newcombe_contain_zero", all(r[t]["newcombe95_pts"][0] <= 0 <= r[t]["newcombe95_pts"][1] for r in fig2 for t in TEAMS), "LAB")
    # the re-grade
    changed = [(a, t) for a in ARMS for t in tasks if res["current"][a][t] != res["first"][a][t]]
    S.put("regrade.changed", len(changed), "REC")
    for a in ARMS:
        S.put(f"regrade.by_arm.{a}", sum(1 for x, _ in changed if x == a), "REC")
    S.put("regrade.tasks", len({t for _, t in changed}), "REC")
    S.put("regrade.to_resolved", sum(1 for a, t in changed if res["current"][a][t]), "REC")
    S.put("regrade.to_unresolved", sum(1 for a, t in changed if not res["current"][a][t]), "REC")
    nres = sum(res["current"][a][t] for a in ARMS for t in tasks)
    S.put("regrade.resolved_total", nres, "REC")
    S.put("regrade.only_current_share_pct", 100 * len(changed) / nres, "REC")
    S.put("regrade.all_records_marked", all(final[a][t]["eval"].get("regraded") for a in ARMS for t in tasks), "REC")
    S.put("regrade.tasks_all_three_changed", sum(1 for t in tasks if all(res["current"][a][t] != res["first"][a][t] for a in ARMS)), "REC")
    # rejected hunks and the hidden test files
    rej = [(a, t) for a in ARMS for t in tasks if final[a][t]["eval"]["patch_rejected_files"]]
    assert {(a, t) for a, t in rej} == {(a, t) for a in ARMS for t in tasks if not final[a][t]["eval"]["patch_applied"]}
    S.put("rejected.patches", len(rej), "REC")
    for a in ARMS:
        S.put(f"rejected.by_arm.{a}", sum(1 for x, _ in rej if x == a), "REC")
    S.put("rejected.tasks", len({t for _, t in rej}), "REC")
    S.put("rejected.resolved_current", sum(1 for a, t in rej if res["current"][a][t]), "REC")
    touch = kept = all_in = 0
    touch_by = Counter()
    kept_resolved = 0
    for a in ARMS:
        for t in tasks:
            r = final[a][t]
            hidden, cand = apply_log_files(r["eval"]["apply_log"])
            pfiles = {m[1] for m in DIFF_FILE.findall(r["patch"] or "")}
            ov = hidden & pfiles
            if r["eval"]["patch_rejected_files"]:
                all_in += set(r["eval"]["patch_rejected_files"]) <= hidden
            if ov:
                touch += 1
                touch_by[a] += 1
                if any(f in cand and (cand[f]["clean"] or cand[f]["ok"] > 0) for f in ov):
                    kept += 1
                    kept_resolved += res["current"][a][t]
    S.put("rejected.all_in_hidden_files", all_in, "REC")
    S.put("hidden_file.touch", touch, "REC")
    for a in ARMS:
        S.put(f"hidden_file.touch_by_arm.{a}", touch_by[a], "REC")
    S.put("hidden_file.hunks_kept", kept, "REC")
    S.put("hidden_file.hunks_kept_resolved", kept_resolved, "REC")
    dec = lab["hidden_file_hunks_decide_unresolved"]
    pairs = [(a, t) for t, v in dec.items() for a in v.split(";")]
    assert all(not res["current"][a][t] for a, t in pairs)
    S.put("hidden_file.decides_unresolved", len(pairs), "LAB")
    S.put("unresolved.total", sum(1 for a in ARMS for t in tasks if not res["current"][a][t]), "REC")
    S.put("current_equals_test_only", sum(1 for a in ARMS for t in tasks if res["current"][a][t] == bool(final[a][t]["eval"]["report"]["resolved"])), "REC")
    # missing PASS_TO_PASS tests
    miss = [(a, t) for a in ARMS for t in tasks if final[a][t]["eval"]["report"]["PASS_TO_PASS"]["missing"]]
    S.put("missing_p2p.records", len(miss), "REC")
    S.put("missing_p2p.tasks", len({t for _, t in miss}), "REC")
    listed = sum(len(final[a][t]["eval"]["report"]["PASS_TO_PASS"][k]) for a in ARMS for t in tasks for k in ("success", "failure", "missing"))
    nmiss = sum(len(final[a][t]["eval"]["report"]["PASS_TO_PASS"]["missing"]) for a, t in miss)
    S.put("missing_p2p.listed_tests", listed, "REC")
    S.put("missing_p2p.missing_tests", nmiss, "REC")
    S.put("missing_p2p.share_pct", 100 * nmiss / listed, "REC")
    moves = [(a, t) for a in ARMS for t in tasks if res["current"][a][t] != res["missing"][a][t]]
    S.put("missing_p2p.moves", len(moves), "REC")
    movers = sorted({t for _, t in moves})
    S.put("missing_p2p.move_tasks", len(movers), "REC")
    S.put("missing_p2p.class_c_movers", sum(1 for t in movers if d["cls"][t] == "C"), "LAB")
    # failure classes of the unresolved final records (Appendix B)
    rep_cls, f2p_cls = Counter(), Counter()
    for a in ARMS:
        for t in tasks:
            if res["current"][a][t]:
                continue
            rep = final[a][t]["eval"]["report"]
            f2p, p2pf = rep["FAIL_TO_PASS"], bool(rep["PASS_TO_PASS"]["failure"])
            noresult = not f2p["success"] and not f2p["failure"]
            allpass = not f2p["failure"] and not f2p["missing"]
            rep_cls[(a, "p2p" if p2pf else ("nof2p" if noresult else "f2pfail"))] += 1
            f2p_cls[(a, "nof2p" if noresult else ("f2pfail" if not allpass else ("p2p" if p2pf else "other")))] += 1
    for name, cnt in (("report", rep_cls), ("f2p_first", f2p_cls)):
        for a in ARMS:
            S.put(f"failure_class.{name}.{a}", [cnt[(a, "nof2p")], cnt[(a, "f2pfail")], cnt[(a, "p2p")]], "REC")
        S.put(f"failure_class.{name}.all", [sum(cnt[(a, k)] for a in ARMS) for k in ("nof2p", "f2pfail", "p2p")], "REC")
        assert not any(k[1] == "other" for k in cnt)


# =============================================================================================== exclusion
def exclusion(p, d, lab, S):
    """Table 3 (exclusion), §9 and Table B2: upper limits by four methods, one-sided p against fixed margins, coverage."""
    tasks, res = d["tasks"], d["res"]["current"]
    tri = Trinomial(48)
    margins = []
    for team in TEAMS:
        both, wins, losses, neither = table22(res, team, "solo", tasks)
        lim = {}
        for lev, alpha, z in (("90", 0.05, Z90), ("95", 0.025, Z95)):
            lim[("newcombe", lev)] = [100 * x for x in newcombe10(both, wins, losses, neither, z)]
            lim[("tango", lev)] = [100 * x for x in tango_interval(wins, losses, 48, z)]
            lim[("exact", lev)] = [100 * tri.bound(wins, losses, alpha, "lower"), 100 * tri.bound(wins, losses, alpha, "upper")]
            by = defaultdict(list)
            for t in tasks:
                by[d["repo"][t]].append(int(res[team][t]) - int(res["solo"][t]))
            lim[("cluster", lev)] = [100 * x for x in cluster_robust(by, 1 - 2 * alpha)[1]]
        for (method, lev), v in lim.items():
            S.put(f"upper.{method}.{lev}.{team}_pts", v[1], "REC")
            S.put(f"interval.{method}.{lev}.{team}_pts", v, "REC")
            if lev == "90":
                margins.append(max(v[1], -v[0]))
        # one-sided p-values against fixed margins: Tango's score test (asymptotic) and the exact unconditional test
        for kind, m in (("gain", 5), ("gain", 10), ("gain", 15), ("gain", 20), ("loss", 5), ("loss", 10), ("loss", 15)):
            d0 = (m if kind == "gain" else -m) / 100
            side = "upper" if kind == "gain" else "lower"
            zz = tango_z(wins, losses, 48, d0)
            pt = statistics.NormalDist().cdf(zz) if kind == "gain" else 1 - statistics.NormalDist().cdf(zz)
            pe = tri.pvalue(wins, losses, d0, side)
            S.put(f"onesided.{kind}{m}.{team}", {"tango": pt, "exact": pe}, "REC")
            S.put(f"onesided.{kind}{m}.{team}.range", [min(pt, pe), max(pt, pe)], "REC")
    S.put("equivalence.margin_needed_pts", [min(margins), max(margins)], "REC")
    S.put("onesided.gain15.max", max(S.values[f"onesided.gain15.{t}.range"][1] for t in TEAMS), "REC")
    # the 15-point exclusion on the 38 tasks without classes B and C, and under the missing-test rule on all 48
    for name, rule, sub, tag in (("noBC", "current", d["sets"]["noBC"], "LAB"), ("missing", "missing", tasks, "REC")):
        trs = Trinomial(len(sub))
        n = len(sub)
        for team in TEAMS:
            _, wins, losses, _ = table22(d["res"][rule], team, "solo", sub)
            S.put(f"upper.exact.90.{name}.{team}_pts", 100 * trs.bound(wins, losses, 0.05, "upper"), tag)
            pt = statistics.NormalDist().cdf(tango_z(wins, losses, n, 0.15))
            pe = trs.pvalue(wins, losses, 0.15, "upper")
            S.put(f"onesided.gain15.{name}.{team}", {"tango": pt, "exact": pe}, tag)
            S.put(f"onesided.gain15.{name}.{team}.range", [min(pt, pe), max(pt, pe)], tag)
            S.put(f"onesided.gain15.{name}.{team}.exact", pe, tag)
    S.put("onesided.loss10.rejected_both", all(S.values[f"onesided.loss10.{t}.range"][1] < 0.05 for t in TEAMS), "REC")
    S.put("p.wins_le2_given_gain10", sum(math.comb(48, i) * 0.1**i * 0.9 ** (48 - i) for i in range(3)), "ARITH")
    S.put("cp.upper95_2of48_pct", 100 * clopper_pearson(2, 48, 0.10)[1], "ARITH")
    # coverage of the 90% upper limits near the observed table (exact enumeration of all 2x2 tables)
    n = 48
    lf = [math.lgamma(i + 1) for i in range(n + 1)]
    tango_up = {}
    for b in range(n + 1):
        for c in range(n + 1 - b):
            tango_up[(b, c)] = tango_interval(b, c, n, Z90)[1]
    tables = []
    for a_ in range(n + 1):
        for b in range(n + 1 - a_):
            for c in range(n + 1 - a_ - b):
                dd = n - a_ - b - c
                tables.append((a_, b, c, dd, newcombe10(a_, b, c, dd, Z90)[1], tango_up[(b, c)]))
    S.put("coverage.tables", len(tables), "ARITH")
    miss_n, miss_t = [], []
    for q in (0.0, 1 / 48, 2 / 48):
        for m in (5, 7.5, 10, 12.5, 15):
            delta = m / 100
            probs = (27 / 48 - q, q + delta, q, 1 - (27 / 48 - q) - (q + delta) - q)
            logs = [math.log(x) if x > 0 else None for x in probs]
            pn = pt_ = tot = 0.0
            for a_, b, c, dd, nup, tup in tables:
                x = lf[n]
                skip = False
                for cnt, lg in zip((a_, b, c, dd), logs):
                    if cnt:
                        if lg is None:
                            skip = True
                            break
                        x += cnt * lg - lf[cnt]
                if skip:
                    continue
                pr = math.exp(x)
                tot += pr
                pn += pr * (nup < delta - 1e-12)
                pt_ += pr * (tup < delta - 1e-12)
            assert abs(tot - 1) < 1e-9
            miss_n.append(100 * pn)
            miss_t.append(100 * pt_)
    S.put("coverage.newcombe90_pct", [min(miss_n), max(miss_n)], "ARITH")
    S.put("coverage.tango90_pct", [min(miss_t), max(miss_t)], "ARITH")
    S.put("coverage.points", 15, "ARITH")


# =============================================================================================== cost
def cost(p, d, lab, S):
    """Table 1 rows 6-7, §5.3, Appendix D and Figure A1 data: model cost, tokens, time, sandbox."""
    final, tasks = p["final"], d["tasks"]
    billed, base = d["billed"], d["base"]
    for a in ARMS:
        S.put(f"cost.billed.total.{a}", sum(billed[a]), "REC")
        S.put(f"cost.billed.mean.{a}", statistics.fmean(billed[a]), "REC")
        S.put(f"cost.billed.median.{a}", statistics.median(billed[a]), "REC")
        S.put(f"cost.base.total.{a}", sum(base[a]), "PRICE")
        S.put(f"cost.base.mean.{a}", statistics.fmean(base[a]), "PRICE")
    S.put("total.billed_final", sum(sum(billed[a]) for a in ARMS), "REC")
    S.put("total.billed_all_records", sum((r.get("usage") or {}).get("cost_usd") or 0.0 for r in p["records"]), "REC")
    lance = "lance-format__lance-7835"
    il = tasks.index(lance)
    for team in TEAMS:
        for kind, xs, tag in (("billed", billed, "REC"), ("base", base, "PRICE")):
            S.put(f"ratio.{kind}.{team}", sum(xs[team]) / sum(xs["solo"]), tag)
            S.put(f"ratio.{kind}.{team}.boot95", boot_ratio_of_totals(xs[team], xs["solo"]), tag)
            per = [x / y for x, y in zip(xs[team], xs["solo"])]
            hi = sum(1 for r in per if r > 1)
            assert all(r != 1 for r in per)
            S.put(f"costlier.{kind}.{team}", hi, tag)
            S.put(f"sign_test.{kind}.{team}", sign_test(hi, 48 - hi), tag)
            g, ci = geomean_t(per)
            S.put(f"geomean.{kind}.{team}", g, tag)
            S.put(f"geomean.{kind}.{team}.t95", ci, tag)
            loo = [(sum(xs[team]) - xs[team][i]) / (sum(xs["solo"]) - xs["solo"][i]) for i in range(48)]
            S.put(f"loo.{kind}.{team}", [min(loo), max(loo)], tag)
            S.put(f"without_lance.{kind}.{team}", (sum(xs[team]) - xs[team][il]) / (sum(xs["solo"]) - xs["solo"][il]), tag)
            S.put(f"ratio_of_medians.{kind}.{team}", statistics.median(xs[team]) / statistics.median(xs["solo"]), tag)
            S.put(f"median_ratio.{kind}.{team}", statistics.median(per), tag)
        d.setdefault("per_ratio", {})[team] = {"billed": [x / y for x, y in zip(billed[team], billed["solo"])],
                                               "base": [x / y for x, y in zip(base[team], base["solo"])]}
        S.put(f"cheaper_tasks.billed.{team}", sorted(t for i, t in enumerate(tasks) if billed[team][i] < billed["solo"][i]), "REC")
    d["solo_base"] = base["solo"]
    # tokens
    tok = {a: {k: sum(final[a][t]["usage"][k] for t in tasks) for k in ("input_tokens", "cached_input_tokens", "output_tokens")} for a in ARMS}
    for a in ARMS:
        S.put(f"tokens.input.{a}", tok[a]["input_tokens"], "REC")
        S.put(f"tokens.output.{a}", tok[a]["output_tokens"], "REC")
        S.put(f"tokens.cache_read_share_pct.{a}", 100 * tok[a]["cached_input_tokens"] / tok[a]["input_tokens"], "REC")
    S.put("tokens.cache_read_share_pct.all", 100 * sum(tok[a]["cached_input_tokens"] for a in ARMS) / sum(tok[a]["input_tokens"] for a in ARMS), "REC")
    for team in TEAMS:
        S.put(f"tokens.ratio.input.{team}", tok[team]["input_tokens"] / tok["solo"]["input_tokens"], "REC")
        S.put(f"tokens.ratio.output.{team}", tok[team]["output_tokens"] / tok["solo"]["output_tokens"], "REC")
    # concentration and the one run
    for a in ARMS:
        i = max(range(48), key=lambda k: billed[a][k])
        S.put(f"costliest.share_billed_pct.{a}", 100 * billed[a][i] / sum(billed[a]), "REC")
        S.put(f"costliest.task.{a}", tasks[i], "REC")
    solo_lance = final["solo"][lance]
    assert len(solo_lance["runs"]) == 1
    S.put("lance7835.solo.cost_usd", solo_lance["usage"]["cost_usd"], "REC")
    S.put("lance7835.solo.input_tokens", solo_lance["usage"]["input_tokens"], "REC")
    S.put("lance7835.solo.billed_over_base", solo_lance["usage"]["cost_usd"] / base["solo"][il], "PRICE")
    S.put("lance7835.solo.share_billed_pct", 100 * billed["solo"][il] / sum(billed["solo"]), "REC")
    S.put("lance7835.solo.share_base_pct", 100 * base["solo"][il] / sum(base["solo"]), "PRICE")
    S.put("lance7835.solo.resolved", d["res"]["current"]["solo"][lance], "REC")
    # fit of every run to the price schedule
    runs = [(a, t, x) for a in ARMS for t in tasks for x in final[a][t]["runs"]]
    ratio = [x["usage"]["cost_usd"] / base_cost(x["usage"]) for _, _, x in runs]
    at_base = sum(1 for r in ratio if abs(r - 1) <= 1.3e-4)
    at_five = sum(1 for r in ratio if abs(r / 5 - 1) <= 1.3e-4)
    above = [(a, t, x) for (a, t, x), r in zip(runs, ratio) if r > 1 + 1.3e-4]
    S.put("pricefit.runs", len(runs), "PRICE")
    S.put("pricefit.at_base", at_base, "PRICE")
    S.put("pricefit.at_five", at_five, "PRICE")
    S.put("pricefit.between", len(runs) - at_base - at_five, "PRICE")
    S.put("pricefit.below_base", sum(1 for r in ratio if r < 1 - 1.3e-4), "PRICE")
    S.put("pricefit.ratio_range", [min(ratio), max(ratio)], "PRICE")
    S.put("pricefit.above_runs", len(above), "PRICE")
    S.put("pricefit.above_tasks", len({t for _, t, _ in above}), "PRICE")
    all_billed = sum(x["usage"]["cost_usd"] for _, _, x in runs)
    all_base = sum(base_cost(x["usage"]) for _, _, x in runs)
    S.put("pricefit.above_billed_share_pct", 100 * sum(x["usage"]["cost_usd"] for _, _, x in above) / all_billed, "PRICE")
    S.put("pricefit.higher_rate_share_pct", 100 * (all_billed - all_base) / all_billed, "PRICE")
    S.put("pricefit.billed_over_base_all", all_billed / all_base, "PRICE")
    # share of base-price cost billed at the higher rates, F = (billed / base - 1) / 4
    for a in ARMS:
        S.put(f"price_step.share_pct.{a}", 100 * (sum(billed[a]) / sum(base[a]) - 1) / 4, "PRICE")
    S.put("price_step.share_pct.solo_without_lance", 100 * ((sum(billed["solo"]) - billed["solo"][il]) / (sum(base["solo"]) - base["solo"][il]) - 1) / 4, "PRICE")
    resumed = [x for t in tasks for x in [y for y in final["review"][t]["runs"] if y["role"] == "coder"][1:]]
    S.put("price_step.resumed_coder_runs", len(resumed), "REC")
    S.put("price_step.share_pct.resumed_coder", 100 * (sum(x["usage"]["cost_usd"] for x in resumed) / sum(base_cost(x["usage"]) for x in resumed) - 1) / 4, "PRICE")
    # roles and revision rounds
    for team, roles in (("review", ("coder", "reviewer")), ("best", ("worker", "judge"))):
        for kind, f, tag in (("billed", lambda u: u["cost_usd"], "REC"), ("base", base_cost, "PRICE")):
            tot = sum(f(x["usage"]) for t in tasks for x in final[team][t]["runs"])
            S.put(f"share.role.{kind}.{team}_pct", [100 * sum(f(x["usage"]) for t in tasks for x in final[team][t]["runs"] if x["role"] == ro) / tot for ro in roles], tag)
    later = [x for t in tasks for x in final["review"][t]["runs"][2:]]
    S.put("revision.later_runs", len(later), "REC")
    S.put("revision.share_billed_pct", 100 * sum(x["usage"]["cost_usd"] for x in later) / sum(billed["review"]), "REC")
    S.put("revision.share_base_pct", 100 * sum(base_cost(x["usage"]) for x in later) / sum(base["review"]), "PRICE")
    S.put("revision.coder_share_billed_pct", 100 * sum(x["usage"]["cost_usd"] for x in resumed) / sum(billed["review"]), "REC")
    nine = [i for i, t in enumerate(tasks) if final["review"][t]["meta"]["rounds"] > 1]
    S.put("revision.nine_tasks.review_usd", sum(billed["review"][i] for i in nine), "REC")
    S.put("revision.nine_tasks.review_share_pct", 100 * sum(billed["review"][i] for i in nine) / sum(billed["review"]), "REC")
    S.put("revision.nine_tasks.solo_usd", sum(billed["solo"][i] for i in nine), "REC")
    one = [i for i in range(48) if i not in nine]
    S.put("revision.one_round.review_over_solo_billed", sum(billed["review"][i] for i in one) / sum(billed["solo"][i] for i in one), "REC")
    S.put("revision.one_round.review_over_solo_base", sum(base["review"][i] for i in one) / sum(base["solo"][i] for i in one), "PRICE")
    ih = tasks.index("xroche__httrack-789")
    S.put("httrack789.review_cost", billed["review"][ih], "REC")
    S.put("httrack789.multiple_of_solo", billed["review"][ih] / billed["solo"][ih], "REC")
    # the five same-configuration rollouts per task and the band between them
    roll = {"solo": [], "coder1": [], "worker1": [], "worker2": [], "worker3": []}
    for t in tasks:
        roll["solo"].append(final["solo"][t]["runs"][0])
        c1 = final["review"][t]["runs"][0]
        assert c1["role"] == "coder"
        roll["coder1"].append(c1)
        b = final["best"][t]
        for k, cand in enumerate(b["meta"]["candidates"]):
            roll[f"worker{k + 1}"].append(b["runs"][cand["runs"][0]])
    band = {"billed_total": 1.0, "base_total": 1.0, "billed_geomean": 1.0, "base_geomean": 1.0}
    for x, y in itertools.combinations(roll, 2):
        for kind, f in (("billed", lambda u: u["cost_usd"]), ("base", base_cost)):
            xs = [f(r["usage"]) for r in roll[x]]
            ys = [f(r["usage"]) for r in roll[y]]
            rt = sum(xs) / sum(ys)
            gm = math.exp(statistics.fmean(math.log(u / v) for u, v in zip(xs, ys)))
            band[f"{kind}_total"] = max(band[f"{kind}_total"], rt, 1 / rt)
            band[f"{kind}_geomean"] = max(band[f"{kind}_geomean"], gm, 1 / gm)
    for k, v in band.items():
        S.put(f"band.same_config.{k}", v, "PRICE" if k.startswith("base") else "REC")
    for k, v in roll.items():
        S.put(f"band.rollout_total_billed.{k}", sum(r["usage"]["cost_usd"] for r in v), "REC")
    # run order
    order = sum(1 for t in tasks if final["solo"][t]["started_at"] < final["review"][t]["started_at"] < final["best"][t]["started_at"])
    S.put("run_order.solo_review_best", order, "REC")
    S.put("run_order.solo_first", sum(1 for t in tasks if final["solo"][t]["started_at"] < min(final["review"][t]["started_at"], final["best"][t]["started_at"])), "REC")
    unc = {k: [r["usage"]["input_tokens"] - r["usage"]["cached_input_tokens"] for r in v] for k, v in roll.items()}
    others = [statistics.fmean([unc[k][i] for k in ("coder1", "worker1", "worker2", "worker3")]) for i in range(48)]
    g, ci = geomean_t([s / o for s, o in zip(unc["solo"], others)])
    S.put("run_order.uncached_ratio", g, "REC")
    S.put("run_order.uncached_ratio.t95", ci, "REC")
    S.put("run_order.uncached_ratio_totals", sum(unc["solo"]) / sum(others), "REC")
    S.put("run_order.solo_higher", sum(1 for s, o in zip(unc["solo"], others) if s > o), "REC")
    excess = (sum(unc["solo"]) - sum(others)) * (PRICE["uncached"] - PRICE["cache_read"]) / 1e6
    S.put("run_order.excess_as_reads_usd", excess, "PRICE")
    S.put("run_order.solo_base_drop_pct", 100 * excess / sum(base["solo"]), "PRICE")
    S.put("run_order.understatement_factor", sum(base["solo"]) / (sum(base["solo"]) - excess), "PRICE")
    # wall time and sandbox
    dur = {a: [final[a][t]["duration_s"] for t in tasks] for a in ARMS}
    for a in ARMS:
        S.put(f"time.median_s.{a}", statistics.median(dur[a]), "REC")
        S.put(f"time.mean_s.{a}", statistics.fmean(dur[a]), "REC")
    for team in TEAMS:
        S.put(f"time.slower.{team}", sum(1 for x, y in zip(dur[team], dur["solo"]) if x > y), "REC")
        g, ci = geomean_t([x / y for x, y in zip(dur[team], dur["solo"])])
        S.put(f"time.geomean_ratio.{team}", g, "REC")
        S.put(f"time.geomean_ratio.{team}.t95", ci, "REC")
    longest = max(((final[a][t]["duration_s"], a, t) for a in ARMS for t in tasks))
    S.put("time.longest_min", longest[0] / 60, "REC")
    S.put("time.longest_pair", [longest[2], longest[1]], "REC")
    S.put("time.reached_limit", sum(1 for a in ARMS for t in tasks if final[a][t]["duration_s"] >= 3600), "REC")
    forked = {"solo": set(), "review": {"reviewer"}, "best": {"worker", "judge"}}
    solve_s = {a: [final[a][t]["duration_s"] + sum(x["duration_s"] or 0.0 for x in final[a][t]["runs"] if x["role"] in forked[a]) for t in tasks] for a in ARMS}
    grade_s = {a: [final[a][t]["eval"]["duration_s"] for t in tasks] for a in ARMS}
    msb = {}
    for a in ARMS:
        S.put(f"sandbox.solve_hours.{a}", sum(solve_s[a]) / 3600, "REC")
        S.put(f"sandbox.grading_hours.{a}", sum(grade_s[a]) / 3600, "REC")
        msb[a] = [b_ + s / 3600 * SANDBOX_USD_PER_HOUR for b_, s in zip(billed[a], solve_s[a])]
        S.put(f"per_task.model_plus_sandbox.{a}", sum(msb[a]) / 48, "ARITH")
    for team in TEAMS:
        S.put(f"ratio.model_plus_sandbox.{team}", sum(msb[team]) / sum(msb["solo"]), "ARITH")
        S.put(f"ratio.model_plus_sandbox.{team}.boot95", boot_ratio_of_totals(msb[team], msb["solo"]), "ARITH")
    allg = [x / 60 for a in ARMS for x in grade_s[a]]
    S.put("grading.minutes_median", statistics.median(allg), "REC")
    S.put("grading.minutes_p90", quantile7(allg, 0.9), "REC")
    d["sandbox_per_task"] = {a: sum(msb[a]) / 48 for a in ARMS}
    d["grading_hours"] = sum(sum(grade_s[a]) for a in ARMS) / 3600


# =============================================================================================== verifier
def verifier(p, d, lab, S):
    """Table 2, §6, Table F1 and Figure A3 data: reviewer and judge verdicts against outcomes."""
    final, tasks, res, cls = p["final"], d["tasks"], d["res"]["current"], d["cls"]
    rounds = Counter(final["review"][t]["meta"]["rounds"] for t in tasks)
    S.put("review.rounds", [rounds[1], rounds[2], rounds[3]], "REC")
    S.put("review.reviews", sum(len(d["review_v"][t]) for t in tasks), "REC")
    S.put("review.passing", sum(v[1] for t in tasks for v in d["review_v"][t]), "REC")
    S.put("review.rejections", sum(not v[1] for t in tasks for v in d["review_v"][t]), "REC")
    first_pass = {t: d["review_v"][t][0][1] for t in tasks}
    assert all(first_pass[t] == (final["review"][t]["meta"]["rounds"] == 1) for t in tasks)
    accepted = {t: bool(final["review"][t]["meta"]["accepted"]) for t in tasks}
    assert all(accepted[t] == any(v[1] for v in d["review_v"][t]) for t in tasks)
    S.put("review.first_approved", sum(first_pass.values()), "REC")
    S.put("review.first_approved_pct", 100 * sum(first_pass.values()) / 48, "REC")
    S.put("review.final_approved", sum(accepted.values()), "REC")
    S.put("review.not_approved_tasks", sorted(t for t in tasks if not accepted[t]), "REC")
    # every loop ended on an approval or after three reviewed rounds, so the no-change stop never ended one
    S.put("review.ended_by_approval_or_three_rounds", all((accepted[t] or final["review"][t]["meta"]["rounds"] == 3)
                                                          and len(final["review"][t]["meta"]["scores"]) == final["review"][t]["meta"]["rounds"]
                                                          for t in tasks), "REC")

    def xtab(pred, out, sub):
        return [sum(1 for t in sub if pred[t] and out[t]), sum(1 for t in sub if pred[t] and not out[t]),
                sum(1 for t in sub if not pred[t] and out[t]), sum(1 for t in sub if not pred[t] and not out[t])]

    rv, bv = res["review"], res["best"]
    jflag = {t: d["judge_v"][t][final["best"][t]["meta"]["chosen"] - 1][1] for t in tasks}
    for name, pred, out in (("review.final", accepted, rv), ("review.first", first_pass, rv), ("judge.flag", jflag, bv)):
        tab = xtab(pred, out, tasks)
        S.put(f"{name}_x_outcome", tab, "REC")
        S.put(f"{name}.precision_pct", 100 * tab[0] / (tab[0] + tab[1]), "REC")
        S.put(f"{name}.right", tab[0] + tab[3], "REC")
        S.put(f"{name}.share_resolved_pct", 100 * (tab[0] + tab[2]) / 48, "REC")
        S.put(f"{name}.pass_all_right", tab[0] + tab[2], "REC")
        for c in ("A", "B", "C", None):
            sub = [t for t in tasks if cls[t] == c]
            S.put(f"byclass.{name}.{c or 'unflagged'}", xtab(pred, out, sub), "LAB")
    S.put("judge.passed_flags_true", sum(v[1] for t in tasks for v in d["judge_v"][t]), "REC")
    # ties and choice
    top = Counter()
    chosen = Counter()
    tie_c1 = unique_c1 = tie_to_c2 = 0
    tie_resolved = Counter()
    for t in tasks:
        sc = final["best"][t]["meta"]["scores"]
        m = max(sc)
        k = sc.count(m)
        top[k] += 1
        ch = final["best"][t]["meta"]["chosen"]
        assert ch == sc.index(m) + 1
        chosen[ch] += 1
        if ch == 1 and k > 1:
            tie_c1 += 1
        if ch == 1 and k == 1:
            unique_c1 += 1
        if ch == 2 and k > 1:
            tie_to_c2 += 1
        tie_resolved[k] += bv[t]
    S.put("judge.top", [top[1], top[2], top[3]], "REC")
    S.put("judge.tied", top[2] + top[3], "REC")
    S.put("judge.chosen", [chosen[1], chosen[2], chosen[3]], "REC")
    S.put("judge.tiebreak_c1", tie_c1, "REC")
    S.put("judge.unique_c1", unique_c1, "REC")
    S.put("judge.tiebreak_c2", tie_to_c2, "REC")
    S.put("judge.resolved_by_top", [tie_resolved[1], tie_resolved[2], tie_resolved[3]], "REC")
    # scores and AUC
    ret = {t: final["review"][t]["meta"]["scores"][final["review"][t]["meta"]["best_round"] - 1] for t in tasks}
    first_sc = {t: final["review"][t]["meta"]["scores"][0] for t in tasks}
    chosen_sc = {t: final["best"][t]["meta"]["scores"][final["best"][t]["meta"]["chosen"] - 1] for t in tasks}
    d["scores"] = {"review_returned": ret, "judge_all": {t: final["best"][t]["meta"]["scores"] for t in tasks}, "judge_chosen": chosen_sc}
    for name, sc, out in (("review_final", ret, rv), ("review_first", first_sc, rv), ("judge_chosen", chosen_sc, bv)):
        pairs = [(sc[t], out[t]) for t in tasks]
        S.put(f"auc.{name}", auc(pairs), "REC")
        ci, used = boot_auc(pairs)
        S.put(f"auc.{name}.boot95", ci, "REC")
        S.put(f"auc.{name}.boot_used", used, "REC")
        S.put(f"scores.means_by_outcome.{name}", [statistics.fmean([sc[t] for t in tasks if out[t]]), statistics.fmean([sc[t] for t in tasks if not out[t]])], "REC")
    unfl = [t for t in tasks if cls[t] is None]
    S.put("unflagged.n", len(unfl), "LAB")
    S.put("unflagged.review_final", xtab(accepted, rv, unfl), "LAB")
    S.put("unflagged.review_first", xtab(first_pass, rv, unfl), "LAB")
    S.put("unflagged.judge_flag", xtab(jflag, bv, unfl), "LAB")
    for name, sc, out in (("review_final", ret, rv), ("judge_chosen", chosen_sc, bv)):
        pairs = [(sc[t], out[t]) for t in unfl]
        S.put(f"unflagged.auc.{name}", auc(pairs), "LAB")
        ci, used = boot_auc(pairs)
        S.put(f"unflagged.auc.{name}.boot95", ci, "LAB")
        S.put(f"unflagged.auc.{name}.boot_used", used, "LAB")
    S.put("classC.approved_resolved", sum(1 for t in tasks if cls[t] == "C" and accepted[t] and rv[t]), "LAB")
    au = [t for t in tasks if accepted[t] and not rv[t]]
    partial = set(lab["partial_apply"])
    cause = Counter("A" if cls[t] == "A" else "B" if cls[t] == "B" else "C" if cls[t] == "C" else "partial" if t in partial else "other" for t in au)
    S.put("nineteen.n", len(au), "REC")
    S.put("nineteen.by_cause", [cause["A"], cause["B"], cause["partial"], cause["other"], cause["C"]], "LAB")
    allfail = [t for t in tasks if not any(res[a][t] for a in ARMS)]
    S.put("nineteen.equal_all_fail_tasks", sorted(au) == sorted(allfail), "REC")
    # distributions (Figure A3)
    S.put("scores.review_returned", dict(sorted(Counter(str(ret[t]) for t in tasks).items())), "REC")
    allj = [s for t in tasks for s in final["best"][t]["meta"]["scores"]]
    S.put("scores.judge_all", dict(sorted(Counter(str(s) for s in allj).items())), "REC")
    S.put("scores.judge_mean", statistics.fmean(allj), "REC")
    S.put("scores.judge_median", statistics.median(allj), "REC")
    S.put("scores.judge_ge8_pct", 100 * sum(1 for s in allj if s >= 8) / len(allj), "REC")
    # between-task share of judge-score variance, ICC(1), correlation with the reviewer
    grand = statistics.fmean(allj)
    means = {t: statistics.fmean(final["best"][t]["meta"]["scores"]) for t in tasks}
    ssb = sum(3 * (means[t] - grand) ** 2 for t in tasks)
    ssw = sum((s - means[t]) ** 2 for t in tasks for s in final["best"][t]["meta"]["scores"])
    msb, msw = ssb / 47, ssw / (48 * 2)
    S.put("scores.between_share_pct", 100 * ssb / (ssb + ssw), "REC")
    S.put("scores.icc", (msb - msw) / (msb + 2 * msw), "REC")
    S.put("scores.r_review_judge", pearson([first_sc[t] for t in tasks], [means[t] for t in tasks]), "REC")
    # change requests
    cr = [t for t in tasks if final["review"][t]["meta"]["rounds"] > 1]
    one = [t for t in tasks if t not in cr]
    S.put("change_requests.tasks", len(cr), "REC")
    S.put("change_requests.resolved", [sum(res["solo"][t] for t in cr), sum(rv[t] for t in cr)], "REC")
    S.put("change_requests.same_set", {t for t in cr if res["solo"][t]} == {t for t in cr if rv[t]}, "REC")
    S.put("change_requests.one_round_resolved", [sum(res["solo"][t] for t in one), sum(rv[t] for t in one)], "REC")
    S.put("change_requests.first_rule_resolved", [sum(d["res"]["first"]["solo"][t] for t in cr), sum(d["res"]["first"]["review"][t] for t in cr)], "REC")


# =============================================================================================== noise and power
def noise_power(p, d, lab, S):
    """Table 3 (power), §7, Appendix E and Figure A2 data: rollout noise, power, clustering, planning."""
    final, tasks, res = p["final"], d["tasks"], d["res"]
    r1 = [t for t in tasks if final["review"][t]["meta"]["rounds"] == 1]
    S.put("noise.r1_tasks", len(r1), "REC")
    for rule, key in (("current", "noise.replicate"), ("first", "noise.replicate_first_rule")):
        k = sum(1 for t in r1 if res[rule]["review"][t] != res[rule]["solo"][t])
        S.put(f"{key}.disagree", k, "REC")
        S.put(f"{key}.pct", 100 * k / len(r1), "REC")
        S.put(f"{key}.wilson95_pct", [100 * x for x in wilson(k, len(r1))], "REC")
    k = S.values["noise.replicate.disagree"]
    psi0 = k / len(r1)
    wl, wh = wilson(k, len(r1))
    S.put("noise.replicate.tasks_are_team_wins", sorted(t for t in r1 if res["current"]["review"][t] != res["current"]["solo"][t]) == S.values["discordant.review_wins"], "REC")
    sd = [100 * math.sqrt(x / 48) for x in (psi0, wl, wh)]
    S.put("noise.sd_diff_pts", sd[0], "ARITH")
    S.put("noise.sd_diff_wilson_pts", sd[1:], "ARITH")
    S.put("noise.gain_in_sd", [S.values["pair.review.diff_pts"] / sd[0], S.values["pair.best.diff_pts"] / sd[0]], "ARITH")

    def ahead(dd, net):
        pw = dd / 2
        return sum(math.factorial(48) / (math.factorial(w) * math.factorial(lo) * math.factorial(48 - w - lo)) * pw ** (w + lo) * (1 - dd) ** (48 - w - lo)
                   for w in range(49) for lo in range(49 - w) if w - lo >= net)

    S.put("noise.p_second_ahead2", ahead(psi0, 2), "ARITH")
    S.put("noise.p_second_ahead2_wilson", [ahead(wl, 2), ahead(wh, 2)], "ARITH")
    S.put("noise.p_second_ahead1", ahead(psi0, 1), "ARITH")
    # a second accidental set: best of 3 returned candidate 1, a rollout of the solo configuration, by the tie-break
    tb = [t for t in tasks if final["best"][t]["meta"]["chosen"] == 1 and final["best"][t]["meta"]["scores"].count(max(final["best"][t]["meta"]["scores"])) > 1]
    S.put("noise.tiebreak_c1.tasks", len(tb), "REC")
    S.put("noise.tiebreak_c1.disagree", sum(1 for t in tb if res["current"]["best"][t] != res["current"]["solo"][t]), "REC")
    # the replicate disagreements are review's own discordant tasks, both returned from round 1
    S.put("noise.replicate.review_wins_round1", all(final["review"][t]["meta"]["best_round"] == 1 for t in S.values["discordant.review_wins"]), "REC")
    # power of the exact McNemar test (and of its mid-p version)
    pw = McNemarPower(1000)
    S.put("power.min_discordant", min(D for D in range(1, 30) if pw.crit[D] is not None), "ARITH")
    S.put("power.min_discordant_p", mcnemar_exact(6, 0), "ARITH")
    pm = McNemarPower(1000, "midp")
    S.put("power.midp.min_discordant", min(D for D in range(1, 30) if pm.crit[D] is not None), "ARITH")
    S.put("power.midp.min_discordant_p", mcnemar_midp(S.values["power.midp.min_discordant"], 0), "ARITH")
    S.put("power.asymptotic.min_discordant", min(D for D in range(1, 30) if 2 * (1 - statistics.NormalDist().cdf(math.sqrt(D))) < 0.05), "ARITH")
    noises = {"0": 0.0, "2of39": psi0, "6of39": S.values["noise.replicate_first_rule.disagree"] / len(r1), "wilson_hi": wh}
    S.put("power.noise_levels", noises, "ARITH")
    for g in (5, 10, 15, 20):
        S.put(f"power.n48.{g}", pw.power(48, psi0 / 2 + g / 100, psi0 / 2), "ARITH")
    for name in ("0", "2of39", "wilson_hi"):
        nz = noises[name]
        mde = next(i / 1000 for i in range(1, 600) if pw.power(48, nz / 2 + i / 1000, nz / 2) >= 0.8)
        S.put(f"power.min_gain80_pts.{name}", 100 * mde, "ARITH")
    S.put("power.size_n48", 2 * pw.power(48, psi0 / 2, psi0 / 2), "ARITH")
    S.put("power.p_ge6_discordant_pct", 100 * sum(math.comb(48, i) * psi0**i * (1 - psi0) ** (48 - i) for i in range(6, 49)), "ARITH")
    n80 = {}
    for name, nz in noises.items():
        for g in (5, 10, 15):
            n80[(name, g)] = pw.n_for(nz / 2 + g / 100, nz / 2)
            S.put(f"power.n80.{name}.{g}", n80[(name, g)], "ARITH")
    for psi in (0.20, 0.30):
        for g in (5, 10, 15):
            S.put(f"power.n80_fixed_psi.{int(psi * 100)}.{g}", pw.n_for((psi + g / 100) / 2, (psi - g / 100) / 2), "ARITH")
    # the same under the mid-p test
    for g in (5, 10, 15, 20):
        S.put(f"power.midp.n48.{g}", pm.power(48, psi0 / 2 + g / 100, psi0 / 2), "ARITH")
    S.put("power.midp.size_n48", 2 * pm.power(48, psi0 / 2, psi0 / 2), "ARITH")
    for name in ("0", "2of39", "wilson_hi"):
        nz = noises[name]
        mde = next(i / 1000 for i in range(1, 600) if pm.power(48, nz / 2 + i / 1000, nz / 2) >= 0.8)
        S.put(f"power.midp.min_gain80_pts.{name}", 100 * mde, "ARITH")
        for g in (5, 10, 15):
            S.put(f"power.midp.n80.{name}.{g}", pm.n_for(nz / 2 + g / 100, nz / 2), "ARITH")
    d["power"] = pw
    d["noises"] = noises
    # clustering by repository
    for team in TEAMS:
        by = defaultdict(list)
        for t in tasks:
            by[d["repo"][t]].append(int(res["current"][team][t]) - int(res["current"]["solo"][t]))
        loo = [100 * (sum(sum(v) for v in by.values()) - sum(by[g])) / (48 - len(by[g])) for g in by]
        S.put(f"cluster.loo_repo.{team}_pts", [min(loo), max(loo)], "REC")
        S.put(f"cluster.cr95.{team}_pts", [100 * x for x in cluster_robust(by, 0.95)[1]], "REC")
        repos = sorted(by)
        nonzero = [g for g in repos if sum(by[g])]
        S.put(f"cluster.nonzero_repos.{team}", len(nonzero), "REC")
        if all(sum(by[g]) > 0 for g in nonzero):
            S.put(f"cluster.boot_zero_share_exact_pct.{team}", 100 * ((len(repos) - len(nonzero)) / len(repos)) ** len(repos), "REC")
        # the repository bootstrap, exact over all resamples of the 23 repositories
        dist = cluster_boot_exact(sorted(((sum(by[g]), len(by[g])) for g in repos), key=lambda c: (c[0] != 0, c)))
        S.put(f"cluster.repo_boot95.{team}_pts", exact_limits(dist, Fraction(1, 40)), "REC")
        S.put(f"cluster.repo_boot90.{team}_pts", exact_limits(dist, Fraction(1, 20)), "REC")
        S.put(f"cluster.repo_boot_le0_pct.{team}", float(100 * sum(pr for x, pr in dist if x <= 0)), "REC")
    # planning arithmetic
    pool = int(lab["pool.tasks"][""])
    S.put("plan.valid_expected", pool * S.values["gold.valid"] / S.values["gold.sampled"], "LAB")
    # the same yield on the pool rows the pilot did not sample
    S.put("plan.valid_fresh", (pool - S.values["gold.sampled"]) * S.values["gold.valid"] / S.values["gold.sampled"], "LAB")
    # and at the Wilson 95% limits of that validation rate
    S.put("plan.valid_fresh_wilson95", [(pool - S.values["gold.sampled"]) * x for x in wilson(S.values["gold.valid"], S.values["gold.sampled"])], "LAB")
    per = sum(d["sandbox_per_task"].values())
    S.put("plan.three_arms_per_task", per, "ARITH")
    for n in (72, 124, 341):
        S.put(f"plan.cost_three_arms.{n}", n * per, "ARITH")
    S.put("plan.grading_per_task", d["grading_hours"] / 48 * SANDBOX_USD_PER_HOUR, "ARITH")


# =============================================================================================== validity
def validity(p, d, lab, S):
    """§5.2, §8, Appendix C and Table C1: validity classes, pool rates, dates, files, trailers, selection, leakage."""
    tasks, res, cls = d["tasks"], d["res"]["current"], d["cls"]
    counts = Counter(cls[t] for t in tasks)
    for c in ("A", "B", "C"):
        S.put(f"class.counts.{c}", counts[c], "LAB")
        S.put(f"class.share_pct.{c}", 100 * counts[c] / len(tasks), "LAB")
        sub = [t for t in tasks if cls[t] == c]
        S.put(f"class.outcomes.{c}", [sum(res[a][t] for t in sub) for a in ARMS], "LAB")
    S.put("class.counts.flagged", sum(counts[c] for c in ("A", "B", "C")), "LAB")
    S.put("class.outcomes.unflagged", [sum(res[a][t] for t in tasks if cls[t] is None) for a in ARMS], "LAB")
    kinds = Counter(lab["class_a_kind"].values())
    S.put("class.a_kind", [kinds["introduces"], kinds["makes accessible"]], "LAB")
    S.put("class.decidable_from_rows", counts["B"] + counts["C"], "LAB")
    S.put("class.a_static_screen", [int(lab["class_a.static_screen_confirmed"][""]), int(lab["class_a.static_screen_flagged"][""])], "LAB")
    pool = int(lab["pool.tasks"][""])
    S.put("pool.tasks", pool, "LAB")
    S.put("pool.repos", int(lab["pool.repos"][""]), "LAB")
    for key, k in (("pool.class_b", int(lab["pool.class_b"][""])), ("pool.class_c_checked", int(lab["pool.class_c_checked"][""]))):
        S.put(f"{key}.count", k, "LAB")
        S.put(f"{key}.pct", 100 * k / pool, "LAB")
        S.put(f"{key}.wilson95_pct", [100 * x for x in wilson(k, pool)], "LAB")
    S.put("pool.class_b_rule", int(lab["pool.class_b_rule"][""]), "LAB")
    S.put("pool.class_c_rule", int(lab["pool.class_c_rule"][""]), "LAB")
    rust = int(lab["pool.rust_tasks"][""])
    S.put("pool.class_b_rust", [int(lab["pool.class_b_rust"][""]), rust], "LAB")
    rust48 = [t for t in tasks if d["lang"][t] == "rust"]
    S.put("class.rust_flagged", [sum(1 for t in rust48 if cls[t]), len(rust48)], "LAB")
    S.put("class.discordant_flagged", [sum(1 for t in d["discordant"] if cls[t]), len(d["discordant"])], "LAB")
    third = lab["class_a_thirdparty_symbol"]
    S.put("class.a_thirdparty", [sum(1 for v in third.values() if v == "yes"), sum(1 for v in third.values() if v != "not covered")], "LAB")
    beh = Counter(lab["all_fail_issue_states_test"].values())
    S.put("class.behavioural_not_stated", [beh["not stated"], sum(beh.values())], "LAB")
    # issue and pull-request dates
    issue = lab["issue_created"]
    early = [t for t in tasks if issue[t] < "2026-07-01"]
    S.put("issues.before_cutoff", len(early), "LAB")
    S.put("issues.before_cutoff_ids", sorted(early), "LAB")
    S.put("issues.resolved_each", [sum(res[a][t] for t in early) for a in ARMS], "LAB")
    S.put("issues.after_pr", sorted(t for t in tasks if issue[t] > p["final"]["solo"][t]["created_at"].replace("+00:00", "Z")), "LAB")
    pr = lab["pr_created_checked"]
    assert all(pr[t] == p["final"]["solo"][t]["created_at"].replace("+00:00", "Z") for t in pr)
    S.put("pr_date.checked", len(pr), "LAB")
    # repository instruction files and fix-commit trailers
    inst = Counter(lab["root_instruction_file"][t] for t in tasks)
    S.put("instruction_files.claude_md", inst["CLAUDE.md"], "LAB")
    S.put("instruction_files.agents_md_only", inst["AGENTS.md"], "LAB")
    S.put("instruction_files.either", inst["CLAUDE.md"] + inst["AGENTS.md"], "LAB")
    S.put("instruction_files.langchain4j_none", all(lab["root_instruction_file"][t] == "none" for t in tasks if d["repo"][t] == "langchain4j/langchain4j"), "LAB")
    S.put("repo_hooks.tasks_with_hook", int(lab["repo_hooks.tasks_with_hook"][""]), "LAB")
    # final messages (full text) that name either file; reviewer and judge messages are verdicts
    named = [(t, role) for a in ARMS for t in tasks for role, m in p["full"][a][t] if INSTRUCTION_FILE.search(m)]
    S.put("instruction_files.messages_citing", len(named), "REC")
    S.put("instruction_files.tasks_citing", len({t for t, _ in named}), "REC")
    S.put("instruction_files.verdicts_citing", sum(1 for _, role in named if role in ("reviewer", "judge")), "REC")
    S.put("instruction_files.citing_tasks_have_file", all(lab["root_instruction_file"][t] != "none" for t, _ in named), "LAB")
    tr = [t for t in tasks if lab["fix_trailer_credits_model"][t] != "none found"]
    S.put("trailer_credit.tasks", len(tr), "LAB")
    S.put("trailer_credit.resolved_each", [sum(res[a][t] for t in tr) for a in ARMS], "LAB")
    # selection by reference-patch validation
    dropped = S.values["gold.sampled"] - S.values["gold.valid"]
    S.put("selection.dropped", dropped, "REC")
    S.put("selection.rust_dropped", int(lab["selection.rust_dropped"][""]), "LAB")
    S.put("selection.rust_share_kept_pct", 100 * len(rust48) / 48, "REC")
    S.put("selection.rust_share_pool_pct", 100 * rust / pool, "LAB")
    S.put("selection.dropped_as_unresolved_pct", [100 * sum(res[a].values()) / S.values["gold.sampled"] for a in ARMS], "REC")
    # values read from labels as they are
    for key in ("dataset.issue_chars_median", "dataset.ref_lines_median", "dataset.ref_lines_q1", "dataset.ref_lines_q3",
                "dataset.ref_lines_min", "dataset.ref_lines_max", "dataset.ref_single_file", "dataset.dropped_ref_lines_median",
                "leak.pairs_compared", "leak.verbatim_median", "leak.pairs_ge_half", "leak.tasks_ge_half", "leak.ge_half_failing",
                "leak.online_build_runs", "leak.online_build_tasks", "background.messages",
                "review.approved_unresolved_reports_tests", "review.approved_resolved_reports_tests",
                "review.approved_unresolved_says_not_run", "review.rejections_functional", "review.rejections_tests_only"):
        v = lab[key][""]
        S.put("label." + key, float(v) if "." in v else int(v), "LAB")


# =============================================================================================== appendix extras
CASE_TASKS = ("langchain4j__langchain4j-5806", "langchain4j__langchain4j-5869", "nunit__nunit-5351",
              "JasperFx__wolverine-3695", "xroche__httrack-789")


def extras(p, d, lab, S):
    """Appendix values: start times of the records (Appendix H, I), the margins of the verdict tables
    (Table 2, Appendix F), discordant and agreeing task counts, and the five case studies (Appendix F)."""
    final, tasks, res = p["final"], d["tasks"], d["res"]["current"]

    def stamp(iso):
        return iso[:19] + "Z"

    runs = p["meta"]["runs"]
    for k in ("since", "gold_file", "max_cost_usd", "allow_unpriced", "isolate_history", "check_base_commit", "backend"):
        assert all(r[k] == runs[0][k] for r in runs)
        S.put(f"run.{k}", runs[0][k], "REC")
    sup = sorted(r["started_at"] for r in p["superseded"])
    S.put("superseded.started_range", [stamp(sup[0]), stamp(sup[-1])], "REC")
    for day, key in (("2026-10-29", "day1"), ("2026-10-31", "day2")):
        st = sorted(final[a][t]["started_at"] for a in ARMS for t in tasks if final[a][t]["started_at"].startswith(day))
        S.put(f"final.started_range.{key}", [stamp(st[0]), stamp(st[-1])], "REC")
    # tasks with a kept record for every arm from the first invocation: what the partial publication covered
    part = [t for t in tasks if all(final[a][t]["started_at"].startswith("2026-10-29") for a in ARMS)]
    S.put("partial.tasks", len(part), "REC")
    S.put("partial.resolved_first_rule", [sum(d["res"]["first"][a][t] for t in part) for a in ARMS], "REC")
    # margins of the three verdict tables: verdicts passed, outcomes unresolved
    for name in ("review.final", "review.first", "judge.flag"):
        tab = S.values[f"{name}_x_outcome"]
        S.put(f"{name}.passed", tab[0] + tab[1], "REC")
        S.put(f"{name}.unresolved", tab[1] + tab[3], "REC")
    for name in ("review_final", "review_first", "judge_flag"):
        tab = S.values[f"unflagged.{name}"]
        S.put(f"unflagged.{name}.passed", tab[0] + tab[1], "LAB")
        S.put(f"unflagged.{name}.unresolved", tab[1] + tab[3], "LAB")
    for team in TEAMS:
        S.put(f"pair.{team}.discordant", S.values[f"pair.{team}.wins"] + S.values[f"pair.{team}.losses"], "REC")
    S.put("overlap.agree", sum(1 for t in tasks if len({res[a][t] for a in ARMS}) == 1), "REC")
    # the five case studies: per arm the verdict, apply status, required-test counts and verifier scores
    for t in CASE_TASKS:
        for a in ARMS:
            r = final[a][t]
            rep = r["eval"]["report"]
            S.put(f"case.{t}.{a}.resolved", bool(res[a][t]), "REC")
            S.put(f"case.{t}.{a}.applied", bool(r["eval"]["patch_applied"]), "REC")
            S.put(f"case.{t}.{a}.f2p", [len(rep["FAIL_TO_PASS"][k]) for k in ("success", "failure", "missing")], "REC")
            S.put(f"case.{t}.{a}.p2p_failed", len(rep["PASS_TO_PASS"]["failure"]), "REC")
        S.put(f"case.{t}.review.scores", final["review"][t]["meta"]["scores"], "REC")
        S.put(f"case.{t}.review.returned_round", final["review"][t]["meta"]["best_round"], "REC")
        S.put(f"case.{t}.best.scores", final["best"][t]["meta"]["scores"], "REC")
        S.put(f"case.{t}.best.chosen", final["best"][t]["meta"]["chosen"], "REC")


# =============================================================================================== expectations
def check_expectations(S):
    """Compare the computed values with the expected values stored here, at their printed precision.
    Soft expectations are bootstrap limits obtained with seeds 1 and 2 at the same resample count: listed, not enforced."""
    E = S.expect
    for k, v in (("records.total", 251), ("records.final", 144), ("records.superseded", 107), ("superseded.no_model", 106),
                 ("superseded.mid_run", 1), ("superseded.with_cost", 4), ("superseded.graded_resolved", 2),
                 ("final.started_day1", 34), ("final.started_day2", 110), ("runs.by_role.solo_worker", 48),
                 ("runs.by_role.review_coder", 61), ("runs.by_role.review_reviewer", 61), ("runs.by_role.best_worker", 144),
                 ("runs.by_role.best_judge", 144), ("runs.exit0", 458), ("tasks.n", 48), ("repos.n", 23),
                 ("repos.top2", [10, 7]), ("gold.sampled", 70), ("gold.valid", 48), ("gold.invalid", 21), ("gold.error", 1),
                 ("review.patch_over_40k", 3), ("verdicts.parsed", 205), ("verdicts.nowrite_cited", 11), ("tasks.languages.Java", 18), ("tasks.languages.C#", 9),
                 ("tasks.languages.C", 8), ("tasks.languages.Rust", 8), ("tasks.languages.JavaScript", 3), ("tasks.languages.C++", 2),
                 ("superseded.swap_resolved.solo", 27), ("superseded.swap_resolved.review", 29), ("superseded.swap_resolved.best", 28),
                 ("gold.invalid_by_failure.f2p_failed", 7), ("gold.invalid_by_failure.p2p_only", 12), ("gold.invalid_by_failure.none", 2),
                 ("redaction.flagged", 12), ("tasks.f2p_one", 16), ("tasks.f2p_over100", 14), ("tasks.f2p_max", 3953),
                 ("selection.repos_sampled", 32), ("selection.repos_mixed", 4)):
        E(k, v)
    E("superseded.cost_usd", 1.14, 2)
    E("superseded.graded", 4)
    E("superseded.graded_same_verdict", 4)
    E("repos.top2_share_pct", 35.4, 1)
    E("repos.eff_clusters", 11.1, 1)
    E("tasks.age_days_range", [94.4, 120.2], 1)
    E("tasks.age_days_median", 105.3, 1)
    E("tasks.f2p_median", 3)
    E("tasks.p2p_median", 2410)
    # B
    for a, k, pct, wil in (("solo", 27, 56.3, [42.3, 69.3]), ("review", 29, 60.4, [46.3, 73.0]), ("best", 28, 58.3, [44.3, 71.2])):
        E(f"resolved.{a}.count", k)
        E(f"resolved.{a}.pct", pct, 1)
        E(f"resolved.{a}.wilson95_pct", wil, 1)
    for team, w, lo, diff, nc, tg, pe, pm in (("review", 2, 0, 4.2, [-2.7, 10.9], [-3.6, 14.0], 0.5, 0.25),
                                              ("best", 2, 1, 2.1, [-5.9, 10.0], [-7.4, 12.3], 1.0, 0.625)):
        E(f"pair.{team}.wins", w)
        E(f"pair.{team}.losses", lo)
        E(f"pair.{team}.diff_pts", diff, 1)
        E(f"pair.{team}.newcombe95_pts", nc, 1)
        E(f"pair.{team}.tango95_pts", tg, 1)
        E(f"pair.{team}.mcnemar_exact", pe, 3)
        E(f"pair.{team}.mcnemar_midp", pm, 3)
    E("pair.review_vs_best.wins", 1)
    E("pair.review_vs_best.losses", 0)
    E("pair.review_vs_best.diff_pts", 2.1, 1)
    E("pair.review_vs_best.newcombe95_pts", [-3.6, 7.8], 1)
    E("pair.review_vs_best.mcnemar_exact", 1.0, 2)
    for k, v in (("overlap.all3", 26), ("overlap.none", 19), ("overlap.review_best_only", 2), ("overlap.solo_review_only", 1),
                 ("overlap.union", 29), ("overlap.other_patterns", 0),
                 ("discordant.ids", ["langchain4j__langchain4j-5806", "langchain4j__langchain4j-5869", "nunit__nunit-5351"])):
        E(k, v)
    for k, v in (("agreement.solo_review_pct", 95.8), ("agreement.solo_best_pct", 93.8), ("agreement.review_best_pct", 97.9)):
        E(k, v, 1)
    for k, v in (("kappa.solo_review", 0.91), ("kappa.solo_best", 0.87), ("kappa.review_best", 0.96)):
        E(k, v, 2)
    for lang, v in (("Java", [18, 10, 12, 12]), ("C#", [9, 7, 7, 6]), ("C", [8, 5, 5, 5]), ("Rust", [8, 3, 3, 3]),
                    ("JavaScript", [3, 1, 1, 1]), ("C++", [2, 1, 1, 1])):
        E(f"bylang.{lang}", v)
    # C
    for k, v in (("rule.first.all48.resolved", [20, 19, 17]), ("rule.missing.all48.resolved", [23, 26, 27]),
                 ("rule.both.all48.resolved", [16, 17, 16]), ("rule.current.noBC.resolved", [21, 23, 23]),
                 ("rule.current.noflag.resolved", [21, 22, 22]), ("rule.current.nolangchain.resolved", [21, 21, 20]),
                 ("rule.current.issue41.resolved", [25, 27, 26]),
                 ("rule.first.noBC.resolved", [15, 14, 12]), ("rule.missing.noBC.resolved", [20, 22, 23]),
                 ("rule.first.noflag.resolved", [15, 13, 12]), ("rule.missing.noflag.resolved", [20, 21, 22]),
                 ("rule.first.issue41.resolved", [19, 18, 16]), ("rule.missing.issue41.resolved", [21, 24, 25]),
                 ("rule.first.nolangchain.resolved", [15, 13, 12]), ("rule.missing.nolangchain.resolved", [17, 18, 19])):
        E(k, v)
    for rule, name, team, w, lo, diff in (("first", "all48", "review", 3, 4, -2.1), ("first", "all48", "best", 2, 5, -6.3),
                                          ("missing", "all48", "review", 3, 0, 6.3), ("missing", "all48", "best", 5, 1, 8.3),
                                          ("both", "all48", "review", 4, 3, 2.1), ("both", "all48", "best", 4, 4, 0.0),
                                          ("current", "noBC", "review", 2, 0, 5.3), ("current", "noBC", "best", 2, 0, 5.3),
                                          ("current", "noflag", "review", 1, 0, 3.2), ("current", "noflag", "best", 1, 0, 3.2),
                                          ("current", "nolangchain", "review", 0, 0, 0.0), ("current", "nolangchain", "best", 0, 1, -2.6),
                                          ("current", "issue41", "review", 2, 0, 4.9), ("current", "issue41", "best", 2, 1, 2.4)):
        E(f"rule.{rule}.{name}.{team}.wins", w)
        E(f"rule.{rule}.{name}.{team}.losses", lo)
        E(f"rule.{rule}.{name}.{team}.diff_pts", diff, 1)
    E("rule.first.all48.review.newcombe95_pts", [-13.2, 9.2], 1)
    E("rule.first.all48.best.newcombe95_pts", [-17.2, 5.0], 1)
    E("rule.first.all48.review.mcnemar_exact", 1.0, 2)
    E("rule.first.all48.best.mcnemar_exact", 0.45, 2)
    E("rule.missing.all48.review.newcombe95_pts", [-1.6, 13.8], 1)
    # the unrounded limit -2.0475 rounds half up to -2.0
    E("rule.missing.all48.best.newcombe95_pts", [-2.0, 18.3], 1)
    E("rule.missing.all48.review.mcnemar_exact", 0.25, 2)
    E("rule.missing.all48.best.mcnemar_exact", 0.22, 2)
    E("sens.sign_count.appB", [20, 6, 14])
    E("sens.min_exact_p.appB", 0.22, 2)
    for k, v in (("regrade.changed", 28), ("regrade.by_arm.solo", 7), ("regrade.by_arm.review", 10), ("regrade.by_arm.best", 11),
                 ("regrade.tasks", 14), ("regrade.to_unresolved", 0), ("regrade.resolved_total", 84), ("rejected.patches", 49),
                 ("rejected.by_arm.solo", 14), ("rejected.by_arm.review", 17), ("rejected.by_arm.best", 18), ("rejected.tasks", 21),
                 ("rejected.all_in_hidden_files", 49), ("current_equals_test_only", 144), ("missing_p2p.records", 21),
                 ("missing_p2p.tasks", 9), ("missing_p2p.moves", 8), ("missing_p2p.move_tasks", 4), ("missing_p2p.class_c_movers", 3),
                 ("missing_p2p.listed_tests", 657666), ("missing_p2p.missing_tests", 1477),
                 ("hidden_file.touch", 71), ("hidden_file.hunks_kept", 34), ("hidden_file.decides_unresolved", 4), ("unresolved.total", 60),
                 ("fig2.all_newcombe_contain_zero", True), ("failure_class.report.all", [20, 33, 7]), ("failure_class.f2p_first.all", [23, 36, 1]),
                 ("failure_class.report.solo", [7, 11, 3]), ("failure_class.report.review", [7, 10, 2]), ("failure_class.report.best", [6, 12, 2])):
        E(k, v)
    E("regrade.only_current_share_pct", 33.3, 1)
    E("missing_p2p.share_pct", 0.22, 2)
    E("fig2.min_exact_p", 0.22, 2)
    # D
    for k, v in (("upper.newcombe.90.review_pts", 9.9), ("upper.newcombe.95.review_pts", 10.9), ("upper.cluster.90.review_pts", 10.2),
                 ("upper.cluster.95.review_pts", 11.4), ("upper.tango.90.review_pts", 11.8), ("upper.tango.95.review_pts", 14.0),
                 ("upper.exact.90.review_pts", 12.5), ("upper.exact.95.review_pts", 14.3), ("upper.newcombe.90.best_pts", 8.8),
                 ("upper.newcombe.95.best_pts", 10.0), ("upper.cluster.90.best_pts", 9.7), ("upper.cluster.95.best_pts", 11.2),
                 ("upper.tango.90.best_pts", 10.1), ("upper.tango.95.best_pts", 12.3), ("upper.exact.90.best_pts", 10.3),
                 ("upper.exact.95.best_pts", 13.2)):
        E(k, v, 1)
    E("onesided.gain5.review.range", [0.40, 0.57], 2)
    E("onesided.gain5.best.range", [0.23, 0.31], 2)
    E("onesided.gain10.review.range", [0.089, 0.129], 3)
    E("onesided.gain10.best.range", [0.052, 0.055], 3)
    E("onesided.gain15.max", 0.020, 3)
    E("onesided.loss5.review.range", [0.016, 0.016], 3)
    E("onesided.loss5.best.range", [0.055, 0.061], 3)
    E("onesided.loss10.review.range", [0.003, 0.003], 3)
    E("onesided.loss10.best.range", [0.010, 0.012], 3)
    E("onesided.loss10.rejected_both", True)
    E("p.wins_le2_given_gain10", 0.13, 2)
    E("coverage.newcombe90_pct", [5.1, 11.3], 1)
    E("coverage.tango90_pct", [0.0, 5.7], 1)
    E("equivalence.margin_needed_pts", [8.8, 12.5], 1)
    E("rule.missing.all48.review.tango90_pts", [0.6, 14.6], 1)  # Appendix B: excludes zero at one-sided 5%
    for name, up_r, up_b, rng_r, rng_b in (("noBC", 15.7, 15.7, [0.046, 0.062], [0.046, 0.062]),
                                           ("missing", 15.4, 18.4, [0.045, 0.057], [0.121, 0.141])):
        E(f"upper.exact.90.{name}.review_pts", up_r, 1)
        E(f"upper.exact.90.{name}.best_pts", up_b, 1)
        E(f"onesided.gain15.{name}.review.range", rng_r, 3)
        E(f"onesided.gain15.{name}.best.range", rng_b, 3)
    E("onesided.gain15.noBC.review.exact", 0.062, 3)
    E("onesided.gain15.noBC.best.exact", 0.062, 3)
    E("onesided.gain15.missing.review.exact", 0.057, 3)
    # E
    for a, tot, mean, med, btot, bmean in (("solo", 2.9477, 0.061, 0.011, 1.31, 0.027), ("review", 4.7255, 0.098, 0.019, 2.45, 0.051),
                                           ("best", 7.9936, 0.167, 0.070, 5.16, 0.107)):
        E(f"cost.billed.total.{a}", tot, 4)
        E(f"cost.billed.mean.{a}", mean, 3)
        E(f"cost.billed.median.{a}", med, 3)
        E(f"cost.base.total.{a}", btot, 2)
        E(f"cost.base.mean.{a}", bmean, 3)
    for team, rb, rbase, cb, cbase, gb, gbase, gbci, gbaseci in (
            ("review", 1.60, 1.86, 43, 44, 2.09, 1.97, [1.56, 2.79], [1.60, 2.42]),
            ("best", 2.71, 3.92, 46, 48, 5.14, 5.15, [4.28, 6.17], [4.50, 5.90])):
        E(f"ratio.billed.{team}", rb, 2)
        E(f"ratio.base.{team}", rbase, 2)
        E(f"costlier.billed.{team}", cb)
        E(f"costlier.base.{team}", cbase)
        E(f"geomean.billed.{team}", gb, 2)
        E(f"geomean.base.{team}", gbase, 2)
        E(f"geomean.billed.{team}.t95", gbci, 2)
        E(f"geomean.base.{team}.t95", gbaseci, 2)
    E("ratio.billed.review.boot95", [0.7, 4.9], 1, soft=True)
    E("ratio.billed.best.boot95", [1.6, 6.0], 1, soft=True)
    E("ratio.base.review.boot95", [1.2, 3.1], 1, soft=True)
    E("ratio.base.best.boot95", [2.9, 5.4], 1, soft=True)
    E("ratio.model_plus_sandbox.review.boot95", [1.0, 4.3], 1, soft=True)
    E("ratio.model_plus_sandbox.best.boot95", [2.8, 6.9], 1, soft=True)
    E("ratio.model_plus_sandbox.review", 1.92, 2)
    E("ratio.model_plus_sandbox.best", 4.08, 2)
    E("sign_test.base.review", 1.5e-9, 10)
    E("sign_test.base.best", 7.1e-15, 16)
    for k, v, nd in (("tokens.ratio.input.review", 1.86, 2), ("tokens.ratio.input.best", 3.91, 2), ("tokens.ratio.output.review", 2.21, 2),
                     ("tokens.ratio.output.best", 4.40, 2), ("tokens.cache_read_share_pct.all", 96.0, 0),
                     ("costliest.share_billed_pct.solo", 47.1, 1), ("costliest.share_billed_pct.review", 16.0, 1),
                     ("costliest.share_billed_pct.best", 14.9, 1), ("lance7835.solo.cost_usd", 1.39, 2),
                     ("lance7835.solo.billed_over_base", 4.37, 2), ("lance7835.solo.share_base_pct", 24.2, 1),
                     ("pricefit.above_billed_share_pct", 65.3, 1), ("pricefit.higher_rate_share_pct", 43.1, 1),
                     ("loo.billed.review", [1.36, 2.87], 2), ("loo.billed.best", [2.44, 4.36], 2), ("loo.base.review", [1.70, 2.35], 2),
                     ("loo.base.best", [3.77, 4.68], 2), ("without_lance.billed.review", 2.87, 2), ("without_lance.billed.best", 4.36, 2),
                     ("without_lance.base.review", 2.35, 2), ("without_lance.base.best", 4.68, 2),
                     ("ratio_of_medians.billed.review", 1.68, 2), ("ratio_of_medians.billed.best", 6.20, 2),
                     ("band.same_config.billed_total", 2.21, 2), ("band.same_config.base_total", 1.37, 2),
                     ("band.same_config.billed_geomean", 1.13, 2), ("band.same_config.base_geomean", 1.08, 2),
                     ("run_order.uncached_ratio", 1.31, 2), ("run_order.uncached_ratio.t95", [1.17, 1.46], 2),
                     ("revision.share_billed_pct", 50.4, 1), ("revision.share_base_pct", 33.3, 1),
                     ("revision.coder_share_billed_pct", 46.3, 1), ("revision.nine_tasks.review_usd", 3.03, 2),
                     ("revision.nine_tasks.review_share_pct", 64.2, 1), ("revision.nine_tasks.solo_usd", 0.30, 2),
                     ("httrack789.review_cost", 0.75, 2), ("httrack789.multiple_of_solo", 34.0, 0),
                     ("time.median_s.solo", 77.0, 0), ("time.median_s.review", 201.0, 0), ("time.median_s.best", 267.0, 0),
                     ("time.mean_s.solo", 288.0, 0), ("time.mean_s.review", 479.0, 0), ("time.mean_s.best", 485.0, 0),
                     ("time.longest_min", 47.8, 1), ("sandbox.solve_hours.solo", 3.84, 2), ("sandbox.solve_hours.review", 8.75, 2),
                     ("sandbox.solve_hours.best", 21.70, 2), ("sandbox.grading_hours.solo", 5.12, 2),
                     ("sandbox.grading_hours.review", 4.36, 2), ("sandbox.grading_hours.best", 4.79, 2),
                     ("per_task.model_plus_sandbox.solo", 0.115, 3), ("per_task.model_plus_sandbox.review", 0.220, 3),
                     ("per_task.model_plus_sandbox.best", 0.469, 3), ("total.billed_final", 15.67, 2),
                     ("price_step.share_pct.solo", 31.0, 0), ("price_step.share_pct.solo_without_lance", 14.0, 0),
                     ("price_step.share_pct.review", 23.0, 0), ("price_step.share_pct.best", 14.0, 0),
                     ("price_step.share_pct.resumed_coder", 63.0, 0), ("time.geomean_ratio.review", 2.24, 2),
                     ("time.geomean_ratio.best", 2.61, 2), ("grading.minutes_median", 3.6, 1), ("grading.minutes_p90", 11.5, 1),
                     ("revision.one_round.review_over_solo_base", 1.12, 2)):
        E(k, v, nd)
    E("share.role.billed.review_pct", [81.3, 18.7], 1)
    E("share.role.base.review_pct", [69.8, 30.2], 1)
    E("share.role.billed.best_pct", [74.0, 26.0], 1)
    E("share.role.base.best_pct", [65.7, 34.3], 1)
    for k, v in (("pricefit.at_base", 421), ("pricefit.at_five", 5), ("pricefit.between", 32), ("pricefit.above_runs", 37),
                 ("pricefit.above_tasks", 14), ("pricefit.below_base", 0), ("run_order.solo_review_best", 48), ("run_order.solo_first", 48),
                 ("run_order.solo_higher", 40), ("time.slower.review", 40), ("time.slower.best", 45), ("time.reached_limit", 0),
                 ("tokens.input.solo", 68197444), ("tokens.input.review", 126726846), ("tokens.input.best", 266860225),
                 ("tokens.output.solo", 617452), ("tokens.output.review", 1362890), ("tokens.output.best", 2718402),
                 ("lance7835.solo.resolved", False)):
        E(k, v)
    E("lance7835.solo.input_tokens", 20.1e6, -5)
    # F
    for k, v in (("review.rounds", [39, 5, 4]), ("review.reviews", 61), ("review.passing", 47), ("review.first_approved", 39),
                 ("review.final_approved", 47), ("review.rejections", 14), ("review.final_x_outcome", [28, 19, 1, 0]),
                 ("review.final.right", 28), ("review.final.pass_all_right", 29), ("review.first_x_outcome", [24, 15, 5, 4]),
                 ("review.first.right", 28), ("judge.flag_x_outcome", [27, 16, 1, 4]), ("judge.flag.right", 31),
                 ("judge.flag.pass_all_right", 28), ("judge.top", [16, 17, 15]), ("judge.tied", 32), ("judge.chosen", [29, 12, 7]),
                 ("judge.tiebreak_c1", 25), ("judge.unique_c1", 4), ("judge.tiebreak_c2", 7), ("judge.passed_flags_true", 122),
                 ("review.not_approved_tasks", ["JasperFx__wolverine-3695"]), ("review.ended_by_approval_or_three_rounds", True), ("unflagged.n", 31),
                 ("unflagged.review_final", [22, 9, 0, 0]), ("unflagged.review_first", [20, 5, 2, 4]),
                 ("unflagged.judge_flag", [22, 6, 0, 3]), ("classC.approved_resolved", 5), ("nineteen.n", 19),
                 ("nineteen.by_cause", [6, 4, 1, 8, 0]), ("change_requests.tasks", 9), ("change_requests.resolved", [5, 5]),
                 ("change_requests.same_set", True), ("change_requests.one_round_resolved", [22, 24]),
                 ("judge.resolved_by_top", [8, 12, 8]),
                 ("scores.review_returned", {"4.0": 1, "7.0": 1, "7.5": 1, "8.0": 21, "8.5": 6, "9.0": 18}),
                 ("scores.judge_all", {"2.0": 2, "3.0": 1, "5.0": 5, "6.0": 8, "7.0": 7, "7.5": 8, "8.0": 20, "8.5": 26, "9.0": 61, "9.5": 6})):
        E(k, v)
    E("review.final.precision_pct", 59.6, 1)
    E("review.first.precision_pct", 61.5, 1)
    E("judge.flag.precision_pct", 62.8, 1)
    E("review.first_approved_pct", 81.3, 1)
    E("auc.review_final", 0.58, 2)
    E("auc.review_first", 0.56, 2)
    E("auc.judge_chosen", 0.57, 2)
    E("auc.review_final.boot95", [0.42, 0.73], 2, soft=True)
    E("auc.judge_chosen.boot95", [0.41, 0.72], 2, soft=True)
    E("unflagged.auc.review_final", 0.66, 2)
    E("unflagged.auc.judge_chosen", 0.67, 2)
    E("auc.review_first.boot95", [0.39, 0.72], 2, soft=True)
    E("unflagged.auc.review_final.boot95", [0.46, 0.85], 2, soft=True)
    E("unflagged.auc.judge_chosen.boot95", [0.44, 0.89], 2, soft=True)
    E("scores.means_by_outcome.review_final", [8.345, 8.289], 3)
    E("scores.means_by_outcome.judge_chosen", [8.554, 8.475], 3)
    E("scores.judge_mean", 8.17, 2)
    E("scores.judge_median", 8.5, 1)
    E("scores.judge_ge8_pct", 78.5, 1)
    E("scores.between_share_pct", 81.7, 1)
    E("scores.icc", 0.73, 2)
    E("scores.r_review_judge", 0.81, 2)
    E("byclass.review.final.A", [1, 6, 0, 0])
    E("byclass.review.final.B", [0, 4, 0, 0])
    E("byclass.review.final.C", [5, 0, 1, 0])
    E("byclass.review.first.C", [3, 0, 3, 0])
    E("byclass.judge.flag.B", [0, 3, 0, 1])
    E("byclass.judge.flag.C", [4, 1, 1, 0])
    # G
    for k, v, nd in (("noise.replicate.pct", 5.1, 1), ("noise.replicate.wilson95_pct", [1.4, 16.9], 1),
                     ("noise.replicate_first_rule.pct", 15.4, 1), ("noise.replicate_first_rule.wilson95_pct", [7.2, 29.7], 1),
                     ("noise.sd_diff_pts", 3.3, 1), ("noise.sd_diff_wilson_pts", [1.7, 5.9], 1), ("noise.gain_in_sd", [1.3, 0.6], 1),
                     ("noise.p_second_ahead2", 0.16, 2), ("noise.p_second_ahead2_wilson", [0.03, 0.30], 2),
                     ("power.min_discordant_p", 0.031, 3), ("power.n48.5", 0.058, 3), ("power.n48.10", 0.280, 3),
                     ("power.n48.15", 0.563, 3), ("power.n48.20", 0.783, 3), ("power.min_gain80_pts.0", 16.0, 1),
                     ("power.min_gain80_pts.2of39", 20.5, 1), ("power.min_gain80_pts.wilson_hi", 27.4, 1),
                     ("power.size_n48", 0.0009, 4), ("cluster.loo_repo.review_pts", [0.0, 4.9], 1),
                     ("cluster.loo_repo.best_pts", [-2.6, 4.3], 1), ("cluster.boot_zero_share_exact_pct.review", 36.0, 1),
                     ("cluster.cr95.review_pts", [-3.1, 11.4], 1), ("cluster.cr95.best_pts", [-7.1, 11.2], 1),
                     ("plan.valid_expected", 158.4, 1), ("plan.cost_three_arms.72", 58.0, 0), ("plan.cost_three_arms.124", 100.0, 0),
                     ("plan.cost_three_arms.341", 274.0, 0), ("plan.grading_per_task", 0.20, 2), ("power.p_ge6_discordant_pct", 3.5, 1)):
        E(k, v, nd)
    for k, v in (("noise.replicate.disagree", 2), ("noise.replicate_first_rule.disagree", 6), ("noise.r1_tasks", 39),
                 ("noise.replicate.tasks_are_team_wins", True), ("power.min_discordant", 6),
                 ("power.n80.0.5", 157), ("power.n80.0.10", 78), ("power.n80.0.15", 52), ("power.n80.2of39.5", 341),
                 ("power.n80.2of39.10", 124), ("power.n80.2of39.15", 72), ("power.n80.wilson_hi.5", 718),
                 ("power.n80.wilson_hi.10", 222), ("power.n80.wilson_hi.15", 118), ("power.n80.6of39.5", 669),
                 ("power.n80.6of39.10", 210), ("power.n80.6of39.15", 113), ("power.n80_fixed_psi.20.5", 658),
                 ("power.n80_fixed_psi.20.10", 168), ("power.n80_fixed_psi.20.15", 72), ("power.n80_fixed_psi.30.5", 975),
                 ("power.n80_fixed_psi.30.10", 249), ("power.n80_fixed_psi.30.15", 112)):
        E(k, v)
    for k, v in (("cluster.repo_boot95.review_pts", [0.0, 10.2]), ("cluster.repo_boot90.review_pts", [0.0, 9.2]),
                 ("cluster.repo_boot95.best_pts", [-7.3, 9.2]), ("cluster.repo_boot90.best_pts", [-5.7, 8.2])):
        E(k, v, 1)
    for k, v, nd in (("power.midp.n48.5", 0.118, 3), ("power.midp.n48.10", 0.397, 3), ("power.midp.n48.15", 0.671, 3),
                     ("power.midp.n48.20", 0.852, 3), ("power.midp.min_gain80_pts.0", 13.7, 1),
                     ("power.midp.min_gain80_pts.2of39", 18.3, 1), ("power.midp.min_gain80_pts.wilson_hi", 25.2, 1),
                     ("power.midp.size_n48", 0.005, 3), ("power.midp.min_discordant_p", 0.031, 3), ("plan.valid_fresh", 110.4, 1), ("plan.valid_fresh_wilson95", [92.0, 126.0], 0),
                     ("run_order.uncached_ratio_totals", 1.54, 2)):
        E(k, v, nd)
    for k, v in (("power.midp.min_discordant", 5), ("power.asymptotic.min_discordant", 4), ("power.midp.n80.0.5", 134),
                 ("power.midp.n80.0.10", 66), ("power.midp.n80.0.15", 44), ("power.midp.n80.2of39.5", 309),
                 ("power.midp.n80.2of39.10", 108), ("power.midp.n80.2of39.15", 63), ("power.midp.n80.wilson_hi.5", 681),
                 ("power.midp.n80.wilson_hi.10", 205), ("power.midp.n80.wilson_hi.15", 108), ("noise.tiebreak_c1.tasks", 25),
                 ("noise.tiebreak_c1.disagree", 1), ("noise.replicate.review_wins_round1", True), ("partial.tasks", 11),
                 ("partial.resolved_first_rule", [2, 3, 2])):
        E(k, v)
    # H
    for k, v in (("class.counts.A", 7), ("class.counts.B", 4), ("class.counts.C", 6), ("class.counts.flagged", 17), ("class.a_kind", [6, 1]),
                 ("class.outcomes.A", [0, 1, 1]), ("class.outcomes.B", [0, 0, 0]), ("class.outcomes.C", [6, 6, 5]),
                 ("class.outcomes.unflagged", [21, 22, 22]), ("class.decidable_from_rows", 10), ("class.a_static_screen", [6, 13]),
                 ("pool.class_b.count", 15), ("pool.class_b_rule", 19), ("repo_hooks.tasks_with_hook", 0), ("pool.class_b_rust", [15, 68]), ("pool.class_c_rule", 28), ("pool.class_c_checked.count", 23),
                 ("class.rust_flagged", [7, 8]), ("class.discordant_flagged", [2, 3]), ("class.a_thirdparty", [5, 5]),
                 ("class.behavioural_not_stated", [4, 8]), ("issues.before_cutoff", 7), ("issues.resolved_each", [2, 2, 2]),
                 ("pr_date.checked", 12), ("instruction_files.claude_md", 19), ("instruction_files.agents_md_only", 6),
                 ("instruction_files.either", 25), ("instruction_files.langchain4j_none", True), ("instruction_files.messages_citing", 40),
                 ("instruction_files.tasks_citing", 16), ("instruction_files.verdicts_citing", 15), ("trailer_credit.tasks", 13),
                 ("trailer_credit.resolved_each", [8, 8, 8]), ("selection.dropped", 22), ("selection.rust_dropped", 10),
                 ("issues.after_pr", ["apache__parquet-java-3642", "apache__parquet-java-3669"])):
        E(k, v)
    E("pool.class_b.pct", 6.5, 1)
    E("class.share_pct.B", 8.3, 1)
    E("class.share_pct.C", 12.5, 1)
    E("pool.class_b.wilson95_pct", [4.0, 10.4], 1)
    E("pool.class_c_checked.pct", 10.0, 1)
    E("pool.class_c_checked.wilson95_pct", [6.7, 14.5], 1)
    E("selection.rust_share_kept_pct", 16.7, 1)
    E("selection.rust_share_pool_pct", 29.4, 1)
    E("selection.dropped_as_unresolved_pct", [39.0, 41.0, 40.0], 0)
    # appendix extras
    for k, v in (("superseded.started_range", ["2026-10-29T19:09:04Z", "2026-10-29T19:26:32Z"]),
                 ("final.started_range.day1", ["2026-10-29T17:54:03Z", "2026-10-29T19:05:17Z"]),
                 ("final.started_range.day2", ["2026-10-31T01:43:12Z", "2026-10-31T04:24:41Z"]),
                 ("judge.flag.passed", 43), ("judge.flag.unresolved", 20), ("review.final.passed", 47),
                 ("review.first.passed", 39), ("review.final.unresolved", 19), ("pair.review.discordant", 2),
                 ("pair.best.discordant", 3), ("overlap.agree", 45),
                 ("case.langchain4j__langchain4j-5806.solo.f2p", [2, 0, 2235]),
                 ("case.langchain4j__langchain4j-5806.best.scores", [9.0, 9.0, 9.0]),
                 ("case.JasperFx__wolverine-3695.review.scores", [2.0, 4.0, 4.0]),
                 ("case.JasperFx__wolverine-3695.best.scores", [2.0, 2.0, 3.0])):
        E(k, v)


# =============================================================================================== SVG
FONT = "Helvetica, Arial, sans-serif"
INK = "#1f2933"
MID = "#7b8794"
LIGHT = "#d9dee4"
BG = "#ffffff"
C_REVIEW = "#1f6fb2"
C_BEST = "#c2571a"
C_GREEN = "#2f855a"
MINUS = "−"


def num(x):
    """SVG coordinate format: one decimal, trailing zero dropped."""
    s = f"{x:.1f}"
    if s in ("-0.0", "0.0"):
        return "0"
    return s[:-2] if s.endswith(".0") else s


def esc(s):
    """Escape text for SVG."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class Svg:
    """A minimal SVG writer with an explicit light background, a title and a description."""

    def __init__(self, width, height, title, desc):
        self.w, self.h, self.title, self.desc = width, height, title, desc
        self.parts = []

    def text(self, x, y, s, size=12, anchor="start", fill=INK, weight=None, rotate=None, style=None):
        attrs = f'x="{num(x)}" y="{num(y)}" font-size="{size}" fill="{fill}"'
        if anchor != "start":
            attrs += f' text-anchor="{anchor}"'
        if weight:
            attrs += f' font-weight="{weight}"'
        if style:
            attrs += f' font-style="{style}"'
        if rotate is not None:
            attrs += f' transform="rotate({num(rotate)} {num(x)} {num(y)})"'
        self.parts.append(f"<text {attrs}>{esc(s)}</text>")

    def line(self, x1, y1, x2, y2, stroke=MID, width=1.0, dash=None, opacity=None):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        if opacity is not None:
            extra += f' stroke-opacity="{opacity}"'
        self.parts.append(f'<line x1="{num(x1)}" y1="{num(y1)}" x2="{num(x2)}" y2="{num(y2)}" stroke="{stroke}" stroke-width="{num(width)}"{extra}/>')

    def rect(self, x, y, w, h, fill="none", stroke=None, width=1.0, opacity=None):
        extra = f' stroke="{stroke}" stroke-width="{num(width)}"' if stroke else ""
        if opacity is not None:
            extra += f' fill-opacity="{opacity}"'
        self.parts.append(f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" fill="{fill}"{extra}/>')

    def circle(self, cx, cy, r, fill=INK, stroke=None, width=1.0):
        extra = f' stroke="{stroke}" stroke-width="{num(width)}"' if stroke else ""
        self.parts.append(f'<circle cx="{num(cx)}" cy="{num(cy)}" r="{num(r)}" fill="{fill}"{extra}/>')

    def marker(self, kind, cx, cy, r, fill, stroke=BG, width=1.0):
        """A point marker whose shape repeats what its colour encodes: circle, square or diamond."""
        if kind == "circle":
            self.circle(cx, cy, r, fill, stroke, width)
        elif kind == "square":
            self.rect(cx - 0.9 * r, cy - 0.9 * r, 1.8 * r, 1.8 * r, fill, stroke, width)
        else:
            k = 1.3 * r
            d = " ".join(f"{num(x)},{num(y)}" for x, y in ((cx, cy - k), (cx + k, cy), (cx, cy + k), (cx - k, cy)))
            self.parts.append(f'<polygon points="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{num(width)}"/>')

    def polyline(self, pts, stroke=INK, width=1.5, dash=None):
        d = " ".join(f"{num(x)},{num(y)}" for x, y in pts)
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<polyline points="{d}" fill="none" stroke="{stroke}" stroke-width="{num(width)}" stroke-linejoin="round"{extra}/>')

    def polygon(self, pts, fill, opacity):
        d = " ".join(f"{num(x)},{num(y)}" for x, y in pts)
        self.parts.append(f'<polygon points="{d}" fill="{fill}" fill-opacity="{opacity}" stroke="none"/>')

    def render(self):
        """The SVG document as text."""
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}" '
                f'role="img" aria-labelledby="t d" font-family="{FONT}">\n'
                f'<title id="t">{esc(self.title)}</title>\n<desc id="d">{esc(self.desc)}</desc>\n'
                f'<rect x="0" y="0" width="{self.w}" height="{self.h}" fill="{BG}"/>\n')
        return head + "\n".join(self.parts) + "\n</svg>\n"


def short_id(t):
    """The number of a task id, as Figure 1 labels the discordant columns."""
    return t.rsplit("-", 1)[1]


def task_key(t):
    """Sort key of task ids within a repository group of Figure 1 (numeric suffix)."""
    head, n = t.rsplit("-", 1)
    return (head, int(n)) if n.isdigit() else (head, 0)


def fig1_outcome_grid(p, d, S):
    """Figure 1: every graded outcome, 48 task columns by solo, review, best of 3 and the class strip."""
    tasks, res, cls = d["tasks"], d["res"], d["cls"]
    groups = defaultdict(list)
    for t in tasks:
        groups[d["repo"][t]].append(t)
    order = sorted(groups, key=lambda g: (-len(groups[g]), g.lower()))
    for g in order:
        groups[g].sort(key=task_key)
    for a in ARMS:  # no verdict moves from resolved under the first rule to unresolved under the current one
        assert all(res["current"][a][t] or not res["first"][a][t] for t in tasks)
    pitch, cell, gap = 11.5, 9.5, 3
    x0, top = 72, 58
    xs = {}
    x = x0
    spans = []
    framed = set(d["discordant"])
    for g in order:
        start = x
        for t in groups[g]:
            if t in framed:
                x += 2.5
            xs[t] = x
            x += pitch
            if t in framed:
                x += 2.5
        spans.append((g, start, xs[groups[g][-1]] + cell))
        x += gap
    grid_right = x - gap
    rowh = 17
    rows_y = {a: top + i * rowh for i, a in enumerate(ARMS)}
    class_y = top + 3 * rowh
    width = 776
    height = 222
    svg = Svg(width, height, "Outcome grid of the 48-task pilot",
              "Forty-eight task columns grouped by repository and three rows (solo, review, best of 3). Filled squares are "
              "tasks resolved under both grading rules, squares with a white dot are resolved only under the current rule, "
              "outlined squares are unresolved. A fourth row gives the validity class letter of the 17 flagged tasks. "
              "Framed columns are the three tasks on which a team's outcome differs from solo's. Brackets mark the "
              "repositories with three or more tasks.")
    # repository brackets, labels on two staggered levels
    level = 0
    for g, s, e in spans:
        if len(groups[g]) < 3:
            continue
        name = g.split("/", 1)[1]
        ly = 22 if level % 2 == 0 else 36
        svg.text((s + e) / 2, ly, name, 12, "middle", MID)
        by = ly + 5
        svg.line(s, by, e, by, MID, 1)
        svg.line(s, by, s, by + 4, MID, 1)
        svg.line(e, by, e, by + 4, MID, 1)
        level += 1
    for a in ARMS:
        y = rows_y[a]
        svg.text(x0 - 8, y + cell - 1, ARM_LABEL[a], 13, "end")
        for t in tasks:
            cx = xs[t]
            if res["current"][a][t] and res["first"][a][t]:
                svg.rect(cx, y, cell, cell, INK)
            elif res["current"][a][t]:
                svg.rect(cx, y, cell, cell, INK)
                svg.circle(cx + cell / 2, y + cell / 2, 2.2, BG)
            else:
                svg.rect(cx + 0.5, y + 0.5, cell - 1, cell - 1, BG, MID, 1)
        cur = sum(res["current"][a].values())
        fst = sum(res["first"][a].values())
        svg.text(grid_right + 10, y + cell - 1, f"{cur} ({fst})", 12)
    svg.text(grid_right + 10, top - 8, "resolved", 12, "start", MID)
    svg.text(x0 - 8, class_y + cell - 1, "class", 13, "end", MID)
    for t in tasks:
        if cls[t]:
            svg.text(xs[t] + cell / 2, class_y + cell - 0.5, cls[t], 12, "middle", INK, "bold")
    for t in d["discordant"]:
        cx = xs[t]
        svg.rect(cx - 1.6, top - 3, cell + 3.2, class_y + cell + 4 - (top - 3), "none", INK, 1.6)
        svg.text(cx + cell / 2, class_y + cell + 18, short_id(t), 12, "middle", INK)
    # legend
    ly = class_y + 46
    lx = x0
    svg.rect(lx, ly, cell, cell, INK)
    svg.text(lx + 15, ly + 9, "resolved under both rules", 12)
    lx += 182
    svg.rect(lx, ly, cell, cell, INK)
    svg.circle(lx + cell / 2, ly + cell / 2, 2.2, BG)
    svg.text(lx + 15, ly + 9, "resolved only under the current rule", 12)
    lx += 240
    svg.rect(lx + 0.5, ly + 0.5, cell - 1, cell - 1, BG, MID, 1)
    svg.text(lx + 15, ly + 9, "unresolved", 12)
    svg.text(x0, ly + 30, "Right: resolved of 48, current rule (rule first published). Letters: validity class A, B, C (coded).",
             12, "start", MID)
    svg.text(x0, ly + 46, "Framed: the three discordant tasks. Brackets: repositories with three or more tasks.", 12, "start", MID)
    return svg.render()


def fig2_paired_difference(p, d, S):
    """Figure 2: team minus solo under four grading rules and three task subsets, Newcombe and Tango 95%."""
    rows = d["fig2"]
    # the description below states that every interval contains zero
    assert all(v[0] <= 0 <= v[1] for r in rows for t in TEAMS for v in (r[t]["newcombe95_pts"], r[t]["tango95_pts"]))
    labels = ["All 48, current rule", "All 48, rule first published", "All 48, missing test counts as failed",
              "All 48, both stricter rules", "38, without classes B and C", "31, without all 17 flagged",
              "38, without langchain4j"]
    lims = [v for r in rows for t in TEAMS for v in r[t]["newcombe95_pts"] + r[t]["tango95_pts"]]
    lo = 5 * math.floor(min(lims) / 5)
    hi = 5 * math.ceil(max(lims) / 5)
    width, height = 760, 300
    left, gap_panel, wl_w = 236, 26, 34
    panel_w = (width - left - 10 - gap_panel - 2 * wl_w) / 2
    top, rowh = 52, 27
    svg = Svg(width, height, "Paired difference in resolve rate, team minus solo",
              "Two panels, review minus solo and best of 3 minus solo, in percentage points. Seven rows: all 48 tasks under "
              "four grading rules, then three task subsets under the current rule. Each row shows the difference as a dot, "
              "the Newcombe 95% interval as a thick bar and the Tango score 95% interval as a thin bar, with wins and losses "
              "at the right. Every interval contains zero.")
    for k, lab_ in enumerate(labels):
        svg.text(left - 10, top + k * rowh + 4 + (6 if k >= 4 else 0), lab_, 12, "end")
    for j, team in enumerate(TEAMS):
        px = left + j * (panel_w + wl_w + gap_panel)

        def sx(v, px=px):
            return px + (v - lo) / (hi - lo) * panel_w

        svg.text(px + panel_w / 2, 22, ("review" if team == "review" else "best of 3") + f" {MINUS} solo, points", 13, "middle", INK, "bold")
        svg.text(px + panel_w + wl_w / 2 + 2, 40, "W–L", 12, "middle", MID)
        bottom = top + 6 * rowh + 6 + 14
        for v in range(lo, hi + 1, 5):
            svg.line(sx(v), top - 12, sx(v), bottom, LIGHT if v != 0 else MID, 1 if v != 0 else 1.2)
            if v % 10 == 0:
                svg.text(sx(v), bottom + 15, ("+" if v > 0 else (MINUS if v < 0 else "")) + str(abs(v)), 12, "middle", MID)
        sep = top + 3 * rowh + rowh / 2 + 3
        svg.line(px - 4, sep, px + panel_w + wl_w, sep, MID, 0.8, "3 3")
        for k, r in enumerate(rows):
            y = top + k * rowh + (6 if k >= 4 else 0)
            v = r[team]
            nl, nh = v["newcombe95_pts"]
            svg.rect(sx(nl), y - 3.5, sx(nh) - sx(nl), 7, MID)
            tl, th = v["tango95_pts"]
            svg.line(sx(tl), y, sx(th), y, INK, 1.2)
            svg.line(sx(tl), y - 5, sx(tl), y + 5, INK, 1.2)
            svg.line(sx(th), y - 5, sx(th), y + 5, INK, 1.2)
            svg.circle(sx(v["diff_pts"]), y, 4.2, INK, BG, 1.2)
            svg.text(px + panel_w + wl_w / 2 + 2, y + 4, f"{v['wins']}–{v['losses']}", 12, "middle")
    ly = height - 22
    svg.circle(left + 6, ly - 4, 4.2, INK, BG, 1.2)
    svg.text(left + 16, ly, "difference", 12)
    svg.rect(left + 100, ly - 7.5, 26, 7, MID)
    svg.text(left + 132, ly, "Newcombe 95% interval", 12)
    svg.line(left + 290, ly - 4, left + 316, ly - 4, INK, 1.2)
    svg.line(left + 290, ly - 9, left + 290, ly + 1, INK, 1.2)
    svg.line(left + 316, ly - 9, left + 316, ly + 1, INK, 1.2)
    svg.text(left + 322, ly, "Tango score 95% interval", 12)
    return svg.render()


class LogAxis:
    """Map a value to a pixel position on a log10 axis (Figures A1 and A2)."""

    def __init__(self, lo, hi, a, b):
        self.l0, self.l1, self.a, self.b = math.log10(lo), math.log10(hi), a, b

    def __call__(self, v):
        return self.a + (math.log10(v) - self.l0) / (self.l1 - self.l0) * (self.b - self.a)


def figA1_cost_ratio(p, d, S):
    """Appendix Figure A1: per-task cost of each team over solo's, at base prices and as billed."""
    tasks = d["tasks"]
    width, height = 760, 400
    top, pw, ph = 46, 318, 270
    xs_left = (64, 64 + pw + 56)
    svg = Svg(width, height, "Per-task cost of each team relative to solo",
              "Two panels, review and best of 3. Horizontal axis: solo's base-price cost of the task, log scale. Vertical axis: "
              "team cost over solo cost, log scale. Filled points at base prices, hollow points as billed, joined where they "
              "differ. Horizontal lines: geometric mean of per-task ratios and ratio of totals at base prices, and the billed "
              "ratio of totals.")
    xa_lo, xa_hi = 0.003, 0.5
    ya_lo, ya_hi = 0.1, 100
    lance = tasks.index("lance-format__lance-7835")
    for j, team in enumerate(TEAMS):
        x0 = xs_left[j]
        X = LogAxis(xa_lo, xa_hi, x0, x0 + pw)
        Y = LogAxis(ya_lo, ya_hi, top + ph, top)
        svg.rect(x0, top, pw, ph, "none", LIGHT, 1)
        for v in (0.005, 0.01, 0.02, 0.05, 0.1, 0.2):
            svg.line(X(v), top, X(v), top + ph, LIGHT, 0.8)
            svg.text(X(v), top + ph + 16, f"${v:g}", 12, "middle", MID)
        for v in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100):
            svg.line(x0, Y(v), x0 + pw, Y(v), LIGHT if v != 1 else MID, 0.8 if v != 1 else 1.3)
            svg.text(x0 - 6, Y(v) + 4, f"{v:g}", 12, "end", MID)
        col = C_REVIEW if team == "review" else C_BEST
        gb = S.values[f"geomean.base.{team}"]
        rb = S.values[f"ratio.base.{team}"]
        rbill = S.values[f"ratio.billed.{team}"]
        svg.line(x0, Y(gb), x0 + pw, Y(gb), col, 1.6)
        svg.line(x0, Y(rb), x0 + pw, Y(rb), col, 1.6, "7 4")
        svg.line(x0, Y(rbill), x0 + pw, Y(rbill), INK, 1.3, "2 3")
        solo_b = d["solo_base"]
        pr = d["per_ratio"][team]
        assert all(ya_lo < v < ya_hi for k in ("base", "billed") for v in pr[k]) and all(xa_lo < v < xa_hi for v in solo_b)
        for i in range(48):
            x = X(solo_b[i])
            yb, yr = Y(pr["base"][i]), Y(pr["billed"][i])
            if abs(pr["billed"][i] / pr["base"][i] - 1) > 0.01:
                svg.line(x, yb, x, yr, MID, 0.9)
                svg.circle(x, yr, 3.4, BG, col, 1.3)
            svg.circle(x, yb, 3.4, col, BG, 0.6)
        lx, ly = X(solo_b[lance]), Y(pr["base"][lance])
        svg.text(lx - 8, ly + 4, "lance-7835", 12, "end", INK)
        above = S.values[f"costlier.base.{team}"]
        svg.text(x0, top - 10, f"{ARM_LABEL[team]}: {above} of 48 above 1 at base prices", 12, "start", INK, "bold")
        svg.text(x0 + pw / 2, top + ph + 34, "solo's base-price cost of the task (log)", 12, "middle", MID)
    svg.text(16, top + ph / 2, "team cost / solo cost (log)", 12, "middle", MID, rotate=-90)
    ly = height - 28
    svg.circle(70, ly - 4, 3.4, C_REVIEW, BG, 0.6)
    svg.circle(80, ly - 4, 3.4, C_BEST, BG, 0.6)
    svg.text(90, ly, "base prices (post hoc)", 12)
    svg.circle(250, ly - 4, 3.4, BG, C_REVIEW, 1.3)
    svg.circle(260, ly - 4, 3.4, BG, C_BEST, 1.3)
    svg.text(270, ly, "as billed", 12)
    svg.line(350, ly - 4, 378, ly - 4, INK, 1.6)
    gtxt = f"geometric mean, base ({S.values['geomean.base.review']:.2f}; {S.values['geomean.base.best']:.2f})"
    svg.text(384, ly, gtxt, 12)
    ly2 = height - 10
    svg.line(70, ly2 - 4, 98, ly2 - 4, INK, 1.6, "7 4")
    svg.text(104, ly2, f"ratio of totals, base ({S.values['ratio.base.review']:.2f}; {S.values['ratio.base.best']:.2f})", 12)
    svg.line(350, ly2 - 4, 378, ly2 - 4, INK, 1.3, "2 3")
    svg.text(384, ly2, f"ratio of totals, as billed ({S.values['ratio.billed.review']:.2f}; {S.values['ratio.billed.best']:.2f})", 12)
    return svg.render()


def figA2_power(p, d, S):
    """Appendix Figure A2: power of the exact McNemar test against the number of tasks."""
    pw, noises = d["power"], d["noises"]
    width, height = 760, 420
    x0, top, w, h = 70, 30, 536, 300
    X = LogAxis(20, 1000, x0, x0 + w)

    def Y(v):
        return top + h - v * h

    svg = Svg(width, height, "Power of the paired test against the number of tasks",
              "Power of the two-sided exact McNemar test at 0.05, counting rejections in the team's favour, for true gains of "
              "5, 10 and 15 points, against the number of tasks on a log scale from 20 to 1,000. Lines: rollout noise 2 of 39. "
              "Shaded bands: between no noise and noise 16.9%. Each gain has its own colour and marker shape (5 points a "
              "circle, 10 points a square, 15 points a diamond). Marked: the pilot's 48 tasks, 80% power, the 231-task pool "
              "and its expected 158 valid tasks.")
    svg.rect(x0, top, w, h, "none", LIGHT, 1)
    for v in (20, 50, 100, 200, 500, 1000):
        svg.line(X(v), top, X(v), top + h, LIGHT, 0.8)
        svg.text(X(v), top + h + 16, f"{v:,}", 12, "middle", MID)
    for v in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        svg.line(x0, Y(v), x0 + w, Y(v), LIGHT, 0.8)
        svg.text(x0 - 6, Y(v) + 4, f"{v:.1f}", 12, "end", MID)
    ns = list(range(20, 1001))
    colors = {5: C_REVIEW, 10: C_GREEN, 15: C_BEST}
    for g in (5, 10, 15):
        a = [pw.power(n, noises["0"] / 2 + g / 100, noises["0"] / 2) for n in ns]
        b = [pw.power(n, noises["wilson_hi"] / 2 + g / 100, noises["wilson_hi"] / 2) for n in ns]
        upper = [(X(n), Y(max(u, v))) for n, u, v in zip(ns, a, b)]
        lower = [(X(n), Y(min(u, v))) for n, u, v in zip(ns, a, b)]
        svg.polygon(upper + lower[::-1], colors[g], 0.14)
    for g in (5, 10, 15):
        c = [pw.power(n, noises["2of39"] / 2 + g / 100, noises["2of39"] / 2) for n in ns]
        svg.polyline([(X(n), Y(v)) for n, v in zip(ns, c)], colors[g], 1.8)
    svg.line(x0, Y(0.8), x0 + w, Y(0.8), INK, 1.0, "5 3")
    svg.line(X(48), top, X(48), top + h, INK, 1.0, "5 3")
    svg.text(X(48) + 4, top + 14, "48 tasks", 12, "start", INK)
    shapes = {5: "circle", 10: "square", 15: "diamond"}
    for g in (5, 10, 15):
        v = S.values[f"power.n48.{g}"]
        svg.marker(shapes[g], X(48), Y(v), 4, colors[g], BG, 1)
        svg.text(X(48) - 9, Y(v) + 4, f"{v:.2f}", 12, "end", colors[g])
        n80 = S.values[f"power.n80.2of39.{g}"]
        svg.marker(shapes[g], X(n80), Y(0.8), 4, colors[g], BG, 1)
        svg.text(X(n80), Y(0.8) - 9, str(n80), 12, "middle", colors[g], "bold")
    valid = round(S.values["plan.valid_expected"])
    for v, lab_, anchor, dx in ((valid, f"{valid} expected valid", "end", -3), (S.values["pool.tasks"], f"{S.values['pool.tasks']} pool", "start", 3)):
        svg.line(X(v), top + h, X(v), top + h - 10, INK, 1.4)
        svg.text(X(v) + dx, top + h - 14, lab_, 12, anchor, INK)
    svg.text(x0 + w / 2, top + h + 34, "number of tasks (log)", 12, "middle", MID)
    svg.text(18, top + h / 2, "power", 12, "middle", MID, rotate=-90)
    lx = x0 + w + 14
    svg.text(lx, top + 10, "true gain", 12, "start", MID)
    for k, g in enumerate((5, 10, 15)):
        y = top + 30 + k * 20
        svg.line(lx, y - 4, lx + 22, y - 4, colors[g], 1.8)
        svg.marker(shapes[g], lx + 11, y - 4, 4, colors[g], BG, 1)
        svg.text(lx + 28, y, f"{g} points", 12)
    svg.rect(lx, top + 100, 22, 10, MID, None, 1, 0.3)
    svg.text(lx + 28, top + 109, "noise 0 to 16.9%", 12)
    svg.line(lx, top + 126, lx + 22, top + 126, MID, 1.8)
    svg.text(lx + 28, top + 130, "noise 2 of 39", 12)
    svg.text(x0, height - 14, "Dashed: 80% power and the pilot's 48 tasks. Numbers on the 0.8 line: smallest n with power of at "
             "least 0.80 at noise 2 of 39.", 12, "start", MID)
    return svg.render()


def figA3_verifier_scores(p, d, S):
    """Appendix Figure A3: reviewer and judge scores by outcome, with the judge's tie counts."""
    tasks, res = d["tasks"], d["res"]["current"]
    sc = d["scores"]
    bins = [2 + 0.5 * i for i in range(16)]
    width, height = 760, 360
    top, ph = 52, 220
    panels = ((60, 300), (410, 330))
    svg = Svg(width, height, "Verifier scores by outcome",
              "Panel a: the reviewer's score of the 48 returned review patches, stacked by resolved and unresolved. Panel b: "
              "the 144 judge scores as outlined bars, with the 48 chosen candidates overlaid and stacked by resolved and "
              "unresolved, and the counts of unique and tied top scores.")
    rev = Counter((sc["review_returned"][t], res["review"][t]) for t in tasks)
    allj = Counter(s for t in tasks for s in sc["judge_all"][t])
    chj = Counter((sc["judge_chosen"][t], res["best"][t]) for t in tasks)
    for j, (x0, pw) in enumerate(panels):
        bw = pw / len(bins)
        ymax = 25 if j == 0 else 65
        step = 5 if j == 0 else 10

        def Y(v, ymax=ymax):
            return top + ph - v / ymax * ph

        for v in range(0, ymax + 1, step):
            svg.line(x0, Y(v), x0 + pw, Y(v), LIGHT, 0.8)
            svg.text(x0 - 6, Y(v) + 4, str(v), 12, "end", MID)
        for i, b in enumerate(bins):
            if b == int(b):
                svg.text(x0 + (i + 0.5) * bw, top + ph + 16, str(int(b)), 12, "middle", MID)
        for i, b in enumerate(bins):
            bx = x0 + i * bw + 1.5
            if j == 0:
                r_, u_ = rev[(b, True)], rev[(b, False)]
            else:
                tot = allj[b]
                if tot:
                    svg.rect(bx - 0.5, Y(tot), bw - 2, Y(0) - Y(tot), "none", MID, 1.2)
                r_, u_ = chj[(b, True)], chj[(b, False)]
            if r_:
                svg.rect(bx + 2, Y(r_), bw - 7, Y(0) - Y(r_), INK)
            if u_:
                svg.rect(bx + 2.5, Y(r_ + u_), bw - 8, Y(r_) - Y(r_ + u_), "#e4e7eb", INK, 1)
        svg.line(x0, Y(0), x0 + pw, Y(0), MID, 1)
        title = "a. Reviewer, 48 returned patches" if j == 0 else "b. Judge, 144 candidates; 48 chosen"
        svg.text(x0, 24, title, 13, "start", INK, "bold")
        svg.text(x0 + pw / 2, top + ph + 34, "score", 12, "middle", MID)
    svg.text(16, top + ph / 2, "patches", 12, "middle", MID, rotate=-90)
    top3 = S.values["judge.top"]
    bx = panels[1][0] + 8
    svg.rect(bx, top + 6, 172, 62, BG, LIGHT, 1)
    svg.text(bx + 8, top + 22, "Top score per task:", 12, "start", INK)
    svg.text(bx + 8, top + 39, f"unique {top3[0]}, two-way tie {top3[1]},", 12)
    svg.text(bx + 8, top + 56, f"three-way tie {top3[2]}", 12)
    ly = height - 16
    svg.rect(60, ly - 10, 12, 10, INK)
    svg.text(78, ly, f"resolved (review {S.values['resolved.review.count']}, chosen {S.values['resolved.best.count']})", 12)
    svg.rect(330, ly - 10, 12, 10, "#e4e7eb", INK, 1)
    svg.text(348, ly, f"unresolved (review {48 - S.values['resolved.review.count']}, chosen {48 - S.values['resolved.best.count']})", 12)
    svg.rect(600, ly - 10, 12, 10, "none", MID, 1.2)
    svg.text(618, ly, "all 144 judge scores", 12)
    return svg.render()


# =============================================================================================== main
def build():
    """Run every section and return the store and the figure texts."""
    self_checks()
    p = load_pilot()
    lab = load_labels()
    d = derive(p, lab)
    S = Store()
    records_and_sample(p, d, lab, S)
    outcomes(p, d, lab, S)
    grading(p, d, lab, S)
    exclusion(p, d, lab, S)
    cost(p, d, lab, S)
    verifier(p, d, lab, S)
    noise_power(p, d, lab, S)
    validity(p, d, lab, S)
    extras(p, d, lab, S)
    check_expectations(S)
    figures = {
        "fig1-outcome-grid.svg": fig1_outcome_grid(p, d, S),
        "fig2-paired-difference.svg": fig2_paired_difference(p, d, S),
        "figA1-cost-ratio.svg": figA1_cost_ratio(p, d, S),
        "figA2-power.svg": figA2_power(p, d, S),
        "figA3-verifier-scores.svg": figA3_verifier_scores(p, d, S),
    }
    return S, figures


def numbers_json(S):
    """numbers.json: every value under its key (sorted), with _about and _tags."""
    about = {"seed": SEED, "bootstrap_resamples": BOOT, "rounding": "integers as they are; other numbers to six significant digits",
             "inputs": ["results/2026-10-haiku-pilot/results.jsonl.gz", "results/2026-10-haiku-pilot/gold.jsonl",
                        "results/2026-10-haiku-pilot/meta.json", "results/2026-10-haiku-pilot/artifacts.tar.gz",
                        "docs/paper/labels.csv"],
             "price_usd_per_million": PRICE, "sandbox_usd_per_hour": SANDBOX_USD_PER_HOUR}
    out = {k: sig6(v) for k, v in S.values.items()}
    out["_about"] = about
    out["_tags"] = dict(S.tags)
    return json.dumps(out, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def main(argv=None):
    """Write the outputs, or with --check compare them with the files on disk."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true", help="recompute and compare with the files on disk")
    ap.add_argument("--quiet", action="store_true", help="print only expectation failures and the summary")
    args = ap.parse_args(argv)
    S, figures = build()
    outputs = {HERE / "numbers.json": numbers_json(S)}
    for name, text in figures.items():
        outputs[FIGS / name] = text
    if not args.quiet:
        for k in sorted(S.values):
            print(f"{k} = {json.dumps(sig6(S.values[k]), ensure_ascii=False)} [{S.tags[k]}]")
    hard = [e for e in S.expect_log if not e[3] and not e[4]]
    soft = [e for e in S.expect_log if not e[3] and e[4]]
    for key, shown, expected, _, is_soft in hard + soft:
        print(f"{'MONTE CARLO' if is_soft else 'MISMATCH'} {key}: computed {shown}, expected {expected}")
    print(f"{len(S.values)} numbers; {len(S.expect_log)} checked against expected values: {len(hard)} mismatches, "
          f"{len(soft)} Monte Carlo differences (not enforced)")
    if args.check:
        stale = [str(path.relative_to(ROOT)) for path, text in outputs.items()
                 if not path.exists() or path.read_text(encoding="utf-8") != text]
        for s in stale:
            print("DIFFERS", s)
        print("check:", "outputs match" if not stale else f"{len(stale)} outputs differ")
        return 1 if stale or hard else 0
    FIGS.mkdir(parents=True, exist_ok=True)
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
