from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.fixtures import OfficialFixtureRunner, load_fixture


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python tests/run_fixture.py <fixture.json>")
        return 2
    fixture = load_fixture(Path(sys.argv[1]))
    result = OfficialFixtureRunner().assert_expected(fixture)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
