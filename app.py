from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response
from flask_sqlalchemy import SQLAlchemy
import os
import csv
import io
from decimal import Decimal

COLORS = {
    "wash amarillo": "#FFEAA7",
    "wash rosa": "#FFCAD4",
    "wash morado": "#DCD0FF",
    "chasis": "#D9E4F5",
    "motor": "#FFFFFF",
    "desmanchado interno": "#C3E5FF",
    "porcelanizado": "#D6F5D6",
    "efecto bross": "#E7D5C6",
    "enjuague": "#8BF9FB"

}


app = Flask(__name__)
app.secret_key = "cambia_esto_por_algo_mas_seguro"


# Base de datos SQLite
# - Local (por defecto): <repo>/agenda.db
# - Railway (con Volume): setear variable de entorno DB_PATH=/data/agenda.db
basedir = os.path.abspath(os.path.dirname(__file__))
default_db_path = os.path.join(basedir, "agenda.db")

# Si DB_PATH viene definido, úsalo. Si no, usa el default local.
db_path = os.environ.get("DB_PATH", default_db_path)

# Asegurar que exista el directorio (ej: /data)
db_dir = os.path.dirname(db_path)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

# SQLAlchemy requiere ruta absoluta para SQLite (mejor práctica)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.abspath(db_path)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


db = SQLAlchemy(app)

# --- Ensure expenses schema migration for is_void column ---
from sqlalchemy import text

def ensure_expenses_schema():
    with app.app_context():
        try:
            # Verificar si la columna is_void existe
            db.session.execute(text("SELECT is_void FROM expenses LIMIT 1"))
        except Exception:
            # Si no existe, crearla sin borrar datos
            db.session.execute(
                text("ALTER TABLE expenses ADD COLUMN is_void BOOLEAN DEFAULT 0")
            )
            db.session.commit()

ensure_expenses_schema()

# --- Ensure appointments schema migration for vehicle_type_id ---
def ensure_appointments_schema():
    with app.app_context():
        try:
            db.session.execute(text("SELECT vehicle_type_id FROM appointments LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE appointments ADD COLUMN vehicle_type_id INTEGER")
            )
            db.session.commit()

ensure_appointments_schema()

# --- Ensure service_sales table exists ---
def ensure_service_sales_schema():
    with app.app_context():
        try:
            db.session.execute(text("SELECT id FROM service_sales LIMIT 1"))
        except Exception:
            ServiceSale.__table__.create(db.engine)

ensure_service_sales_schema()

# -----------------------
# MODELOS
# -----------------------

class Service(db.Model):
    __tablename__ = "services"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    duration_minutes = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f"<Service {self.name} ({self.duration_minutes} min)>"

# -----------------------
# VEHICLE TYPES (CATÁLOGO)
# -----------------------
class VehicleType(db.Model):
    __tablename__ = "vehicle_types"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<VehicleType {self.name} active={self.is_active}>"

# -----------------------
# PAYMENT METHODS (CATÁLOGO)
# -----------------------
class PaymentMethod(db.Model):
    __tablename__ = "payment_methods"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<PaymentMethod {self.name} active={self.is_active}>"

# -----------------------
# SERVICE PRICES (PRECIO + DURACIÓN REAL POR VEHÍCULO)
# -----------------------
class ServicePrice(db.Model):
    __tablename__ = "service_prices"
    id = db.Column(db.Integer, primary_key=True)

    service_id = db.Column(db.Integer, db.ForeignKey("services.id"), nullable=False)
    vehicle_type_id = db.Column(db.Integer, db.ForeignKey("vehicle_types.id"), nullable=False)

    price = db.Column(db.Integer, nullable=False)  # sin decimales
    duration_minutes = db.Column(db.Integer, nullable=False)

    is_active = db.Column(db.Boolean, default=True)

    service = db.relationship("Service", backref=db.backref("prices", lazy=True))
    vehicle_type = db.relationship("VehicleType", backref=db.backref("service_prices", lazy=True))

    __table_args__ = (
        db.UniqueConstraint("service_id", "vehicle_type_id", name="uix_service_vehicle"),
    )

    def __repr__(self):
        return (
            f"<ServicePrice service={self.service_id} "
            f"vehicle={self.vehicle_type_id} "
            f"price={self.price} "
            f"duration={self.duration_minutes}min>"
        )



class Appointment(db.Model):
    __tablename__ = "appointments"
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=True)
    plate = db.Column(db.String(20), nullable=True)
    phone = db.Column(db.String(20)) 
    services = db.Column(db.String(255), nullable=False)  # "Wash Morado, Motor"
    start_datetime = db.Column(db.DateTime, nullable=False)
    end_datetime = db.Column(db.DateTime, nullable=False)
    notes = db.Column(db.Text, nullable=True)

    # Nueva columna para tipo de vehículo (nullable por compatibilidad)
    vehicle_type_id = db.Column(
        db.Integer,
        db.ForeignKey("vehicle_types.id"),
        nullable=True
    )
    vehicle_type = db.relationship("VehicleType")

    def __repr__(self):
        return f"<Appointment {self.customer_name} - {self.services}>"

