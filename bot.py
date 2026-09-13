import os, threading, time, json
from dotenv import load_dotenv
load_dotenv()
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll,VkBotEventType
from vk_api.utils import get_random_id
from flask import Flask,render_template,request,redirect,url_for,session,flash,send_from_directory
from werkzeug.utils import secure_filename
import database
from game_config import TEAM_CONFIG,STATIONS
TOKEN=os.getenv('VK_TOKEN',''); GROUP_ID=int(os.getenv('GROUP_ID','0')); PORT=int(os.getenv('PORT','3000'))
ADMIN_LOGIN=os.getenv('ADMIN_LOGIN','admin'); ADMIN_PASSWORD=os.getenv('ADMIN_PASSWORD','CHANGE_ME'); SECRET=os.getenv('FLASK_SECRET','CHANGE_ME')
app=Flask(__name__); app.secret_key=SECRET; app.config['MAX_CONTENT_LENGTH']=15*1024*1024
database.init(TEAM_CONFIG)
vk_api_obj=None; upload=None
def rnd(): return int(database.get_('round','1'))
def station_for(t): return ((t['start']-1+rnd()-1)%5)+1
def kb(rows): return {'one_time':False,'buttons':[[{'action':{'type':'text','label':x}} for x in row] for row in rows]}
def main_kb(): return kb([['🎫 Получить слог'],['🧩 Мои слоги','🔤 Собрать слово'],['📝 Собрать предложение'],['🏆 Клад'],['🔄 Восстановить команду'],['ℹ️ Помощь']])
def send(peer,text,k=None,att=None):
    if not vk_api_obj:return
    p={'peer_id':peer,'random_id':get_random_id(),'message':text}
    if k:p['keyboard']=json.dumps(k,ensure_ascii=False)
    if att:p['attachment']=att
    vk_api_obj.messages.send(**p)
def uname(uid):
    try:
        x=vk_api_obj.users.get(user_ids=uid)[0]; return f"{x.get('first_name','')} {x.get('last_name','')}".strip()
    except:return str(uid)
def award(peer,t):
    if database.get_('event','0')!='1': send(peer,'⏸ Мероприятие пока не запущено.',main_kb()); return
    st=station_for(t)
    if database.awarded(t['id'],st):
        a=[x['syllable'] for x in database.awards(t['id']) if x['station']==st][0]
        send(peer,f'ℹ️ Эта станция уже пройдена. Ваш слог: <b>{a}</b>',main_kb()); return
    s=TEAM_CONFIG[t['id']]['syllables'][st]
    database.add_award(t['id'],st,s); send(peer,f'✅ Станция «{STATIONS[st]}» пройдена!\n\nВаш слог:\n<b>{s}</b>',main_kb())
def words_menu(peer,t):
    cfg=TEAM_CONFIG[t['id']]; a=[x['syllable'] for x in database.awards(t['id'])]
    if len(a)<5: send(peer,f'🧩 Нужно получить все 5 слогов. Сейчас {len(a)}/5.',main_kb()); return
    i=t['word_i'];
    if i>=len(cfg['words']): send(peer,'✅ Все слова собраны. Переходите к предложению.',main_kb()); return
    d=database.draft(t); avail=[x for x in a if x not in d]; text=f'🔤 Слово {i+1}/{len(cfg["words"])}\n\nСейчас: <b>{"".join(d) or "—"}</b>\n\nНажимайте слоги по порядку.'
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
            database.save_word(t['id'],i,''.join(d)); send(peer,f'✅ Слово собрано: <b>{"".join(d)}</b>'); words_menu(peer,database.team(t['id']))
        else: send(peer,'❌ Неверный порядок. Попробуйте ещё раз.'); return True
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
    rows.append(['✅ Проверить','↩️ Очистить']); send(peer,f'📝 Соберите предложение\n\nСейчас: <b>{" ".join(d) or "—"}</b>',kb(rows))
def process_sentence(peer,t,text):
    cfg=TEAM_CONFIG[t['id']]; d=database.sentence_draft(t)
    if text=='↩️ Очистить': database.set_sentence_draft(t['id'],[]); sentence_menu(peer,database.team(t['id'])); return True
    if text=='✅ Проверить':
        if d==cfg['sentence']:
            database.finish(t['id']); send(peer,'🎉 <b>Поздравляем!</b> Вы правильно собрали предложение. Клад разблокирован!'); treasure(peer,database.team(t['id']))
        else: send(peer,'❌ Неверный порядок слов.'); sentence_menu(peer,database.team(t['id']))
        return True
    ws=[x['word'] for x in database.words(t['id'])]
    if text in ws and text not in d: d.append(text); database.set_sentence_draft(t['id'],d); sentence_menu(peer,database.team(t['id'])); return True
    return False
