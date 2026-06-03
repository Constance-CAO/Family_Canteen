import os
import json
import re
from datetime import datetime, timedelta, date
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from flask_sqlalchemy import SQLAlchemy
from PIL import Image
import pytz

app = Flask(__name__)
app.config['SECRET_KEY'] = 'family-canteen-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///canteen.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['ORDERS_FOLDER'] = 'orders_txt'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['ORDERS_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)

class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    parent_id = db.Column(db.Integer, default=0)
    sort_order = db.Column(db.Integer, default=0)

class Dish(db.Model):
    __tablename__ = 'dishes'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'))
    ingredients = db.Column(db.Text)
    bento_compatible = db.Column(db.Boolean, default=True)
    preprocess_days = db.Column(db.Integer, default=0)
    cook_minutes = db.Column(db.Integer, default=40)
    image_url = db.Column(db.String(300))
    extra_options = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(80), nullable=False)
    week_monday = db.Column(db.Date, nullable=False)
    order_data = db.Column(db.Text)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    txt_filename = db.Column(db.String(200))

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_uploaded_image(file, crop_data=None):
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    new_filename = f"{timestamp}.jpg"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)
    img = Image.open(file)
    if crop_data:
        img = img.crop((crop_data['x'], crop_data['y'],
                        crop_data['x']+crop_data['width'],
                        crop_data['y']+crop_data['height']))
    img.thumbnail((300, 300), Image.LANCZOS)
    if img.mode in ('RGBA', 'P', 'LA'):
        img = img.convert('RGB')
    img.save(filepath, 'JPEG', optimize=True, quality=85)
    return f"static/uploads/{new_filename}"

def get_syd_time():
    tz = pytz.timezone('Australia/Sydney')
    return datetime.now(tz)

def is_order_editable(week_monday_date):
    syd_now = get_syd_time()
    deadline = datetime.combine(week_monday_date - timedelta(days=1), datetime.min.time())
    deadline = deadline.replace(hour=10, minute=0, second=0)
    deadline = pytz.timezone('Australia/Sydney').localize(deadline)
    return syd_now < deadline

def _zh_date(d):
    return f"{d.year}年{d.month}月{d.day}日"

def _zh_datetime(dt):
    return f"{dt.year}年{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"

def generate_txt(order_record):
    data = json.loads(order_record.order_data)
    week_monday = order_record.week_monday
    week_saturday = week_monday + timedelta(days=5)
    submitted_local = order_record.submitted_at
    if submitted_local.tzinfo is None:
        submitted_local = pytz.utc.localize(submitted_local).astimezone(pytz.timezone('Australia/Sydney'))
    else:
        submitted_local = submitted_local.astimezone(pytz.timezone('Australia/Sydney'))
    lines = []
    lines.append("="*40)
    lines.append("熊家食堂 订单详情")
    lines.append(f"订餐人：{order_record.customer_name}")
    lines.append(f"点菜周：{_zh_date(week_monday)} 至 {_zh_date(week_saturday)}")
    lines.append(f"提交时间：{_zh_datetime(submitted_local)}")
    lines.append("="*40)
    lines.append("")
    for day_data in data['days']:
        day_name = day_data['day_name']
        day_date = datetime.strptime(day_data['date'], '%Y-%m-%d').date()
        mode = day_data['mode']
        lines.append(f"【{day_name} {_zh_date(day_date)}】 {mode}")
        if mode == '爱心便当':
            if day_data.get('dish1'):
                lines.append(f"  菜品1：{day_data['dish1']}")
            if day_data.get('dish2'):
                lines.append(f"  菜品2：{day_data['dish2']}")
            if day_data.get('staple'):
                lines.append(f"  主食：{day_data['staple']}")
        elif mode == '熊山洞开小灶':
            items = []
            if day_data.get('dish1'): items.append(day_data['dish1'])
            if day_data.get('dish2'): items.append(day_data['dish2'])
            if day_data.get('extra1'): items.append(day_data['extra1'])
            if day_data.get('extra2'): items.append(day_data['extra2'])
            if day_data.get('extra3'): items.append(day_data['extra3'])
            for idx, item in enumerate(items, 1):
                lines.append(f"  菜品{idx}：{item}")
            if day_data.get('staple'):
                lines.append(f"  主食：{day_data['staple']}")
        elif mode in ['出去觅食', '特殊事件']:
            pass
        lines.append("")
    txt_filename = f"{order_record.customer_name}_{week_monday.strftime('%Y%m%d')}一周点单.txt"
    txt_path = os.path.join(app.config['ORDERS_FOLDER'], txt_filename)
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    return txt_path

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/order')
def order_page():
    return render_template('order.html')

