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

# ─── 应用初始化 ───────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = 'family-canteen-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///canteen.db'   # SQLite 文件存在 instance/ 目录
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'     # 菜品图片上传目录（需在项目根目录运行）
app.config['ORDERS_FOLDER'] = 'orders_txt'          # 可打印的中文订单 TXT 存放目录
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 上传图片限制 5 MB
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# 启动时确保目录存在，避免首次运行报错
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['ORDERS_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)


# ─── 数据模型 ─────────────────────────────────────────────────

class Category(db.Model):
    """菜品分类，支持两级树形结构（大分类 → 子分类）。
    约定：parent_id=0 表示顶级分类（不用 NULL，避免 NULL 比较歧义）。
    排序通过 sort_order 字段手动维护，越小越靠前。
    """
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    parent_id = db.Column(db.Integer, default=0)   # 0 = 顶级，否则为父分类 id
    sort_order = db.Column(db.Integer, default=0)


class Dish(db.Model):
    """菜品信息。
    extra_options: JSON 字符串，格式为 [{name, type:'radio'|'checkbox', options:[...]}]，
                   用于记录口味、份量等可选规格，由 dish_form.html 的重复表单字段维护。
    preprocess_days: 提前几天准备（用于便当可用性判断，前端计算，服务端不校验）。
    image_url: 相对路径如 'static/uploads/xxx.jpg'，删除时直接调 os.remove，
               因此必须在项目根目录运行 app.py，否则删除会静默失败。
    """
    __tablename__ = 'dishes'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'))
    ingredients = db.Column(db.Text)                 # 食材描述，显示在菜品 tooltip
    bento_compatible = db.Column(db.Boolean, default=True)
    preprocess_days = db.Column(db.Integer, default=0)
    cook_minutes = db.Column(db.Integer, default=40)
    image_url = db.Column(db.String(300))
    extra_options = db.Column(db.Text)               # 规格选项，存储为 JSON 字符串
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Order(db.Model):
    """一条订单 = 某人某周的全部点菜记录（每人每周只有一行，重复提交会覆盖）。
    order_data: JSON 字符串，格式为 {days: [{day_name, date, mode, dish1, ...}, ...]}。
    week_sunday: 存该周周日日期（日历行的第一天），用作唯一键之一。
    txt_filename: 生成的可打印 TXT 文件名，管理员可在后台直接下载。
    submitted_at: 存的是 UTC naive datetime（datetime.utcnow），显示时转悉尼时区。
    """
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(80), nullable=False)
    week_sunday = db.Column(db.Date, nullable=False)
    order_data = db.Column(db.Text)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    txt_filename = db.Column(db.String(200))


# ─── 工具函数 ─────────────────────────────────────────────────

