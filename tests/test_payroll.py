"""
Suite de pruebas del módulo de nómina (quincenas, bonos, errores de calidad,
vales, ausencias).

Cómo correrlas:
    cd agenda-detalling
    pip install -r requirements.txt pytest
    pytest tests/ -v
"""
from datetime import date, timedelta

import app as app_module
from conftest import db, flask_app, login_as, make_user

User = app_module.User
PayrollEntry = app_module.PayrollEntry
PayrollPeriod = app_module.PayrollPeriod
QualityError = app_module.QualityError
QualityErrorEmployee = app_module.QualityErrorEmployee
Vale = app_module.Vale


def make_admin(client):
    admin = make_user("admin_test", role="admin")
    login_as(client, admin)
    return admin


def create_period(client, start, end):
    resp = client.post("/payroll/new", data={
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }, follow_redirects=True)
    assert resp.status_code == 200
    return PayrollPeriod.query.order_by(PayrollPeriod.id.desc()).first()


def create_quality_error(client, error_type, employee_ids, description="Error de prueba"):
    resp = client.post("/quality-errors/new", data={
        "error_type": error_type,
        "description": description,
        "employee_ids": [str(eid) for eid in employee_ids],
    }, follow_redirects=True)
    assert resp.status_code == 200


def create_vale(client, employee_id, amount, description=""):
    resp = client.post("/vales/new", data={
        "employee_id": employee_id,
        "amount": amount,
        "description": description,
    }, follow_redirects=True)
    assert resp.status_code == 200


def entry_for(period_id, employee_id):
    return PayrollEntry.query.filter_by(period_id=period_id, employee_id=employee_id).first()


# =====================================================================
# A. User.in_trial
# =====================================================================
class TestInTrial:
    def test_in_trial_true_within_30_days(self):
        with flask_app.app_context():
            u = make_user("op_a", hire_date=date.today() - timedelta(days=10))
            assert u.in_trial is True

    def test_in_trial_false_after_30_days(self):
        with flask_app.app_context():
            u = make_user("op_b", hire_date=date.today() - timedelta(days=31))
            assert u.in_trial is False

    def test_in_trial_boundary_exactly_30_days_is_false(self):
        # (hoy - hire_date).days == 30 -> ya NO cuenta como prueba (< 30)
        with flask_app.app_context():
            u = make_user("op_c", hire_date=date.today() - timedelta(days=30))
            assert u.in_trial is False

    def test_in_trial_boundary_29_days_is_true(self):
        with flask_app.app_context():
            u = make_user("op_d", hire_date=date.today() - timedelta(days=29))
            assert u.in_trial is True

    def test_in_trial_falls_back_to_manual_flag_without_hire_date(self):
        with flask_app.app_context():
            u1 = make_user("op_e", hire_date=None, is_trial_period=True)
            u2 = make_user("op_f", hire_date=None, is_trial_period=False)
            assert u1.in_trial is True
            assert u2.in_trial is False


# =====================================================================
# B. PayrollEntry.recalculate()
# =====================================================================
class TestRecalculate:
    def test_basic_sum_no_deductions(self):
        with flask_app.app_context():
            e = PayrollEntry(period_id=1, employee_id=1,
                              base_salary=1_000_000, bonus=100_000, bonus_extra=0,
                              deduction_absences=0, deduction_vales=0,
                              deduction_drinks=0, deduction_quality=0, deduction_other=0)
            e.recalculate()
            assert e.total == 1_100_000

    def test_quality_deduction_is_not_subtracted_a_second_time(self):
        """
        Regresión del bug crítico: deduction_quality es informativo (ya
        reflejado en `bonus`, que llega recortado desde payroll_new) y NO
        debe restarse de nuevo en recalculate().
        """
        with flask_app.app_context():
            e = PayrollEntry(
                period_id=1, employee_id=1,
                base_salary=1_000_000,
                bonus=90_000,          # ya rebajado por un error de 10.000
                bonus_extra=0, deduction_absences=0, deduction_vales=0,
                deduction_drinks=0, deduction_other=0,
                deduction_quality=10_000,  # solo informativo
            )
            e.recalculate()
            assert e.total == 1_090_000  # NO 1_080_000 (que sería el doble descuento)

    def test_all_deduction_fields_subtract_once(self):
        with flask_app.app_context():
            e = PayrollEntry(
                period_id=1, employee_id=1,
                base_salary=1_000_000, bonus=100_000, bonus_extra=20_000,
                deduction_absences=50_000, deduction_vales=30_000,
                deduction_drinks=5_000, deduction_quality=10_000,
                deduction_other=15_000,
            )
            e.recalculate()
            # deduction_quality NO participa en la resta (ver test anterior)
            expected = 1_000_000 + 100_000 + 20_000 - 50_000 - 30_000 - 5_000 - 15_000
            assert e.total == expected

    def test_total_can_go_negative_if_deductions_exceed_earnings(self):
        # Documenta el comportamiento actual: no hay piso en 0.
        with flask_app.app_context():
            e = PayrollEntry(period_id=1, employee_id=1,
                              base_salary=100_000, bonus=0, bonus_extra=0,
                              deduction_absences=0, deduction_drinks=0,
                              deduction_quality=0, deduction_other=0,
                              deduction_vales=500_000)
            e.recalculate()
            assert e.total == -400_000