@app.route('/api/dishes', methods=['GET'])
def get_dishes():
    dishes = Dish.query.all()
    categories = Category.query.order_by(Category.sort_order).all()
    cat_dict = {c.id: {'id': c.id, 'name': c.name, 'parent_id': c.parent_id, 'children': []} for c in categories}
    root_cats = []
    for c in categories:
        if c.parent_id == 0:
            root_cats.append(cat_dict[c.id])
        else:
            if c.parent_id in cat_dict:
                cat_dict[c.parent_id]['children'].append(cat_dict[c.id])
    dish_list = []
    for d in dishes:
        dish_list.append({
            'id': d.id,
            'name': d.name,
            'category_id': d.category_id,
            'ingredients': d.ingredients or '',
            'bento_compatible': d.bento_compatible,
            'preprocess_days': d.preprocess_days,
            'cook_minutes': d.cook_minutes,
            'image_url': d.image_url or '/static/img/placeholder.png',
            'extra_options': json.loads(d.extra_options) if d.extra_options else []
        })
    return jsonify({'success': True, 'dishes': dish_list, 'categories': root_cats})

@app.route('/api/order/check_existing', methods=['POST'])
def check_existing_order():
    data = request.json
    customer_name = data['customer_name']
    week_monday_str = data['week_monday']
    week_monday = datetime.strptime(week_monday_str, '%Y-%m-%d').date()
    editable = is_order_editable(week_monday)
    existing_order = Order.query.filter_by(customer_name=customer_name, week_monday=week_monday).first()
    if existing_order:
        return jsonify({'exists': True, 'editable': editable, 'order_data': existing_order.order_data})
    return jsonify({'exists': False, 'editable': editable})

@app.route('/api/order/submit', methods=['POST'])
def submit_order():
    data = request.json
    customer_name = data['customer_name']
    week_monday_str = data['week_monday']
    week_monday = datetime.strptime(week_monday_str, '%Y-%m-%d').date()
    order_data = data['order_data']
    if not is_order_editable(week_monday):
        return jsonify({'success': False, 'error': '修改时间已过（周日上午10点截止），无法提交订单'})
    existing = Order.query.filter_by(customer_name=customer_name, week_monday=week_monday).first()
    if existing:
        existing.order_data = json.dumps(order_data)
        existing.submitted_at = get_syd_time()
        db.session.commit()
        order_record = existing
    else:
        new_order = Order(
            customer_name=customer_name,
            week_monday=week_monday,
            order_data=json.dumps(order_data),
            submitted_at=get_syd_time()
        )
        db.session.add(new_order)
        db.session.commit()
        order_record = new_order
    txt_path = generate_txt(order_record)
    order_record.txt_filename = os.path.basename(txt_path)
    db.session.commit()
    return jsonify({'success': True, 'txt_filename': order_record.txt_filename})

@app.route('/api/order/history', methods=['POST'])
def get_history():
    data = request.json
    customer_name = data['customer_name']
    orders = Order.query.filter_by(customer_name=customer_name).order_by(Order.week_monday.desc()).all()
    history = []
    for o in orders:
        txt_content = ""
        if o.txt_filename:
            txt_path = os.path.join(app.config['ORDERS_FOLDER'], o.txt_filename)
            if os.path.exists(txt_path):
                with open(txt_path, 'r', encoding='utf-8') as f:
                    txt_content = f.read()
        history.append({
            'week_monday': o.week_monday.strftime('%Y-%m-%d'),
            'txt_content': txt_content
        })
    return jsonify({'success': True, 'history': history})

# 管理员密码哈希存储
_admin_password_hash = generate_password_hash('Vantage2020@')

def get_admin_hash():
    return _admin_password_hash

def set_admin_hash(new_hash):
    global _admin_password_hash
    _admin_password_hash = new_hash

