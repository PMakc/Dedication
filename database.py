import os
import sqlite3
import secrets
import string
from datetime import datetime, timedelta

DATA_DIR = os.getenv('DATA_DIR', '/app/data')
DB_PATH = os.path.join(DATA_DIR, 'quest_v5.db')

DEFAULT_STATIONS = [
    ('Верёвки', '1001'),
    ('Танцы', '2002'),
    ('Зарисовка фраз', '3003'),
    ('Лабиринт', '4004'),
    ('Переправа', '5005'),
    ('Станция 6', '6006'),
]

DEFAULT_TASKS = [
    'Ход гуськом.',
    'Переход с правой рукой между ног, держась за руки.',
    'Говорите друг другу комплименты во время перехода.',
    'Маршировка.',
    'Ход крабиком.',
    'Ламбада — танцы до следующей станции.',
    'Вприпрыжку.',
]

DEFAULT_TEAM_NAMES = [f'Команда {i}' for i in range(1, 7)]


def now():
    return datetime.now().isoformat(timespec='seconds')


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def connect():
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    c = connect()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            registration_code TEXT NOT NULL UNIQUE,
            vk_user_id INTEGER UNIQUE,
            captain_name TEXT,
            recovery_code TEXT,
            recovery_expires_at TEXT,
            finished INTEGER NOT NULL DEFAULT 0,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS routes (
            team_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            station_id INTEGER NOT NULL,
            PRIMARY KEY(team_id, position),
            UNIQUE(team_id, station_id),
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
            FOREIGN KEY(station_id) REFERENCES stations(id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            position INTEGER PRIMARY KEY,
            text TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS passes (
            team_id INTEGER NOT NULL,
            station_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            code TEXT NOT NULL,
            passed_at TEXT NOT NULL,
            PRIMARY KEY(team_id, position),
            UNIQUE(team_id, station_id),
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
            FOREIGN KEY(station_id) REFERENCES stations(id)
        );

        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER,
            vk_user_id INTEGER,
            action TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL
        );
    ''')

    defaults = {
        'event_enabled': '0',
        'final_location': 'Место финала пока не задано.',
        'oath': 'Клятва пока не задана.',
    }
    for k, v in defaults.items():
        c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))

    for idx, (name, code) in enumerate(DEFAULT_STATIONS, 1):
        c.execute('INSERT OR IGNORE INTO stations(id,name,code) VALUES(?,?,?)', (idx, name, code))

    for idx, task in enumerate(DEFAULT_TASKS, 1):
        c.execute('INSERT OR IGNORE INTO tasks(position,text) VALUES(?,?)', (idx, task))

    for idx, team_name in enumerate(DEFAULT_TEAM_NAMES, 1):
        code = f'{10000 + idx}'
        c.execute(
            'INSERT OR IGNORE INTO teams(id,name,registration_code) VALUES(?,?,?)',
            (idx, team_name, code)
        )

    # Default route is a cyclic rotation of stations, one unique route per team.
    for team_id in range(1, 7):
        for pos in range(1, 7):
            station_id = ((team_id + pos - 2) % 6) + 1
            c.execute(
                'INSERT OR IGNORE INTO routes(team_id,position,station_id) VALUES(?,?,?)',
                (team_id, pos, station_id)
            )

    c.commit()
    c.close()


def log_action(team_id, vk_user_id, action, payload=''):
    c = connect()
    c.execute(
        'INSERT INTO actions(team_id,vk_user_id,action,payload,created_at) VALUES(?,?,?,?,?)',
        (team_id, vk_user_id, action, payload, now())
    )
    c.commit(); c.close()


def get_setting(key, default=None):
    c = connect(); row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone(); c.close()
    return row['value'] if row else default


def set_setting(key, value):
    c = connect()
    c.execute('''INSERT INTO settings(key,value) VALUES(?,?)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, str(value)))
    c.commit(); c.close()


def get_stations():
    c = connect(); rows = c.execute('SELECT * FROM stations ORDER BY id').fetchall(); c.close(); return rows


def get_station(station_id):
    c = connect(); row = c.execute('SELECT * FROM stations WHERE id=?', (station_id,)).fetchone(); c.close(); return row


def update_station(station_id, name, code):
    c = connect()
    duplicate = c.execute('SELECT 1 FROM stations WHERE code=? AND id<>?', (code.strip(), station_id)).fetchone()
    if duplicate:
        c.close()
        raise ValueError('Этот 4-значный код уже назначен другой станции.')
    c.execute('UPDATE stations SET name=?,code=? WHERE id=?', (name.strip(), code.strip(), station_id))
    c.commit(); c.close()


def get_tasks():
    c = connect(); rows = c.execute('SELECT * FROM tasks ORDER BY position').fetchall(); c.close(); return rows


def get_task_text(position):
    c = connect(); row = c.execute('SELECT text FROM tasks WHERE position=?', (position,)).fetchone(); c.close()
    return row['text'] if row else ''


def update_task(position, text):
    c = connect(); c.execute('UPDATE tasks SET text=? WHERE position=?', (text.strip(), position)); c.commit(); c.close()


def get_teams():
    c = connect(); rows = c.execute('SELECT * FROM teams ORDER BY id').fetchall(); c.close(); return rows


def get_team(team_id):
    c = connect(); row = c.execute('SELECT * FROM teams WHERE id=?', (team_id,)).fetchone(); c.close(); return row


def get_team_by_vk(vk_user_id):
    c = connect(); row = c.execute('SELECT * FROM teams WHERE vk_user_id=?', (vk_user_id,)).fetchone(); c.close(); return row


