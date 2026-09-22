import os
import time
import threading
import json
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


def keyboard():
    return {
        'one_time': False,
        'buttons': [
            [{'action': {'type': 'text', 'label': '🎫 Ввести код станции'}}],
            [{'action': {'type': 'text', 'label': '🧭 Мой маршрут'}},
             {'action': {'type': 'text', 'label': '📍 Где я сейчас?'}}],
            [{'action': {'type': 'text', 'label': '🔐 Восстановить капитана'}},
             {'action': {'type': 'text', 'label': 'ℹ️ Помощь'}}],
        ]
    }


def send_message(peer_id, text):
    if not vk:
        return
    vk.messages.send(
        peer_id=peer_id,
        random_id=get_random_id(),
        message=text,
        keyboard=json.dumps(keyboard(), ensure_ascii=False),
    )


def user_name(user_id):
    try:
        info = vk.users.get(user_ids=user_id)[0]
        return f"{info.get('first_name','')} {info.get('last_name','')}".strip() or str(user_id)
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
    lines = ['🧭 Ваш маршрут:']
    for row in route:
        passed = any(p['position'] == row['position'] for p in database.get_passes(team_id))
        mark = '✅' if passed else '⬜'
        lines.append(f"{mark} {row['position']}. {row['name']}")
    return '\n'.join(lines)


def send_team_status(peer_id, team):
    step = database.current_step(team['id'])
    expected = database.expected_station(team['id'])
    if expected:
        current = f"Следующая станция: {expected['name']}"
    else:
        current = 'Маршрут завершён'
    send_message(peer_id, f"📍 {current}\nПройдено станций: {step}/6\n\n{route_text(team['id'])}")


def welcome_registered(peer_id, team):
    expected = database.expected_station(team['id'])
    if expected:
        task = database.get_task_text(1)
        if database.current_step(team['id']) == 0:
            send_message(
                peer_id,
                f"✅ Команда «{team['name']}» зарегистрирована.\n\n"
                f"Ваш капитан: {team['captain_name']}\n\n"
                f"Ваша первая станция: {expected['name']}\n\n"
                f"Задание перед первой станцией:\n{task}"
            )
        else:
            send_team_status(peer_id, team)
    else:
        send_final(peer_id, team)


def send_final(peer_id, team):
    location = database.get_setting('final_location', 'Место финала не задано.')
    oath = database.get_setting('oath', 'Клятва не задана.')
    send_message(
        peer_id,
        '🏁 Вы прошли все 6 станций!\n\n'
        f'Направляйтесь в место финала:\n{location}\n\n'
        'Не забудьте принести все артефакты, которые получили на станциях.\n\n'
        f'Клятва:\n{oath}'
    )


def handle_station_code(peer_id, team, text):
    if not event_enabled():
        send_message(peer_id, '⏸ Мероприятие ещё не запущено организаторами.')
        return

    code = text.strip()
    expected = database.expected_station(team['id'])
    if not expected:
        send_final(peer_id, team)
        return

    if len(code) != 4 or not code.isdigit():
        send_message(peer_id, 'Введите 4-значный код станции, например: 4821')
        return

    if code != expected['code']:
        # If the code belongs to a known station, tell them the station number but not the code.
        station = next((s for s in database.get_stations() if s['code'] == code), None)
        if station:
            send_message(
                peer_id,
                f"❌ Этот код относится к станции «{station['name']}».\n\n"
                f"Сейчас ваша команда должна пройти станцию «{expected['name']}» и только после неё ввести её код."
            )
        else:
            send_message(peer_id, '❌ Такой код станции не найден. Проверьте цифры и попробуйте ещё раз.')
        return

    step = database.current_step(team['id']) + 1
    if not database.record_pass(team['id'], expected['station_id'], expected['position'], code):
        send_message(peer_id, 'ℹ️ Эта станция уже была отмечена.')
        return

    database.log_action(team['id'], team['vk_user_id'], 'station_passed', f"station={expected['station_id']};step={step}")

    next_station = database.expected_station(team['id'])
    task_position = step + 1
    task = database.get_task_text(task_position)

    if next_station:
        send_message(
            peer_id,
            f"✅ Станция «{expected['name']}» пройдена!\n\n"
            f"Задание на переход:\n{task}\n\n"
            f"Следующая станция: {next_station['name']}"
        )
    else:
        # Task 7 is the transition from station 6 to the final location.
        send_message(
            peer_id,
            f"✅ Станция «{expected['name']}» пройдена!\n\n"
            f"Задание на путь к финалу:\n{task}"
        )
        send_final(peer_id, team)


