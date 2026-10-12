import os, secrets, csv
from getpass import getpass
import psycopg
from psycopg.rows import dict_row
from functools import wraps
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, Response, jsonify, abort
import advisor
from werkzeug.security import generate_password_hash, check_password_hash

BASE = Path(__file__).resolve().parent
DATA_PATH = BASE / 'data' / 'sugarcane_dataset.csv'
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or 'local-dev-' + secrets.token_hex(24)
if os.environ.get('FLASK_ENV') == 'production' and not os.environ.get('SECRET_KEY'):
    raise RuntimeError('Set SECRET_KEY in the hosting environment before production use.')



DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_NAME = os.environ.get('DB_NAME', 'sugarcane_advisory')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_PASSWORD = os.environ.get('DB_PASSWORD') or getpass('Enter PostgreSQL password for user postgres: ')


def connect_db():
    return psycopg.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD, row_factory=dict_row
    )


def db():
    if 'db' not in g:
        g.db = connect_db()
    return g.db

@app.teardown_appcontext
def close_db(_error=None):
    conn = g.pop('db', None)
    if conn is not None: conn.close()


def init_db():
    # Fresh PostgreSQL tables for website accounts and activity.
    # The imported sugarcane_dataset table is separate and is not changed here.
    conn = connect_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'farmer',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS farms (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            farm_name TEXT NOT NULL,
            area_acres DOUBLE PRECISION NOT NULL,
            district TEXT,
            state TEXT DEFAULT 'Maharashtra',
            soil_type TEXT,
            soil_ph DOUBLE PRECISION,
            soil_moisture DOUBLE PRECISION,
            variety TEXT,
            growth_stage TEXT DEFAULT 'Mid',
            irrigation_method TEXT DEFAULT 'Drip',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS advisories (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            farm_id BIGINT REFERENCES farms(id) ON DELETE SET NULL,
            irrigation TEXT NOT NULL,
            disease_risk TEXT NOT NULL,
            predicted_yield DOUBLE PRECISION,
            details TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS recommendations (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            farm_id BIGINT REFERENCES farms(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('irrigation','fertilizer','alert')),
            severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN ('low','medium','high')),
            title TEXT NOT NULL,
            reason TEXT NOT NULL,
            product TEXT,
            volume_kl DOUBLE PRECISION,
            pump_hours DOUBLE PRECISION,
            deficit_pct DOUBLE PRECISION,
            quantity_kg DOUBLE PRECISION,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','dismissed')),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recs_user_status ON recommendations(user_id, status)")
        conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            advisory_id BIGINT REFERENCES advisories(id) ON DELETE SET NULL,
            rating INTEGER,
            comments TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute(
            "INSERT INTO users(name,email,password_hash,role) VALUES(%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING",
            ('Project Admin', 'admin@sugarcane.local', generate_password_hash('Admin@12345'), 'admin')
        )
        conn.commit()
    finally:
        conn.close()

init_db()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('user_id'):
            flash('Please sign in to continue.', 'info'); return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('Admin access is required for that page.', 'error'); return redirect(url_for('dashboard'))
        return fn(*args, **kwargs)
    return wrapper

def current_user():
    if not session.get('user_id'): return None
    return db().execute('SELECT * FROM users WHERE id=%s', (session['user_id'],)).fetchone()

@app.context_processor
def inject_user(): return {'current_user': current_user()}

@app.route('/')
def home():
    if session.get('user_id'): return redirect(url_for('dashboard'))
    return render_template('home.html', dataset_stats=cached_dataset_summary())

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        if not name or not email or len(password)<8:
            flash('Enter your name, a valid email, and a password of at least 8 characters.', 'error')
        else:
            try:
                cur=db().execute('INSERT INTO users(name,email,password_hash) VALUES(%s,%s,%s) RETURNING id',(name,email,generate_password_hash(password)))
                new_user_id=cur.fetchone()['id']
                db().commit(); session.update(user_id=new_user_id,name=name,role='farmer'); flash('Account created. Add your farm details to get started.','success'); return redirect(url_for('dashboard'))
            except psycopg.IntegrityError: flash('That email is already registered. Please sign in.','error')
    return render_template('auth.html', mode='register')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        user=db().execute('SELECT * FROM users WHERE email=%s',(email,)).fetchone()
        if user and check_password_hash(user['password_hash'],password):
            session.clear(); session.update(user_id=user['id'],name=user['name'],role=user['role']); return redirect(url_for('admin') if user['role']=='admin' else url_for('dashboard'))
        flash('Email or password is incorrect.','error')
    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    conn=db(); uid=session['user_id']
    farms=conn.execute('SELECT * FROM farms WHERE user_id=%s ORDER BY id DESC',(uid,)).fetchall()
    advisories=conn.execute('SELECT a.*, f.farm_name FROM advisories a LEFT JOIN farms f ON f.id=a.farm_id WHERE a.user_id=%s ORDER BY a.id DESC LIMIT 5',(uid,)).fetchall()
    latest={r['farm_id']:r for r in conn.execute('SELECT DISTINCT ON (farm_id) * FROM advisories WHERE user_id=%s AND farm_id IS NOT NULL ORDER BY farm_id, id DESC',(uid,)).fetchall()}
    pending_by_farm={r['farm_id']:r['n'] for r in conn.execute("SELECT farm_id, COUNT(*) n FROM recommendations WHERE user_id=%s AND status='pending' GROUP BY farm_id",(uid,)).fetchall()}
    priorities=conn.execute("""SELECT r.*, f.farm_name, EXTRACT(EPOCH FROM (NOW()-r.created_at))::bigint AS age_s
        FROM recommendations r LEFT JOIN farms f ON f.id=r.farm_id
        WHERE r.user_id=%s AND r.status='pending'
        ORDER BY CASE r.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, r.id DESC LIMIT 4""",(uid,)).fetchall()
    rc=conn.execute("""SELECT COUNT(*) FILTER (WHERE status='pending') AS pending,
        COUNT(*) FILTER (WHERE status='pending' AND severity='high') AS high,
        COUNT(*) FILTER (WHERE status='done' AND completed_at > NOW() - INTERVAL '30 days') AS done_30
        FROM recommendations WHERE user_id=%s""",(uid,)).fetchone()
    trend=list(reversed(conn.execute('SELECT predicted_yield, created_at FROM advisories WHERE user_id=%s AND predicted_yield IS NOT NULL ORDER BY id DESC LIMIT 10',(uid,)).fetchall()))
    risk_counts={r['disease_risk']:r['n'] for r in conn.execute('SELECT disease_risk, COUNT(*) n FROM advisories WHERE user_id=%s GROUP BY disease_risk',(uid,)).fetchall()}
    stages=['Early','Mid','Late']; cards=[]; total_acres=0.0; est_prod=0.0; yields=[]
    for f in farms:
        status,target,deficit=advisor.moisture_status(f['soil_moisture'],f['growth_stage'])
        ph=f['soil_ph']; ph_state=None if ph is None else ('acidic' if ph<5.5 else ('alkaline' if ph>8 else 'ok'))
        adv=latest.get(f['id']); total_acres+=f['area_acres'] or 0
        if adv and adv['predicted_yield'] is not None:
            est_prod+=adv['predicted_yield']*(f['area_acres'] or 0); yields.append(adv['predicted_yield'])
        cards.append({'farm':f,'status':status,'target':target,'deficit':deficit,
            'moisture_pct':min(100,max(0,(f['soil_moisture'] or 0)/45*100)),
            'band_lo':(target-3)/45*100,'band_w':6/45*100,
            'ph_state':ph_state,'ph_pct':None if ph is None else min(100,max(0,(ph-4)/6*100)),
            'stage_idx':stages.index(f['growth_stage']) if f['growth_stage'] in stages else 1,
            'pending':pending_by_farm.get(f['id'],0),'last':adv})
    stats={'farms':len(farms),'acres':round(total_acres,1),
           'advisories':conn.execute('SELECT COUNT(*) n FROM advisories WHERE user_id=%s',(uid,)).fetchone()['n'],
           'avg_yield':round(sum(yields)/len(yields),1) if yields else None,'est_prod':round(est_prod) if est_prod else None,
           'pending':rc['pending'],'high':rc['high'],'done_30':rc['done_30']}
    needs_attention=sum(1 for c in cards if c['status'] in ('low','wet') or c['ph_state'] in ('acidic','alkaline'))
    return render_template('dashboard.html',farms=farms,cards=cards,advisories=advisories,stats=stats,priorities=priorities,trend=trend,
        risk_counts=risk_counts,needs_attention=needs_attention,dataset_stats=cached_dataset_summary(),ago=advisor.time_ago)

@app.route('/dashboard-dataset.csv')
@login_required
def dashboard_dataset():
    return Response(
        DATA_PATH.read_bytes(),
        mimetype='text/csv'
    )
@app.route('/farms/new', methods=['GET','POST'])
@login_required
def farm_new():
    if request.method=='POST':
        try:
            area=float(request.form.get('area_acres','0'))
            ph=float(request.form['soil_ph']) if request.form.get('soil_ph') else None
            moisture=float(request.form['soil_moisture']) if request.form.get('soil_moisture') else None
            if area<=0: raise ValueError()
            new_farm=db().execute('''INSERT INTO farms(user_id,farm_name,area_acres,district,state,soil_type,soil_ph,soil_moisture,variety,growth_stage,irrigation_method)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',(session['user_id'],request.form['farm_name'].strip(),area,request.form.get('district','').strip(),request.form.get('state','Maharashtra'),request.form.get('soil_type','Loamy'),ph,moisture,request.form.get('variety','Co 86032'),request.form.get('growth_stage','Mid'),request.form.get('irrigation_method','Drip')))
            regenerate_recommendations(db(), session['user_id'], [new_farm.fetchone()['id']])
            db().commit(); flash('Farm saved. Recommendations for it are ready.','success'); return redirect(url_for('advisory'))
        except (ValueError, KeyError): flash('Please enter a farm name and a positive farm area.','error')
    return render_template('farm_form.html')

@app.route('/farms/<int:farm_id>/edit', methods=['GET', 'POST'])
@login_required
def farm_edit(farm_id):
    farm = db().execute('SELECT * FROM farms WHERE id=%s AND user_id=%s', (farm_id, session['user_id'])).fetchone()
    if not farm:
        flash('Farm not found.', 'error')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        try:
            name = request.form.get('farm_name', '').strip()
            area = float(request.form.get('area_acres', '0'))
            ph = float(request.form['soil_ph']) if request.form.get('soil_ph') else None
            moisture = float(request.form['soil_moisture']) if request.form.get('soil_moisture') else None
            if not name or area <= 0 or (ph is not None and not 0 <= ph <= 14) or (moisture is not None and not 0 <= moisture <= 100):
                raise ValueError()
            db().execute('''UPDATE farms SET farm_name=%s, area_acres=%s, district=%s, state=%s, soil_type=%s,
                soil_ph=%s, soil_moisture=%s, variety=%s, growth_stage=%s, irrigation_method=%s
                WHERE id=%s AND user_id=%s''', (name, area, request.form.get('district','').strip(), request.form.get('state','Maharashtra'),
                request.form.get('soil_type','Loamy'), ph, moisture, request.form.get('variety','').strip(),
                request.form.get('growth_stage','Mid'), request.form.get('irrigation_method','Drip'), farm_id, session['user_id']))
            regenerate_recommendations(db(), session['user_id'], [farm_id])
            db().commit()
            flash('Farm profile updated and recommendations re-checked.', 'success')
            return redirect(url_for('dashboard'))
        except (ValueError, KeyError):
            flash('Check the farm name, positive area, pH (0–14), and moisture (0–100%).', 'error')
    return render_template('farm_form.html', farm=farm, edit_mode=True)

@app.route('/farms/<int:farm_id>/delete', methods=['POST'])
@login_required
def farm_delete(farm_id):
    db().execute('DELETE FROM farms WHERE id=%s AND user_id=%s',(farm_id,session['user_id'])); db().commit(); flash('Farm removed.','info'); return redirect(url_for('dashboard'))

# Dataset-backed descriptive stats; predictions below are clearly labelled estimates.
def dataset_summary():
    try:
        import pandas as pd
        df=pd.read_csv(DATA_PATH)
        return {'rows':len(df),'columns':len(df.columns),'yield_avg':round(float(df['Yield_Quintal_per_Acre'].mean()),1),'diseases':int(df['Disease_Type'].notna().sum())}
    except Exception: return {'rows':0,'columns':0,'yield_avg':0,'diseases':0}

_dataset_cache={}
def cached_dataset_summary():
    if 'v' not in _dataset_cache: _dataset_cache['v']=dataset_summary()
    return _dataset_cache['v']

def safe_float(v, default):
    try:
        x=float(v)
        return x if x==x else default
    except (ValueError,TypeError): return default

def prediction_bundle(data):
    """Train lightweight models from the bundled CSV on demand; gracefully fall back to transparent heuristics."""
    moisture=safe_float(data.get('soil_moisture'),25)
    rainfall=safe_float(data.get('rainfall'),10)
    temp=safe_float(data.get('temperature'),28)
    humidity=safe_float(data.get('humidity'),65)
    ph=safe_float(data.get('soil_ph'),6.8)
    nitrogen=safe_float(data.get('nitrogen'),120)
    potassium=safe_float(data.get('potassium'),100)
    stage=data.get('growth_stage','Mid')
    # Advice is a rules-based first version, not live sensor data or agronomist-certified instructions.
    if moisture < 18 and rainfall < 15: irrigation='Irrigation may be needed soon'; irrigation_detail='Soil moisture is low and recent rainfall is limited. Check the root-zone moisture before irrigating.'; water='Prioritise a measured irrigation; avoid flooding.'
    elif moisture > 32 or rainfall > 80: irrigation='Delay and reassess irrigation'; irrigation_detail='Moisture or rainfall is already high. Check drainage and soil conditions before adding water.'; water='Avoid unnecessary irrigation and monitor for waterlogging.'
    else: irrigation='Monitor soil moisture'; irrigation_detail='Current inputs do not indicate an urgent irrigation trigger. Recheck moisture and local weather.'; water='Use a measured schedule suited to crop stage and soil.'
    risk='High' if humidity>=82 and temp>=28 else ('Moderate' if humidity>=68 or temp>=34 else 'Lower')
    warnings=[]
    if ph<5.5 or ph>8.0: warnings.append('Soil pH is outside a commonly used screening range; confirm with a soil test.')
    if nitrogen<80: warnings.append('Nitrogen input is relatively low compared with many dataset records; use a soil test and local nutrient guidance.')
    if potassium<70: warnings.append('Review potassium status using a soil test before deciding on fertilizer.')
    try:
        import pandas as pd
        from sklearn.compose import ColumnTransformer
        from sklearn.pipeline import make_pipeline
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import OneHotEncoder
        from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
        global _yield_model, _disease_model
        if '_yield_model' not in globals():
            df=pd.read_csv(DATA_PATH)
            numeric=['Temp_Avg_C','Rainfall_Total_mm','Humidity_%','Soil_pH','Soil_Moisture_%','Nitrogen_kg_per_acre','Phosphorus_kg_per_acre','Potassium_kg_per_acre','Cane_Height_cm','Brix_Value','Crop_Duration_Days']
            categorical=['Soil_Type','Growth_Stage','Irrigation_Method_Type','Season']
            features=numeric+categorical
            prep=ColumnTransformer([('num',SimpleImputer(strategy='median'),numeric),('cat',make_pipeline(SimpleImputer(strategy='most_frequent'),OneHotEncoder(handle_unknown='ignore')),categorical)])
            train=df.dropna(subset=['Yield_Quintal_per_Acre'])
            _yield_model=make_pipeline(prep,RandomForestRegressor(n_estimators=20,min_samples_leaf=5,random_state=42,n_jobs=1,max_depth=12))
            _yield_model.fit(train[features],train['Yield_Quintal_per_Acre'])
            dis=df.dropna(subset=['Disease_Type'])
            _disease_model=None
            if len(dis) and dis['Disease_Type'].nunique()>1:
                _disease_model=make_pipeline(prep,RandomForestClassifier(n_estimators=10,min_samples_leaf=5,random_state=42,n_jobs=1,max_depth=10,class_weight='balanced'))
                _disease_model.fit(dis[features],dis['Disease_Type'])
        row={'Temp_Avg_C':temp,'Rainfall_Total_mm':rainfall,'Humidity_%':humidity,'Soil_pH':ph,'Soil_Moisture_%':moisture,'Nitrogen_kg_per_acre':nitrogen,'Phosphorus_kg_per_acre':safe_float(data.get('phosphorus'),45),'Potassium_kg_per_acre':potassium,'Cane_Height_cm':safe_float(data.get('cane_height'),220),'Brix_Value':safe_float(data.get('brix'),19),'Crop_Duration_Days':safe_float(data.get('duration'),300),'Soil_Type':data.get('soil_type','Loamy'),'Growth_Stage':stage,'Irrigation_Method_Type':data.get('irrigation_method','Drip'),'Season':data.get('season','Kharif')}
        frame=pd.DataFrame([row]); predicted=float(_yield_model.predict(frame)[0]); disease=str(_disease_model.predict(frame)[0]) if _disease_model else 'Insufficient labelled data'
        return {'irrigation':irrigation,'irrigation_detail':irrigation_detail,'water':water,'risk':risk,'predicted_yield':round(max(0,predicted),1),'disease':disease,'warnings':warnings,'method':'Dataset-trained model + screening rules'}
    except Exception as e:
        print("PREDICTION ERROR:", repr(e), flush=True)
        return {'irrigation':irrigation,'irrigation_detail':irrigation_detail,'water':water,'risk':risk,'predicted_yield':None,'disease':'Model unavailable; inspect crop symptoms locally','warnings':warnings,'method':'Transparent rules-based fallback'}

@app.route('/advisory/check', methods=['GET','POST'])
@login_required
def field_check():
    farms=db().execute('SELECT * FROM farms WHERE user_id=%s ORDER BY farm_name',(session['user_id'],)).fetchall()
    result=None
    if request.method=='POST':
        form=dict(request.form)
        farm_id=form.get('farm_id') or None
        if farm_id:
            farm=db().execute('SELECT * FROM farms WHERE id=%s AND user_id=%s',(farm_id,session['user_id'])).fetchone()
            if not farm: farm_id=None
            else:
                for key,val in [('soil_type',farm['soil_type']),('soil_ph',farm['soil_ph']),('soil_moisture',farm['soil_moisture']),('growth_stage',farm['growth_stage']),('irrigation_method',farm['irrigation_method'])]:
                    if not form.get(key) and val is not None: form[key]=str(val)
        result=prediction_bundle(form)
        details=f"{result['irrigation_detail']} {result['water']} Disease screen: {result['risk']} risk; dataset model class: {result['disease']}. These are estimates, not a confirmed diagnosis or live weather forecast."
        saved = db().execute('INSERT INTO advisories(user_id,farm_id,irrigation,disease_risk,predicted_yield,details) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id,created_at', (session['user_id'],farm_id,result['irrigation'],result['risk'],result['predicted_yield'],details)).fetchone()
        db().commit()
        result['details']=details
        result['id']=saved['id']
        result['created_at']=saved['created_at']
        result['primary_action']=result['irrigation']
    preselect=request.args.get('farm', type=int)
    return render_template('field_check.html',farms=farms,result=result,preselect=preselect)

# ---------------------------------------------------------------- recommendations
def regenerate_recommendations(conn, user_id, farm_ids=None):
    """Rebuild the pending recommendations for the user's farms from current farm data."""
    q='SELECT * FROM farms WHERE user_id=%s'; params=[user_id]
    if farm_ids: q+=' AND id = ANY(%s)'; params.append(list(farm_ids))
    for farm in conn.execute(q,params).fetchall():
        last=conn.execute("SELECT * FROM advisories WHERE farm_id=%s AND created_at > NOW() - INTERVAL '7 days' ORDER BY id DESC LIMIT 1",(farm['id'],)).fetchone()
        recent=[('fertilizer',r['product']) for r in conn.execute("SELECT product FROM recommendations WHERE farm_id=%s AND kind='fertilizer' AND status IN ('done','dismissed') AND completed_at > NOW() - INTERVAL '21 days'",(farm['id'],)).fetchall()]
        conn.execute("DELETE FROM recommendations WHERE farm_id=%s AND status='pending'",(farm['id'],))
        for r in advisor.build_for_farm(farm,last,recent):
            conn.execute("""INSERT INTO recommendations(user_id,farm_id,kind,severity,title,reason,product,volume_kl,pump_hours,deficit_pct,quantity_kg)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(user_id,farm['id'],r['kind'],r['severity'],r['title'],r['reason'],r.get('product'),r.get('volume_kl'),r.get('pump_hours'),r.get('deficit_pct'),r.get('quantity_kg')))

def rec_counts(conn, uid):
    rows=conn.execute("SELECT kind, COUNT(*) n FROM recommendations WHERE user_id=%s AND status='pending' GROUP BY kind",(uid,)).fetchall()
    c={r['kind']:r['n'] for r in rows}; c['all']=sum(c.values()); return c

@app.route('/advisory')
@login_required
def advisory():
    conn=db(); uid=session['user_id']
    farms=conn.execute('SELECT id FROM farms WHERE user_id=%s',(uid,)).fetchall()
    ever=conn.execute('SELECT COUNT(*) n FROM recommendations WHERE user_id=%s',(uid,)).fetchone()['n']
    if farms and not ever:
        regenerate_recommendations(conn,uid); conn.commit()
    base="""SELECT r.*, f.farm_name, EXTRACT(EPOCH FROM (NOW()-COALESCE(r.completed_at,r.created_at)))::bigint AS age_s
            FROM recommendations r LEFT JOIN farms f ON f.id=r.farm_id WHERE r.user_id=%s AND r.status{}"""
    current=conn.execute(base.format("='pending' ORDER BY CASE r.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, r.id DESC"),(uid,)).fetchall()
    past=conn.execute(base.format(" IN ('done','dismissed') ORDER BY r.completed_at DESC NULLS LAST LIMIT 12"),(uid,)).fetchall()
    show=request.args.get('show','all'); show=show if show in ('all','irrigation','fertilizer','alert') else 'all'
    return render_template('advisory.html',current=current,past=past,counts=rec_counts(conn,uid),show=show,has_farms=bool(farms),ago=advisor.time_ago)

@app.route('/recommendations/recheck', methods=['POST'])
@login_required
def recommendations_recheck():
    regenerate_recommendations(db(),session['user_id']); db().commit()
    flash('All fields re-checked against their latest profile data.','success')
    return redirect(url_for('advisory'))

@app.route('/recommendations/<int:rec_id>/<action>', methods=['POST'])
@login_required
def recommendation_action(rec_id, action):
    if action not in ('done','dismissed','reopen'): abort(400)
    conn=db(); uid=session['user_id']
    rec=conn.execute('SELECT * FROM recommendations WHERE id=%s AND user_id=%s',(rec_id,uid)).fetchone()
    if not rec: abort(404)
    note=''
    if action=='reopen':
        conn.execute("UPDATE recommendations SET status='pending', completed_at=NULL WHERE id=%s",(rec_id,))
    else:
        conn.execute('UPDATE recommendations SET status=%s, completed_at=CURRENT_TIMESTAMP WHERE id=%s',(action,rec_id))
        if action=='done' and rec['kind']=='irrigation' and rec['farm_id']:
            farm=conn.execute('SELECT growth_stage FROM farms WHERE id=%s',(rec['farm_id'],)).fetchone()
            target=advisor.moisture_target(farm['growth_stage'] if farm else 'Mid')
            conn.execute('UPDATE farms SET soil_moisture=%s WHERE id=%s',(target,rec['farm_id']))
            note=f'Farm soil moisture set to the {target:g}% target.'
    conn.commit()
    if request.headers.get('X-Requested-With')=='fetch':
        return jsonify(ok=True,counts=rec_counts(conn,uid),note=note)
    if note: flash(note,'info')
    return redirect(url_for('advisory'))

@app.route('/history')
@login_required
def history():
    q = request.args.get('q', '').strip()
    if q:
        rows=db().execute("SELECT a.*,f.farm_name FROM advisories a LEFT JOIN farms f ON f.id=a.farm_id WHERE a.user_id=%s AND (COALESCE(f.farm_name, 'General check') ILIKE %s OR a.irrigation ILIKE %s OR a.disease_risk ILIKE %s OR a.details ILIKE %s) ORDER BY a.id DESC", (session['user_id'], f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
    else:
        rows=db().execute('SELECT a.*,f.farm_name FROM advisories a LEFT JOIN farms f ON f.id=a.farm_id WHERE a.user_id=%s ORDER BY a.id DESC',(session['user_id'],)).fetchall()
    return render_template('history.html',rows=rows, search_query=q)

@app.route('/history/export.csv')
@login_required
def history_export():
    rows = db().execute("SELECT a.id, a.created_at, COALESCE(f.farm_name, 'General check') AS farm_name, a.irrigation, a.disease_risk, a.predicted_yield, a.details FROM advisories a LEFT JOIN farms f ON f.id=a.farm_id WHERE a.user_id=%s ORDER BY a.id DESC", (session['user_id'],)).fetchall()
    import io
    from csv import writer
    output = io.StringIO()
    csv_writer = writer(output)
    csv_writer.writerow(['Advisory ID', 'Created at', 'Farm', 'Irrigation screen', 'Disease screening risk', 'Estimated yield (quintals/acre)', 'Details'])
    for row in rows:
        csv_writer.writerow([row['id'], row['created_at'], row['farm_name'], row['irrigation'], row['disease_risk'], row['predicted_yield'], row['details']])
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=canefield-advisory-history.csv'})

@app.route('/feedback',methods=['POST'])
@login_required
def send_feedback():
    rating=safe_float(request.form.get('rating'),5); comments=request.form.get('comments','').strip()
    advisory_id=request.form.get('advisory_id') or None
    if advisory_id:
        owned=db().execute('SELECT id FROM advisories WHERE id=%s AND user_id=%s',(advisory_id,session['user_id'])).fetchone()
        if not owned: advisory_id=None
    db().execute('INSERT INTO feedback(user_id,advisory_id,rating,comments) VALUES(%s,%s,%s,%s)',(session['user_id'],advisory_id,int(max(1,min(5,rating))),comments[:2000])); db().commit(); flash('Thanks—your feedback was saved.','success'); return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for('dashboard'))

@app.route('/admin')
@login_required
@admin_required
def admin():
    conn=db(); users=conn.execute("SELECT id,name,email,role,created_at FROM users ORDER BY id DESC").fetchall()
    farms=conn.execute('SELECT f.*,u.name AS farmer_name FROM farms f JOIN users u ON u.id=f.user_id ORDER BY f.id DESC LIMIT 50').fetchall()
    recent=conn.execute('SELECT a.*,u.name AS farmer_name,f.farm_name FROM advisories a JOIN users u ON u.id=a.user_id LEFT JOIN farms f ON f.id=a.farm_id ORDER BY a.id DESC LIMIT 30').fetchall()
    feedback=conn.execute('SELECT fb.*,u.name AS farmer_name,a.id AS advisory_number FROM feedback fb JOIN users u ON u.id=fb.user_id LEFT JOIN advisories a ON a.id=fb.advisory_id ORDER BY fb.id DESC LIMIT 100').fetchall()
    rating_stats=conn.execute('SELECT AVG(rating) AS average_rating, COUNT(rating) AS rating_count FROM feedback WHERE rating IS NOT NULL').fetchone()
    counts={'users':conn.execute('SELECT COUNT(*) n FROM users').fetchone()['n'],'farms':conn.execute('SELECT COUNT(*) n FROM farms').fetchone()['n'],'advisories':conn.execute('SELECT COUNT(*) n FROM advisories').fetchone()['n']}
    return render_template('admin.html',users=users,farms=farms,recent=recent,feedback=feedback,rating_stats=rating_stats,counts=counts,dataset_stats=dataset_summary())

@app.route('/health')
def health(): return {'status':'ok','service':'Sugarcane Advisory','dataset_present':DATA_PATH.exists()}

if __name__=='__main__':
    app.run(host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT',5000)), debug=os.environ.get('FLASK_DEBUG') == '1', use_reloader=False)
