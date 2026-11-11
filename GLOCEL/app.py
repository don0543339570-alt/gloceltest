import os
import secrets
import time
from collections import defaultdict
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func, inspect, or_, text
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.getenv('SECRET_KEY', 'glocel-secret-change-in-production')

IS_PRODUCTION = os.getenv('FLASK_ENV', '').lower() == 'production' or bool(os.getenv('RENDER'))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE='Lax',
    REMEMBER_COOKIE_SECURE=IS_PRODUCTION,
    WTF_CSRF_TIME_LIMIT=None,
)

# Database: Render / Supabase / local SQLite
database_url = os.getenv('DATABASE_URL', 'sqlite:///database.db')
# Strip surrounding quotes that may come from .env files
database_url = database_url.strip('"').strip("'")
# Remove pgbouncer=true (psycopg2 doesn't support it; PgBouncer handles it transparently)
database_url = database_url.replace('?pgbouncer=true&', '?').replace('?pgbouncer=true', '').replace('&pgbouncer=true', '')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
# Force psycopg2 driver so SQLAlchemy 2.x doesn't try psycopg3
if database_url.startswith('postgresql://') and '+' not in database_url.split('://')[0]:
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please sign in to continue.'
login_manager.login_message_category = 'warning'

PAYMENT_STATUSES = ['Paid', 'Unpaid']
ROLES = ['admin', 'staff']
DEFAULT_CURRENCY = 'GH₵'
DEFAULT_EXCHANGE_RATE = 15.0  # GH₵ per USD for new installs; set the real rate in Settings
# Extra landed costs that can be paid in USD or local currency.
# (amount column, currency column, label). Amounts stay in the original *_usd columns.
EXTRA_COSTS = [
    ('shipping_cost_usd', 'shipping_currency', 'Shipping'),
    ('duty_usd', 'duty_currency', 'Duty'),
    ('transport_usd', 'transport_currency', 'Transport'),
    ('charge_usd', 'charge_currency', 'Other charges'),
    ('clearance_usd', 'clearance_currency', 'Clearance'),
]
COST_CURRENCIES = ['USD', 'LOCAL']


# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------
class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exchange_rate = db.Column(db.Float, default=DEFAULT_EXCHANGE_RATE)
    company_name = db.Column(db.String(200), default='GLOCEL')
    company_address = db.Column(db.String(200), default='')
    company_phone = db.Column(db.String(100), default='')
    company_email = db.Column(db.String(100), default='')
    tax_percent = db.Column(db.Float, default=0.0)
    currency_symbol = db.Column(db.String(20), default=DEFAULT_CURRENCY)


class User(UserMixin, db.Model):
    __tablename__ = 'app_users'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(120), default='')
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='staff', nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.String(50), default='')
    last_login = db.Column(db.String(50), default='')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return bool(self.active)

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def display_name(self):
        return self.full_name or self.username


class Part(db.Model):
    __tablename__ = 'parts'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    serial_number = db.Column(db.Integer, default=0)           # auto S/N
    item_number = db.Column(db.String(100), nullable=False, index=True)  # manual
    part_number = db.Column(db.String(200), unique=True, nullable=False) # "SN-0001 ITEM"
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(100), default='')
    supplier = db.Column(db.String(200), default='')

    # Cost chain (USD)
    price_usd = db.Column(db.Float, default=0)
    shipping_cost_usd = db.Column(db.Float, default=0)
    duty_usd = db.Column(db.Float, default=0)
    transport_usd = db.Column(db.Float, default=0)
    charge_usd = db.Column(db.Float, default=0)
    clearance_usd = db.Column(db.Float, default=0)
    shipping_currency = db.Column(db.String(5), default='USD')
    duty_currency = db.Column(db.String(5), default='USD')
    transport_currency = db.Column(db.String(5), default='USD')
    charge_currency = db.Column(db.String(5), default='USD')
    clearance_currency = db.Column(db.String(5), default='USD')
    total_usd = db.Column(db.Float, default=0)  # whole landed cost expressed in USD

    exchange_rate = db.Column(db.Float, default=0)
    total_local = db.Column(db.Float, default=0)

    margin_percent = db.Column(db.Float, default=0)
    selling_price = db.Column(db.Float, default=0)

    stock_quantity = db.Column(db.Integer, default=0)
    reorder_level = db.Column(db.Integer, default=0)

    date_added = db.Column(db.String(50), default=datetime.utcnow)
    last_updated = db.Column(db.String(50), default=datetime.utcnow)

    sale_items = db.relationship('SaleItem', backref='part', lazy=True)

    @property
    def is_low(self):
        return self.reorder_level > 0 and self.stock_quantity <= self.reorder_level


