import os, threading, time, json, re
from dotenv import load_dotenv
load_dotenv()

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.utils import get_random_id
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory
from werkzeug.utils import secure_filename

import database
from game_config import TEAM_CONFIG, STATIONS

TOKEN = os.getenv('VK_TOKEN', '').strip()
GROUP_ID = int(os.getenv('GROUP_ID', '0'))
PORT = int(os.getenv('PORT', '3000'))
ADMIN_LOGIN = os.getenv('ADMIN_LOGIN', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'CHANGE_ME')
SECRET = os.getenv('FLASK_SECRET', 'CHANGE_ME')

app = Flask(__name__)
app.secret_key = SECRET
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024

database.init(TEAM_CONFIG)
vk_api_obj = None
upload = None

REG_TEXT = '🔑 Регистрация капитана'

# Russian syllabification heuristic. It is designed for the event's short words.
VOWELS = set('АЕЁИОУЫЭЮЯ')

def syllabify_word(word):
    word = re.sub(r'[^А-ЯЁ-]', '', word.upper())
    if not word:
        return []
    vowel_positions = [i for i, ch in enumerate(word) if ch in VOWELS]
    if len(vowel_positions) <= 1:
        return [word]

    # Boundaries are placed before the last consonant of the group before the next vowel.
    parts = []
    start = 0
    for idx in range(1, len(vowel_positions)):
        prev_v = vowel_positions[idx - 1]
        next_v = vowel_positions[idx]
        between = next_v - prev_v - 1
        if between <= 0:
            cut = next_v
        elif between == 1:
            cut = next_v
        else:
            cut = next_v - 1
        parts.append(word[start:cut])
        start = cut
    parts.append(word[start:])
    return [p for p in parts if p]


def parse_sentence(sentence):
    words = [re.sub(r'[^А-ЯЁ-]', '', x.upper()) for x in sentence.split()]
    words = [w for w in words if w]
    syllables = []
    words_with_syllables = []
    for word in words:
        s = syllabify_word(word)
        words_with_syllables.append(s)
        syllables.extend(s)
    return words, words_with_syllables, syllables


def kb(rows):
    return {
        'one_time': False,
        'buttons': [[{'action': {'type': 'text', 'label': x}} for x in row] for row in rows]
    }


def main_kb():
    return kb([
        ['🎫 Получить слог'],
        ['🧩 Мои слоги', '🔤 Собрать слово'],
        ['📝 Собрать предложение'],
        ['🏆 Мой клад'],
        [REG_TEXT],
        ['ℹ️ Помощь']
    ])


def send(peer, text, keyboard=None, attachment=None):
    if not vk_api_obj:
        return
    p = {'peer_id': peer, 'random_id': get_random_id(), 'message': text}
    if keyboard:
        p['keyboard'] = json.dumps(keyboard, ensure_ascii=False)
    if attachment:
        p['attachment'] = attachment
    vk_api_obj.messages.send(**p)


def uname(uid):
    try:
        u = vk_api_obj.users.get(user_ids=uid)[0]
        return f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or str(uid)
    except Exception:
        return str(uid)


def registration_prompt(peer):
    send(peer,
         '🔑 Регистрация капитана\n\n'
         'Введите 5-значный код вашей команды.\n\n'
         'Пример: 38142\n\n'
         'Этот код можно получить у организатора и при необходимости использовать повторно на другом телефоне или VK-аккаунте.',
         main_kb())


def team_data(t):
    return database.config(t)


def station_for_code(t, code):
    cfg = team_data(t)
    for station, station_code in cfg['station_codes'].items():
        if code == station_code:
            return station
    return None


def award_from_station_code(peer, t, text):
    station = station_for_code(t, text)
    if station is None:
        return False
    if database.awarded(t['id'], station):
        existing = [x['syllable'] for x in database.awards(t['id']) if x['station'] == station][0]
        send(peer, f'ℹ️ Станция «{STATIONS[station]}» уже пройдена.\nВаш слог: {existing}', main_kb())
        return True

    cfg = team_data(t)
    try:
        syll = cfg['syllables'][station - 1]
    except (IndexError, KeyError):
        send(peer, '⚠️ Для этой станции ещё не настроен слог. Сообщите организатору.', main_kb())
        return True

    database.add_award(t['id'], station, syll)
    database.log(t['id'], t['vk_id'], 'award', f'{station}:{text}:{syll}')
    send(peer,
         f'✅ Станция «{STATIONS[station]}» пройдена!\n\n'
         f'Ваш слог:\n{syll}\n\n'
         'Он сохранён. Переходите на следующую станцию.',
         main_kb())
    return True


def award_button(peer):
    send(peer,
         '🎫 Для получения слога отправьте цифровой код вашей станции.\n\n'
         'Код выдаёт организатор на станции. Для каждой команды код свой.',
         main_kb())