# -----------------------
# SERVICE SALES (INGRESOS / BI)
# -----------------------
class ServiceSale(db.Model):
    __tablename__ = "service_sales"
    id = db.Column(db.Integer, primary_key=True)

    appointment_id = db.Column(
        db.Integer,
        db.ForeignKey("appointments.id"),
        nullable=False
    )

    # Fecha del servicio (día en que se cerró)
    service_date = db.Column(db.Date, nullable=False)

    # Fecha/hora de creación del registro
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Datos planos (BI friendly)
    vehicle_type = db.Column(db.String(80), nullable=False)
    plate = db.Column(db.String(20), nullable=True)
    customer_name = db.Column(db.String(120), nullable=True)
    services = db.Column(db.String(255), nullable=False)

    base_amount = db.Column(db.Integer, nullable=False)
    discount_amount = db.Column(db.Integer, nullable=False, default=0)
    final_amount = db.Column(db.Integer, nullable=False)

    payment_method = db.Column(db.String(80), nullable=True)

    # completed | cancelled
    status = db.Column(db.String(20), nullable=False)

    notes = db.Column(db.Text, nullable=True)

    appointment = db.relationship("Appointment")

    def __repr__(self):
        return f"<ServiceSale {self.service_date} {self.final_amount} {self.status}>"
    
# -----------------------
# CLIENT MODEL
# -----------------------
class Client(db.Model):
    __tablename__ = "clients"
    # Placa como identificador principal (normalizada a mayúsculas sin espacios)
    plate = db.Column(db.String(20), primary_key=True)
    full_name = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Client {self.plate} {self.full_name}>"


class Expense(db.Model):
    __tablename__ = "expenses"
    id = db.Column(db.Integer, primary_key=True)

    # Fecha real del gasto (editable por el usuario). Por defecto: hoy.
    expense_date = db.Column(db.Date, nullable=False, default=date.today)

    # Fecha/hora del registro (automática)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    amount = db.Column(db.Numeric(12, 2), nullable=False)
    category = db.Column(db.String(80), nullable=False)
    payment_method = db.Column(db.String(40), nullable=False)
    vendor = db.Column(db.String(120), nullable=True)
    description = db.Column(db.String(255), nullable=False)
    receipt = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    is_void = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self):
        return f"<Expense {self.expense_date} {self.category} {self.amount}>"


class ExpenseCategory(db.Model):
    __tablename__ = "expense_categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f"<ExpenseCategory {self.name} active={self.is_active}>"

# -----------------------
# Helper: Get list of existing vendors (for expense forms)
# -----------------------
def get_existing_vendors():
    vendors = (
        db.session.query(Expense.vendor)
        .filter(Expense.vendor.isnot(None))
        .filter(Expense.vendor != "")
        .distinct()
        .order_by(Expense.vendor)
        .all()
    )
    return [v[0] for v in vendors]

# -----------------------
# SEED INICIAL DE SERVICIOS
# -----------------------
def seed_services():
    """Crea servicios base si la tabla está vacía."""
    if Service.query.count() > 0:
        return

    services_data = [
        ("Wash Amarillo", 60),
        ("Wash Rosa", 120),
        ("Wash Morado", 160),
        ("Chasis", 60),
        ("Motor", 60),
        ("Porcelanizado", 240),
        ("Efecto Bross", 540),
        ("Desmanchado Interno", 540),
        ("Enjuague", 40),
    ]

    for name, minutes in services_data:
        s = Service(name=name, duration_minutes=minutes)
        db.session.add(s)
    db.session.commit()
    print("Servicios iniciales creados.")



def seed_expense_categories():
    """Crea categorías base de gastos si la tabla está vacía."""
    if ExpenseCategory.query.count() > 0:
        return

    for name in EXPENSE_CATEGORIES_DEFAULT:
        db.session.add(ExpenseCategory(name=name, is_active=True))
    db.session.commit()
    print("Categorías iniciales de gastos creadas.")

# -----------------------
# SEED INICIAL DE TIPOS DE VEHÍCULO
# -----------------------
def seed_vehicle_types():
    if VehicleType.query.count() > 0:
        return

    vehicle_types = [
        "Automovil",
        "SUV",
        "Camioneta",
        "Moto",
        "Cuatrimoto",
        "Buggy",
        "Jet Ski",
    ]

    for name in vehicle_types:
        db.session.add(VehicleType(name=name, is_active=True))

    db.session.commit()
    print("Tipos de vehículo iniciales creados.")

# -----------------------
# SEED INICIAL DE MEDIOS DE PAGO
# -----------------------
def seed_payment_methods():
    if PaymentMethod.query.count() > 0:
        return

    methods = [
        "Efectivo",
        "Transferencia",
        "Tarjeta de Credito",
    ]

    for name in methods:
        db.session.add(PaymentMethod(name=name, is_active=True))

    db.session.commit()
    print("Medios de pago iniciales creados.")


# -----------------------
# CLIENT HELPERS
# -----------------------
def normalize_plate(value: str | None) -> str:
    """Normaliza placa: trim, sin espacios internos, mayúsculas."""
    if not value:
        return ""
    return "".join(value.split()).upper()


def upsert_client_from_appointment(plate: str, full_name: str | None, phone: str | None):
    """Crea o actualiza el cliente por placa."""
    plate_n = normalize_plate(plate)
    if not plate_n:
        return

    full_name = (full_name or "").strip()
    phone = (phone or "").strip()

    client = Client.query.get(plate_n)
    if client:
        # Actualizar solo si viene algún dato
        if full_name:
            client.full_name = full_name
        if phone:
            client.phone = phone
    else:
        db.session.add(Client(
            plate=plate_n,
            full_name=full_name or None,
            phone=phone or None
        ))