def treasure(peer,t):
    if not t['finished']: send(peer,'🏆 Клад закрыт. Сначала соберите предложение.',main_kb()); return
    if database.get_('treasure','0')!='1': send(peer,'🏆 Вы победили! Организатор ещё не открыл фотографию клада.',main_kb()); return
    fn=database.get_('photo',''); path=os.path.join(database.DATA_DIR,'photos',fn)
    if not fn or not os.path.exists(path): send(peer,'Фото клада ещё не загружено.',main_kb()); return
    try:
        item=upload.photo_messages(path,peer_id=peer)[0]; att=f"photo{item['owner_id']}_{item['id']}"; send(peer,'🏆 <b>Место клада найдено!</b>',main_kb(),att)
    except Exception as e: print(e); send(peer,'Ошибка отправки фотографии. Сообщите организатору.',main_kb())
def msg(event):
    m=event.object.message; peer=m['peer_id']; uid=m['from_id']; text=(m.get('text') or '').strip(); t=database.by_vk(uid)
    if not t and '-' in text:
        tid=database.claim(text,uid,uname(uid))
        if tid: t=database.team(tid); send(peer,f'✅ Вы назначены капитаном «{t["name"]}». Прогресс сохранён.',main_kb()); return
    if not t:
        send(peer,'👋 Вы ещё не привязаны к команде. Нажмите «🔄 Восстановить команду».',main_kb()); return
    if text in ('/start','начать','старт'): send(peer,f'🏔 <b>{t["name"]}</b>\nКапитан: {t["captain"] or "—"}\nРаунд: {rnd()}/5\nТекущая станция: {STATIONS[station_for(t)]}',main_kb()); return
    if process_word(peer,t,text): return
    if process_sentence(peer,t,text): return
    if text=='🎫 Получить слог': award(peer,t)
    elif text=='🧩 Мои слоги': send(peer,'\n'.join(f'{STATIONS[x["station"]]} → <b>{x["syllable"]}</b>' for x in database.awards(t['id'])) or 'Слогов пока нет.',main_kb())
    elif text=='🔤 Собрать слово': words_menu(peer,t)
    elif text=='📝 Собрать предложение': sentence_menu(peer,t)
    elif text=='🏆 Клад': treasure(peer,t)
    elif text=='🔄 Восстановить команду': send(peer,'🔄 Организатор выдаёт временный код. Отправьте его сюда.',main_kb())
    elif text=='ℹ️ Помощь': send(peer,'1) На каждом раунде нажмите «Получить слог».\n2) После 5 станций соберите слова.\n3) Затем соберите предложение.\n4) После правильного ответа получите фото клада.\n\nТелефон можно заменить кодом восстановления.',main_kb())
    else: send(peer,'Используйте кнопки меню.',main_kb())
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
@app.get('/health')
def health(): return {'status':'ok'}
@app.route('/admin/login',methods=['GET','POST'])
def login():
    if request.method=='POST' and request.form.get('login')==ADMIN_LOGIN and request.form.get('password')==ADMIN_PASSWORD: session['admin']=1; return redirect('/admin')
    return render_template('login.html')
@app.get('/admin/logout')
def logout(): session.clear(); return redirect('/admin/login')
@app.get('/admin')
@admin
def dashboard(): return render_template('dashboard.html',teams=database.teams(),round=rnd(),event=database.get_('event')=='1',treasure=database.get_('treasure')=='1',photo=database.get_('photo',''),stations=STATIONS)
@app.post('/admin/round')
@admin
def setround(): database.set_('round',request.form['round']); return redirect('/admin')
@app.post('/admin/event')
@admin
def setevent(): database.set_('event',request.form['value']); return redirect('/admin')
@app.post('/admin/treasure')
@admin
def settreasure():
    f=request.files.get('photo')
    if f and f.filename:
        ext=f.filename.rsplit('.',1)[-1].lower();
        if ext not in {'jpg','jpeg','png','webp'}: flash('Только JPG/PNG/WEBP.'); return redirect('/admin')
        fn=f"treasure_{int(time.time())}_{secure_filename(f.filename)}"; f.save(os.path.join(database.DATA_DIR,'photos',fn)); database.set_('photo',fn)
    database.set_('treasure','1' if request.form.get('enabled')=='1' else '0'); return redirect('/admin')
@app.post('/admin/team/<int:tid>/recovery')
@admin
def rec(tid):
    code,exp=database.recovery(tid); flash(f'Код для {database.team(tid)["name"]}: {code} — действует до {exp.strftime("%H:%M:%S")}'); return redirect('/admin')
@app.post('/admin/team/<int:tid>/unlink')
@admin
def un(tid): database.unlink(tid); return redirect('/admin')
@app.post('/admin/team/<int:tid>/reset')
@admin
def reset(tid): database.reset(tid); return redirect('/admin')
@app.get('/admin/photos/<path:name>')
@admin
def photo(name): return send_from_directory(os.path.join(database.DATA_DIR,'photos'),name)
if __name__=='__main__':
    threading.Thread(target=bot_loop,daemon=True).start(); app.run('0.0.0.0',PORT,threaded=True)