class Sale(db.Model):
    __tablename__ = 'sales'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    invoice_number = db.Column(db.String(100), unique=True, nullable=False, index=True)
    customer_name = db.Column(db.String(200), default='')
    customer_phone = db.Column(db.String(100), default='')
    customer_email = db.Column(db.String(100), default='')
    sale_date = db.Column(db.String(50), default=datetime.utcnow)
    subtotal = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    total_amount = db.Column(db.Float, default=0)
    payment_status = db.Column(db.String(50), default='Paid')
    notes = db.Column(db.Text, default='')
    created_by = db.Column(db.String(100), default='')

    items = db.relationship('SaleItem', backref='sale', lazy=True, cascade='all, delete-orphan')


class SaleItem(db.Model):
    __tablename__ = 'sale_items'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sales.id'), nullable=False)
    part_id = db.Column(db.Integer, db.ForeignKey('parts.id'), nullable=False)
    part_number = db.Column(db.String(100), default='')
    part_name = db.Column(db.String(200), default='')
    quantity = db.Column(db.Integer, default=0)
    unit_price = db.Column(db.Float, default=0)
    line_total = db.Column(db.Float, default=0)


# ---------------------------------------------------------------------------
# INITIALISATION
# ---------------------------------------------------------------------------
# Columns added after the first release. db.create_all() never alters existing
# tables, so they are added here (additive only, existing data untouched).
ADDED_COLUMNS = {
    'settings': [('currency_symbol', "VARCHAR(20) DEFAULT ''")],
    'sales': [('created_by', "VARCHAR(100) DEFAULT ''")],
    'parts': [(currency_col, "VARCHAR(5) DEFAULT 'USD'") for _, currency_col, _ in EXTRA_COSTS],
}


def ensure_columns():
    insp = inspect(db.engine)
    for table, columns in ADDED_COLUMNS.items():
        if not insp.has_table(table):
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        for name, ddl in columns:
            if name in existing:
                continue
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
            except Exception as exc:  # another worker may have added it already
                app.logger.warning('Could not add column %s.%s: %s', table, name, exc)


def bootstrap_admin():
    """Create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD if no users exist."""
    username = (os.getenv('ADMIN_USERNAME') or '').strip().lower()
    password = os.getenv('ADMIN_PASSWORD') or ''
    if not username or not password or User.query.count() > 0:
        return
    user = User(username=username, full_name='Administrator', role='admin',
                active=True, created_at=now_str())
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def init_db():
    with app.app_context():
        db.create_all()
        ensure_columns()
        if not Settings.query.first():
            db.session.add(Settings(id=1, exchange_rate=DEFAULT_EXCHANGE_RATE, company_name='GLOCEL'))
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
        bootstrap_admin()


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def to_float(value, default=0.0, minimum=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float('inf'), float('-inf')):
        return default
    return max(number, minimum) if minimum is not None else number


def to_int(value, default=0, minimum=0):
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(number, minimum) if minimum is not None else number


def get_settings():
    row = g.get('settings_row')
    if row is None:
        row = Settings.query.first()
        if not row:
            row = Settings(id=1, exchange_rate=DEFAULT_EXCHANGE_RATE, company_name='GLOCEL')
            db.session.add(row)
            db.session.commit()
        g.settings_row = row
    return row


def next_serial():
    last = db.session.query(func.max(Part.serial_number)).scalar()
    return (last or 0) + 1


def build_part_number(serial, item_number):
    return f"SN-{serial:04d} {item_number.strip()}"


def calc_costs(form, rate=None):
    """Landed cost = part price (USD) + extra costs, each entered in USD or local currency."""
    price_usd = to_float(form.get('price_usd'))
    margin = to_float(form.get('margin_percent'))
    ex_rate = to_float(form.get('exchange_rate'), default=0) or to_float(rate, default=DEFAULT_EXCHANGE_RATE) or DEFAULT_EXCHANGE_RATE

    data = {'price_usd': round(price_usd, 2)}
    extras_usd = extras_local = 0.0
    for amount_col, currency_col, _ in EXTRA_COSTS:
        amount = to_float(form.get(amount_col))
        currency = form.get(currency_col) if form.get(currency_col) in COST_CURRENCIES else 'USD'
        data[amount_col] = round(amount, 2)
        data[currency_col] = currency
        if currency == 'LOCAL':
            extras_local += amount
        else:
            extras_usd += amount

    total_local = (price_usd + extras_usd) * ex_rate + extras_local
    selling_price = total_local * (1 + margin / 100.0)
    data.update({
        'total_usd': round(total_local / ex_rate, 2),
        'exchange_rate': round(ex_rate, 2),
        'total_local': round(total_local, 2),
        'margin_percent': round(margin, 2),
        'selling_price': round(selling_price, 2),
    })
    return data


