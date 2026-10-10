"""botたんのいまの様子と直近の行動を、biorhythm_server の記憶の内部 API から読む。"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"

config_stub = types.ModuleType("config")
config_stub.BIORHYTHM_MEMORY_API_URL = "http://192.168.1.200:3204"
config_stub.BIORHYTHM_INTERNAL_SECRET = "test-secret"
config_stub.BIORHYTHM_MEMORY_API_TIMEOUT_SEC = 15.0
config_stub.BIORHYTHM_STATE_TIMEOUT_SEC = 3.0

_previous = sys.modules.get("config")
sys.modules["config"] = config_stub
try:
    spec = importlib.util.spec_from_file_location("bot_memory_client_presence_test", LIVE / "bot_memory_client.py")
    client_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client_module)
finally:
    if _previous is None:
        sys.modules.pop("config", None)
    else:
        sys.modules["config"] = _previous


class PresenceTest(unittest.TestCase):
    def test_presence_is_a_bearer_get(self):
        calls = []

        def transport(method, url, headers, payload, timeout):
            calls.append((method, url, headers, payload, timeout))
            return {"status": "Relax", "energy": 24.3, "mood": "ソファでのんびり", "moodEn": "relaxing"}

        client = client_module.BotMemoryClient(transport=transport, timeout=3.0)
        self.assertEqual(client.presence()["energy"], 24.3)
        method, url, headers, payload, timeout = calls[0]
        self.assertEqual((method, url, payload, timeout), ("GET", "http://192.168.1.200:3204/bot/presence", None, 3.0))
        self.assertEqual(headers["Authorization"], "Bearer test-secret")

    def test_activities_clamps_limit_and_drops_bad_rows(self):
        calls = []

        def transport(method, url, headers, payload, timeout):
            calls.append(url)
            return {"activities": [{"status": "Relax", "mood": "のんびり", "energy": 24,
                                    "createdAt": "2026-10-10T06:00:00.000Z"}, "bad"]}

        client = client_module.BotMemoryClient(transport=transport)
        rows = client.activities(hours=18, limit=50)
        self.assertEqual([r["mood"] for r in rows], ["のんびり"])
        self.assertEqual(calls[0], "http://192.168.1.200:3204/bot/activities?hours=18&limit=20")

    def test_unconfigured_client_raises(self):
        client = client_module.BotMemoryClient(base_url="", secret="")
        with self.assertRaises(RuntimeError):
            client.presence()


if __name__ == "__main__":
    unittest.main()
