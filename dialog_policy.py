"""Conservative dialog rules independent of model wording."""
import re

KAZAKH = re.compile(r'[әіңғүұқөһ]|\b(?:салем|рахмет|балам|казакша|казахша|казак|бола\s+ма|барамыз|неше|жастамын|жасым)\b', re.I)
RUSSIAN = re.compile(r'\b(?:здравствуйте|скажите|подскажите|реб[её]нок|реб[её]нка|сколько|хочу|можно|пожалуйста|интересует|напишите|цена|ценам|стоимость)\b', re.I)
DEFER = re.compile(r'посоветуюсь|подумаю|сам[аи]?\s+(?:напиш|свяж)|(?:потом|позже)\s+(?:напиш|отпиш)|отпишусь|кеңесіп|ақылдасып|ойланайын|өзім\s+хабарлас', re.I)
EMPLOYMENT = re.compile(r'трудоустрой|ваканси|нужны\s+(?:ли\s+)?тренер|работ\w*\s+тренер|тренер\w*\s+жұмыс|жаттықтырушы\w*\s+(?:керек|қажет)|жұмысқа\s+орналас', re.I)
BOT_CLOSED = re.compile(
    r'(?:минимальн\w*\s+возраст|(?:набор|запись|принимаем)\s+(?:возможн\w*\s+)?(?:только\s+)?с\s*5)|'
    r'когда\s+(?:реб[её]нку\s+)?исполнится\s*5|младше\s*5|меньше\s*5\s*лет|'
    r'5\s*жастан|5\s*жасқа\s+тол|Еркежан\s+Маратовна|'
    r'(?:бесплатн\w*\s+(?:вход|игр)|свободн\w*\s+(?:игр|посещени)).{0,55}(?:нет|не\s+предусмотр)|'
    r'не\s+(?:проводим|предоставляем|предусмотрены).{0,45}(?:свободн|бесплатн)|'
    r'(?:хорошего\s+(?:дня|вечера)|всего\s+доброго|сау\s+болыңыз|күніңіз\s+жақсы\s+өтсін)\s*[.!]?$', re.I)
EXTERNAL_REQUEST = re.compile(
    r'конкурент|друг(?:ой|ие|их|ом)\s+(?:шахматн\w*\s+)?(?:клуб|школ|академ)|'
    r'(?:где|куда).{0,100}(?:поиграть|поиграт|свободн\w*\s+игр)|'
    r'басқа\s+(?:шахмат\s+)?(?:клуб|мектеп)|қайда.{0,70}шахмат\s+ойна', re.I)
EXTERNAL_OUTPUT = re.compile(
    r'шахматн\w*\s+клуб\s*[«"“]?астана|центр\s+шахматного\s+образования|'
    r'(?:рекомендую|советую|попробуйте|обратитесь|можете\s+(?:посетить|сходить)).{0,75}(?:друг\w*\s+(?:клуб|школ)|клуб\s*[«"“])|'
    r'(?:поищите|найдите|посмотрите).{0,80}(?:клуб|школ).{0,60}(?:2гис|2gis)|'
    r'(?:басқа\s+клуб).{0,60}(?:ұсынам|барыңыз)|'
    r'популярные\s+места.{0,50}(?:шахмат|клуб)|несколько\s+шахматных\s+клубов', re.I)
PRICE_QUESTION = re.compile(r'цен[ауы]|ценам|стоимост|баға|қанша|сколько\s+стоят', re.I)
VISIT_NOW = re.compile(r'(?:қазір|бүгін).{0,45}(?:бар|кел)|(?:сейчас|сегодня).{0,45}(?:прийти|приехать|подойти)|(?:прийти|приехать|подойти).{0,45}(?:сейчас|сегодня)', re.I)

SCHOOL_ADMIN = re.compile(r'\bharmony\b|хармони|хормони|\bngs\b|\bнгс\b|энджиес|\bquantum\b|квантум', re.I)
SCHOOL_59 = re.compile(r'(?:школ\w*|мектеп\w*|school)\s*(?:№|номер|number|n)?\s*59\b|\b59\s*[-–]?\s*(?:школ\w*|мектеп\w*|school)', re.I)
SCHOOL_ADMIN_REPLY = 'Для записи на шахматный кружок при данной школе вам необходимо обратиться к администрации школы'
COURSES = ((r'шахмат|chess', 'шахматы'), (r'английск|ағылшын|english', 'английский'),
           (r'математ|math', 'математика'))