def apply_part_form(part, form, rate):
    data = calc_costs(form, rate)
    for key, value in data.items():
        setattr(part, key, value)
    part.name = form.get('name', '').strip()
    part.category = form.get('category', '').strip()
    part.supplier = form.get('supplier', '').strip()
    part.stock_quantity = to_int(form.get('stock_quantity'))
    part.reorder_level = to_int(form.get('reorder_level'))
    part.last_updated = now_str()


def new_invoice_number():
    return f'INV-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(2).upper()}'


def get_cart():
    return session.get('cart', {})


def cart_count():
    return sum(it.get('qty', 0) for it in get_cart().values())


def customer_key(name, phone):
    phone = (phone or '').strip()
    if phone:
        return 'p:' + phone
    return 'n:' + (name or '').strip().lower()


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.is_admin:
            flash('Only an admin can do that.', 'danger')
            return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapper


@login_manager.user_loader
def load_user(user_id):
    user = db.session.get(User, to_int(user_id))
    return user if user and user.active else None


PUBLIC_ENDPOINTS = {'login', 'setup', 'static', 'healthz', 'manifest'}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if not current_user.is_authenticated:
        if request.path.startswith('/api/') or request.is_json:
            return jsonify({'ok': False, 'error': 'Not signed in'}), 401
        return login_manager.unauthorized()
    return None


app.jinja_env.globals['EXTRA_COSTS'] = EXTRA_COSTS


@app.template_filter('money')
def money_filter(value):
    symbol = getattr(get_settings(), 'currency_symbol', '') or DEFAULT_CURRENCY
    amount = f"{(value or 0):,.2f}"
    return f"{symbol} {amount}" if symbol else amount


@app.template_filter('usd')
def usd_filter(value):
    return f"${(value or 0):,.2f}"


@app.context_processor
def inject_globals():
    ctx = {'cart_count': 0, 'currency_symbol': ''}
    if current_user.is_authenticated:
        ctx['cart_count'] = cart_count()
        ctx['currency_symbol'] = get_settings().currency_symbol or DEFAULT_CURRENCY
    return ctx


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------
_failed_logins = defaultdict(list)
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 300


def _login_locked(ip):
    cutoff = time.time() - LOCKOUT_SECONDS
    _failed_logins[ip] = [t for t in _failed_logins[ip] if t > cutoff]
    return len(_failed_logins[ip]) >= MAX_FAILED_LOGINS


def _safe_next(target):
    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return url_for('index')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if User.query.count() == 0:
        return redirect(url_for('setup'))
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        if _login_locked(ip):
            flash('Too many failed attempts. Try again in a few minutes.', 'danger')
            return render_template('login.html'), 429
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter(func.lower(User.username) == username).first()
        if user and user.active and user.check_password(password):
            _failed_logins.pop(ip, None)
            session.clear()
            login_user(user, remember=bool(request.form.get('remember')))
            user.last_login = now_str()
            db.session.commit()
            return redirect(_safe_next(request.args.get('next')))
        _failed_logins[ip].append(time.time())
        flash('Wrong username or password.', 'danger')
    return render_template('login.html')


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """First-run page to create the initial admin. Disabled once any user exists."""
    if User.query.count() > 0:
        return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        full_name = request.form.get('full_name', '').strip()
        password = request.form.get('password', '')
        error = validate_new_user(username, password, request.form.get('confirm_password', ''))
        if error:
            flash(error, 'danger')
            return render_template('setup.html')
        user = User(username=username, full_name=full_name, role='admin', active=True, created_at=now_str())
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return redirect(url_for('login'))
        login_user(user)
        flash('Admin account created. Welcome to GLOCEL!', 'success')
        return redirect(url_for('index'))
    return render_template('setup.html')


@app.route('/logout', methods=['POST'])
def logout():
    logout_user()
    session.clear()
    flash('You have been signed out.', 'info')
    return redirect(url_for('login'))


