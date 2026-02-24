"""
Run from project root:
  python -m gpak path/to/file.gpak                    # list contents
  python -m gpak path/to/file.gpak --extract D       # extract to directory D
  python -m gpak --pack DIR -o path/to/output.gpak   # repack directory into .gpak
  python -m gpak --pack DIR -o out.gpak --no-zlib    # repack uncompressed (use if game fails after repack)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as python -m gpak from repo root
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpak.reader import GpakReader, list_gpak, extract_gpak
from gpak.writer import pack_gpak


def main() -> None:
    ap = argparse.ArgumentParser(
        description="List, extract, or pack a GPAK archive (Mewgenics / The End Is Nigh)."
    )
    ap.add_argument("gpak", type=Path, nargs="?", help="Path to .gpak file (for list/extract)")
    ap.add_argument("--extract", "-x", metavar="DIR", type=Path, help="Extract .gpak into DIR")
    ap.add_argument("--pack", "-p", metavar="DIR", type=Path, help="Pack directory DIR into a .gpak")
    ap.add_argument("-o", "--output", type=Path, help="Output .gpak path (required with --pack)")
    ap.add_argument("--no-zlib", action="store_true", help="Extract: skip zlib. Pack: store uncompressed.")
    args = ap.parse_args()

    if args.pack is not None:
        # Pack directory → .gpak
        if args.output is None:
            print("Error: --pack requires -o / --output (path to the .gpak to create)", file=sys.stderr)
            sys.exit(1)
        pack_dir = args.pack.resolve()
        if not pack_dir.is_dir():
            print(f"Not a directory: {pack_dir}", file=sys.stderr)
            print("(Use an absolute path, or run from the directory that contains the folder.)", file=sys.stderr)
            sys.exit(1)
        args.pack = pack_dir
        n = pack_gpak(args.pack, args.output, compress=not args.no_zlib)
        print(f"Packed {n} file(s) into {args.output}")
        return

    # List or extract
    if args.gpak is None:
        print("Error: gpak path required (or use --pack DIR -o file.gpak)", file=sys.stderr)
        sys.exit(1)
    gpak_path = args.gpak.resolve()
    if not gpak_path.is_file():
        print(f"Not a file: {gpak_path}", file=sys.stderr)
        print("(Use an absolute path, or run from the directory that contains the .gpak file.)", file=sys.stderr)
        sys.exit(1)
    args.gpak = gpak_path

    if args.extract is not None:
        dest = args.extract.resolve()
        dest.mkdir(parents=True, exist_ok=True)
        paths = extract_gpak(args.gpak, dest, try_zlib=not args.no_zlib)
        print(f"Extracted {len(paths)} file(s) to {dest}")
        for p in paths[:20]:
            print(f"  {p.relative_to(dest)}")
        if len(paths) > 20:
            print(f"  ... and {len(paths) - 20} more")
    else:
        entries = list_gpak(args.gpak)
        print(f"{args.gpak}: {len(entries)} file(s)")
        for e in entries[:50]:
            print(f"  {e.stored_size:>10}  {e.name}")
        if len(entries) > 50:
            print(f"  ... and {len(entries) - 50} more")


if __name__ == "__main__":
    main()
