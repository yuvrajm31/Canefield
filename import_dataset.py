import os, secrets, csv
import psycopg
from psycopg.rows import dict_row
from functools import wraps
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

BASE = Path(__file__).resolve().parent
DB_PATH = os.environ.get('DATABASE_PATH', str(BASE / 'sugarcane.db'))
DATA_PATH = BASE / 'data' / 'sugarcane_dataset.csv'
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-before-deployment-' + secrets.token_hex(8))



def db():
    if 'db' not in g:
        g.db = psycopg.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            port=os.environ.get('DB_PORT', '5432'),
            dbname=os.environ.get('DB_NAME', 'sugarcane_advisory'),
            user=os.environ.get('DB_USER', 'postgres'),
            password=os.environ.get('DB_PASSWORD') or input(
                'Enter PostgreSQL password: '
            ),
            row_factory=dict_row
        )
    return g.db

@app.teardown_appcontext
def close_db(_error=None):
    conn = g.pop('db', None)
    if conn is not None: conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'farmer', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS farms (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      farm_name TEXT NOT NULL, area_acres REAL NOT NULL, district TEXT, state TEXT DEFAULT 'Maharashtra',
      soil_type TEXT, soil_ph REAL, soil_moisture REAL, variety TEXT, growth_stage TEXT DEFAULT 'Mid',
      irrigation_method TEXT DEFAULT 'Drip', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS advisories (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      farm_id INTEGER REFERENCES farms(id) ON DELETE SET NULL, irrigation TEXT NOT NULL, disease_risk TEXT NOT NULL,
      predicted_yield REAL, details TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS feedback (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      advisory_id INTEGER REFERENCES advisories(id) ON DELETE SET NULL, rating INTEGER, comments TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    ''')
    # Local demo admin. Change the password and SECRET_KEY before any public deployment.
    row = conn.execute('SELECT id FROM users WHERE email=?', ('admin@sugarcane.local',)).fetchone()
    if not row:
        conn.execute('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',
          ('Project Admin','admin@sugarcane.local',generate_password_hash('Admin@12345'),'admin'))
    conn.commit(); conn.close()

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
    return db().execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()

@app.context_processor
def inject_user(): return {'current_user': current_user()}

@app.route('/')
def home():
    if session.get('user_id'): return redirect(url_for('dashboard'))
    return render_template('home.html')

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        if not name or not email or len(password)<8:
            flash('Enter your name, a valid email, and a password of at least 8 characters.', 'error')
        else:
            try:
                cur=db().execute('INSERT INTO users(name,email,password_hash) VALUES(?,?,?)',(name,email,generate_password_hash(password)))
                db().commit(); session.update(user_id=cur.lastrowid,name=name,role='farmer'); flash('Account created. Add your farm details to get started.','success'); return redirect(url_for('dashboard'))
            except sqlite3.IntegrityError: flash('That email is already registered. Please sign in.','error')
    return render_template('auth.html', mode='register')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        user=db().execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        if user and check_password_hash(user['password_hash'],password):
            session.clear(); session.update(user_id=user['id'],name=user['name'],role=user['role']); return redirect(url_for('admin') if user['role']=='admin' else url_for('dashboard'))
        flash('Email or password is incorrect.','error')
    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    conn=db(); farms=conn.execute('SELECT * FROM farms WHERE user_id=? ORDER BY id DESC',(session['user_id'],)).fetchall()
    advisories=conn.execute('SELECT a.*, f.farm_name FROM advisories a LEFT JOIN farms f ON f.id=a.farm_id WHERE a.user_id=? ORDER BY a.id DESC LIMIT 5',(session['user_id'],)).fetchall()
    stats={'farms':len(farms),'advisories':conn.execute('SELECT COUNT(*) n FROM advisories WHERE user_id=?',(session['user_id'],)).fetchone()['n']}
    dataset_stats=dataset_summary()
    return render_template('dashboard.html',farms=farms,advisories=advisories,stats=stats,dataset_stats=dataset_stats)

@app.route('/farms/new', methods=['GET','POST'])
@login_required
def farm_new():
    if request.method=='POST':
        try:
            area=float(request.form.get('area_acres','0'))
            ph=float(request.form['soil_ph']) if request.form.get('soil_ph') else None
            moisture=float(request.form['soil_moisture']) if request.form.get('soil_moisture') else None
            if area<=0: raise ValueError()
            db().execute('''INSERT INTO farms(user_id,farm_name,area_acres,district,state,soil_type,soil_ph,soil_moisture,variety,growth_stage,irrigation_method)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(session['user_id'],request.form['farm_name'].strip(),area,request.form.get('district','').strip(),request.form.get('state','Maharashtra'),request.form.get('soil_type','Loamy'),ph,moisture,request.form.get('variety','Co 86032'),request.form.get('growth_stage','Mid'),request.form.get('irrigation_method','Drip')))
            db().commit(); flash('Farm saved successfully.','success'); return redirect(url_for('dashboard'))
        except (ValueError, KeyError): flash('Please enter a farm name and a positive farm area.','error')
    return render_template('farm_form.html')

@app.route('/farms/<int:farm_id>/delete', methods=['POST'])
@login_required
def farm_delete(farm_id):
    db().execute('DELETE FROM farms WHERE id=? AND user_id=?',(farm_id,session['user_id'])); db().commit(); flash('Farm removed.','info'); return redirect(url_for('dashboard'))

# Dataset-backed descriptive stats; predictions below are clearly labelled estimates.
def dataset_summary():
    try:
        import pandas as pd
        df=pd.read_csv(DATA_PATH)
        return {'rows':len(df),'columns':len(df.columns),'yield_avg':round(float(df['Yield_Quintal_per_Acre'].mean()),1),'diseases':int(df['Disease_Type'].notna().sum())}
    except Exception: return {'rows':0,'columns':0,'yield_avg':0,'diseases':0}

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
            _yield_model=make_pipeline(prep,RandomForestRegressor(n_estimators=80,min_samples_leaf=3,random_state=42,n_jobs=-1))
            _yield_model.fit(train[features],train['Yield_Quintal_per_Acre'])
            dis=df.dropna(subset=['Disease_Type'])
            _disease_model=None
            if len(dis) and dis['Disease_Type'].nunique()>1:
                _disease_model=make_pipeline(prep,RandomForestClassifier(n_estimators=60,min_samples_leaf=3,random_state=42,n_jobs=-1,class_weight='balanced'))
                _disease_model.fit(dis[features],dis['Disease_Type'])
        row={'Temp_Avg_C':temp,'Rainfall_Total_mm':rainfall,'Humidity_%':humidity,'Soil_pH':ph,'Soil_Moisture_%':moisture,'Nitrogen_kg_per_acre':nitrogen,'Phosphorus_kg_per_acre':safe_float(data.get('phosphorus'),45),'Potassium_kg_per_acre':potassium,'Cane_Height_cm':safe_float(data.get('cane_height'),220),'Brix_Value':safe_float(data.get('brix'),19),'Crop_Duration_Days':safe_float(data.get('duration'),300),'Soil_Type':data.get('soil_type','Loamy'),'Growth_Stage':stage,'Irrigation_Method_Type':data.get('irrigation_method','Drip'),'Season':data.get('season','Kharif')}
        frame=pd.DataFrame([row]); predicted=float(_yield_model.predict(frame)[0]); disease=str(_disease_model.predict(frame)[0]) if _disease_model else 'Insufficient labelled data'
        return {'irrigation':irrigation,'irrigation_detail':irrigation_detail,'water':water,'risk':risk,'predicted_yield':round(max(0,predicted),1),'disease':disease,'warnings':warnings,'method':'Dataset-trained model + screening rules'}
    except Exception as e:
        return {'irrigation':irrigation,'irrigation_detail':irrigation_detail,'water':water,'risk':risk,'predicted_yield':None,'disease':'Model unavailable; inspect crop symptoms locally','warnings':warnings,'method':'Transparent rules-based fallback'}

@app.route('/advisory', methods=['GET','POST'])
@login_required
def advisory():
    farms=db().execute('SELECT * FROM farms WHERE user_id=? ORDER BY farm_name',(session['user_id'],)).fetchall()
    result=None
    if request.method=='POST':
        form=dict(request.form)
        farm_id=form.get('farm_id') or None
        if farm_id:
            farm=db().execute('SELECT * FROM farms WHERE id=? AND user_id=?',(farm_id,session['user_id'])).fetchone()
            if not farm: farm_id=None
            else:
                for key,val in [('soil_type',farm['soil_type']),('soil_ph',farm['soil_ph']),('soil_moisture',farm['soil_moisture']),('growth_stage',farm['growth_stage']),('irrigation_method',farm['irrigation_method'])]:
                    if not form.get(key) and val is not None: form[key]=str(val)
        result=prediction_bundle(form)
        details=f"{result['irrigation_detail']} {result['water']} Disease screen: {result['risk']} risk; dataset model class: {result['disease']}. These are estimates, not a confirmed diagnosis or live weather forecast."
        db().execute('INSERT INTO advisories(user_id,farm_id,irrigation,disease_risk,predicted_yield,details) VALUES(?,?,?,?,?,?)',(session['user_id'],farm_id,result['irrigation'],result['risk'],result['predicted_yield'],details)); db().commit()
        result['details']=details
    return render_template('advisory.html',farms=farms,result=result)

@app.route('/history')
@login_required
def history():
    rows=db().execute('SELECT a.*,f.farm_name FROM advisories a LEFT JOIN farms f ON f.id=a.farm_id WHERE a.user_id=? ORDER BY a.id DESC',(session['user_id'],)).fetchall()
    return render_template('history.html',rows=rows)

@app.route('/feedback',methods=['POST'])
@login_required
def send_feedback():
    rating=safe_float(request.form.get('rating'),5); comments=request.form.get('comments','').strip()
    db().execute('INSERT INTO feedback(user_id,rating,comments) VALUES(?,?,?)',(session['user_id'],int(max(1,min(5,rating))),comments)); db().commit(); flash('Thanks—your feedback was saved.','success'); return redirect(url_for('dashboard'))

@app.route('/admin')
@login_required
@admin_required
def admin():
    conn=db(); users=conn.execute("SELECT id,name,email,role,created_at FROM users ORDER BY id DESC").fetchall()
    farms=conn.execute('SELECT f.*,u.name AS farmer_name FROM farms f JOIN users u ON u.id=f.user_id ORDER BY f.id DESC LIMIT 50').fetchall()
    recent=conn.execute('SELECT a.*,u.name AS farmer_name,f.farm_name FROM advisories a JOIN users u ON u.id=a.user_id LEFT JOIN farms f ON f.id=a.farm_id ORDER BY a.id DESC LIMIT 30').fetchall()
    counts={'users':conn.execute('SELECT COUNT(*) n FROM users').fetchone()['n'],'farms':conn.execute('SELECT COUNT(*) n FROM farms').fetchone()['n'],'advisories':conn.execute('SELECT COUNT(*) n FROM advisories').fetchone()['n']}
    return render_template('admin.html',users=users,farms=farms,recent=recent,counts=counts,dataset_stats=dataset_summary())

@app.route('/health')
def health(): return {'status':'ok','service':'Sugarcane Advisory','dataset_present':DATA_PATH.exists()}

if __name__=='__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT',5000)), debug=True)