def mentioned_courses(text):
    return [label for pattern, label in COURSES if re.search(pattern, text or '', re.I)]


def school_admin_reply(language='ru'):
    return ('Осы мектептегі шахмат үйірмесіне жазылу үшін мектеп әкімшілігіне хабарласуыңыз қажет'
            if language == 'kk' else SCHOOL_ADMIN_REPLY)


def non_chess_course(facts):
    courses = facts.get('courses') or []
    return (bool(courses) and 'шахматы' not in courses) or (bool(facts.get('campaign_interest')) and not courses)


def reports_existing_student(text):
    """A client's claim selects support, but never verifies a CRM identity."""
    for clause in re.split(r'[.!?;\n]', text.lower().replace('ё', 'е')):
        if re.search(r'\bне\b|еще\s+не|раньше|перестал|больше\s+не|оқымай|бармай', clause):
            continue
        studying = r'\b(?:учимся|учится|учусь|обучаемся|обучается|обучаюсь|занимаемся|занимается|занимаюсь|ходим|ходит|хожу)\b'
        if re.search(r'(?:у\s+вас|к\s+вам).{0,35}' + studying + '|' +
                     studying + r'.{0,35}(?:у\s+вас|к\s+вам)|'
                     r'мы\s+(?:уже\s+)?ваши\s+(?:ученики|клиенты)|'
                     r'(?:біз|балам|баламыз).{0,40}(?:сіздерде|сізде).{0,30}(?:оқиды|оқимыз|оқимын|оқып\s+жүр|барады|барамыз|қатысады|қатысамыз)', clause):
            return True
    return False


TRAINER_CONTACT = re.compile(
    r'(?=.*(?:тренер|преподавател|учител|жаттықтырушы|мұғалім))'
    r'(?=.*(?:номер|телефон|контакт|имя|зовут|кто|нөмір|телефон|аты|байланыс)).+', re.I | re.S)
SUPPORT_SALES_REPLY = re.compile(
    r'для\s+кого\s+(?:рассматриваете|занятия)|для\s+реб[её]нка\s+или\s+для\s+себя|'
    r'сколько\s+лет|возраст\s+(?:вашего\s+)?(?:реб[её]нка|ученика)|'
    r'(?:какой|какие)\s+филиал\w*\s+вам\s+удоб|начинаете\s+с\s+нуля|'
    r'(?:(?:есть|предлагаем|доступны)\s+групповые\s+занятия)|'
    r'для\s+\d+[- ]летнего|неше\s+жаста|балаңыз\s+үшін\s+бе|қай\s+филиал\s+ыңғайлы', re.I)


def detect_language(text, previous='ru'):
    if re.search(r'(?:на\s+русском|орысша)', text, re.I):
        return 'ru'
    if re.search(r'қазақша|казахша|казакша|қазақ\s+тіл', text, re.I):
        return 'kk'
    if KAZAKH.search(text):
        return 'kk'
    if RUSSIAN.search(text):
        return 'ru'
    # Names, numbers, branches and «иа/сред/ок» retain the conversation language.
    return previous


def chess_rank(text):
    rank = re.search(r'\b([1-5]|iv|iii|ii|i)\s*(?:[-–]?(?:й|ый|ий|тий|го))?\s*разряд\w*\b', text, re.I)
    if not rank:
        return None
    value = rank.group(1).lower()
    return int(value) if value.isdigit() else {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4}[value]


def student_age(facts):
    try:
        age = float(str(facts.get('age', '')).replace(',', '.'))
        return age if 0 <= age < 100 else None
    except ValueError:
        return None


