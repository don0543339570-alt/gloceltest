import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from sqlalchemy import func, or_

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'glocel-secret-change-in-production')

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

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------
class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exchange_rate = db.Column(db.Float, default=1500.0)
    company_name = db.Column(db.String(200), default='GLOCEL')
    company_address = db.Column(db.String(200), default='')
    company_phone = db.Column(db.String(100), default='')
    company_email = db.Column(db.String(100), default='')
    tax_percent = db.Column(db.Float, default=0.0)


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
    total_usd = db.Column(db.Float, default=0)

    exchange_rate = db.Column(db.Float, default=0)
    total_local = db.Column(db.Float, default=0)

    margin_percent = db.Column(db.Float, default=0)
    selling_price = db.Column(db.Float, default=0)

    stock_quantity = db.Column(db.Integer, default=0)
    reorder_level = db.Column(db.Integer, default=0)

    date_added = db.Column(db.String(50), default=datetime.utcnow)
    last_updated = db.Column(db.String(50), default=datetime.utcnow)

    sale_items = db.relationship('SaleItem', backref='part', lazy=True)


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
def init_db():
    with app.app_context():
        db.create_all()
        if not Settings.query.first():
            db.session.add(Settings(id=1, exchange_rate=1500.0, company_name='GLOCEL'))
            db.session.commit()


def next_serial():
    with app.app_context():
        last = db.session.query(func.max(Part.serial_number)).scalar()
        return (last or 0) + 1


def build_part_number(serial, item_number):
    return f"SN-{serial:04d} {item_number.strip()}"


def calc_costs(form, rate=None):
    price_usd = float(form.get('price_usd') or 0)
    shipping = float(form.get('shipping_cost_usd') or 0)
    duty = float(form.get('duty_usd') or 0)
    transport = float(form.get('transport_usd') or 0)
    charge = float(form.get('charge_usd') or 0)
    clearance = float(form.get('clearance_usd') or 0)
    margin = float(form.get('margin_percent') or 0)

    total_usd = price_usd + shipping + duty + transport + charge + clearance
    ex_rate = float(rate) if rate else float(form.get('exchange_rate') or 1500.0)
    total_local = total_usd * ex_rate
    selling_price = total_local * (1 + margin / 100.0)

    return {
        'price_usd': round(price_usd, 2),
        'shipping_cost_usd': round(shipping, 2),
        'duty_usd': round(duty, 2),
        'transport_usd': round(transport, 2),
        'charge_usd': round(charge, 2),
        'clearance_usd': round(clearance, 2),
        'total_usd': round(total_usd, 2),
        'exchange_rate': round(ex_rate, 2),
        'total_local': round(total_local, 2),
        'margin_percent': round(margin, 2),
        'selling_price': round(selling_price, 2),
    }


init_db()


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    total_parts = Part.query.count()
    low_stock = Part.query.filter(Part.stock_quantity <= Part.reorder_level, Part.reorder_level > 0).count()
    inventory_value = db.session.query(func.sum(Part.stock_quantity * Part.selling_price)).scalar() or 0
    total_sales = db.session.query(func.sum(Sale.total_amount)).scalar() or 0

    recent_parts = Part.query.order_by(Part.serial_number.desc()).limit(5).all()
    recent_sales = Sale.query.order_by(Sale.sale_date.desc()).limit(5).all()

    return render_template('index.html',
                           total_parts=total_parts,
                           low_stock=low_stock,
                           inventory_value=inventory_value,
                           total_sales=total_sales,
                           recent_parts=recent_parts,
                           recent_sales=recent_sales)


# ---------------------------------------------------------------------------
# PARTS / STOCK CONTROL
# ---------------------------------------------------------------------------
@app.route('/parts')
def parts():
    search = request.args.get('search', '').strip()
    query = Part.query
    if search:
        like = f'%{search}%'
        query = query.filter(or_(
            Part.part_number.ilike(like),
            Part.item_number.ilike(like),
            Part.name.ilike(like),
            Part.category.ilike(like),
        ))
    rows = query.order_by(Part.serial_number).all()
    return render_template('parts.html', parts=rows, search=search)