def validate_new_user(username, password, confirm, check_existing=True):
    if not username or len(username) < 3 or not all(c.isalnum() or c in '._-' for c in username):
        return 'Username must be at least 3 characters (letters, numbers, . _ -).'
    if check_existing and User.query.filter(func.lower(User.username) == username).first():
        return 'That username is already taken.'
    return validate_password(password, confirm)


def validate_password(password, confirm):
    if len(password) < 8:
        return 'Password must be at least 8 characters.'
    if password != confirm:
        return 'Passwords do not match.'
    return None


@app.route('/account', methods=['GET', 'POST'])
def account():
    if request.method == 'POST':
        if not current_user.check_password(request.form.get('current_password', '')):
            flash('Current password is wrong.', 'danger')
            return redirect(url_for('account'))
        new_password = request.form.get('new_password', '')
        error = validate_password(new_password, request.form.get('confirm_password', ''))
        if error:
            flash(error, 'danger')
            return redirect(url_for('account'))
        current_user.full_name = request.form.get('full_name', '').strip() or current_user.full_name
        current_user.set_password(new_password)
        db.session.commit()
        flash('Password updated.', 'success')
        return redirect(url_for('index'))
    return render_template('account.html')


# ---------------------------------------------------------------------------
# USERS (admin)
# ---------------------------------------------------------------------------
def active_admin_count():
    return User.query.filter_by(role='admin', active=True).count()


@app.route('/users')
@admin_required
def users():
    rows = User.query.order_by(User.username).all()
    return render_template('users.html', users=rows, roles=ROLES)


@app.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '')
    role = request.form.get('role', 'staff')
    error = validate_new_user(username, password, request.form.get('confirm_password', password))
    if error or role not in ROLES:
        flash(error or 'Invalid role.', 'danger')
        return redirect(url_for('users'))
    user = User(username=username, full_name=request.form.get('full_name', '').strip(),
                role=role, active=True, created_at=now_str())
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f'User "{username}" created.', 'success')
    return redirect(url_for('users'))


@app.route('/users/<int:user_id>/update', methods=['POST'])
@admin_required
def update_user(user_id):
    user = db.session.get(User, user_id) or abort(404)
    role = request.form.get('role', user.role)
    active = request.form.get('active') == '1'
    if role not in ROLES:
        flash('Invalid role.', 'danger')
        return redirect(url_for('users'))
    losing_admin = user.is_admin and user.active and (role != 'admin' or not active)
    if losing_admin and active_admin_count() <= 1:
        flash('You need at least one active admin.', 'danger')
        return redirect(url_for('users'))
    if user.id == current_user.id and not active:
        flash("You can't deactivate your own account.", 'danger')
        return redirect(url_for('users'))
    user.role = role
    user.active = active
    user.full_name = request.form.get('full_name', user.full_name).strip()
    new_password = request.form.get('new_password', '')
    if new_password:
        error = validate_password(new_password, new_password)
        if error:
            flash(error, 'danger')
            return redirect(url_for('users'))
        user.set_password(new_password)
    db.session.commit()
    flash(f'User "{user.username}" updated.', 'success')
    return redirect(url_for('users'))


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    today = datetime.now().strftime('%Y-%m-%d')
    month = datetime.now().strftime('%Y-%m')
    total_parts = Part.query.count()
    low_stock_q = Part.query.filter(Part.stock_quantity <= Part.reorder_level, Part.reorder_level > 0)
    low_stock = low_stock_q.count()
    inventory_value = db.session.query(func.sum(Part.stock_quantity * Part.selling_price)).scalar() or 0
    total_sales = db.session.query(func.sum(Sale.total_amount)).scalar() or 0
    sales_today = db.session.query(func.sum(Sale.total_amount)).filter(Sale.sale_date.like(f'{today}%')).scalar() or 0
    count_today = Sale.query.filter(Sale.sale_date.like(f'{today}%')).count()
    sales_month = db.session.query(func.sum(Sale.total_amount)).filter(Sale.sale_date.like(f'{month}%')).scalar() or 0
    unpaid_q = Sale.query.filter(Sale.payment_status != 'Paid')
    unpaid_count = unpaid_q.count()
    unpaid_total = db.session.query(func.sum(Sale.total_amount)).filter(Sale.payment_status != 'Paid').scalar() or 0

    recent_sales = Sale.query.order_by(Sale.sale_date.desc()).limit(5).all()
    low_parts = low_stock_q.order_by(Part.stock_quantity).limit(5).all()

    return render_template('index.html',
                           total_parts=total_parts,
                           low_stock=low_stock,
                           inventory_value=inventory_value,
                           total_sales=total_sales,
                           sales_today=sales_today,
                           count_today=count_today,
                           sales_month=sales_month,
                           unpaid_count=unpaid_count,
                           unpaid_total=unpaid_total,
                           recent_sales=recent_sales,
                           low_parts=low_parts)