def qualification_directive(facts):
    if non_chess_course(facts):
        return ('Клиента интересует: ' + ', '.join(facts.get('courses') or ['диагностика GM Intellect']) + '. '
                'Английский и математика доступны в Аркаде, Камале и школе №59. '
                'Уточняй возраст, выбранный курс и филиал; не спрашивай шахматный разряд, '
                'не применяй шахматные ограничения по возрасту и взрослым. '
                'Цена — по новому прайсу выбранной площадки. Дни, длительность, возрастные группы уточняет администратор.')
    age = student_age(facts)
    if age is None:
        return ('Возраст ученика неизвестен. «Для себя» НЕ означает взрослый. '
                'Перед выбором детского/взрослого сценария уточни возраст ученика; '
                'не утверждай, что доступны только индивидуальные занятия.')
    if age >= 18:
        return 'Категория ученика: взрослый (возраст подтверждён). Только индивидуальные занятия от 7 000 тг/час.'
    return ('Категория ученика: ребёнок, даже если пишет сам и сказал «для себя». '
            'Возраст клиента важнее прежних предположений и ответов бота о взрослых. '
            'Предлагай детские группы по разряду и индивидуальные как вариант. '
            'Для 3 разряда: GMCA Аркада/Камал, группа 35 000 тг/месяц, 3 раза в неделю по 1,5 часа. '
            'Не ограничивай ребёнка индивидуальными без его явного выбора; '
            'если пишет сам, обращайся к нему, не спрашивай о «вашем ребёнке». '
            'Минимальный возраст для занятий — 5 лет.')


def child_lesson_options(facts):
    """Grounded fallback when a minor was offered only the adult format."""
    kk = facts.get('language') == 'kk'
    exp = facts.get('experience', '')
    rank = chess_rank(exp)
    master = bool(re.search(r'мастер\s+спорта', exp, re.I))
    kms = bool(re.search(r'кмс', exp, re.I))
    branch = 'GMCA Аркада' if kms or master else 'GMCA Аркада / Камал'
    if facts.get('preference') == 'онлайн':
        branch = 'онлайн'
    price, hours = ('35 000', '1,5') if rank == 3 else ('40 000', '2') if rank in (1, 2) or kms else ('30 000', '1')
    known_level = rank is not None or kms or bool(re.search(r'начинающ|нович|бастаушы', exp, re.I))
    if kk:
        group = (f'{branch}: топтық сабақ — айына {price} теңге, аптасына 3 рет, әр сабақ {hours} сағат.'
                 if known_level and not master else f'{branch}: деңгейіңізге сәйкес топтық сабақтар бар.')
        individual = ' Жеке сабақ та бар — бір сағат үшін 7 000 теңгеден басталады.'
        question = (' Шахматтағы деңгейіңіз немесе разрядыңыз қандай?' if not exp else
                    ' Сізге қай филиал ыңғайлы?' if not facts.get('preference') else '')
    else:
        group = (f'В {branch} доступны групповые занятия — {price} тг в месяц, 3 раза в неделю по {hours} ч.'
                 if known_level and not master else f'В {branch} доступны групповые занятия по уровню ученика.')
        individual = ' Также можно заниматься индивидуально — от 7 000 тг за час.'
        question = (' Какой у ученика опыт или разряд?' if not exp else
                    ' Какой филиал вам удобнее?' if not facts.get('preference') else '')
    return group + individual + question


def guard_student_options(answer, facts, user_text):
    """Do not let an earlier adult assumption hide a child's group option."""
    if non_chess_course(facts):
        return answer
    age = student_age(facts)
    adult = bool(re.search(r'для\s+взросл|вы\s+взросл|ересектер', answer, re.I))
    individual = bool(re.search(r'индивидуал|жеке\s+сабақ', answer, re.I))
    group = bool(re.search(r'групп|топтық|топта', answer, re.I))
    exclusive = bool(re.search(r'(?:только|лишь)\s+индивидуал|тек\s+жеке|'
                              r'групп\w*\s+(?:занят\w*\s+)?(?:нет|не\s+(?:подход|доступ|предусмотр))|'
                              r'топтық\s+сабақ\w*\s+жоқ', answer, re.I))
    self_intro = bool(re.fullmatch(r'\s*(?:для\s+(?:себя|меня)|өзім(?:е|\s+үшін)?)[.!]?\s*', user_text, re.I))
    if age is None and (self_intro or adult or exclusive or (facts.get('audience') == 'для себя' and individual and not group)):
        question = ('Оқушы неше жаста? Жасына қарай сабақ түрлерін ұсынамын.' if facts.get('language') == 'kk'
                    else 'Сколько лет ученику? Подскажу подходящие варианты занятий по возрасту.')
        if individual and PRICE_QUESTION.search(user_text):
            price = ('Жеке сабақ — бір сағат үшін 7 000 теңгеден басталады. ' if facts.get('language') == 'kk'
                     else 'Индивидуальные занятия — от 7 000 тг за час. ')
            return price + question
        return question
    if age is not None and 5 <= age < 18:
        requested_individual = facts.get('lesson_format') == 'индивидуально' or bool(
            re.search(r'индивидуал|жеке\s+сабақ', user_text, re.I)
            and not re.search(r'групп|топтық|\bне\b', user_text, re.I))
        offer = bool(re.search(r'доступ|можно|предлаг|подойд|подход|стоят|стоим|тг|теңге|\bбар\b', answer, re.I))
        if adult and requested_individual:
            return ('Жеке сабақ — бір сағат үшін 7 000 теңгеден басталады.' if facts.get('language') == 'kk'
                    else 'Индивидуальные занятия — от 7 000 тг за час.')
        if adult or (not requested_individual and (exclusive or (individual and not group and offer))):
            return child_lesson_options(facts)
    return answer


