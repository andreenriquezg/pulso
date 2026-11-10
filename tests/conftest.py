import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pulso.db import DEFAULT_DB, Database  # noqa: E402


@pytest.fixture(scope="session")
def db():
    if not DEFAULT_DB.exists():
        sys.path.insert(0, str(ROOT / "data"))
        from generate import build

        build(DEFAULT_DB)
    return Database(DEFAULT_DB)