# -----------------------
# HELPER: Calcular duración real por servicios + tipo de vehículo
# -----------------------
def calculate_real_duration_minutes(service_ids: list[int], vehicle_type_id: int) -> int:
    """
    Calcula duración total real usando ServicePrice.
    Estrategia:
    - Suma todas las duraciones reales encontradas
    - Si falta alguna combinación, usa duración base del servicio
    - Aplica solapamiento: servicio más largo + 50% de los demás
    """

    durations = []

    for sid in service_ids:
        sp = (
            ServicePrice.query
            .filter_by(service_id=sid, vehicle_type_id=vehicle_type_id, is_active=True)
            .first()
        )

        if sp:
            durations.append(sp.duration_minutes)
        else:
            # fallback seguro
            svc = Service.query.get(sid)
            if svc:
                durations.append(svc.duration_minutes)

    if not durations:
        return 60  # fallback absoluto

    durations.sort(reverse=True)
    longest = durations[0]
    others = durations[1:]

    total = longest + sum(d * 0.5 for d in others)
    return int(round(total))

# -----------------------
# HELPER: Calcular precio real por servicios + tipo de vehículo
# -----------------------
def calculate_real_price(service_ids: list[int], vehicle_type_id: int) -> int:
    """
    Calcula el precio base real usando ServicePrice.
    Estrategia:
    - Suma los precios reales encontrados
    - Si falta alguna combinación, ignora ese servicio (precio 0)
    - Devuelve entero (sin decimales)
    """

    total_price = 0

    for sid in service_ids:
        sp = (
            ServicePrice.query
            .filter_by(service_id=sid, vehicle_type_id=vehicle_type_id, is_active=True)
            .first()
        )
        if sp:
            total_price += sp.price

    return int(total_price)

# -----------------------
# HELPER: Verificar si la cita ya fue cerrada (ServiceSale existe para appointment_id)
# -----------------------
def appointment_already_closed(appointment_id: int) -> bool:
    return (
        ServiceSale.query
        .filter_by(appointment_id=appointment_id)
        .first()
        is not None
    )

# -----------------------
# PAYMENT METHODS (CRUD)
# -----------------------

@app.route("/payment-methods")
def payment_methods_list():
    methods = PaymentMethod.query.order_by(PaymentMethod.name).all()
    return render_template(
        "payment_methods.html",
        payment_methods=methods
    )


@app.route("/payment-methods/new", methods=["POST"])
def payment_methods_new():
    name = (request.form.get("name") or "").strip()

    if not name:
        flash("Debes ingresar el nombre del medio de pago.", "danger")
        return redirect(url_for("payment_methods_list"))

    name = " ".join(name.split())

    existing = PaymentMethod.query.filter_by(name=name).first()
    if existing:
        existing.is_active = True
        db.session.commit()
        flash("El medio de pago ya existía y fue activado.", "info")
        return redirect(url_for("payment_methods_list"))

    db.session.add(PaymentMethod(name=name, is_active=True))
    db.session.commit()
    flash("Medio de pago creado.", "success")
    return redirect(url_for("payment_methods_list"))


@app.route("/payment-methods/<int:method_id>/toggle", methods=["POST"])
def payment_methods_toggle(method_id):
    pm = PaymentMethod.query.get_or_404(method_id)
    pm.is_active = not pm.is_active
    db.session.commit()
    flash("Medio de pago actualizado.", "info")
    return redirect(url_for("payment_methods_list"))

# -----------------------
# VEHICLE TYPES (CRUD)
# -----------------------

@app.route("/vehicle-types")
def vehicle_types_list():
    vehicle_types = VehicleType.query.order_by(VehicleType.name).all()
    return render_template(
        "vehicle_types.html",
        vehicle_types=vehicle_types
    )


@app.route("/vehicle-types/new", methods=["POST"])
def vehicle_types_new():
    name = (request.form.get("name") or "").strip()

    if not name:
        flash("Debes ingresar el nombre del tipo de vehículo.", "danger")
        return redirect(url_for("vehicle_types_list"))

    name = " ".join(name.split())

    existing = VehicleType.query.filter_by(name=name).first()
    if existing:
        existing.is_active = True
        db.session.commit()
        flash("El tipo de vehículo ya existía y fue activado.", "info")
        return redirect(url_for("vehicle_types_list"))

    db.session.add(VehicleType(name=name, is_active=True))
    db.session.commit()
    flash("Tipo de vehículo creado.", "success")
    return redirect(url_for("vehicle_types_list"))


@app.route("/vehicle-types/<int:vehicle_type_id>/toggle", methods=["POST"])
def vehicle_types_toggle(vehicle_type_id):
    vt = VehicleType.query.get_or_404(vehicle_type_id)
    vt.is_active = not vt.is_active
    db.session.commit()
    flash("Tipo de vehículo actualizado.", "info")
    return redirect(url_for("vehicle_types_list"))

# -----------------------
# SERVICE PRICES (CRUD)
# -----------------------