@app.route('/healthz')
def healthz():
    return {'ok': True}


@app.route('/manifest.webmanifest')
def manifest():
    data = {
        'name': 'GLOCEL',
        'short_name': 'GLOCEL',
        'start_url': '/',
        'display': 'standalone',
        'background_color': '#f4f6fb',
        'theme_color': '#1e3a8a',
        'icons': [{'src': url_for('static', filename='icon.svg'), 'sizes': 'any', 'type': 'image/svg+xml'}],
    }
    return app.response_class(app.json.dumps(data), mimetype='application/manifest+json')


# ---------------------------------------------------------------------------
# PARTS / STOCK CONTROL
# ---------------------------------------------------------------------------
@app.route('/parts')
def parts():
    search = request.args.get('search', '').strip()
    show = request.args.get('show', '')
    query = Part.query
    if search:
        like = f'%{search}%'
        query = query.filter(or_(
            Part.part_number.ilike(like),
            Part.item_number.ilike(like),
            Part.name.ilike(like),
            Part.category.ilike(like),
            Part.supplier.ilike(like),
        ))
    if show == 'low':
        query = query.filter(Part.stock_quantity <= Part.reorder_level, Part.reorder_level > 0)
    elif show == 'out':
        query = query.filter(Part.stock_quantity <= 0)
    rows = query.order_by(Part.serial_number).all()
    return render_template('parts.html', parts=rows, search=search, show=show)


@app.route('/parts/add', methods=['GET', 'POST'])
def add_part():
    settings = get_settings()
    default_rate = settings.exchange_rate or DEFAULT_EXCHANGE_RATE

    if request.method == 'POST':
        item_number = request.form.get('item_number', '').strip()
        name = request.form.get('name', '').strip()
        if not item_number or not name:
            flash('Item Number and Part Name are required.', 'danger')
            return render_template('part_form.html', part=None, form=request.form,
                                   default_rate=default_rate, next_sn=next_serial())

        existing = Part.query.filter(func.lower(Part.item_number) == item_number.lower()).first()
        if existing:
            flash(f'Item Number "{item_number}" already exists ({existing.part_number}). Restock it instead.', 'warning')
            return redirect(url_for('edit_part', part_id=existing.id))

        for _ in range(3):
            serial = next_serial()
            part = Part(serial_number=serial, item_number=item_number,
                        part_number=build_part_number(serial, item_number), date_added=now_str())
            apply_part_form(part, request.form, default_rate)
            db.session.add(part)
            try:
                db.session.commit()
                flash(f'Part added: {part.part_number}', 'success')
                return redirect(url_for('parts'))
            except IntegrityError:
                db.session.rollback()
        flash('Error saving part. Please try again.', 'danger')

    return render_template('part_form.html', part=None, form={}, default_rate=default_rate, next_sn=next_serial())


@app.route('/parts/edit/<int:part_id>', methods=['GET', 'POST'])
def edit_part(part_id):
    part = db.session.get(Part, part_id)
    if not part:
        flash('Part not found.', 'danger')
        return redirect(url_for('parts'))

    default_rate = get_settings().exchange_rate or DEFAULT_EXCHANGE_RATE

    if request.method == 'POST':
        item_number = request.form.get('item_number', '').strip()
        name = request.form.get('name', '').strip()
        if not item_number or not name:
            flash('Item Number and Part Name are required.', 'danger')
            return redirect(url_for('edit_part', part_id=part.id))
        clash = Part.query.filter(func.lower(Part.item_number) == item_number.lower(), Part.id != part.id).first()
        if clash:
            flash(f'Item Number "{item_number}" is already used by {clash.part_number}.', 'danger')
            return redirect(url_for('edit_part', part_id=part.id))

        part.item_number = item_number
        part.part_number = build_part_number(part.serial_number, item_number)
        apply_part_form(part, request.form, default_rate)
        try:
            db.session.commit()
            flash('Part updated.', 'success')
            return redirect(url_for('parts'))
        except IntegrityError:
            db.session.rollback()
            flash('Error updating part.', 'danger')

    return render_template('part_form.html', part=part, form={}, default_rate=default_rate, next_sn=part.serial_number)