def process_message(event):
    msg = event.object.message
    peer_id = msg['peer_id']
    user_id = msg['from_id']
    text = (msg.get('text') or '').strip()

    team = database.get_team_by_vk(user_id)

    # Recovery is always available before the registration check.
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

        if len(text) == 5 and text.isdigit():
            recovery_team_id, error = database.claim_recovery(text, user_id, user_name(user_id))
            if recovery_team_id:
                team = database.get_team(recovery_team_id)
                database.log_action(team['id'], user_id, 'captain_recovered', '')
                send_message(peer_id, f"✅ Капитан восстановлен.\n\nКоманда: {team['name']}\nПрогресс сохранён.")
                welcome_registered(peer_id, team)
                return
            send_message(peer_id, f"❌ {error}")
            return

        send_unregistered(peer_id)
        return

    if text in ('🎫 Ввести код станции', 'ввести код станции'):
        expected = database.expected_station(team['id'])
        if expected:
            send_message(peer_id, f"Отправьте 4-значный код станции «{expected['name']}».")
        else:
            send_final(peer_id, team)
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
            '3. Бот выдаст задание на переход и покажет следующую станцию.\n'
            '4. После 6 станций бот направит вас в финальную точку.\n'
            '5. Принесите все артефакты и произнесите клятву.'
        ); return

    if database.current_step(team['id']) == 0 and len(text) == 5 and text.isdigit():
        send_message(peer_id, 'Вы уже зарегистрированы. Для станции нужен 4-значный код.')
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
            longpoll = VkBotLongPoll(session_vk, GROUP_ID)
            print('VK Long Poll started.')
            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    try:
                        process_message(event)
                    except Exception as exc:
                        print('Message error:', repr(exc))
        except Exception as exc:
            print('Long Poll error:', repr(exc))
            time.sleep(5)


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return func(*args, **kwargs)
    return wrapper


@app.route('/')
def index():
    return redirect(url_for('admin_dashboard') if session.get('admin') else url_for('admin_login'))


@app.get('/health')
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
        routes=routes,
        passes_map=passes_map,
        expected_map=expected_map,
        stations=database.get_stations(),
        tasks=database.get_tasks(),
        stats=database.stats(),
        final_location=database.get_setting('final_location', ''),
        oath=database.get_setting('oath', ''),
        event_enabled=event_enabled(),
    )


@app.post('/admin/general')
@admin_required
def admin_general():
    database.set_setting('final_location', request.form.get('final_location', '').strip())
    database.set_setting('oath', request.form.get('oath', '').strip())
    flash('Финальная точка и клятва сохранены.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/event')
@admin_required
def admin_event():
    database.set_setting('event_enabled', '1' if request.form.get('enabled') == '1' else '0')
    flash('Состояние мероприятия изменено.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/station/<int:station_id>')
@admin_required
def admin_station(station_id):
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip()
    if not name or len(code) != 4 or not code.isdigit():
        flash('Название обязательно, код должен состоять из 4 цифр.', 'error')
        return redirect(url_for('admin_dashboard'))
    try:
        database.update_station(station_id, name, code)
        flash(f'Станция {station_id} сохранена.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/task/<int:position>')
@admin_required
def admin_task(position):
    text = request.form.get('text', '').strip()
    if 1 <= position <= 7 and text:
        database.update_task(position, text)
        flash(f'Задание {position} сохранено.', 'success')
    else:
        flash('Задание не может быть пустым.', 'error')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>')
@admin_required
def admin_team(team_id):
    name = request.form.get('name', '').strip()
    reg = request.form.get('registration_code', '').strip()
    if not name or len(reg) != 5 or not reg.isdigit():
        flash('Название команды обязательно, код капитана должен быть из 5 цифр.', 'error')
        return redirect(url_for('admin_dashboard'))
    try:
        database.update_team(team_id, name, reg)
        flash(f'Команда {team_id} сохранена.', 'success')
    except Exception as exc:
        flash(f'Не удалось сохранить команду: {exc}', 'error')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>/route')
@admin_required
def admin_route(team_id):
    station_ids = []
    for pos in range(1, 7):
        try:
            station_ids.append(int(request.form.get(f'position_{pos}', '0')))
        except ValueError:
            station_ids.append(0)
    try:
        database.save_routes(team_id, station_ids)
        flash(f'Маршрут команды {team_id} сохранён. Прогресс команды сброшен.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>/recovery')
@admin_required
def admin_recovery(team_id):
    code, expires = database.make_recovery_code(team_id)
    flash(f'Код восстановления команды {team_id}: {code}. Действует до {expires.strftime("%H:%M:%S")}.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>/unlink')
@admin_required
def admin_unlink(team_id):
    database.clear_captain(team_id)
    flash(f'Капитан команды {team_id} отвязан.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/team/<int:team_id>/reset')
@admin_required
def admin_reset(team_id):
    database.reset_team(team_id)
    flash(f'Прогресс команды {team_id} сброшен.', 'success')
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