def closure_reason(user_text='', answer='', facts=None):
    if EMPLOYMENT.search(user_text):
        return 'employment'
    if DEFER.search(user_text):
        return 'client_deferred'
    try:
        if not non_chess_course(facts or {}) and float((facts or {}).get('age', '100').replace(',', '.')) < 5:
            return 'under_age'
    except (ValueError, AttributeError):
        pass
    if BOT_CLOSED.search(answer):
        return 'bot_closed'
    return ''


def external_reply(language):
    if language == 'kk':
        return 'Мен GMCA және GM Legends сабақтары туралы ақпарат бере аламын. Бізде еркін ойынға арналған бөлек алаң қарастырылмаған.'
    return 'Я могу подсказать по занятиям GMCA и GM Legends. Свободное посещение для игры у нас не предусмотрено.'


def employment_reply(language):
    if language == 'kk':
        return 'Жұмысқа орналасу мәселесі бойынша аға жаттықтырушыға хабарласа аласыз. Еркежан Маратовна: +7 702 561 3672.'
    return 'По вопросам работы тренером можно обратиться к нашему старшему тренеру. Еркежан Маратовна: +7 702 561 3672.'


def deferred_price_reply(facts, text):
    """Known GMCA tariff, only when explicitly asked while postponing a decision."""
    if not DEFER.search(text) or not PRICE_QUESTION.search(text) or facts.get('preference') not in ('Камал', 'Аркада', 'онлайн'):
        return None
    kk = facts.get('language') == 'kk'
    if non_chess_course(facts):
        if facts.get('preference') == 'онлайн':
            return None
        return ('Бір курс: 1 ай — 30 000, 3 ай — 80 000, 6 ай — 145 000 теңге. '
                'Екі курс: 55 000 / 155 000 / 280 000 теңге. '
                'Үш курс: 80 000 / 220 000 / 405 000 теңге (1 / 3 / 6 ай).'
                if kk else 'Один курс: 1 месяц — 30 000, 3 месяца — 80 000, 6 месяцев — 145 000 тг. '
                'Два курса: 55 000 / 155 000 / 280 000 тг. '
                'Три курса: 80 000 / 220 000 / 405 000 тг (за 1 / 3 / 6 месяцев).')
    age = student_age(facts)
    if age is None and facts.get('audience') == 'для себя':
        return None
    if (age is not None and age >= 18) or facts.get('lesson_format') == 'индивидуально' or re.search(r'индивидуал|жеке', text, re.I):
        return ('Жеке сабақтың бағасы — бір сағат үшін 7 000 теңгеден басталады. Ақылдасып, шешім қабылдаған соң хабарласа аласыз.'
                if kk else 'Индивидуальные занятия — от 7 000 тг за урок длительностью один час. Напишите, когда примете решение.')
    # Do not infer a rank from an unqualified «средний».
    exp = facts.get('experience', '')
    rank = chess_rank(exp)
    price, hours = ('35 000', '1,5') if rank == 3 else ('40 000', '2') if rank in (1, 2) or re.search(r'кмс', exp, re.I) else ('30 000', '1')
    qualifier_kk = '' if rank or re.search(r'кмс', exp, re.I) else 'бастаушыларға арналған '
    qualifier_ru = '' if rank or re.search(r'кмс', exp, re.I) else 'для начинающих '
    return (f'GMCA-да {qualifier_kk}топтық сабақтың бағасы — айына {price} теңге: аптасына 3 рет, әр сабақ {hours} сағат. Ақылдасып, шешім қабылдаған соң хабарласа аласыз.'
            if kk else f'В GMCA групповые занятия {qualifier_ru}стоят {price} тг в месяц: 3 раза в неделю по {hours} ч. Напишите, когда примете решение.')


