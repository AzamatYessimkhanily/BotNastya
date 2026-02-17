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
SESSION_TIMEOUT = 5 * 60 * 60  # 5 часов
IDLE_TIMEOUT = 2 * 60 * 60     # 2 часа (время до напоминания)

# --- ТЕЛЕФОНЫ МЕНЕДЖЕРОВ ---
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

# КАРТА ФИЛИАЛОВ
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
SYSTEM_PROMPT = """
Вы — консультант шахматной академии GMCA и сети шахматных кружков при школах GM Legends в Астане.
Ваша задача — помочь клиенту выбрать формат обучения, собрать информацию и передать её менеджеру.

Шаг 1. Приветствие и возраст.
«Здравствуйте! 🌸 Меня зовут Алия, я консультант шахматной академии GMCA. Как я могу к вам обращаться? Вы ищете обучение для себя или для ребёнка?»
Если ребенок: Узнай возраст и опыт (разряд).

Шаг 2. Выбор филиала и ПРОВЕРКА ШКОЛЫ.
ВАЖНОЕ ИЗМЕНЕНИЕ: Не перечисляй адреса текстом.
Скажи клиенту:
«Чтобы выбрать удобный филиал, пожалуйста, посмотрите карту наших локаций по ссылке:
👉 https://gmchess.kz/obuchenievshkole/
Напишите мне, какой филиал или школа вам подходит.»

🔴 КРИТИЧЕСКИ ВАЖНАЯ ПРОВЕРКА (ПРОТОКОЛ БЕЗОПАСНОСТИ):
Как только клиент называет школу (филиал GM Legends), ты ОБЯЗАНА спросить:
«Подскажите, ваш ребенок является учеником этой школы?»

Сценарий А: Клиент говорит "НЕТ" (ребенок не учится в этой школе).
Ты отвечаешь:
«К сожалению, согласно строгому протоколу безопасности школы, мы не можем принимать на кружок детей, которые там не обучаются. 😔
Но вы можете записаться в наши профессиональные Академии GMCA (открытый доступ для всех):
1. Филиал "Аркада" (р-н Хан-Шатыр)
2. Филиал "Камал" (пр. Улы Дала)
Какой из них вам ближе?»
(НЕ записывай клиента в школу, если он там не учится).

Сценарий Б: Клиент говорит "ДА" (ребенок учится в этой школе).
Отвечаешь: «Отлично! Тогда мы можем продолжить оформление в эту группу.»

Сценарий В: Клиент выбирает GMCA (Аркада или Камал).
Сразу переходи к оформлению, вопрос про школу задавать не нужно.

ЛОГИКА ПО УРОВНЮ ИГРЫ:
- Если 2 разряд и выше: Только GMCA (Аркада/Камал). В школьные кружки (GM Legends) таких сильных детей не берем.
- Взрослые: Только индивидуально и только в GMCA.

Шаг 3. Пробное занятие.
Если клиент подходит (новичок или слабый разряд) — предложи бесплатный пробный урок.
Исключения (не предлагать пробный): 2 разряд и выше, взрослые.

Шаг 4. Сбор данных и Финал.
Собери: Имя, Телефон, Возраст, Опыт, Филиал.
Вызови функцию `register_client_request`.
После вызова функции попрощайся и скажи, что заявка передана.
ОБЯЗАТЕЛЬНО дай номер управляющего выбранного филиала (если филиал определен).

--- СПРАВОЧНИК УПРАВЛЯЮЩИХ (ДЛЯ ТВОЕГО ОТВЕТА) ---
Аркада: Аяулым Жумажановна +7 778 104 8197
Камал: Шолпан Жолдыбаевна +7 778 104 8127
Binom Алтынсарина / Riviera / РФМШ: Томирис Ержанқызы +7 771 231 4549
Binom Кекилбаева / Кадыр Мырза Али / Steppe: Айгерим Аманжолқызы +7 775 254 2671
Harmony: Адиль мырза +7 778 200 2088
NGS: Ясмин +7 771 857 5505
Quantum: Зарина +7 705 287 6382

--- ЦЕНЫ ---
GMCA (Академия): 30-40 тыс тг/мес.
GM Legends (Школы): от 30 тыс тг (Binom от 20 тыс тг).
Индивидуально: от 7000 тг/урок.
"""

