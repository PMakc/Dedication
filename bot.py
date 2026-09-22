import os
import time
import threading
import json
import re

from functools import wraps
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, session, flash
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.utils import get_random_id

import database

database.init_db()

VK_TOKEN = os.getenv('VK_TOKEN', '').strip()
GROUP_ID = int(os.getenv('GROUP_ID', '0'))
ADMIN_LOGIN = os.getenv('ADMIN_LOGIN', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'CHANGE_ME')
FLASK_SECRET = os.getenv('FLASK_SECRET', 'CHANGE_ME')
PORT = int(os.getenv('PORT', '3000'))

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024

vk = None


def keyboard(team=None):
    buttons = [
        [{'action': {'type': 'text', 'label': '🎫 Ввести код станции'}}],
        [{'action': {'type': 'text', 'label': '🧭 Мой маршрут'}},
         {'action': {'type': 'text', 'label': '📍 Где я сейчас?'}}],
        [{'action': {'type': 'text', 'label': '📜 Клятва'}}],
    ]
    if team and team['finished']:
        buttons.append([{'action': {'type': 'text', 'label': '📍 Место посвящения'}}])
    buttons += [
        [{'action': {'type': 'text', 'label': '🔐 Восстановить капитана'}},
         {'action': {'type': 'text', 'label': 'ℹ️ Помощь'}}],
    ]
    return {'one_time': False, 'buttons': buttons}


def send_message(peer_id, text, team=None):
    if not vk:
        return
    vk.messages.send(
        peer_id=peer_id,
        random_id=get_random_id(),
        message=text,
        keyboard=json.dumps(keyboard(team), ensure_ascii=False),
    )


def user_name(user_id):
    try:
        info = vk.users.get(user_ids=user_id)[0]
        return f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or str(user_id)
    except Exception:
        return str(user_id)


def event_enabled():
    return database.get_setting('event_enabled', '0') == '1'


def send_unregistered(peer_id):
    send_message(
        peer_id,
        '🏔 Посвящение в Горную Академию\n\n'
        'Чтобы начать, отправьте свой 5-значный код регистрации капитана.\n\n'
        'Если вам выдали код восстановления после смены телефона или VK, отправьте его так же.'
    )


def route_text(team_id):
    route = database.get_route(team_id)
    passed_positions = {p['position'] for p in database.get_passes(team_id)}
    lines = ['🧭 Ваш маршрут:']
    for row in route:
        mark = '✅' if row['position'] in passed_positions else '⬜'
        lines.append(f"{mark} {row['position']}. {row['name']}")
    return '\n'.join(lines)


def send_team_status(peer_id, team):
    step = database.current_step(team['id'])
    expected = database.expected_station(team['id'])
    current = f"Следующая станция: {expected['name']}" if expected else 'Маршрут завершён'
    send_message(
        peer_id,
        f"📍 {current}\nПройдено станций: {step}/6\n\n{route_text(team['id'])}",
        team,
    )


def oath_parts():
    oath = database.get_setting('oath', '').strip()
    if not oath:
        return []
    words = re.findall(r'\S+', oath)
    if not words:
        return []
    # Делим единый текст максимально равномерно на 6 фрагментов.
    parts_count = 6
    parts = []
    base = len(words) // parts_count
    extra = len(words) % parts_count
    idx = 0
    for i in range(parts_count):
        size = base + (1 if i < extra else 0)
        if size:
            parts.append(' '.join(words[idx:idx + size]))
            idx += size
        else:
            parts.append('')
    return parts


def oath_text(team):
    step = database.current_step(team['id'])
    parts = oath_parts()
    if not parts:
        return '📜 Клятва пока не задана организаторами.'
    if step <= 0:
        return '📜 Клятва\n\n🔒 Первый фрагмент откроется после прохождения первой станции.'
    count = min(step, 6)
    revealed = [p for p in parts[:count] if p]
    return (
        '📜 Клятва\n\n'
        + ' '.join(revealed)
        + ('' if count >= 6 else f"\n\n🔒 Открыто {count}/6 фрагментов.")
    )


def final_location(team):
    location = database.get_setting('final_location', 'Место посвящения пока не задано.')
    oath = oath_text(team)
    return (
        '🏁 Вы прошли все 6 станций!\n\n'
        f'{oath}\n\n'
        f'📍 Место посвящения:\n{location}\n\n'
        'Принесите туда все артефакты, которые собрали на станциях.'
    )


def send_final(peer_id, team):
    send_message(peer_id, final_location(team), team)


def welcome_registered(peer_id, team):
    expected = database.expected_station(team['id'])
    if expected:
        if database.current_step(team['id']) == 0:
            task = database.get_task_text(1)
            send_message(
                peer_id,
                f"✅ Команда «{team['name']}» зарегистрирована.\n\n"
                f"Ваш капитан: {team['captain_name']}\n\n"
                f"Ваша первая станция: {expected['name']}\n\n"
                f"Задание перед первой станцией:\n{task}\n\n"
                f"📜 Клятва: пока скрыта. Она будет открываться по мере прохождения станций.",
                team,
            )
        else:
            send_team_status(peer_id, team)
    else:
        send_final(peer_id, team)