@app.route('/parts/add', methods=['GET', 'POST'])
def add_part():
    settings = Settings.query.first()
    default_rate = settings.exchange_rate if settings else 1500.0
    next_sn = next_serial()

    if request.method == 'POST':
        item_number = request.form.get('item_number', '').strip()
        name = request.form.get('name', '').strip()
        if not item_number or not name:
            flash('Item Number and Part Name are required.', 'danger')
            return redirect(url_for('add_part'))

        serial = next_serial()
        part_number = build_part_number(serial, item_number)

        # check duplicate manual item number
        existing = Part.query.filter_by(item_number=item_number).first()
        if existing:
            flash(f'Item Number "{item_number}" already exists (SN-{existing.serial_number:04d}). Use Edit to restock.', 'warning')
            return redirect(url_for('edit_part', part_id=existing.id))

        data = calc_costs(request.form, default_rate)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            p = Part(
                serial_number=serial,
                item_number=item_number,
                part_number=part_number,
                name=name,
                category=request.form.get('category', '').strip(),
                supplier=request.form.get('supplier', '').strip(),
                price_usd=data['price_usd'],
                shipping_cost_usd=data['shipping_cost_usd'],
                duty_usd=data['duty_usd'],
                transport_usd=data['transport_usd'],
                charge_usd=data['charge_usd'],
                clearance_usd=data['clearance_usd'],
                total_usd=data['total_usd'],
                exchange_rate=data['exchange_rate'],
                total_local=data['total_local'],
                margin_percent=data['margin_percent'],
                selling_price=data['selling_price'],
                stock_quantity=int(request.form.get('stock_quantity') or 0),
                reorder_level=int(request.form.get('reorder_level') or 0),
                date_added=now,
                last_updated=now,
            )
            db.session.add(p)
            db.session.commit()
            flash(f'Part added: {part_number}', 'success')
            return redirect(url_for('parts'))
        except Exception:
            db.session.rollback()
            flash('Error saving part.', 'danger')

    return render_template('add_part.html', default_rate=default_rate, next_sn=next_sn)


@app.route('/parts/edit/<int:part_id>', methods=['GET', 'POST'])
def edit_part(part_id):
    part = db.session.get(Part, part_id)
    if not part:
        flash('Part not found.', 'danger')
        return redirect(url_for('parts'))

    settings = Settings.query.first()
    default_rate = settings.exchange_rate if settings else 1500.0

    if request.method == 'POST':
        data = calc_costs(request.form)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        part.item_number = request.form.get('item_number', '').strip()
        part.part_number = build_part_number(part.serial_number, part.item_number)
        part.name = request.form.get('name', '').strip()
        part.category = request.form.get('category', '').strip()
        part.supplier = request.form.get('supplier', '').strip()
        part.price_usd = data['price_usd']
        part.shipping_cost_usd = data['shipping_cost_usd']
        part.duty_usd = data['duty_usd']
        part.transport_usd = data['transport_usd']
        part.charge_usd = data['charge_usd']
        part.clearance_usd = data['clearance_usd']
        part.total_usd = data['total_usd']
        part.exchange_rate = data['exchange_rate']
        part.total_local = data['total_local']
        part.margin_percent = data['margin_percent']
        part.selling_price = data['selling_price']
        part.stock_quantity = int(request.form.get('stock_quantity') or 0)
        part.reorder_level = int(request.form.get('reorder_level') or 0)
        part.last_updated = now

        try:
            db.session.commit()
            flash('Part updated.', 'success')
            return redirect(url_for('parts'))
        except Exception:
            db.session.rollback()
            flash('Error updating part.', 'danger')

    return render_template('edit_part.html', part=part, default_rate=default_rate)


@app.route('/parts/restock/<int:part_id>', methods=['POST'])
def restock_part(part_id):
    part = db.session.get(Part, part_id)
    if not part:
        flash('Part not found.', 'danger')
        return redirect(url_for('parts'))
    add_qty = int(request.form.get('add_quantity') or 0)
    if add_qty <= 0:
        flash('Enter a valid quantity.', 'warning')
        return redirect(url_for('parts'))
    part.stock_quantity += add_qty
    part.last_updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.session.commit()
    flash(f'Restocked {add_qty} units of {part.part_number}. New stock: {part.stock_quantity}', 'success')
    return redirect(url_for('parts'))


@app.route('/parts/delete/<int:part_id>', methods=['POST'])
def delete_part(part_id):
    part = db.session.get(Part, part_id)
    if part:
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
    ).order_by(Part.serial_number).all()
    return render_template('reorder.html', parts=rows)


# ---------------------------------------------------------------------------
# SALES & INVOICES
# ---------------------------------------------------------------------------
@app.route('/sales')
def sales():
    rows = Sale.query.order_by(Sale.sale_date.desc()).all()
    return render_template('sales.html', sales=rows)


