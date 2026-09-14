"""Run the behavioral tests shipped with the English subtitle skill.

Requires regex and wcwidth; no ASR model download or external LLM invocation.
The optional podcast exporter case is skipped when that separate skill is absent.
"""
from pathlib import Path
import unittest

TESTS = Path(__file__).resolve().parents[1] / "skills/generate-subtitle/tests"


def load_tests(loader, tests, pattern):
    return loader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(TESTS))


if __name__ == "__main__":
    unittest.main()
