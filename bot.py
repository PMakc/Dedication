import os,threading,time,json
from dotenv import load_dotenv
load_dotenv()
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll,VkBotEventType
from vk_api.utils import get_random_id
from flask import Flask,render_template,request,redirect,url_for,session,flash,send_from_directory
from werkzeug.utils import secure_filename
import database
from game_config import TEAM_CONFIG,STATIONS

TOKEN=os.getenv('VK_TOKEN','').strip(); GROUP_ID=int(os.getenv('GROUP_ID','0')); PORT=int(os.getenv('PORT','3000'))
ADMIN_LOGIN=os.getenv('ADMIN_LOGIN','admin'); ADMIN_PASSWORD=os.getenv('ADMIN_PASSWORD','CHANGE_ME'); SECRET=os.getenv('FLASK_SECRET','CHANGE_ME')
app=Flask(__name__); app.secret_key=SECRET; app.config['MAX_CONTENT_LENGTH']=15*1024*1024
database.init(TEAM_CONFIG)
vk_api_obj=None; upload=None

CONNECT_MODE_TEXT='🔑 Регистрация капитана'

def kb(rows): return {'one_time':False,'buttons':[[{'action':{'type':'text','label':x}} for x in row] for row in rows]}
def main_kb(): return kb([
    ['🎫 Получить слог'],['🧩 Мои слоги','🔤 Собрать слово'],['📝 Собрать предложение'],
    ['🏆 Мой клад'],[CONNECT_MODE_TEXT],['ℹ️ Помощь']
])
def send(peer,text,k=None,att=None):
    if not vk_api_obj:return
    p={'peer_id':peer,'random_id':get_random_id(),'message':text}
    if k:p['keyboard']=json.dumps(k,ensure_ascii=False)
    if att:p['attachment']=att
    vk_api_obj.messages.send(**p)
def uname(uid):
    try:
        u=vk_api_obj.users.get(user_ids=uid)[0]
        return f"{u.get('first_name','')} {u.get('last_name','')}".strip() or str(uid)
    except:return str(uid)

def registration_prompt(peer):
    send(peer,
        '🔑 Регистрация капитана\n\n'
        'Введите 5-значный код вашей команды.\n\n'
        'Пример: 38142\n\n'
        'Этот код можно использовать повторно, если капитану пришлось перейти на другой телефон или другой VK-аккаунт.',
        main_kb())

def award_from_station_code(peer,t,text):
    code=text.strip()
    cfg=TEAM_CONFIG[t['id']]
    station=None
    for st,station_code in cfg['station_codes'].items():
        if code==station_code:
            station=st; break
    if station is None: return False
    if database.awarded(t['id'],station):
        a=[x['syllable'] for x in database.awards(t['id']) if x['station']==station][0]
        send(peer,f'ℹ️ Станция «{STATIONS[station]}» уже пройдена.\nВаш слог: {a}',main_kb()); return True
    syll=cfg['syllables'][station]
    database.add_award(t['id'],station,syll)
    database.log(t['id'],t['vk_id'],'award',f'{station}:{code}:{syll}')
    send(peer,
         f'✅ Станция «{STATIONS[station]}» пройдена!\n\n'
         f'Ваш слог:\n{syll}\n\n'
         'Сохраните его для финальной сборки.',main_kb())
    return True

def award_button(peer,t):
    send(peer,
        '🎫 Получение слога теперь идёт по цифровому коду станции.\n\n'
        'Получите код у организатора и отправьте его боту.\n\n'
        'Для каждой команды код свой.',main_kb())

def words_menu(peer,t):
    cfg=TEAM_CONFIG[t['id']]; a=[x['syllable'] for x in database.awards(t['id'])]
    if len(a)<5: send(peer,f'🧩 Нужно получить все 5 слогов. Сейчас {len(a)}/5.',main_kb()); return
    i=t['word_i']
    if i>=len(cfg['words']): send(peer,'✅ Все слова собраны. Переходите к сборке предложения.',main_kb()); return
    d=database.draft(t); avail=[x for x in a if x not in d]
    text=f'🔤 Слово {i+1}/{len(cfg["words"])}\n\nСейчас: {"".join(d) or "—"}\n\nНажимайте слоги в правильном порядке.'
    rows=[]
    for x in avail:
        if not rows or len(rows[-1])==2: rows.append([])
        rows[-1].append(x)
    rows.append(['✅ Проверить','↩️ Очистить']); send(peer,text,kb(rows))

