import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# run the tests against this checkout, even if another fragment_api is installed
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    path = FIXTURES / name
    if name.endswith(".json"):
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def vectors():
    """Reference vectors produced by tonutils 2.3.1 with throwaway random seeds."""
    return load_fixture("tonutils_vectors.json")