@app.route("/service-prices")
def service_prices_list():
    service_prices = (
        ServicePrice.query
        .join(Service)
        .join(VehicleType)
        .order_by(Service.name, VehicleType.name)
        .all()
    )

    services = Service.query.filter_by(is_active=True).order_by(Service.name).all()
    vehicle_types = VehicleType.query.filter_by(is_active=True).order_by(VehicleType.name).all()

    return render_template(
        "service_prices.html",
        service_prices=service_prices,
        services=services,
        vehicle_types=vehicle_types
    )


@app.route("/service-prices/new", methods=["POST"])
def service_prices_new():
    service_id = request.form.get("service_id")
    vehicle_type_id = request.form.get("vehicle_type_id")
    price = request.form.get("price")
    duration = request.form.get("duration_minutes")

    if not service_id or not vehicle_type_id or not price or not duration:
        flash("Debes completar todos los campos.", "danger")
        return redirect(url_for("service_prices_list"))

    try:
        price = int(price)
        duration = int(duration)
    except ValueError:
        flash("Precio y duración deben ser números enteros.", "danger")
        return redirect(url_for("service_prices_list"))

    existing = ServicePrice.query.filter_by(
        service_id=service_id,
        vehicle_type_id=vehicle_type_id
    ).first()

    if existing:
        existing.price = price
        existing.duration_minutes = duration
        existing.is_active = True
        flash("Precio actualizado para esta combinación.", "info")
    else:
        sp = ServicePrice(
            service_id=service_id,
            vehicle_type_id=vehicle_type_id,
            price=price,
            duration_minutes=duration,
            is_active=True
        )
        db.session.add(sp)
        flash("Precio creado.", "success")

    db.session.commit()
    return redirect(url_for("service_prices_list"))


@app.route("/service-prices/<int:price_id>/toggle", methods=["POST"])
def service_prices_toggle(price_id):
    sp = ServicePrice.query.get_or_404(price_id)
    sp.is_active = not sp.is_active
    db.session.commit()
    flash("Registro actualizado.", "info")
    return redirect(url_for("service_prices_list"))

# -----------------------
# RUTAS
# -----------------------
@app.route("/")
def index():
    return redirect(url_for("calendar_view"))


@app.route("/calendar")
def calendar_view():
    """Vista principal con el calendario."""
    return render_template("calendar.html")


@app.route("/appointments/new", methods=["GET", "POST"])
def new_appointment():
    services = Service.query.filter_by(is_active=True).order_by(Service.name).all()
    vehicle_types = VehicleType.query.filter_by(is_active=True).order_by(VehicleType.name).all()

    if request.method == "POST":
        customer_name = request.form.get("customer_name") or "Sin nombre"
        plate = normalize_plate(request.form.get("plate") or "")
        phone = request.form.get("phone") or ""
        date_str = request.form.get("date")
        time_str = request.form.get("start_time")
        notes = request.form.get("notes") or ""
        selected_ids = request.form.getlist("service_ids")
        vehicle_type_id = request.form.get("vehicle_type_id")

        if not date_str or not time_str:
            flash("Debes seleccionar fecha y hora.", "danger")
            return redirect(url_for("new_appointment"))

        if not selected_ids:
            flash("Debes seleccionar al menos un servicio.", "danger")
            return redirect(url_for("new_appointment"))

        if not vehicle_type_id:
            flash("Debes seleccionar el tipo de vehículo.", "danger")
            return redirect(url_for("new_appointment"))

        # Convertir fecha/hora
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")

        # Traer servicios seleccionados
        int_ids = [int(x) for x in selected_ids]
        selected_services = Service.query.filter(Service.id.in_(int_ids)).all()

        if not selected_services:
            flash("Los servicios seleccionados no son válidos.", "danger")
            return redirect(url_for("new_appointment"))

        service_ids = [s.id for s in selected_services]

        total_minutes = calculate_real_duration_minutes(
            service_ids=service_ids,
            vehicle_type_id=int(vehicle_type_id)
        )

        estimated_price = calculate_real_price(
            service_ids=service_ids,
            vehicle_type_id=int(vehicle_type_id)
        )

        end_dt = start_dt + timedelta(minutes=total_minutes)

        services_str = ", ".join(s.name for s in selected_services)

        # Guardar/actualizar datos del cliente por placa
        upsert_client_from_appointment(plate=plate, full_name=customer_name, phone=phone)

        appt = Appointment(
            customer_name=customer_name,
            plate=plate,
            phone=phone,
            services=services_str,
            start_datetime=start_dt,
            end_datetime=end_dt,
            notes=notes,
            vehicle_type_id=int(vehicle_type_id),
        )
        db.session.add(appt)
        db.session.commit()

        flash("Cita creada correctamente.", "success")
        return redirect(url_for("calendar_view"))

    return render_template(
        "new_appointment.html",
        services=services,
        vehicle_types=vehicle_types,
        today=date.today().isoformat()
    )


@app.route("/appointments")
def appointments_list():
    """Lista simple en tabla de las próximas citas."""
    appointments = Appointment.query.order_by(Appointment.start_datetime.asc()).all()
    return render_template("appointments_list.html", appointments=appointments)


