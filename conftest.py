"""让 pytest 能同时 import 仓库根（wxread 包）与 tests/support.py，并提供跨文件共用的 store fixture。"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent
TESTS = ROOT / "tests"
for entry in (str(TESTS), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from support import MemoryContents  # noqa: E402
from wxread.state import StateStore  # noqa: E402

STATE_KEY = b"k" * 32


@pytest.fixture()
def store() -> StateStore:
    backend = MemoryContents()
    store = StateStore(backend, STATE_KEY, owner_repo="owner/repo", key_version=1)
    backend.branch = store  # 便于测试直接看写入次数
    return store
