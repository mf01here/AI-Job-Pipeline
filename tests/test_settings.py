"""Offline tests for settings: provider presets and personal overrides.   python -m unittest discover tests -v"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import settings as S  # noqa: E402


class Providers(unittest.TestCase):
    def load(self, local: str) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / "config.toml"
        cfg.write_text((S.ROOT / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
        if local:
            (Path(tmp.name) / "config.local.toml").write_text(local, encoding="utf-8")
        return S.load_config(cfg)["local_llm"]

    def test_default_is_lm_studio(self):
        lc = self.load("")
        self.assertEqual((lc["provider"], lc["base_url"]), ("lmstudio", "http://localhost:1234/v1"))

    def test_ollama_gets_its_usual_address(self):
        lc = self.load('[local_llm]\nprovider = "ollama"\nmodel = "qwen3.5:9b"\n')
        self.assertEqual((lc["base_url"], lc["model"]), ("http://localhost:11434/v1", "qwen3.5:9b"))

    def test_explicit_address_wins(self):
        lc = self.load('[local_llm]\nprovider = "other"\nbase_url = "http://127.0.0.1:9000/v1"\n')
        self.assertEqual(lc["base_url"], "http://127.0.0.1:9000/v1")


if __name__ == "__main__":
    unittest.main()