@app.route("/appointments/<int:appointment_id>/delete", methods=["POST"])
def delete_appointment(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    db.session.delete(appt)
    db.session.commit()
    flash("Cita eliminada.", "info")
    return redirect(url_for("appointments_list"))

@app.route("/appointment/<int:appointment_id>/edit", methods=["GET", "POST"])
def edit_appointment(appointment_id):
    appointment = Appointment.query.get_or_404(appointment_id)
    services = Service.query.filter_by(is_active=True).all()

    if request.method == "POST":
        # Campos básicos
        appointment.customer_name = request.form["customer_name"]
        appointment.plate = normalize_plate(request.form["plate"])
        appointment.phone = request.form.get("phone") or ""
        appointment.notes = request.form["notes"]

        # Fecha y hora
        date = request.form["date"]
        start_time = request.form["start_time"]
        start_dt = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
        appointment.start_datetime = start_dt

        # Servicios seleccionados
        selected_ids = request.form.getlist("service_ids")
        selected_services = Service.query.filter(Service.id.in_(selected_ids)).all()
        
        # Guardar en texto (como antes)
        appointment.services = ", ".join([s.name for s in selected_services])

        # Calcular duración
        service_ids = [s.id for s in selected_services]

        if appointment.vehicle_type_id:
            total_duration = calculate_real_duration_minutes(
                service_ids=service_ids,
                vehicle_type_id=appointment.vehicle_type_id
            )
        else:
            # fallback si la cita es antigua y no tiene tipo de vehículo
            durations = [s.duration_minutes for s in selected_services]
            if durations:
                longest = max(durations)
                extras = sum(durations) - longest
                total_duration = longest + int(extras * 0.5)
            else:
                total_duration = 60

        # Asignar nueva hora final
        appointment.end_datetime = appointment.start_datetime + timedelta(minutes=total_duration)

        # Guardar/actualizar datos del cliente por placa (si hay placa)
        upsert_client_from_appointment(plate=appointment.plate, full_name=appointment.customer_name, phone=appointment.phone)

        db.session.commit()
        flash("Cita actualizada correctamente.", "success")
        return redirect(url_for("appointments_list"))

    return render_template("edit_appointment.html", appointment=appointment, services=services)


@app.route("/services", methods=["GET", "POST"])
def services_view():
    """Gestión simple de servicios: ver y agregar nuevos."""
    if request.method == "POST":
        name = request.form.get("name")
        duration = request.form.get("duration_minutes")

        if not name or not duration:
            flash("Debes ingresar nombre y duración.", "danger")
        else:
            try:
                duration = int(duration)
                s = Service(name=name, duration_minutes=duration, is_active=True)
                db.session.add(s)
                db.session.commit()
                flash("Servicio creado.", "success")
            except ValueError:
                flash("La duración debe ser un número entero de minutos.", "danger")

        return redirect(url_for("services_view"))

    services = Service.query.order_by(Service.name).all()
    return render_template("services.html", services=services)


@app.route("/services/<int:service_id>/toggle", methods=["POST"])
def toggle_service(service_id):
    s = Service.query.get_or_404(service_id)
    s.is_active = not s.is_active
    db.session.commit()
    flash("Servicio actualizado.", "info")
    return redirect(url_for("services_view"))


# -----------------------
# GASTOS (MÓDULO MVP)
# -----------------------
EXPENSE_CATEGORIES_DEFAULT = [
    "Inventario",
    "Arriendo",
    "Servicios Publicos",
    "Nomina",
    "Arreglos locativos",
    "Caja menor",
]

PAYMENT_METHODS = [
    "Efectivo",
    "Transferencia",
    "Tarjeta",
    "Crédito",
    "Otro",
]


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None



@app.route("/expenses")
def expenses_list():
    """Listado de gastos con filtros (sin límite) y búsqueda simple."""
    q = (request.args.get("q") or "").strip()
    from_str = request.args.get("from")
    to_str = request.args.get("to")
    category = (request.args.get("category") or "").strip()
    payment_method = (request.args.get("payment_method") or "").strip()

    date_from = _parse_date(from_str)
    date_to = _parse_date(to_str)

    query = Expense.query

    if date_from:
        query = query.filter(Expense.expense_date >= date_from)
    if date_to:
        query = query.filter(Expense.expense_date <= date_to)
    if category:
        query = query.filter(Expense.category == category)
    if payment_method:
        query = query.filter(Expense.payment_method == payment_method)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Expense.description.ilike(like))
            | (Expense.vendor.ilike(like))
            | (Expense.receipt.ilike(like))
            | (Expense.notes.ilike(like))
        )

    expenses = query.order_by(Expense.expense_date.desc(), Expense.created_at.desc()).all()

    return render_template(
        "expenses_list.html",
        expenses=expenses,
        categories=[c.name for c in ExpenseCategory.query.filter_by(is_active=True).order_by(ExpenseCategory.name).all()],
        categories_all=ExpenseCategory.query.order_by(ExpenseCategory.name).all(),
        payment_methods=PAYMENT_METHODS,
        filters={
            "q": q,
            "from": from_str or "",
            "to": to_str or "",
            "category": category,
            "payment_method": payment_method,
        },
    )



