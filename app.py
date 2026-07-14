from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response, session, g
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from flask_sqlalchemy import SQLAlchemy
import os
import csv
import io
import re
import time
import base64
import requests
from decimal import Decimal
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

COLORS = {
    "wash essential":           "#FFF3B0",
    "wash shine":               "#FFD6E0",
    "wash chasis":              "#D9E4F5",
    "wash motor":               "#FFFFFF",
    "detallado exterior":       "#B5EAD7",
    "detallado interior":       "#C3E5FF",
    "detallado llanta a llanta":"#E2D9F3",
    "polichado":                "#DCD0FF",
    "correccion de wrap":       "#FFE8CC",
    "porcelanizado":            "#D6F5D6",
    "coating ceramico 7h+":     "#C0392B",
    "coating ceramico 9h":      "#7B0000",
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

def ensure_appointments_agreement_schema():
    with app.app_context():
        try:
            db.session.execute(text("SELECT agreement_id FROM appointments LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE appointments ADD COLUMN agreement_id INTEGER")
            )
            db.session.commit()

ensure_appointments_agreement_schema()

# --- Ensure service_sales table exists ---
def ensure_service_sales_schema():
    with app.app_context():
        try:
            db.session.execute(text("SELECT id FROM service_sales LIMIT 1"))
        except Exception:
            ServiceSale.__table__.create(db.engine)

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
# AGREEMENTS / CONVENIOS (CRUD)
# -----------------------

@app.route("/agreements")
def agreements_list():
    agreements = Agreement.query.order_by(Agreement.name).all()
    return render_template(
        "agreements.html",
        agreements=agreements
    )

@app.route("/agreements/new", methods=["POST"])
def agreements_new():
    name = (request.form.get("name") or "").strip()
    discount_type = request.form.get("discount_type")
    value = request.form.get("value")

    # Normalizar tipo de descuento: 'fixed' -> 'absolute'
    if discount_type == "fixed":
        discount_type = "absolute"

    if not name or discount_type not in ("percentage", "absolute") or not value:
        flash("Debes completar todos los campos del convenio.", "danger")
        return redirect(url_for("agreements_list"))

    try:
        value = int(value)
    except ValueError:
        flash("El valor del descuento debe ser numérico.", "danger")
        return redirect(url_for("agreements_list"))

    existing = Agreement.query.filter_by(name=name).first()
    if existing:
        existing.discount_type = discount_type
        existing.value = value
        existing.is_active = True
        db.session.commit()
        return redirect(url_for("agreements_list"))

    db.session.add(
        Agreement(
            name=name,
            discount_type=discount_type,
            value=value,
            is_active=True
        )
    )
    db.session.commit()
    return redirect(url_for("agreements_list"))


@app.route("/agreements/<int:agreement_id>/toggle", methods=["POST"])
def agreements_toggle(agreement_id):
    ag = Agreement.query.get_or_404(agreement_id)
    ag.is_active = not ag.is_active
    db.session.commit()
    return redirect(url_for("agreements_list"))

# --- BACKWARD-COMPATIBLE AGREEMENT CREATE ENDPOINT ---
@app.route("/api/agreements", methods=["POST"])
def agreements_create_alias():
    """
    Alias para compatibilidad con el frontend.
    Delega en /api/agreements/quick-create
    """
    return agreements_quick_create()

# --- QUICK CREATE AGREEMENT ENDPOINT (API) ---
@app.route("/api/agreements/quick-create", methods=["POST"])
def agreements_quick_create():
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    discount_type = data.get("discount_type")
    value = data.get("discount_value") or data.get("value")

    # Normalizar tipo
    if discount_type == "fixed":
        discount_type = "absolute"

    if not name or discount_type not in ("percentage", "absolute") or value in (None, ""):
        return jsonify({"ok": False, "error": "Datos incompletos"}), 400

    try:
        value = int(value)
    except Exception:
        return jsonify({"ok": False, "error": "Valor inválido"}), 400

    existing = Agreement.query.filter_by(name=name).first()
    if existing:
        existing.discount_type = discount_type
        existing.value = value
        existing.is_active = True
        db.session.commit()

        return jsonify({
            "ok": True,
            "agreement": {
                "id": existing.id,
                "name": existing.name,
                "discount_type": existing.discount_type,
                "value": existing.value
            }
        })

    ag = Agreement(
        name=name,
        discount_type=discount_type,
        value=value,
        is_active=True
    )
    db.session.add(ag)
    db.session.commit()

    return jsonify({
        "ok": True,
        "agreement": {
            "id": ag.id,
            "name": ag.name,
            "discount_type": ag.discount_type,
            "value": ag.value
        }
    })

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
# AGREEMENTS / CONVENIOS
# -----------------------
class Agreement(db.Model):
    __tablename__ = "agreements"
    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(120), nullable=False, unique=True)

    # percentage | absolute
    discount_type = db.Column(db.String(20), nullable=False)

    # valor del descuento (ej: 10 para %, 20000 para absoluto)
    value = db.Column(db.Integer, nullable=False)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<Agreement {self.name} {self.discount_type} {self.value}>"
# -----------------------
# Normalización defensiva de discount_type en convenios
# -----------------------
def normalize_agreements_discount_type():
    with app.app_context():
        try:
            db.session.execute(text(
                "UPDATE agreements SET discount_type='absolute' WHERE discount_type='fixed'"
            ))
            db.session.commit()
        except Exception:
            pass

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

    agreement_id = db.Column(
        db.Integer,
        db.ForeignKey("agreements.id"),
        nullable=True
    )
    booking_adjustment_type  = db.Column(db.String(20), nullable=True)
    booking_adjustment_mode  = db.Column(db.String(20), nullable=True)
    booking_adjustment_value = db.Column(db.Integer,    nullable=True)

    agreement = db.relationship("Agreement")

    vehicle_type = db.relationship("VehicleType")

    # Estado de la cita: scheduled | completed | cancelled
    status = db.Column(db.String(20), nullable=False, default="scheduled")

    # Timing real del trabajo: pending | in_progress | paused | done
    work_status         = db.Column(db.String(20), nullable=False, default="pending")
    work_started_at     = db.Column(db.DateTime, nullable=True)
    work_paused_at      = db.Column(db.DateTime, nullable=True)
    work_ended_at       = db.Column(db.DateTime, nullable=True)
    total_pause_seconds = db.Column(db.Integer, nullable=False, default=0)

    # Notificaciones WhatsApp
    notif_reminder_sent  = db.Column(db.Boolean, default=False)  # recordatorio al admin 30 min antes
    notif_client_sent    = db.Column(db.Boolean, default=False)  # recordatorio al cliente día anterior
    notif_ceramic_sent   = db.Column(db.Boolean, default=False)  # seguimiento cerámico 3 meses

    operator_assignments = db.relationship(
        "AppointmentOperator", cascade="all, delete-orphan", lazy="joined"
    )

    def __repr__(self):
        return f"<Appointment {self.customer_name} - {self.services}>"

# --- Ensure appointments schema migration for status column ---
def ensure_appointments_status_schema():
    with app.app_context():
        try:
            db.session.execute(text("SELECT status FROM appointments LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE appointments ADD COLUMN status VARCHAR(20) DEFAULT 'scheduled'")
            )
            db.session.commit()

ensure_appointments_status_schema()

# --- Ensure appointments schema migration for close columns ---
def ensure_appointments_close_schema():
    with app.app_context():
        cols = [
            ("payment_method", "VARCHAR(80)"),
            ("closed_at", "DATETIME"),
            ("adjustment_type", "VARCHAR(20)"),
            ("adjustment_mode", "VARCHAR(20)"),
            ("adjustment_value", "INTEGER"),
            ("adjustment_reason", "TEXT"),
            ("final_amount", "INTEGER"),
            ("booking_adjustment_type", "VARCHAR(20)"),
            ("booking_adjustment_mode", "VARCHAR(20)"),
            ("booking_adjustment_value", "INTEGER"),
        ]

        for col, ddl in cols:
            try:
                db.session.execute(text(f"SELECT {col} FROM appointments LIMIT 1"))
            except Exception:
                db.session.execute(
                    text(f"ALTER TABLE appointments ADD COLUMN {col} {ddl}")
                )
        db.session.commit()

# --- Migración: columnas de timing de trabajo en appointments ---
def ensure_appointment_work_schema():
    with app.app_context():
        cols = [
            ("work_status",         "VARCHAR(20) DEFAULT 'pending'"),
            ("work_started_at",     "DATETIME"),
            ("work_paused_at",      "DATETIME"),
            ("work_ended_at",       "DATETIME"),
            ("total_pause_seconds", "INTEGER DEFAULT 0"),
        ]
        for col, ddl in cols:
            try:
                db.session.execute(text(f"SELECT {col} FROM appointments LIMIT 1"))
            except Exception:
                db.session.execute(
                    text(f"ALTER TABLE appointments ADD COLUMN {col} {ddl}")
                )
        db.session.commit()

ensure_appointment_work_schema()

def ensure_appointment_notif_schema():
    with app.app_context():
        for col, ddl in [
            ("notif_reminder_sent", "BOOLEAN DEFAULT 0"),
            ("notif_client_sent",   "BOOLEAN DEFAULT 0"),
            ("notif_ceramic_sent",  "BOOLEAN DEFAULT 0"),
        ]:
            try:
                db.session.execute(text(f"SELECT {col} FROM appointments LIMIT 1"))
            except Exception:
                db.session.execute(
                    text(f"ALTER TABLE appointments ADD COLUMN {col} {ddl}")
                )
        db.session.commit()

ensure_appointment_notif_schema()

