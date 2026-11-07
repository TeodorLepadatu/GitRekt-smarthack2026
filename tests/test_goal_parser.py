import unittest

from goal_parser import GoalParser, normalize_time, parse_goals
from goal_parser.prompt import _EXAMPLES, SYSTEM_PROMPT


class FakeLLM:
    """Returns a canned response (or raises) and counts calls."""

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def chat_json(self, system, user, schema=None):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def goals(result):
    return [{k: v for k, v in g.to_dict().items() if k not in ("id", "source_text")}
            for g in result.config.goals]


class ContractTest(unittest.TestCase):
    def test_plan_section5_example(self):
        r = parse_goals("I want to maximise profit. Store 6 MWh by 18:00. Never sell below 50 EUR/MWh")
        d = r.config.to_dict()
        for g in d["goals"]:
            g.pop("source_text")
        self.assertEqual(d, {
            "objective": "max_profit",
            "goals": [
                {"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "18:00", "hard": True},
                {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50, "hard": True},
            ],
        })
        self.assertFalse(r.needs_clarification)
        self.assertEqual(r.to_dict()["status"], "ok")


class RuleParserTest(unittest.TestCase):
    def test_plan_section4_table(self):
        cases = {
            "I want to maximise profit": ("max_profit", []),
            "Use my own energy first": ("self_consumption", []),
            "Store 6 MWh by 18:00": ("max_profit", [
                {"type": "reserve", "mwh": 6, "by": "18:00", "until": "18:00", "hard": True}]),
            "Keep 2 MWh backup overnight": ("max_profit", [
                {"type": "reserve", "mwh": 2, "by": "22:00", "until": "06:00", "hard": True}]),
            "Never sell below €50/MWh": ("max_profit", [
                {"type": "min_sell_price", "eur_per_mwh": 50, "hard": True}]),
        }
        for text, (objective, expected) in cases.items():
            with self.subTest(text=text):
                r = parse_goals(text)
                self.assertEqual(r.config.objective, objective)
                self.assertEqual(goals(r), expected)
                self.assertFalse(r.needs_clarification)

    def test_units_are_converted_in_code(self):
        r = parse_goals("Keep 1500 kWh from 7 pm to 23:00. Don't sell for less than 4.5 ct/kWh")
        self.assertEqual(goals(r), [
            {"type": "reserve", "mwh": 1.5, "by": "19:00", "until": "23:00", "hard": True},
            {"type": "min_sell_price", "eur_per_mwh": 45, "hard": True},
        ])

    def test_and_between_times_is_not_split(self):
        r = parse_goals("Hold 3 MWh between 21:00 and 05:00 and never export under 60 €/MWh")
        self.assertEqual(goals(r), [
            {"type": "reserve", "mwh": 3, "by": "21:00", "until": "05:00", "hard": True},
            {"type": "min_sell_price", "eur_per_mwh": 60, "hard": True},
        ])

    def test_decimal_amount(self):
        r = parse_goals("Store 6.5 MWh by 18.30")
        self.assertEqual(goals(r), [
            {"type": "reserve", "mwh": 6.5, "by": "18:30", "until": "18:30", "hard": True}])

    def test_vague_goals_ask_back(self):
        for text in ("Keep some energy for the evening", "Store 4 MWh", "Store 4 by 18:00",
                     "Keep 2 MWh until 06:00", "Make the neighbours happy"):
            with self.subTest(text=text):
                r = parse_goals(text)
                self.assertEqual(r.config.goals, [])
                self.assertEqual(len(r.clarifications), 1)
                self.assertEqual(r.to_dict()["status"], "needs_clarification")

    def test_clear_goals_survive_next_to_vague_ones(self):
        r = parse_goals("Store 6 MWh by 18:00. Keep a bit for later.")
        self.assertEqual(len(r.config.goals), 1)
        self.assertEqual(len(r.clarifications), 1)

    def test_over_capacity_asks_back(self):
        r = parse_goals("Store 12 MWh by 18:00", battery_capacity_mwh=10)
        self.assertEqual(r.config.goals, [])
        self.assertIn("10 MWh", r.clarifications[0].question)

    def test_later_statement_wins(self):
        r = parse_goals("Maximise profit. Actually, use my own energy first.")
        self.assertEqual(r.config.objective, "self_consumption")
        self.assertFalse(r.needs_clarification)
        r = parse_goals("Never sell below 50 €/MWh. Store 6 MWh by 18:00. Actually never sell below 60 €/MWh")
        self.assertEqual(goals(r), [
            {"type": "min_sell_price", "eur_per_mwh": 60, "hard": True},
            {"type": "reserve", "mwh": 6, "by": "18:00", "until": "18:00", "hard": True},
        ])
        self.assertEqual([g.id for g in r.config.goals], ["g1", "g2"])

    def test_reserves_for_different_windows_are_all_kept(self):
        r = parse_goals("Store 6 MWh by 18:00. Keep 2 MWh backup overnight")
        self.assertEqual(len(r.config.goals), 2)

    def test_ambiguous_hour_asks_am_or_pm(self):
        r = parse_goals("Store 6 MWh by 6")
        self.assertEqual(r.config.goals, [])
        self.assertIn("06:00 or 18:00", r.clarifications[0].question)
        r = parse_goals("Store 2 MWh by 12")
        self.assertIn("12:00 or 00:00", r.clarifications[0].question)

    def test_unambiguous_hours(self):
        cases = {
            "Store 6 MWh by 6 pm": "18:00",
            "Store 6 MWh by 6 in the evening": "18:00",
            "Store 6 MWh by 7 in the morning": "07:00",
            "Store 6 MWh by 10 at night": "22:00",
            "Store 6 MWh by 12 at night": "00:00",
            "Store 6 MWh by 18": "18:00",
            "Store 6 MWh by 18 o'clock": "18:00",
            "Store 6 MWh by noon": "12:00",
        }
        for text, by in cases.items():
            with self.subTest(text=text):
                r = parse_goals(text)
                self.assertFalse(r.needs_clarification, r.clarifications)
                self.assertEqual(r.config.goals[0].by, by)

    def test_more_phrasings(self):
        cases = {
            "Store 6 megawatt hours by 18:00": {"type": "reserve", "mwh": 6, "by": "18:00", "until": "18:00", "hard": True},
            "Keep 500 kilowatt-hours until 23:00 from 20:00": None,  # "from" without "to": asks
            "Sell only above 40 €/MWh": {"type": "min_sell_price", "eur_per_mwh": 40, "hard": True},
            "Minimum sell price is 45 EUR/MWh": {"type": "min_sell_price", "eur_per_mwh": 45, "hard": True},
            "Do not export under €30": {"type": "min_sell_price", "eur_per_mwh": 30, "hard": True},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                r = parse_goals(text)
                if expected is None:
                    self.assertTrue(r.needs_clarification)
                else:
                    self.assertEqual(goals(r), [expected])
        for text in ("Use my solar first", "I want to be self-sufficient", "Avoid the grid"):
            with self.subTest(text=text):
                self.assertEqual(parse_goals(text).config.objective, "self_consumption")

    def test_filler_is_ignored(self):
        r = parse_goals("Hi! Store 6 MWh by 18:00. Thanks!")
        self.assertFalse(r.needs_clarification)
        self.assertEqual(len(r.config.goals), 1)

    def test_price_outside_market_range_asks_about_unit(self):
        r = parse_goals("Never sell below 50 €/kWh")
        self.assertEqual(r.config.goals, [])
        self.assertIn("unit", r.clarifications[0].question)

    def test_very_long_text_is_refused(self):
        r = parse_goals("Store 6 MWh by 18:00. " * 200, llm=FakeLLM(TimeoutError()))
        self.assertEqual(r.config.goals, [])
        self.assertTrue(r.needs_clarification)

    def test_empty_text_gives_default_config(self):
        r = parse_goals("   ")
        self.assertEqual(r.config.to_dict(), {"objective": "max_profit", "goals": []})


class LLMPathTest(unittest.TestCase):
    TEXT = "Use my own energy first and keep 2 MWh backup overnight"
    GOOD = {
        "objective": "self_consumption",
        "goals": [{"type": "reserve", "amount": 2, "unit": "MWh", "by": "22:00", "until": "06:00",
                   "source_text": "keep 2 MWh backup overnight"}],
        "clarifications": [],
    }

    def test_valid_llm_output_is_used(self):
        r = parse_goals(self.TEXT, llm=FakeLLM(self.GOOD))
        self.assertEqual(r.source, "llm")
        self.assertEqual(r.config.objective, "self_consumption")
        self.assertEqual(goals(r), [{"type": "reserve", "mwh": 2, "by": "22:00", "until": "06:00", "hard": True}])

    def test_llm_clarifications_pass_through(self):
        out = {"objective": None, "goals": [],
               "clarifications": [{"source_text": "keep some", "question": "How much?"}]}
        r = parse_goals("keep some", llm=FakeLLM(out))
        self.assertEqual(r.source, "llm")
        self.assertEqual(r.clarifications[0].question, "How much?")

    def test_llm_goal_with_missing_time_becomes_question(self):
        out = {"objective": None, "goals": [{"type": "reserve", "amount": 4, "unit": "MWh",
                                             "by": None, "until": None, "source_text": "Store 4 MWh"}],
               "clarifications": []}
        r = parse_goals("Store 4 MWh", llm=FakeLLM(out))
        self.assertEqual(r.source, "llm")
        self.assertTrue(r.needs_clarification)

    def test_fallback_when_llm_fails_or_misbehaves(self):
        bad_outputs = {
            "exception": TimeoutError("timed out"),
            "not an object": ["nope"],
            "unknown objective": {**self.GOOD, "objective": "be_green"},
            "converted number": {**self.GOOD, "goals": [{**self.GOOD["goals"][0], "amount": 2000, "unit": "kWh"}]},
            "invented number": {**self.GOOD, "goals": [{**self.GOOD["goals"][0], "amount": 7}]},
            "invented time": {**self.GOOD, "goals": [{**self.GOOD["goals"][0], "by": "20:00"}]},
            "dropped goal": {**self.GOOD, "goals": []},
        }
        for name, out in bad_outputs.items():
            with self.subTest(name):
                r = parse_goals(self.TEXT, llm=FakeLLM(out))
                self.assertEqual(r.source, "rules")
                self.assertEqual(goals(r), [{"type": "reserve", "mwh": 2, "by": "22:00", "until": "06:00", "hard": True}])

    def test_llm_may_turn_a_goal_into_a_question_instead(self):
        out = {"objective": "self_consumption", "goals": [],
               "clarifications": [{"source_text": "keep 2 MWh backup overnight",
                                   "question": "Which hours count as overnight?"}]}
        r = parse_goals(self.TEXT, llm=FakeLLM(out))
        self.assertEqual(r.source, "llm")
        self.assertTrue(r.needs_clarification)

    def test_llm_time_written_as_pm_is_accepted(self):
        out = {"objective": None, "goals": [{"type": "reserve", "amount": 6, "unit": "MWh", "by": "18:00",
                                             "until": None, "source_text": "Store 6 MWh by 6 pm"}],
               "clarifications": []}
        r = parse_goals("Store 6 MWh by 6 pm", llm=FakeLLM(out))
        self.assertEqual(r.source, "llm")
        self.assertEqual(r.config.goals[0].by, "18:00")

    def test_prompt_examples_pass_the_validator(self):
        # The few-shot examples must teach exactly what the validator accepts.
        for text, out in _EXAMPLES:
            with self.subTest(text=text):
                r = parse_goals(text, llm=FakeLLM(out))
                self.assertEqual(r.source, "llm")
                self.assertEqual(len(r.config.goals), len(out["goals"]))
                self.assertIn(text, SYSTEM_PROMPT)


class GoalParserCacheTest(unittest.TestCase):
    def test_reparses_only_when_text_changes(self):
        llm = FakeLLM(LLMPathTest.GOOD)
        p = GoalParser(llm)
        p.parse(LLMPathTest.TEXT)
        p.parse("  " + LLMPathTest.TEXT.upper() + " ")
        self.assertEqual(llm.calls, 1)
        p.parse("Never sell below €50/MWh")
        self.assertEqual(llm.calls, 2)


class AnswerTest(unittest.TestCase):
    def test_answering_a_question_completes_the_goal(self):
        p = GoalParser()
        r = p.parse("Maximise profit. Keep some energy for the evening")
        self.assertEqual(len(r.clarifications), 1)
        r = p.answer(r.clarifications[0], "3 MWh by 19:00")
        self.assertFalse(r.needs_clarification)
        self.assertEqual(goals(r), [{"type": "reserve", "mwh": 3, "by": "19:00", "until": "19:00", "hard": True}])

    def test_answering_am_pm(self):
        p = GoalParser()
        r = p.answer(p.parse("Store 6 MWh by 6").clarifications[0], "6 pm")
        self.assertFalse(r.needs_clarification)
        self.assertEqual(r.config.goals[0].by, "18:00")

    def test_answer_before_parse_fails(self):
        from goal_parser import Clarification
        with self.assertRaises(RuntimeError):
            GoalParser().answer(Clarification("x", "y"), "z")


class TimeTest(unittest.TestCase):
    def test_normalize_time(self):
        for raw, expected in {"18:00": "18:00", "6 pm": "18:00", "6:30pm": "18:30", "18h": "18:00",
                              "7": "07:00", "12 am": "00:00", "12 pm": "12:00", "noon": "12:00",
                              "midnight": "00:00", "24:00": "00:00", "18.30": "18:30"}.items():
            self.assertEqual(normalize_time(raw), expected, raw)
        for raw in ("25:00", "13 pm", "evening", "", None):
            with self.assertRaises(ValueError):
                normalize_time(raw)


if __name__ == "__main__":
    unittest.main()