# -----------------------
# Listado de ingresos (ventas de servicios) con filtros básicos
# -----------------------
@app.route("/sales")
def sales_list():
    """Listado de ingresos (ventas de servicios) con filtros básicos."""
    from_str = request.args.get("from")
    to_str = request.args.get("to")
    status = (request.args.get("status") or "").strip()
    payment_method = (request.args.get("payment_method") or "").strip()

    date_from = _parse_date(from_str)
    date_to = _parse_date(to_str)

    query = ServiceSale.query

    if date_from:
        query = query.filter(ServiceSale.service_date >= date_from)
    if date_to:
        query = query.filter(ServiceSale.service_date <= date_to)
    if status:
        query = query.filter(ServiceSale.status == status)
    if payment_method:
        query = query.filter(ServiceSale.payment_method == payment_method)

    sales = query.order_by(
        ServiceSale.service_date.desc(),
        ServiceSale.created_at.desc()
    ).all()

    return render_template(
        "service_sales_list.html",
        sales=sales,
        filters={
            "from": from_str or "",
            "to": to_str or "",
            "status": status,
            "payment_method": payment_method,
        }
    )


# -----------------------
# Export CSV de ingresos (service_sales) con los mismos filtros del listado.
# -----------------------
@app.route("/sales/export")
def sales_export():
    """Export CSV de ingresos (service_sales) con los mismos filtros del listado."""
    from_str = request.args.get("from")
    to_str = request.args.get("to")
    status = (request.args.get("status") or "").strip()
    payment_method = (request.args.get("payment_method") or "").strip()

    date_from = _parse_date(from_str)
    date_to = _parse_date(to_str)

    query = ServiceSale.query

    if date_from:
        query = query.filter(ServiceSale.service_date >= date_from)
    if date_to:
        query = query.filter(ServiceSale.service_date <= date_to)
    if status:
        query = query.filter(ServiceSale.status == status)
    if payment_method:
        query = query.filter(ServiceSale.payment_method == payment_method)

    sales = query.order_by(
        ServiceSale.service_date.asc(),
        ServiceSale.created_at.asc()
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header BI-friendly
    writer.writerow([
        "service_date",
        "created_at",
        "appointment_id",
        "vehicle_type",
        "plate",
        "customer_name",
        "services",
        "base_amount",
        "discount_amount",
        "final_amount",
        "payment_method",
        "status",
        "notes",
    ])

    for s in sales:
        writer.writerow([
            s.service_date.strftime("%Y-%m-%d") if s.service_date else "",
            s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "",
            s.appointment_id,
            s.vehicle_type,
            s.plate or "",
            s.customer_name or "",
            s.services or "",
            s.base_amount,
            s.discount_amount,
            s.final_amount,
            s.payment_method or "",
            s.status,
            s.notes or "",
        ])

    filename = "service_sales_export.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/expenses/new", methods=["GET", "POST"])
