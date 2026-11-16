"""CLI: no network operations; generate output only after all inputs validate."""

from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path
import sys
import tempfile

from .core import InvalidInput, analyze, render_json, render_markdown, MAX_REPORTS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Group JUnit failures with explainable textual fingerprints.")
    parser.add_argument("reports", nargs="+", help="XML paths or glob patterns (duplicates read once)")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path, help="write UTF-8 atomically instead of stdout")
    args = parser.parse_args(argv)
    try:
        paths = []
        for pattern in args.reports:
            matches = glob.iglob(pattern) if glob.has_magic(pattern) else iter([pattern])
            found = False
            for match in matches:
                found = True
                paths.append(Path(match))
                if len(paths) > MAX_REPORTS:
                    raise InvalidInput(f"at most {MAX_REPORTS} input matches allowed")
            if not found:
                raise InvalidInput(f"no files match: {pattern}")
        if args.output:
            target = args.output.resolve()
            for path in paths:
                if target == path.resolve() or (target.exists() and path.exists() and os.path.samefile(target, path)):
                    raise InvalidInput("output must not overwrite an input report")
        result = analyze(paths)
        output = render_json(result) if args.format == "json" else render_markdown(result)
        if args.output:
            if not args.output.parent.is_dir():
                raise OSError("output directory does not exist")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 dir=args.output.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(output)
                os.replace(temporary, args.output)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        else:
            sys.stdout.write(output)
        return 0
    except (InvalidInput, FileNotFoundError, IsADirectoryError) as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(f"Operational error: {exc}", file=sys.stderr)
        return 3