def process_word(peer,t,text):
    cfg=TEAM_CONFIG[t['id']]; d=database.draft(t); i=t['word_i']
    if i>=len(cfg['words']): return False
    if text=='↩️ Очистить': database.set_draft(t['id'],[]); words_menu(peer,database.team(t['id'])); return True
    if text=='✅ Проверить':
        if d==cfg['words'][i]:
            w=''.join(d); database.save_word(t['id'],i,w); send(peer,f'✅ Слово собрано: {w}'); words_menu(peer,database.team(t['id']))
        else:
            send(peer,'❌ Неверный порядок. Попробуйте ещё раз.'); words_menu(peer,database.team(t['id']))
        return True
    if text in [x['syllable'] for x in database.awards(t['id'])] and text not in d:
        d.append(text); database.set_draft(t['id'],d); words_menu(peer,database.team(t['id'])); return True
    return False

def sentence_menu(peer,t):
    cfg=TEAM_CONFIG[t['id']]; ws=[x['word'] for x in database.words(t['id'])]; d=database.sentence_draft(t)
    if len(ws)<len(cfg['words']): send(peer,'Сначала соберите все слова.',main_kb()); return
    avail=[x for x in ws if x not in d]; rows=[]
    for x in avail:
        if not rows or len(rows[-1])==2: rows.append([])
        rows[-1].append(x)
    rows.append(['✅ Проверить','↩️ Очистить']); send(peer,f'📝 Соберите предложение\n\nСейчас: {" ".join(d) or "—"}',kb(rows))

def process_sentence(peer,t,text):
    cfg=TEAM_CONFIG[t['id']]; d=database.sentence_draft(t)
    if text=='↩️ Очистить': database.set_sentence_draft(t['id'],[]); sentence_menu(peer,database.team(t['id'])); return True
    if text=='✅ Проверить':
        if d==cfg['sentence']:
            database.finish(t['id']); send(peer,'🎉 Поздравляем!\n\nВы правильно собрали финальное предложение.\nВаш индивидуальный клад открыт!'); treasure(peer,database.team(t['id']))
        else:
            send(peer,'❌ Неверный порядок слов.'); sentence_menu(peer,database.team(t['id']))
        return True
    ws=[x['word'] for x in database.words(t['id'])]
    if text in ws and text not in d: d.append(text); database.set_sentence_draft(t['id'],d); sentence_menu(peer,database.team(t['id'])); return True
    return False

def treasure(peer,t):
    if not t['finished']: send(peer,'🏆 Клад пока закрыт. Сначала соберите правильное предложение.',main_kb()); return
    cfg=TEAM_CONFIG[t['id']]
    photo=database.get_(f'photo_{t["id"]}','')
    enabled=database.get_(f'treasure_{t["id"]}','0')=='1'
    if not enabled:
        send(peer,'🏆 Вы победили!\n\nОрганизатор ещё не открыл фотографию вашего клада.',main_kb()); return
    path=os.path.join(database.DATA_DIR,'photos',photo)
    if not photo or not os.path.exists(path):
        send(peer,'🏆 Вы победили, но фотография вашего клада ещё не загружена.',main_kb()); return
    try:
        item=upload.photo_messages(path,peer_id=peer)[0]
        send(peer,'🏆 Место вашего клада найдено!',main_kb(),f"photo{item['owner_id']}_{item['id']}")
    except Exception as e:
        print('photo error',repr(e)); send(peer,'Не удалось отправить фото. Сообщите организатору.',main_kb())

def msg(event):
    m=event.object.message; peer=m['peer_id']; uid=m['from_id']; text=(m.get('text') or '').strip()
    t=database.by_vk(uid)

    if text in ('/start','начать','старт') and not t:
        send(peer,'🏔 Посвящение в Горную Академию\n\nДля начала зарегистрируйтесь как капитан команды по 5-значному коду.',main_kb()); return
    if text==CONNECT_MODE_TEXT or text.lower() in ('регистрация','код команды','подключить команду'):
        registration_prompt(peer); return

    if not t and text.isdigit() and len(text)==5:
        team=database.by_reg(text)
        if team:
            database.bind(team['id'],uid,uname(uid)); t=database.team(team['id'])
            send(peer,f'✅ Регистрация завершена!\n\nВы капитан команды: {t["name"]}\n\nТеперь на каждой станции вы будете получать отдельный код.',main_kb()); return
        send(peer,'❌ Такой код команды не найден. Проверьте 5 цифр и попробуйте ещё раз.',main_kb()); return

    if not t:
        send(peer,'👋 Вы ещё не зарегистрированы. Нажмите «🔑 Регистрация капитана» и введите 5-значный код.',main_kb()); return

    # Любой 4-значный код после регистрации пытаемся распознать как код станции.
    if text.isdigit() and len(text)==4:
        if award_from_station_code(peer,t,text): return
        send(peer,'❌ Этот код станции не подходит вашей команде или уже недействителен.',main_kb()); return

    if process_word(peer,t,text): return
    if process_sentence(peer,t,text): return
    if text=='🎫 Получить слог': award_button(peer,t)
    elif text=='🧩 Мои слоги':
        rows=database.awards(t['id']); send(peer,'🧩 Ваши слоги\n\n'+('Нет полученных слогов.' if not rows else '\n'.join(f'{STATIONS[x["station"]]} → {x["syllable"]}' for x in rows)),main_kb())
    elif text=='🔤 Собрать слово': words_menu(peer,t)
    elif text=='📝 Собрать предложение': sentence_menu(peer,t)
    elif text=='🏆 Мой клад': treasure(peer,t)
    elif text=='ℹ️ Помощь':
        send(peer,'ℹ️ Как проходит игра\n\n1. Зарегистрируйтесь по 5-значному коду команды.\n2. На каждой станции получите свой цифровой код.\n3. Отправьте код боту — бот выдаст вашей команде персональный слог.\n4. После 5 станций соберите слова.\n5. Затем соберите предложение.\n6. После правильного ответа откроется ваш индивидуальный клад.\n\nЕсли капитану нужно перейти на другой VK, администратор может выдать новый 5-значный код регистрации для этой команды.',main_kb())
    else: send(peer,'Используйте кнопки меню. Если вы получили код станции — просто отправьте его сообщением.',main_kb())

