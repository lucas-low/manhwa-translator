import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))


def load(max_gb="10"):
    """Imports mt_ui against a throwaway output directory so tests never touch the real cache."""
    os.environ["MT_OUT_ROOT"] = tempfile.mkdtemp(prefix="mt-test-")
    os.environ["MT_CACHE_MAX_GB"] = max_gb
    sys.modules.pop("mt_ui", None)
    return importlib.import_module("mt_ui")


class CacheKey(unittest.TestCase):
    def test_same_input_same_key(self):
        m = load()
        self.assertEqual(m._cache_key(b"img", "{}"), m._cache_key(b"img", "{}"))

    def test_config_changes_key(self):
        m = load()
        self.assertNotEqual(m._cache_key(b"img", '{"a":1}'), m._cache_key(b"img", '{"a":2}'))

    def test_image_changes_key(self):
        m = load()
        self.assertNotEqual(m._cache_key(b"a", "{}"), m._cache_key(b"b", "{}"))

    def test_boundary_is_not_ambiguous(self):
        m = load()
        self.assertNotEqual(m._cache_key(b"ab", "c"), m._cache_key(b"a", "bc"))


class Eviction(unittest.TestCase):
    def fill(self, m, n, size):
        for i in range(n):
            p = m.CACHE_DIR / f"{i:02d}.png"
            p.write_bytes(b"x" * size)
            m.CACHE_DIR.joinpath(f"{i:02d}.meta").write_text("{}")
            os.utime(p, (time.time() - (n - i) * 100,) * 2)  # 00 is oldest

    def test_under_cap_evicts_nothing(self):
        m = load("10")
        self.fill(m, 3, 1000)
        self.assertEqual(m._cache_evict_if_needed(), 0)

    def test_over_cap_evicts_oldest_first_and_removes_meta(self):
        m = load("0.00001")  # about 10 KB cap
        self.fill(m, 10, 2000)  # 20 KB
        evicted = m._cache_evict_if_needed()
        self.assertGreater(evicted, 0)
        left = sorted(p.name for p in m.CACHE_DIR.glob("*.png"))
        self.assertNotIn("00.png", left)  # oldest gone
        self.assertIn("09.png", left)  # newest kept
        self.assertFalse((m.CACHE_DIR / "00.meta").exists())
        self.assertLessEqual(m._cache_total_bytes(), int(m.CACHE_EVICT_TO_GB * 1024**3))

    def test_touch_protects_an_entry(self):
        m = load("0.00001")
        self.fill(m, 10, 2000)
        m._cache_touch(m.CACHE_DIR / "00.png")  # oldest becomes newest
        m._cache_evict_if_needed()
        self.assertTrue((m.CACHE_DIR / "00.png").exists())


class Paths(unittest.TestCase):
    def test_no_hardcoded_home_in_source(self):
        for f in list((ROOT / "server").glob("*.py")) + list((ROOT / "scripts").glob("*")) + list((ROOT / "extension").glob("*.js")):
            text = f.read_text(errors="ignore")
            self.assertNotIn("/Users/", text, f.name)

    def test_gpt_config_env_override(self):
        os.environ["MT_GPT_CONFIG"] = "/tmp/x.yaml"
        m = load()
        self.assertEqual(m.CONFIG["translator"]["gpt_config"], "/tmp/x.yaml")
        os.environ.pop("MT_GPT_CONFIG")

    def test_no_secrets_in_repo(self):
        for f in ROOT.rglob("*"):
            if f.is_file() and ".git" not in f.parts and f.suffix not in {".png", ".jpg"}:
                text = f.read_text(errors="ignore")
                self.assertNotRegex(text, r"AIza[0-9A-Za-z_-]{20,}", f.name)


if __name__ == "__main__":
    unittest.main()