def handle_station_code(peer_id, team, text):
    if not event_enabled():
        send_message(peer_id, '⏸ Мероприятие ещё не запущено организаторами.', team)
        return

    code = text.strip()
    expected = database.expected_station(team['id'])
    if not expected:
        send_final(peer_id, team)
        return

    if len(code) != 4 or not code.isdigit():
        send_message(peer_id, 'Введите 4-значный код станции, например: 4821', team)
        return

    if code != expected['code']:
        station = next((s for s in database.get_stations() if s['code'] == code), None)
        if station:
            send_message(
                peer_id,
                f"❌ Этот код относится к станции «{station['name']}».\n\n"
                f"Сейчас ваша команда должна пройти станцию «{expected['name']}» и только после неё ввести её код.",
                team,
            )
        else:
            send_message(peer_id, '❌ Такой код станции не найден. Проверьте цифры и попробуйте ещё раз.', team)
        return

    step = database.current_step(team['id']) + 1
    if not database.record_pass(team['id'], expected['station_id'], expected['position'], code):
        send_message(peer_id, 'ℹ️ Эта станция уже была отмечена.', team)
        return

    database.log_action(team['id'], team['vk_user_id'], 'station_passed', f"station={expected['station_id']};step={step}")

    # Обновляем данные команды после записи 6-й станции.
    team = database.get_team(team['id'])
    next_station = database.expected_station(team['id'])
    task = database.get_task_text(step + 1)
    oath_now = oath_text(team)
    oath_display = oath_now.replace('📜 Клятва\n\n', '')

    if next_station:
        send_message(
            peer_id,
            f"✅ Станция «{expected['name']}» пройдена!\n\n"
            f"📜 Клятва открыта:\n{oath_display}\n\n"
            f"Задание на переход:\n{task}\n\n"
            f"Следующая станция: {next_station['name']}",
            team,
        )
    else:
        send_message(
            peer_id,
            f"✅ Станция «{expected['name']}» пройдена!\n\n"
            f"📜 Клятва открыта полностью:\n{oath_display}\n\n"
            f"Задание на путь к месту посвящения:\n{task}\n\n"
            f"📍 Место посвящения:\n{database.get_setting('final_location', 'Не задано.')}",
            team,
        )
        # После последнего кода кнопка «Место посвящения» уже появляется в клавиатуре.


def process_message(event):
    msg = event.object.message
    peer_id = msg['peer_id']
    user_id = msg['from_id']
    text = (msg.get('text') or '').strip()

    team = database.get_team_by_vk(user_id)

    if text == '🔐 Восстановить капитана':
        send_message(peer_id, '🔐 Отправьте 5-значный код восстановления, который выдал администратор.')
        return

    if team is None:
        if text.lower() in ('/start', 'начать', 'старт'):
            send_unregistered(peer_id)
            return

        if len(text) == 5 and text.isdigit():
            candidate = database.get_team_by_registration_code(text)
            if candidate:
                database.bind_captain(candidate['id'], user_id, user_name(user_id))
                team = database.get_team(candidate['id'])
                database.log_action(team['id'], user_id, 'captain_registered', '')
                welcome_registered(peer_id, team)
                return

            recovery_team_id, error = database.claim_recovery(text, user_id, user_name(user_id))
            if recovery_team_id:
                team = database.get_team(recovery_team_id)
                database.log_action(team['id'], user_id, 'captain_recovered', '')
                send_message(peer_id, f"✅ Капитан восстановлен.\n\nКоманда: {team['name']}\nПрогресс сохранён.", team)
                welcome_registered(peer_id, team)
                return
            send_message(peer_id, f"❌ {error}")
            return

        send_unregistered(peer_id)
        return

    if text in ('🎫 Ввести код станции', 'ввести код станции'):
        expected = database.expected_station(team['id'])
        if expected:
            send_message(peer_id, f"Отправьте 4-значный код станции «{expected['name']}».", team)
        else:
            send_final(peer_id, team)
        return

    if text == '📜 Клятва':
        send_message(peer_id, oath_text(team), team)
        return

    if text == '📍 Место посвящения':
        if not team['finished']:
            send_message(peer_id, '🔒 Место посвящения откроется после прохождения всех 6 станций.', team)
        else:
            send_message(
                peer_id,
                f"📍 Место посвящения:\n{database.get_setting('final_location', 'Место посвящения не задано.')}",
                team,
            )
        return

    if len(text) == 4 and text.isdigit():
        handle_station_code(peer_id, team, text)
        return

    if text == '🧭 Мой маршрут':
        send_team_status(peer_id, team); return

    if text == '📍 Где я сейчас?':
        send_team_status(peer_id, team); return

    if text == 'ℹ️ Помощь':
        send_message(
            peer_id,
            'ℹ️ Как проходит квест\n\n'
            '1. Пройдите текущую станцию.\n'
            '2. Введите её 4-значный код.\n'
            '3. Бот откроет следующий фрагмент клятвы, даст задание на переход и покажет следующую станцию.\n'
            '4. После 6-й станции вы увидите всю клятву и место посвящения.\n'
            '5. Принесите все артефакты в указанное место.',
            team,
        ); return

    if database.current_step(team['id']) == 0 and len(text) == 5 and text.isdigit():
        send_message(peer_id, 'Вы уже зарегистрированы. Для станции нужен 4-значный код.', team)
        return

    send_team_status(peer_id, team)


