import os
import shutil
import sys
import tempfile

import pytest

# La app corre migraciones tipo `ALTER TABLE ... ADD COLUMN` sobre tablas que
# asume ya existentes apenas se importa el módulo (ver ensure_expenses_schema,
# ensure_payroll_schema, etc. al final de app.py), así que no sirve apuntar a
# un archivo sqlite vacío. En su lugar copiamos el agenda.db real (esquema +
# datos semilla) a un archivo temporal ANTES del primer `import app`, para
# nunca tocar la base real del proyecto.
_repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp_dir = tempfile.mkdtemp(prefix="noxa_payroll_tests_")
_test_db_path = os.path.join(_tmp_dir, "test_agenda.db")
shutil.copyfile(os.path.join(_repo_dir, "agenda.db"), _test_db_path)
os.environ["DB_PATH"] = _test_db_path

# Evita que el scheduler de recordatorios (WhatsApp/Twilio) arranque hilos de
# background durante los tests.
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
BackgroundScheduler.start = lambda self, *a, **kw: None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as app_module  # noqa: E402

flask_app = app_module.app
db = app_module.db


@pytest.fixture(scope="session", autouse=True)
def _session_setup():
    flask_app.config["TESTING"] = True
    yield


@pytest.fixture(autouse=True)
def _clean_db():
    """Cada test arranca con las tablas de nómina/usuarios vacías, y corre
    dentro de un app_context activo (las queries directas en el cuerpo del
    test, fuera de una request del `client`, lo necesitan)."""
    ctx = flask_app.app_context()
    ctx.push()
    app_module.PayrollEntry.query.delete()
    app_module.PayrollPeriod.query.delete()
    app_module.QualityErrorEmployee.query.delete()
    app_module.QualityError.query.delete()
    app_module.Vale.query.delete()
    app_module.User.query.delete()
    db.session.commit()
    yield
    db.session.rollback()
    ctx.pop()


@pytest.fixture
def client():
    return flask_app.test_client()


def make_user(username, role="operario", salary=0, hire_date=None,
              is_trial_period=False, is_active=True):
    user = app_module.User(
        username=username,
        role=role,
        salary=salary,
        hire_date=hire_date,
        is_trial_period=is_trial_period,
        is_active=is_active,
    )
    user.set_password("test1234")
    db.session.add(user)
    db.session.commit()
    return user


def login_as(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