def expenses_new():
    if request.method == "POST":
        expense_date_str = request.form.get("expense_date")
        category = (request.form.get("category") or "").strip()
        payment_method = (request.form.get("payment_method") or "").strip()
        vendor = (request.form.get("vendor") or "").strip()
        vendor_other = (request.form.get("vendor_other") or "").strip()

        if vendor == "__other__":
            if not vendor_other:
                flash("Debes especificar el proveedor.", "danger")
                return redirect(url_for("expenses_new"))
            vendor = vendor_other

        description = (request.form.get("description") or "").strip()
        receipt = (request.form.get("receipt") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        amount_str = (request.form.get("amount") or "").strip().replace(",", ".")

        expense_date = _parse_date(expense_date_str)
        if not expense_date:
            flash("Debes ingresar una fecha de gasto válida.", "danger")
            return redirect(url_for("expenses_new"))

        if not category:
            flash("Debes seleccionar una categoría.", "danger")
            return redirect(url_for("expenses_new"))

        if not payment_method:
            flash("Debes seleccionar un método de pago.", "danger")
            return redirect(url_for("expenses_new"))

        if not description:
            flash("Debes ingresar una descripción.", "danger")
            return redirect(url_for("expenses_new"))

        if category.strip().lower() == "caja menor":
            if len((notes or "").strip()) < 5:
                flash("Para 'Caja menor', las notas son obligatorias (mínimo 5 caracteres).", "danger")
                return redirect(url_for("expenses_new"))

        try:
            amount = Decimal(amount_str)
        except Exception:
            flash("Monto inválido. Ej: 45000 o 45000.50", "danger")
            return redirect(url_for("expenses_new"))

        if amount <= 0:
            flash("El monto debe ser mayor a 0.", "danger")
            return redirect(url_for("expenses_new"))

        exp = Expense(
            expense_date=expense_date,
            amount=amount,
            category=category,
            payment_method=payment_method,
            vendor=vendor or None,
            description=description,
            receipt=receipt or None,
            notes=notes or None,
        )
        db.session.add(exp)
        db.session.commit()

        flash("Gasto registrado.", "success")
        return redirect(url_for("expenses_list"))

    # Precargar fecha con hoy (editable)
    return render_template(
        "expenses_new.html",
        categories=[c.name for c in ExpenseCategory.query.filter_by(is_active=True).order_by(ExpenseCategory.name).all()],
        payment_methods=PAYMENT_METHODS,
        today=date.today().strftime("%Y-%m-%d"),
        vendors=get_existing_vendors()
    )


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
def expenses_edit(expense_id):
    exp = Expense.query.get_or_404(expense_id)

    if request.method == "POST":
        expense_date = _parse_date(request.form.get("expense_date"))
        category = (request.form.get("category") or "").strip()
        payment_method = (request.form.get("payment_method") or "").strip()
        vendor = (request.form.get("vendor") or "").strip()
        vendor_other = (request.form.get("vendor_other") or "").strip()

        if vendor == "__other__":
            if not vendor_other:
                flash("Debes especificar el proveedor.", "danger")
                return redirect(url_for("expenses_edit", expense_id=expense_id))
            vendor = vendor_other

        description = (request.form.get("description") or "").strip()
        receipt = (request.form.get("receipt") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        amount_str = (request.form.get("amount") or "").strip().replace(",", ".")

        if not expense_date:
            flash("Debes ingresar una fecha de gasto válida.", "danger")
            return redirect(url_for("expenses_edit", expense_id=expense_id))

        if not category or not payment_method or not description:
            flash("Categoría, método de pago y descripción son obligatorios.", "danger")
            return redirect(url_for("expenses_edit", expense_id=expense_id))

        if category.strip().lower() == "caja menor":
            if len((notes or "").strip()) < 5:
                flash("Para 'Caja menor', las notas son obligatorias (mínimo 5 caracteres).", "danger")
                return redirect(url_for("expenses_edit", expense_id=expense_id))

        try:
            amount = Decimal(amount_str)
        except Exception:
            flash("Monto inválido. Ej: 45000 o 45000.50", "danger")
            return redirect(url_for("expenses_edit", expense_id=expense_id))

        if amount <= 0:
            flash("El monto debe ser mayor a 0.", "danger")
            return redirect(url_for("expenses_edit", expense_id=expense_id))

        exp.expense_date = expense_date
        exp.amount = amount
        exp.category = category
        exp.payment_method = payment_method
        exp.vendor = vendor or None
        exp.description = description
        exp.receipt = receipt or None
        exp.notes = notes or None

        db.session.commit()
        flash("Gasto actualizado.", "success")
        return redirect(url_for("expenses_list"))

    return render_template(
        "expenses_edit.html",
        expense=exp,
        categories=[c.name for c in ExpenseCategory.query.filter_by(is_active=True).order_by(ExpenseCategory.name).all()],
        payment_methods=PAYMENT_METHODS,
        vendors=get_existing_vendors()
    )





# Nueva ruta para anular/des-anular un gasto
@app.route("/expenses/<int:expense_id>/toggle-void", methods=["POST"])
def expenses_toggle_void(expense_id):
    exp = Expense.query.get_or_404(expense_id)

    exp.is_void = not exp.is_void

    if exp.is_void:
        flash("Gasto anulado.", "warning")
    else:
        flash("Gasto reactivado.", "success")

    db.session.commit()
    return redirect(url_for("expenses_list"))


@app.route("/expenses/export")
def expenses_export():
    """Export CSV por filtros (para Google Sheets / Looker Studio)."""
    q = (request.args.get("q") or "").strip()
    from_str = request.args.get("from")
    to_str = request.args.get("to")
    category = (request.args.get("category") or "").strip()
    payment_method = (request.args.get("payment_method") or "").strip()

    date_from = _parse_date(from_str)
    date_to = _parse_date(to_str)

    query = Expense.query
    if date_from:
        query = query.filter(Expense.expense_date >= date_from)
    if date_to:
        query = query.filter(Expense.expense_date <= date_to)
    if category:
        query = query.filter(Expense.category == category)
    if payment_method:
        query = query.filter(Expense.payment_method == payment_method)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Expense.description.ilike(like))
            | (Expense.vendor.ilike(like))
            | (Expense.receipt.ilike(like))
            | (Expense.notes.ilike(like))
        )

    expenses = query.order_by(Expense.expense_date.asc(), Expense.created_at.asc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "expense_date",
        "created_at",
        "amount",
        "category",
        "payment_method",
        "vendor",
        "description",
        "receipt",
        "notes",
        "is_void",
    ])

    for e in expenses:
        writer.writerow([
            e.expense_date.strftime("%Y-%m-%d") if e.expense_date else "",
            e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
            f"{e.amount}" if e.amount is not None else "",
            e.category or "",
            e.payment_method or "",
            e.vendor or "",
            e.description or "",
            e.receipt or "",
            e.notes or "",
            "1" if e.is_void else "0",
        ])

    filename = "expenses_export.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# -----------------------
# Gestión de categorías de gastos
# -----------------------

@app.route("/expense-categories/new", methods=["POST"])
def expense_categories_new():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Debes ingresar el nombre de la categoría.", "danger")
        return redirect(url_for("expenses_list"))

    # Normalizar espacios múltiples
    name = " ".join(name.split())

    existing = ExpenseCategory.query.filter_by(name=name).first()
    if existing:
        existing.is_active = True
        db.session.commit()
        flash("La categoría ya existía y fue activada.", "info")
        return redirect(url_for("expenses_list"))

    db.session.add(ExpenseCategory(name=name, is_active=True))
    db.session.commit()
    flash("Categoría creada.", "success")
    return redirect(url_for("expenses_list"))


@app.route("/expense-categories/<int:category_id>/toggle", methods=["POST"])
def expense_categories_toggle(category_id):
    c = ExpenseCategory.query.get_or_404(category_id)
    c.is_active = not c.is_active
    db.session.commit()
    flash("Categoría actualizada.", "info")
    return redirect(url_for("expenses_list"))

