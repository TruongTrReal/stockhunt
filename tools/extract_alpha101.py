"""Regenerate `walk-forward optimization/alpha101_formulas.py` from the paper's own PDF.

A one-off tool, not part of the pipeline. It exists so the 101 formulas are *extracted*
rather than retyped: a hand-copied expression that differs from the published one by a
single window length produces a result nobody can reproduce and nobody can spot, and
there are 101 chances to make that mistake.

    Kakushadze, Z. (2016) "101 Formulaic Alphas", Wilmott 2016(84), 72-81.
    arXiv:1601.00991 -- https://arxiv.org/pdf/1601.00991

Run::

    python -m pip install pypdf            # not a project dependency; this tool only
    python tools/extract_alpha101.py       # downloads the PDF, writes the module

The checks below are the point of the tool. It refuses to write unless all 101 formulas
are present, every one has balanced parentheses, and the emitted module re-parses to
strings identical to what came out of the PDF.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
import urllib.request
from pathlib import Path

PAPER_URL = "https://arxiv.org/pdf/1601.00991"
REPO = Path(__file__).resolve().parents[1]
DEST = REPO / "walk-forward optimization" / "alpha101_formulas.py"

HEADER = '''"""The 101 formulaic alphas of Kakushadze (2016), verbatim.

GENERATED -- do not edit. Extracted from Appendix A.1 of arXiv:1601.00991 by
`tools/extract_alpha101.py`, which parses the paper's own PDF. Nothing here was retyped,
because a hand-copied formula that differs from the paper by one window length is a
result nobody can reproduce and nobody can spot.

    Kakushadze, Z. (2016) "101 Formulaic Alphas", Wilmott 2016(84), 72-81.
    The formulae are WorldQuant LLC's and appear in the paper with its permission.

`FORMULAS[n]` is the expression string for Alpha#n, in the paper's own grammar.
`alpha101.py` parses and evaluates it, and carries the string into every result row so a
number on a sheet can be read back to the expression that produced it.
"""

FORMULAS: dict[int, str] = {'''


def paper_text(pdf: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("pypdf is not installed. `python -m pip install pypdf` and re-run.")
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf)).pages)


def extract(text: str) -> dict[int, str]:
    """Appendix A.1 -> {n: expression}. Page-number lines are the only noise in it."""
    body = text[text.index("A.1. Formulaic Expressions for Alphas"):
                text.index("A.1. Functions and Operators")]
    # The paper breaks `Alpha#58:` across a newline in a few places, hence the \s* here.
    parts = re.split(r"Alpha#(\d+)\s*\n?\s*:", body)
    out: dict[int, str] = {}
    for i in range(1, len(parts), 2):
        lines = [ln for ln in parts[i + 1].split("\n")
                 if not re.fullmatch(r"\s*\d{1,3}\s*", ln)]
        out[int(parts[i])] = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return out


def render(formulas: dict[int, str]) -> str:
    lines = [HEADER]
    for n in sorted(formulas):
        wrapped = textwrap.wrap(formulas[n], width=84,
                                break_long_words=False, break_on_hyphens=False)
        if len(wrapped) == 1:
            lines.append(f'    {n}: "{wrapped[0]}",')
            continue
        lines.append(f"    {n}: (")
        for j, w in enumerate(wrapped):
            lines.append(f'        "{w}{"" if j == len(wrapped) - 1 else " "}"')
        lines.append("    ),")
    lines += ["}", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", type=Path, default=REPO / ".cache" / "alpha101.pdf")
    ap.add_argument("--dump-json", type=Path, default=None)
    args = ap.parse_args()

    if not args.pdf.exists():
        args.pdf.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {PAPER_URL}")
        urllib.request.urlretrieve(PAPER_URL, args.pdf)

    formulas = extract(paper_text(args.pdf))

    missing = sorted(set(range(1, 102)) - set(formulas))
    if missing:
        sys.exit(f"extraction incomplete, missing Alpha#{missing}")
    unbalanced = [n for n, e in formulas.items() if e.count("(") != e.count(")")]
    if unbalanced:
        sys.exit(f"unbalanced parentheses in Alpha#{unbalanced}")
    # No escaping is emitted below, so anything needing it must fail loudly here.
    dirty = [n for n, e in formulas.items()
             if any(c in e for c in ('"', "\\")) or any(ord(c) > 126 for c in e)]
    if dirty:
        sys.exit(f"Alpha#{dirty} contain characters this writer cannot emit safely")

    src = render(formulas)
    ns: dict = {}
    exec(compile(src, str(DEST), "exec"), ns)          # noqa: S102 -- our own output
    drift = [n for n in formulas
             if re.sub(r"\s+", " ", ns["FORMULAS"][n]) != formulas[n]]
    if drift:
        sys.exit(f"round-trip drift in Alpha#{drift}")

    DEST.write_text(src, encoding="utf-8")
    print(f"wrote {DEST} -- {len(formulas)} formulas, round-trip clean")
    if args.dump_json:
        args.dump_json.write_text(json.dumps(formulas, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