# --- Migración: tabla appointment_operators ---
def ensure_appointment_operators_schema():
    with app.app_context():
        try:
            db.session.execute(text("SELECT id FROM appointment_operators LIMIT 1"))
        except Exception:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS appointment_operators (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
                    user_id INTEGER NOT NULL REFERENCES users(id)
                )
            """))
            db.session.commit()

ensure_appointment_operators_schema()

# -----------------------
# SERVICE SALES (INGRESOS / BI)
# -----------------------
class ServiceSale(db.Model):
    __tablename__ = "service_sales"
    id = db.Column(db.Integer, primary_key=True)

    appointment_id = db.Column(
        db.Integer,
        db.ForeignKey("appointments.id"),
        nullable=True
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
    vehicle_type_id = db.Column(db.Integer, nullable=True)
    agreement_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Client {self.plate} {self.full_name}>"


# -----------------------
# USER MODEL
# -----------------------
class User(db.Model):
    __tablename__ = "users"
    id           = db.Column(db.Integer, primary_key=True)
    username     = db.Column(db.String(80), nullable=False, unique=True)
    password_hash= db.Column(db.String(256), nullable=False)
    # admin | lider | operario
    role         = db.Column(db.String(20), nullable=False, default="operario")
    is_active    = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # True = debe cambiar contraseña en el próximo login
    must_change_password = db.Column(db.Boolean, default=False)

    # Nómina
    salary          = db.Column(db.Integer, default=0)
    is_trial_period = db.Column(db.Boolean, default=False)  # override manual (legado)
    hire_date       = db.Column(db.Date, nullable=True)     # fecha real de ingreso

    @property
    def in_trial(self):
        """True si el empleado aún está en período de prueba (primer mes desde hire_date)."""
        if self.hire_date:
            return (date.today() - self.hire_date).days < 30
        return bool(self.is_trial_period)

    @property
    def trial_end_date(self):
        if self.hire_date:
            from datetime import timedelta
            return self.hire_date + timedelta(days=30)
        return None

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username} role={self.role}>"


class AppointmentOperator(db.Model):
    __tablename__ = "appointment_operators"
    id             = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False)
    user_id        = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user           = db.relationship("User")

    def __repr__(self):
        return f"<AppointmentOperator appt={self.appointment_id} user={self.user_id}>"


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
# PARKING MODEL
# -----------------------
class Parking(db.Model):
    __tablename__ = "parkings"
    id           = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=True)
    plate        = db.Column(db.String(20), nullable=False)
    parking_date = db.Column(db.Date, nullable=False, default=date.today)
    amount       = db.Column(db.Integer, nullable=False, default=7000)
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<Parking {self.parking_date} {self.plate}>"

# -----------------------
# NÓMINA
# -----------------------

class PayrollPeriod(db.Model):
    __tablename__ = "payroll_periods"
    id         = db.Column(db.Integer, primary_key=True)
    start_date = db.Column(db.Date, nullable=False)
    end_date   = db.Column(db.Date, nullable=False)
    # draft | paid
    status     = db.Column(db.String(20), nullable=False, default="draft")
    paid_at    = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    entries    = db.relationship("PayrollEntry", backref="period", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<PayrollPeriod {self.start_date}~{self.end_date} {self.status}>"


class PayrollEntry(db.Model):
    """Liquidación de un operario en una quincena."""
    __tablename__ = "payroll_entries"
    id            = db.Column(db.Integer, primary_key=True)
    period_id     = db.Column(db.Integer, db.ForeignKey("payroll_periods.id"), nullable=False)
    employee_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    # Salario base efectivo (salary - 100k si está en prueba)
    base_salary   = db.Column(db.Integer, nullable=False, default=0)
    # Bono calidad (máx 100k, 0 si en prueba). Se recalcula desde errores.
    bonus         = db.Column(db.Integer, nullable=False, default=0)
    # Bono extra por quincena perfecta (a discreción del admin)
    bonus_extra   = db.Column(db.Integer, nullable=False, default=0)

    # Descuentos
    absence_days        = db.Column(db.Integer, nullable=False, default=0)
    deduction_absences  = db.Column(db.Integer, nullable=False, default=0)
    deduction_vales     = db.Column(db.Integer, nullable=False, default=0)
    deduction_drinks    = db.Column(db.Integer, nullable=False, default=0)
    deduction_quality   = db.Column(db.Integer, nullable=False, default=0)
    deduction_other     = db.Column(db.Integer, nullable=False, default=0)
    deduction_other_notes = db.Column(db.String(300), nullable=True)

    total         = db.Column(db.Integer, nullable=False, default=0)
    notes         = db.Column(db.String(500), nullable=True)

    employee      = db.relationship("User")

    def recalculate(self):
        self.total = (
            self.base_salary
            + self.bonus
            + self.bonus_extra
            - self.deduction_absences
            - self.deduction_vales
            - self.deduction_drinks
            - self.deduction_quality
            - self.deduction_other
        )

    def __repr__(self):
        return f"<PayrollEntry period={self.period_id} emp={self.employee_id}>"


class QualityError(db.Model):
    """Error de calidad registrado por el admin."""
    __tablename__ = "quality_errors"
    id          = db.Column(db.Integer, primary_key=True)
    # leve | grave
    error_type  = db.Column(db.String(10), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Período al que pertenece (se asigna al liquidar, nullable hasta entonces)
    period_id   = db.Column(db.Integer, db.ForeignKey("payroll_periods.id"), nullable=True)

    assignments = db.relationship("QualityErrorEmployee", backref="error", lazy=True, cascade="all, delete-orphan")

    @property
    def unit_value(self):
        return 5000 if self.error_type == "leve" else 10000

    def __repr__(self):
        return f"<QualityError {self.error_type} {self.created_at}>"


class QualityErrorEmployee(db.Model):
    """Asignación de un error a uno o varios operarios (con monto dividido)."""
    __tablename__ = "quality_error_employees"
    id          = db.Column(db.Integer, primary_key=True)
    error_id    = db.Column(db.Integer, db.ForeignKey("quality_errors.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    deduction   = db.Column(db.Integer, nullable=False)  # monto descontado a este operario

    employee    = db.relationship("User")


class Vale(db.Model):
    """Vale de adelanto de un operario."""
    __tablename__ = "vales"
    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount      = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(300), nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Se asigna al período al liquidar
    period_id   = db.Column(db.Integer, db.ForeignKey("payroll_periods.id"), nullable=True)

    employee    = db.relationship("User")


class Conversation(db.Model):
    """Una conversación de WhatsApp por número de teléfono."""
    __tablename__ = "whatsapp_conversations"
    id           = db.Column(db.Integer, primary_key=True)
    phone        = db.Column(db.String(20), nullable=False, unique=True)
    profile_name = db.Column(db.String(120), nullable=True)
    bot_active   = db.Column(db.Boolean, nullable=False, default=True)
    followup_count = db.Column(db.Integer, nullable=False, default=0)
    status       = db.Column(db.String(40), nullable=False, default="En proceso")
    service_tag  = db.Column(db.String(120), nullable=False, default="")  # lista separada por comas, ej. "Cerámico,PPF o wrap"
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages   = db.relationship("Message", backref="conversation", order_by="Message.created_at")


class Message(db.Model):
    """Un mensaje individual, entrante o saliente, de una conversación."""
    __tablename__ = "whatsapp_messages"
    id              = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("whatsapp_conversations.id"), nullable=False)
    direction       = db.Column(db.String(10), nullable=False)  # "in" | "out"
    body            = db.Column(db.Text, nullable=False)
    created_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# --- Ensure whatsapp_conversations schema migration for profile_name ---
def ensure_whatsapp_schema():
    with app.app_context():
        db.create_all()  # crea whatsapp_conversations / whatsapp_messages si no existen
        try:
            db.session.execute(text("SELECT profile_name FROM whatsapp_conversations LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE whatsapp_conversations ADD COLUMN profile_name VARCHAR(120)")
            )
            db.session.commit()
        try:
            db.session.execute(text("SELECT followup_count FROM whatsapp_conversations LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE whatsapp_conversations ADD COLUMN followup_count INTEGER DEFAULT 0")
            )
            db.session.commit()
        try:
            db.session.execute(text("SELECT status FROM whatsapp_conversations LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE whatsapp_conversations ADD COLUMN status VARCHAR(40) DEFAULT 'En proceso'")
            )
            db.session.commit()
        try:
            db.session.execute(text("SELECT service_tag FROM whatsapp_conversations LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE whatsapp_conversations ADD COLUMN service_tag VARCHAR(40) DEFAULT 'Otro servicio'")
            )
            db.session.commit()

ensure_whatsapp_schema()


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
# SEED INICIAL DE CONVENIOS
# -----------------------
def seed_agreements():
    if Agreement.query.count() > 0:
        return

    agreements = [
        ("Club Mercedes-Benz", "percentage", 10),
    ]

    for name, dtype, value in agreements:
        db.session.add(
            Agreement(
                name=name,
                discount_type=dtype,
                value=value,
                is_active=True
            )
        )

    db.session.commit()
    print("Convenios iniciales creados.")


# -----------------------
# CLIENT HELPERS
# -----------------------
def normalize_plate(value: str | None) -> str:
    """Normaliza placa: trim, sin espacios internos, mayúsculas."""
    if not value:
        return ""
    return "".join(value.split()).upper()


def upsert_client_from_appointment(
    plate: str,
    full_name: str | None,
    phone: str | None,
    vehicle_type_id: int | None = None,
    agreement_id: int | None = None
):
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
        if vehicle_type_id is not None:
            client.vehicle_type_id = vehicle_type_id
        if agreement_id is not None:
            client.agreement_id = agreement_id
    else:
        db.session.add(Client(
            plate=plate_n,
            full_name=full_name or None,
            phone=phone or None,
            vehicle_type_id=vehicle_type_id,
            agreement_id=agreement_id
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

# Servicios excluidos de descuentos por convenio (siempre precio completo)
AGREEMENT_EXCLUDED_SERVICES = {
    "Wash Essential",
    "Wash Shine",
    "Detallado Exterior",
    "Detallado Llanta a Llanta",
}

def split_price_by_agreement_eligibility(service_ids: list[int], vehicle_type_id: int) -> tuple[int, int]:
    """Devuelve (precio_con_descuento, precio_sin_descuento)."""
    discountable = 0
    excluded = 0
    for sid in service_ids:
        sp = ServicePrice.query.filter_by(
            service_id=sid, vehicle_type_id=vehicle_type_id, is_active=True
        ).first()
        if not sp:
            continue
        service = Service.query.get(sid)
        if service and service.name in AGREEMENT_EXCLUDED_SERVICES:
            excluded += sp.price
        else:
            discountable += sp.price
    return int(discountable), int(excluded)

def apply_agreement_discount(price: int, agreement: Agreement | None) -> int:
    if not agreement or not agreement.is_active:
        return price

    if agreement.discount_type == "percentage":
        discount = int(round(price * (agreement.value / 100)))
    else:
        discount = agreement.value

    return max(price - discount, 0)

def apply_agreement_discount_split(service_ids: list[int], vehicle_type_id: int, agreement: Agreement | None) -> tuple[int, int]:
    """
    Aplica el descuento del convenio solo a los servicios elegibles.
    Devuelve (precio_final, precio_base_total).
    """
    discountable, excluded = split_price_by_agreement_eligibility(service_ids, vehicle_type_id)
    base_total = discountable + excluded
    discounted = apply_agreement_discount(discountable, agreement)
    return discounted + excluded, base_total

# -----------------------
# HELPER: Calcular valor estimado de una cita (precio base + convenio, sin ajustes manuales)
# -----------------------
def calculate_estimated_amount_for_appointment(appt: Appointment) -> int:
    """
    Calcula el valor estimado de una cita:
    - Precio real por servicios + tipo de vehículo
    - Aplica convenio si existe
    - Aplica ajuste al crear (booking_adjustment) si existe
    """
    if not appt.vehicle_type_id:
        return 0

    service_names = [s.strip() for s in appt.services.split(",") if s.strip()]
    services = Service.query.filter(Service.name.in_(service_names)).all()
    service_ids = [s.id for s in services]

    base_price = calculate_real_price(
        service_ids=service_ids,
        vehicle_type_id=appt.vehicle_type_id
    )

    after_agreement, _ = apply_agreement_discount_split(service_ids, appt.vehicle_type_id, appt.agreement)

    # Aplicar ajuste al crear (booking adjustment)
    b_type  = getattr(appt, "booking_adjustment_type", None)
    b_mode  = getattr(appt, "booking_adjustment_mode", None)
    b_value = int(getattr(appt, "booking_adjustment_value", None) or 0)

    if b_type and b_value > 0:
        if b_mode == "percentage":
            b_amount = int(round(after_agreement * (b_value / 100)))
        else:
            b_amount = b_value
        if b_type == "discount":
            after_agreement = max(after_agreement - b_amount, 0)
        elif b_type == "surcharge":
            after_agreement = after_agreement + b_amount

    return after_agreement

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
        return redirect(url_for("payment_methods_list"))

    db.session.add(PaymentMethod(name=name, is_active=True))
    db.session.commit()
    return redirect(url_for("payment_methods_list"))


@app.route("/payment-methods/<int:method_id>/toggle", methods=["POST"])
def payment_methods_toggle(method_id):
    pm = PaymentMethod.query.get_or_404(method_id)
    pm.is_active = not pm.is_active
    db.session.commit()
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
        return redirect(url_for("vehicle_types_list"))

    db.session.add(VehicleType(name=name, is_active=True))
    db.session.commit()
    return redirect(url_for("vehicle_types_list"))


@app.route("/vehicle-types/<int:vehicle_type_id>/toggle", methods=["POST"])
def vehicle_types_toggle(vehicle_type_id):
    vt = VehicleType.query.get_or_404(vehicle_type_id)
    vt.is_active = not vt.is_active
    db.session.commit()
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
    else:
        sp = ServicePrice(
            service_id=service_id,
            vehicle_type_id=vehicle_type_id,
            price=price,
            duration_minutes=duration,
            is_active=True
        )
        db.session.add(sp)

    db.session.commit()
    return redirect(url_for("service_prices_list"))


@app.route("/service-prices/<int:price_id>/update", methods=["POST"])
def service_prices_update(price_id):
    sp = ServicePrice.query.get_or_404(price_id)
    data = request.get_json()
    if not data:
        return {"error": "No data"}, 400
    try:
        if "price" in data:
            sp.price = int(data["price"])
        if "duration_minutes" in data:
            sp.duration_minutes = int(data["duration_minutes"])
    except (ValueError, TypeError):
        return {"error": "Valores inválidos"}, 400
    db.session.commit()
    return {"ok": True, "price": sp.price, "duration_minutes": sp.duration_minutes}


@app.route("/service-prices/<int:price_id>/toggle", methods=["POST"])
def service_prices_toggle(price_id):
    sp = ServicePrice.query.get_or_404(price_id)
    sp.is_active = not sp.is_active
    db.session.commit()
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
    agreements = Agreement.query.filter_by(is_active=True).order_by(Agreement.name).all()
    operators_list = User.query.filter(
        User.is_active == True,
        User.role.in_(["operario", "lider", "admin"])
    ).order_by(User.username).all()

    if request.method == "POST":
        customer_name = request.form.get("customer_name") or "Sin nombre"
        plate = normalize_plate(request.form.get("plate") or "")
        phone = request.form.get("phone") or ""
        date_str = request.form.get("date")
        time_str = request.form.get("start_time")
        notes = request.form.get("notes") or ""
        selected_ids = request.form.getlist("service_ids")
        vehicle_type_id = request.form.get("vehicle_type_id")
        agreement_id = request.form.get("agreement_id")
        # Validar acuerdo: si viene vacío, None. Si viene, convertir a int.
        if agreement_id is None or agreement_id == "":
            agreement_id = None
        else:
            try:
                agreement_id = int(agreement_id)
            except Exception:
                agreement_id = None

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
        upsert_client_from_appointment(
            plate=plate,
            full_name=customer_name,
            phone=phone,
            vehicle_type_id=int(vehicle_type_id) if vehicle_type_id else None,
            agreement_id=agreement_id
        )

        booking_adjustment_type  = request.form.get("booking_adjustment_type") or None
        booking_adjustment_mode  = request.form.get("booking_adjustment_mode") or None
        booking_adjustment_value = request.form.get("booking_adjustment_value")
        try:
            booking_adjustment_value = int(booking_adjustment_value) if booking_adjustment_value else None
        except Exception:
            booking_adjustment_value = None

        appt = Appointment(
            customer_name=customer_name,
            plate=plate,
            phone=phone,
            services=services_str,
            start_datetime=start_dt,
            end_datetime=end_dt,
            notes=notes,
            vehicle_type_id=int(vehicle_type_id),
            status="scheduled",
            agreement_id=agreement_id,
            booking_adjustment_type=booking_adjustment_type,
            booking_adjustment_mode=booking_adjustment_mode,
            booking_adjustment_value=booking_adjustment_value,
        )
        db.session.add(appt)
        db.session.flush()

        for uid in request.form.getlist("operator_ids"):
            try:
                db.session.add(AppointmentOperator(appointment_id=appt.id, user_id=int(uid)))
            except Exception:
                pass

        db.session.commit()

        return redirect(url_for("calendar_view"))

    return render_template(
        "new_appointment.html",
        services=services,
        vehicle_types=vehicle_types,
        agreements=agreements,
        operators_list=operators_list,
        today=date.today().isoformat()
    )


@app.route("/appointments")
def appointments_list():
    """Lista simple en tabla de las próximas citas."""
    appointments = Appointment.query.order_by(Appointment.start_datetime.asc()).all()
    agreements   = Agreement.query.filter_by(is_active=True).order_by(Agreement.name).all()
    estimated_prices = {
        a.id: calculate_estimated_amount_for_appointment(a) for a in appointments
    }
    return render_template(
        "appointments_list.html",
        appointments=appointments,
        agreements=agreements,
        estimated_prices=estimated_prices,
    )


@app.route("/appointments/<int:appointment_id>/delete", methods=["POST"])
def delete_appointment(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    db.session.delete(appt)
    db.session.commit()
    return redirect(url_for("calendar_view"))

@app.route("/appointment/<int:appointment_id>/edit", methods=["GET", "POST"])
def edit_appointment(appointment_id):
    appointment = Appointment.query.get_or_404(appointment_id)
    # --- Cargar catálogos igual que en nueva cita ---
    services = Service.query.filter_by(is_active=True).order_by(Service.name).all()
    vehicle_types = VehicleType.query.filter_by(is_active=True).order_by(VehicleType.name).all()
    agreements = Agreement.query.filter_by(is_active=True).order_by(Agreement.name).all()
    operators_list = User.query.filter(
        User.is_active == True,
        User.role.in_(["operario", "lider", "admin"])
    ).order_by(User.username).all()

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

        # Obtener vehicle_type_id y agreement_id del form (si existen)
        vehicle_type_id = request.form.get("vehicle_type_id")
        agreement_id = request.form.get("agreement_id")
        if vehicle_type_id:
            try:
                appointment.vehicle_type_id = int(vehicle_type_id)
            except Exception:
                pass
        if agreement_id is None or agreement_id == "":
            appointment.agreement_id = None
        else:
            try:
                appointment.agreement_id = int(agreement_id)
            except Exception:
                appointment.agreement_id = None

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

        # Guardar ajuste al crear
        appointment.booking_adjustment_type  = request.form.get("booking_adjustment_type") or None
        appointment.booking_adjustment_mode  = request.form.get("booking_adjustment_mode") or None
        bav = request.form.get("booking_adjustment_value")
        appointment.booking_adjustment_value = int(bav) if bav else None

        # Guardar/actualizar datos del cliente por placa (si hay placa)
        upsert_client_from_appointment(
            plate=appointment.plate,
            full_name=appointment.customer_name,
            phone=appointment.phone,
            vehicle_type_id=appointment.vehicle_type_id,
            agreement_id=appointment.agreement_id
        )

        # Actualizar operarios asignados
        AppointmentOperator.query.filter_by(appointment_id=appointment.id).delete()
        for uid in request.form.getlist("operator_ids"):
            try:
                db.session.add(AppointmentOperator(appointment_id=appointment.id, user_id=int(uid)))
            except Exception:
                pass

        db.session.commit()
        return redirect(url_for("calendar_view"))

    return render_template(
        "edit_appointment.html",
        appointment=appointment,
        services=services,
        vehicle_types=vehicle_types,
        agreements=agreements,
        operators_list=operators_list,
        mode="edit",
        today=appointment.start_datetime.date().isoformat()
    )


@app.route("/services", methods=["GET", "POST"])
def services_view():
    """Gestión simple de servicios: ver y agregar nuevos."""
    if request.method == "POST":
        name = request.form.get("name")
        duration = request.form.get("duration_minutes")

        if not name:
            flash("Debes ingresar nombre.", "danger")
        else:
            try:
                duration = int(duration) if duration else 60
                s = Service(name=name, duration_minutes=duration, is_active=True)
                db.session.add(s)
                db.session.commit()
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

    # Header BI-friendly (PASO 3)
    writer.writerow([
        "service_date",
        "created_at",
        "appointment_id",
        "vehicle_type",
        "plate",
        "customer_name",
        "services",
        "estimated_amount",
        "base_amount",
        "manual_discount_amount",
        "final_amount",
        "payment_method",
        "status",
        "notes",
    ])

    for s in sales:
        # Valor estimado = base_amount (ya incluye convenio)
        estimated_amount = s.base_amount
        writer.writerow([
            s.service_date.strftime("%Y-%m-%d") if s.service_date else "",
            s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "",
            s.appointment_id,
            s.vehicle_type,
            s.plate or "",
            s.customer_name or "",
            s.services or "",
            estimated_amount,
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

@app.route("/expense-categories")
def expense_categories_list():
    if not getattr(g, "current_user", None) or g.current_user.role != "admin":
        flash("Acceso restringido a administradores.", "danger")
        return redirect(url_for("expenses_list"))
    categories = ExpenseCategory.query.order_by(ExpenseCategory.name).all()
    # Contar gastos por categoría para saber si se puede eliminar
    from sqlalchemy import func
    counts = {
        row.category: row.count
        for row in db.session.query(
            Expense.category, func.count(Expense.id).label("count")
        ).group_by(Expense.category).all()
    }
    return render_template("expense_categories.html", categories=categories, counts=counts)


@app.route("/expense-categories/new", methods=["POST"])
def expense_categories_new():
    name = " ".join((request.form.get("name") or "").split())
    if not name:
        flash("Debes ingresar el nombre de la categoría.", "danger")
        return redirect(url_for("expense_categories_list"))

    existing = ExpenseCategory.query.filter_by(name=name).first()
    if existing:
        existing.is_active = True
        db.session.commit()
        flash(f"Categoría '{name}' reactivada.", "success")
        return redirect(url_for("expense_categories_list"))

    db.session.add(ExpenseCategory(name=name, is_active=True))
    db.session.commit()
    flash(f"Categoría '{name}' creada.", "success")
    return redirect(url_for("expense_categories_list"))


@app.route("/expense-categories/<int:category_id>/rename", methods=["POST"])
def expense_categories_rename(category_id):
    if not getattr(g, "current_user", None) or g.current_user.role != "admin":
        return redirect(url_for("expense_categories_list"))
    c = ExpenseCategory.query.get_or_404(category_id)
    new_name = " ".join((request.form.get("name") or "").split())
    if not new_name:
        flash("El nombre no puede estar vacío.", "danger")
        return redirect(url_for("expense_categories_list"))
    if ExpenseCategory.query.filter(ExpenseCategory.name == new_name, ExpenseCategory.id != category_id).first():
        flash(f"Ya existe una categoría con el nombre '{new_name}'.", "danger")
        return redirect(url_for("expense_categories_list"))
    old_name = c.name
    # Actualizar también los gastos existentes que usen este nombre
    Expense.query.filter_by(category=old_name).update({"category": new_name})
    c.name = new_name
    db.session.commit()
    flash(f"Categoría renombrada a '{new_name}'.", "success")
    return redirect(url_for("expense_categories_list"))


@app.route("/expense-categories/<int:category_id>/toggle", methods=["POST"])
def expense_categories_toggle(category_id):
    c = ExpenseCategory.query.get_or_404(category_id)
    c.is_active = not c.is_active
    db.session.commit()
    return redirect(url_for("expense_categories_list"))


@app.route("/expense-categories/<int:category_id>/delete", methods=["POST"])
def expense_categories_delete(category_id):
    if not getattr(g, "current_user", None) or g.current_user.role != "admin":
        return redirect(url_for("expense_categories_list"))
    c = ExpenseCategory.query.get_or_404(category_id)
    in_use = Expense.query.filter_by(category=c.name).count()
    if in_use > 0:
        flash(f"No se puede eliminar '{c.name}': tiene {in_use} gasto(s) asociados.", "danger")
        return redirect(url_for("expense_categories_list"))
    db.session.delete(c)
    db.session.commit()
    flash(f"Categoría '{c.name}' eliminada.", "success")
    return redirect(url_for("expense_categories_list"))

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

        # Calcular el valor estimado antes de construir el dict
        estimated_amount = calculate_estimated_amount_for_appointment(appt)

        # Si en el futuro extendedProps tiene más campos, los conservamos y solo agregamos/actualizamos estimated_amount
        extended_props = {
            "estimated_amount": estimated_amount
        }

        events.append(
            {
                "id": appt.id,
                "title": title,
                "start": appt.start_datetime.isoformat(),
                "end": appt.end_datetime.isoformat(),
                "backgroundColor": color,
                "borderColor": color,
                "extendedProps": extended_props
            }
        )

    return jsonify(events)


@app.route("/appointment/<int:appointment_id>/json")
def appointment_json(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    estimated_amount = calculate_estimated_amount_for_appointment(appt)

    operators = [
        {"id": ao.user_id, "username": ao.user.username}
        for ao in appt.operator_assignments
    ]

    work_duration_minutes = None
    if appt.work_started_at and appt.work_ended_at:
        total_secs = int((appt.work_ended_at - appt.work_started_at).total_seconds())
        net_secs = max(0, total_secs - (appt.total_pause_seconds or 0))
        work_duration_minutes = net_secs // 60

    return jsonify({
        "id": appt.id,
        "customer_name": appt.customer_name,
        "plate": appt.plate,
        "phone": appt.phone,
        "services": appt.services,
        "notes": appt.notes,
        "start": appt.start_datetime.strftime("%Y-%m-%d %H:%M"),
        "end": appt.end_datetime.strftime("%Y-%m-%d %H:%M"),
        "estimated_amount": estimated_amount,
        "status": appt.status,
        "booking_adjustment_type":  getattr(appt, "booking_adjustment_type", None),
        "booking_adjustment_mode":  getattr(appt, "booking_adjustment_mode", None),
        "booking_adjustment_value": getattr(appt, "booking_adjustment_value", None),
        "operators": operators,
        "work_status": appt.work_status or "pending",
        "work_started_at": appt.work_started_at.strftime("%Y-%m-%d %H:%M") if appt.work_started_at else None,
        "work_ended_at": appt.work_ended_at.strftime("%Y-%m-%d %H:%M") if appt.work_ended_at else None,
        "work_duration_minutes": work_duration_minutes,
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
        "vehicle_type_id": client.vehicle_type_id,
        "agreement_id": client.agreement_id,
    })
# --- Ensure clients schema migration for vehicle_type_id column ---
def ensure_clients_vehicle_type_schema():
    with app.app_context():
        try:
            db.session.execute(text("SELECT vehicle_type_id FROM clients LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE clients ADD COLUMN vehicle_type_id INTEGER")
            )
            db.session.commit()

# --- Ensure clients schema migration for agreement_id column ---
def ensure_clients_agreement_schema():
    with app.app_context():
        try:
            db.session.execute(text("SELECT agreement_id FROM clients LIMIT 1"))
        except Exception:
            db.session.execute(
                text("ALTER TABLE clients ADD COLUMN agreement_id INTEGER")
            )
            db.session.commit()


# -----------------------
# API: SUGERIR PLACAS
# -----------------------
@app.route("/api/clients/plates")
def api_client_plates():
    q = (request.args.get("q") or "").strip().upper()
    query = db.session.query(Client.plate).filter(Client.plate != "")
    if q:
        query = query.filter(Client.plate.like(f"{q}%"))
    plates = [r[0] for r in query.order_by(Client.plate).limit(10).all()]
    return jsonify(plates)

# -----------------------
# API: SUGERIR NOMBRES
# -----------------------
@app.route("/api/clients/names")
def api_client_names():
    q = (request.args.get("q") or "").strip()
    query = db.session.query(Client.full_name).filter(
        Client.full_name != None, Client.full_name != ""
    )
    if q:
        query = query.filter(Client.full_name.ilike(f"%{q}%"))
    names = list({r[0] for r in query.limit(20).all()})
    names.sort()
    return jsonify(names[:10])

# -----------------------
# API: DATOS DE CLIENTE POR NOMBRE
# -----------------------
@app.route("/api/clients/by-name")
def api_client_by_name():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"found": False}), 400

    clients = Client.query.filter(
        Client.full_name.ilike(name)
    ).order_by(Client.created_at.asc()).all()

    if not clients:
        return jsonify({"found": False, "name": name})

    first = clients[0]
    plates = [c.plate for c in clients if c.plate]

    return jsonify({
        "found": True,
        "full_name": first.full_name or "",
        "phone": first.phone or "",
        "vehicle_type_id": first.vehicle_type_id,
        "agreement_id": first.agreement_id,
        "plates": plates,
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
    - convenio (opcional)
    No guarda nada en BD.
    """
    data = request.get_json(silent=True) or {}

    service_ids = data.get("service_ids") or []
    vehicle_type_id = data.get("vehicle_type_id")
    agreement_id = data.get("agreement_id")

    try:
        service_ids = [int(sid) for sid in service_ids]
        vehicle_type_id = int(vehicle_type_id)
        agreement_id = int(agreement_id) if agreement_id not in (None, "") else None
    except Exception:
        return jsonify({"ok": False, "error": "Datos inválidos"}), 400

    if not service_ids or not vehicle_type_id:
        return jsonify({"ok": False, "error": "Datos incompletos"}), 400

    # Precio base real
    base_price = calculate_real_price(
        service_ids=service_ids,
        vehicle_type_id=vehicle_type_id
    )

    agreement = Agreement.query.get(agreement_id) if agreement_id else None

    final_price, _ = apply_agreement_discount_split(service_ids, vehicle_type_id, agreement)

    # Ajuste al crear (booking adjustment)
    b_type  = data.get("booking_adjustment_type")
    b_mode  = data.get("booking_adjustment_mode")
    b_value = int(data.get("booking_adjustment_value") or 0)

    if b_type and b_value > 0:
        if b_mode == "percentage":
            b_amount = int(round(final_price * (b_value / 100)))
        else:
            b_amount = b_value
        if b_type == "discount":
            final_price = max(final_price - b_amount, 0)
        elif b_type == "surcharge":
            final_price = final_price + b_amount

    discount_amount = base_price - final_price

    return jsonify({
        "ok": True,
        "base_price": base_price,
        "discount_amount": discount_amount,
        "final_price": final_price
    })

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
    notes = (data.get("notes") or "").strip()

    if status not in ("completed", "cancelled"):
        return jsonify({"ok": False, "error": "Estado inválido"}), 400

    if status == "completed" and not payment_method:
        return jsonify({"ok": False, "error": "Medio de pago requerido"}), 400

    # Resolver servicios por nombre
    service_names = [s.strip() for s in appt.services.split(",") if s.strip()]
    services = Service.query.filter(Service.name.in_(service_names)).all()
    service_ids = [s.id for s in services]

    # Precio base real con convenio (excluye servicios no elegibles)
    base_price = calculate_real_price(
        service_ids=service_ids,
        vehicle_type_id=appt.vehicle_type_id
    )

    base_amount, _ = apply_agreement_discount_split(service_ids, appt.vehicle_type_id, appt.agreement)

    # Aplicar ajuste hecho al crear la cita (booking adjustment)
    b_type  = getattr(appt, "booking_adjustment_type", None)
    b_mode  = getattr(appt, "booking_adjustment_mode", None)
    b_value = int(getattr(appt, "booking_adjustment_value", None) or 0)

    if b_type and b_value > 0:
        if b_mode == "percentage":
            b_amount = int(round(base_amount * (b_value / 100)))
        else:
            b_amount = b_value
        if b_type == "discount":
            base_amount = max(base_amount - b_amount, 0)
        elif b_type == "surcharge":
            base_amount = base_amount + b_amount

    # Ajuste manual al cierre (descuento/recargo)
    adjustment_type = data.get("adjustment_type")  # discount | surcharge | None
    adjustment_mode = data.get("adjustment_mode")  # percentage | fixed
    adjustment_value = int(data.get("adjustment_value") or 0)
    adjustment_reason = (data.get("adjustment_reason") or "").strip()

    adjustment_amount = 0

    if adjustment_value > 0:
        if adjustment_mode == "percentage":
            adjustment_amount = int(round(base_amount * (adjustment_value / 100)))
        else:
            adjustment_amount = adjustment_value

    if adjustment_type == "discount":
        final_amount = max(base_amount - adjustment_amount, 0)
    elif adjustment_type == "surcharge":
        final_amount = base_amount + adjustment_amount
    else:
        final_amount = base_amount

    vt_name = appt.vehicle_type.name if appt.vehicle_type else "N/A"

    # Actualizar el estado de la cita antes de crear la venta
    appt.status = status

    sale = ServiceSale(
        appointment_id=appt.id,
        service_date=appt.start_datetime.date(),
        vehicle_type=vt_name,
        plate=appt.plate,
        customer_name=appt.customer_name,
        services=appt.services,
        base_amount=base_amount,
        discount_amount=adjustment_amount if adjustment_type == "discount" else 0,
        final_amount=final_amount,
        payment_method=payment_method if status == "completed" else None,
        status=status,
        notes=notes or None
    )

    db.session.add(sale)
    db.session.commit()

    return jsonify({"ok": True})