# -----------------------
# API PARA FULLCALENDAR
# -----------------------
@app.route("/api/events")
def api_events():
    """Devuelve las citas en formato JSON para FullCalendar."""
    appointments = Appointment.query.all()
    events = []

    for appt in appointments:
        # Definir el color según el PRIMER servicio listado
        first_service = appt.services.split(",")[0].strip().lower()
        color = COLORS.get(first_service, "#A0C8FF")  # color por defecto pastel

        # Primer nombre
        first_name = ""
        if appt.customer_name:
            first_name = appt.customer_name.strip().split(" ")[0]

        # Placa
        plate = appt.plate.upper() if appt.plate else ""

        # Observaciones
        notes = (appt.notes or "").strip()

        # Construcción del título (líneas separadas)
        title_lines = []

        if first_name:
            title_lines.append(first_name)

        if plate:
            title_lines.append(plate)

        if notes:
            title_lines.append(notes)

        title = "\n".join(title_lines)

        events.append(
            {
                "id": appt.id,
                "title": title,
                "start": appt.start_datetime.isoformat(),
                "end": appt.end_datetime.isoformat(),
                "backgroundColor": color,
                "borderColor": color,
            }
        )

    return jsonify(events)


@app.route("/appointment/<int:appointment_id>/json")
def appointment_json(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    return jsonify({
        "id": appt.id,
        "customer_name": appt.customer_name,
        "plate": appt.plate,
        "phone": appt.phone,
        "services": appt.services,
        "notes": appt.notes,
        "start": appt.start_datetime.strftime("%Y-%m-%d %H:%M"),
        "end": appt.end_datetime.strftime("%Y-%m-%d %H:%M"),
    })


# -----------------------
# API: CLIENT BY PLATE
# -----------------------
@app.route("/api/clients/by-plate")
def api_client_by_plate():
    """
    Devuelve datos de cliente por placa.
    Uso: /api/clients/by-plate?plate=ABC123
    """
    plate = normalize_plate(request.args.get("plate") or "")
    if not plate:
        return jsonify({"found": False}), 400

    client = Client.query.get(plate)
    if not client:
        return jsonify({"found": False, "plate": plate})

    return jsonify({
        "found": True,
        "plate": client.plate,
        "full_name": client.full_name or "",
        "phone": client.phone or "",
    })


# -----------------------
# API: ESTIMAR PRECIO DE CITA
# -----------------------
@app.route("/api/estimate-price", methods=["POST"])
def api_estimate_price():
    """
    Calcula el precio estimado según:
    - servicios seleccionados
    - tipo de vehículo
    No guarda nada en BD.
    """
    data = request.get_json(silent=True) or {}

    service_ids = data.get("service_ids") or []
    vehicle_type_id = data.get("vehicle_type_id")

    try:
        service_ids = [int(sid) for sid in service_ids]
        vehicle_type_id = int(vehicle_type_id)
    except Exception:
        return jsonify({"ok": False, "error": "Datos inválidos"}), 400

    if not service_ids or not vehicle_type_id:
        return jsonify({"ok": False, "error": "Datos incompletos"}), 400

    price = calculate_real_price(
        service_ids=service_ids,
        vehicle_type_id=vehicle_type_id
    )

    return jsonify({
        "ok": True,
        "price": price
    })


# -----------------------
# INICIALIZACIÓN
# -----------------------
with app.app_context():
    db.create_all()
    seed_services()
    seed_vehicle_types()
    seed_payment_methods()
    seed_expense_categories()


if __name__ == "__main__":
    app.run(debug=True)
@app.route("/appointments/<int:appointment_id>/close", methods=["POST"])
def close_appointment(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)

    if appointment_already_closed(appointment_id):
        return jsonify({
            "ok": False,
            "error": "La cita ya fue cerrada."
        }), 400

    data = request.get_json(silent=True) or {}

    payment_method = (data.get("payment_method") or "").strip()
    status = (data.get("status") or "").strip()  # completed | cancelled
    discount = int(data.get("discount") or 0)
    notes = (data.get("notes") or "").strip()

    if status not in ("completed", "cancelled"):
        return jsonify({"ok": False, "error": "Estado inválido"}), 400

    if status == "completed" and not payment_method:
        return jsonify({"ok": False, "error": "Medio de pago requerido"}), 400

    # Resolver servicios por nombre
    service_names = [s.strip() for s in appt.services.split(",") if s.strip()]
    services = Service.query.filter(Service.name.in_(service_names)).all()
    service_ids = [s.id for s in services]

    # Precio base real
    base_amount = calculate_real_price(
        service_ids=service_ids,
        vehicle_type_id=appt.vehicle_type_id
    )

    discount_amount = max(discount, 0)
    final_amount = max(base_amount - discount_amount, 0)

    vt_name = appt.vehicle_type.name if appt.vehicle_type else "N/A"

    sale = ServiceSale(
        appointment_id=appt.id,
        service_date=appt.start_datetime.date(),
        vehicle_type=vt_name,
        plate=appt.plate,
        customer_name=appt.customer_name,
        services=appt.services,
        base_amount=base_amount,
        discount_amount=discount_amount,
        final_amount=final_amount,
        payment_method=payment_method if status == "completed" else None,
        status=status,
        notes=notes or None
    )

    db.session.add(sale)
    db.session.commit()

    return jsonify({"ok": True})