def words_menu(peer, t):
    cfg = team_data(t)
    a = [x['syllable'] for x in database.awards(t['id'])]
    if len(a) < len(cfg['syllables']):
        send(peer, f'🧩 Нужно получить все {len(cfg["syllables"])} слогов. Сейчас {len(a)}/{len(cfg["syllables"])}.', main_kb())
        return
    i = t['word_i']
    if i >= len(cfg['words']):
        send(peer, '✅ Все слова собраны. Переходите к сборке предложения.', main_kb())
        return
    d = database.draft(t)
    avail = [x for x in a if x not in d]
    rows = []
    for x in avail:
        if not rows or len(rows[-1]) == 2:
            rows.append([])
        rows[-1].append(x)
    rows.append(['✅ Проверить', '↩️ Очистить'])
    send(peer,
         f'🔤 Слово {i + 1}/{len(cfg["words"])}\n\n'
         f'Сейчас: {"".join(d) or "—"}\n\n'
         'Нажимайте слоги в правильном порядке.', kb(rows))


def process_word(peer, t, text):
    cfg = team_data(t)
    d = database.draft(t)
    i = t['word_i']
    if i >= len(cfg['words']):
        return False
    if text == '↩️ Очистить':
        database.set_draft(t['id'], [])
        words_menu(peer, database.team(t['id']))
        return True
    if text == '✅ Проверить':
        if d == cfg['words'][i]:
            w = ''.join(d)
            database.save_word(t['id'], i, w)
            send(peer, f'✅ Слово собрано: {w}')
            words_menu(peer, database.team(t['id']))
        else:
            send(peer, '❌ Неверный порядок. Попробуйте ещё раз.')
            words_menu(peer, database.team(t['id']))
        return True
    if text in [x['syllable'] for x in database.awards(t['id'])] and text not in d:
        d.append(text)
        database.set_draft(t['id'], d)
        words_menu(peer, database.team(t['id']))
        return True
    return False


def sentence_menu(peer, t):
    cfg = team_data(t)
    ws = [x['word'] for x in database.words(t['id'])]
    d = database.sentence_draft(t)
    if len(ws) < len(cfg['words']):
        send(peer, 'Сначала соберите все слова.', main_kb())
        return
    avail = [x for x in ws if x not in d]
    rows = []
    for x in avail:
        if not rows or len(rows[-1]) == 2:
            rows.append([])
        rows[-1].append(x)
    rows.append(['✅ Проверить', '↩️ Очистить'])
    send(peer, f'📝 Соберите предложение\n\nСейчас: {" ".join(d) or "—"}', kb(rows))


def process_sentence(peer, t, text):
    cfg = team_data(t)
    d = database.sentence_draft(t)
    if text == '↩️ Очистить':
        database.set_sentence_draft(t['id'], [])
        sentence_menu(peer, database.team(t['id']))
        return True
    if text == '✅ Проверить':
        if d == cfg['sentence']:
            database.finish(t['id'])
            send(peer, '🎉 Поздравляем!\n\nВы правильно собрали финальное предложение.\nВаш индивидуальный клад открыт!')
            treasure(peer, database.team(t['id']))
        else:
            send(peer, '❌ Неверный порядок слов.')
            sentence_menu(peer, database.team(t['id']))
        return True
    ws = [x['word'] for x in database.words(t['id'])]
    if text in ws and text not in d:
        d.append(text)
        database.set_sentence_draft(t['id'], d)
        sentence_menu(peer, database.team(t['id']))
        return True
    return False


def treasure(peer, t):
    if not t['finished']:
        send(peer, '🏆 Клад пока закрыт. Сначала соберите правильное предложение.', main_kb())
        return
    photo = database.get_(f'photo_{t["id"]}', '')
    enabled = database.get_(f'treasure_{t["id"]}', '0') == '1'
    if not enabled:
        send(peer, '🏆 Вы победили!\n\nОрганизатор ещё не открыл фотографию вашего клада.', main_kb())
        return
    path = os.path.join(database.DATA_DIR, 'photos', photo)
    if not photo or not os.path.exists(path):
        send(peer, '🏆 Вы победили, но фотография вашего клада ещё не загружена.', main_kb())
        return
    try:
        item = upload.photo_messages(path, peer_id=peer)[0]
        send(peer, '🏆 Место вашего клада найдено!', main_kb(), f"photo{item['owner_id']}_{item['id']}")
    except Exception as e:
        print('photo error', repr(e))
        send(peer, 'Не удалось отправить фото. Сообщите организатору.', main_kb())