@app.route('/parts/restock/<int:part_id>', methods=['POST'])
def restock_part(part_id):
    part = db.session.get(Part, part_id)
    if not part:
        flash('Part not found.', 'danger')
        return redirect(url_for('parts'))
    add_qty = to_int(request.form.get('add_quantity'))
    if add_qty <= 0:
        flash('Enter a valid quantity.', 'warning')
        return redirect(request.referrer or url_for('parts'))
    part.stock_quantity += add_qty
    part.last_updated = now_str()
    db.session.commit()
    flash(f'Restocked {add_qty} × {part.name}. New stock: {part.stock_quantity}', 'success')
    return redirect(request.referrer or url_for('parts'))


@app.route('/parts/delete/<int:part_id>', methods=['POST'])
@admin_required
def delete_part(part_id):
    part = db.session.get(Part, part_id)
    if not part:
        return redirect(url_for('parts'))
    if SaleItem.query.filter_by(part_id=part.id).first():
        flash(f'{part.part_number} appears on past invoices, so it can\'t be deleted. Set its stock to 0 instead.', 'warning')
        return redirect(url_for('edit_part', part_id=part.id))
    db.session.delete(part)
    db.session.commit()
    flash('Part deleted.', 'info')
    return redirect(url_for('parts'))


# ---------------------------------------------------------------------------
# REORDER REPORT
# ---------------------------------------------------------------------------
@app.route('/reorder')
def reorder():
    rows = Part.query.filter(
        Part.stock_quantity <= Part.reorder_level,
        Part.reorder_level > 0,
    ).order_by(Part.supplier, Part.serial_number).all()
    return render_template('reorder.html', parts=rows)


# ---------------------------------------------------------------------------
# SALES & INVOICES
# ---------------------------------------------------------------------------
@app.route('/sales')
def sales():
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '')
    query = Sale.query
    if search:
        like = f'%{search}%'
        query = query.filter(or_(Sale.invoice_number.ilike(like), Sale.customer_name.ilike(like),
                                 Sale.customer_phone.ilike(like)))
    if status in PAYMENT_STATUSES:
        query = query.filter(Sale.payment_status == status)
    rows = query.order_by(Sale.sale_date.desc()).all()
    return render_template('sales.html', sales=rows, search=search, status=status, statuses=PAYMENT_STATUSES)


def known_customers():
    rows = db.session.query(Sale.customer_name, Sale.customer_phone, Sale.customer_email, Sale.sale_date) \
        .filter(Sale.customer_name != '').order_by(Sale.sale_date.desc()).all()
    seen, result = set(), []
    for name, phone, email, _ in rows:
        key = customer_key(name, phone)
        if key in seen:
            continue
        seen.add(key)
        result.append({'name': name, 'phone': phone or '', 'email': email or ''})
    return result


