import os
import asyncio
import json
import httpx
import re
import io
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from fastapi import FastAPI, Request
from openai import AsyncOpenAI
from dotenv import load_dotenv

# --- 1. НАСТРОЙКИ ---
load_dotenv()

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

GREEN_API_ID = os.getenv("GREEN_API_ID")
GREEN_API_TOKEN = os.getenv("GREEN_API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MOYKLASS_API_KEY = os.getenv("MOYKLASS_API_KEY")

BUFFER_DELAY = 6.0
MOYKLASS_BASE_URL = "https://api.moyklass.com/v1/company"
LEAD_CLASS_ID = 341820
MANAGER_ID = 98753
SESSION_TIMEOUT = 5 * 60 * 60 # 5 часов

# --- ТЕЛЕФОНЫ МЕНЕДЖЕРОВ (ИЗ ТВОЕГО ТЕКСТА) ---
# Ключ: ID филиала (или название). Значение: Номер телефона.
BRANCH_PHONES = {
    37754: "+7 778 104 8197", # Аркада (Аяулым Жумажановна)
    42763: "+7 778 104 8127", # Камал (Шолпан Жолдыбаевна)
    44021: "+7 771 231 4549", # Алтынсарина (Томирис)
    54673: "+7 775 254 2671", # Кекилбаева (Айгерим)
    54648: "+7 775 254 2671", # Кадыр Мырза Али (Айгерим)
    47033: "+7 771 231 4549", # Riviera (Томирис)
    "harmony": "+7 778 200 2088", # Harmony (Адиль мырза)
    60426: "+7 775 254 2671", # Steppe/МША (Айгерим)
    "ngs": "+7 771 857 5505", # NGS (Ясмин)
    "quantum": "+7 705 287 6382", # Quantum (Зарина)
    "рфмш": "+7 771 231 4549", # РФМШ (Томирис)
    "default": "+7 708 174 7426" # Общий
}

# КАРТА ФИЛИАЛОВ (Для поиска ID)
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

# --- 2. ПОЛНЫЙ СИСТЕМНЫЙ ПРОМПТ (ТВОЙ НОВЫЙ ТЕКСТ) ---
SYSTEM_PROMPT = """
Вы — консультант шахматной академии GMCA и сети шахматных кружков при школах GM Legends в Астане.
Ваша задача — помочь клиенту выбрать формат обучения, собрать информацию (имя, возраст, школа/филиал, уровень, контактные данные) и передать её менеджеру в CRM Moyklass.
Вы не записываете клиента напрямую, а передаёте заявку для дальнейшей связи с менеджером, который подтвердит занятие и согласует время.

Шаг 1. Приветствие. Определение возраста и опыта игры в шахматы.
«Здравствуйте! 🌸 Меня зовут Алия, я консультант шахматной академии GMCA и шахматных кружков GMLegends. Как я могу к вам обращаться?
Подскажите, вы рассматриваете обучение для взрослого или для ребёнка?»

После ответа:
Если ребёнок → уточните возраст и разряд/был ли опыт занятия шахматами и когда удобно посещать занятия.
«Сколько лет вашему ребёнку?»
«А какой у него разряд или опыт в шахматах?»

ЛОГИКА ПОДБОРА (GMCA):
- Если есть опыт более трёх лет или второй, первый разряд, то предложи профессиональные академии GMCA:
📍 Аркада — ул. Айтеке би 15, ЖК “Аркада-1”, район Манхэттана, за ТРЦ “Хан-Шатыр”
📍 Камал — пр. Улы Дала 65/2, ЖК “Камал-3”, пересечение с Мангилик Ел»
Если никакой из этих адресов не подходит, то предложи обучение в онлайн формате.
- Если есть опыт более пяти лет или клиент кандидат в мастера спорта/мастер спорта, то предложи профессиональную академию GMCA (Аркада).

ЛОГИКА ПОДБОРА (ОБЩАЯ):
- Если нет опыта или у ребёнка 3 разряд/4 разряд/5 разряд или нет разряда, то переходи к шагу 2 по определению ближайшего филиала.
- Если возраст ребёнка от 13 лет и выше при этом ребёнок без опыта, то нужно предупредить родителя морально подготовить ребёнка к тому, что он будет вначале тренироваться с детьми младше его по возрасту, но это продлиться не долго. Обычно более взрослые дети в первые два-три месяца начинают приближаться по уровню игры к своим сверстникам. Также и мы постараемся побыстрее двигать старшего ребёнка к более старшим ребятам, чтобы он чувствовал себя комфортно.

ВЗРОСЛЫЕ:
- Если это взрослый, то нужно предупредить, что взрослых мы обучаем только в формате индивидуального обучения.
- Стоимость индивидуального обучения зависит от квалификации тренера и начинается от 7 тысяч тенге за один урок (длительность один астрономический час).
- Если клиент согласен, то можно переходить к выбору ближайшего филиала.

Шаг 2. Определение ближайшего филиала.
«Отлично! Давайте подберём самый удобный для вас филиал:
GMCA (шахматная академия) – стоимость от 30 тысяч тг/мес:
📍 Аркада — ул. Айтеке би 15, ЖК “Аркада-1”, район Манхэттана, за ТРЦ “Хан-Шатыр” (https://go.2gis.com/gkCPX)
📍 Камал — пр. Улы Дала 65/2, ЖК “Камал-3”, район школы «Дарын» (https://go.2gis.com/zAWEk)

GM Legends (шахматный кружок при школах):
📍Binom School им. Ы.Алтынсарина
📍Binom School им. Кекилбаева (https://go.2gis.com/KDbzR)
📍Binom School им. Қадыр Мырза Әлі (https://go.2gis.com/gKycJ)
📍Riviera International School (https://go.2gis.com/cfz56)
📍Harmony School (могут посещать только ученики данной школы)
📍International Steppe School of Astana (https://go.2gis.com/hTlBB)
📍NGS Астана (https://go.2gis.com/chenD)
📍Quantum STEM School (https://go.2gis.com/mwVqn)
📍РФМШ (https://go.2gis.com/vjy0U)
Филиал GMCA Аркада: Аяулым Жумажановна +7 778 104 8197
Филиал GMCA Камал: Шолпан Жолдыбаевна +7 778 104 8127
Филиал GM Legends Binom School им. Ы.Алтынсарина: Томирис Ержанқызы +7 771 231 4549
Филиал GM Legends Binom School им. Кекилбаева: Айгерим Аманжолқызы +7 775 254 2671
Филиал GM Legends Binom School им. Қадыр Мырза Әлі: Айгерим Аманжолқызы +7 775 254 2671
Филиал GM Legends Riviera: Томирис Ержанқызы +7 771 231 4549
Филиал GM Legends Harmony School: Адиль мырза +7 778 200 2088
Филиал GM Legends International Steppe School of Astana(бывший МША): Айгерим Аманжолқызы +7 775 254 2671
Филиал GM Legends NGS Астана: мисс Ясмин +7 771 857 5505
Филиал GM Legends Quantum STEM School: Зарина устаз +7 705 287 6382
Филиал РФМШ: Томирис Ержанқызы +7 771 231 4549
Если никакой из этих адресов не подходит, то предложи обучение в онлайн формате.
Если это взрослый клиент, то предлагай только филиалы GMCA, так как в школе мы взрослых не обучаем.

Шаг 3. Бесплатное пробное занятие
Предложите бесплатное пробное занятие:
«Рекомендую посетить бесплатный пробный урок 🎓, чтобы познакомиться с тренером. Важно, чтобы тренер понравился ребёнку.»

ИСКЛЮЧЕНИЯ (КОМУ НЕ ПРЕДЛАГАТЬ ПРОБНЫЙ):
- Если клиент занимался шахматами более шести месяцев или имеет разряд (пятый разряд не считается, он выдуманный).
- Если ребёнок 3 разряд, 2 разряд, 1 разряд, КМС или Мастер спорта.
(Если у ребёнка нет опыта или опыт менее 6 месяцев — пробный предлагать).
(Если клиент с 4 разрядом САМ спросил про пробный — можно допустить).

Шаг 4. Подтверждение контактов и детализация информации.
Для подтверждения информации уточни у клиента:
Имя обратившегося
Имя ребёнка (при необходимости)
Возраст (если для ребёнка)
Контактный номер (номер обратившегося)
Опыт/разряд: укажи если клиент указывал
Формат (офлайн/онлайн)
Филиал

ФИНАЛ:
Передайте менеджеру (вызов функции register_client_request).
Ответ пользователю (ПОСЛЕ УСПЕШНОГО ВЫЗОВА):
«Спасибо, {{client_name}}! 💬 Я передала вашу заявку управляющему филиала. Он свяжется с вами в ближайшее время, чтобы подобрать удобное время.»




Также нужно передать клиенту номер телефона управляющего филиалом:
[Номера телефонов бот подставит автоматически из базы знаний ниже, если клиент выбрал филиал]

--- СПРАВОЧНАЯ ИНФОРМАЦИЯ ---
Стоимость обучения в академии шахмат GMCA:
Групповые занятия (дети):
• Начинающие, 5 разряд, 4 разряд — 30 000 тг/мес, 3 раза в неделю по 1 часу
• 3 разряд — 35 000 тг/мес, 3 раза в неделю по 1,5 часа
• 1 разряд, 2 разряд – 40 000 тг/мес, 3 раза в неделю по 2 часа
• КМС – 40 000 тг/мес, 3 раза в неделю по 2 часа
Индивидуальные занятия от 7 000 тг за 1 урок (1 час).

Стоимость обучения в шахматном кружке GM Legends:
• Во всех школах (кроме Binom) — от 30 000 тг/мес.
• В школах Binom — от 20 000 тг/мес.

Время занятий:
GMCA — утром, до обеда, после обеда и вечером.
GM Legends — после уроков или до начала уроков (2 смена).

Онлайн-занятия: Стоимость такая же, как при очных занятиях GMCA.
Франшиза: Если интересно — задать вопросы (опыт, город, почему шахматы) и дать номер Данияра +7 777 955 9999.

ОБЩАЯ ИНФОРМАЦИЯ (ПО ЗАПРОСУ):
GMCA — профи академия (от нуля до гроссмейстера).
GM Legends — школьный кружок (от нуля до 2 разряда).
Миссия: Воспитать умных, самостоятельных людей.
Методика: Живое преподавание + ChessClass (видеоуроки чемпионов).
Лагерь GM Camp: Досуг без гаджетов.
Qosymsha (Damubala): Бесплатное обучение за счет государства.

--- РАБОТА С СУЩЕСТВУЮЩИМИ УЧЕНИКАМИ ---
Если система сообщает [СИСТЕМНОЕ ДОСЬЕ КЛИЕНТА]:
1. Уже не спрашивай имя и телефон.
2. Дай расписание на ближайший урок и имя преподавателя.
3. Если вопрос сложный — дай номер менеджера.
4. Если данных нет: «Пока не вижу информации о занятиях...»

⚠️ ТЕХНИЧЕСКОЕ ЗАДАНИЕ:
Когда собраны данные (Имя, Телефон, Возраст, Опыт, Филиал), вызовите `register_client_request`.
Не пишите "Новая заявка..." текстом. Только через функцию.
"""

app = FastAPI()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

chat_history: Dict[str, List[Dict]] = {}
message_buffers: Dict[str, Dict] = {}
known_users: Dict[str, dict] = {}
last_activity: Dict[str, float] = {} 

# --- 3. CRM МОДУЛЬ ---
class MoyKlassCRM:
    def __init__(self, api_key):
        self.api_key = api_key
        self.token = None

    async def _get_headers(self):
        if not self.token:
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.post(f"{MOYKLASS_BASE_URL}/auth/getToken", json={"apiKey": self.api_key})
                    if resp.status_code == 200:
                        self.token = resp.json()["accessToken"]
                        logger.info("✅ CRM: Токен получен")
                    else:
                        logger.error(f"❌ Auth Error: {resp.text}")
                        return None
                except Exception as e:
                    logger.error(f"❌ Connection Error: {e}")
                    return None
        return {"x-access-token": self.token, "Content-Type": "application/json"}

    async def get_schedule(self, user_id, headers):
        """Получает расписание + ИМЕНА ПРЕПОДАВАТЕЛЕЙ"""
        schedule_text = "Нет ближайших уроков."
        try:
            async with httpx.AsyncClient() as client:
                # 1. Скачиваем список учителей
                teachers_map = {}
                try:
                    r_mgr = await client.get(f"{MOYKLASS_BASE_URL}/managers", headers=headers)
                    if r_mgr.status_code == 200:
                        for m in r_mgr.json():
                            teachers_map[m['id']] = m['name']
                except: pass

                # 2. Скачиваем уроки
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
        except: pass
        return "Группа"

    async def find_user_smart(self, phone):
        """УМНЫЙ ПОИСК (7, 8, +7, +8) + ДОСЬЕ"""
        headers = await self._get_headers()
        if not headers: return None
        
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
                            logger.info(f"✅ НАЙДЕН КЛИЕНТ: {user['name']} (ID {user_id})")
                            
                            # --- СБОР ДОСЬЕ ---
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
                except: pass
        
        logger.info(f"👤 Клиент не найден (проверено {len(phones_to_try)} вар).")
        return None

    async def create_lead(self, name, phone, age, experience, preference):
        headers = await self._get_headers()
        clean_phone = re.sub(r"[^\d]", "", phone)
        wa_link = f"https://wa.me/{clean_phone}"
        
        filial_id = None
        mgr_phone_text = "" # Номер менеджера для ответа

        if preference:
            branch_lower = preference.lower()
            for key, f_id in FILIALS_MAP.items():
                if key in branch_lower:
                    filial_id = f_id
                    # Находим телефон менеджера этого филиала
                    mgr_phone = BRANCH_PHONES.get(f_id, BRANCH_PHONES["default"])
                    mgr_phone_text = f"Номер управляющего филиалом: {mgr_phone}"
                    break
        
        logger.info(f"📍 Выбран филиал: {preference} -> ID {filial_id}")

        # ДР
        birth_attr = []
        try:
            age_num = int(re.search(r'\d+', str(age)).group())
            year = datetime.now().year - age_num
            birth_attr = [{"attributeId": 1, "value": f"{year}-01-01"}]
        except: pass

        full_text = (
            f"🤖 БОТ ЗАЯВКА:\n"
            f"👤 {name}\n"
            f"📱 {phone}\n"
            f"📍 Выбор: {preference}\n"
            f"👶 Возраст: {age}\n"
            f"♟️ Опыт: {experience}\n"
            f"🔗 WhatsApp: {wa_link}"
        )

        async with httpx.AsyncClient() as client:
            user_id = None
            found_data = await self.find_user_smart(clean_phone)
            
            if found_data:
                user_id = found_data["user"]["id"]
                await client.post(f"{MOYKLASS_BASE_URL}/userComments", headers=headers, json={
                    "userId": user_id, "comment": full_text
                })
            else:
                payload = {
                    "name": name, 
                    "phone": clean_phone, 
                    "responsibles": [MANAGER_ID],
                    "attributes": birth_attr
                }
                if filial_id: payload["filials"] = [filial_id]

                create_resp = await client.post(f"{MOYKLASS_BASE_URL}/users", headers=headers, json=payload)
                if create_resp.status_code in [200, 201]:
                    user_id = create_resp.json()["id"]
                    await client.post(f"{MOYKLASS_BASE_URL}/userComments", headers=headers, json={
                        "userId": user_id, "comment": full_text
                    })
                else:
                    return f"ERROR CRM: {create_resp.text}"

            # Заявка
            join_payload = {
                "userId": user_id, "statusId": 1, "classId": LEAD_CLASS_ID,
                "comment": full_text, "managerId": MANAGER_ID
            }
            if filial_id: join_payload["filialId"] = filial_id
            await client.post(f"{MOYKLASS_BASE_URL}/joins", headers=headers, json=join_payload)

            # Задача
            try:
                now = datetime.now().strftime("%Y-%m-%d")
                await client.post(f"{MOYKLASS_BASE_URL}/tasks", headers=headers, json={
                    "userId": user_id, "body": full_text,
                    "beginDate": now, "endDate": now,
                    "typeId": 1, "managerId": MANAGER_ID
                })
            except: pass

            # Добавляем в ответ номер менеджера, если филиал был определен
            final_msg = f"СИСТЕМНОЕ СООБЩЕНИЕ: УСПЕХ. Заявка создана. Попрощайся. "
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
            "description": "Записать заявку в CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "client_name": {"type": "string", "description": "Имя клиента"},
                    "client_phone": {"type": "string", "description": "Телефон"},
                    "client_age": {"type": "string", "description": "Возраст"},
                    "experience": {"type": "string", "description": "Опыт"},
                    "preference": {"type": "string", "description": "Филиал/Школа"}
                },
                "required": ["client_name", "client_phone"]
            }
        }
    }
]

