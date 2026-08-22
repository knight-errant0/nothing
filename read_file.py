"""Print the contents of a text file."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a text file and print its contents.")
    parser.add_argument("file", type=Path, help="Path to the file to read")
    args = parser.parse_args()

    try:
        print(args.file.read_text(encoding="utf-8"), end="")
    except FileNotFoundError:
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        print(f"Unable to decode as UTF-8: {args.file}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Unable to read {args.file}: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