# 安全问题及答案
SECURITY_QUESTIONS = [
    {"question": "你的第一辆车是什么牌子？", "answer": "大众"},
    {"question": "母亲的生日YYYYMMDD？", "answer": "19550524"},
    {"question": "你的第一个自建网站是哪个游戏的攻略和周边？此游戏是哪个公司出品？答案所需格式：游戏名 公司名", "answer": "心跳回忆 科乐美"}
]

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'login':
            password = request.form.get('password')
            if check_password_hash(get_admin_hash(), password):
                session['admin'] = True
                return redirect(url_for('admin_dashboard'))
            else:
                return render_template('admin_login.html', error='密码错误')
        elif action == 'forgot':
            answers = []
            for i in range(3):
                answers.append(request.form.get(f'answer_{i}'))
            correct = True
            for i, q in enumerate(SECURITY_QUESTIONS):
                if answers[i].strip() != q['answer']:
                    correct = False
                    break
            if correct:
                new_password = request.form.get('new_password')
                if new_password:
                    set_admin_hash(generate_password_hash(new_password))
                    session['admin'] = True
                    return redirect(url_for('admin_dashboard'))
                else:
                    return render_template('admin_login.html', reset_mode=True, questions=SECURITY_QUESTIONS, error='请输入新密码')
            else:
                return render_template('admin_login.html', reset_mode=True, questions=SECURITY_QUESTIONS, error='答案错误')
    return render_template('admin_login.html')
@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('index'))

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    return render_template('admin_dashboard.html')

@app.route('/admin/orders')
@admin_required
def admin_orders():
    orders = Order.query.order_by(Order.submitted_at.desc()).all()
    return render_template('admin_orders.html', orders=orders)

@app.route('/admin/download_txt/<filename>')
@admin_required
def download_txt(filename):
    return send_file(os.path.join(app.config['ORDERS_FOLDER'], filename), as_attachment=True)

@app.route('/admin/dishes')
@admin_required
def admin_dishes():
    dishes = Dish.query.all()
    categories = Category.query.all()
    return render_template('admin_dishes.html', dishes=dishes, categories=categories)

@app.route('/admin/dish/<int:dish_id>', methods=['GET', 'POST'])
@admin_required
def edit_dish(dish_id):
    dish = Dish.query.get_or_404(dish_id)
    if request.method == 'POST':
        dish.name = request.form['name']
        dish.ingredients = request.form.get('ingredients', '')
        dish.category_id = int(request.form['category_id'])
        dish.bento_compatible = 'bento_compatible' in request.form
        dish.preprocess_days = int(request.form.get('preprocess_days', 0))
        dish.cook_minutes = int(request.form.get('cook_minutes', 40))
        extra_options = []
        opt_names = request.form.getlist('opt_name[]')
        opt_types = request.form.getlist('opt_type[]')
        opt_options = request.form.getlist('opt_options[]')
        for i in range(len(opt_names)):
            if opt_names[i].strip():
                opts = [o.strip() for o in opt_options[i].split(',') if o.strip()]
                extra_options.append({'name': opt_names[i], 'type': opt_types[i], 'options': opts})
        dish.extra_options = json.dumps(extra_options)
        if 'image' in request.files and request.files['image'].filename:
            file = request.files['image']
            crop_data = None
            if request.form.get('crop_x'):
                crop_data = {
                    'x': int(float(request.form['crop_x'])),
                    'y': int(float(request.form['crop_y'])),
                    'width': int(float(request.form['crop_width'])),
                    'height': int(float(request.form['crop_height']))
                }
            new_url = save_uploaded_image(file, crop_data)
            if dish.image_url and os.path.exists(dish.image_url):
                os.remove(dish.image_url)
            dish.image_url = new_url
        db.session.commit()
        return redirect(url_for('admin_dishes'))
    categories = Category.query.order_by(Category.sort_order).all()
    return render_template('dish_form.html', dish=dish, categories=categories)

