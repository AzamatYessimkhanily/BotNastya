"""Durable conversation snapshots and conservative, language-aware dialog guards."""
import json
from contextlib import closing
import re
import sqlite3
from dialog_policy import detect_language, chess_rank


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
    r'|\bотпиши(?:те)?\b|\bотписка\b|\bудал\w*\s+(?:мой\s+(?:номер|контакт)|меня\s+из\s+рассыл\w*)'
    r'|\bотстан\w*|\b(?:stop|unsubscribe)\b'
    r'|\b(?:жазба|хабарласпа|мазалама)(?:ңыз(?:дар)?|ңдар)?\b', re.I)


AUTO_REPLY = re.compile(
    r'автоответ|автоматическ\w*\s+ответ|в\s+нерабочее\s+время|'
    r'вне\s+рабочего\s+времени|жұмыс\s+уақытынан\s+тыс|'
    r'мы\s+(?:обязательно\s+)?ответим\s+вам\s+при\s+первой\s+возможности', re.I)

QUESTION_FIELDS = {
    'audience': re.compile(r'для\s+кого|для\s+реб[её]нка\s+или|кім\s+үшін', re.I),
    'age': re.compile(r'сколько\s+(?:\w+\s+){0,3}лет|возраст|неше\s+жас|жасы\s+қанша', re.I),
    'experience': re.compile(r'опыт|начинающ|уровень|занимал\w*|играл\w*|разряд|тәжірибе|бастаушы|айналысқан|деңгей', re.I),
    'preference': re.compile(r'како\w*\s+(?:формат|филиал|вариант)|где.*(?:заним|обуч)|қай\s+филиал', re.I),
}


AGE_NUMBER = r'(?P<age>\d{1,2}(?:[.,]\d+)?)(?!\d|[.,:]\d|\s*[-–]\s*\d)'
AGE_UNIT = r'(?:лет|год(?:а)?|жас(?:та(?:мын|ғы)?)?)\b'
CHILD = r'\b(?:ребен\w*|сын(?:у|а|ом)?|доч\w*|балам\w*|балаға)\b'
SELF_REQUEST = re.compile(r'\bдля\s+(?:себя|меня)\b|\bөзім(?:е|\s+үшін)\b|^\s*өзім[.!]?\s*$', re.I)


def student_age_from_text(low, facts, last_question):
    # Prefer the pupil's age over the parent's, irrespective of sentence order.
    child_age = re.search(CHILD + r'\s*(?:сейчас\s+|уже\s+|исполнилось\s+)?' + AGE_NUMBER, low)
    self_age = re.search(r'\b(?:мне\s+(?:сейчас\s+|уже\s+|исполнилось\s+)?|менің\s+жасым\s+)' + AGE_NUMBER, low)
    if not self_age:
        self_age = re.search(r'\bмен\s+' + AGE_NUMBER + r'\s*(?:жастамын|[-–]?(?:темін|тамын|демін|дамын))\b', low)
    person_age = child_age or (self_age if facts.get('audience') != 'ребёнок' else None)
    if person_age:
        # «Мне 3 разряд», «мне 13:00» and «мне 13 месяцев» are not ages in years.
        tail = low[person_age.end():]
        if not re.match(r'\s*(?:[-–]?(?:й|ий|тий|го)\s*)?(?:разряд|месяц|ай\b)|\s*:', tail):
            return person_age.group('age')
    for age in re.finditer(r'(?<![\d.,:+–-])' + AGE_NUMBER + r'\s*' + AGE_UNIT, low):
        clause = re.split(r'[;.!?\n]|,\s*(?!\d)', low[:age.start()])[-1]
        if re.search(r'опыт|занима|игра|шахмат|стаж', clause):
            continue
        if facts.get('audience') == 'ребёнок' and re.search(r'\bмне\b|\bмен\b|менің\s+жасым', clause):
            continue
        return age.group('age')
    if re.fullmatch(r'\d{1,2}(?:[.,]\d+)?[.!]?', low.strip()) and (
        QUESTION_FIELDS['age'].search(last_question)
        or ('audience' in facts and 'age' not in facts
            and QUESTION_FIELDS['audience'].search(last_question))
    ):
        return low.strip().rstrip('.!')
    return None


def update_facts(facts, text, *, last_question=''):
    """Only client statements; never extract facts from the bot's offered options."""
    low = text.lower().replace('ё', 'е')
    facts['language'] = detect_language(text, facts.get('language', 'ru'))
    audience = 'ребёнок' if re.search(CHILD, low) else 'для себя' if SELF_REQUEST.search(low) else None
    if audience and audience != facts.get('audience'):
        if facts.get('audience'):
            # A parent may ask about themselves after asking about their child.
            for key in ('age', 'experience', 'name', 'lesson_format'):
                facts.pop(key, None)
        facts['audience'] = audience
    age = student_age_from_text(low, facts, last_question)
    if age is not None:
        facts['age'] = age
        if 'audience' not in facts and re.search(r'\bмне\b|\bмен\b|менің\s+жасым', low):
            facts['audience'] = 'для себя'
    rank = chess_rank(low)
    title = re.search(r'\b(?:кмс|мастер\s+спорта)\b', low)
    if title:
        facts['experience'] = title.group(0) + ' — есть опыт'
    elif rank is not None:
        facts['experience'] = str(rank) + ' разряд — есть опыт'
    elif re.search(r'\b(?:есть|имеет)\s+(?:\w+\s+)?разряд', low):
        facts['experience'] = text.strip() + ' — есть опыт'
    elif re.search(r'нович|начинающ|нет\s+опыта|без\s+опыта|не\s+(?:играл|занимал)|бастаушы', low):
        facts['experience'] = 'начинающий'
    elif re.search(r'(?:имеет|есть)\s+опыт|(?:занима\w*|игра\w*)\s+\d+\s+(?:лет|год|месяц)', low):
        facts['experience'] = 'есть опыт'
    elif re.search(r'дома\s+играем|үйде\s+ойнай', low):
        facts['experience'] = 'играет дома, разряд не указан'
    elif QUESTION_FIELDS['experience'].search(last_question) and re.fullmatch(r'(?:иа|иә|да|сред|средний|орташа)[.!]?', low.strip()):
        facts['experience'] = text.strip()
    # Remember an explicit format choice, not a price question or offered option.
    format_choice = re.fullmatch(r'(?:(?:хочу|выбираю|только|лучше|давайте|мне)\s+)?'
        r'(индивидуаль\w*(?:\s+занятия)?|групп\w*(?:\s+занятия)?|в\s+группе|жеке(?:\s+сабақ)?|топтық(?:\s+сабақ)?)[.!]?', low.strip())
    if format_choice:
        facts['lesson_format'] = 'индивидуально' if re.search(r'индивидуал|жеке', format_choice.group(1)) else 'группа'
    if re.search(r'как\s+зовут|имя\s+реб|аты\s+кім|аты\s+қандай', last_question, re.I):
        if re.fullmatch(r'[А-ЯЁӘІҢҒҮҰҚӨҺA-Z][а-яёәіңғүұқөһa-z-]{1,30}(?:\s+[А-ЯЁӘІҢҒҮҰҚӨҺA-Z][а-яёәіңғүұқөһa-z-]{1,30})?', text.strip()):
            if low.strip() not in {'спасибо', 'хорошо', 'рахмет', 'рақмет', 'жақсы', 'камал', 'аркада', 'онлайн'}:
                facts['name'] = text.strip()
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
