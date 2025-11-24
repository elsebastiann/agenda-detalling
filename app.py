from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
import os

app = Flask(__name__)
app.secret_key = "cambia_esto_por_algo_mas_seguro"

# Base de datos SQLite
basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, "agenda.db")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + db_path
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


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


class Appointment(db.Model):
    __tablename__ = "appointments"
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=True)
    plate = db.Column(db.String(20), nullable=True)
    services = db.Column(db.String(255), nullable=False)  # "Wash Morado, Motor"
    start_datetime = db.Column(db.DateTime, nullable=False)
    end_datetime = db.Column(db.DateTime, nullable=False)
    notes = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<Appointment {self.customer_name} - {self.services}>"


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
        ("Porcelanizado", 180),
        ("Efecto Bross", 300),
        ("Desmanchado Interno", 360),
    ]

    for name, minutes in services_data:
        s = Service(name=name, duration_minutes=minutes)
        db.session.add(s)
    db.session.commit()
    print("Servicios iniciales creados.")


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

    if request.method == "POST":
        customer_name = request.form.get("customer_name") or "Sin nombre"
        plate = request.form.get("plate") or ""
        date_str = request.form.get("date")
        time_str = request.form.get("start_time")
        notes = request.form.get("notes") or ""
        selected_ids = request.form.getlist("service_ids")

        if not date_str or not time_str:
            flash("Debes seleccionar fecha y hora.", "danger")
            return redirect(url_for("new_appointment"))

        if not selected_ids:
            flash("Debes seleccionar al menos un servicio.", "danger")
            return redirect(url_for("new_appointment"))

        # Convertir fecha/hora
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")

        # Traer servicios seleccionados
        int_ids = [int(x) for x in selected_ids]
        selected_services = Service.query.filter(Service.id.in_(int_ids)).all()

        if not selected_services:
            flash("Los servicios seleccionados no son válidos.", "danger")
            return redirect(url_for("new_appointment"))

        # Algoritmo: servicio más largo + 50% de los demás
        durations = [s.duration_minutes for s in selected_services]
        durations_sorted = sorted(durations, reverse=True)
        longest = durations_sorted[0]
        others = durations_sorted[1:]
        overlap_factor = 0.5  # 50%

        total_minutes = longest + sum(d * overlap_factor for d in others)
        total_minutes = int(round(total_minutes))

        end_dt = start_dt + timedelta(minutes=total_minutes)

        services_str = ", ".join(s.name for s in selected_services)

        appt = Appointment(
            customer_name=customer_name,
            plate=plate,
            services=services_str,
            start_datetime=start_dt,
            end_datetime=end_dt,
            notes=notes,
        )
        db.session.add(appt)
        db.session.commit()

        flash("Cita creada correctamente.", "success")
        return redirect(url_for("calendar_view"))

    return render_template("new_appointment.html", services=services)


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
        appointment.plate = request.form["plate"]
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
        durations = [s.duration_minutes for s in selected_services]

        if durations:
            longest = max(durations)
            extras = sum(durations) - longest
            total_duration = longest + int(extras * 0.5)
        else:
            total_duration = 60

        # Asignar nueva hora final
        appointment.end_datetime = appointment.start_datetime + timedelta(minutes=total_duration)

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
# API PARA FULLCALENDAR
# -----------------------
@app.route("/api/events")
def api_events():
    """Devuelve las citas en formato JSON para FullCalendar."""
    appointments = Appointment.query.all()
    events = []

    for appt in appointments:
        title_parts = [appt.customer_name]
        if appt.plate:
            title_parts.append(appt.plate.upper())
        if appt.services:
            title_parts.append(appt.services)

        title = " - ".join(part for part in title_parts if part)

        events.append(
            {
                "id": appt.id,
                "title": title,
                "start": appt.start_datetime.isoformat(),
                "end": appt.end_datetime.isoformat(),
            }
        )

    return jsonify(events)


# -----------------------
# INICIALIZACIÓN
# -----------------------
with app.app_context():
    db.create_all()
    seed_services()


if __name__ == "__main__":
    app.run(debug=True)