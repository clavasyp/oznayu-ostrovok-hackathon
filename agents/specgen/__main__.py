"""Черновик характеристики из текста в терминале.

python -m agents.specgen "доля застройки в 300 м"
python -m agents.specgen "доля застройки в 300 м" --trial --city sochi
python -m agents.specgen "сколько ступенек у входа" --llm      # модель — стоит денег
"""

from __future__ import annotations

import argparse
import json

from agents.specgen.draft import draft_spec
from agents.specgen.trial import TRIAL_N, trial_run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agents.specgen")
    ap.add_argument("text")
    ap.add_argument("--llm", action="store_true", help="модель, если правила текст не узнали")
    ap.add_argument("--trial", action="store_true", help="пробный расчёт на отелях города")
    ap.add_argument("--city", default="sochi")
    ap.add_argument("--n", type=int, default=TRIAL_N)
    args = ap.parse_args(argv)

    draft = draft_spec(args.text, use_llm=args.llm)
    for note in draft.notes:
        print(f"# {note}")
    if draft.spec is None:
        return 1
    print(draft.yaml)
    for p in draft.problems:
        print(f"ПРОБЛЕМА: {p}")
    for w in draft.warnings:
        print(f"внимание: {w}")
    if args.trial:
        from app.stub_store import StubStore

        result = trial_run(draft.spec, StubStore.load(), args.city, n=args.n)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if not draft.problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