def get_team_by_registration_code(code):
    c = connect(); row = c.execute('SELECT * FROM teams WHERE registration_code=?', (code,)).fetchone(); c.close(); return row


def update_team(team_id, name, registration_code):
    c = connect()
    c.execute('UPDATE teams SET name=?,registration_code=? WHERE id=?', (name.strip(), registration_code.strip(), team_id))
    c.commit(); c.close()


def clear_captain(team_id):
    c = connect(); c.execute('UPDATE teams SET vk_user_id=NULL,captain_name=NULL WHERE id=?', (team_id,)); c.commit(); c.close()


def bind_captain(team_id, vk_user_id, captain_name):
    c = connect()
    c.execute('UPDATE teams SET vk_user_id=NULL,captain_name=NULL WHERE vk_user_id=?', (vk_user_id,))
    c.execute('UPDATE teams SET vk_user_id=?,captain_name=?,recovery_code=NULL,recovery_expires_at=NULL WHERE id=?',
              (vk_user_id, captain_name, team_id))
    c.commit(); c.close()


def make_recovery_code(team_id, ttl_minutes=15):
    c = connect()
    for _ in range(100):
        code = ''.join(secrets.choice(string.digits) for _ in range(5))
        exists = c.execute(
            'SELECT 1 FROM teams WHERE registration_code=? OR recovery_code=?',
            (code, code)
        ).fetchone()
        if not exists:
            break
    else:
        c.close()
        raise RuntimeError('Не удалось сгенерировать уникальный код.')
    expires = datetime.now() + timedelta(minutes=ttl_minutes)
    c.execute('UPDATE teams SET recovery_code=?,recovery_expires_at=? WHERE id=?',
              (code, expires.isoformat(timespec='seconds'), team_id))
    c.commit(); c.close()
    return code, expires


def claim_recovery(code, vk_user_id, captain_name):
    c = connect()
    row = c.execute('SELECT * FROM teams WHERE recovery_code=?', (code,)).fetchone()
    if not row:
        c.close(); return None, 'Код восстановления не найден.'
    try:
        expires = datetime.fromisoformat(row['recovery_expires_at'])
    except Exception:
        expires = datetime.min
    if datetime.now() > expires:
        c.close(); return None, 'Срок действия кода восстановления истёк.'
    c.execute('UPDATE teams SET vk_user_id=NULL,captain_name=NULL WHERE vk_user_id=?', (vk_user_id,))
    c.execute('UPDATE teams SET vk_user_id=?,captain_name=?,recovery_code=NULL,recovery_expires_at=NULL WHERE id=?',
              (vk_user_id, captain_name, row['id']))
    c.commit(); c.close()
    return row['id'], None


def save_routes(team_id, station_ids):
    if len(station_ids) != 6 or len(set(station_ids)) != 6:
        raise ValueError('Маршрут должен содержать все 6 разных станций ровно по одному разу.')
    c = connect()
    c.execute('DELETE FROM routes WHERE team_id=?', (team_id,))
    for pos, station_id in enumerate(station_ids, 1):
        c.execute('INSERT INTO routes(team_id,position,station_id) VALUES(?,?,?)', (team_id, pos, station_id))
    # Route changes make the team's current progress invalid by design.
    c.execute('DELETE FROM passes WHERE team_id=?', (team_id,))
    c.execute('UPDATE teams SET finished=0,finished_at=NULL WHERE id=?', (team_id,))
    c.commit(); c.close()


def get_route(team_id):
    c = connect()
    rows = c.execute('''SELECT r.position,r.station_id,s.name,s.code
                        FROM routes r JOIN stations s ON s.id=r.station_id
                        WHERE r.team_id=? ORDER BY r.position''', (team_id,)).fetchall()
    c.close(); return rows


def get_passes(team_id):
    c = connect()
    rows = c.execute('''SELECT p.position,p.station_id,p.code,p.passed_at,s.name
                        FROM passes p JOIN stations s ON s.id=p.station_id
                        WHERE p.team_id=? ORDER BY p.position''', (team_id,)).fetchall()
    c.close(); return rows


def current_step(team_id):
    return len(get_passes(team_id))


def expected_station(team_id):
    step = current_step(team_id)
    route = get_route(team_id)
    if step >= 6:
        return None
    return route[step]


def record_pass(team_id, station_id, position, code):
    c = connect()
    try:
        c.execute('INSERT INTO passes(team_id,station_id,position,code,passed_at) VALUES(?,?,?,?,?)',
                  (team_id, station_id, position, code, now()))
        c.commit(); ok=True
        if position == 6:
            c.execute('UPDATE teams SET finished=1,finished_at=? WHERE id=?', (now(), team_id))
            c.commit()
    except sqlite3.IntegrityError:
        ok=False
    c.close(); return ok


def reset_team(team_id):
    c = connect()
    c.execute('DELETE FROM passes WHERE team_id=?', (team_id,))
    c.execute('UPDATE teams SET finished=0,finished_at=NULL WHERE id=?', (team_id,))
    c.commit(); c.close()


def reset_all():
    c = connect()
    c.execute('DELETE FROM passes')
    c.execute('UPDATE teams SET finished=0,finished_at=NULL')
    c.commit(); c.close()


def stats():
    c = connect()
    data = {
        'teams': c.execute('SELECT COUNT(*) AS c FROM teams').fetchone()['c'],
        'captains': c.execute('SELECT COUNT(*) AS c FROM teams WHERE vk_user_id IS NOT NULL').fetchone()['c'],
        'finished': c.execute('SELECT COUNT(*) AS c FROM teams WHERE finished=1').fetchone()['c'],
        'passes': c.execute('SELECT COUNT(*) AS c FROM passes').fetchone()['c'],
    }
    c.close(); return data