def allowed_file(filename):
    """检查上传文件的扩展名是否在白名单内。"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_image(file, crop_data=None):
    """保存上传的菜品图片：可选裁剪 → 缩略图 300×300 → 转 JPEG 存盘。
    crop_data 由前端裁剪控件提供 {x, y, width, height}（像素坐标）。
    RGBA/P 模式图片（PNG 透明通道、调色板图）转为 RGB 再存 JPEG，
    否则 PIL 保存 JPEG 会报错。
    文件名用时间戳+微秒，避免并发上传时重名。
    """
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
    """返回当前悉尼时区的 aware datetime，用于截止时间比较。"""
    tz = pytz.timezone('Australia/Sydney')
    return datetime.now(tz)


def is_order_editable(week_sunday_date):
    """判断该周订单是否仍在可编辑期内。
    截止时间：该周周日（week_sunday）悉尼时间上午 10:00。
    超过截止时间后，前端会提示用户无法修改，服务端也拒绝提交。
    """
    syd_now = get_syd_time()
    deadline = datetime.combine(week_sunday_date, datetime.min.time())
    deadline = deadline.replace(hour=10, minute=0, second=0)
    deadline = pytz.timezone('Australia/Sydney').localize(deadline)
    return syd_now < deadline


def _zh_date(d):
    """将 date 对象格式化为中文日期字符串，如 '2026年6月8日'。"""
    return f"{d.year}年{d.month}月{d.day}日"


def _zh_datetime(dt):
    """将 datetime 对象格式化为中文日期时间字符串。"""
    return f"{dt.year}年{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


def generate_txt(order_record):
    """根据订单数据生成可打印的中文 TXT 文件，保存到 orders_txt/ 目录。
    submitted_at 存储的是 UTC naive datetime，需先加上 UTC tzinfo 再转悉尼时区显示。
    TXT 文件名格式：{姓名}_{YYYYMMDD}一周点单.txt，同时写回 order_record.txt_filename。
    """
    data = json.loads(order_record.order_data)
    week_sunday = order_record.week_sunday
    week_monday = week_sunday + timedelta(days=1)
    week_saturday = week_sunday + timedelta(days=6)

    # submitted_at 是 UTC naive，先补 tzinfo 再转悉尼时区
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
            # 便当只输出两道菜 + 主食
            if day_data.get('dish1'):
                lines.append(f"  菜品1：{day_data['dish1']}")
            if day_data.get('dish2'):
                lines.append(f"  菜品2：{day_data['dish2']}")
            if day_data.get('staple'):
                lines.append(f"  主食：{day_data['staple']}")
        elif mode == '熊山洞开小灶':
            # 家里开小灶：最多 5 道菜（dish1/2 + extra1/2/3）+ 主食
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
            pass  # 这两种模式无需记录菜品，仅记录模式名称即可
        lines.append("")

    txt_filename = f"{order_record.customer_name}_{week_sunday.strftime('%Y%m%d')}一周点单.txt"
    txt_path = os.path.join(app.config['ORDERS_FOLDER'], txt_filename)
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    return txt_path


# ─── 公开路由 ─────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/order')
def order_page():
    return render_template('order.html')


@app.route('/menu')
def menu_page():
    """熊家菜谱翻书页，仅桌面端入口按钮可见，手机不显示入口。"""
    return render_template('menu.html')


@app.route('/api/dishes', methods=['GET'])
def get_dishes():
    """返回所有菜品及分类树（两级）。
    分类树在 Python 侧重建：先建 id→节点字典，再遍历挂载子节点。
    parent_id=0 的节点为根节点，其余挂到对应父节点的 children 列表。
    """
    dishes = Dish.query.all()
    categories = Category.query.order_by(Category.sort_order).all()

    # 建 id→节点 字典，每个节点带空的 children 列表
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
    """检查某人某周是否已有订单，同时返回是否仍在可编辑期。
    前端据此决定：直接进入点菜、提示覆盖确认、还是提示已过截止时间。
    """
    data = request.json
    customer_name = data['customer_name']
    week_sunday_str = data['week_sunday']
    week_sunday = datetime.strptime(week_sunday_str, '%Y-%m-%d').date()
    editable = is_order_editable(week_sunday)
    existing_order = Order.query.filter_by(customer_name=customer_name, week_sunday=week_sunday).first()
    if existing_order:
        return jsonify({'exists': True, 'editable': editable, 'order_data': existing_order.order_data})
    return jsonify({'exists': False, 'editable': editable})


@app.route('/api/order/submit', methods=['POST'])
def submit_order():
    """提交或覆盖订单（每人每周只保留一条记录）。
    先检查截止时间，再做 upsert：存在则更新，不存在则新建。
    提交成功后立即生成 TXT 文件并将文件名写回数据库，供管理员下载。
    """
    data = request.json
    customer_name = data['customer_name']
    week_sunday_str = data['week_sunday']
    week_sunday = datetime.strptime(week_sunday_str, '%Y-%m-%d').date()
    order_data = data['order_data']

    if not is_order_editable(week_sunday):
        return jsonify({'success': False, 'error': '修改时间已过（周日上午10点截止），无法提交订单'})

    # upsert：已存在则覆盖，否则新建
    existing = Order.query.filter_by(customer_name=customer_name, week_sunday=week_sunday).first()
    if existing:
        existing.order_data = json.dumps(order_data)
        existing.submitted_at = get_syd_time()
        db.session.commit()
        order_record = existing
    else:
        new_order = Order(
            customer_name=customer_name,
            week_sunday=week_sunday,
            order_data=json.dumps(order_data),
            submitted_at=get_syd_time()
        )
        db.session.add(new_order)
        db.session.commit()
        order_record = new_order

    # 生成可打印 TXT，并把文件名记回数据库
    txt_path = generate_txt(order_record)
    order_record.txt_filename = os.path.basename(txt_path)
    db.session.commit()
    return jsonify({'success': True, 'txt_filename': order_record.txt_filename})


@app.route('/api/order/history', methods=['POST'])
def get_history():
    """返回某人所有历史订单的 TXT 内容，供前端历史记录模态框展示。
    TXT 内容从磁盘文件读取；若文件不存在（被手动删除），返回空字符串。
    """
    data = request.json
    customer_name = data['customer_name']
    orders = Order.query.filter_by(customer_name=customer_name).order_by(Order.week_sunday.desc()).all()
    history = []
    for o in orders:
        txt_content = ""
        if o.txt_filename:
            txt_path = os.path.join(app.config['ORDERS_FOLDER'], o.txt_filename)
            if os.path.exists(txt_path):
                with open(txt_path, 'r', encoding='utf-8') as f:
                    txt_content = f.read()
        history.append({
            'week_sunday': o.week_sunday.strftime('%Y-%m-%d'),
            'txt_content': txt_content
        })
    return jsonify({'success': True, 'history': history})


# ─── 管理员认证 ───────────────────────────────────────────────

# 管理员密码哈希存在内存中（模块级全局变量）。
# 注意：通过"忘记密码"流程重置的密码在进程重启后会丢失，恢复为初始密码。
# 如需持久化，需将哈希写入数据库或配置文件。
_admin_password_hash = generate_password_hash('Vantage2020@')


def get_admin_hash():
    return _admin_password_hash


def set_admin_hash(new_hash):
    global _admin_password_hash
    _admin_password_hash = new_hash


# 忘记密码时需回答的三道私人问题，答案硬编码（家庭内部使用，无需更高安全级别）
SECURITY_QUESTIONS = [
    {"question": "你的第一辆车是什么牌子？", "answer": "大众"},
    {"question": "母亲的生日YYYYMMDD？", "answer": "19550524"},
    {"question": "你的第一个自建网站是哪个游戏的攻略和周边？此游戏是哪个公司出品？答案所需格式：游戏名 公司名", "answer": "心跳回忆 科乐美"}
]


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """管理员登录页，支持两种 POST 动作：
    - action=login：验证密码，成功后写 session['admin']=True。
    - action=forgot：验证三个安全问题，全部正确后允许设置新密码。
    """
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
    """装饰器：未登录时重定向到登录页，保护所有管理员路由。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


