from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fx():
    def read(name: str) -> str:
        return (FIX / name).read_text(encoding="utf-8")

    return read