@app.route('/sales/new', methods=['GET', 'POST'])
def new_sale():
    settings = get_settings()

    if request.method == 'POST':
        part_ids = request.form.getlist('part_id[]')
        quantities = request.form.getlist('quantity[]')

        wanted = {}
        for pid, qty in zip(part_ids, quantities):
            pid, qty = to_int(pid), to_int(qty)
            if pid and qty > 0:
                wanted[pid] = wanted.get(pid, 0) + qty
        if not wanted:
            flash('Add at least one item to the sale.', 'warning')
            return redirect(url_for('new_sale'))

        subtotal = 0.0
        items = []
        for pid, qty in wanted.items():
            part = db.session.get(Part, pid, with_for_update=True)
            if not part:
                db.session.rollback()
                flash('One of the selected parts no longer exists.', 'danger')
                return redirect(url_for('new_sale'))
            if part.stock_quantity < qty:
                db.session.rollback()
                flash(f'Not enough stock for {part.name}. Available: {part.stock_quantity}', 'danger')
                return redirect(url_for('new_sale'))
            line_total = round(part.selling_price * qty, 2)
            subtotal += line_total
            items.append((part, qty, line_total))

        tax_pct = to_float(request.form.get('tax_percent'))
        discount = to_float(request.form.get('discount'))
        tax = subtotal * (tax_pct / 100.0)
        discount = min(discount, subtotal + tax)
        total_amount = subtotal + tax - discount
        status = request.form.get('payment_status', 'Paid')
        if status not in PAYMENT_STATUSES:
            status = 'Paid'

        sale = Sale(
            invoice_number=new_invoice_number(),
            customer_name=request.form.get('customer_name', '').strip(),
            customer_phone=request.form.get('customer_phone', '').strip(),
            customer_email=request.form.get('customer_email', '').strip(),
            sale_date=now_str(),
            subtotal=round(subtotal, 2),
            tax=round(tax, 2),
            discount=round(discount, 2),
            total_amount=round(total_amount, 2),
            payment_status=status,
            notes=request.form.get('notes', '').strip(),
            created_by=current_user.username,
        )
        db.session.add(sale)
        for part, qty, line_total in items:
            sale.items.append(SaleItem(part_id=part.id, part_number=part.part_number, part_name=part.name,
                                       quantity=qty, unit_price=part.selling_price, line_total=line_total))
            part.stock_quantity -= qty
            part.last_updated = now_str()
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Could not save the sale. Please try again.', 'danger')
            return redirect(url_for('new_sale'))
        session.pop('cart', None)
        flash('Invoice generated successfully.', 'success')
        return redirect(url_for('view_invoice', sale_id=sale.id))

    parts = Part.query.filter(Part.stock_quantity > 0).order_by(Part.name).all()
    parts_data = [{'id': p.id, 'name': p.name, 'part_number': p.part_number, 'item_number': p.item_number,
                   'price': p.selling_price, 'stock': p.stock_quantity} for p in parts]
    in_stock = {p.id for p in parts}
    cart_items = [{'id': to_int(pid), 'qty': it.get('qty', 1)} for pid, it in get_cart().items()
                  if to_int(pid) in in_stock]
    prefill = {k: request.args.get(k, '') for k in ('customer_name', 'customer_phone', 'customer_email')}
    return render_template('new_sale.html', parts=parts_data, cart_items=cart_items,
                           tax_percent=settings.tax_percent or 0, customers=known_customers(),
                           prefill=prefill, statuses=PAYMENT_STATUSES)


@app.route('/sale/add-to-cart/<int:part_id>', methods=['POST'])
def add_to_cart(part_id):
    """Quick add one unit of a part to the cart (used from the scan page)."""
    part = db.session.get(Part, part_id)
    if not part or part.stock_quantity <= 0:
        return jsonify({'ok': False, 'error': 'Out of stock or not found'}), 400
    cart = get_cart()
    pid = str(part_id)
    if pid in cart:
        if cart[pid]['qty'] >= part.stock_quantity:
            return jsonify({'ok': False, 'error': 'Max stock reached'}), 400
        cart[pid]['qty'] += 1
    else:
        cart[pid] = {
            'part_number': part.part_number,
            'part_name': part.name,
            'unit_price': part.selling_price,
            'qty': 1,
        }
    session['cart'] = cart
    return jsonify({'ok': True, 'cart': cart, 'count': cart_count()})


@app.route('/cart')
def view_cart():
    return redirect(url_for('new_sale'))


@app.route('/cart/clear', methods=['POST'])
def clear_cart():
    session.pop('cart', None)
    return redirect(request.referrer or url_for('new_sale'))


@app.route('/cart/remove/<int:part_id>', methods=['POST'])
def remove_from_cart(part_id):
    cart = get_cart()
    cart.pop(str(part_id), None)
    session['cart'] = cart
    return redirect(request.referrer or url_for('scan'))


@app.route('/invoice/<int:sale_id>')
def view_invoice(sale_id):
    sale = db.session.get(Sale, sale_id)
    if not sale:
        flash('Invoice not found.', 'danger')
        return redirect(url_for('sales'))
    items = SaleItem.query.filter_by(sale_id=sale_id).all()
    return render_template('invoice.html', settings=get_settings(), sale=sale, items=items,
                           statuses=PAYMENT_STATUSES)


@app.route('/invoice/<int:sale_id>/status', methods=['POST'])
def update_invoice_status(sale_id):
    sale = db.session.get(Sale, sale_id) or abort(404)
    status = request.form.get('payment_status')
    if status in PAYMENT_STATUSES:
        sale.payment_status = status
        db.session.commit()
        flash(f'{sale.invoice_number} marked as {status}.', 'success')
    return redirect(request.referrer or url_for('view_invoice', sale_id=sale.id))


