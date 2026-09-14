import os, sqlite3, json, secrets, hashlib
from datetime import datetime

DATA_DIR = os.getenv('DATA_DIR', '/app/data')
DB = os.path.join(DATA_DIR, 'quest.db')


def ensure():
    os.makedirs(os.path.join(DATA_DIR, 'photos'), exist_ok=True)


def conn():
    ensure()
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def add_column(c, table, column, definition):
    cols = {r[1] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
    if column not in cols:
        c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def init(config):
    ensure()
    c = conn()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS teams(
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      registration_code TEXT UNIQUE,
      vk_id INTEGER UNIQUE,
      captain TEXT,
      sentence TEXT DEFAULT '',
      words_json TEXT DEFAULT '[]',
      syllables_json TEXT DEFAULT '[]',
      station_codes_json TEXT DEFAULT '{}',
      word_i INTEGER DEFAULT 0,
      word_draft TEXT DEFAULT '[]',
      sentence_draft TEXT DEFAULT '[]',
      finished INTEGER DEFAULT 0,
      finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS awards(
      team_id INTEGER,station INTEGER,syllable TEXT,created_at TEXT,
      PRIMARY KEY(team_id,station)
    );
    CREATE TABLE IF NOT EXISTS words(
      team_id INTEGER,word_i INTEGER,word TEXT,created_at TEXT,
      PRIMARY KEY(team_id,word_i)
    );
    CREATE TABLE IF NOT EXISTS actions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,team_id INTEGER,vk_id INTEGER,
      action TEXT,payload TEXT,created_at TEXT
    );
    ''')
    # Migration for the older database versions.
    add_column(c, 'teams', 'registration_code', 'TEXT')
    add_column(c, 'teams', 'sentence', "TEXT DEFAULT ''")
    add_column(c, 'teams', 'words_json', "TEXT DEFAULT '[]'")
    add_column(c, 'teams', 'syllables_json', "TEXT DEFAULT '[]'")
    add_column(c, 'teams', 'station_codes_json', "TEXT DEFAULT '{}'")
    add_column(c, 'teams', 'word_i', 'INTEGER DEFAULT 0')
    add_column(c, 'teams', 'word_draft', "TEXT DEFAULT '[]'")
    add_column(c, 'teams', 'sentence_draft', "TEXT DEFAULT '[]'")
    add_column(c, 'teams', 'finished', 'INTEGER DEFAULT 0')
    add_column(c, 'teams', 'finished_at', 'TEXT')

    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_teams_registration_code ON teams(registration_code)')
    c.execute('INSERT OR IGNORE INTO settings(k,v) VALUES(?,?)', ('event', '0'))

    for tid, x in config.items():
        c.execute('INSERT OR IGNORE INTO teams(id,name,registration_code) VALUES(?,?,?)',
                  (tid, x['name'], x.get('registration_code')))
        c.execute('UPDATE teams SET name=? WHERE id=?', (x['name'], tid))
        # Import v3 config only if DB is not configured yet.
        row = c.execute('SELECT sentence,station_codes_json,syllables_json FROM teams WHERE id=?', (tid,)).fetchone()
        if row and not row['sentence']:
            sentence = ' '.join(row2 for row2 in x.get('sentence', []))
            c.execute('UPDATE teams SET sentence=?, words_json=?, syllables_json=?, station_codes_json=? WHERE id=?',
                      (sentence, json.dumps(x.get('words', []), ensure_ascii=False),
                       json.dumps(list(x.get('syllables', {}).values()), ensure_ascii=False),
                       json.dumps({str(k): v for k, v in x.get('station_codes', {}).items()}, ensure_ascii=False), tid))
    c.commit(); c.close()


def set_(k, v):
    c = conn(); c.execute('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v))); c.commit(); c.close()


def get_(k, default=None):
    c = conn(); r = c.execute('SELECT v FROM settings WHERE k=?', (k,)).fetchone(); c.close(); return r['v'] if r else default


def teams():
    c = conn(); r = c.execute('''SELECT t.*,
      (SELECT COUNT(*) FROM awards a WHERE a.team_id=t.id) awards_count,
      (SELECT COUNT(*) FROM words w WHERE w.team_id=t.id) words_count
      FROM teams t ORDER BY t.id''').fetchall(); c.close(); return r


def team(tid):
    c = conn(); r = c.execute('SELECT * FROM teams WHERE id=?', (tid,)).fetchone(); c.close(); return r


def by_vk(vk):
    c = conn(); r = c.execute('SELECT * FROM teams WHERE vk_id=?', (vk,)).fetchone(); c.close(); return r


def by_reg(code):
    c = conn(); r = c.execute('SELECT * FROM teams WHERE registration_code=?', (str(code),)).fetchone(); c.close(); return r


def bind(tid, vk, name):
    c = conn()
    c.execute('UPDATE teams SET vk_id=NULL,captain=NULL WHERE vk_id=?', (vk,))
    c.execute('UPDATE teams SET vk_id=?,captain=? WHERE id=?', (vk, name, tid))
    c.commit(); c.close()


def unlink(tid):
    c = conn(); c.execute('UPDATE teams SET vk_id=NULL,captain=NULL WHERE id=?', (tid,)); c.commit(); c.close()


def update_config(tid, name, registration_code, station_codes, sentence, words, syllables):
    c = conn()
    c.execute('''UPDATE teams SET name=?,registration_code=?,station_codes_json=?,sentence=?,words_json=?,syllables_json=? WHERE id=?''',
              (name, registration_code, json.dumps({str(k): str(v) for k, v in station_codes.items()}, ensure_ascii=False),
               sentence, json.dumps(words, ensure_ascii=False), json.dumps(syllables, ensure_ascii=False), tid))
    c.commit(); c.close()


def config(team_row):
    try: codes = {int(k): str(v) for k, v in json.loads(team_row['station_codes_json'] or '{}').items()}
    except Exception: codes = {}
    try: words = json.loads(team_row['words_json'] or '[]')
    except Exception: words = []
    try: syllables = json.loads(team_row['syllables_json'] or '[]')
    except Exception: syllables = []
    return {'station_codes': codes, 'words': words, 'syllables': syllables, 'sentence': team_row['sentence'] or ''}


def log(tid, vk, action, payload=''):
    c = conn(); c.execute('INSERT INTO actions(team_id,vk_id,action,payload,created_at) VALUES(?,?,?,?,?)',
                          (tid, vk, action, payload, datetime.now().isoformat(timespec='seconds'))); c.commit(); c.close()


def awards(tid):
    c = conn(); r = c.execute('SELECT * FROM awards WHERE team_id=? ORDER BY station', (tid,)).fetchall(); c.close(); return r


def awarded(tid, station):
    c = conn(); r = c.execute('SELECT 1 FROM awards WHERE team_id=? AND station=?', (tid, station)).fetchone(); c.close(); return bool(r)


def add_award(tid, station, syllable):
    c = conn()
    try:
        c.execute('INSERT INTO awards VALUES(?,?,?,?)', (tid, station, syllable, datetime.now().isoformat(timespec='seconds')))
        c.commit(); ok = True
    except sqlite3.IntegrityError: ok = False
    c.close(); return ok


def words(tid):
    c = conn(); r = c.execute('SELECT * FROM words WHERE team_id=? ORDER BY word_i', (tid,)).fetchall(); c.close(); return r


def save_word(tid, i, w):
    c = conn(); c.execute('INSERT OR REPLACE INTO words(team_id,word_i,word,created_at) VALUES(?,?,?,?)',
                          (tid, i, w, datetime.now().isoformat(timespec='seconds')))
    c.execute('UPDATE teams SET word_i=?,word_draft="[]" WHERE id=?', (i + 1, tid)); c.commit(); c.close()


def draft(t): return json.loads(t['word_draft'] or '[]')
def set_draft(tid, a):
    c = conn(); c.execute('UPDATE teams SET word_draft=? WHERE id=?', (json.dumps(a, ensure_ascii=False), tid)); c.commit(); c.close()
def sentence_draft(t): return json.loads(t['sentence_draft'] or '[]')
def set_sentence_draft(tid, a):
    c = conn(); c.execute('UPDATE teams SET sentence_draft=? WHERE id=?', (json.dumps(a, ensure_ascii=False), tid)); c.commit(); c.close()


def finish(tid):
    c = conn(); c.execute('UPDATE teams SET finished=1,finished_at=? WHERE id=?', (datetime.now().isoformat(timespec='seconds'), tid)); c.commit(); c.close()


def reset(tid):
    c = conn()
    c.execute("UPDATE teams SET word_i=0,word_draft='[]',sentence_draft='[]',finished=0,finished_at=NULL WHERE id=?", (tid,))
    c.execute('DELETE FROM awards WHERE team_id=?', (tid,)); c.execute('DELETE FROM words WHERE team_id=?', (tid,)); c.commit(); c.close()


def stats():
    c = conn(); out = {
      'teams': c.execute('SELECT COUNT(*) c FROM teams').fetchone()['c'],
      'captains': c.execute('SELECT COUNT(*) c FROM teams WHERE vk_id IS NOT NULL').fetchone()['c'],
      'finished': c.execute('SELECT COUNT(*) c FROM teams WHERE finished=1').fetchone()['c'],
      'awards': c.execute('SELECT COUNT(*) c FROM awards').fetchone()['c']
    }; c.close(); return out
