"""Formatting helpers and the number ledger.

Every figure the notebook prints goes through one of these helpers, which registers the printed form in the ledger so
``validate`` can prove that each number in the Markdown (or in an LLM-polished copy) exists in facts.json.
``per_day`` refuses sample-tier figures: a 9-minute sample is never scaled to a day.
"""
import re

WORDS = {0: "no", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
         15: "fifteen", 20: "twenty", 25: "twenty-five", 30: "thirty", 40: "forty", 50: "fifty", 100: "a hundred"}


class TierError(Exception):
    pass


class Ledger:
    """Collects every number string the renderer printed, in the forms the validator will meet in the Markdown."""

    def __init__(self):
        self.numbers = set()

    def add(self, text):
        for tok in re.findall(r"\d[\d,]*\.?\d*", str(text)):
            self.numbers.add(tok)
            self.numbers.add(tok.replace(",", ""))
        return text


LEDGER = Ledger()


def n(x):
    """Integer with thousands separators; None -> '—'."""
    if x is None:
        return "—"
    return LEDGER.add(f"{int(round(x)):,}")


def big(x):
    """Prose form for large counts: 6,722,195 -> '6.7 million'; 68,721 -> '68,721'."""
    if x is None:
        return "—"
    x = float(x)
    if x >= 1e9:
        return LEDGER.add(f"{x / 1e9:.1f} billion")
    if x >= 1e6:
        return LEDGER.add(f"{x / 1e6:.1f} million")
    return n(x)


def pct(rate_or_value, d=None):
    """A Rate dict or a plain percentage. Precision: 1–2 dp; 3–4 dp below 0.1 %; 2 dp between 99.5 and 100 so a 99.95 % target
    never reads as 100 %."""
    v = rate_or_value["pct"] if isinstance(rate_or_value, dict) else rate_or_value
    if v is None:
        return "—"
    if v == 0:
        return LEDGER.add("0 %")
    if d is None:
        if v >= 100 or (v == int(v) and v >= 10):
            d = 0
        elif v >= 99.5:
            # the fewest decimals (1 to 4) that keep the value as given and below 100: 99.5, 99.95, 99.999
            d = next((k for k in (1, 2, 3, 4) if round(v, k) < 100 and round(v, k) == round(v, 4)), 4)
        else:
            d = 1 if v >= 10 else (2 if v >= 0.1 else (3 if v >= 0.01 else 4))
    return LEDGER.add(f"{v:.{d}f} %")


RULE_WORDS = {"5xx_or_flag": "5xx responses and application errors", "non_2xx": "any status outside 2xx and 3xx, and application errors",
              "flag_only": "the error flag only"}


def rule_words(rule):
    """A failure rule as the client reads it."""
    return RULE_WORDS.get(rule, str(rule))


def frac(rate):
    """'396 of 7,394 (5.36 %)' — numerator, denominator and rate in one phrase."""
    return f"{n(rate['num'])} of {n(rate['den'])} ({pct(rate)})"


def sec(x, d=None):
    """Seconds: '< 1 ms' / '56 ms' / '2.70 s' / '74 s'."""
    if x is None:
        return "—"
    x = float(x)
    if x < 0.001:
        return "< 1 ms"
    if x < 1:
        return LEDGER.add(f"{x * 1000:.0f} ms")
    if x >= 10 and d is None:
        return LEDGER.add(f"{x:.0f} s")
    return LEDGER.add(f"{x:.{2 if d is None else d}f} s")


def sec_range(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return "—"
    if len(v) == 1 or v[0] == v[-1]:
        return sec(v[0])
    if v[-1] < 1:
        return f"{LEDGER.add(f'{v[0] * 1000:.0f}')}–{sec(v[-1])}"
    return f"{sec(v[0])}–{sec(v[-1])}"


def ms(x):
    if x is None:
        return "—"
    return sec(float(x) / 1000.0)


def words(k):
    k = int(k)
    return WORDS.get(k, n(k))


def plural(k, one, many=None):
    many = many or one + "s"
    return f"{words(k)} {one if k == 1 else many}"


ONE_IN_LADDER = (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30, 40, 50, 100)


def one_in(pct_value):
    """5.36 % -> 'one in twenty'. Snaps to the nearest rung of a fixed ladder (so the phrase never runs backwards as the rate
    rises) and returns None — the caller then writes '{pct} of ... fail' — outside 0.5–50 % or when no rung is within 15 %."""
    if pct_value is None or pct_value < 0.5 or pct_value > 50:
        return None
    k = 100.0 / pct_value
    snap = min(ONE_IN_LADDER, key=lambda c: (abs(c - k), c))
    if abs(snap - k) / k > 0.15:
        return None
    return f"one in {words(snap)}"


def fail_phrase(rate, what):
    """'One in twenty claim creations fails' or '0.19 % of claim creations fail'."""
    oi = one_in(rate["pct"])
    if oi:
        return f"{oi} {what} fails"
    return f"{pct(rate)} of {what} fail"


def per_day(count, window, tier="aggregate"):
    """'about 57 a day' — only for aggregate-tier figures over a proved or asserted window."""
    if tier != "aggregate" or not window.get("per_day_allowed"):
        raise TierError("per-day wording needs an aggregate-tier figure over a proved or asserted window")
    days = window["aggregate"]["days"]
    return f"about {n(count / days)} a day"


def maybe_per_day(count, window, tier):
    try:
        return per_day(count, window, tier)
    except TierError:
        return ""


def ratio_text(r):
    if not r or r.get("value") is None:
        return "—"
    return LEDGER.add(f"{r['value']:.2f}" if r["value"] < 10 else f"{r['value']:.0f}")


def range_words(days):
    """An estimated export range: 'about 7 days', 'about 1 day', 'about 3 hours'."""
    if days < 1:
        h = max(1, round(days * 24))
        return f"about {LEDGER.add(str(h))} hour{'s' if h != 1 else ''}"
    d = round(days)
    return f"about {LEDGER.add(str(d))} day{'s' if d != 1 else ''}"


def x_text(v, d=1):
    return LEDGER.add(f"{v:.{d}f}")


def trend_arrow(direction):
    return {"worse": "▲", "better": "▼", "flat": "→", "new": "🆕", "closed": "✅"}.get(direction, "")


def cap(s):
    return s[:1].upper() + s[1:] if s else s


def table(header, rows):
    """GFM pipe table: header + '|---|' separator, pipes in cells replaced by '/', one line per row."""
    clean = lambda c: str(c).replace("|", "/").replace("\n", " ").strip()
    out = ["| " + " | ".join(clean(h) for h in header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(clean(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def code(s):
    """A name as inline code; backticks become quotes and pipes become '/', exactly as table() rewrites them, so a name
    reads the same in headings, bullets, tables and prompts."""
    return "`" + str(s).replace("`", "'").replace("|", "/") + "`"


def join_and(items):
    items = [str(i) for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
