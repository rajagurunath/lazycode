from __future__ import annotations

from pathlib import Path

import pytest

from lazycode.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "lazycode.sqlite3")
    yield s
    s.close()
