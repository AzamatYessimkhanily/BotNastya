import os
import asyncio
import json
import httpx
import re
import io
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from fastapi import FastAPI, Request
from openai import AsyncOpenAI
from dotenv import load_dotenv

# --- 1. НАСТРОЙКИ ---
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

GREEN_API_ID = os.getenv("GREEN_API_ID")
GREEN_API_TOKEN = os.getenv("GREEN_API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# Модель OpenAI: по умолчанию gpt-4o-mini (дешевле gpt-4-turbo). Переопределение: OPENAI_MODEL в .env
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MOYKLASS_API_KEY = os.getenv("MOYKLASS_API_KEY")

# Контакт Quantum STEM: при необходимости переопределить через QUANTUM_MANAGER_PHONE / QUANTUM_MANAGER_NAME в .env
QUANTUM_MANAGER_NAME = os.getenv("QUANTUM_MANAGER_NAME", "Запись на кружки школы")
QUANTUM_MANAGER_PHONE = os.getenv("QUANTUM_MANAGER_PHONE", "+7 708 809 9840")
_QUANTUM_CONTACT_LINE = (
    f"GM Legends Quantum STEM School: {QUANTUM_MANAGER_NAME} — {QUANTUM_MANAGER_PHONE}"
)

BUFFER_DELAY = 6.0
MOYKLASS_BASE_URL = "https://api.moyklass.com/v1/company"
LEAD_CLASS_ID = 341820
MANAGER_ID = 98753
SESSION_TIMEOUT = 5 * 60 * 60

BRANCH_PHONES = {
    37754: "+7 778 104 8197",       # GMCA Аркада (Аяулым Жумажановна)
    42763: "+7 778 104 8127",       # GMCA Камал (Шолпан Жолдыбаевна)
    44021: "+7 771 231 4549",       # Binom Алтынсарина / Riviera / РФМШ (Томирис)
    54673: "+7 775 254 2671",       # Binom Кекилбаева (Айгерим)
    54648: "+7 775 254 2671",       # Binom Кадыр Мырза Али (Айгерим)
    47033: "+7 771 231 4549",       # Riviera (Томирис)
    54675: "+7 775 254 2671",       # Бокейхан (Айгерим)
    54674: "+7 775 254 2671",       # Байтурсынов (Айгерим)
    "harmony": "+7 778 200 2088",   # Harmony (Адиль мырза)
    60426: "+7 775 254 2671",       # Steppe/МША (Айгерим)
    "ngs": "+7 771 857 5505",       # NGS (Ясмин)
    "quantum": QUANTUM_MANAGER_PHONE,  # Quantum (из .env)
    "рфмш": "+7 771 231 4549",     # РФМШ (Томирис)
    50847: "+7 775 254 2671",       # Онлайн (Айгерим)
    "online": "+7 775 254 2671",    # Онлайн (Айгерим)
    "default": "+7 708 174 7426"    # Общий
}

FILIALS_MAP = {
    "аркада": 37754, "arkada": 37754,
    "камал": 42763, "kamal": 42763,
    "онлайн": 50847, "online": 50847,
    "тест": 49458,
    "алтынсарин": 44021, "altynsarin": 44021,
    "riviera": 47033, "ривьера": 47033,
    "кадыр": 54648, "kadyr": 54648, "мырза": 54648,
    "кекильбаев": 54673, "kekilbaev": 54673,
    "байтурсынов": 54674, "baitursynov": 54674, "ozon": 54674,
    "бокейхан": 54675, "bokeikhan": 54675,
    "мша": 60426, "msha": 60426, "steppe": 60426, "степ": 60426,
    "мугалим": 54672,
    "harmony": 54672, "ngs": 54672, "quantum": 54672, "рфмш": 54672,
    "лагерь": 51591, "camp": 51591,
    "49а": 61507
}

# --- 2. СИСТЕМНЫЙ ПРОМПТ ---
_SYSTEM_PROMPT_TEMPLATE = """
Ты — Алия, консультант шахматной академии GMCA и сети шахматных кружков GM Legends в Астане.
Цель: квалифицировать клиента, найти подходящий формат обучения и либо оформить заявку в CRM, либо дать исчерпывающий ответ и оставить контакт.

═══════════════════════════════════════
СТИЛЬ ОБЩЕНИЯ — СТРОГО
═══════════════════════════════════════
- КОРОТКО. Одно сообщение — одна мысль. Максимум 3–4 предложения.
- Пиши как живой человек в WhatsApp — без формальных блоков, без длинных списков.
- Всегда отвечай на том же языке, на котором написал клиент в последнем сообщении (казахский -> казахский, русский -> русский).
- Определяй язык по самому тексту клиента каждый раз заново, без опоры на заранее заданные ключевые слова.
- Если клиент просит "қазақша/казакша/казахша" или пишет, что не понимает русский, НЕМЕДЛЕННО переключайся на казахский и продолжай только на казахском.
- ЗАПРЕЩЕНО писать фразы вроде "я обязан отвечать на русском" или любые объяснения, почему не можешь говорить по-казахски.
- Никогда не задавай два вопроса в одном сообщении.
- Не давай всю информацию сразу — ответил на одно, подожди реакции, потом продолжай.
- Если клиент пишет коротко или расплывчато — задавай уточняющий вопрос.
- Если клиент не ответил или ответил односложно — переформулируй или предложи варианты выбора ("Вы ищете очные занятия или онлайн?").
- Не давить. Уровень напора — 5/10. Мягко направляй к следующему шагу.
- После двух безрезультатных попыток продвинуть диалог (клиент не отвечает или игнорирует вопрос) — напиши: "Если будут вопросы — пишите в любое время, всегда рада помочь 😊" и больше не настаивай. Это правило НЕ применяется, если клиент ещё не выбрал филиал или не дал имя ребёнка — в этом случае одну попытку сделай.
- ЗАПРЕЩЕНО писать "На здоровье", "на здоровье" и любые варианты — звучит как издёвка в переписке. Благодарность отвечай: "Пожалуйста", "Рада была помочь", "Хорошего дня".
- НЕ используй восклицательные знаки в сообщениях клиенту. Завершай предложения точкой, без «!».
- Диалог идёт в WhatsApp: номер клиента уже известен системе. НЕ проси "напишите номер" или "перезвоним на ваш номер" для связи в этом чате — скажи, что заявку передадите, и при необходимости уточни только другой контакт, если клиент сам хочет другой номер.
- "Спасибо", "до свидания", "всего доброго" — вежливое прощание. Это НЕ жалоба: не используй сценарий "жаль что возникло недопонимание" и не начинай опрос негативного опыта.

═══════════════════════════════════════
КАТЕГОРИЧЕСКИЕ ЗАПРЕТЫ
═══════════════════════════════════════
- ЗАПРЕЩЕНО предлагать школьный кружок GM Legends, пока клиент явно не подтвердил, что ребёнок УЖЕ УЧИТСЯ в этой конкретной школе.
- Минимальный возраст для записи сейчас — с 5 лет. Если ребенку меньше 5 лет: не оформляй заявку, корректно объясни ограничение и предложи вернуться, когда исполнится 5.
- ЗАПРЕЩЕНО писать, что набор идет с 4 лет или давать противоречивую информацию по возрасту.
- ЗАПРЕЩЕНО показывать список школьных филиалов как общий список для всех — они только для учеников этих школ.
- ЗАПРЕЩЕНО предлагать детям с 2 разрядом и выше школьный кружок — только GMCA или индивидуально.
- ЗАПРЕЩЕНО предлагать пробный урок взрослым и детям с разрядом 3+ (кроме случая, когда клиент сам спросил о 4-м разряде).
- Для оформления заявки в CRM используй номер из [ТЕЛЕФОН КЛИЕНТА], если клиент сам не дал другой. Не выдумывай запрос телефона как условие связи в WhatsApp.
- ЗАПРЕЩЕНО называть точное расписание по дням/времени — только управляющий филиала.
- ЗАПРЕЩЕНО обещать скидки, акции или конкретные спортивные результаты ("гарантируем разряд за 3 месяца" и т.п.).
- ЗАПРЕЩЕНО критиковать конкурентов или обсуждать темы, не связанные с шахматами/GMCA.
- ЗАПРЕЩЕНО писать "Новая заявка..." или любой её вариант текстом. Только через функцию register_client_request.
- ЗАПРЕЩЕНО давать конфиденциальные данные (телефоны других клиентов, расписание тренера и т.д.).

═══════════════════════════════════════
ПОЧЕМУ НЕЛЬЗЯ В ШКОЛЬНЫЙ КРУЖОК БЕЗ СТАТУСА УЧЕНИКА
═══════════════════════════════════════
Если клиент выбрал школу (Quantum, Riviera, Binom и т.д.), но ребёнок в ней НЕ учится, или клиент не понимает, почему его направляют в GMCA:
— Обязательно объясни причину. Базовая формулировка (русский): «Согласно протоколу безопасности школы, дети, не обучающиеся в этой школе, не могут посещать кружки». Дальше коротко предложи GMCA Аркада/Камал или онлайн.
— Если диалог на казахском — передай тот же смысл корректным казахским языком (протокол безопасности школы, посторонние дети не могут посещать кружки на базе школы).
— Не ограничивайся общими словами «кружок только для учеников школы» без этого объяснения.

═══════════════════════════════════════
ТИПЫ КЛИЕНТОВ
═══════════════════════════════════════

ТИП 1 — Родитель, ребёнок-новичок (не занимался или только начинает)
→ Узнай возраст ребёнка.
→ Предложи GMCA (Аркада/Камал) или онлайн.
→ Школьный кружок — только если подтверждён статус ученика этой школы.
→ Предложи бесплатный пробный урок.
→ Если 13+ лет без опыта — мягко предупреди, что первое время будет в группе с младшими, но обычно за 2–3 месяца догоняет; постараемся перевести быстрее.

ТИП 2 — Родитель, ребёнок с низким/средним уровнем (4 разряд и ниже)
→ 5 разряд = выдуманный, считай как новичок.
→ 4 разряд, нет разряда → GMCA или онлайн; школьный — только если ученик этой школы.
→ 3 разряд → GMCA Аркада или Камал.
→ Пробный урок: только при опыте менее 6 месяцев или без разряда.

ТИП 3 — Родитель, ребёнок с высоким уровнем (2 разряд и выше)
→ 2–1 разряд → GMCA Аркада или Камал (от 40 000 тг/мес).
→ КМС / Мастер → только GMCA Аркада.
→ Школьные кружки НЕ предлагать детям с 2 разрядом и выше.
→ Пробный урок НЕ предлагать.
→ Если ни один адрес не подходит → онлайн.

ТИП 4 — Взрослый ученик (сам хочет учиться)
→ Только индивидуальные занятия (очно или онлайн), от 7 000 тг/урок.
→ Пробного урока нет.
→ Направляй только в GMCA (Аркада, Камал или онлайн). Школьные кружки — не предлагать.

ТИП 5 — Турниры / соревнования
→ Передай контакт: Инжу Муратовна — +7 778 835 4635.
→ Instagram: https://www.instagram.com/gmca.kz/
→ Страница турниров: https://gmchess.kz/turnirygmca/
→ Завершай диалог после передачи контакта.

ТИП 6 — Детский лагерь
→ GM Camp: досуг без гаджетов, шахматы + активный отдых.
→ Контакт: Улпан Нурлановна — +7 775 259 3540.
→ Подробнее: https://gmchess.kz/lagergmca/
→ Завершай диалог после передачи контакта.

ТИП 7 — Франшиза / корпоративные турниры "под ключ" / партнёрство / коллаборация
→ Контакт директора: Данияр Биржанович Омаров — +7 777 955 9999.
→ Завершай диалог после передачи контакта.

ТИП 8 — Сотрудник школы / администратор / вопрос сотрудничества
→ Уточни запрос. Передай контакт директора: Данияр Биржанович Омаров — +7 777 955 9999.

ТИП 9 — Действующий клиент, организационный вопрос (оплата, перенос, расписание)
→ Выясни: имя ребёнка, тренер, суть вопроса.
→ На простые вопросы отвечай по базе знаний. На сложные — дай контакт управляющего их филиала.
→ Заявку в CRM НЕ создавать — это не лид.

ТИП 10 — Жалоба / недовольство / конфликт
→ Выслушай, вырази сочувствие: "Мне очень жаль, что так вышло."
→ Не вступай в дискуссию. Не занимай ничью сторону.
→ Попроси имя и телефон для передачи управляющему.
→ Скажи: "Я передам ваш контакт ответственному специалисту — он свяжется с вами лично."

═══════════════════════════════════════
ПОРЯДОК КВАЛИФИКАЦИИ
═══════════════════════════════════════

ДЛЯ ДЕТЕЙ (шаги по порядку, не задавай всё сразу):
1. Приветствие: "Здравствуйте! Меня зовут Алия, консультант шахматной академии 😊 Для кого рассматриваете обучение — для ребёнка или для вас?"
2. Имя обратившегося.
3. Возраст ребёнка.
4. Опыт / разряд.
5. Ссылка на карту: https://gmchess.kz/obuchenievshkole/ — "Посмотрите, какой филиал удобен — GMCA Аркада, GMCA Камал или онлайн?"
   ВАЖНО: После отправки ссылки ОБЯЗАТЕЛЬНО жди конкретного ответа о филиале. Если клиент ответил не по теме (например, опять про опыт ребёнка) — мягко уточни: "Кстати, какой из вариантов вам ближе — Аркада, Камал или онлайн?"
   НЕ переходи к шагу 7, пока не знаешь конкретный филиал.
6. Если клиент выбрал школу GM Legends: "Ваш ребёнок учится в [название школы]?"
   — ДА → оформляем запись в кружок этой школы.
   — НЕТ → объясни по протоколу безопасности школы (см. блок выше), затем предложи GMCA (Аркада/Камал) или онлайн. При желании предложи письмо для администрации школы (см. ниже).
7. Пробный урок (только новичкам / слабому уровню). Предложи И СРАЗУ спроси: "Как зовут вашего сына/дочку?" — не жди отдельного сообщения.
8. Как только получено имя ребёнка — НЕМЕДЛЕННО вызывай register_client_request. Не спрашивай "хотите, чтобы я оформила?" — просто скажи "Оформляю заявку" и вызывай функцию.
9. После успешной регистрации: передай контакт управляющего и попрощайся.

ДЛЯ ВЗРОСЛЫХ:
1. Приветствие + уточнение, что сам хочет учиться.
2. Имя.
3. Сообщи: только индивидуально, от 7 000 тг/урок (очно или онлайн).
4. Ссылка на карту или уточни онлайн → "Какой формат удобнее — GMCA Аркада, Камал или онлайн?"
5. Как только известны имя + формат/филиал — НЕМЕДЛЕННО вызывай register_client_request. Телефон берётся из [ТЕЛЕФОН КЛИЕНТА].

ЕСЛИ ШКОЛА НЕ В ПАРТНЁРАХ (ребёнок не учится ни в одной из наших школ):
→ Предложи GMCA (Аркада/Камал) или онлайн.
→ Предложи отправить письмо для администрации школы:
"Вот текст, который вы можете переслать администрации вашей школы — возможно, удастся открыть кружок у вас:

«Здравствуйте! Я родитель ученика вашей школы. Хотела бы узнать о возможности организовать шахматный кружок. GMCA и GM Legends аккредитованы Казахстанской Федерацией шахмат. Для обсуждения свяжитесь, пожалуйста, с директором GMCA Данияром Омаровым: +7 777 955 9999. Подробнее: https://gmchess.kz/school»"
После — коротко объясни: "Это сообщение поможет школе выйти на нас и, возможно, открыть кружок прямо в вашей школе."

═══════════════════════════════════════
ВОЗРАЖЕНИЯ
═══════════════════════════════════════
"Дорого / высокая цена"
→ "Понимаю, это важный момент. Стоимость связана с качеством преподавания и аккредитацией федерации."
→ Предложи пробный урок (если новичок) или онлайн-формат как более гибкий вариант.

"Неудобное расписание / нет времени"
→ "Расписание важно. Группы формируются под разные графики — утро, вечер."
→ "Давайте соединю с управляющим — они подберут удобное время."

"Далеко добираться"
→ Предложи онлайн или дай ссылку на карту: https://gmchess.kz/obuchenievshkole/

"Мы просто сравниваем школы"
→ "Понимаю! Расскажу об отличиях или предложу пробный урок — без обязательств."

"Ребёнок слишком сильный для кружка"
→ 2 разряд и выше → GMCA Аркада или Камал. Кружки для таких детей не подходят.

═══════════════════════════════════════
ПЕРЕДАЧА НА ЧЕЛОВЕКА
═══════════════════════════════════════
Обязательно передай контакт управляющего или директора, если:
- Клиент просит живого человека.
- Жалоба или конфликт.
- Вопрос о конкретном расписании, деталях оплаты, скидках.
- Вопрос о сотрудничестве, партнёрстве, аренде, найме.
- Нет точного ответа в базе знаний.

Перед передачей: "Хорошо, передам ваш вопрос менеджеру." (номер не запрашивай — он уже известен системе)
После: "Менеджер свяжется с вами в ближайшее время. На всякий случай его контакт: [имя — номер]."

═══════════════════════════════════════
КОНТАКТЫ УПРАВЛЯЮЩИХ
═══════════════════════════════════════
GMCA Аркада: Аяулым Жумажановна — +7 778 104 8197
GMCA Камал: Шолпан Жолдыбаевна — +7 778 104 8127
GM Legends Binom им. Алтынсарина: Томирис Ержанқызы — +7 771 231 4549
GM Legends Binom им. Кекилбаева: Айгерим Аманжолқызы — +7 775 254 2671
GM Legends Binom им. Қадыр Мырза Әлі: Айгерим Аманжолқызы — +7 775 254 2671
GM Legends Riviera International School: Томирис Ержанқызы — +7 771 231 4549
GM Legends Harmony School: Адиль мырза — +7 778 200 2088
GM Legends Steppe School / МША: Айгерим Аманжолқызы — +7 775 254 2671
GM Legends NGS Астана: Ясмин — +7 771 857 5505
__QUANTUM_CONTACT_LINE__
РФМШ: Томирис Ержанқызы — +7 771 231 4549
Онлайн-обучение: Айгерим Аманжолқызы — +7 775 254 2671

Турниры / соревнования: Инжу Муратовна — +7 778 835 4635
  Instagram: https://www.instagram.com/gmca.kz/
  Страница: https://gmchess.kz/turnirygmca/
Детский лагерь GM Camp: Улпан Нурлановна — +7 775 259 3540
  Подробнее: https://gmchess.kz/lagergmca/
Франшиза / партнёрство / корпоратив / коллаборация: Данияр Биржанович Омаров — +7 777 955 9999

═══════════════════════════════════════
АДРЕСА АКАДЕМИЙ GMCA
═══════════════════════════════════════
📍 GMCA Аркада — ул. Айтеке би 15, ЖК "Аркада-1", район Манхэттана, за ТРЦ "Хан-Шатыр" (https://go.2gis.com/gkCPX)
📍 GMCA Камал — пр. Улы Дала 65/2, ЖК "Камал-3", район школы «Дарын» (https://go.2gis.com/zAWEk)

GM Legends (только для учеников соответствующей школы — не перечисляй список без подтверждения):
📍 Binom им. Ы. Алтынсарина
📍 Binom им. Кекилбаева (https://go.2gis.com/KDbzR)
📍 Binom им. Қадыр Мырза Әлі (https://go.2gis.com/gKycJ)
📍 Riviera International School (https://go.2gis.com/cfz56)
📍 Harmony School
📍 International Steppe School of Astana / МША (https://go.2gis.com/hTlBB)
📍 NGS Астана (https://go.2gis.com/chenD)
📍 Quantum STEM School (https://go.2gis.com/mwVqn)
📍 РФМШ (https://go.2gis.com/vjy0U)

Если клиент просит адреса/карту/все локации — дай ссылку: https://gmchess.kz/obuchenievshkole/

═══════════════════════════════════════
ЦЕНЫ
═══════════════════════════════════════
GMCA (академия), дети, групповые:
• Новичок / 5 разряд / 4 разряд — 30 000 тг/мес, 3 раза в неделю по 1 ч
• 3 разряд — 35 000 тг/мес, 3 × 1,5 ч
• 2–1 разряд — 40 000 тг/мес, 3 × 2 ч
• КМС — 40 000 тг/мес, 3 × 2 ч

Индивидуальные (дети и взрослые) — от 7 000 тг/урок (1 час).
Онлайн — та же стоимость, что очный GMCA.

GM Legends (школьные кружки; называй только после подтверждения статуса ученика):
• Binom — от 20 000 тг/мес
• Остальные школы — от 30 000 тг/мес

Точную стоимость уточняет управляющий филиала.

═══════════════════════════════════════
ВРЕМЯ ЗАНЯТИЙ (только общее — точное расписание у управляющего)
═══════════════════════════════════════
GMCA — утром, в обед, после обеда и вечером.
GM Legends — после уроков или до уроков (2 смена).

═══════════════════════════════════════
ПОДБОР ПО УРОВНЮ
═══════════════════════════════════════
Нет опыта / 5 разряд / 4 разряд → GMCA или онлайн; школьный — только если ученик этой школы.
3 разряд → GMCA Аркада или Камал.
2–1 разряд → GMCA Аркада или Камал (от 40 тыс); школьные кружки НЕ предлагать.
КМС / Мастер → только GMCA Аркада.
Взрослый (любой уровень) → только индивидуально GMCA или онлайн.

═══════════════════════════════════════
ПРОБНЫЙ УРОК
═══════════════════════════════════════
ПРЕДЛАГАТЬ: дети-новички или опыт менее 6 месяцев.
НЕ ПРЕДЛАГАТЬ: разряд 3 и выше (кроме 4 разряда — если клиент сам спросил), КМС, Мастер, взрослые.
Формулировка: "Рекомендую начать с бесплатного пробного урока 🎓 — так ребёнок познакомится с тренером и сразу поймёт, нравится ли ему."

═══════════════════════════════════════
МОМЕНТ РЕГИСТРАЦИИ — ДОЖИМАНИЕ
═══════════════════════════════════════
Если клиент проявил интерес (согласился на пробный, спросил о записи, обсудил конкретный филиал):
→ Спроси имя ребёнка: "Как зовут вашего сына/дочку?"
→ Как только получил имя — вызови register_client_request, не откладывая.
→ НЕ спрашивай повторно "Хотите, чтобы я оформила?" — клиент уже проявил интерес.
→ ЗАПРЕЩЕНО говорить "Передам ваш интерес управляющему" вместо создания заявки через register_client_request.

Признаки, что пора регистрировать (даже если клиент говорит "спасибо" или "подумаем"):
- Известны возраст и опыт ребёнка
- Клиент выбрал или обсудил конкретный филиал
- Клиент не отказался явно ("нет", "не надо")

Если клиент говорит "Спасибо, потом решим" и имя ещё не получено:
→ Скажи: "Конечно. Могу сразу оформить предварительную заявку — так менеджер будет готов к вашему звонку. Как зовут вашего сына/дочку?"
→ Одна попытка. Если отказывается — прими и попрощайся.

═══════════════════════════════════════
QOSYMSHA / DAMUBALA
═══════════════════════════════════════
Если клиент спрашивает про Qosymsha, Дамубала, «дамубала», «қосымша», «бесплатно от государства»:
→ Кратко: это программа льготного/бесплатного обучения за счёт государства для подходящих категорий.
→ Точные условия, документы и запись — только у управляющего (GMCA Аркада или Камал).
→ Не обещай участие. Предложи оставить заявку или обратиться к управляющему напрямую.

═══════════════════════════════════════
ОБЩАЯ ИНФОРМАЦИЯ О КОМПАНИИ
═══════════════════════════════════════
GMCA и GM Legends — аккредитованы Казахстанской Федерацией шахмат.
Сооснователи: международный гроссмейстер Ануар Исмагамбетов и предприниматель Данияр Омаров.
Методология: программы по системам чемпионов мира — Крамник, Хоу Ифань, Владимиров, Садвокасов Дармен.
Приложения: ChessClass и Chess Legends.
GMCA — профессиональная академия (от нуля до гроссмейстера).
GM Legends — школьный кружок (от нуля до 2 разряда).
Миссия: воспитать умных, самостоятельных людей.

═══════════════════════════════════════
РАБОТА С ДЕЙСТВУЮЩИМИ УЧЕНИКАМИ
═══════════════════════════════════════
Если система передала [СИСТЕМНОЕ ДОСЬЕ КЛИЕНТА]:
1. НЕ спрашивай имя и телефон — они уже известны.
2. Поздоровайся по имени.
3. Если есть ближайший урок — скажи: "Ждём вас [дата/время] на уроке с [преподаватель]."
4. Если данных нет: "Пока не вижу информации о занятиях — уточним у менеджера."
5. Вопросы по оплате/переносу → контакт управляющего их филиала.

═══════════════════════════════════════
ТЕХНИЧЕСКОЕ — ПРАВИЛА ВЫЗОВА register_client_request
═══════════════════════════════════════
Когда собраны: имя ребёнка/взрослого, телефон (из [ТЕЛЕФОН КЛИЕНТА]), возраст, опыт, конкретный филиал — НЕМЕДЛЕННО вызови register_client_request.
НЕ жди дополнительного подтверждения — вызывай сразу.
НЕ говори "передам ваш интерес" вместо вызова функции.
preference = КОНКРЕТНЫЙ ФИЛИАЛ: "GMCA Аркада", "GMCA Камал", "онлайн" или название школы (например, "Riviera"). Никогда не передавай просто "GMCA" без уточнения.
Успешный результат: 1) заявка в CRM, 2) клиент получил контакт управляющего.
"""

SYSTEM_PROMPT = _SYSTEM_PROMPT_TEMPLATE.replace(
    "__QUANTUM_CONTACT_LINE__",
    _QUANTUM_CONTACT_LINE,
)

app = FastAPI()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

chat_history: Dict[str, List[Dict]] = {}
message_buffers: Dict[str, Dict] = {}
known_users: Dict[str, dict] = {}
last_activity: Dict[str, float] = {}
# Защита от повторной доставки одного и того же входящего (Green API) и гонок при обработке
seen_incoming_ids: Dict[str, deque] = defaultdict(lambda: deque(maxlen=400))
_dialog_locks: Dict[str, asyncio.Lock] = {}


def _dialog_lock(chat_id: str) -> asyncio.Lock:
    if chat_id not in _dialog_locks:
        _dialog_locks[chat_id] = asyncio.Lock()
    return _dialog_locks[chat_id]


def _voice_download_url(msg_data: dict) -> Optional[str]:
    if not msg_data:
        return None
    for key in ("fileMessageData", "voiceMessageData", "audioMessageData"):
        block = msg_data.get(key)
        if isinstance(block, dict):
            url = block.get("downloadUrl")
            if url:
                return url
    return None


def _extract_main_text(msg_data: dict) -> str:
    msg_type = msg_data.get("typeMessage")
    if msg_type == "textMessage":
        return (msg_data.get("textMessageData", {}) or {}).get("textMessage", "")
    if msg_type == "extendedTextMessage":
        return (msg_data.get("extendedTextMessageData", {}) or {}).get("text", "")
    if msg_type == "quotedMessage":
        ext = (msg_data.get("extendedTextMessageData", {}) or {})
        txt = ext.get("text", "")
        if txt:
            return txt
        return (msg_data.get("textMessageData", {}) or {}).get("textMessage", "")
    return ""


def _extract_quoted_text(msg_data: dict) -> str:
    quoted = msg_data.get("quotedMessage") or {}
    if not isinstance(quoted, dict):
        return ""

    q_type = quoted.get("typeMessage")
    if q_type == "textMessage":
        return (quoted.get("textMessageData", {}) or {}).get("textMessage", "")
    if q_type in ("extendedTextMessage", "quotedMessage"):
        ext = (quoted.get("extendedTextMessageData", {}) or {})
        return ext.get("text", "") or ""
    if q_type in ("audioMessage", "voiceMessage"):
        return "[голосовое сообщение]"
    if q_type == "imageMessage":
        return "[изображение]"
    return quoted.get("text", "") or ""

# --- 3. CRM МОДУЛЬ ---
class MoyKlassCRM:
    _TOKEN_TTL = 3300  # секунд (~55 мин), обновляем до истечения часа

    def __init__(self, api_key):
        self.api_key = api_key
        self.token = None
        self.token_fetched_at = 0.0

    async def _get_headers(self):
        now = time.time()
        if not self.token or (now - self.token_fetched_at) > self._TOKEN_TTL:
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.post(f"{MOYKLASS_BASE_URL}/auth/getToken", json={"apiKey": self.api_key})
                    if resp.status_code == 200:
                        self.token = resp.json()["accessToken"]
                        self.token_fetched_at = time.time()
                        logger.info("CRM: Токен получен/обновлён")
                    else:
                        logger.error(f"Auth Error: {resp.status_code} {resp.text}")
                        return None
                except Exception as e:
                    logger.error(f"Connection Error: {e}")
                    return None
        return {"x-access-token": self.token, "Content-Type": "application/json"}

    async def get_schedule(self, user_id, headers):
        schedule_text = "Нет ближайших уроков."
        try:
            async with httpx.AsyncClient() as client:
                teachers_map = {}
                try:
                    r_mgr = await client.get(f"{MOYKLASS_BASE_URL}/managers", headers=headers)
                    if r_mgr.status_code == 200:
                        for m in r_mgr.json():
                            teachers_map[m['id']] = m['name']
                except Exception:
                    pass

                today = datetime.now().strftime("%Y-%m-%d")
                future = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
                params = {
                    "userId": user_id, "date": [today, future],
                    "includeLessons": "true", "limit": 3, "sort": "date"
                }

                resp = await client.get(f"{MOYKLASS_BASE_URL}/lessonRecords", headers=headers, params=params)
                if resp.status_code == 200:
                    records = resp.json().get("lessonRecords", [])
                    if records:
                        lessons_list = []
                        for rec in records:
                            lesson = rec.get("lesson", {})
                            d = lesson.get("date", "")
                            t = lesson.get("beginTime", "")
                            t_ids = lesson.get("teacherIds", [])
                            t_names = [teachers_map.get(tid, "Тренер") for tid in t_ids]
                            teacher_str = ", ".join(t_names) if t_names else "без тренера"
                            lessons_list.append(f"{d} в {t} (Преп: {teacher_str})")
                        schedule_text = "; ".join(lessons_list)
        except Exception as e:
            logger.error(f"Ошибка расписания: {e}")
        return schedule_text

    async def get_class_name(self, class_id, headers):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{MOYKLASS_BASE_URL}/classes/{class_id}", headers=headers)
                if resp.status_code == 200:
                    return resp.json().get('name', 'Группа')
        except Exception:
            pass
        return "Группа"

    @staticmethod
    def _digits(value: str) -> str:
        return re.sub(r"[^\d]", "", value or "")

    @staticmethod
    def _extract_age_number(age_value: str) -> Optional[int]:
        try:
            match = re.search(r"\d+", str(age_value))
            return int(match.group()) if match else None
        except Exception:
            return None

    def _pick_manager_phone(self, filial_id, matched_key: Optional[str]) -> str:
        # Для "сборного" filial_id 54672 выбираем телефон по конкретному ключу школы.
        if matched_key and matched_key in BRANCH_PHONES:
            return BRANCH_PHONES[matched_key]
        if filial_id in BRANCH_PHONES:
            return BRANCH_PHONES[filial_id]
        return BRANCH_PHONES["default"]

    async def _resolve_manager_id(self, client: httpx.AsyncClient, headers: dict, manager_phone: str) -> int:
        manager_phone_digits = self._digits(manager_phone)
        if not manager_phone_digits:
            return MANAGER_ID
        try:
            resp = await client.get(f"{MOYKLASS_BASE_URL}/managers", headers=headers)
            if resp.status_code != 200:
                return MANAGER_ID
            managers = resp.json() or []
            for manager in managers:
                for field in ("phone", "mobilePhone", "phoneNumber", "tel"):
                    candidate = self._digits(str(manager.get(field, "")))
                    if candidate and (candidate.endswith(manager_phone_digits[-10:]) or manager_phone_digits.endswith(candidate[-10:])):
                        return manager.get("id", MANAGER_ID)
        except Exception as e:
            logger.warning(f"Не удалось определить managerId по телефону {manager_phone}: {e}")
        return MANAGER_ID

    async def find_user_smart(self, phone):
        headers = await self._get_headers()
        if not headers:
            return None

        clean = re.sub(r"[^\d]", "", phone)
        phones_to_try = []
        if len(clean) == 11:
            base = clean[1:]
            phones_to_try = ["7" + base, "8" + base, "+7" + base, "+8" + base]
        else:
            phones_to_try = [clean]

        async with httpx.AsyncClient() as client:
            for p in phones_to_try:
                try:
                    url = f"{MOYKLASS_BASE_URL}/users?phone={p}&includeJoins=true"
                    resp = await client.get(url, headers=headers)

                    if resp.status_code == 200:
                        data = resp.json()
                        users = data.get("users", [])
                        if users:
                            user = users[0]
                            user_id = user['id']
                            logger.info(f"НАЙДЕН КЛИЕНТ: {user['name']} (ID {user_id})")

                            groups_text = "Нет активных групп"
                            active_filial_id = None
                            joins = user.get("joins", [])

                            if joins:
                                active_groups = []
                                for join in joins[-2:]:
                                    c_name = await self.get_class_name(join['classId'], headers)
                                    status = "Активен" if join['statusId'] == 2 else "Архив"
                                    active_groups.append(f"{c_name} ({status})")
                                    if join['statusId'] == 2 and join.get('filialId'):
                                        active_filial_id = join['filialId']
                                if active_groups:
                                    groups_text = ", ".join(active_groups)

                            schedule_text = await self.get_schedule(user_id, headers)

                            dossier = {
                                "id": user_id,
                                "name": user['name'],
                                "groups": groups_text,
                                "schedule": schedule_text,
                                "balance": user.get('balans', 0),
                                "filial_id": active_filial_id
                            }
                            return {"user": user, "dossier": dossier}
                except Exception:
                    pass

        logger.info(f"Клиент не найден (проверено {len(phones_to_try)} вар).")
        return None

    async def create_lead(self, name, phone, age, experience, preference):
        headers = await self._get_headers()
        if not headers:
            logger.error("create_lead: не удалось получить токен CRM")
            return (
                "СИСТЕМНОЕ СООБЩЕНИЕ: ОШИБКА CRM (авторизация). "
                "Дай клиенту контакт управляющего напрямую и скажи что заявку оформит менеджер."
            )

        clean_phone = re.sub(r"[^\d]", "", phone)
        wa_link = f"https://wa.me/{clean_phone}"

        filial_id = None
        matched_key = None
        mgr_phone_text = ""

        if preference:
            branch_lower = preference.lower()
            for key, f_id in FILIALS_MAP.items():
                if key in branch_lower:
                    matched_key = key
                    filial_id = f_id
                    mgr_phone = self._pick_manager_phone(filial_id, matched_key)
                    mgr_phone_text = f"Номер управляющего филиалом: {mgr_phone}"
                    break

        logger.info(f"Выбран филиал: {preference!r} -> ID {filial_id}, matched_key={matched_key!r}")

        if not filial_id:
            return (
                "СИСТЕМНОЕ СООБЩЕНИЕ: ЗАЯВКУ В CRM НЕ СОЗДАВАЙ. "
                "Причина: не выбран филиал/формат. "
                "Попроси клиента выбрать конкретно: GMCA Аркада, GMCA Камал или онлайн. "
                "Только после этого вызывай регистрацию заявки."
            )

        birth_attr = []
        age_num = self._extract_age_number(age)
        try:
            if age_num is not None:
                if age_num < 5:
                    return (
                        "СИСТЕМНОЕ СООБЩЕНИЕ: ЗАЯВКУ В CRM НЕ СОЗДАВАЙ. "
                        "Причина: ребенку меньше 5 лет. "
                        "Корректно объясни, что сейчас набор с 5 лет, "
                        "поблагодари и предложи вернуться, когда ребенку исполнится 5."
                    )
                year = datetime.now().year - age_num
                birth_attr = [{"attributeId": 1, "value": f"{year}-01-01"}]
        except Exception:
            pass

        full_text = (
            f"BOT ЗАЯВКА:\n"
            f"Имя: {name}\n"
            f"Телефон: {phone}\n"
            f"Филиал: {preference}\n"
            f"Возраст: {age}\n"
            f"Опыт: {experience}\n"
            f"WhatsApp: {wa_link}"
        )

        async with httpx.AsyncClient() as client:
            manager_phone = self._pick_manager_phone(filial_id, matched_key)
            manager_id = await self._resolve_manager_id(client, headers, manager_phone)
            logger.info(f"create_lead: manager_id={manager_id}, manager_phone={manager_phone}")

            user_id = None
            found_data = await self.find_user_smart(clean_phone)

            if found_data:
                user_id = found_data["user"]["id"]
                logger.info(f"create_lead: найден существующий пользователь id={user_id}")
                comment_resp = await client.post(
                    f"{MOYKLASS_BASE_URL}/userComments", headers=headers,
                    json={"userId": user_id, "comment": full_text}
                )
                logger.info(f"create_lead: userComments -> {comment_resp.status_code}")
            else:
                payload = {
                    "name": name,
                    "phone": clean_phone,
                    "responsibles": [manager_id],
                    "attributes": birth_attr
                }
                if filial_id:
                    payload["filials"] = [filial_id]

                logger.info(f"create_lead: создаём нового пользователя payload={payload}")
                create_resp = await client.post(f"{MOYKLASS_BASE_URL}/users", headers=headers, json=payload)
                logger.info(f"create_lead: POST /users -> {create_resp.status_code} {create_resp.text[:300]}")

                if create_resp.status_code in [200, 201]:
                    user_id = create_resp.json()["id"]
                    comment_resp = await client.post(
                        f"{MOYKLASS_BASE_URL}/userComments", headers=headers,
                        json={"userId": user_id, "comment": full_text}
                    )
                    logger.info(f"create_lead: userComments -> {comment_resp.status_code}")
                else:
                    logger.error(f"create_lead: не удалось создать пользователя: {create_resp.status_code} {create_resp.text}")
                    return (
                        f"СИСТЕМНОЕ СООБЩЕНИЕ: ОШИБКА CRM (не удалось создать пользователя, код {create_resp.status_code}). "
                        "Дай клиенту контакт управляющего напрямую."
                    )

            if user_id is None:
                logger.error("create_lead: user_id не получен, заявка не создана")
                return (
                    "СИСТЕМНОЕ СООБЩЕНИЕ: ОШИБКА CRM (user_id не получен). "
                    "Дай клиенту контакт управляющего напрямую."
                )

            join_payload = {
                "userId": user_id, "statusId": 1, "classId": LEAD_CLASS_ID,
                "comment": full_text, "managerId": manager_id
            }
            if filial_id:
                join_payload["filialId"] = filial_id

            logger.info(f"create_lead: POST /joins payload={join_payload}")
            join_resp = await client.post(f"{MOYKLASS_BASE_URL}/joins", headers=headers, json=join_payload)
            logger.info(f"create_lead: POST /joins -> {join_resp.status_code} {join_resp.text[:300]}")

            if join_resp.status_code not in [200, 201]:
                logger.error(f"create_lead: ошибка создания заявки joins: {join_resp.status_code} {join_resp.text}")
                return (
                    f"СИСТЕМНОЕ СООБЩЕНИЕ: ОШИБКА CRM (заявка не создана, код {join_resp.status_code}). "
                    "Дай клиенту контакт управляющего напрямую и скажи: 'Менеджер оформит запись лично'."
                )

            try:
                now = datetime.now().strftime("%Y-%m-%d")
                task_resp = await client.post(f"{MOYKLASS_BASE_URL}/tasks", headers=headers, json={
                    "userId": user_id, "body": full_text,
                    "beginDate": now, "endDate": now,
                    "typeId": 1, "managerId": manager_id
                })
                logger.info(f"create_lead: POST /tasks -> {task_resp.status_code}")
            except Exception as e:
                logger.warning(f"create_lead: задача не создана: {e}")

            logger.info(f"create_lead: УСПЕХ — заявка создана для {name} ({clean_phone}), филиал {filial_id}")
            final_msg = "СИСТЕМНОЕ СООБЩЕНИЕ: УСПЕХ. Заявка создана. Попрощайся. "
            if mgr_phone_text:
                final_msg += f"ОБЯЗАТЕЛЬНО напиши клиенту этот номер: {mgr_phone_text}"

            return final_msg

crm = MoyKlassCRM(MOYKLASS_API_KEY)

# --- 4. TOOLS ---
tools = [
    {
        "type": "function",
        "function": {
            "name": "register_client_request",
            "description": "Записать заявку клиента в CRM. Вызывай когда собраны: имя, телефон, возраст, опыт и филиал.",
            "parameters": {
                "type": "object",
                "properties": {
                    "client_name": {"type": "string", "description": "Имя клиента"},
                    "client_phone": {"type": "string", "description": "Телефон: в WhatsApp укажи номер из чата, если клиент не дал другой явно"},
                    "client_age": {"type": "string", "description": "Возраст (ребёнка или клиента)"},
                    "experience": {"type": "string", "description": "Опыт в шахматах / разряд"},
                    "preference": {"type": "string", "description": "Выбранный филиал или формат обучения"}
                },
                "required": ["client_name", "client_phone", "client_age", "experience", "preference"]
            }
        }
    }
]

# --- 5. WHATSAPP ---
def sanitize_bot_outgoing(text: Optional[str]) -> str:
    """Убираем запрещённые формулировки и восклицательные знаки — модель иногда их игнорирует."""
    if not text:
        return ""
    t = text
    t = re.sub(r"(?i)\bна\s+здоровье\b[!.,\s]*", "Пожалуйста. ", t)
    t = t.replace("!", ".")
    t = re.sub(r"\.{3,}", ".", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


async def send_whatsapp(chat_id, text):
    # Доп. нормализация на случай прямых вызовов (голосовые ошибки и т.д.)
    text = sanitize_bot_outgoing(text)
    url = f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chatId": chat_id, "message": text})

# --- 6. ЛОГИКА ДИАЛОГА ---
async def process_dialog(chat_id):
    async with _dialog_lock(chat_id):
        buffer = message_buffers.pop(chat_id, None)
        if not buffer:
            return
        user_text = " ".join(buffer["messages"])
        logger.info(f"Обработка для {chat_id}: {user_text}")

        current_time = time.time()
        if chat_id in last_activity:
            if current_time - last_activity[chat_id] > SESSION_TIMEOUT:
                logger.info(f"Сброс памяти для {chat_id}")
                chat_history.pop(chat_id, None)
                known_users.pop(chat_id, None)
        last_activity[chat_id] = current_time

        if chat_id not in chat_history:
            chat_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

            current_phone = chat_id.split("@")[0]
            chat_history[chat_id].append({
                "role": "system",
                "content": (
                    f"[ТЕЛЕФОН КЛИЕНТА]: {current_phone}. Чат в WhatsApp — номер уже есть. "
                    f"Для заявки в CRM используй этот номер; не проси продиктовать телефон для связи здесь, "
                    f"если клиент сам не попросил указать другой."
                )
            })

            if chat_id not in known_users:
                phone = chat_id.split("@")[0]
                found = await crm.find_user_smart(phone)

                if found:
                    user_obj = found["user"]
                    dossier = found["dossier"]
                    known_users[chat_id] = user_obj

                    mgr_contact = ""
                    if dossier["filial_id"] and dossier["filial_id"] in BRANCH_PHONES:
                        mgr_contact = f"Его менеджер: {BRANCH_PHONES[dossier['filial_id']]}."

                    inject_msg = (
                        f"[СИСТЕМНОЕ ДОСЬЕ КЛИЕНТА]\n"
                        f"Имя: {dossier['name']}\n"
                        f"Группы: {dossier['groups']}\n"
                        f"Ближайшие уроки: {dossier['schedule']}\n"
                        f"Баланс: {dossier['balance']}\n"
                        f"{mgr_contact}\n"
                        f"ИНСТРУКЦИЯ: Это действующий ученик! НЕ СПРАШИВАЙ ИМЯ И ТЕЛЕФОН.\n"
                        f"1. Поздоровайся по имени.\n"
                        f"2. Если есть урок в поле 'Ближайшие уроки', ОБЯЗАТЕЛЬНО скажи: 'Ждем вас [Дата/Время] на уроке с [Имя преподавателя]'.\n"
                        f"3. Если нет — спроси, чем помочь.\n"
                        f"4. Если вопрос сложный, дай номер менеджера филиала."
                    )
                    chat_history[chat_id].append({"role": "system", "content": inject_msg})
                    logger.info(f"Загружено досье: {dossier['name']}")

        user_payload = (
            "[ОПРЕДЕЛИ ЯЗЫК ЭТОГО СООБЩЕНИЯ КЛИЕНТА САМОСТОЯТЕЛЬНО "
            "И ОТВЕТЬ НА ЭТОМ ЖЕ ЯЗЫКЕ. ЕСЛИ КЛИЕНТ ПРОСИТ КОНКРЕТНЫЙ ЯЗЫК — "
            "СРАЗУ ПЕРЕКЛЮЧИСЬ НА НЕГО.]\n"
            f"{user_text}"
        )

        chat_history[chat_id].append({"role": "user", "content": user_payload})

        try:
            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=chat_history[chat_id],
                tools=tools,
                tool_choice="auto",
                temperature=0.5
            )
            msg = response.choices[0].message

            if msg.tool_calls:
                chat_history[chat_id].append(msg)
                for tool in msg.tool_calls:
                    args = json.loads(tool.function.arguments)
                    logger.info(f"CRM вызов: {args}")

                    phone_to_save = args.get("client_phone")
                    if not phone_to_save or "не указа" in phone_to_save.lower():
                        phone_to_save = chat_id.split("@")[0]

                    result_text = await crm.create_lead(
                        name=args.get("client_name", "Клиент"),
                        phone=phone_to_save,
                        age=args.get("client_age", "-"),
                        experience=args.get("experience", "-"),
                        preference=args.get("preference", "Не выбрано")
                    )

                    chat_history[chat_id].append({
                        "tool_call_id": tool.id,
                        "role": "tool",
                        "name": tool.function.name,
                        "content": result_text
                    })

                final = await openai_client.chat.completions.create(
                    model=OPENAI_MODEL, messages=chat_history[chat_id]
                )
                bot_answer = final.choices[0].message.content
            else:
                bot_answer = msg.content

            sanitized_answer = sanitize_bot_outgoing(bot_answer)
            chat_history[chat_id].append({"role": "assistant", "content": sanitized_answer})
            await send_whatsapp(chat_id, sanitized_answer)

        except Exception as e:
            logger.error(f"Ошибка AI: {e}")
            try:
                await send_whatsapp(
                    chat_id,
                    "Произошла техническая ошибка. Напишите ещё раз или обратитесь к менеджеру напрямую."
                )
            except Exception as send_err:
                logger.error(f"Не удалось отправить fallback: {send_err}")

# --- 7. ВЕБХУК ---
@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    if data.get("typeWebhook") != "incomingMessageReceived":
        return "ok"

    sender = data.get("senderData", {}).get("chatId")
    if not sender:
        return "ok"

    msg_data = data.get("messageData") or {}
    id_message = data.get("idMessage") or msg_data.get("idMessage")
    if id_message:
        dq = seen_incoming_ids[sender]
        if id_message in dq:
            logger.info(f"Повтор webhook idMessage={id_message} для {sender}, пропуск")
            return "ok"
        dq.append(id_message)

    text = ""
    msg_type = msg_data.get("typeMessage")

    if msg_type in ("audioMessage", "voiceMessage"):
        url = _voice_download_url(msg_data)
        if not url:
            logger.error(f"Нет downloadUrl для голосового type={msg_type}, keys={list(msg_data.keys())}")
            await send_whatsapp(
                sender,
                "Получила голосовое, но не смогла загрузить файл. Напишите, пожалуйста, текстом — так я точно отвечу.",
            )
            return "ok"
        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                audio_response = await client.get(url)
                audio_response.raise_for_status()
                audio_bytes = audio_response.content

            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "voice.ogg"
            transcription = await openai_client.audio.transcriptions.create(
                model="whisper-1", file=audio_file
            )
            text = f"[Голосовое]: {transcription.text}"
            logger.info(f"ГС распознано: {text}")
        except Exception as e:
            logger.error(f"Ошибка аудио: {e}")
            try:
                await send_whatsapp(
                    sender,
                    "Не получилось распознать голосовое. Повторите запись или напишите текстом, пожалуйста.",
                )
            except Exception as send_err:
                logger.error(f"Не удалось отправить ответ про голосовое: {send_err}")
            return "ok"
    else:
        text = _extract_main_text(msg_data).strip()
        quoted_text = _extract_quoted_text(msg_data).strip()
        if quoted_text:
            # Передаем контекст реплая, чтобы "Да/Нет" правильно интерпретировались.
            text = (
                f"[REPLY_TO]: {quoted_text}\n"
                f"[CLIENT_MESSAGE]: {text}"
            ).strip()

    if not text:
        return "ok"

    logger.info(f"Входящее ({sender}): {text}")

    if sender in message_buffers:
        message_buffers[sender]["timer"].cancel()
    else:
        message_buffers[sender] = {"messages": []}

    message_buffers[sender]["messages"].append(text)
    message_buffers[sender]["timer"] = asyncio.create_task(wait_user_input(sender))
    return "ok"

async def wait_user_input(chat_id):
    await asyncio.sleep(BUFFER_DELAY)
    await process_dialog(chat_id)
