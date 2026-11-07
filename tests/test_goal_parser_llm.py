"""Checks the goal parser against the real LLM.

Skipped unless Ollama is running and the goal model (config.Settings.goal_model) is pulled:
    ollama pull qwen3.5:4b          # or set ENERGY_AGENT_GOAL_MODEL
    python -m pytest tests/test_goal_parser_llm.py -v
"""
import unittest

import httpx

from config import Settings
from goal_parser import parse_goals
from llm import OllamaClient

SETTINGS = Settings()
MODEL = SETTINGS.goal_model


def _model_available() -> bool:
    try:
        resp = httpx.get(f"{SETTINGS.ollama_host}/api/tags", timeout=2)
        names = {m["name"] for m in resp.json().get("models", [])}
    except Exception:
        return False
    return MODEL in names or f"{MODEL}:latest" in names


@unittest.skipUnless(_model_available(), f"Ollama model {MODEL} not available")
class RealLLMTest(unittest.TestCase):
    CASES = {
        "I want to maximise profit": ("max_profit", []),
        "Use my own energy first": ("self_consumption", []),
        "Store 6 MWh by 18:00": ("max_profit", [("reserve", 6, "18:00", "18:00", None)]),
        "Keep 2 MWh backup overnight": ("max_profit", [("reserve", 2, "22:00", "06:00", None)]),
        "Never sell below €50/MWh": ("max_profit", [("min_sell_price", None, None, None, 50)]),
        "Use my own energy first, keep 1500 kWh from 7 pm until 23:00 and never sell under 5 ct/kWh": (
            "self_consumption",
            [("reserve", 1.5, "19:00", "23:00", None), ("min_sell_price", None, None, None, 50)],
        ),
    }

    @classmethod
    def setUpClass(cls):
        cls.llm = OllamaClient(MODEL, SETTINGS)

    def test_plan_examples(self):
        for text, (objective, expected) in self.CASES.items():
            with self.subTest(text=text):
                r = parse_goals(text, llm=self.llm, battery_capacity_mwh=10)
                self.assertEqual(r.source, "llm", "LLM output was rejected; the rule parser answered")
                self.assertFalse(r.needs_clarification, r.clarifications)
                self.assertEqual(r.config.objective, objective)
                got = [(g.type, g.mwh, g.by, g.until, g.eur_per_mwh) for g in r.config.goals]
                self.assertEqual(got, expected)

    def test_vague_goals_ask_back(self):
        for text in ("Keep some energy for the evening", "Store 4 MWh", "Store 6 MWh by 6"):
            with self.subTest(text=text):
                r = parse_goals(text, llm=self.llm, battery_capacity_mwh=10)
                self.assertEqual(r.config.goals, [])
                self.assertTrue(r.needs_clarification)


if __name__ == "__main__":
    unittest.main()