# =====================================================================
# C. División de errores de calidad entre varios operarios
# =====================================================================
class TestQualityErrorSplit:
    def test_single_employee_gets_full_unit_value_leve(self, client):
        make_admin(client)
        op = make_user("op_split1", role="operario")
        create_quality_error(client, "leve", [op.id])
        qee = QualityErrorEmployee.query.filter_by(employee_id=op.id).first()
        assert qee.deduction == 5000

    def test_single_employee_gets_full_unit_value_grave(self, client):
        make_admin(client)
        op = make_user("op_split2", role="operario")
        create_quality_error(client, "grave", [op.id])
        qee = QualityErrorEmployee.query.filter_by(employee_id=op.id).first()
        assert qee.deduction == 10000

    def test_even_split_between_two_employees(self, client):
        make_admin(client)
        op1 = make_user("op_split3", role="operario")
        op2 = make_user("op_split4", role="operario")
        create_quality_error(client, "grave", [op1.id, op2.id])
        d1 = QualityErrorEmployee.query.filter_by(employee_id=op1.id).first().deduction
        d2 = QualityErrorEmployee.query.filter_by(employee_id=op2.id).first().deduction
        assert d1 == 5000
        assert d2 == 5000
        assert d1 + d2 == 10000

    def test_remainder_goes_to_first_employee_and_sum_matches_unit(self, client):
        make_admin(client)
        op1 = make_user("op_split5", role="operario")
        op2 = make_user("op_split6", role="operario")
        op3 = make_user("op_split7", role="operario")
        create_quality_error(client, "leve", [op1.id, op2.id, op3.id])
        d1 = QualityErrorEmployee.query.filter_by(employee_id=op1.id).first().deduction
        d2 = QualityErrorEmployee.query.filter_by(employee_id=op2.id).first().deduction
        d3 = QualityErrorEmployee.query.filter_by(employee_id=op3.id).first().deduction
        assert d1 + d2 + d3 == 5000
        assert d1 == 1668  # 1666 + remainder(2)
        assert d2 == 1666
        assert d3 == 1666


