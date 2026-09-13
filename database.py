import os, sqlite3, json, secrets, hashlib
from datetime import datetime,timedelta
DATA_DIR=os.getenv('DATA_DIR','/app/data'); DB=os.path.join(DATA_DIR,'quest.db')
os.makedirs(os.path.join(DATA_DIR,'photos'),exist_ok=True)
def db():
    c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; return c
def init(config):
    c=db(); c.executescript('''
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS teams(id INTEGER PRIMARY KEY,name TEXT NOT NULL,start INTEGER NOT NULL,vk_id INTEGER UNIQUE,captain TEXT,word_i INTEGER DEFAULT 0,word_draft TEXT DEFAULT '[]',sentence_draft TEXT DEFAULT '[]',finished INTEGER DEFAULT 0,finished_at TEXT,recovery_hash TEXT,recovery_exp TEXT);
    CREATE TABLE IF NOT EXISTS awards(team_id INTEGER,station INTEGER,syllable TEXT,created_at TEXT,PRIMARY KEY(team_id,station));
    CREATE TABLE IF NOT EXISTS words(team_id INTEGER,word_i INTEGER,word TEXT,created_at TEXT,PRIMARY KEY(team_id,word_i));
    ''')
    for k,v in {'event':'0','round':'1','treasure':'0','photo':''}.items(): c.execute('INSERT OR IGNORE INTO settings(k,v) VALUES(?,?)',(k,v))
    for tid,x in config.items():
        c.execute('INSERT OR IGNORE INTO teams(id,name,start) VALUES(?,?,?)',(tid,x['name'],x['start']))
        c.execute('UPDATE teams SET name=?,start=? WHERE id=?',(x['name'],x['start'],tid))
    c.commit(); c.close()
def set_(k,v):
    c=db(); c.execute('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,str(v))); c.commit(); c.close()
def get_(k,d=None):
    c=db(); r=c.execute('SELECT v FROM settings WHERE k=?',(k,)).fetchone(); c.close(); return r['v'] if r else d
def teams():
    c=db(); r=c.execute('SELECT * FROM teams ORDER BY id').fetchall(); c.close(); return r
def team(tid):
    c=db(); r=c.execute('SELECT * FROM teams WHERE id=?',(tid,)).fetchone(); c.close(); return r
def by_vk(vk):
    c=db(); r=c.execute('SELECT * FROM teams WHERE vk_id=?',(vk,)).fetchone(); c.close(); return r
def bind(tid,vk,name):
    c=db(); c.execute('UPDATE teams SET vk_id=NULL,captain=NULL WHERE vk_id=?',(vk,)); c.execute('UPDATE teams SET vk_id=?,captain=? WHERE id=?',(vk,name,tid)); c.commit(); c.close()
def unlink(tid):
    c=db(); c.execute('UPDATE teams SET vk_id=NULL,captain=NULL WHERE id=?',(tid,)); c.commit(); c.close()
def awards(tid):
    c=db(); r=c.execute('SELECT * FROM awards WHERE team_id=? ORDER BY station',(tid,)).fetchall(); c.close(); return r
def awarded(tid,st):
    c=db(); r=c.execute('SELECT 1 FROM awards WHERE team_id=? AND station=?',(tid,st)).fetchone(); c.close(); return bool(r)
def add_award(tid,st,s):
    c=db()
    try:
        c.execute('INSERT INTO awards VALUES(?,?,?,?)',(tid,st,s,datetime.now().isoformat(timespec='seconds'))); c.commit(); ok=True
    except sqlite3.IntegrityError: ok=False
    c.close(); return ok
def words(tid):
    c=db(); r=c.execute('SELECT * FROM words WHERE team_id=? ORDER BY word_i',(tid,)).fetchall(); c.close(); return r
def save_word(tid,i,w):
    c=db(); c.execute('INSERT OR REPLACE INTO words(team_id,word_i,word,created_at) VALUES(?,?,?,?)',(tid,i,w,datetime.now().isoformat(timespec='seconds'))); c.execute('UPDATE teams SET word_i=?,word_draft="[]" WHERE id=?',(i+1,tid)); c.commit(); c.close()
def draft(t):
    return json.loads(t['word_draft'] or '[]')
def set_draft(tid,a):
    c=db(); c.execute('UPDATE teams SET word_draft=? WHERE id=?',(json.dumps(a,ensure_ascii=False),tid)); c.commit(); c.close()
def sentence_draft(t): return json.loads(t['sentence_draft'] or '[]')
def set_sentence_draft(tid,a):
    c=db(); c.execute('UPDATE teams SET sentence_draft=? WHERE id=?',(json.dumps(a,ensure_ascii=False),tid)); c.commit(); c.close()
def finish(tid):
    c=db(); c.execute('UPDATE teams SET finished=1,finished_at=? WHERE id=?',(datetime.now().isoformat(timespec='seconds'),tid)); c.commit(); c.close()
def reset(tid):
    c=db(); c.execute("UPDATE teams SET word_i=0,word_draft='[]',sentence_draft='[]',finished=0,finished_at=NULL WHERE id=?",(tid,)); c.execute('DELETE FROM awards WHERE team_id=?',(tid,)); c.execute('DELETE FROM words WHERE team_id=?',(tid,)); c.commit(); c.close()
def recovery(tid):
    code=secrets.token_hex(3).upper(); tail=secrets.token_hex(4); raw=f'{code}:{tail}'; h=hashlib.sha256(raw.encode()).hexdigest(); exp=datetime.now()+timedelta(minutes=15)
    c=db(); c.execute('UPDATE teams SET recovery_hash=?,recovery_exp=? WHERE id=?',(h,exp.isoformat(timespec='seconds'),tid)); c.commit(); c.close(); return f'{code}-{tail}',exp
def claim(value,vk,name):
    if '-' not in value:return None
    code,tail=value.split('-',1); h=hashlib.sha256(f'{code.upper()}:{tail}'.encode()).hexdigest(); c=db(); r=c.execute('SELECT * FROM teams WHERE recovery_hash=?',(h,)).fetchone()
    if not r:return None
    try: ok=datetime.now()<=datetime.fromisoformat(r['recovery_exp'])
    except: ok=False
    if not ok:return None
    c.execute('UPDATE teams SET vk_id=NULL,captain=NULL WHERE vk_id=?',(vk,)); c.execute('UPDATE teams SET vk_id=?,captain=?,recovery_hash=NULL,recovery_exp=NULL WHERE id=?',(vk,name,r['id'])); c.commit(); c.close(); return r['id']