# --- 5. WHATSAPP ---
async def send_whatsapp(chat_id, text):
    url = f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chatId": chat_id, "message": text})

# --- 6. ЛОГИКА ---
async def process_dialog(chat_id):
    buffer = message_buffers.pop(chat_id, None)
    if not buffer: return
    user_text = " ".join(buffer["messages"])
    logger.info(f"📩 Обработка для {chat_id}: {user_text}")

    # Сброс сессии
    current_time = time.time()
    if chat_id in last_activity:
        if current_time - last_activity[chat_id] > SESSION_TIMEOUT:
            logger.info(f"🧹 Сброс памяти для {chat_id}")
            chat_history.pop(chat_id, None)
            known_users.pop(chat_id, None)
    last_activity[chat_id] = current_time

    if chat_id not in chat_history:
        chat_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        # --- ДОБАВЬ ЭТОТ БЛОК СЮДА ---
        current_phone = chat_id.split("@")[0]
        chat_history[chat_id].append({
            "role": "system", 
            "content": f"[ТЕЛЕФОН КЛИЕНТА]: {current_phone}. Если клиент пишет 'запиши на этот номер' или 'мой номер', бери этот."
        })
        # --- ПРОВЕРКА КЛИЕНТА + ДОСЬЕ ---
        if chat_id not in known_users:
            phone = chat_id.split("@")[0]
            found = await crm.find_user_smart(phone)
            
            if found:
                user_obj = found["user"]
                dossier = found["dossier"]
                known_users[chat_id] = user_obj
                
                # Определяем телефон менеджера филиала (если есть)
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
                logger.info(f"🤖 Загружено досье: {dossier['name']}")
                logger.info(f"📄 ПОЛНОЕ ДОСЬЕ (ОТПРАВЛЕНО В ИИ):\n{inject_msg}")

    chat_history[chat_id].append({"role": "user", "content": user_text})

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
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
                logger.info(f"⚙️ CRM: {args}")
                
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
                model="gpt-4o-mini", messages=chat_history[chat_id]
            )
            bot_answer = final.choices[0].message.content
        else:
            bot_answer = msg.content

        chat_history[chat_id].append({"role": "assistant", "content": bot_answer})
        await send_whatsapp(chat_id, bot_answer)

    except Exception as e:
        logger.error(f"🚨 Ошибка AI: {e}")

# --- 7. ВЕБХУК ---
@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    if data.get("typeWebhook") != "incomingMessageReceived": return "ok"
    
    sender = data.get("senderData", {}).get("chatId")
    msg_data = data.get("messageData", {})
    text = ""

    msg_type = msg_data.get("typeMessage")
    if msg_type == "textMessage":
        text = msg_data["textMessageData"]["textMessage"]
    elif msg_type == "extendedTextMessage":
        text = msg_data["extendedTextMessageData"]["text"]
    elif msg_type == "audioMessage":
        try:
            url = msg_data["fileMessageData"]["downloadUrl"]
            async with httpx.AsyncClient() as client:
                audio_response = await client.get(url)
                audio_bytes = audio_response.content

            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "voice.ogg" 
            transcription = await openai_client.audio.transcriptions.create(
                model="whisper-1", file=audio_file
            )
            text = f"[Голосовое]: {transcription.text}"
            logger.info(f"🎤 ГС распознано: {text}")
        except Exception as e:
            logger.error(f"Ошибка аудио: {e}")

    if not text or not sender: return "ok"

    print(f"📩 Входящее ({sender}): {text}")

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