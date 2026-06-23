from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import add


def test_add_positive_numbers():
    assert add(1, 2) == 3


def test_add_zero_boundary():
    assert add(0, 5) == 5
