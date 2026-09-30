"""Durable conversation snapshots and conservative, language-aware dialog guards."""
import json
from contextlib import closing
import re
import sqlite3


def save_snapshot(path, chat_id, payload):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS conversations (chat_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO conversations VALUES (?, ?)',
                   (chat_id, json.dumps(payload, ensure_ascii=False)))


def load_snapshots(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS conversations (chat_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        return [(chat, json.loads(payload)) for chat, payload in db.execute('SELECT chat_id, payload FROM conversations')]


STOP_CONTACT = re.compile(
    r'\bне\s+(?:(?:надо|нужно|хочу|смейте?)\s+)?(?:мне\s+|нам\s+|больше\s+)*'
    r'(?:пиши(?:те)?|писать|звони(?:те)?|звонить|беспокой(?:те)?|беспокоить|'
    r'присылай(?:те)?|присылать|отправляй(?:те)?|отправлять)\b'
    r'|\bне\s+хочу\s*,?\s*чтобы\s+(?:вы|ты)\s+(?:мне\s+|нам\s+)?(?:писал|звонил|беспокоил)'
    r'|\b(?:перестан\w*|прекрат\w*|хватит)\s+(?:мне\s+|нам\s+)?(?:писать|звонить|рассыл\w*)'
    r'|\bотпиш\w*|\bотписка\b|\bудал\w*\s+(?:мой\s+(?:номер|контакт)|меня\s+из\s+рассыл\w*)'
    r'|\bотстан\w*|\b(?:stop|unsubscribe)\b|\bжазба\w*|\bхабарласпа\w*|\bмазалама\w*', re.I)


AUTO_REPLY = re.compile(
    r'автоответ|автоматическ\w*\s+ответ|в\s+нерабочее\s+время|'
    r'вне\s+рабочего\s+времени|жұмыс\s+уақытынан\s+тыс|'
    r'мы\s+(?:обязательно\s+)?ответим\s+вам\s+при\s+первой\s+возможности', re.I)

QUESTION_FIELDS = {
    'audience': re.compile(r'для\s+кого|для\s+реб[её]нка\s+или|кім\s+үшін', re.I),
    'age': re.compile(r'сколько\s+(?:\w+\s+){0,3}лет|возраст|неше\s+жас', re.I),
    'experience': re.compile(r'опыт|начинающ|уровень|занимал\w*|играл\w*|разряд|тәжірибе|бастаушы', re.I),
    'preference': re.compile(r'како\w*\s+(?:формат|филиал|вариант)|где.*(?:заним|обуч)|қай\s+филиал', re.I),
}


def update_facts(facts, text, *, last_question=''):
    """Only client statements; never extract facts from the bot's offered options."""
    low = text.lower().replace('ё', 'е')
    if re.search(r'ребен|сын|доч|балам|балама|балаға', low):
        facts['audience'] = 'ребёнок'
    elif 'для себя' in low or 'өзім' in low:
        facts['audience'] = 'для себя'
    age = re.search(r'\b(\d{1,2})\s*(?:лет|год(?:а)?|жас)\b', low)
    if age and not re.search(r'(?:опыт|занима|игра|шахмат|стаж)', low[:age.start()]):
        facts['age'] = age.group(1)
    elif re.fullmatch(r'\d{1,2}[.!]?', low.strip()) and (
        QUESTION_FIELDS['age'].search(last_question)
        or ('audience' in facts and 'age' not in facts
            and QUESTION_FIELDS['audience'].search(last_question))
    ):
        # A number answers an age question, but not a question about rank/time.
        facts['age'] = low.strip().rstrip('.!')
    rank = re.search(r'\b(?:[1-4iv]+\s*(?:-?й\s+)?разряд\w*|кмс|мастер\s+спорта)\b', low)
    if rank:
        facts['experience'] = rank.group(0) + ' — есть опыт'
    elif re.search(r'\b(?:есть|имеет)\s+(?:\w+\s+)?разряд', low):
        facts['experience'] = text.strip() + ' — есть опыт'
    elif re.search(r'нович|начинающ|нет\s+опыта|без\s+опыта|не\s+(?:играл|занимал)|бастаушы', low):
        facts['experience'] = 'начинающий'
    elif re.search(r'(?:имеет|есть)\s+опыт|(?:занима\w*|игра\w*)\s+\d+\s+(?:лет|год|месяц)', low):
        facts['experience'] = 'есть опыт'
    # A question/comparison or negated option is not a chosen branch.
    if '?' not in low and not re.search(r'\b(?:или|либо|не)\b', low):
        branches = [label for pattern, label in (
            (r'аркад', 'Аркада'), (r'камал', 'Камал'), (r'онлайн', 'онлайн'),
            (r'quantum|квантум', 'Quantum'), (r'harmony|хармони', 'Harmony'))
            if re.search(pattern, low)]
        if len(branches) == 1:
            facts['preference'] = branches[0]
    return facts


def has_question(text):
    return '?' in text or bool(re.search(
        r'\b(?:подскаж\w*|уточни\w*|напиши\w*|сообщи\w*|скажите)\b', text, re.I))


def remove_answered_questions(answer, facts):
    """Drop questions whose answers are already explicitly known; retain useful prose."""
    parts = re.split(r'(?<=[.!?])\s+', answer)
    return ' '.join(part for part in parts if not (
        has_question(part) and any(key in facts and pattern.search(part)
                            for key, pattern in QUESTION_FIELDS.items())))
