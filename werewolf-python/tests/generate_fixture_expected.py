from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain.fixtures import OfficialFixtureRunner, load_fixture


def main() -> None:
    root = Path(__file__).parent / "fixtures"
    runner = OfficialFixtureRunner()
    for path in sorted(root.glob("*.json")):
        fixture = load_fixture(path)
        result = runner.run(fixture).as_dict()
        fixture["expected"] = result
        path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(path.name)


if __name__ == "__main__":
    main()