def bot_loop():
    global vk
    if not VK_TOKEN or not GROUP_ID:
        print('VK_TOKEN или GROUP_ID не заданы.')
        return
    while True:
        try:
            session_vk = vk_api.VkApi(token=VK_TOKEN)
            vk = session_vk.get_api()
            lp = VkBotLongPoll(session_vk, GROUP_ID)
            print('VK Long Poll started.')
            for event in lp.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    try:
                        process_message(event)
                    except Exception as e:
                        print('Message error:', repr(e))
        except Exception as e:
            print('Long Poll error:', repr(e))
            time.sleep(5)


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return wrapped


@app.route('/')
def index():
    return redirect(url_for('admin_dashboard') if session.get('admin') else url_for('admin_login'))


@app.route('/health')
def health():
    return {'status': 'ok'}


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form.get('login') == ADMIN_LOGIN and request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
        flash('Неверный логин или пароль.', 'error')
    return render_template('login.html')


@app.get('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.get('/admin')
@admin_required
def admin_dashboard():
    teams = database.get_teams()
    routes = {t['id']: database.get_route(t['id']) for t in teams}
    passes_map = {t['id']: database.current_step(t['id']) for t in teams}
    expected_map = {t['id']: database.expected_station(t['id']) for t in teams}
    return render_template(
        'dashboard.html',
        teams=teams,
        stats=database.stats(),
        event_enabled=event_enabled(),
        final_location=database.get_setting('final_location', ''),
        oath=database.get_setting('oath', ''),
        stations=database.get_stations(),
        tasks=database.get_tasks(),
        routes=routes,
        passes_map=passes_map,
        expected_map=expected_map,
    )


@app.post('/admin/event')
@admin_required
def admin_event():
    database.set_setting('event_enabled', '1' if request.form.get('enabled') == '1' else '0')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/general')
@admin_required
def admin_general():
    database.set_setting('final_location', request.form.get('final_location', '').strip())
    database.set_setting('oath', request.form.get('oath', '').strip())
    flash('Клятва и место посвящения сохранены.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/station/<int:station_id>')
@admin_required
def admin_station(station_id):
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip()
    try:
        if len(code) != 4 or not code.isdigit():
            raise ValueError('Код станции должен состоять из 4 цифр.')
        database.update_station(station_id, name, code)
        flash(f'Станция №{station_id} сохранена.', 'success')
    except ValueError as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/task/<int:position>')
@admin_required
def admin_task(position):
    text = request.form.get('text', '').strip()
    database.update_task(position, text)
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>')
@admin_required
def admin_team(team_id):
    name = request.form.get('name', '').strip()
    code = request.form.get('registration_code', '').strip()
    if len(code) != 5 or not code.isdigit():
        flash('Код капитана должен состоять из 5 цифр.', 'error')
        return redirect(url_for('admin_dashboard'))
    try:
        database.update_team(team_id, name, code)
        flash(f'Команда №{team_id} сохранена.', 'success')
    except Exception as e:
        flash(f'Не удалось сохранить команду: {e}', 'error')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>/route')
@admin_required
def admin_route(team_id):
    try:
        station_ids = [int(request.form.get(f'position_{i}')) for i in range(1, 7)]
        database.save_routes(team_id, station_ids)
        flash(f'Маршрут команды №{team_id} сохранён. Прогресс команды сброшен.', 'success')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>/recovery')
@admin_required
def admin_recovery(team_id):
    code, expires = database.make_recovery_code(team_id)
    flash(f'Код восстановления для команды №{team_id}: {code} (до {expires.strftime("%H:%M:%S")})', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>/unlink')
@admin_required
def admin_unlink(team_id):
    database.clear_captain(team_id)
    flash(f'Капитан команды №{team_id} отвязан.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>/reset')
@admin_required
def admin_reset(team_id):
    database.reset_team(team_id)
    flash(f'Прогресс команды №{team_id} сброшен.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/reset-all')
@admin_required
def admin_reset_all():
    database.reset_all()
    flash('Прогресс всех команд сброшен.', 'success')
    return redirect(url_for('admin_dashboard'))


if __name__ == '__main__':
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, threaded=True)