# =====================================================================
# D. payroll_new: creación de una quincena
# =====================================================================
class TestPayrollNew:
    def test_regular_employee_gets_full_salary_and_full_bonus(self, client):
        make_admin(client)
        op = make_user("op_reg", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)
        assert e.base_salary == 1_000_000
        assert e.bonus == 100_000
        assert e.total == 1_100_000

    def test_quality_error_reduces_bonus_only_once_CRITICAL(self, client):
        """
        Este es el test que habría atrapado el bug reportado: un error de
        calidad de $10.000 debía descontar $10.000 netos del pago, no
        $20.000 (bono reducido Y restado de nuevo en el total).
        """
        make_admin(client)
        op = make_user("op_qc", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        create_quality_error(client, "grave", [op.id])  # 10.000

        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)

        assert e.bonus == 90_000
        assert e.deduction_quality == 10_000  # informativo
        assert e.total == 1_090_000
        assert e.total != 1_080_000  # valor que daría el bug del doble descuento

    def test_quality_errors_exceeding_bonus_never_touch_base_salary(self, client):
        make_admin(client)
        op = make_user("op_qc_over", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        # 20 errores graves de 10.000 = 200.000 > bono máximo de 100.000
        for _ in range(20):
            create_quality_error(client, "grave", [op.id])

        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)

        assert e.bonus == 0
        assert e.deduction_quality == 200_000  # informativo, aunque exceda el bono
        assert e.total == 1_000_000  # nunca baja del salario base por calidad

    def test_trial_employee_gets_salary_minus_100k_and_zero_bonus(self, client):
        make_admin(client)
        op = make_user("op_trial", role="operario", salary=800_000,
                        hire_date=date.today())  # recién ingresado, en prueba
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)
        assert e.base_salary == 700_000
        assert e.bonus == 0
        assert e.total == 700_000

    def test_trial_employee_salary_floors_at_zero_not_negative(self, client):
        make_admin(client)
        op = make_user("op_trial_low", role="operario", salary=50_000,
                        hire_date=date.today())
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)
        assert e.base_salary == 0  # max(50_000 - 100_000, 0)

    def test_quality_error_ignored_for_bonus_during_trial(self, client):
        make_admin(client)
        op = make_user("op_trial_qc", role="operario", salary=800_000,
                        hire_date=date.today())
        create_quality_error(client, "grave", [op.id])
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)
        assert e.bonus == 0  # ya era 0 por estar en prueba
        assert e.total == 700_000  # el error no resta nada adicional

    def test_pending_vale_is_swept_into_new_period_and_deducted(self, client):
        make_admin(client)
        op = make_user("op_vale", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        create_vale(client, op.id, 50_000, "Adelanto")
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)
        assert e.deduction_vales == 50_000
        assert e.total == 1_050_000

        vale = Vale.query.filter_by(employee_id=op.id).first()
        assert vale.period_id == period.id

    def test_vale_already_assigned_is_not_swept_into_a_second_period(self, client):
        make_admin(client)
        op = make_user("op_vale2", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        create_vale(client, op.id, 50_000)
        period1 = create_period(client, date.today() - timedelta(days=28), date.today() - timedelta(days=15))
        e1 = entry_for(period1.id, op.id)
        assert e1.deduction_vales == 50_000

        period2 = create_period(client, date.today() - timedelta(days=14), date.today())
        e2 = entry_for(period2.id, op.id)
        assert e2.deduction_vales == 0  # ya se descontó en period1, no se duplica

    def test_only_active_operarios_get_entries(self, client):
        make_admin(client)
        make_user("op_lider", role="lider", salary=1_000_000)
        make_user("op_admin2", role="admin", salary=1_000_000)
        make_user("op_inactive", role="operario", salary=1_000_000, is_active=False)
        op_ok = make_user("op_ok", role="operario", salary=1_000_000,
                           hire_date=date.today() - timedelta(days=200))

        period = create_period(client, date.today() - timedelta(days=14), date.today())
        entries = PayrollEntry.query.filter_by(period_id=period.id).all()

        assert len(entries) == 1
        assert entries[0].employee_id == op_ok.id


# =====================================================================
# E. payroll_entry_update: ediciones manuales sobre una quincena
# =====================================================================
class TestPayrollEntryUpdate:
    def test_absence_deduction_formula(self, client):
        make_admin(client)
        op = make_user("op_abs", role="operario", salary=900_000,
                        hire_date=date.today() - timedelta(days=200))
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)

        resp = client.post(
            f"/payroll/{period.id}/entry/{e.id}/update",
            json={"absence_days": 3},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

        e = PayrollEntry.query.get(e.id)
        assert e.absence_days == 3
        assert e.deduction_absences == round(900_000 / 30 * 3)
        assert e.total == data["total"]

    def test_cannot_edit_entry_after_period_paid(self, client):
        make_admin(client)
        op = make_user("op_paid", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)
        client.post(f"/payroll/{period.id}/pay")

        resp = client.post(
            f"/payroll/{period.id}/entry/{e.id}/update",
            json={"deduction_other": 50_000},
        )
        assert resp.status_code == 400
        e = PayrollEntry.query.get(e.id)
        assert e.deduction_other == 0  # no se aplicó el cambio

    def test_bonus_extra_forced_to_zero_during_trial(self, client):
        make_admin(client)
        op = make_user("op_trial_bonus", role="operario", salary=800_000,
                        hire_date=date.today())
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)

        resp = client.post(
            f"/payroll/{period.id}/entry/{e.id}/update",
            json={"bonus_extra": 100_000},
        )
        assert resp.status_code == 200
        e = PayrollEntry.query.get(e.id)
        assert e.bonus_extra == 0  # bloqueado por estar en prueba

    def test_bonus_extra_applied_for_non_trial_employee(self, client):
        make_admin(client)
        op = make_user("op_bonus_extra", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)

        resp = client.post(
            f"/payroll/{period.id}/entry/{e.id}/update",
            json={"bonus_extra": 30_000},
        )
        assert resp.status_code == 200
        e = PayrollEntry.query.get(e.id)
        assert e.bonus_extra == 30_000
        assert e.total == 1_000_000 + 100_000 + 30_000

    def test_vale_added_via_payroll_screen_updates_entry_once(self, client):
        make_admin(client)
        op = make_user("op_vale3", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        e = entry_for(period.id, op.id)
        assert e.deduction_vales == 0

        resp = client.post(f"/payroll/{period.id}/vale/new", data={
            "employee_id": op.id, "amount": 20_000, "description": "Adelanto rápido",
        }, follow_redirects=True)
        assert resp.status_code == 200

        e = PayrollEntry.query.get(e.id)
        assert e.deduction_vales == 20_000
        assert e.total == 1_100_000 - 20_000

        # Y que quede sincronizado también por la vía de entry_update
        client.post(f"/payroll/{period.id}/entry/{e.id}/update", json={"absence_days": 0})
        e = PayrollEntry.query.get(e.id)
        assert e.deduction_vales == 20_000


# =====================================================================
# F. payroll_pay / payroll_delete
# =====================================================================
class TestPayrollLifecycle:
    def test_cannot_pay_twice(self, client):
        make_admin(client)
        make_user("op_pay2", role="operario", salary=1_000_000,
                  hire_date=date.today() - timedelta(days=200))
        period = create_period(client, date.today() - timedelta(days=14), date.today())

        client.post(f"/payroll/{period.id}/pay")
        first_paid_at = PayrollPeriod.query.get(period.id).paid_at

        client.post(f"/payroll/{period.id}/pay")
        second_paid_at = PayrollPeriod.query.get(period.id).paid_at
        assert first_paid_at == second_paid_at

    def test_delete_draft_period_releases_vales_and_errors(self, client):
        make_admin(client)
        op = make_user("op_del", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        create_vale(client, op.id, 20_000)
        create_quality_error(client, "leve", [op.id])
        period = create_period(client, date.today() - timedelta(days=14), date.today())

        vale = Vale.query.filter_by(employee_id=op.id).first()
        error = QualityError.query.first()
        assert vale.period_id == period.id
        assert error.period_id == period.id

        client.post(f"/payroll/{period.id}/delete")

        assert PayrollEntry.query.filter_by(period_id=period.id).count() == 0
        assert Vale.query.get(vale.id).period_id is None
        assert QualityError.query.get(error.id).period_id is None

    def test_cannot_delete_paid_period(self, client):
        make_admin(client)
        make_user("op_del2", role="operario", salary=1_000_000,
                  hire_date=date.today() - timedelta(days=200))
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        client.post(f"/payroll/{period.id}/pay")

        client.post(f"/payroll/{period.id}/delete")
        assert PayrollPeriod.query.get(period.id) is not None


# =====================================================================
# G. Guardas de eliminación de vales / errores sueltos
# =====================================================================
class TestDeletionGuards:
    def test_cannot_delete_vale_already_assigned_to_period(self, client):
        make_admin(client)
        op = make_user("op_guard1", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        create_vale(client, op.id, 10_000)
        period = create_period(client, date.today() - timedelta(days=14), date.today())
        vale = Vale.query.filter_by(employee_id=op.id).first()

        client.post(f"/vales/{vale.id}/delete", follow_redirects=True)
        assert Vale.query.get(vale.id) is not None

    def test_cannot_delete_quality_error_already_assigned_to_period(self, client):
        make_admin(client)
        op = make_user("op_guard2", role="operario", salary=1_000_000,
                        hire_date=date.today() - timedelta(days=200))
        create_quality_error(client, "leve", [op.id])
        create_period(client, date.today() - timedelta(days=14), date.today())
        error = QualityError.query.first()

        client.post(f"/quality-errors/{error.id}/delete", follow_redirects=True)
        assert QualityError.query.get(error.id) is not None