app = FastAPI()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Хранилище данных
chat_history: Dict[str, List[Dict]] = {}
message_buffers: Dict[str, Dict] = {}
known_users: Dict[str, dict] = {}
last_activity: Dict[str, float] = {}
conversation_status: Dict[str, str] = {} # "active", "registered", "nudged"

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
                    else:
                        logger.error(f"❌ Auth Error: {resp.text}")
                        return None
                except Exception as e:
                    logger.error(f"❌ Connection Error: {e}")
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
                except: pass

                today = datetime.now().strftime("%Y-%m-%d")
                future = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
                params = {"userId": user_id, "date": [today, future], "includeLessons": "true", "limit": 3, "sort": "date"}
                
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
                                "id": user_id, "name": user['name'], "groups": groups_text,
                                "schedule": schedule_text, "balance": user.get('balans', 0),
                                "filial_id": active_filial_id
                            }
                            return {"user": user, "dossier": dossier}
                except: pass
        return None

    async def create_lead(self, name, phone, age, experience, preference):
        headers = await self._get_headers()
        clean_phone = re.sub(r"[^\d]", "", phone)
        wa_link = f"https://wa.me/{clean_phone}"
        
        filial_id = None
        mgr_phone_text = ""

        if preference:
            branch_lower = preference.lower()
            for key, f_id in FILIALS_MAP.items():
                if key in branch_lower:
                    filial_id = f_id
                    mgr_phone = BRANCH_PHONES.get(f_id, BRANCH_PHONES["default"])
                    mgr_phone_text = f"Номер управляющего филиалом: {mgr_phone}"
                    break
        
        # ДР
        birth_attr = []
        try:
            age_num = int(re.search(r'\d+', str(age)).group())
            year = datetime.now().year - age_num
            birth_attr = [{"attributeId": 1, "value": f"{year}-01-01"}]
        except: pass

        full_text = (
            f"🤖 БОТ ЗАЯВКА:\n👤 {name}\n📱 {phone}\n📍 Выбор: {preference}\n"
            f"👶 Возраст: {age}\n♟️ Опыт: {experience}\n🔗 WhatsApp: {wa_link}"
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
                    "name": name, "phone": clean_phone, 
                    "responsibles": [MANAGER_ID], "attributes": birth_attr
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

            join_payload = {
                "userId": user_id, "statusId": 1, "classId": LEAD_CLASS_ID,
                "comment": full_text, "managerId": MANAGER_ID
            }
            if filial_id: join_payload["filialId"] = filial_id
            await client.post(f"{MOYKLASS_BASE_URL}/joins", headers=headers, json=join_payload)

            try:
                now = datetime.now().strftime("%Y-%m-%d")
                await client.post(f"{MOYKLASS_BASE_URL}/tasks", headers=headers, json={
                    "userId": user_id, "body": full_text,
                    "beginDate": now, "endDate": now,
                    "typeId": 1, "managerId": MANAGER_ID
                })
            except: pass

            final_msg = f"СИСТЕМНОЕ СООБЩЕНИЕ: УСПЕХ. Заявка создана. "
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
            "description": "Записать заявку в CRM. Вызывать в конце диалога.",
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

# --- 5. WHATSAPP & BACKGROUND TASKS ---
async def send_whatsapp(chat_id, text):
    url = f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chatId": chat_id, "message": text})

async def check_idle_chats():
    """Фоновая задача: проверяет зависшие диалоги каждые 5 минут"""
    while True:
        try:
            current_time = time.time()
            # Создаем копию ключей, чтобы не было ошибки при изменении словаря
            for chat_id, last_time in list(last_activity.items()):
                status = conversation_status.get(chat_id, "active")
                
                # Если прошло больше 2 часов, статус активен (не зарегистрирован) и еще не напоминали
                if (current_time - last_time > IDLE_TIMEOUT) and status == "active":
                    
                    logger.info(f"⏰ Напоминание для {chat_id}")
                    nudge_text = "Здравствуйте! Мы с вами не закончили оформление заявки. Подскажите, у вас остались какие-то вопросы или сложности с выбором?"
                    
                    # Отправляем сообщение
                    await send_whatsapp(chat_id, nudge_text)
                    
                    # Добавляем это в историю, чтобы ИИ знал о напоминании
                    if chat_id in chat_history:
                        chat_history[chat_id].append({"role": "assistant", "content": nudge_text})
                    
                    # Меняем статус, чтобы не спамить
                    conversation_status[chat_id] = "nudged"
                    
        except Exception as e:
            logger.error(f"Error in watchdog: {e}")
        
        await asyncio.sleep(300) # Проверка каждые 5 минут

# --- 6. ЛОГИКА ---
async def process_dialog(chat_id):
    buffer = message_buffers.pop(chat_id, None)
    if not buffer: return
    user_text = " ".join(buffer["messages"])
    logger.info(f"📩 Обработка для {chat_id}: {user_text}")

    # Обновляем время активности и сбрасываем сессию если нужно
    current_time = time.time()
    if chat_id in last_activity:
        if current_time - last_activity[chat_id] > SESSION_TIMEOUT:
            logger.info(f"🧹 Сброс памяти для {chat_id}")
            chat_history.pop(chat_id, None)
            known_users.pop(chat_id, None)
            conversation_status[chat_id] = "active" # Сброс статуса
    
    last_activity[chat_id] = current_time
    
    # Если это новый диалог или после сброса
    if chat_id not in chat_history:
        chat_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        conversation_status[chat_id] = "active"
        
        current_phone = chat_id.split("@")[0]
        chat_history[chat_id].append({
            "role": "system", 
            "content": f"[ТЕЛЕФОН КЛИЕНТА]: {current_phone}. Если клиент пишет 'запиши на этот номер', используй этот."
        })
        
        # Проверка клиента
        if chat_id not in known_users:
            phone = chat_id.split("@")[0]
            found = await crm.find_user_smart(phone)
            if found:
                user_obj = found["user"]
                dossier = found["dossier"]
                known_users[chat_id] = user_obj
                conversation_status[chat_id] = "registered" # Существующий клиент считается "закрытым" в плане лида
                
                mgr_contact = ""
                if dossier["filial_id"] and dossier["filial_id"] in BRANCH_PHONES:
                     mgr_contact = f"Его менеджер: {BRANCH_PHONES[dossier['filial_id']]}."

                inject_msg = (
                    f"[СИСТЕМНОЕ ДОСЬЕ КЛИЕНТА]\nИмя: {dossier['name']}\nГруппы: {dossier['groups']}\n"
                    f"Ближайшие уроки: {dossier['schedule']}\nБаланс: {dossier['balance']}\n{mgr_contact}\n"
                    f"ИНСТРУКЦИЯ: Это действующий ученик! Сразу переходи к делу (расписание/проблема). Не регистрируй как нового."
                )
                chat_history[chat_id].append({"role": "system", "content": inject_msg})

    chat_history[chat_id].append({"role": "user", "content": user_text})

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=chat_history[chat_id],
            tools=tools,
            tool_choice="auto",
            temperature=0.3
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
                
                # Ставим статус "Зарегистрирован", чтобы watchdog не слал напоминания
                conversation_status[chat_id] = "registered"
                
                chat_history[chat_id].append({
                    "tool_call_id": tool.id,
                    "role": "tool",
                    "name": tool.function.name,
                    "content": result_text
                })

            final = await openai_client.chat.completions.create(
                model="gpt-4o", messages=chat_history[chat_id]
            )
            bot_answer = final.choices[0].message.content
        else:
            bot_answer = msg.content

        chat_history[chat_id].append({"role": "assistant", "content": bot_answer})
        await send_whatsapp(chat_id, bot_answer)

    except Exception as e:
        logger.error(f"🚨 Ошибка AI: {e}")

# --- 7. ЗАПУСК И ВЕБХУК ---
@app.on_event("startup")
async def startup_event():
    # Запускаем фоновую задачу при старте
    asyncio.create_task(check_idle_chats())

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

    # Если клиент ответил, меняем статус с "nudged" на "active"
    if conversation_status.get(sender) == "nudged":
        conversation_status[sender] = "active"

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