def bot_loop():
    global vk_api_obj,upload
    if not TOKEN or not GROUP_ID: print('VK_TOKEN или GROUP_ID не заданы'); return
    while True:
        try:
            s=vk_api.VkApi(token=TOKEN); vk_api_obj=s.get_api(); upload=vk_api.VkUpload(s); lp=VkBotLongPoll(s,GROUP_ID); print('VK bot started')
            for e in lp.listen():
                if e.type==VkBotEventType.MESSAGE_NEW:
                    try: msg(e)
                    except Exception as ex: print('message error',repr(ex))
        except Exception as ex: print('longpoll error',repr(ex)); time.sleep(5)

def admin(f):
    from functools import wraps
    @wraps(f)
    def w(*a,**kw): return f(*a,**kw) if session.get('admin') else redirect(url_for('login'))
    return w
@app.get('/')
def root(): return redirect('/admin')
@app.get('/health')
def health(): return {'status':'ok'}
@app.route('/admin/login',methods=['GET','POST'])
def login():
    if request.method=='POST' and request.form.get('login')==ADMIN_LOGIN and request.form.get('password')==ADMIN_PASSWORD:
        session['admin']=1; return redirect('/admin')
    if request.method=='POST': flash('Неверный логин или пароль.','error')
    return render_template('login.html')
@app.get('/admin/logout')
def logout(): session.clear(); return redirect('/admin/login')
@app.get('/admin')
@admin
def dashboard():
    return render_template('dashboard.html',teams=database.teams(),stats=database.stats(),event=database.get_('event')=='1',treasures=[database.get_(f'treasure_{i}','0')=='1' for i in range(1,6)],photos=[database.get_(f'photo_{i}','') for i in range(1,6)],config=TEAM_CONFIG)
@app.post('/admin/event')
@admin
def setevent(): database.set_('event',request.form.get('value','0')); return redirect('/admin')
@app.post('/admin/team/<int:tid>/photo')
@admin
def upload_treasure(tid):
    f=request.files.get('photo')
    if f and f.filename:
        ext=f.filename.rsplit('.',1)[-1].lower()
        if ext not in {'jpg','jpeg','png','webp'}: flash('Разрешены JPG, JPEG, PNG и WEBP.','error'); return redirect('/admin')
        fn=f'team_{tid}_{int(time.time())}_{secure_filename(f.filename)}'; f.save(os.path.join(database.DATA_DIR,'photos',fn)); database.set_(f'photo_{tid}',fn); flash(f'Фото клада команды {tid} загружено.','success')
    database.set_(f'treasure_{tid}','1' if request.form.get('enabled')=='1' else '0'); return redirect('/admin')
@app.post('/admin/team/<int:tid>/new-code')
@admin
def new_code(tid):
    import secrets
    code=str(secrets.randbelow(90000)+10000)
    while database.by_reg(code): code=str(secrets.randbelow(90000)+10000)
    c=database.conn(); c.execute('UPDATE teams SET registration_code=? WHERE id=?',(code,tid)); c.commit(); c.close(); flash(f'Новый код регистрации для команды {tid}: {code}','success'); return redirect('/admin')
@app.post('/admin/team/<int:tid>/unlink')
@admin
def unlink(tid): database.unlink(tid); flash('Капитан отвязан.','success'); return redirect('/admin')
@app.post('/admin/team/<int:tid>/reset')
@admin
def reset(tid): database.reset(tid); flash('Прогресс команды сброшен.','success'); return redirect('/admin')
@app.get('/admin/photos/<path:name>')
@admin
def photo(name): return send_from_directory(os.path.join(database.DATA_DIR,'photos'),name)

if __name__=='__main__':
    threading.Thread(target=bot_loop,daemon=True).start(); app.run('0.0.0.0',PORT,threaded=True)