# ─── 管理员后台路由 ───────────────────────────────────────────

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    return render_template('admin_dashboard.html')


@app.route('/admin/orders')
@admin_required
def admin_orders():
    """订单管理页：按提交时间倒序显示所有订单。"""
    orders = Order.query.order_by(Order.submitted_at.desc()).all()
    return render_template('admin_orders.html', orders=orders)


@app.route('/admin/download_txt/<filename>')
@admin_required
def download_txt(filename):
    """提供订单 TXT 文件下载，文件名由前端从订单记录中取得。"""
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
    """编辑菜品信息。规格选项（extra_options）由表单中多组重复字段拼装成 JSON。
    上传新图片时先删除旧图片文件，再保存新图片（依赖 CWD 为项目根目录）。
    """
    dish = Dish.query.get_or_404(dish_id)
    if request.method == 'POST':
        dish.name = request.form['name']
        dish.ingredients = request.form.get('ingredients', '')
        dish.category_id = int(request.form['category_id'])
        dish.bento_compatible = 'bento_compatible' in request.form
        dish.preprocess_days = int(request.form.get('preprocess_days', 0))
        dish.cook_minutes = int(request.form.get('cook_minutes', 40))

        # 将表单中的多组规格字段合并为 extra_options JSON
        extra_options = []
        opt_names = request.form.getlist('opt_name[]')
        opt_types = request.form.getlist('opt_type[]')
        opt_options = request.form.getlist('opt_options[]')
        for i in range(len(opt_names)):
            if opt_names[i].strip():
                opts = [o.strip() for o in opt_options[i].split(',') if o.strip()]
                extra_options.append({'name': opt_names[i], 'type': opt_types[i], 'options': opts})
        dish.extra_options = json.dumps(extra_options)

        # 如果上传了新图片，先删旧文件再替换
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
    """新增菜品，逻辑与编辑相同，区别是 dish 对象从 POST 数据构建而非从数据库读取。"""
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
    """删除菜品时同步删除磁盘上的图片文件。"""
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
    """新增分类，sort_order 自动取同级最大值 +1，追加到末尾。"""
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
    """删除前检查是否有子分类或菜品引用，有则拒绝删除（避免孤儿数据）。"""
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
    """上移/下移分类排序。
    先对同级分类重新分配连续 sort_order（1, 2, 3, ...），
    避免初始全为 0 时交换无效的问题，再执行相邻两项的值互换。
    """
    cat_id = int(request.form['cat_id'])
    direction = request.form['direction']
    cat = Category.query.get_or_404(cat_id)
    siblings = Category.query.filter_by(parent_id=cat.parent_id).order_by(Category.sort_order, Category.id).all()

    # 重新分配稳定递增的 sort_order，消除历史脏数据（如全 0）的影响
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


with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