def msg(event):
    m = event.object.message
    peer = m['peer_id']; uid = m['from_id']; text = (m.get('text') or '').strip()
    t = database.by_vk(uid)

    if text in ('/start', 'начать', 'старт') and not t:
        send(peer, '🏔 Посвящение в Горную Академию\n\nДля начала зарегистрируйтесь как капитан команды по 5-значному коду.', main_kb())
        return
    if text == REG_TEXT or text.lower() in ('регистрация', 'код команды', 'подключить команду'):
        registration_prompt(peer)
        return
    if not t and text.isdigit() and len(text) == 5:
        team = database.by_reg(text)
        if team:
            database.bind(team['id'], uid, uname(uid)); t = database.team(team['id'])
            send(peer, f'✅ Регистрация завершена!\n\nВы капитан команды: {t["name"]}\n\nТеперь на каждой станции отправляйте цифровой код, который вам выдаст организатор.', main_kb())
            return
        send(peer, '❌ Такой 5-значный код команды не найден. Проверьте цифры и попробуйте ещё раз.', main_kb())
        return
    if not t:
        send(peer, '👋 Вы ещё не зарегистрированы. Нажмите «🔑 Регистрация капитана» и введите 5-значный код.', main_kb())
        return

    # A 4-digit number is a station code. It is resolved only within this team.
    if text.isdigit() and len(text) == 4:
        if award_from_station_code(peer, t, text):
            return
        send(peer, '❌ Этот код станции не подходит вашей команде или уже недействителен.', main_kb())
        return

    if process_word(peer, t, text): return
    if process_sentence(peer, t, text): return
    if text == '🎫 Получить слог': award_button(peer)
    elif text == '🧩 Мои слоги':
        rows = database.awards(t['id'])
        send(peer, '🧩 Ваши слоги\n\n' + ('Нет полученных слогов.' if not rows else '\n'.join(f'{STATIONS[x["station"]]} → {x["syllable"]}' for x in rows)), main_kb())
    elif text == '🔤 Собрать слово': words_menu(peer, t)
    elif text == '📝 Собрать предложение': sentence_menu(peer, t)
    elif text == '🏆 Мой клад': treasure(peer, t)
    elif text == 'ℹ️ Помощь':
        send(peer,
             'ℹ️ Как проходит игра\n\n'
             '1. Зарегистрируйтесь по 5-значному коду команды.\n'
             '2. На каждой станции получите свой цифровой код.\n'
             '3. Отправьте код боту — бот выдаст персональный слог вашей команды.\n'
             '4. После 5 станций соберите слова.\n'
             '5. Затем соберите предложение.\n'
             '6. После правильного ответа откроется ваш индивидуальный клад.', main_kb())
    else:
        send(peer, 'Используйте кнопки меню. Если вы получили код станции, просто отправьте его сообщением.', main_kb())


def bot_loop():
    global vk_api_obj, upload
    if not TOKEN or not GROUP_ID:
        print('VK_TOKEN или GROUP_ID не заданы')
        return
    while True:
        try:
            s = vk_api.VkApi(token=TOKEN)
            vk_api_obj = s.get_api()
            upload = vk_api.VkUpload(s)
            lp = VkBotLongPoll(s, GROUP_ID)
            print('VK bot started')
            for e in lp.listen():
                if e.type == VkBotEventType.MESSAGE_NEW:
                    try: msg(e)
                    except Exception as ex: print('message error', repr(ex))
        except Exception as ex:
            print('longpoll error', repr(ex)); time.sleep(5)


def admin_required(f):
    from functools import wraps
    @wraps(f)
    def w(*a, **kw):
        return f(*a, **kw) if session.get('admin') else redirect(url_for('login'))
    return w


@app.get('/')
def root():
    return redirect('/admin')


@app.get('/health')
def health(): return {'status': 'ok'}


@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('login') == ADMIN_LOGIN and request.form.get('password') == ADMIN_PASSWORD:
        session['admin'] = 1
        return redirect('/admin')
    if request.method == 'POST': flash('Неверный логин или пароль.', 'error')
    return render_template('login.html')


@app.get('/admin/logout')
def logout():
    session.clear(); return redirect('/admin/login')


def station_matrix(teams):
    rows = []
    for station_id in range(1, 6):
        cells = []
        for t in teams:
            passed = database.awarded(t['id'], station_id)
            cells.append({'team': t, 'passed': passed})
        rows.append({'station': station_id, 'name': STATIONS[station_id], 'cells': cells})
    return rows


@app.get('/admin')
@admin_required
def dashboard():
    ts = database.teams()
    return render_template(
        'dashboard.html', teams=ts, stats=database.stats(), event=database.get_('event') == '1',
        treasures=[database.get_(f'treasure_{i}', '0') == '1' for i in range(1, 6)],
        photos=[database.get_(f'photo_{i}', '') for i in range(1, 6)],
        stations=STATIONS, matrix=station_matrix(ts)
    )