@app.route('/sales/new', methods=['GET', 'POST'])
def new_sale():
    settings = Settings.query.first()
    tax_percent = settings.tax_percent if settings else 0

    if request.method == 'POST':
        customer_name = request.form.get('customer_name', '').strip()
        customer_phone = request.form.get('customer_phone', '').strip()
        customer_email = request.form.get('customer_email', '').strip()
        discount = float(request.form.get('discount') or 0)
        tax_pct = float(request.form.get('tax_percent') or 0)
        notes = request.form.get('notes', '').strip()
        sale_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        part_ids = request.form.getlist('part_id[]')
        quantities = request.form.getlist('quantity[]')

        invoice_number = f'INV-{datetime.now().strftime("%Y%m%d%H%M%S")}'
        subtotal = 0.0
        items = []

        for pid, qty in zip(part_ids, quantities):
            pid = int(pid)
            qty = int(qty)
            if qty <= 0:
                continue
            part = db.session.get(Part, pid)
            if not part:
                continue
            if part.stock_quantity < qty:
                flash(f"Not enough stock for {part.part_number} ({part.name}). Available: {part.stock_quantity}", 'danger')
                return redirect(url_for('new_sale'))
            unit_price = part.selling_price
            line_total = unit_price * qty
            subtotal += line_total
            items.append({
                'part_id': pid,
                'part_number': part.part_number,
                'part_name': part.name,
                'quantity': qty,
                'unit_price': unit_price,
                'line_total': line_total,
            })

        tax = subtotal * (tax_pct / 100.0)
        total_amount = subtotal + tax - discount

        sale = Sale(
            invoice_number=invoice_number,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            sale_date=sale_date,
            subtotal=round(subtotal, 2),
            tax=round(tax, 2),
            discount=round(discount, 2),
            total_amount=round(total_amount, 2),
            payment_status='Paid',
            notes=notes,
        )
        db.session.add(sale)
        db.session.flush()

        for it in items:
            si = SaleItem(
                sale_id=sale.id,
                part_id=it['part_id'],
                part_number=it['part_number'],
                part_name=it['part_name'],
                quantity=it['quantity'],
                unit_price=it['unit_price'],
                line_total=it['line_total'],
            )
            db.session.add(si)
            part = db.session.get(Part, it['part_id'])
            part.stock_quantity -= it['quantity']

        db.session.commit()
        flash('Invoice generated successfully.', 'success')
        return redirect(url_for('view_invoice', sale_id=sale.id))

    parts = Part.query.filter(Part.stock_quantity > 0).order_by(Part.serial_number).all()
    return render_template('new_sale.html', parts=parts, tax_percent=tax_percent)


@app.route('/sale/add-to-cart/<int:part_id>', methods=['POST'])
def add_to_cart(part_id):
    """Quick add one unit of a part to a new sale (used from scan/page)."""
    part = db.session.get(Part, part_id)
    if not part or part.stock_quantity <= 0:
        return jsonify({'ok': False, 'error': 'Out of stock or not found'}), 400
    # Store in session cart
    cart = session.get('cart', {})
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
    return jsonify({'ok': True, 'cart': cart})


@app.route('/cart')
def view_cart():
    cart = session.get('cart', {})
    subtotal = sum(it['unit_price'] * it['qty'] for it in cart.values())
    return render_template('cart.html', cart=cart, subtotal=round(subtotal, 2))


@app.route('/cart/clear')
def clear_cart():
    session.pop('cart', None)
    return redirect(url_for('new_sale'))


@app.route('/cart/remove/<int:part_id>')
def remove_from_cart(part_id):
    cart = session.get('cart', {})
    cart.pop(str(part_id), None)
    session['cart'] = cart
    return redirect(url_for('view_cart'))


@app.route('/invoice/<int:sale_id>')
def view_invoice(sale_id):
    settings = Settings.query.first()
    sale = db.session.get(Sale, sale_id)
    if not sale:
        flash('Invoice not found.', 'danger')
        return redirect(url_for('sales'))
    items = SaleItem.query.filter_by(sale_id=sale_id).all()
    return render_template('invoice.html', settings=settings, sale=sale, items=items)


# ---------------------------------------------------------------------------
# CAMERA SCAN (mobile friendly)
# ---------------------------------------------------------------------------
@app.route('/scan')
def scan():
    return render_template('scan.html')


@app.route('/api/part/lookup/<item_number>')
def lookup_part(item_number):
    """JSON lookup by item number or part number for scanner."""
    part = Part.query.filter(
        or_(Part.item_number == item_number, Part.part_number == item_number)
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
        'stock_quantity': part.stock_quantity,
    })


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
@app.route('/settings', methods=['GET', 'POST'])
def settings():
    row = Settings.query.first()
    if not row:
        row = Settings(id=1, exchange_rate=1500.0, company_name='GLOCEL')
        db.session.add(row)
        db.session.commit()

    if request.method == 'POST':
        row.exchange_rate = float(request.form.get('exchange_rate') or 1500)
        row.company_name = request.form.get('company_name', '').strip()
        row.company_address = request.form.get('company_address', '').strip()
        row.company_phone = request.form.get('company_phone', '').strip()
        row.company_email = request.form.get('company_email', '').strip()
        row.tax_percent = float(request.form.get('tax_percent') or 0)
        try:
            db.session.commit()
            flash('Settings saved.', 'success')
        except Exception:
            db.session.rollback()
            flash('Error saving settings.', 'danger')
        return redirect(url_for('settings'))

    return render_template('settings.html', settings=row)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