# -----------------------
# CONTROL DE TRABAJO (START / PAUSE / END)
# -----------------------

@app.route("/appointments/<int:appointment_id>/work/start", methods=["POST"])
def work_start(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    if appt.work_status != "pending":
        return jsonify({"ok": False, "error": "El servicio ya fue iniciado"}), 400
    appt.work_status = "in_progress"
    appt.work_started_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "work_status": appt.work_status})


@app.route("/appointments/<int:appointment_id>/work/pause", methods=["POST"])
def work_pause(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    now = datetime.utcnow()
    if appt.work_status == "in_progress":
        appt.work_status = "paused"
        appt.work_paused_at = now
    elif appt.work_status == "paused":
        if appt.work_paused_at:
            pause_secs = int((now - appt.work_paused_at).total_seconds())
            appt.total_pause_seconds = (appt.total_pause_seconds or 0) + pause_secs
        appt.work_paused_at = None
        appt.work_status = "in_progress"
    else:
        return jsonify({"ok": False, "error": "Estado inválido para pausar/reanudar"}), 400
    db.session.commit()
    return jsonify({"ok": True, "work_status": appt.work_status})


@app.route("/appointments/<int:appointment_id>/work/end", methods=["POST"])
def work_end(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    if appt.work_status not in ("in_progress", "paused"):
        return jsonify({"ok": False, "error": "El servicio no está en curso"}), 400
    now = datetime.utcnow()
    if appt.work_status == "paused" and appt.work_paused_at:
        pause_secs = int((now - appt.work_paused_at).total_seconds())
        appt.total_pause_seconds = (appt.total_pause_seconds or 0) + pause_secs
        appt.work_paused_at = None
    appt.work_status = "done"
    appt.work_ended_at = now
    db.session.commit()
    return jsonify({"ok": True, "work_status": appt.work_status})


# -----------------------
# PARKING (PARQUEADEROS)
# -----------------------
PARKING_AMOUNT = 7000

@app.route("/parking")
def parking_list():
    from_str = request.args.get("from")
    to_str   = request.args.get("to")
    plate_q  = (request.args.get("plate") or "").strip().upper()

    date_from = _parse_date(from_str)
    date_to   = _parse_date(to_str)

    query = Parking.query
    if date_from:
        query = query.filter(Parking.parking_date >= date_from)
    if date_to:
        query = query.filter(Parking.parking_date <= date_to)
    if plate_q:
        query = query.filter(Parking.plate.like(f"%{plate_q}%"))

    parkings = query.order_by(Parking.parking_date.desc(), Parking.created_at.desc()).all()
    total    = sum(p.amount for p in parkings)

    return render_template(
        "parking_list.html",
        parkings=parkings,
        total=total,
        today=date.today().isoformat(),
        filters={
            "from":  from_str or "",
            "to":    to_str or "",
            "plate": plate_q,
        }
    )


@app.route("/parking/new", methods=["POST"])
def parking_new():
    customer_name = (request.form.get("customer_name") or "").strip() or None
    plate         = normalize_plate(request.form.get("plate") or "")
    date_str      = request.form.get("parking_date")

    if not plate:
        flash("La placa es obligatoria.", "danger")
        return redirect(url_for("parking_list"))

    parking_date = _parse_date(date_str)
    if not parking_date:
        flash("Fecha inválida.", "danger")
        return redirect(url_for("parking_list"))

    p = Parking(
        customer_name=customer_name,
        plate=plate,
        parking_date=parking_date,
        amount=PARKING_AMOUNT,
    )
    db.session.add(p)
    db.session.flush()  # para obtener p.id

    # Registrar como venta
    sale = ServiceSale(
        appointment_id=None,
        service_date=parking_date,
        vehicle_type="N/A",
        plate=plate,
        customer_name=customer_name,
        services="Parqueadero",
        base_amount=PARKING_AMOUNT,
        discount_amount=0,
        final_amount=PARKING_AMOUNT,
        payment_method=None,
        status="completed",
        notes=None
    )
    db.session.add(sale)
    db.session.commit()

    return redirect(url_for("parking_list"))


@app.route("/parking/<int:parking_id>/delete", methods=["POST"])
def parking_delete(parking_id):
    p = Parking.query.get_or_404(parking_id)
    db.session.delete(p)
    db.session.commit()
    return redirect(url_for("parking_list"))


# INICIALIZACIÓN
# -----------------------
def ensure_payroll_schema():
    """Agrega columnas de nómina a users si no existen."""
    with app.app_context():
        for col, definition in [
            ("salary",          "INTEGER DEFAULT 0"),
            ("is_trial_period", "BOOLEAN DEFAULT 0"),
            ("hire_date",       "DATE"),
        ]:
            try:
                db.session.execute(text(f"SELECT {col} FROM users LIMIT 1"))
            except Exception:
                db.session.execute(text(f"ALTER TABLE users ADD COLUMN {col} {definition}"))
                db.session.commit()

with app.app_context():
    db.create_all()
    ensure_service_sales_schema()
    ensure_clients_vehicle_type_schema()
    ensure_clients_agreement_schema()
    ensure_appointments_close_schema()
    ensure_payroll_schema()
    # --- Normalización defensiva de convenios (migración suave) ---
    normalize_agreements_discount_type()
    seed_services()
    seed_vehicle_types()
    seed_payment_methods()
    seed_expense_categories()
    seed_agreements()

@app.route("/seed-new-services")
def seed_new_services():
    # ---- 1. Eliminar servicios viejos y sus precios ----
    to_delete = [
        "Wash Amarillo", "Wash Rosa", "Efecto Bross", "Enjuague",
        "Wash Morado", "Desmanchado Interno", "Chasis", "Motor"
    ]
    for name in to_delete:
        svc = Service.query.filter_by(name=name).first()
        if svc:
            ServicePrice.query.filter_by(service_id=svc.id).delete()
            db.session.delete(svc)

    # Renombrar Porcelanizado por si acaso tiene nombre distinto (lo dejamos igual)

    db.session.commit()

    # ---- 2. Crear servicios nuevos ----
    new_services = [
        "Wash Essential",
        "Wash Shine",
        "Wash Chasis",
        "Wash Motor",
        "Detallado Exterior",
        "Detallado Interior",
        "Detallado Llanta a Llanta",
        "Polichado",
        "Correccion de Wrap",
        "Porcelanizado",        # ya existe, se omite si está
        "Coating Ceramico 7H+",
        "Coating Ceramico 9H",
    ]
    for name in new_services:
        if not Service.query.filter_by(name=name).first():
            db.session.add(Service(name=name, duration_minutes=60, is_active=True))

    db.session.commit()

    # ---- 3. Insertar precios ----
    # Mapa nombre -> id de vehículo (Auto=1, SUV=2, Camioneta=3, Moto=4)
    # Los IDs reales se buscan por nombre para no depender del orden
    def vid(name):
        vt = VehicleType.query.filter_by(name=name).first()
        return vt.id if vt else None

    def sid(name):
        s = Service.query.filter_by(name=name).first()
        return s.id if s else None

    auto      = vid("Automovil")
    suv       = vid("SUV")
    camioneta = vid("Camioneta")
    moto      = vid("Moto")

    # (service_name, vehicle_name, price, duration_minutes)
    prices = [
        # Wash Essential
        ("Wash Essential",              "Automovil",   40000,  40),
        ("Wash Essential",              "SUV",         45000,  50),
        ("Wash Essential",              "Camioneta",   50000,  50),
        ("Wash Essential",              "Moto",        20000,  30),
        # Wash Shine
        ("Wash Shine",                  "Automovil",   60000,  60),
        ("Wash Shine",                  "SUV",         65000,  70),
        ("Wash Shine",                  "Camioneta",   75000,  70),
        ("Wash Shine",                  "Moto",        35000,  40),
        # Wash Chasis
        ("Wash Chasis",                 "Automovil",   80000,  60),
        ("Wash Chasis",                 "SUV",         90000,  70),
        ("Wash Chasis",                 "Camioneta",  100000,  70),
        # Wash Motor
        ("Wash Motor",                  "Automovil",   80000,  60),
        ("Wash Motor",                  "SUV",         90000,  70),
        ("Wash Motor",                  "Camioneta",  100000,  70),
        # Detallado Exterior
        ("Detallado Exterior",          "Automovil",   90000,  90),
        ("Detallado Exterior",          "SUV",        110000, 110),
        ("Detallado Exterior",          "Camioneta",  150000, 120),
        ("Detallado Exterior",          "Moto",        45000,  50),
        # Detallado Interior
        ("Detallado Interior",          "Automovil",  240000, 240),
        ("Detallado Interior",          "SUV",        310000, 300),
        ("Detallado Interior",          "Camioneta",  370000, 360),
        # Detallado Llanta a Llanta
        ("Detallado Llanta a Llanta",   "Automovil",  110000, 120),
        ("Detallado Llanta a Llanta",   "SUV",        110000, 130),
        ("Detallado Llanta a Llanta",   "Camioneta",  110000, 130),
        # Polichado
        ("Polichado",                   "Automovil",  180000, 180),
        ("Polichado",                   "SUV",        230000, 210),
        ("Polichado",                   "Camioneta",  280000, 240),
        ("Polichado",                   "Moto",        55000,  60),
        # Correccion de Wrap
        ("Correccion de Wrap",          "Automovil",  180000, 180),
        ("Correccion de Wrap",          "SUV",        230000, 210),
        ("Correccion de Wrap",          "Camioneta",  280000, 240),
        ("Correccion de Wrap",          "Moto",        55000,  60),
        # Porcelanizado
        ("Porcelanizado",               "Automovil",  290000, 240),
        ("Porcelanizado",               "SUV",        340000, 270),
        ("Porcelanizado",               "Camioneta",  390000, 300),
        ("Porcelanizado",               "Moto",       100000,  90),
        # Coating Ceramico 7H+
        ("Coating Ceramico 7H+",        "Automovil",  899000, 480),
        ("Coating Ceramico 7H+",        "SUV",       1099000, 540),
        ("Coating Ceramico 7H+",        "Camioneta", 1299000, 600),
        ("Coating Ceramico 7H+",        "Moto",       399000, 300),
        # Coating Ceramico 9H
        ("Coating Ceramico 9H",         "Automovil", 1899000, 600),
        ("Coating Ceramico 9H",         "SUV",       2199000, 660),
        ("Coating Ceramico 9H",         "Camioneta", 2499000, 720),
        ("Coating Ceramico 9H",         "Moto",       799000, 360),
    ]

    for svc_name, vt_name, price, duration in prices:
        s_id = sid(svc_name)
        v_id = vid(vt_name)
        if not s_id or not v_id:
            continue
        existing = ServicePrice.query.filter_by(service_id=s_id, vehicle_type_id=v_id).first()
        if existing:
            existing.price = price
            existing.duration_minutes = duration
            existing.is_active = True
        else:
            db.session.add(ServicePrice(
                service_id=s_id,
                vehicle_type_id=v_id,
                price=price,
                duration_minutes=duration,
                is_active=True
            ))

    db.session.commit()
    return "<h2>✅ Servicios y precios actualizados correctamente. Ya puedes eliminar esta ruta.</h2>"

# ============================================================
# GESTIÓN DE USUARIOS (solo admin)
# ============================================================

@app.route("/users")
def users_list():
    if not getattr(g, "current_user", None) or g.current_user.role != "admin":
        flash("Acceso restringido a administradores.", "danger")
        return redirect(url_for("calendar_view"))
    users = User.query.order_by(User.created_at.asc()).all()
    return render_template("users.html", users=users, today=date.today())


@app.route("/users/new", methods=["POST"])
def users_new():
    if not getattr(g, "current_user", None) or g.current_user.role != "admin":
        return redirect(url_for("calendar_view"))

    username       = (request.form.get("username") or "").strip()
    password       = request.form.get("password") or ""
    role           = request.form.get("role") or "operario"
    hire_date_str  = (request.form.get("hire_date") or "").strip()

    if not username or not password:
        flash("Usuario y contraseña son obligatorios.", "danger")
        return redirect(url_for("users_list"))
    if role not in ("admin", "lider", "operario"):
        flash("Rol inválido.", "danger")
        return redirect(url_for("users_list"))
    if User.query.filter_by(username=username).first():
        flash(f"El usuario '{username}' ya existe.", "danger")
        return redirect(url_for("users_list"))

    hire_date = None
    if hire_date_str:
        try:
            hire_date = date.fromisoformat(hire_date_str)
        except ValueError:
            pass

    u = User(username=username, role=role, is_active=True, must_change_password=True,
             hire_date=hire_date)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    flash(f"Usuario '{username}' creado. Deberá cambiar su contraseña en el primer acceso.", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<int:user_id>/edit", methods=["POST"])
def users_edit(user_id):
    if not getattr(g, "current_user", None) or g.current_user.role != "admin":
        return redirect(url_for("calendar_view"))

    user = User.query.get_or_404(user_id)
    new_username    = (request.form.get("username") or "").strip()
    new_role        = request.form.get("role") or user.role
    new_password    = request.form.get("password") or ""
    hire_date_str   = (request.form.get("hire_date") or "").strip()

    if not new_username:
        flash("El nombre de usuario no puede estar vacío.", "danger")
        return redirect(url_for("users_list"))
    if new_role not in ("admin", "lider", "operario"):
        flash("Rol inválido.", "danger")
        return redirect(url_for("users_list"))

    existing = User.query.filter(User.username == new_username, User.id != user_id).first()
    if existing:
        flash(f"El nombre '{new_username}' ya está en uso.", "danger")
        return redirect(url_for("users_list"))

    user.username = new_username
    user.role     = new_role
    if new_password:
        user.set_password(new_password)
    if hire_date_str:
        try:
            user.hire_date = date.fromisoformat(hire_date_str)
        except ValueError:
            pass
    elif hire_date_str == "":
        user.hire_date = None
    db.session.commit()
    flash(f"Usuario '{new_username}' actualizado.", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
def users_toggle(user_id):
    if not getattr(g, "current_user", None) or g.current_user.role != "admin":
        return redirect(url_for("calendar_view"))

    user = User.query.get_or_404(user_id)
    if user.id == g.current_user.id:
        flash("No puedes desactivarte a ti mismo.", "danger")
        return redirect(url_for("users_list"))
    user.is_active = not user.is_active
    db.session.commit()
    estado = "activado" if user.is_active else "desactivado"
    flash(f"Usuario '{user.username}' {estado}.", "success")
    return redirect(url_for("users_list"))


# ============================================================
# AUTENTICACIÓN
# ============================================================

# --- Migración: crear tabla users si no existe ---
def ensure_users_schema():
    with app.app_context():
        db.create_all()  # crea solo las tablas que faltan
        # Migración: agregar must_change_password si no existe
        try:
            db.session.execute(text("SELECT must_change_password FROM users LIMIT 1"))
        except Exception:
            db.session.execute(text(
                "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"
            ))
            db.session.commit()

ensure_users_schema()

# --- Seed: crear super admin si no existe ningún usuario ---
def seed_superadmin():
    with app.app_context():
        if User.query.count() == 0:
            u = User(username="sa", role="admin", is_active=True)
            u.set_password("Slm2026$$")
            db.session.add(u)
            db.session.commit()

seed_superadmin()

# --- Endpoints que NO requieren sesión ---
PUBLIC_ENDPOINTS  = {"login", "logout", "static", "whatsapp_webhook"}
CHANGE_PWD_ENDPOINTS = {"change_password", "logout", "static"}

# --- Endpoints accesibles por operario (además de los públicos) ---
OPERARIO_ENDPOINTS = {
    "calendar_view", "new_appointment", "edit_appointment",
    "appointments_list", "appointment_delete", "appointment_json",
    "close_appointment",
    "parking_list", "parking_new", "parking_delete",
    "api_events", "api_client_by_plate", "api_client_plates",
    "api_client_names", "api_client_by_name", "api_estimate_price",
    "change_password",
}

@app.before_request
def require_login():
    endpoint = request.endpoint
    if endpoint in PUBLIC_ENDPOINTS:
        return

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login", next=request.path))

    user = User.query.get(user_id)
    if not user or not user.is_active:
        session.clear()
        return redirect(url_for("login"))

    g.current_user = user

    # Forzar cambio de contraseña en primer login
    if bool(user.must_change_password) and endpoint not in CHANGE_PWD_ENDPOINTS:
        flash("Debes cambiar tu contraseña antes de continuar.", "warning")
        return redirect(url_for("change_password"))

    # Restricción por rol
    if user.role == "operario" and endpoint not in OPERARIO_ENDPOINTS:
        flash("No tienes permiso para acceder a esa sección.", "danger")
        return redirect(url_for("calendar_view"))


@app.context_processor
def inject_user():
    return {"current_user": getattr(g, "current_user", None)}


# --- Cambiar contraseña ---
@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    user = User.query.get(user_id)
    if not user:
        session.clear()
        return redirect(url_for("login"))
    g.current_user = user

    error = None
    if request.method == "POST":
        current_pwd = request.form.get("current_password") or ""
        new_pwd     = request.form.get("new_password") or ""
        confirm_pwd = request.form.get("confirm_password") or ""

        if not user.check_password(current_pwd):
            error = "La contraseña actual es incorrecta."
        elif len(new_pwd) < 6:
            error = "La nueva contraseña debe tener al menos 6 caracteres."
        elif new_pwd != confirm_pwd:
            error = "Las contraseñas nuevas no coinciden."
        else:
            user.set_password(new_pwd)
            user.must_change_password = False
            db.session.commit()
            flash("Contraseña actualizada correctamente.", "success")
            return redirect(url_for("calendar_view"))

    return render_template("change_password.html", error=error,
                           forced=user.must_change_password)


# --- Login ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("calendar_view"))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username, is_active=True).first()
        if user and user.check_password(password):
            session.clear()
            session["user_id"] = user.id
            session["user_role"] = user.role
            session.permanent = True
            # Si debe cambiar contraseña, ignorar el 'next' y forzar el cambio
            if bool(user.must_change_password):
                return redirect(url_for("change_password"))
            next_url = request.form.get("next") or url_for("calendar_view")
            return redirect(next_url)
        error = "Usuario o contraseña incorrectos."

    return render_template("login.html", error=error, next=request.args.get("next", ""))


# --- Logout ---
@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/run-migrate-prices")
def run_migrate_prices():
    CATALOG = {
        "Coating Ceramico 7H+": {"Automovil": 899000, "SUV": 1099000, "Camioneta": 1299000, "Moto": 399000},
        "Coating Ceramico 9H":  {"Automovil": 1899000,"SUV": 2199000, "Camioneta": 2499000, "Moto": 799000},
        "Wash Shine":           {"Automovil": 65000,  "SUV": 70000,   "Camioneta": 85000,   "Moto": 45000},
        "Wash Essential":       {"Automovil": 45000,  "SUV": 50000,   "Camioneta": 60000,   "Moto": 35000},
        "Detallado Exterior":   {"Automovil": 90000,  "SUV": 110000,  "Camioneta": 150000,  "Moto": 70000},
        "Wash Chasis":          {"Automovil": 80000,  "SUV": 90000,   "Camioneta": 100000},
        "Detallado Motor":      {"Automovil": 80000,  "SUV": 90000,   "Camioneta": 100000},
        "Detallado Interior":   {"Automovil": 270000, "SUV": 330000,  "Camioneta": 410000},
        "Detallado Llanta a Llanta": {"Automovil": 110000, "SUV": 110000, "Camioneta": 110000},
        "Polichado":            {"Automovil": 180000, "SUV": 230000,  "Camioneta": 280000,  "Moto": 120000},
        "Correccion de Wrap":   {"Automovil": 180000, "SUV": 230000,  "Camioneta": 280000,  "Moto": 120000},
        "Porcelanizado":        {"Automovil": 290000, "SUV": 340000,  "Camioneta": 390000,  "Moto": 150000},
    }

    log = []

    # 1. Renombrar "Wash Motor" -> "Detallado Motor"
    wash_motor = Service.query.filter_by(name="Wash Motor").first()
    if wash_motor:
        wash_motor.name = "Detallado Motor"
        log.append("Renombrado: Wash Motor -> Detallado Motor")

    # 2. Eliminar servicios que empiezan por "Enjuague"
    enjuagues = Service.query.filter(Service.name.ilike("Enjuague%")).all()
    for s in enjuagues:
        ServicePrice.query.filter_by(service_id=s.id).delete()
        db.session.delete(s)
        log.append(f"Eliminado: {s.name}")

    db.session.flush()

    # 3. Upsert de precios
    vehicle_cache = {vt.name: vt for vt in VehicleType.query.all()}
    service_cache = {s.name: s for s in Service.query.all()}
    updated = created = skipped = 0

    for service_name, prices_by_vehicle in CATALOG.items():
        service = service_cache.get(service_name)
        if not service:
            log.append(f"OMITIDO (no existe): {service_name}")
            skipped += 1
            continue
        for vehicle_name, price in prices_by_vehicle.items():
            vehicle = vehicle_cache.get(vehicle_name)
            if not vehicle:
                skipped += 1
                continue
            sp = ServicePrice.query.filter_by(
                service_id=service.id, vehicle_type_id=vehicle.id
            ).first()
            if sp:
                sp.price = price
                sp.is_active = True
                updated += 1
            else:
                db.session.add(ServicePrice(
                    service_id=service.id,
                    vehicle_type_id=vehicle.id,
                    price=price,
                    duration_minutes=60,
                    is_active=True,
                ))
                created += 1

    db.session.commit()
    log.append(f"Precios: {updated} actualizados, {created} creados, {skipped} omitidos.")
    return "<br>".join(log) + "<br><b>Migración completada.</b>"


# =============================================================
# NÓMINA
# =============================================================

BONUS_MAX = 100_000
TRIAL_DEDUCTION = 100_000

# ── Vales ────────────────────────────────────────────────────
@app.route("/vales")
def vales_list():
    employees = User.query.filter(
        User.role == "operario", User.is_active == True
    ).order_by(User.username).all()
    vales = (Vale.query
             .filter_by(period_id=None)
             .order_by(Vale.created_at.desc())
             .all())
    return render_template("vales.html", vales=vales, employees=employees)

@app.route("/vales/new", methods=["POST"])
def vales_new():
    emp_id = request.form.get("employee_id")
    amount = request.form.get("amount")
    desc   = (request.form.get("description") or "").strip()
    if not emp_id or not amount:
        flash("Completa todos los campos.", "danger")
        return redirect(url_for("vales_list"))
    try:
        amount = int(amount)
    except ValueError:
        flash("Monto inválido.", "danger")
        return redirect(url_for("vales_list"))
    db.session.add(Vale(employee_id=int(emp_id), amount=amount, description=desc))
    db.session.commit()
    flash("Vale registrado.", "success")
    return redirect(url_for("vales_list"))

@app.route("/vales/<int:vale_id>/delete", methods=["POST"])
def vales_delete(vale_id):
    vale = Vale.query.get_or_404(vale_id)
    if vale.period_id:
        flash("No se puede eliminar un vale ya asignado a una quincena.", "danger")
        return redirect(url_for("vales_list"))
    db.session.delete(vale)
    db.session.commit()
    flash("Vale eliminado.", "success")
    return redirect(url_for("vales_list"))

# ── Errores de calidad ────────────────────────────────────────
@app.route("/quality-errors")
def quality_errors_list():
    employees = User.query.filter(
        User.role == "operario", User.is_active == True
    ).order_by(User.username).all()
    errors = (QualityError.query
              .filter_by(period_id=None)
              .order_by(QualityError.created_at.desc())
              .all())
    return render_template("quality_errors.html", errors=errors, employees=employees)

@app.route("/quality-errors/new", methods=["POST"])
def quality_errors_new():
    error_type  = request.form.get("error_type")
    description = (request.form.get("description") or "").strip()
    emp_ids     = request.form.getlist("employee_ids")  # lista de ids

    if error_type not in ("leve", "grave"):
        flash("Tipo de error inválido.", "danger")
        return redirect(url_for("quality_errors_list"))
    if not description:
        flash("La descripción es obligatoria.", "danger")
        return redirect(url_for("quality_errors_list"))
    if not emp_ids:
        flash("Selecciona al menos un operario.", "danger")
        return redirect(url_for("quality_errors_list"))

    unit = 5000 if error_type == "leve" else 10000
    # División entera; si no es exacta el primer operario absorbe el resto
    n = len(emp_ids)
    per_person = unit // n
    remainder  = unit - per_person * n

    err = QualityError(error_type=error_type, description=description)
    db.session.add(err)
    db.session.flush()

    for i, eid in enumerate(emp_ids):
        amt = per_person + (remainder if i == 0 else 0)
        db.session.add(QualityErrorEmployee(
            error_id=err.id,
            employee_id=int(eid),
            deduction=amt
        ))

    db.session.commit()
    flash("Error registrado.", "success")
    return redirect(url_for("quality_errors_list"))

@app.route("/quality-errors/<int:error_id>/delete", methods=["POST"])
def quality_errors_delete(error_id):
    err = QualityError.query.get_or_404(error_id)
    if err.period_id:
        flash("No se puede eliminar un error ya asignado a una quincena.", "danger")
        return redirect(url_for("quality_errors_list"))
    db.session.delete(err)
    db.session.commit()
    flash("Error eliminado.", "success")
    return redirect(url_for("quality_errors_list"))

# ── Períodos de nómina ────────────────────────────────────────
@app.route("/payroll")
def payroll_list():
    periods = PayrollPeriod.query.order_by(PayrollPeriod.start_date.desc()).all()
    return render_template("payroll_list.html", periods=periods)

@app.route("/payroll/new", methods=["POST"])
def payroll_new():
    start_str = request.form.get("start_date")
    end_str   = request.form.get("end_date")
    try:
        start = date.fromisoformat(start_str)
        end   = date.fromisoformat(end_str)
    except (TypeError, ValueError):
        flash("Fechas inválidas.", "danger")
        return redirect(url_for("payroll_list"))
    if end < start:
        flash("La fecha de fin debe ser posterior a la de inicio.", "danger")
        return redirect(url_for("payroll_list"))

    period = PayrollPeriod(start_date=start, end_date=end)
    db.session.add(period)
    db.session.flush()

    employees = User.query.filter(
        User.role == "operario", User.is_active == True
    ).all()

    for emp in employees:
        salary    = emp.salary or 0
        is_trial  = emp.in_trial
        base      = max(salary - TRIAL_DEDUCTION, 0) if is_trial else salary
        bonus     = 0 if is_trial else BONUS_MAX

        # Calcular descuento de calidad acumulado (errores sin período asignado)
        quality_deduction = 0
        unassigned_errors = (QualityErrorEmployee.query
                             .join(QualityError)
                             .filter(
                                 QualityErrorEmployee.employee_id == emp.id,
                                 QualityError.period_id == None
                             ).all())
        for qee in unassigned_errors:
            quality_deduction += qee.deduction
            if not is_trial:
                bonus = max(bonus - qee.deduction, 0)
            # Asignar error al período
            qee.error.period_id = period.id

        # Calcular vales sin período asignado
        vales_pendientes = Vale.query.filter_by(employee_id=emp.id, period_id=None).all()
        vales_total = sum(v.amount for v in vales_pendientes)
        for v in vales_pendientes:
            v.period_id = period.id

        entry = PayrollEntry(
            period_id=period.id,
            employee_id=emp.id,
            base_salary=base,
            bonus=bonus,
            bonus_extra=0,
            absence_days=0,
            deduction_absences=0,
            deduction_vales=vales_total,
            deduction_drinks=0,
            deduction_quality=quality_deduction,
            deduction_other=0,
        )
        entry.recalculate()
        db.session.add(entry)

    db.session.commit()
    flash("Quincena creada.", "success")
    return redirect(url_for("payroll_detail", period_id=period.id))

@app.route("/payroll/<int:period_id>")
def payroll_detail(period_id):
    period = PayrollPeriod.query.get_or_404(period_id)
    entries = (PayrollEntry.query
               .filter_by(period_id=period_id)
               .join(User)
               .order_by(User.username)
               .all())
    # Errores del período por operario
    errors_by_emp = {}
    for err in QualityError.query.filter_by(period_id=period_id).all():
        for asgn in err.assignments:
            errors_by_emp.setdefault(asgn.employee_id, []).append({
                "type": err.error_type,
                "description": err.description,
                "deduction": asgn.deduction,
                "created_at": err.created_at,
            })
    # Vales del período por operario
    vales_by_emp = {}
    for v in Vale.query.filter_by(period_id=period_id).all():
        vales_by_emp.setdefault(v.employee_id, []).append(v)

    return render_template("payroll_detail.html",
        period=period,
        entries=entries,
        errors_by_emp=errors_by_emp,
        vales_by_emp=vales_by_emp,
    )

@app.route("/payroll/<int:period_id>/entry/<int:entry_id>/update", methods=["POST"])
def payroll_entry_update(period_id, entry_id):
    entry  = PayrollEntry.query.get_or_404(entry_id)
    period = PayrollPeriod.query.get_or_404(period_id)
    if period.status == "paid":
        return jsonify({"ok": False, "error": "La quincena ya está pagada."}), 400

    data = request.get_json(silent=True) or {}
    is_trial = entry.employee.in_trial

    if "absence_days" in data:
        days = int(data["absence_days"])
        salary_raw = entry.employee.salary or 0
        entry.absence_days      = days
        entry.deduction_absences = int(round(salary_raw / 30 * days))

    if "deduction_drinks" in data:
        entry.deduction_drinks = int(data["deduction_drinks"])

    if "deduction_other" in data:
        entry.deduction_other = int(data["deduction_other"])

    if "deduction_other_notes" in data:
        entry.deduction_other_notes = data["deduction_other_notes"]

    if "bonus_extra" in data:
        entry.bonus_extra = 0 if is_trial else int(data["bonus_extra"])

    if "notes" in data:
        entry.notes = data["notes"]

    # Recalcular vales (puede haberse agregado un vale nuevo)
    vales_total = db.session.query(db.func.sum(Vale.amount)).filter_by(
        employee_id=entry.employee_id, period_id=period_id
    ).scalar() or 0
    entry.deduction_vales = vales_total

    entry.recalculate()
    db.session.commit()
    return jsonify({"ok": True, "total": entry.total})

@app.route("/payroll/<int:period_id>/pay", methods=["POST"])
def payroll_pay(period_id):
    period = PayrollPeriod.query.get_or_404(period_id)
    if period.status == "paid":
        flash("Esta quincena ya fue pagada.", "warning")
        return redirect(url_for("payroll_detail", period_id=period_id))
    period.status  = "paid"
    period.paid_at = datetime.utcnow()
    db.session.commit()
    flash("Quincena marcada como pagada.", "success")
    return redirect(url_for("payroll_detail", period_id=period_id))

@app.route("/payroll/<int:period_id>/delete", methods=["POST"])
def payroll_delete(period_id):
    period = PayrollPeriod.query.get_or_404(period_id)
    if period.status == "paid":
        flash("No se puede eliminar una quincena ya pagada.", "danger")
        return redirect(url_for("payroll_list"))
    # Desasociar errores y vales del período antes de borrar
    QualityError.query.filter_by(period_id=period_id).update({"period_id": None})
    Vale.query.filter_by(period_id=period_id).update({"period_id": None})
    db.session.delete(period)
    db.session.commit()
    flash("Quincena eliminada.", "success")
    return redirect(url_for("payroll_list"))

# ── Vale rápido desde detalle de nómina ──────────────────────
@app.route("/payroll/<int:period_id>/vale/new", methods=["POST"])
def payroll_vale_new(period_id):
    period = PayrollPeriod.query.get_or_404(period_id)
    if period.status == "paid":
        flash("La quincena ya está pagada.", "danger")
        return redirect(url_for("payroll_detail", period_id=period_id))
    emp_id = request.form.get("employee_id")
    amount = request.form.get("amount")
    desc   = (request.form.get("description") or "").strip()
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        flash("Monto inválido.", "danger")
        return redirect(url_for("payroll_detail", period_id=period_id))

    db.session.add(Vale(
        employee_id=int(emp_id), amount=amount,
        description=desc, period_id=period_id
    ))
    # Actualizar entry
    entry = PayrollEntry.query.filter_by(
        period_id=period_id, employee_id=int(emp_id)
    ).first()
    if entry:
        entry.deduction_vales += amount
        entry.recalculate()
    db.session.commit()
    flash("Vale agregado.", "success")
    return redirect(url_for("payroll_detail", period_id=period_id))

# ── Configuración salarial en usuarios ───────────────────────
@app.route("/users/<int:user_id>/salary", methods=["POST"])
def user_salary_update(user_id):
    user = User.query.get_or_404(user_id)
    data = request.get_json(silent=True) or {}
    if "salary" in data:
        user.salary = int(data["salary"])
    if "is_trial_period" in data:
        user.is_trial_period = bool(data["is_trial_period"])
    if "hire_date" in data:
        try:
            user.hire_date = date.fromisoformat(data["hire_date"]) if data["hire_date"] else None
        except ValueError:
            pass
    db.session.commit()
    in_trial = user.in_trial
    trial_end = user.trial_end_date.isoformat() if user.trial_end_date else None
    return jsonify({"ok": True, "in_trial": in_trial, "trial_end": trial_end})


# ============================================================
# WHATSAPP — TWILIO
# ============================================================

_BOGOTA = pytz.timezone("America/Bogota")


def send_whatsapp(to: str, body: str) -> tuple[bool, str]:
    """Envía un mensaje de WhatsApp via Twilio. Retorna (ok, error_msg)."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_FROM", "whatsapp:+14155238886")
    if not account_sid or not auth_token:
        return False, "Variables TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN no configuradas."
    try:
        from twilio.rest import Client as TwilioClient
        # Normalizar número destino
        phone = to.strip().replace(" ", "").replace("whatsapp:", "")
        if not phone.startswith("+"):
            phone = "+57" + phone  # Colombia por defecto
        # Normalizar número origen — siempre forzar prefijo whatsapp:
        from_clean = from_number.strip().replace("whatsapp:", "")
        TwilioClient(account_sid, auth_token).messages.create(
            from_=f"whatsapp:{from_clean}",
            to=f"whatsapp:{phone}",
            body=body,
        )
        app.logger.info(f"[WhatsApp] Mensaje enviado a {phone}")
        return True, ""
    except Exception as exc:
        app.logger.error(f"[WhatsApp] Error al enviar a {to}: {exc}")
        return False, str(exc)


NOXA_MAPS_LINK = "https://maps.app.goo.gl/qjiSRV3ypoV3i4aF9"


# ── Claude — motor de respuesta del bot de ventas ─────────────────────────────
_claude_client = None

def _get_claude_client():
    global _claude_client
    if _claude_client is None:
        import anthropic
        _claude_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    return _claude_client


NOXA_SYSTEM_PROMPT = """Te llamas Mariana y eres la asesora comercial de NOXA Detail (también conocido como NOXA Car Care), un negocio de detailing y car wash de alto nivel en Bogotá (Prado Veraniego). Hablas por WhatsApp con clientes potenciales. Tu objetivo real es cerrar ventas o, como mínimo, agendar diagnósticos — eres una vendedora con oficio, no un catálogo automático.

# IDENTIDAD
- Te llamas Mariana. Si te preguntan quién eres o con quién hablan, responde con tu nombre con naturalidad (ej. "Soy Mariana, de NOXA Detail").
- Si el mensaje que estás respondiendo es el primer mensaje de esa conversación (te lo indicaré explícitamente), saluda así, sin discurso largo ni saludo genérico de "bot":
  - Si ya tienes un nombre real del cliente (nombre de perfil de WhatsApp que suene a nombre de persona): "¡Hola [Nombre]! Soy Mariana, de NØXA Car Care 👋" y de una vez continúa con naturalidad hacia tu primera pregunta (sobre el carro).
  - Si NO tienes un nombre real (perfil vacío, alias, emojis, algo que no sea nombre de persona): "¡Hola! Soy Mariana, de NØXA Car Care 👋" y tu ÚNICA pregunta de ese turno debe ser pidiéndole su nombre (ej. "¿Con quién tengo el gusto?" o "¿cómo te llamas?") — antes de preguntar cualquier otra cosa del carro o el servicio.
- Nunca digas que eres una inteligencia artificial, un bot o un asistente virtual, a menos que el cliente te lo pregunte directamente — en ese caso sé honesta.
- Si un mensaje entrante empieza con "[Sistema:", NO es algo que escribió el cliente — es una instrucción interna nuestra. Síguela para generar el mensaje que corresponda, pero no la trates como parte de lo que dijo el cliente ni la menciones.

# SEGUIMIENTO A LEADS EN SILENCIO
Cuando recibas la instrucción "[Sistema: el cliente quedó en silencio, genera un mensaje de seguimiento — etapa: <etapa>]", el valor de `<etapa>` te dice qué tono usar:
- **recuperar_intencion** (24 horas de silencio): mensaje breve para retomar, referenciando algo concreto y específico de lo que ya hablaron (su carro, el servicio que le interesaba, la duda que tenía). Cercano, sin presión.
- **reabrir_conversacion** (72 horas de silencio): un poco más de contexto que la vez anterior — recuérdale brevemente el valor de lo que hablaron, como retomando un interés genuino, sin sonar a venta forzada.
- **cierre_elegante** (7 días de silencio, es el último intento automático): despídete con elegancia dejando la puerta abierta, sin presionar — algo como reconocer que quizás no es el momento y que ahí estás cuando quiera retomarlo. Después de este mensaje no se vuelve a insistir automáticamente.

En todos los casos:
- Usa su nombre si lo tienes.
- Nunca genérico como "¿sigues ahí?", "¿alguna duda?", "hola?", "quedo atento", "me confirmas?", "¿entonces qué hacemos?" — se siente a persecución, no a continuidad real.
- Un solo mensaje corto, máximo ~300 caracteres, con el mismo límite del resto de tus respuestas.

# TRATO Y TONO
- Cercano pero respetuoso y profesional. Nunca uses lenguaje robótico ni de plantilla. Que se sienta una atención muy personalizada, como si el cliente fuera el único al que le escribes hoy.
- Usa el nombre de la persona cuando lo tengas y suene a un nombre real. Se te va a indicar el nombre de perfil de WhatsApp del cliente en cada conversación: si es un nombre propio normal (ej. "Andrés", "Camila Rojas"), úsalo con naturalidad. Si es un alias, apodo, emojis, o algo que no sea un nombre real (ej. "Solo Millos 💙", "🔥Team🔥"), NO lo uses — pregúntale su nombre como tu primera pregunta en el primer mensaje de la conversación (ver sección IDENTIDAD).
- Emojis: úsalos con mucha moderación, solo en un 5-10% de tus mensajes, y solo cuando aporten (nunca en todos los mensajes ni de forma decorativa constante).
- No seas condescendiente ni exageradamente elogioso. Evita muletillas como "¡buena pregunta!", "excelente elección", "qué bueno que preguntas" en casi todos los mensajes — se siente falso y a lambonería. Responde directo, como alguien seguro de lo que sabe, no como alguien tratando de caerle bien al cliente todo el tiempo.
- Nunca uses la palabra "blindaje" para el cerámico — no es una armadura física. Siempre habla de "protección", y cuando necesites ser más técnico, "protección química".
- Siempre que le pidas algo al cliente (que mande fotos, que avise si necesita reagendar, que confirme algo, etc.), hazlo con amabilidad, pidiendo el favor — usa "por favor" o una fórmula igual de cortés. Ejemplo: no "Si necesitas reagendar, avísame con tiempo", sino "Si necesitas reagendar, por favor avísame con tiempo."

# FRASES PROHIBIDAS
Nunca digas (son promesas absolutas que no puedes garantizar, o suenan poco profesional):
- "El cerámico corrige rayones" / "evita rayones" (sin condicional — siempre depende del diagnóstico)
- "Es la mejor opción para todos los carros"
- "Te protege para siempre"
- "Te sirve sí o sí"
- "Eso queda perfecto sí o sí"
- "Te elimina todos los rayones"
- "Es el mejor servicio"
- "Te dejo agendado" sin hora confirmada
- "Quedo atento" como cierre de seguimiento (sin contenido real)
Tampoco uses palabras demasiado informales/coloquiales: "parce", "uy", "súper", "tranqui".
En su lugar, frases que sí puedes usar con confianza: "para orientarte bien...", "antes de recomendarte un paquete...", "depende del estado real de la pintura...", "la recomendación final se confirma en diagnóstico...", "te puedo dar una recomendación inicial con fotos...".

# FORMATO DE RESPUESTA — MUY IMPORTANTE
- Nunca mandes un párrafo largo con toda la información. Los clientes en WhatsApp no leen bloques de texto.
- LÍMITE DURO: cada mensaje individual debe tener máximo ~300 caracteres (2-4 líneas cortas de celular). Si tu respuesta completa supera eso, es un error tuyo — recórtala, no la mandes larga.
- Casi nunca uses viñetas, negrillas en cadena, ni listas — eso es formato de documento, no de chat. Escribe como si estuvieras tecleando rápido desde el celular.
- Para separar tu respuesta en varios mensajes de WhatsApp, escribe cada mensaje y sepáralos con una línea que contenga únicamente: ---
  Máximo 3 mensajes VISIBLES por turno (la mayoría de las veces con 1-2 basta). Los marcadores internos [ESCALAR: ...], [META: ...] y [NOMBRE: ...] (ver más abajo) van aparte, no cuentan dentro de ese límite de 3 — siempre van al final, cada uno en su propio mensaje separado por "---".
- Ante preguntas técnicas o comparativas (ej. "cerámico vs PPF", "cuál es mejor"): NO expliques todo el detalle técnico de una. Da la diferencia clave en una frase corta, y pregunta qué le interesa más antes de profundizar. Prefiere decir menos y dejar que el cliente pida más, a soltarlo todo de una — el cliente siempre puede preguntar de nuevo, tú no puedes "des-mandar" un mensaje largo.
- Termina siempre tu turno (el último mensaje) con una pregunta que haga avanzar la conversación. Nunca dejes un mensaje "cerrado" sin pregunta.
- REGLA DURA: nunca hagas dos preguntas en el mismo mensaje. Un solo signo de interrogación por turno, siempre — ni siquiera "¿y esto, o esto?" con dos ideas distintas. Elige la más importante ahora y espera la respuesta del cliente antes de hacer la siguiente. Ejemplo de lo que está MAL: "¿Qué carro es, marca y modelo? Y cuéntame, ¿lo usas para el día a día o el fin de semana?" — son dos preguntas, nunca hagas esto. BIEN: "¿Qué carro es?" y en el siguiente turno, ya con esa respuesta, preguntas lo del uso.
- Nunca sueltes el catálogo completo ni una lista larga de servicios de una sola vez.

# MEDIOS DE PAGO
NOXA acepta efectivo, transferencia y datáfono (tarjeta débito/crédito). Si preguntan en general, respóndelo directo y con seguridad, no lo desvíes a "un asesor te confirma".

Si el cliente quiere hacer el **anticipo del 10%** directamente por transferencia (para asegurar su cupo), primero pregúntale cuál medio le sirve más, y según lo que diga dale el dato correspondiente:
- **Bre-B**: 1024501327
- **Daviplata**: 3143068701
- **Nequi**: 3143068701
Esto sí lo puedes manejar tú directamente — no hace falta escalar a un humano solo por dar el número de la transferencia del anticipo. Una vez le des el dato, pídele que te confirme cuando ya haya hecho la transferencia (para que quede registrado, aunque el humano confirme el pago después).

# HORARIO DE ATENCIÓN
Lunes a sábado, 9:00am a 6:00pm. Nunca ofrezcas ni confirmes citas en domingo. Si el cliente propone domingo, dile amablemente que atienden de lunes a sábado y pídele otra fecha dentro de ese horario.

# METODOLOGÍA DE VENTA — VENDER SIN VENDER
Tu trabajo no es convencer al cliente de que NOXA es lo mejor, ni venderle a la fuerza. Es ayudarlo, con las preguntas correctas, a que ÉL MISMO llegue a la conclusión de que quiere cuidar su inversión. Evita sonar a discurso de ventas ("somos los mejores", "es la mejor opción del mercado") — en vez de eso, haz que el cliente piense en su propio carro, su propia situación, y lo que le importa. Si lo logras, el cliente pide comprar, tú no tienes que empujarlo.

Regla de oro, y esta aplica SIEMPRE, no solo la primera vez que sale el tema de plata en la conversación: **nunca escribas un precio sin que el cliente tenga total claridad de todo lo que ese servicio le aporta.** Esto no es un paso que se cumple una vez y ya — cada vez que el precio vuelva a aparecer (el cliente pregunta cuánto vale, objeta que está caro, o duda), tienes que reforzar el valor de nuevo antes o junto con el número, no soltar el precio solo. Los servicios de NOXA no son simples lavados, son tratamientos técnicos que la mayoría de la gente no entiende bien (un cerámico no es "una limpieza", es protección real de la pintura) — por eso el descubrimiento importa tanto como el cierre.

Aunque el cliente pida el precio directamente ("¿cuánto vale?", "dame el precio"), NO se lo des todavía si aún no le has explicado bien en qué consiste la protección y qué le aporta — con solo 1-2 intercambios de descubrimiento (marca del carro, uso, qué le preocupa) NO es suficiente, falta explicarle qué es y cómo funciona el servicio antes del número. En ese caso, reconoce la pregunta sin ignorarla, pero regresa a terminar de explicar el valor antes de dar la cifra — nunca lo sientas como que lo estás evadiendo, sino como que quieres que entienda bien lo que está comprando. Ejemplo: "Ya casi — antes de darte el número quiero que tengas claro qué hace exactamente esta protección por tu carro, para que veas por qué vale la pena." y ahí continúas explicando (sin nueva pregunta en ese mismo mensaje si ya usaste la tuya del turno). El precio debe sentirse como el último paso, cuando el cliente ya entendió todo — no algo que se suelta apenas lo piden.

Cuando el cliente objeta el precio (ej. "eso debe ser caro", "está costoso"): NO te limites a repetir el precio y la garantía en una línea. Refuerza el valor de forma distinta a como ya lo explicaste — piensa en el costo de NO protegerlo (repintar o corregir después siempre sale más caro), en que la garantía es por contrato (compromiso real, no promesa vacía), o en cuánto tiempo/dinero le ahorra en mantenimiento. El objetivo es que el cliente entienda que el precio tiene sentido, no que sienta que le tiraste un número.

Usa la estructura SPIN (metodología de venta consultiva validada en miles de llamadas reales) adaptada a detailing — UNA sola pregunta por mensaje, nunca todas de una, es una conversación no un formulario:

- **Situación** (contexto básico): ¿Qué carro es (marca, modelo, color)? ¿Hace cuánto lo tiene? ¿Para qué usa el carro principalmente (trabajo, ciudad/diario, carro de colección o fin de semana)? — esto último es clave, un carro de colección o de uso ocasional casi siempre es candidato a protección seria, mientras uno de trabajo diario prioriza otras cosas. ¿Le han hecho algún proceso de corrección, polichado o detallado antes?
- **Problema** (el dolor real): ¿Qué es lo que más le molesta de cómo se ve o se siente el carro ahora mismo? ¿Ha notado rayones, opacidad, manchas, mal olor? Si menciona rayones, indaga la profundidad antes de prometer nada: pregúntale si al pasar la uña sobre el rayón esta se queda "pegada"/atrapada (rayón profundo, puede llegar a pintura o primer) o si se siente liso (superficial, en la capa de barniz). Si se traba, NO asumas automáticamente que es corrección incluida en el cerámico — puede ser corregible con más trabajo, o puede necesitar pintura (ver sección de niveles de daño más abajo). Esto te ayuda a calibrar expectativas, no a diagnosticar tú mismo — la certeza real siempre es en el diagnóstico presencial.
- **Implicación** (consecuencia de no actuar — úsala quien no sabe que tiene un problema o está indeciso): si no se protege pronto, la pintura se sigue desgastando con el sol, la lluvia y la contaminación — y corregirla después siempre es más caro que prevenir. No lo sueltes como advertencia dura, es solo una idea corta y natural.
- **Necesidad-beneficio** (que el cliente diga el beneficio, no tú): en vez de listar características, pregúntale algo que lo lleve a imaginar el resultado — "¿te gustaría que quedara protegido varios años sin tener que preocuparte por el mantenimiento?" — cuando el cliente mismo articula que sí lo quiere, está mucho más cerca de comprar que si tú se lo dijiste.
- **Urgencia** (una vez ya hay interés real, antes de proponer diagnóstico): ¿está pensando hacerlo pronto o todavía está evaluando opciones? Esto te ayuda a priorizar qué tan fuerte avanzar el cierre vs. dar espacio.

Con las respuestas, clasifica internamente al cliente (nunca le digas la clasificación explícitamente, solo úsala para decidir cómo guiar la conversación) — esto es central, no todos los leads son iguales y tratarlos igual es un error:

**1. Potencial de ticket:**
- Candidato a cerámico / ticket alto: cuida mucho el carro, es nuevo o de alto valor, quiere protección a largo plazo, ya conoce o pregunta por cerámicos.
- Candidato a ticket medio: busca algo puntual, un lavado o detallado específico, no menciona protección a largo plazo, o da señales de presupuesto limitado.
No todos los leads pueden o quieren pagar un cerámico — no insistas con eso si las señales apuntan a ticket medio. Ajusta qué le ofreces: no le ofrezcas un cerámico de $2.5M a alguien que solo quiere lavar el carro para el fin de semana, y no le ofrezcas solo un Wash Essential a alguien claramente interesado en proteger su inversión.

**2. Nivel de consciencia del cliente:**
1. *No sabe que tiene un problema*: escribe algo genérico ("quiero lavar mi carro"). Tu trabajo es educarlo brevemente sobre por qué la protección importa (sol, lluvia, contaminación desgastan la pintura) antes de ofrecer nada — sin sonar a discurso, con una idea corta.
2. *Sabe que tiene un problema y busca solución*: menciona algo concreto (rayones, manchas, quiere "algo que dure"). Preséntale 1-2 opciones relevantes con su valor — no el catálogo completo.
3. *Sabe el problema y la solución, comparando el mercado*: ya sabe lo que quiere (ej. "cuánto vale un cerámico 9H") y probablemente está cotizando con otros. Aquí diferénciate rápido (garantía por contrato, resultado, tiempos) y genera algo de urgencia para que decida (cupos limitados, agenda ya) — no lo hagas esperar con más preguntas de las necesarias.
No todos los clientes son ignorantes del tema — algunos ya saben exactamente lo que buscan. Detecta esto rápido por cómo preguntan (términos técnicos, comparaciones con otros lugares) y no les repitas explicaciones básicas que no necesitan.

# NUNCA PROMETAS MÁS DE LO QUE PUEDES GARANTIZAR SIN VER EL CARRO
Nunca prometas que "todo se va a quitar" o que un rayón/mancha específica va a desaparecer por completo — eso solo se confirma en el diagnóstico presencial. Habla en términos de "buscamos corregir/mejorar" o "el diagnóstico nos dice exactamente qué tan recuperable es", nunca en garantías absolutas de resultado antes de ver el vehículo en persona.

Manejo por defecto (simple, sin sobre-explicar): si el rayón/mancha suena leve, dile con confianza que se corrige y va incluido en el cerámico. Si suena un poco más profundo o no estás segura, no te compliques explicando niveles técnicos — simplemente dile que en el diagnóstico se da la certeza exacta de qué tan recuperable es, para eso es el diagnóstico. Mantén la respuesta corta y segura.

Solo si el cliente insiste varias veces en saber con certeza si SÍ o NO se puede corregir (antes de pasar por el diagnóstico), entonces sé más honesta y específica: explícale que hay casos (golpes, pintura levantada o desportillada hasta metal/primer, óxido) que no se arreglan con detailing ni cerámico, sino que necesitan repintar — algo que NOXA no hace, pero pueden recomendar talleres de confianza para eso. Nunca finjas que todo se resuelve con lo que ofrece NOXA si el cliente realmente necesita saberlo con certeza.

# CIERRE — SOLO CUANDO EL CLIENTE ESTÉ REALMENTE LISTO (80-90% convencido)
No cierres ni ofrezcas agendar solo porque ya diste el precio. El cliente necesita sentirse convencido, no presionado — si insistes en agendar mientras todavía tiene dudas, lo ahuyentas.

**Señales de que el cliente NO está listo todavía (no ofrezcas agendar, sigue resolviendo dudas):**
- Objeta el precio o se sorprende ("no pensé que costara tanto", "está caro").
- Hace preguntas aclaratorias sobre el proceso, el diagnóstico, o cómo funciona algo.
- Suena dudoso, comparando, o dice que lo va a pensar.
Cuando veas estas señales, tu respuesta debe enfocarse SOLO en resolver esa duda puntual, con una pregunta que profundice en su situación o confirme que quedó claro — NO metas una invitación a agendar en el mismo mensaje. Dale espacio.

**Señales de que el cliente SÍ está listo (ahí sí avanza el cierre):**
- Pregunta por disponibilidad, fechas u horarios.
- Pregunta cómo funciona la reserva o el anticipo.
- Dice explícitamente que le interesa o que quiere hacerlo ("sí, hagámoslo", "me interesa", "dale").
- Pregunta la ubicación para ir.
Cuando ofrezcas agendar, hazlo en **dos pasos, nunca los dos en el mismo mensaje** (respeta la regla de una sola pregunta por turno):
1. Primero ofrece el **día**: "Tengo disponibilidad miércoles o jueves, ¿cuál te queda mejor?"
2. Solo cuando el cliente elige el día, en el siguiente turno ofrece la **hora**: "Para el jueves tengo 3:00pm o 5:00pm, ¿cuál prefieres?"
Nunca preguntes abierto "¿cuándo puedes venir?" — dar demasiadas opciones o dejarlo abierto hace que el cliente posponga la decisión. La cita solo se considera confirmada cuando el cliente ya eligió día Y hora exactos.

- **Nunca repitas la invitación a agendar dos turnos seguidos** si la vez anterior no tuvo una respuesta positiva clara. Si ya la ofreciste y el cliente respondió con una duda u objeción en vez de aceptar, vuelve a resolver la duda — no insistas de nuevo con agendar hasta ver una señal real de que sí quiere.
- El diagnóstico gratuito lo puedes MENCIONAR como parte de explicar el precio (es la referencia, no una obligación), pero mencionarlo no es lo mismo que invitar activamente a agendarlo — eso solo cuando el cliente esté listo, según las señales de arriba.
- Si el cliente ya está decidido (especialmente en cerámicos o detallado interior) y no necesita pasar primero por el diagnóstico: puede reservar directamente el cupo con un **anticipo del 10%** del valor del servicio, para asegurar el espacio. Explícaselo como algo normal y sencillo, no como un obstáculo — es para evitar cancelaciones de última hora, no una barrera de entrada.
- Para agendar, siempre necesitas al menos: qué servicio le interesa, y un día/franja horaria dentro del horario de atención (lunes a sábado, 9am-6pm). No inventes disponibilidad exacta ni confirmes horarios — dile que un asesor le confirma el cupo con esos datos.
- **Confirmación completa antes de cerrar** (reduce el no-show): una vez el cliente eligió día y hora, resume en un mensaje corto y claro: nombre del cliente, vehículo, qué se va a revisar/servicio, día, hora, que es en NOXA (Prado Veraniego), duración estimada (15-20 min si es diagnóstico), y qué hacer si necesita reagendar (avisar con tiempo). No hace falta meterlo todo literal si ya se habló antes en la conversación, pero el resumen final debe dejar claro esos puntos.
- Objeciones: si el cliente duda o dice que está caro, refuerza el valor (garantía, durabilidad, resultado) en vez de bajar el precio o rendirte. No insistas más de 1-2 veces si el cliente claramente no está listo.
- Cuando el cliente diga que necesita pensarlo o evaluar (y no quiere seguir por ahora): despídete cálido, sin presionar, pero dile explícitamente que TÚ le vas a escribir de nuevo pronto (ej. "mañana") para ver qué decidió — eso hace que el seguimiento automático que llega después se sienta esperado, no como un mensaje random. Cierra con un deseo cordial breve. Ejemplo: "Claro que sí, no te afanes. Revísalo con calma y mañana te escribo para ver qué resolviste. Que pases feliz el resto del día 🙂" — no necesitas forzar una pregunta de venta aquí, este tipo de cierre cálido está bien sin pregunta.

# EL DIAGNÓSTICO — explícalo, no solo lo menciones
El diagnóstico es una visita presencial gratuita y sin compromiso en NOXA (Prado Veraniego), de unos 15-20 minutos. Un asesor revisa el vehículo en persona (estado de la pintura, rayones, nivel de contaminación) y ahí mismo le da al cliente el precio exacto para su caso — no es una cita larga ni complicada.
Por qué le conviene al cliente: es la forma de saber con certeza qué necesita su carro puntual (no una estimación genérica), sin ningún compromiso de compra, y sale con el precio real en el momento.
Explica esto de forma natural cuando el cliente no tenga claro qué implica el diagnóstico o cuando dude en agendarlo — no asumas que ya lo sabe.

# UBICACIÓN — puedes mandarla tú misma
Cuando el cliente pida la ubicación o dirección de NOXA, SÍ la puedes mandar directo en tu mensaje de texto — no hace falta escalar a un humano para esto. Da las dos cosas juntas, en el mismo mensaje:
- La dirección exacta: **Calle 128B # 53D-2**, Prado Veraniego, Bogotá.
- El link de Google Maps: https://maps.app.goo.gl/qjiSRV3ypoV3i4aF9
El link sale clickeable en WhatsApp, así que no necesitas nada más — no es un marcador especial, simplemente escríbelo como parte normal de tu mensaje.

# PREDIAGNÓSTICO REMOTO (solo cuando el cliente dice que le queda complicado ir)
Ofrece el **prediagnóstico remoto por fotos** ÚNICAMENTE cuando el cliente diga explícitamente que le queda complicado ir a un diagnóstico presencial (no tiene tiempo, no puede llevar el carro pronto, vive lejos, tiene agenda difícil). No lo ofrezcas de forma proactiva solo porque sí — es una alternativa para cuando el diagnóstico presencial (la opción ideal) no es viable para él.

Cómo pedirlo (sé específica, no digas solo "mándame fotos o video" — eso es débil porque no dice qué ni cómo): pide fotos claras de los 4 frentes del carro — frente, costado izquierdo, costado derecho y trasera — y si quiere, además una foto de alguna zona puntual que le preocupe (rayón, mancha, etc.).

Ya que las tengas (recuerda: SÍ puedes ver las fotos que manda el cliente), dale una **recomendación inicial** con lo que veas — pero deja claro que es preliminar: la recomendación final y el precio exacto siempre se confirman en el diagnóstico presencial, porque hay cosas (como la profundidad real de un rayón) que solo se sienten en persona.

Por qué funciona: cuando el cliente invierte tiempo mandando fotos, aumenta su compromiso con el proceso — todavía no es una compra, pero ya hay una acción concreta de su parte.

# QUÉ ES UN COATING CERÁMICO (usa esto cuando el cliente no entienda bien qué es)
El coating cerámico es una capa de protección química que se adhiere a la pintura del carro (por encima del clear coat/barniz), creando una barrera contra el sol, la lluvia y la contaminación. El agua y la suciedad resbalan en vez de pegarse (efecto hidrofóbico), lo que también facilita mantenerlo limpio.
Beneficios en términos simples: conserva el valor estético y comercial del carro, protege contra rayos UV y oxidación, mantiene un brillo profundo tipo espejo por más tiempo, y reduce el desgaste diario (rayones leves, fricción del uso normal).
El proceso incluye: inspección técnica, lavado técnico especializado, descontaminación química y mecánica, corrección de pintura (pulido para quitar defectos visuales), preparación de superficie, aplicación del coating, y curado (las primeras 12-18 horas son clave para que quede bien adherido).
No lo expliques todo de una — da la idea central en 1-2 mensajes cortos y deja que el cliente pregunte más si quiere profundizar.

# CATÁLOGO DE SERVICIOS
Precios por tipo de vehículo: Auto / SUV / Camioneta / Moto (donde aplique).

CÓMO CLASIFICAR EL VEHÍCULO (no adivines, usa este criterio siempre):
- **Camioneta**: vehículos de 7 puestos, camionetas con platón (pickup, ej. Hilux, Frontier, D-Max), o combis/furgonetas. Son más grandes que una SUV.
- **SUV**: vehículos de 5 puestos sin platón que no son automóvil/sedán/hatchback — ej. crossovers y todoterrenos tipo Tesla Model Y, RAV4, Tucson, CR-V.
- **Auto**: sedanes, hatchbacks y compactos estándar.
- **Moto**: motocicletas.
Si no tienes claro cuántos puestos tiene o si es pickup (ej. el cliente solo dice la marca sin más contexto), pregúntale directamente en vez de asumir — la diferencia de precio entre SUV y Camioneta es considerable y un error aquí genera desconfianza cuando el diagnóstico corrija el valor.

**Coating Cerámico 7H+ (grafeno)** — $899.000 / $1.099.000 / $1.299.000 / $399.000
Protección cerámica de alta resistencia que preserva la pintura original: barniz protegido de rayos UV, contaminación y químicos, efecto hidrofóbico y brillo profundo. Incluye lavado técnico, descontaminado y corrección de pintura previa según estado del vehículo. Garantía por contrato: 3 años. Tiempo estimado: ~2.5 días.

**Coating Cerámico 9H (SiO2 + Grafeno)** — $1.899.000 / $2.199.000 / $2.499.000 / $799.000
El máximo nivel de protección: dureza 9H, mayor resistencia a micro-rayones, químicos, oxidación y desgaste ambiental, efecto hidrofóbico avanzado y duradero. Garantía por contrato: 5 años. Tiempo estimado: ~2.5 días.

⚠️ REGLA ABSOLUTA E INCONDICIONAL sobre los cerámicos, sin excepciones: el precio del cerámico YA incluye toda la corrección y preparación de pintura que el carro necesite, sin importar qué tan rayado esté. NUNCA, bajo ninguna circunstancia, sugieras que el cliente podría necesitar Polichado o Porcelanizado ADEMÁS del cerámico, ni "antes de sellar", ni como paso previo, ni condicionado al diagnóstico. No existe el escenario "puede que necesites Porcelanizado aparte" — eso NO es cierto y confunde al cliente, incluso si tiene rayones notorios. Si el carro tiene rayones, la respuesta correcta es simple: "el cerámico ya incluye la corrección necesaria para tu carro, no es un costo aparte."
  - MAL (nunca digas esto): "el diagnóstico nos ayuda a definir si necesitas Porcelanizado antes de sellar, o si con la corrección incluida es suficiente."
  - BIEN: "tranquilo, el cerámico ya incluye la corrección de esos rayones antes de sellar — no es algo que se cobre aparte."
  Polichado y Porcelanizado como servicios independientes SOLO existen para el cliente que explícitamente NO quiere cerámico y busca únicamente corregir la pintura sin protección cerámica.

⚠️ Secuencia — esta regla es relativa, depende de cómo llega el cliente:
- Si el cliente llega hablando genéricamente de "proteger el carro" o preguntando primero por el cerámico, no le metas PPF todavía — asegúrate de que entienda el cerámico primero, y solo ahí, si aplica (le preocupan golpes de piedra, quiere el máximo nivel de protección, o pregunta directamente por PPF), introduce esta opción.
- Si el cliente llega directamente interesado en PPF (por ejemplo, por una pauta/anuncio específico de PPF, o porque pregunta por PPF desde el primer mensaje), habla de PPF directamente — no le expliques cerámico primero, eso no aplica aquí, ese lead ya sabe lo que quiere.
- Si en la conversación de PPF el cliente se empieza a enfriar (por precio, dudas, o dice que lo va a pensar), ahí sí ofrécele cerámico u otro servicio como alternativa más accesible, sin abandonar PPF de una — dale la opción, no la reemplaces a la fuerza.

**PPF (Paint Protection Film / vinilo de protección)** — NOXA sí ofrece esto, es la opción de protección física de más alto nivel (a diferencia del cerámico, que es protección química — ver la explicación de la diferencia entre ambos más abajo).
Hay 3 marcas de película según el nivel de protección y garantía que busque el cliente:
- **Spectra** — Garantía 5 años (opción de entrada)
- **Avery** — Garantía 7 años (nivel medio)
- **XPEL** — Garantía 10 años (máximo nivel, la más premium)

⚠️ Estos precios de PPF varían más que los del cerámico: dependen del carro específico — hay carros más grandes o con formas más complejas de instalar (más cortes, más curvas, más piezas) que otros, y eso cambia el valor real. Nunca los presentes como un precio fijo y cerrado — siempre como referencia que se confirma con exactitud en el diagnóstico, viendo el carro puntual del cliente.

Precios por marca (Spectra / Avery / XPEL) según la zona a cubrir — valores de referencia, el precio exacto depende del carro y se confirma siempre en el diagnóstico:
- **Full Car** (carrocería exterior completa: bomper delantero, capó, guardabarros, espejos, puertas, pilares, techo, baúl, bomper trasero, zonas de carga) — $10.000.000 / $13.000.000 / $15.000.000
- **Full Front** (bomper delantero, capó, guardabarros delanteros, espejos, farolas delanteras) — $2.500.000 / $3.000.000 / $4.000.000
- **Protección Urbana** (espejos, manijas, borde de puertas, zona de carga del baúl, posapiés) — $850.000 / $1.000.000 / $1.200.000
- **Pianos exteriores** (molduras piano black exteriores) — $200.000 / $250.000 / $350.000
- **Farolas** (delanteras) — $200.000 / $250.000 / $350.000
- **Farolas y stops** (delanteras + stops traseros) — $350.000 / $400.000 / $450.000
- **Farolas fotocromático** (delanteras) — no disponible en Spectra / $300.000 / $400.000
- **Farolas y stops fotocromático** — no disponible en Spectra / $500.000 / $600.000
- **Full Interior** (pantallas, consola central, acabados piano black interiores, controles táctiles, superficies brillantes, paneles vulnerables a rayones) — $800.000 / $1.000.000 / $1.500.000
- **Consola central** (completa, touchpad, mandos, acabados piano black) — $250.000 / $300.000 / $400.000
- **Pantalla** (infoentretenimiento + panel digital de instrumentos si aplica) — $80.000 / $100.000 / $150.000
- **Retrovisores** — $200.000 / $250.000 / $400.000
- **Manijas** — $150.000 / $250.000 / $350.000
- **Capó** — $750.000 / $850.000 / $950.000

Con PPF, igual que con todo lo demás: nunca sueltes toda la tabla de precios de una — pregunta primero qué zona le preocupa (todo el carro, solo el frente, algo puntual como el capó o farolas) y qué nivel de protección busca, y da solo el precio relevante para su caso. La regla de oro aplica exactamente igual aquí: ningún precio de PPF hasta tener certeza de que el cliente entiende bien qué cubre, cómo protege físicamente el carro, y por qué el valor varía según su vehículo — no lo apresures solo porque hay varias marcas y zonas para cotizar.

**Diferencia cerámico vs PPF** (para cuando pregunten cuál elegir, sin sonar a discurso técnico): el cerámico es protección química — una capa que se une a la pintura y la protege de UV, contaminación y rayones leves, con buen brillo. El PPF es protección física — una película que sí absorbe impactos de piedra, ramas y golpes leves que el cerámico no detiene. Muchos clientes ponen PPF en las zonas más expuestas (bomper, capó, farolas) y cerámico en el resto del carro para brillo y mantenimiento — no son excluyentes.

**Wash Shine** (el más popular) — $65.000 / $70.000 / $85.000 / $45.000
Doble shampoo pH neutro, aspirado profundo, restauración de partes negras y encerado que protege, sella y da brillo. Tiempo estimado: 1h30-2h.

**Wash Essential** — $45.000 / $50.000 / $60.000 / $35.000
Lavado de mantenimiento: doble shampoo pH neutro, aspirado profundo, restauración de partes negras. Tiempo estimado: 1h-1h15.

**Detallado Exterior** — $90.000 / $110.000 / $150.000 / $70.000
Limpieza minuciosa de todo el exterior: juntas de puertas, uniones entre latas, desengrasado de vidrios, emblemas, rejillas y zonas ocultas, más encerado protector. Tiempo estimado: 3h.

**Wash Chasis** — $80.000 / $90.000 / $100.000 (no aplica moto)
Elimina barro, grasa, polvo y contaminantes acumulados en la parte baja, con presión controlada para no dañar componentes. Ideal después de viajes largos, lluvia o uso off-road. Tiempo estimado: 1-1.5h.

**Detallado Motor** — $80.000 / $90.000 / $100.000 (no aplica moto)
Limpieza del compartimiento del motor con vapor de alta temperatura y baja humedad, sin riesgo eléctrico. Mejora la estética y facilita detectar fugas. Acabado OEM en plásticos y gomas. Tiempo estimado: 1-1.5h.

**Detallado Interior** — $270.000 / $330.000 / $410.000 (no aplica moto)
Limpieza profunda de tablero, puertas, consola y plásticos; desmanchado de cojinería, alfombras y tapetes; sanitización del aire acondicionado (elimina bacterias y malos olores). Incluye desmontaje de sillas si el cliente lo prefiere para una limpieza más detallada. Tiempo estimado: 6h sin bajar sillas, hasta 1.5 días con sillas abajo.

**Detallado Llanta a Llanta** — $110.000 (mismo precio auto/SUV/camioneta, no aplica moto)
Desmontaje completo de las cuatro ruedas, lavado profundo interior y exterior del rin, detallado de calipers y tornillería, protección cerámica opcional. Tiempo estimado: 2-3h.

**Polichado One Step** — $180.000 / $230.000 / $280.000 / $120.000
Corrige entre 50-60% de micro-rayones, swirls y defectos superficiales de la pintura. Incluye Wash Shine. Tiempo estimado: 4-5h.

**Corrección de Wrap** — $180.000 / $230.000 / $280.000 / $120.000
Para vehículos con vinilo/wrap: corrige marcas leves, opacidad y swirls con productos específicos para vinilo, realzando color y brillo. Incluye Wash Shine. Tiempo estimado: 3-4h.

**Porcelanizado** — $290.000 / $340.000 / $390.000 / $150.000
Corrección profunda en dos pasos, elimina hasta 90% de micro-rayones y marcas de desgaste, acabado tipo espejo. Incluye Wash Shine. Tiempo estimado: 6h.

# LÍMITES
- No inventes servicios, precios ni garantías que no estén en este catálogo.
- Si preguntan algo que no sabes (disponibilidad de agenda específica, detalles muy puntuales), sé honesto y ofrece conectar con un asesor humano en vez de inventar.
- Las fotos que manda el cliente SÍ las puedes ver de verdad — analízalas con confianza cuando te ayuden a entender su caso.
- Las notas de voz se transcriben automáticamente a texto antes de llegarte, así que las tratas como cualquier mensaje normal — pero la transcripción a veces tiene errores. Si algo suena raro, no tiene sentido, o parece una palabra mal transcrita, no asumas — pregunta con naturalidad para confirmar en vez de responder a algo que quizás no dijo.
- Si el mensaje dice "[nota de voz — no se pudo transcribir]" o "[archivo adjunto: ...]", es un audio u otro archivo que no se pudo procesar — pídele amablemente que te lo escriba o te mande una foto en su lugar, sin sonar como un error técnico.

# ESCALAMIENTO A HUMANO — cuándo pasar la conversación
Hay situaciones que tú NO debes manejar sola, porque implican negociación, criterio de negocio o riesgo real de perder la venta. Cuando el cliente muestre cualquiera de estas señales, escala a un humano:
1. Quiere pagar el servicio completo (no el anticipo del 10% estándar, que sí puedes manejar tú — ver sección MEDIOS DE PAGO).
2. Pide un descuento.
3. Pregunta por garantía formal, términos del contrato, o reclama por un servicio ya hecho (queja).
4. Pide factura o documento formal.
5. Pide explícitamente hablar con una persona.
6. Tiene un vehículo premium (ej. de alta gama o de colección) Y ya muestra intención clara de compra — este caso amerita atención personalizada de un asesor.

Cómo hacerlo (proceso de dos partes, en el mismo turno):
1. Responde al cliente con naturalidad y calidez reconociendo lo que pide — nunca lo dejes sin respuesta ni le digas literalmente "te voy a escalar". Algo como "Claro, dame un momento que te conecto con un asesor para eso 🙂" o adaptado a la situación específica.
2. Justo después, como un mensaje SEPARADO (usa el separador "---" como siempre), escribe EXACTAMENTE en este formato, sin nada más en ese mensaje: [ESCALAR: razón breve en pocas palabras]
   Ejemplo: [ESCALAR: cliente quiere pagar el anticipo del cerámico 9H]
   Este mensaje con corchetes NUNCA lo ve el cliente — es una señal interna para el sistema, así que no le agregues nada de conversación ahí, solo el marcador.

# ESTADO Y SERVICIOS DEL LEAD (seguimiento interno para el negocio)
En CADA turno tuyo, sin excepción, además de tu(s) mensaje(s) normal(es), agrega un último mensaje SEPARADO (con "---" antes, como siempre) con este formato EXACTO:
[META: estado=<estado>; servicios=<lista o vacío>]

Esto nunca lo ve el cliente — es solo para que el negocio sepa en qué punto va cada conversación. Cada vez que lo escribas, repasa TODA la conversación hasta ahora y refleja el panorama completo actual — no solo lo que cambió en este mensaje. Es mejor repetir información que ya diste antes que dejarla por fuera.

**<estado>** — uno de estos tres (el más avanzado que ya sea cierto):
- En proceso — todo lo que pasa antes de agendar algo: desde que recién saluda hasta que ya está calificado, cotizado, o incluso con anticipo pendiente.
- Diagnóstico agendado — ya confirmó día Y hora para el diagnóstico presencial. IMPORTANTE: si acabas de confirmar día y hora en ESTE MISMO turno, actualiza el estado ya, en este mismo mensaje — no lo dejes para el siguiente turno.
- Servicio agendado — ya confirmó día Y hora para el servicio real (cerámico, PPF, detallado, etc.), directo o después del diagnóstico. Misma regla: si lo acabas de confirmar en este turno, actualízalo ya.
(No uses "Seguimiento futuro" — ese lo pone el sistema automáticamente.)

**<servicios>** — lista de TODOS los servicios en los que el cliente ha mostrado interés real hasta ahora en la conversación (no solo el de este mensaje), separados por coma, o vacío si ninguno todavía:
- Cerámico — coating cerámico (7H+ o 9H).
- PPF o wrap — PPF/vinilo de protección, o corrección de wrap.
- Otro servicio — cualquier otro (wash, detallado, polichado, porcelanizado, etc.).
Un servicio solo cuenta como "interés" si el cliente lo demostró de verdad (preguntó precio, pidió detalles, dijo que le interesa) — NO por solo haberlo mencionado tú de pasada.

Ejemplo completo: [META: estado=Diagnóstico agendado; servicios=Cerámico,PPF o wrap]
Ejemplo sin servicios aún: [META: estado=En proceso; servicios=]

# ACTUALIZAR EL NOMBRE DEL CLIENTE
Si en algún momento de la conversación el cliente te dice su nombre real (típicamente porque se lo preguntaste al no tener un nombre de perfil válido, pero puede pasar en cualquier momento), agrega otro mensaje separado que diga EXACTAMENTE: [NOMBRE: <nombre que dio>]
Esto actualiza cómo se muestra el contacto en nuestro sistema interno — hazlo siempre que el cliente te dé su nombre real, aunque ya estuviera usando un nombre distinto antes.

Ejemplo de tu respuesta completa en un turno: primer mensaje visible --- segundo mensaje visible (si aplica) --- [META: estado=En proceso; servicios=Cerámico]"""


def _build_message_history(conversation: "Conversation") -> list[dict]:
    """Historial de la conversación en formato Claude. Claude exige alternancia
    estricta user/assistant: si hubo mensajes seguidos del mismo rol (p.ej. por un
    envío fallido anterior), se fusionan en uno solo."""
    history = (
        Message.query
        .filter_by(conversation_id=conversation.id)
        .order_by(Message.created_at)
        .all()
    )
    messages = []
    for m in history:
        role = "user" if m.direction == "in" else "assistant"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + m.body
        else:
            messages.append({"role": role, "content": m.body})
    return messages


def _call_claude(messages: list[dict], extra_system_text: str) -> list[str]:
    """Llama a Claude con la base de conocimiento de NOXA + contexto puntual, y
    parte la respuesta en varios mensajes cortos de WhatsApp (separados por "---")."""
    response = _get_claude_client().messages.create(
        model="claude-sonnet-5",
        max_tokens=600,
        system=[
            {
                "type": "text",
                "text": NOXA_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": extra_system_text,
            },
        ],
        messages=messages,
    )
    text_blocks = [block.text for block in response.content if block.type == "text"]
    full_text = "\n".join(text_blocks).strip()

    if response.stop_reason == "max_tokens" and full_text:
        # Se cortó a mitad de frase — recorta al último punto/salto de línea completo
        # en vez de mandarle al cliente algo que termina a medias.
        app.logger.warning("[Claude] Respuesta truncada por max_tokens, recortando a la última frase completa.")
        cut = max(full_text.rfind("."), full_text.rfind("!"), full_text.rfind("?"), full_text.rfind("\n"))
        if cut > 0:
            full_text = full_text[:cut + 1].strip()

    if not full_text:
        # Puede pasar si el modelo solo devolvió un bloque de pensamiento sin texto
        # (p.ej. cortado por max_tokens). Nunca se debe mandar un mensaje vacío a Twilio.
        raise ValueError("Claude no devolvió texto en la respuesta")

    chunks = [c.strip() for c in re.split(r"\n\s*---\s*\n", full_text)]
    return [c for c in chunks if c] or [full_text]


def _fetch_twilio_media_base64(media_url: str) -> str | None:
    """Descarga una imagen de un mensaje de WhatsApp (requiere auth de Twilio) y la
    devuelve en base64, lista para mandarle a Claude. None si algo falla."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    try:
        resp = requests.get(media_url, auth=(account_sid, auth_token), timeout=15)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode("utf-8")
    except Exception as exc:
        app.logger.error(f"[WhatsApp] Error descargando imagen de Twilio: {exc}")
        return None


def _transcribe_twilio_audio(media_url: str, media_type: str) -> str | None:
    """Descarga una nota de voz de WhatsApp y la transcribe con Whisper (OpenAI).
    None si algo falla (falta la API key, error de red, etc.)."""
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        app.logger.error("[Whisper] OPENAI_API_KEY no configurada, no se puede transcribir audio.")
        return None

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    try:
        audio_resp = requests.get(media_url, auth=(account_sid, auth_token), timeout=15)
        audio_resp.raise_for_status()

        ext = media_type.split("/")[-1].split(";")[0] or "ogg"
        files = {"file": (f"audio.{ext}", audio_resp.content, media_type)}
        data = {"model": "whisper-1", "language": "es"}
        headers = {"Authorization": f"Bearer {openai_key}"}

        transcribe_resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers, files=files, data=data, timeout=30,
        )
        transcribe_resp.raise_for_status()
        return transcribe_resp.json().get("text", "").strip() or None
    except Exception as exc:
        app.logger.error(f"[Whisper] Error transcribiendo audio: {exc}")
        return None


def get_claude_reply(conversation: "Conversation", media_url: str | None = None, media_type: str | None = None) -> list[str]:
    """Genera la respuesta de Claude a un mensaje entrante del cliente. Si el mensaje
    trae una imagen (media_url/media_type), Claude la ve de verdad, no solo el texto."""
    messages = _build_message_history(conversation)
    is_first_message = sum(1 for m in messages if m["role"] == "user") <= 1

    if media_url and media_type and media_type.startswith("image/") and messages and messages[-1]["role"] == "user":
        image_b64 = _fetch_twilio_media_base64(media_url)
        if image_b64:
            caption = messages[-1]["content"] or "El cliente mandó esta foto."
            messages[-1] = {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": caption},
                ],
            }

    profile_line = (
        f"Nombre de perfil de WhatsApp del cliente: {conversation.profile_name!r}"
        if conversation.profile_name else
        "Nombre de perfil de WhatsApp del cliente: no disponible."
    )
    profile_line += (
        "\nEste es el PRIMER mensaje de esta conversación: preséntate por tu nombre."
        if is_first_message else
        "\nYa se han cruzado mensajes antes en esta conversación: no te vuelvas a presentar."
    )

    return _call_claude(messages, profile_line)


def generate_followup_message(conversation: "Conversation", stage: str) -> str:
    """Genera un mensaje de seguimiento personalizado para un lead que quedó en silencio.
    stage: "recuperar_intencion" (24h) | "reabrir_conversacion" (72h) | "cierre_elegante" (7 días)."""
    messages = _build_message_history(conversation)
    messages.append({
        "role": "user",
        "content": f"[Sistema: el cliente quedó en silencio, genera un mensaje de seguimiento — etapa: {stage}. No agregues marcadores de [META], [NOMBRE] ni [ESCALAR] aquí, solo el mensaje de seguimiento.]",
    })

    profile_line = (
        f"Nombre de perfil de WhatsApp del cliente: {conversation.profile_name!r}"
        if conversation.profile_name else
        "Nombre de perfil de WhatsApp del cliente: no disponible."
    )

    chunks = _call_claude(messages, profile_line)
    return chunks[0]


def _summarize_conversation_for_admin(conversation: "Conversation") -> str:
    """Resumen corto y natural (1-2 frases) de qué necesita/preguntó el lead, para el
    aviso al admin — no es un volcado de mensajes, es contexto real y legible."""
    messages = _build_message_history(conversation)
    messages.append({
        "role": "user",
        "content": (
            "[Sistema: no pudimos responderle a este cliente. Resume en 1-2 frases, en "
            "tercera persona y en español, qué necesita o preguntó el cliente en esta "
            "conversación — con el contexto suficiente para que un asesor humano pueda "
            "seguir la conversación sin tener que leer todo el historial. No saludes, "
            "no uses comillas ni el nombre del cliente al inicio, ve directo al resumen. "
            "No agregues marcadores de [META], [NOMBRE] ni [ESCALAR] aquí, "
            "solo el resumen.]"
        ),
    })
    profile_line = (
        f"Nombre de perfil de WhatsApp del cliente: {conversation.profile_name!r}"
        if conversation.profile_name else
        "Nombre de perfil de WhatsApp del cliente: no disponible."
    )
    chunks = _call_claude(messages, profile_line)
    return chunks[0]


def notify_admin_conversation_error(conversation: "Conversation", error: Exception) -> None:
    """Avisa al admin por WhatsApp cuando Mariana no pudo responderle al cliente tras
    varios intentos (por cualquier motivo: generación, envío, etc.), con un resumen real
    de la conversación para que pueda tomarla manualmente con contexto."""
    admin_phone = os.environ.get("ADMIN_WHATSAPP", "")
    if not admin_phone:
        app.logger.error("[WhatsApp] No se pudo avisar al admin: ADMIN_WHATSAPP no configurado.")
        return

    contacto = conversation.profile_name or conversation.phone

    try:
        resumen = _summarize_conversation_for_admin(conversation)
    except Exception as exc:
        app.logger.error(f"[Claude] No se pudo generar el resumen para el admin: {exc}")
        recent = (
            Message.query
            .filter_by(conversation_id=conversation.id)
            .order_by(Message.created_at.desc())
            .limit(8)
            .all()
        )
        recent.reverse()
        transcript = "\n".join(
            f"{'Cliente' if m.direction == 'in' else 'Mariana'}: {m.body}" for m in recent
        )[:1000]
        resumen = f"escribió, pero no logré generar el resumen automático. Últimos mensajes:\n{transcript}"

    msg = (
        f"Diana, {contacto} {resumen}\n\n"
        f"📱 {conversation.phone}\n\n"
        f"Mariana no pudo responderle después de varios intentos — pausé el bot en esa "
        f"conversación, respóndele tú manual desde el panel de Mensajes o por WhatsApp."
    )
    send_whatsapp(admin_phone, msg)


_ESCALATE_RE = re.compile(r"^\[ESCALAR:\s*(.*?)\]$", re.IGNORECASE)
_META_RE = re.compile(r"^\[META:\s*estado\s*=\s*(.*?)\s*;\s*servicios\s*=\s*(.*?)\s*\]$", re.IGNORECASE)
_NOMBRE_RE = re.compile(r"^\[NOMBRE:\s*(.*?)\]$", re.IGNORECASE)

LEAD_STATES = [
    "En proceso",
    "Diagnóstico agendado",
    "Servicio agendado",
    "Seguimiento futuro",
]

SERVICE_TAGS = [
    "Cerámico",
    "PPF o wrap",
    "Otro servicio",
]


def notify_admin_escalation(conversation: "Conversation", reason: str) -> None:
    """Avisa al admin por WhatsApp cuando Mariana detecta una señal de negocio que
    necesita un humano (quiere pagar, pide descuento, se queja, pide hablar con alguien, etc.)."""
    admin_phone = os.environ.get("ADMIN_WHATSAPP", "")
    if not admin_phone:
        app.logger.error("[WhatsApp] No se pudo avisar al admin: ADMIN_WHATSAPP no configurado.")
        return
    contacto = conversation.profile_name or conversation.phone
    msg = (
        f"Diana, {contacto} necesita atención humana: {reason}\n\n"
        f"📱 {conversation.phone}\n\n"
        f"Pausé el bot en esa conversación — respóndele tú desde el panel de Mensajes o por WhatsApp."
    )
    send_whatsapp(admin_phone, msg)


def _generate_and_send_reply(conversation: "Conversation", from_number: str, media_url: str = "", media_type: str = "") -> bool:
    """Genera la respuesta con Claude y manda todos los mensajes. Devuelve False si
    algo falla — generación O envío — para que el webhook pueda reintentar el intento
    completo (nunca deja mensajes a medias sin que el llamador se entere)."""
    reply_chunks = get_claude_reply(conversation, media_url or None, media_type or None)  # puede lanzar excepción

    escalation_reason = None
    new_status = None
    new_service = None
    new_name = None
    visible_chunks = []
    for chunk in reply_chunks:
        stripped = chunk.strip()
        m_esc = _ESCALATE_RE.match(stripped)
        m_meta = _META_RE.match(stripped)
        m_nombre = _NOMBRE_RE.match(stripped)
        if m_esc:
            escalation_reason = m_esc.group(1).strip() or "el cliente necesita atención humana"
        elif m_meta:
            estado_candidate = m_meta.group(1).strip()
            if estado_candidate in LEAD_STATES:
                new_status = estado_candidate
            elif estado_candidate:
                app.logger.warning(f"[WhatsApp] Estado de lead no reconocido, se ignora: {estado_candidate!r}")

            servicio_candidates = [c.strip() for c in m_meta.group(2).split(",") if c.strip()]
            valid = [c for c in servicio_candidates if c in SERVICE_TAGS]
            invalid = [c for c in servicio_candidates if c not in SERVICE_TAGS]
            if invalid:
                app.logger.warning(f"[WhatsApp] Servicio(s) no reconocido(s), se ignoran: {invalid!r}")
            if valid:
                new_service = valid
        elif m_nombre:
            candidate = m_nombre.group(1).strip()
            if candidate:
                new_name = candidate
        else:
            visible_chunks.append(chunk)
    visible_chunks = visible_chunks[:3]  # el límite de "máximo 3 mensajes" aplica solo a lo visible

    if new_status and new_status != conversation.status:
        conversation.status = new_status
        db.session.commit()
    if new_service:
        existing = {t.strip() for t in (conversation.service_tag or "").split(",") if t.strip()}
        merged = existing.union(new_service)
        merged_str = ",".join(sorted(merged, key=SERVICE_TAGS.index))
        if merged_str != conversation.service_tag:
            conversation.service_tag = merged_str
            db.session.commit()
    if new_name and new_name != conversation.profile_name:
        conversation.profile_name = new_name
        db.session.commit()

    for i, chunk in enumerate(visible_chunks):
        ok, err = send_whatsapp(from_number, chunk)
        if not ok:
            app.logger.error(f"[WhatsApp] Error enviando mensaje: {err}")
            return False
        db.session.add(Message(conversation_id=conversation.id, direction="out", body=chunk))
        db.session.commit()
        if i < len(visible_chunks) - 1:
            time.sleep(1.2)  # pausa breve para que se sientan mensajes naturales, no un bloque

    if escalation_reason:
        conversation.bot_active = False
        db.session.commit()
        try:
            notify_admin_escalation(conversation, escalation_reason)
        except Exception as exc:
            app.logger.error(f"[WhatsApp] Error avisando escalamiento al admin: {exc}")

    return True


# ── Webhook: mensajes ENTRANTES de WhatsApp (Twilio) ──────────────────────────
# (redeploy trigger)
@app.route("/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook():
    from_number = request.form.get("From", "").replace("whatsapp:", "")
    body = request.form.get("Body", "").strip()
    profile_name = request.form.get("ProfileName", "").strip()
    num_media = int(request.form.get("NumMedia", "0") or "0")
    media_url = request.form.get("MediaUrl0", "") if num_media > 0 else ""
    media_type = request.form.get("MediaContentType0", "") if num_media > 0 else ""
    app.logger.info(f"[WhatsApp] Mensaje recibido de {from_number} ({profile_name!r}): {body!r} media={media_type or None}")

    conversation = Conversation.query.filter_by(phone=from_number).first()
    if not conversation:
        conversation = Conversation(phone=from_number, profile_name=profile_name or None)
        db.session.add(conversation)
        db.session.flush()
    elif profile_name and conversation.profile_name != profile_name:
        conversation.profile_name = profile_name

    # Palabra clave para limpiar el historial y probar conversaciones desde cero.
    if body.strip().lower() == "/reset":
        Message.query.filter_by(conversation_id=conversation.id).delete()
        db.session.commit()
        send_whatsapp(from_number, "🔄 Listo, empezamos de cero.")
        return ("", 200)

    stored_body = body
    if media_url and media_type.startswith("audio/"):
        transcript = _transcribe_twilio_audio(media_url, media_type)
        if transcript:
            stored_body = f"{body} {transcript}".strip() if body else transcript
            media_url, media_type = "", ""  # ya es texto, no hace falta tratarlo como adjunto
        elif not stored_body:
            stored_body = "[nota de voz — no se pudo transcribir]"
    elif not stored_body and media_url:
        stored_body = "[imagen]" if media_type.startswith("image/") else f"[archivo adjunto: {media_type or 'desconocido'}]"
    db.session.add(Message(conversation_id=conversation.id, direction="in", body=stored_body))
    conversation.followup_count = 0  # el cliente volvió a escribir, resetea el seguimiento
    db.session.commit()

    if conversation.bot_active:
        success = False
        last_exc = None
        for attempt in range(3):
            try:
                success = _generate_and_send_reply(conversation, from_number, media_url, media_type)
                if success:
                    break
                last_exc = RuntimeError("Falló el envío de uno o más mensajes por WhatsApp")
            except Exception as exc:
                last_exc = exc
            if not success:
                app.logger.error(f"[Bot] Intento {attempt + 1}/3 fallido: {last_exc}")

        if not success:
            # 3 intentos fallidos (generación o envío, cualquier error): no dejamos la
            # conversación muerta — pausamos el bot (queda marcado en el panel) y
            # avisamos al admin con contexto para que tome el control manual.
            conversation.bot_active = False
            db.session.commit()
            fallback = "Dame un momento por favor ya te colaboro"
            ok, _ = send_whatsapp(from_number, fallback)
            if ok:
                db.session.add(Message(conversation_id=conversation.id, direction="out", body=fallback))
                db.session.commit()
            try:
                notify_admin_conversation_error(conversation, last_exc)
            except Exception as exc:
                app.logger.error(f"[WhatsApp] Error avisando al admin: {exc}")

    return ("", 200)


# ── Panel de mensajes de WhatsApp (bandeja + human takeover) ─────────────────
def _whatsapp_rows():
    conversations = Conversation.query.all()
    rows = [(c, c.messages[-1] if c.messages else None) for c in conversations]
    rows.sort(key=lambda r: (r[1].created_at if r[1] else r[0].created_at), reverse=True)
    return rows


@app.route("/whatsapp")
def whatsapp_inbox():
    return render_template("whatsapp.html", rows=_whatsapp_rows(), conversation=None, messages=[], lead_states=LEAD_STATES, service_tags=SERVICE_TAGS)


@app.route("/whatsapp/<int:conversation_id>")
def whatsapp_conversation(conversation_id):
    conversation = Conversation.query.get_or_404(conversation_id)
    messages = (
        Message.query
        .filter_by(conversation_id=conversation.id)
        .order_by(Message.created_at)
        .all()
    )
    return render_template("whatsapp.html", rows=_whatsapp_rows(), conversation=conversation, messages=messages, lead_states=LEAD_STATES, service_tags=SERVICE_TAGS)


@app.route("/whatsapp/<int:conversation_id>/messages.json")
def whatsapp_messages_json(conversation_id):
    """Mensajes nuevos desde el último id visto — usado por el polling del chat."""
    since_id = request.args.get("since", 0, type=int)
    conversation = Conversation.query.get_or_404(conversation_id)
    messages = (
        Message.query
        .filter_by(conversation_id=conversation.id)
        .filter(Message.id > since_id)
        .order_by(Message.created_at)
        .all()
    )
    return jsonify({
        "bot_active": conversation.bot_active,
        "messages": [
            {"id": m.id, "direction": m.direction, "body": m.body, "time": m.created_at.strftime("%d/%m %H:%M")}
            for m in messages
        ],
    })


@app.route("/whatsapp/<int:conversation_id>/toggle-bot", methods=["POST"])
def whatsapp_toggle_bot(conversation_id):
    conversation = Conversation.query.get_or_404(conversation_id)
    conversation.bot_active = not conversation.bot_active
    db.session.commit()
    flash("Bot pausado en esta conversación." if not conversation.bot_active else "Bot reactivado.", "success")
    return redirect(url_for("whatsapp_conversation", conversation_id=conversation.id))


@app.route("/whatsapp/<int:conversation_id>/send", methods=["POST"])
def whatsapp_send_manual(conversation_id):
    conversation = Conversation.query.get_or_404(conversation_id)
    body = request.form.get("body", "").strip()
    if body:
        ok, err = send_whatsapp(conversation.phone, body)
        if ok:
            db.session.add(Message(conversation_id=conversation.id, direction="out", body=body))
            conversation.followup_count = 0  # un asesor humano ya respondió, resetea el seguimiento automático
            db.session.commit()
        else:
            flash(f"Error enviando mensaje: {err}", "danger")
    return redirect(url_for("whatsapp_conversation", conversation_id=conversation.id))


# ── Job 1: Recordatorio al ADMIN — 30 minutos antes de cada cita ──────────────
def _job_admin_reminder():
    """Corre cada 5 minutos. Notifica al admin si hay cita en los próximos 30 min."""
    admin_phone = os.environ.get("ADMIN_WHATSAPP", "")
    if not admin_phone:
        return
    with app.app_context():
        now_utc   = datetime.utcnow()
        win_start = now_utc + timedelta(minutes=25)
        win_end   = now_utc + timedelta(minutes=35)
        pendientes = Appointment.query.filter(
            Appointment.start_datetime >= win_start,
            Appointment.start_datetime <= win_end,
            Appointment.status == "scheduled",
            Appointment.notif_reminder_sent == False,
        ).all()
        for appt in pendientes:
            hora_bogota = appt.start_datetime.replace(tzinfo=pytz.utc).astimezone(_BOGOTA)
            msg = (
                f"⏰ *NOXA Detail — Cita en 30 min*\n\n"
                f"👤 {appt.customer_name or 'Sin nombre'}\n"
                f"🚗 Placa: {appt.plate or '—'}\n"
                f"🔧 {appt.services}\n"
                f"📞 {appt.phone or 'Sin teléfono'}\n"
                f"🕐 {hora_bogota.strftime('%I:%M %p')}"
            )
            ok, _ = send_whatsapp(admin_phone, msg)
            if ok:
                appt.notif_reminder_sent = True
                db.session.commit()


# ── Job 2: Recordatorio al CLIENTE — día anterior a las 7 PM ─────────────────
def _job_client_reminder():
    """Corre diariamente a las 7 PM (Bogotá). Notifica a clientes con cita mañana."""
    with app.app_context():
        tomorrow = date.today() + timedelta(days=1)
        citas = Appointment.query.filter(
            db.func.date(Appointment.start_datetime) == tomorrow,
            Appointment.status == "scheduled",
            Appointment.phone.isnot(None),
            Appointment.phone != "",
            Appointment.notif_client_sent == False,
        ).all()
        for appt in citas:
            hora_bogota = appt.start_datetime.replace(tzinfo=pytz.utc).astimezone(_BOGOTA)
            msg = (
                f"👋 Hola {appt.customer_name or 'cliente'}!\n\n"
                f"Te recordamos que mañana tienes una cita en *NOXA Detail*:\n"
                f"🕐 {hora_bogota.strftime('%I:%M %p')}\n"
                f"🔧 {appt.services}\n\n"
                f"Si necesitas reagendar escríbenos. ¡Te esperamos! 🚗✨"
            )
            ok, _ = send_whatsapp(appt.phone, msg)
            if ok:
                appt.notif_client_sent = True
                db.session.commit()


# ── Job 3: Seguimiento cerámico — 3 meses después de la aplicación ────────────
def _job_ceramic_followup():
    """Corre diariamente a las 10 AM (Bogotá). Notifica a clientes cuyo cerámico cumple 90 días."""
    with app.app_context():
        today      = date.today()
        # Ventana de 90 ± 3 días para no perder citas si el job falla un día
        target_ini = datetime.combine(today - timedelta(days=93), datetime.min.time())
        target_fin = datetime.combine(today - timedelta(days=87), datetime.min.time())
        citas = Appointment.query.filter(
            Appointment.start_datetime >= target_ini,
            Appointment.start_datetime <= target_fin,
            Appointment.status == "completed",
            Appointment.services.ilike("%ceramico%"),
            Appointment.phone.isnot(None),
            Appointment.phone != "",
            Appointment.notif_ceramic_sent == False,
        ).all()
        for appt in citas:
            msg = (
                f"✨ Hola {appt.customer_name or 'cliente'}!\n\n"
                f"Han pasado 3 meses desde que aplicamos el cerámico a tu vehículo 🚗\n\n"
                f"Es el momento ideal para el *mantenimiento del recubrimiento* y "
                f"asegurarte de conservar toda la protección.\n\n"
                f"¡Escríbenos para agendar tu mantenimiento! 💎"
            )
            ok, _ = send_whatsapp(appt.phone, msg)
            if ok:
                appt.notif_ceramic_sent = True
                db.session.commit()


# ── Job 4: Seguimiento del bot de WhatsApp a leads en silencio ────────────────
_FOLLOWUP_STAGES = [
    (timedelta(hours=24), "recuperar_intencion"),
    (timedelta(hours=72), "reabrir_conversacion"),
    (timedelta(days=7), "cierre_elegante"),
]


def _job_whatsapp_followup():
    """Corre cada 30 minutos, solo dentro de horario de atención (lunes a sábado, 9am-6pm) —
    ese horario aplica solo para RETOMAR leads fríos, no para responder mensajes nuevos
    (eso siempre pasa de inmediato en el webhook, a cualquier hora).

    Cadencia (según el SOP de NOXA): 24h → recuperar intención, 72h → reabrir conversación,
    7 días → cierre elegante (último intento automático). Después de eso el lead pasa a
    "seguimiento futuro" — no se le vuelve a escribir solo hasta que él responda. Nunca se
    repite un seguimiento antes de que pase el umbral de la siguiente etapa. Se resetea a 0
    en cuanto el cliente vuelve a escribir (ver whatsapp_webhook)."""
    now_bogota = datetime.now(_BOGOTA)
    if now_bogota.weekday() == 6 or not (9 <= now_bogota.hour < 18):  # domingo o fuera de horario
        return
    with app.app_context():
        candidatas = Conversation.query.filter(
            Conversation.bot_active == True,
            Conversation.followup_count < len(_FOLLOWUP_STAGES),
        ).all()
        for conv in candidatas:
            last_msg = (
                Message.query
                .filter_by(conversation_id=conv.id)
                .order_by(Message.created_at.desc())
                .first()
            )
            if not last_msg or last_msg.direction != "out":
                continue  # el cliente ya respondió, o no hay historial

            last_bogota = last_msg.created_at.replace(tzinfo=pytz.utc).astimezone(_BOGOTA)
            threshold, stage = _FOLLOWUP_STAGES[conv.followup_count]

            if (now_bogota - last_bogota) < threshold:
                continue  # todavía no toca esta etapa

            try:
                reply = generate_followup_message(conv, stage)
            except Exception as exc:
                app.logger.error(f"[Claude] Error generando seguimiento: {exc}")
                continue

            ok, _ = send_whatsapp(conv.phone, reply)
            if ok:
                db.session.add(Message(conversation_id=conv.id, direction="out", body=reply))
                conv.followup_count += 1
                if stage == "cierre_elegante":
                    conv.status = "Seguimiento futuro"
                db.session.commit()


# ── Ruta de prueba (solo admin) ───────────────────────────────────────────────
@app.route("/test-whatsapp")
def test_whatsapp():
    if not getattr(g, "current_user", None) or g.current_user.role != "admin":
        flash("Acceso restringido.", "danger")
        return redirect(url_for("calendar_view"))

    admin_phone = os.environ.get("ADMIN_WHATSAPP", "")
    if not admin_phone:
        flash("Variable ADMIN_WHATSAPP no configurada.", "danger")
        return redirect(url_for("calendar_view"))

    # Diagnóstico de variables
    sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_ = os.environ.get("TWILIO_FROM", "whatsapp:+14155238886")

    ok, err = send_whatsapp(
        admin_phone,
        "✅ *NOXA Detail — Prueba exitosa*\n\nLas notificaciones de WhatsApp están funcionando correctamente."
    )
    if ok:
        flash("✅ Mensaje de prueba enviado. Revisa tu WhatsApp.", "success")
    else:
        flash(
            f"❌ Error Twilio: {err} | "
            f"SID: {'✓' if sid else '✗'} | "
            f"Token: {'✓' if token else '✗'} | "
            f"FROM: {from_} | "
            f"TO: {admin_phone}",
            "danger"
        )
    return redirect(url_for("calendar_view"))


# ── Scheduler setup ───────────────────────────────────────────────────────────
_scheduler = BackgroundScheduler(timezone=_BOGOTA)

_scheduler.add_job(
    _job_admin_reminder,
    IntervalTrigger(minutes=5),
    id="admin_reminder",
    replace_existing=True,
)
_scheduler.add_job(
    _job_client_reminder,
    CronTrigger(hour=19, minute=0, timezone=_BOGOTA),
    id="client_reminder",
    replace_existing=True,
)
_scheduler.add_job(
    _job_ceramic_followup,
    CronTrigger(hour=10, minute=0, timezone=_BOGOTA),
    id="ceramic_followup",
    replace_existing=True,
)
_scheduler.add_job(
    _job_whatsapp_followup,
    IntervalTrigger(minutes=30),
    id="whatsapp_followup",
    replace_existing=True,
)

# Inicia solo una vez (evita doble arranque con el reloader de Flask en desarrollo)
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    _scheduler.start()


if __name__ == "__main__":
    app.run(debug=True, port=5001)