# ---------------------------------------------------------------------------
# CUSTOMERS (built from sales history)
# ---------------------------------------------------------------------------
def customer_summaries():
    summaries = {}
    for s in Sale.query.order_by(Sale.sale_date.desc()).all():
        if not (s.customer_name or s.customer_phone):
            continue
        key = customer_key(s.customer_name, s.customer_phone)
        c = summaries.get(key)
        if not c:
            c = summaries[key] = {'key': key, 'name': s.customer_name or s.customer_phone,
                                  'phone': s.customer_phone or '', 'email': s.customer_email or '',
                                  'invoices': 0, 'total': 0.0, 'unpaid': 0.0, 'last': s.sale_date}
        c['invoices'] += 1
        c['total'] += s.total_amount or 0
        if s.payment_status != 'Paid':
            c['unpaid'] += s.total_amount or 0
        if not c['email'] and s.customer_email:
            c['email'] = s.customer_email
    return list(summaries.values())


@app.route('/customers')
def customers():
    search = request.args.get('search', '').strip().lower()
    rows = customer_summaries()
    if search:
        rows = [c for c in rows if search in c['name'].lower() or search in c['phone'] or search in c['email'].lower()]
    sort = request.args.get('sort', 'recent')
    if sort == 'top':
        rows.sort(key=lambda c: c['total'], reverse=True)
    elif sort == 'unpaid':
        rows = sorted([c for c in rows if c['unpaid'] > 0], key=lambda c: c['unpaid'], reverse=True)
    return render_template('customers.html', customers=rows, search=search, sort=sort)


@app.route('/customers/view')
def customer_detail():
    key = request.args.get('key', '')
    match = next((c for c in customer_summaries() if c['key'] == key), None)
    if not match:
        flash('Customer not found.', 'warning')
        return redirect(url_for('customers'))
    if key.startswith('p:'):
        sales_q = Sale.query.filter(Sale.customer_phone == key[2:])
    else:
        sales_q = Sale.query.filter(func.lower(Sale.customer_name) == key[2:],
                                    or_(Sale.customer_phone == '', Sale.customer_phone.is_(None)))
    rows = sales_q.order_by(Sale.sale_date.desc()).all()
    return render_template('customer_detail.html', customer=match, sales=rows)


# ---------------------------------------------------------------------------
# CAMERA SCAN (mobile friendly)
# ---------------------------------------------------------------------------
@app.route('/scan')
def scan():
    cart = get_cart()
    subtotal = sum(it['unit_price'] * it['qty'] for it in cart.values())
    return render_template('scan.html', cart=cart, subtotal=round(subtotal, 2))


@app.route('/api/part/lookup/<path:item_number>')
def lookup_part(item_number):
    """JSON lookup by item number or part number for scanner."""
    code = item_number.strip()
    part = Part.query.filter(
        or_(func.lower(Part.item_number) == code.lower(), func.lower(Part.part_number) == code.lower())
    ).first()
    if not part:
        return jsonify({'found': False})
    return jsonify({
        'found': True,
        'id': part.id,
        'part_number': part.part_number,
        'serial_number': part.serial_number,
        'item_number': part.item_number,
        'name': part.name,
        'selling_price': part.selling_price,
        'selling_price_display': money_filter(part.selling_price),
        'stock_quantity': part.stock_quantity,
        'edit_url': url_for('edit_part', part_id=part.id),
    })


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
@app.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    row = get_settings()

    if request.method == 'POST':
        row.exchange_rate = to_float(request.form.get('exchange_rate'), default=DEFAULT_EXCHANGE_RATE) or DEFAULT_EXCHANGE_RATE
        row.company_name = request.form.get('company_name', '').strip() or 'GLOCEL'
        row.company_address = request.form.get('company_address', '').strip()
        row.company_phone = request.form.get('company_phone', '').strip()
        row.company_email = request.form.get('company_email', '').strip()
        row.tax_percent = to_float(request.form.get('tax_percent'))
        row.currency_symbol = request.form.get('currency_symbol', '').strip()[:20]
        try:
            db.session.commit()
            flash('Settings saved.', 'success')
        except Exception:
            db.session.rollback()
            flash('Error saving settings.', 'danger')
        return redirect(url_for('settings'))

    return render_template('settings.html', settings=row)


@app.errorhandler(404)
def not_found(_):
    return render_template('error.html', code=404, message="That page doesn't exist."), 404


@app.errorhandler(500)
def server_error(_):
    db.session.rollback()
    return render_template('error.html', code=500, message='Something went wrong. Please try again.'), 500


init_db()


if __name__ == '__main__':
    app.run(debug=not IS_PRODUCTION, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
