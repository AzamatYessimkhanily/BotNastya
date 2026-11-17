"""Регрессия по аудиту диалогов WhatsApp-бота от 17.11.2026."""
import os
import unittest

os.environ.setdefault("GREEN_API_ID", "test")
os.environ.setdefault("GREEN_API_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("MOYKLASS_API_KEY", "test")

import bot  # noqa: E402
import conversation_memory as memory  # noqa: E402


def age_of(text):
    facts = {"audience": "ребёнок"}
    memory.update_facts(facts, text, last_question="Сколько лет ученику?")
    return facts.get("age")


class SchoolGradeIsNotAge(unittest.TestCase):
    def test_grade_converted_to_age(self):
        self.assertEqual(age_of("Хотела записать сына 6 класс на шахматы"), "12")
        self.assertEqual(age_of("6 класс"), "12")
        self.assertEqual(age_of("Ребенок в 3 классе"), "9")
        self.assertEqual(age_of("балам 5 сынып"), "11")

    def test_explicit_age_wins_over_grade(self):
        self.assertEqual(age_of("сын 7 лет, 1 класс"), "7")
        self.assertEqual(age_of("8 лет умеет играть"), "8")


class ClosedBranchesAreNotRouted(unittest.TestCase):
    def test_closed_partner_schools_not_guessed(self):
        for text in ("Riviera", "в ривьеру", "Steppe school", "МША", "РФМШ", "у нас тест по математике"):
            self.assertIsNone(bot._guess_filial_from_text(text), text)

    def test_our_branches_still_recognized(self):
        self.assertEqual(bot._guess_filial_from_text("Бином Кекильбаева"), 54673)
        self.assertEqual(bot._guess_filial_from_text("Айтеке би"), 37754)
        self.assertEqual(bot._guess_filial_from_text("Камал"), 42763)


class PromptCarriesAuditRules(unittest.TestCase):
    def test_prompt_mentions_closed_schools_and_addresses(self):
        prompt = bot._SYSTEM_PROMPT_TEMPLATE
        self.assertIn("ЗАКРЫТЫЕ ШКОЛЫ", prompt)
        self.assertIn("ВОЗРАСТ ПО КЛАССУ", prompt)
        self.assertNotIn("GM Legends Riviera International School: Томирис", prompt)
        self.assertNotIn("РФМШ: Томирис", prompt)
        self.assertIn("ул. Айтеке би 15", prompt)

    def test_fallback_no_longer_says_not_confirmed(self):
        with open(bot.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("Оформление заявки пока не подтверждено", source)


if __name__ == "__main__":
    unittest.main()
