"""Read-only Crimson Desert installation probe run inside the CDUMM environment."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    game_dir = Path(sys.argv[1]).resolve()
    result = {
        "ok": False,
        "game_dir": str(game_dir),
        "executable": False,
        "papgt_entries": 0,
        "pamt_dirs": 0,
        "errors": [],
    }
    executable = game_dir / "bin64" / "CrimsonDesert.exe"
    result["executable"] = executable.is_file()
    if not result["executable"]:
        result["errors"].append("bin64/CrimsonDesert.exe is missing")

    try:
        from cdumm.engine.crimson_rs_loader import get_crimson_rs

        crimson_rs = get_crimson_rs()
        if crimson_rs is None:
            raise RuntimeError("crimson_rs is unavailable")
        papgt = crimson_rs.parse_papgt_file(str(game_dir / "meta" / "0.papgt"))
        entries = papgt.get("entries", []) if isinstance(papgt, dict) else papgt.entries
        result["papgt_entries"] = len(entries)
        for pamt_path in sorted(game_dir.glob("[0-9][0-9][0-9][0-9]/0.pamt")):
            crimson_rs.parse_pamt_file(str(pamt_path))
            result["pamt_dirs"] += 1
    except Exception as e:
        result["errors"].append(str(e))

    result["ok"] = bool(
        result["executable"]
        and result["papgt_entries"]
        and result["pamt_dirs"]
        and not result["errors"]
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