@app.route('/admin/dish/new', methods=['GET', 'POST'])
@admin_required
def new_dish():
    if request.method == 'POST':
        dish = Dish(
            name=request.form['name'],
            ingredients=request.form.get('ingredients', ''),
            category_id=int(request.form['category_id']),
            bento_compatible='bento_compatible' in request.form,
            preprocess_days=int(request.form.get('preprocess_days', 0)),
            cook_minutes=int(request.form.get('cook_minutes', 40)),
            extra_options='[]'
        )
        extra_options = []
        opt_names = request.form.getlist('opt_name[]')
        opt_types = request.form.getlist('opt_type[]')
        opt_options = request.form.getlist('opt_options[]')
        for i in range(len(opt_names)):
            if opt_names[i].strip():
                opts = [o.strip() for o in opt_options[i].split(',') if o.strip()]
                extra_options.append({'name': opt_names[i], 'type': opt_types[i], 'options': opts})
        dish.extra_options = json.dumps(extra_options)
        if 'image' in request.files and request.files['image'].filename:
            file = request.files['image']
            crop_data = None
            if request.form.get('crop_x'):
                crop_data = {
                    'x': int(float(request.form['crop_x'])),
                    'y': int(float(request.form['crop_y'])),
                    'width': int(float(request.form['crop_width'])),
                    'height': int(float(request.form['crop_height']))
                }
            dish.image_url = save_uploaded_image(file, crop_data)
        db.session.add(dish)
        db.session.commit()
        return redirect(url_for('admin_dishes'))
    categories = Category.query.order_by(Category.sort_order).all()
    return render_template('dish_form.html', dish=None, categories=categories)

@app.route('/admin/dish/delete/<int:dish_id>')
@admin_required
def delete_dish(dish_id):
    dish = Dish.query.get_or_404(dish_id)
    if dish.image_url and os.path.exists(dish.image_url):
        os.remove(dish.image_url)
    db.session.delete(dish)
    db.session.commit()
    return redirect(url_for('admin_dishes'))

@app.route('/admin/categories')
@admin_required
def admin_categories():
    categories = Category.query.order_by(Category.sort_order).all()
    return render_template('admin_categories.html', categories=categories)

@app.route('/admin/category/add', methods=['POST'])
@admin_required
def add_category():
    name = request.form['name']
    parent_id = int(request.form.get('parent_id', 0))
    max_order = db.session.query(db.func.max(Category.sort_order)).filter_by(parent_id=parent_id).scalar() or 0
    new_cat = Category(name=name, parent_id=parent_id, sort_order=max_order + 1)
    db.session.add(new_cat)
    db.session.commit()
    return redirect(url_for('admin_categories'))

@app.route('/admin/category/edit/<int:cat_id>', methods=['POST'])
@admin_required
def edit_category(cat_id):
    cat = Category.query.get_or_404(cat_id)
    cat.name = request.form['name']
    db.session.commit()
    return redirect(url_for('admin_categories'))

@app.route('/admin/category/delete/<int:cat_id>')
@admin_required
def delete_category(cat_id):
    cat = Category.query.get_or_404(cat_id)
    children = Category.query.filter_by(parent_id=cat_id).count()
    dishes = Dish.query.filter_by(category_id=cat_id).count()
    if children > 0 or dishes > 0:
        return "请先删除子分类或菜品", 400
    db.session.delete(cat)
    db.session.commit()
    return redirect(url_for('admin_categories'))

@app.route('/admin/category/move', methods=['POST'])
@admin_required
def move_category():
    cat_id = int(request.form['cat_id'])
    direction = request.form['direction']
    cat = Category.query.get_or_404(cat_id)
    siblings = Category.query.filter_by(parent_id=cat.parent_id).order_by(Category.sort_order, Category.id).all()
    # 重新分配稳定递增的 sort_order，避免初始全 0 时交换无效
    for i, s in enumerate(siblings):
        s.sort_order = i + 1
    idx = next((i for i, c in enumerate(siblings) if c.id == cat_id), None)
    if idx is not None:
        if direction == 'up' and idx > 0:
            siblings[idx].sort_order, siblings[idx-1].sort_order = siblings[idx-1].sort_order, siblings[idx].sort_order
        elif direction == 'down' and idx < len(siblings)-1:
            siblings[idx].sort_order, siblings[idx+1].sort_order = siblings[idx+1].sort_order, siblings[idx].sort_order
    db.session.commit()
    return redirect(url_for('admin_categories'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)