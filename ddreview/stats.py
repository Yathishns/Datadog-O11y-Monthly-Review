"""Small numeric helpers. Every rate the kit stores is {num, den, pct} so a reader (or the validator) can recompute it."""
import math


def rate(num, den):
    num = int(round(num or 0))
    den = int(round(den or 0))
    return {"num": num, "den": den, "pct": round(100.0 * num / den, 4) if den else 0.0}


def ratio(num, den, digits=2):
    return {"num": num, "den": den, "value": round(num / den, digits) if den else None}


def pctl(values, p):
    """Nearest-rank percentile on a sorted list (deterministic, no interpolation): the ceil(p/100 * n)-th value. p in 0..100."""
    if not values:
        return None
    k = max(1, math.ceil(p / 100.0 * len(values)))
    return values[min(k, len(values)) - 1]


def p95_interval_s(values, p=95, z=1.96):
    """Order-statistic 95 % interval of the p-th percentile of a sorted list of milliseconds, in seconds: the values at ranks
    n*q -/+ z*sqrt(n*q*(1-q)); None under 20 values. Two months whose intervals overlap have not moved beyond sampling noise."""
    m = len(values)
    if m < 20:
        return None
    q = p / 100.0
    half = z * math.sqrt(m * q * (1 - q))
    lo, hi = max(1, math.floor(m * q - half)), min(m, math.ceil(m * q + half))
    return [round(values[lo - 1] / 1000.0, 3), round(values[hi - 1] / 1000.0, 3)]


def percentiles(values, points=(50, 75, 95, 99)):
    s = sorted(values)
    return {f"p{p}": pctl(s, p) for p in points}


def weighted_share(rows, weight_key, predicate):
    """Share (0..100) of total weight carried by rows for which predicate(row) is true."""
    total = sum(r[weight_key] for r in rows)
    hit = sum(r[weight_key] for r in rows if predicate(r))
    return rate(hit, total)


def r3(x):
    return None if x is None else round(float(x), 3)


def r1(x):
    return None if x is None else round(float(x), 1)