@app.post('/admin/event')
@admin_required
def setevent():
    database.set_('event', request.form.get('value', '0'))
    return redirect('/admin')


@app.post('/admin/team/<int:tid>/save')
@admin_required
def save_team_config(tid):
    t = database.team(tid)
    if not t:
        flash('Команда не найдена.', 'error'); return redirect('/admin')

    name = request.form.get('name', '').strip() or f'Команда {tid}'
    reg = request.form.get('registration_code', '').strip()
    sentence = request.form.get('sentence', '').strip()
    codes = {i: request.form.get(f'code_{i}', '').strip() for i in range(1, 6)}

    if not re.fullmatch(r'\d{5}', reg):
        flash(f'Команда {tid}: код регистрации должен состоять из 5 цифр.', 'error'); return redirect('/admin')
    if any(not re.fullmatch(r'\d{4}', c) for c in codes.values()):
        flash(f'Команда {tid}: все 5 кодов станций должны состоять из 4 цифр.', 'error'); return redirect('/admin')
    if len(set(codes.values())) != 5:
        flash(f'Команда {tid}: коды станций внутри команды должны различаться.', 'error'); return redirect('/admin')
    words, word_syllables, syllables = parse_sentence(sentence)
    if not words:
        flash(f'Команда {tid}: укажите предложение.', 'error'); return redirect('/admin')
    if len(syllables) != 5:
        flash(f'Команда {tid}: из предложения автоматически получилось {len(syllables)} слогов. Нужно ровно 5, по одному на каждую станцию.', 'error'); return redirect('/admin')
    if len(set(codes.values())) < 5:
        flash(f'Команда {tid}: коды станций должны быть уникальны.', 'error'); return redirect('/admin')

    database.update_config(tid, name, reg, codes, sentence, word_syllables, syllables)
    # Смена текста задания должна сбрасывать старые полученные слоги и сборку.
    database.reset(tid)
    flash(f'Команда {tid}: предложение, слова, слоги и коды сохранены. Прогресс сброшен.', 'success')
    return redirect('/admin')


@app.post('/admin/team/<int:tid>/photo')
@admin_required
def upload_treasure(tid):
    f = request.files.get('photo')
    if f and f.filename:
        ext = f.filename.rsplit('.', 1)[-1].lower()
        if ext not in {'jpg', 'jpeg', 'png', 'webp'}:
            flash('Разрешены JPG, JPEG, PNG и WEBP.', 'error'); return redirect('/admin')
        fn = f'team_{tid}_{int(time.time())}_{secure_filename(f.filename)}'
        os.makedirs(os.path.join(database.DATA_DIR, 'photos'), exist_ok=True)
        f.save(os.path.join(database.DATA_DIR, 'photos', fn))
        database.set_(f'photo_{tid}', fn)
        flash(f'Фото клада команды {tid} загружено.', 'success')
    database.set_(f'treasure_{tid}', '1' if request.form.get('enabled') == '1' else '0')
    return redirect('/admin')


@app.post('/admin/team/<int:tid>/new-code')
@admin_required
def new_code(tid):
    for _ in range(100):
        code = str(__import__('secrets').randbelow(90000) + 10000)
        if not database.by_reg(code): break
    c = database.conn(); c.execute('UPDATE teams SET registration_code=? WHERE id=?', (code, tid)); c.commit(); c.close()
    flash(f'Новый 5-значный код регистрации для команды {tid}: {code}', 'success')
    return redirect('/admin')


@app.post('/admin/team/<int:tid>/generate-station-codes')
@admin_required
def gen_station_codes(tid):
    used = set()
    for t in database.teams():
        cfg = database.config(t)
        used.update(cfg['station_codes'].values())
    codes = {}
    for station in range(1, 6):
        while True:
            code = f"{__import__('secrets').randbelow(10000):04d}"
            if code not in used and code not in codes.values(): break
        codes[station] = code
    t = database.team(tid); cfg = database.config(t)
    database.update_config(tid, t['name'], t['registration_code'], codes, cfg['sentence'], cfg['words'], cfg['syllables'])
    flash(f'Для команды {tid} сгенерированы новые 4-значные коды станций.', 'success')
    return redirect('/admin')


@app.post('/admin/team/<int:tid>/unlink')
@admin_required
def unlink(tid):
    database.unlink(tid); flash('Капитан отвязан.', 'success'); return redirect('/admin')


@app.post('/admin/team/<int:tid>/reset')
@admin_required
def reset(tid):
    database.reset(tid); flash('Прогресс команды сброшен.', 'success'); return redirect('/admin')


@app.get('/admin/photos/<path:name>')
@admin_required
def photo(name): return send_from_directory(os.path.join(database.DATA_DIR, 'photos'), name)


if __name__ == '__main__':
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run('0.0.0.0', PORT, threaded=True)