def polish_kazakh(text):
    # Only well-defined erroneous phrases; never rewrite client/manager names.
    for pattern, replacement in (
        (r'заявканы\s+оформдадым', 'өтінішіңізді рәсімдедім'),
        (r'заявканы\s+рәсімдедім', 'өтінішіңізді рәсімдедім'),
        (r'Балаңыздың\s+аты\s+қандай', 'Балаңыздың аты кім'),
        (r'Жақсы\s+күн\s+тілеймін', 'Күніңіз жақсы өтсін'),
    ):
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def confirmed_handoff(result, language, visit_now=False):
    """Render verified contact literally; only successful registration may use this."""
    if 'ЗАЯВКА ОФОРМЛЕНА В CRM' not in result:
        return None
    name = re.search(r'^MGR_NAME=(.+)$', result, re.M)
    phone = re.search(r'^MGR_PHONE=(.+)$', result, re.M)
    if not name or not phone:
        return None
    contact = name.group(1).strip() + ', ' + phone.group(1).strip()
    if language == 'kk':
        text = 'Өтінішіңізді рәсімдедім. Филиал басшысы жақын арада сізбен хабарласады.'
        if visit_now:
            text += ' Бүгін келуге болатын уақытты филиал басшысы нақтылайды.'
        return text + ' Байланыс: ' + contact + '.'
    text = 'Заявку оформила. Управляющий свяжется с вами в ближайшее время.'
    if visit_now:
        text += ' Время, когда можно подойти сегодня, уточнит управляющий.'
    return text + ' Контакт: ' + contact + '.'


LANGUAGE_POLICY = """
ҚАЗАҚША САПАСЫ / КАЧЕСТВО КАЗАХСКОГО:
Пиши естественно и грамотно, уважительно на «сіз». Не копируй опечатки клиента.
«заявка» → «өтініш», «оформить» → «рәсімдеу», «пробный урок» → «сынақ сабағы».
Не употребляй «заявканы оформдадым», «заявка на оформ» и смесь русских/казахских окончаний.
Не меняй имена: Айтөре, Мақсат, Адай и любые другие сохраняй ровно как написал клиент.
Одно короткое слово, имя, цифра, «иа», «сред», «Камал» не меняют язык диалога.
Правильные формулировки (используй только подходящую по смыслу):
«Балаңыз неше жаста?»; «Балаңыз бұрын шахматпен айналысқан ба?»;
«Балаңыздың аты кім?»; «Сізге қай филиал ыңғайлы: GMCA Аркада, GMCA Камал әлде онлайн сабақтар ма?».
Только после подтверждённой регистрации: «Өтінішіңізді рәсімдедім. Филиал басшысы жақын арада сізбен хабарласады.»
Никогда не склоняй незнакомые имена менеджеров автоматически: «Байланыс нөмірі: ...».
Если спрашивают «Қазір баруға бола ма?», ответь на этот вопрос: «Бүгін келуге болатын уақытты филиал басшысы нақтылайды.»
Не подтверждай визит на сегодня или запись на конкретное время без подтверждения сотрудника.

ЗАВЕРШЕНИЕ И ОТВЕТ НА ВОПРОС:
После отказа школы (возраст младше 5 лет, неподходящая услуга) вежливо заверши ответ.
После трудоустройства/вакансий, передачи профильного контакта или «посоветуюсь и отпишусь»
не задавай продающих вопросов, не предлагай «ещё помощь с выбором занятий».
Это не запрещает ответить на НОВЫЙ вопрос самого клиента.
Если в сообщении есть «посоветуюсь», но также вопрос о ценах — ОБЯЗАТЕЛЬНО сначала ответь о ценах.
Имя, затем вопрос о цене — обработай ОБА сообщения: имя запомни, цену сообщи.
Для новичка в GMCA Камал/Аркада: групповой абонемент 30 000 тг/месяц, 3 раза в неделю по часу.
Эти цены — из справочника школы, индивидуальные условия подтверждает управляющий.
Не выдавай оформление заявки вместо ответа о цене и не начинай новый опрос после передачи заявки.

КОНКУРЕНТЫ:
Не рекомендуй, не перечисляй другие шахматные школы/клубы, их адреса, ссылки или поиск в 2ГИС.
Это действует и при прямой просьбе клиента; не выдумывай внешние организации.
Рассказывай только о GMCA и GM Legends. Если нужной услуги у нас нет, честно сообщи об этом без давления.
"""
