from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.exceptions import DailyOverloadConflict
from apps.events.models import Event
from apps.tasks.models import LogisticTask, RescheduleHistory, TaskCategory
from apps.tasks.services import TaskService

User = get_user_model()


class TaskServiceOverloadTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="taskplanner",
            email="planner@example.com",
            password="securepassword123",
            daily_hour_limit=Decimal("6.00"),
        )
        self.event = Event.objects.create(
            user=self.user,
            title="Conferencia Anual",
            event_date=date.today() + timedelta(days=10),
        )
        self.category = TaskCategory.objects.create(
            user=self.user,
            name="Sonido y Luces",
        )

    def test_daily_scheduled_hours_calculation(self):
        target_date = date.today()
        LogisticTask.objects.create(
            event=self.event,
            category=self.category,
            title="Montaje de escenario",
            scheduled_date=target_date,
            estimated_hours=Decimal("4.00"),
        )
        hours = TaskService.get_daily_scheduled_hours(self.user, target_date)
        self.assertEqual(hours, Decimal("4.00"))

    def test_overload_throws_409_conflict(self):
        target_date = date.today()
        LogisticTask.objects.create(
            event=self.event,
            category=self.category,
            title="Pruebas de sonido",
            scheduled_date=target_date,
            estimated_hours=Decimal("4.50"),
        )

        # Intentar sumar 2.00 horas más superará el límite de 6.00 (4.50 + 2.00 = 6.50)
        with self.assertRaises(DailyOverloadConflict):
            TaskService.validate_daily_overload(
                user=self.user,
                target_date=target_date,
                additional_hours=Decimal("2.00"),
            )

    def test_reschedule_task_and_history_creation(self):
        original_date = date.today()
        new_date = date.today() + timedelta(days=2)
        task = LogisticTask.objects.create(
            event=self.event,
            category=self.category,
            title="Instalación de proyectores",
            scheduled_date=original_date,
            estimated_hours=Decimal("3.00"),
        )

        updated_task = TaskService.reschedule_task(
            task=task,
            user=self.user,
            new_date=new_date,
            new_hours=Decimal("2.50"),
            reason="Retraso en el envío de equipos",
        )

        self.assertEqual(updated_task.scheduled_date, new_date)
        self.assertEqual(updated_task.estimated_hours, Decimal("2.50"))
        self.assertEqual(updated_task.status, LogisticTask.Status.PENDING)

        history = RescheduleHistory.objects.filter(task=task).first()
        self.assertIsNotNone(history)
        self.assertEqual(history.previous_date, original_date)
        self.assertEqual(history.new_date, new_date)
        self.assertEqual(history.previous_hours, Decimal("3.00"))
        self.assertEqual(history.new_hours, Decimal("2.50"))
        self.assertEqual(history.reason, "Retraso en el envío de equipos")


class TaskRescheduleApiTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="rescheduler",
            email="rescheduler@example.com",
            password="securepassword123",
        )
        self.event = Event.objects.create(
            user=self.user,
            title="Actividad de prueba",
            course="Proyecto Integrador",
            event_date=date.today() + timedelta(days=10),
        )
        self.task = LogisticTask.objects.create(
            event=self.event,
            title="Buscar proveedor",
            scheduled_date=date.today(),
            estimated_hours=Decimal("1.00"),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_patch_scheduled_date_persists_the_reschedule(self):
        new_date = date.today() + timedelta(days=4)

        response = self.client.patch(
            f"/api/v1/tasks/{self.task.id}/",
            {"scheduled_date": new_date.isoformat()},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["scheduled_date"], new_date.isoformat())
        self.task.refresh_from_db()
        self.assertEqual(self.task.scheduled_date, new_date)

    def test_patch_rejects_an_invalid_scheduled_date(self):
        response = self.client.patch(
            f"/api/v1/tasks/{self.task.id}/",
            {"scheduled_date": "not-a-date"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.task.refresh_from_db()
        self.assertEqual(self.task.scheduled_date, date.today())

    def test_task_list_includes_the_event_course_for_filters(self):
        response = self.client.get("/api/v1/tasks/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["event_title"], self.event.title)
        self.assertEqual(response.data["results"][0]["event_course"], self.event.course)

    def test_calculation_excludes_completed_tasks(self):
        target_date = date.today() + timedelta(days=5)
        # Tarea completada de 5 horas
        LogisticTask.objects.create(
            event=self.event,
            title="Tarea completada previa",
            scheduled_date=target_date,
            estimated_hours=Decimal("5.00"),
            status=LogisticTask.Status.COMPLETED,
        )
        # Tarea pendiente de 4 horas
        LogisticTask.objects.create(
            event=self.event,
            title="Tarea pendiente activa",
            scheduled_date=target_date,
            estimated_hours=Decimal("4.00"),
            status=LogisticTask.Status.PENDING,
        )

        hours = TaskService.get_daily_scheduled_hours(self.user, target_date)
        # Debe ser 4.00, excluyendo las 5.00 horas de la tarea completada
        self.assertEqual(hours, Decimal("4.00"))

    def test_scenario_5h_plus_2h_equals_7h_conflict(self):
        """
        Escenario DOD: 5h existentes + 2h a reprogramar = 7h.
        Supera el límite diario de 6 horas -> retorna HTTP 409 y no guarda cambios.
        """
        target_date = date.today() + timedelta(days=3)
        # 5h ya programadas en la fecha destino
        LogisticTask.objects.create(
            event=self.event,
            title="Tarea existente de 5h",
            scheduled_date=target_date,
            estimated_hours=Decimal("5.00"),
            status=LogisticTask.Status.PENDING,
        )

        original_date = self.task.scheduled_date
        original_hours = self.task.estimated_hours

        # Intentar reprogramar tarea de 2h a target_date (5h + 2h = 7h)
        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {
                "new_date": target_date.isoformat(),
                "new_hours": "2.00",
                "reason": "Intento de reprogramación con sobrecarga",
            },
            format="json",
        )

        # Debe responder con HTTP 409 Conflict
        self.assertEqual(response.status_code, 409)
        self.assertIn("detail", response.data)
        self.assertEqual(response.data.get("error"), "DailyOverloadConflict")

        # La tarea NO debe haber sido modificada en la base de datos
        self.task.refresh_from_db()
        self.assertEqual(self.task.scheduled_date, original_date)
        self.assertEqual(self.task.estimated_hours, original_hours)

        # NO debe haberse creado historial de reprogramación
        self.assertEqual(RescheduleHistory.objects.filter(task=self.task).count(), 0)

    def test_scenario_4h_plus_2h_equals_6h_allowed(self):
        """
        Escenario DOD: 4h existentes + 2h a reprogramar = 6h.
        No supera el límite diario de 6 horas -> se guarda correctamente y se registra en RescheduleHistory.
        """
        target_date = date.today() + timedelta(days=6)
        # 4h ya programadas en la fecha destino
        LogisticTask.objects.create(
            event=self.event,
            title="Tarea existente de 4h",
            scheduled_date=target_date,
            estimated_hours=Decimal("4.00"),
            status=LogisticTask.Status.PENDING,
        )

        original_date = self.task.scheduled_date

        # Reprogramar tarea con 2h a target_date (4h + 2h = 6h <= 6h)
        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {
                "new_date": target_date.isoformat(),
                "new_hours": "2.00",
                "reason": "Ajuste de cronograma permitido",
            },
            format="json",
        )

        # Debe responder con HTTP 200 OK
        self.assertEqual(response.status_code, 200)

        # La tarea debe actualizarse en la base de datos
        self.task.refresh_from_db()
        self.assertEqual(self.task.scheduled_date, target_date)
        self.assertEqual(self.task.estimated_hours, Decimal("2.00"))

        # Debe haberse registrado en RescheduleHistory
        history = RescheduleHistory.objects.filter(task=self.task).first()
        self.assertIsNotNone(history)
        self.assertEqual(history.previous_date, original_date)
        self.assertEqual(history.new_date, target_date)
        self.assertEqual(history.new_hours, Decimal("2.00"))
        self.assertEqual(history.reason, "Ajuste de cronograma permitido")


class TaskRescheduleDoDTest(TestCase):
    """
    Pruebas de aceptación del Definition of Done (DoD) para la historia de
    resolución de conflictos de sobrecarga diaria.
    Cubren las 10 verificaciones explícitas del DoD.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="dod_user",
            email="dod@example.com",
            password="securepassword123",
            daily_hour_limit=Decimal("6.00"),
        )
        self.event = Event.objects.create(
            user=self.user,
            title="Evento DoD",
            event_date=date.today() + timedelta(days=20),
        )
        # Tarea base de 2h para la mayoría de los escenarios
        self.task = LogisticTask.objects.create(
            event=self.event,
            title="Tarea base DoD",
            scheduled_date=date.today(),
            estimated_hours=Decimal("2.00"),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # ── DoD 1 & 4: Mover subtarea a un día sin sobrecarga ──────────────────────

    def test_move_task_to_free_day_succeeds_and_persists_scheduled_date(self):
        """
        DoD: Mover subtarea a un día sin sobrecarga → HTTP 200.
        La nueva scheduled_date queda persistida en la BD.
        """
        free_date = date.today() + timedelta(days=10)

        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {"new_date": free_date.isoformat(), "new_hours": "2.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)

        self.task.refresh_from_db()
        # Verificar persistencia de scheduled_date
        self.assertEqual(self.task.scheduled_date, free_date)
        self.assertEqual(self.task.estimated_hours, Decimal("2.00"))

    # ── DoD 2: Mover a fecha que todavía genera conflicto ─────────────────────

    def test_move_task_to_overloaded_day_returns_409_with_suggested_dates(self):
        """
        DoD: Mover subtarea a fecha que aún genera conflicto → HTTP 409.
        La respuesta incluye 'suggested_dates' para resolver el conflicto.
        """
        crowded_date = date.today() + timedelta(days=2)
        # Llenar la fecha destino con 5h
        LogisticTask.objects.create(
            event=self.event,
            title="Tarea bloqueante de 5h",
            scheduled_date=crowded_date,
            estimated_hours=Decimal("5.00"),
        )

        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {"new_date": crowded_date.isoformat(), "new_hours": "2.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data.get("error"), "DailyOverloadConflict")
        # La respuesta debe incluir suggested_dates (lista, puede estar vacía)
        self.assertIn("suggested_dates", response.data)
        self.assertIsInstance(response.data["suggested_dates"], list)

        # La tarea no debe haberse modificado
        self.task.refresh_from_db()
        self.assertEqual(self.task.scheduled_date, date.today())

    # ── DoD 3 & 8: Reducir horas y resolver el conflicto ──────────────────────

    def test_reduce_hours_resolves_conflict_and_persists_estimated_hours(self):
        """
        DoD: Reducir horas y resolver el conflicto → HTTP 200.
        La nueva estimated_hours queda persistida en la BD.
        """
        crowded_date = date.today() + timedelta(days=4)
        # 5h ya programadas
        LogisticTask.objects.create(
            event=self.event,
            title="Tarea existente de 5h",
            scheduled_date=crowded_date,
            estimated_hours=Decimal("5.00"),
        )

        # Reprogramar con sólo 1h (5h + 1h = 6h ≤ 6h) → debe resolver
        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {
                "new_date": crowded_date.isoformat(),
                "new_hours": "1.00",
                "reason": "Reducción de horas para resolver sobrecarga",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)

        # Verificar persistencia de estimated_hours
        self.task.refresh_from_db()
        self.assertEqual(self.task.estimated_hours, Decimal("1.00"))
        self.assertEqual(self.task.scheduled_date, crowded_date)

    # ── DoD 4: Reducir horas pero mantener la sobrecarga ──────────────────────

    def test_reduce_hours_still_overloaded_returns_409(self):
        """
        DoD: Reducir horas pero el día continúa sobrecargado → HTTP 409.
        El error informa la situación actual de la carga.
        """
        crowded_date = date.today() + timedelta(days=5)
        # 5.5h ya programadas
        LogisticTask.objects.create(
            event=self.event,
            title="Tarea bloqueante de 5.5h",
            scheduled_date=crowded_date,
            estimated_hours=Decimal("5.50"),
        )

        # Intentar reprogramar con 1.5h (5.5h + 1.5h = 7h > 6h) → sigue en conflicto
        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {
                "new_date": crowded_date.isoformat(),
                "new_hours": "1.50",
                "reason": "Reducción insuficiente",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data.get("error"), "DailyOverloadConflict")
        # Informa horas actuales y límite para que el frontend muestre el estado
        self.assertIn("current_hours", response.data)
        self.assertIn("daily_hour_limit", response.data)

        # La tarea no debe haberse modificado
        self.task.refresh_from_db()
        self.assertEqual(self.task.scheduled_date, date.today())
        self.assertEqual(self.task.estimated_hours, Decimal("2.00"))

    # ── DoD 5: Seleccionar fecha manualmente cuando no hay sugerencias ─────────

    def test_manual_date_when_no_suggestions_available(self):
        """
        DoD: El usuario puede seleccionar fecha manualmente cuando no hay sugerencias.
        Saturar los próximos 7 días y luego reprogramar a un día libre más adelante.
        """
        # Saturar los próximos 7 días con 6h cada uno
        for i in range(1, 8):
            candidate = date.today() + timedelta(days=i + 10)
            LogisticTask.objects.create(
                event=self.event,
                title=f"Tarea saturación día {i}",
                scheduled_date=candidate,
                estimated_hours=Decimal("6.00"),
            )

        # Elegir manualmente un día que no esté saturado
        manual_date = date.today() + timedelta(days=25)

        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {
                "new_date": manual_date.isoformat(),
                "new_hours": "2.00",
                "reason": "Fecha manual seleccionada por el usuario",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.scheduled_date, manual_date)

    # ── DoD 6: Validar horas fuera del rango permitido ────────────────────────

    def test_hours_below_minimum_returns_400(self):
        """
        DoD: Horas estimadas fuera del rango permitido (< 0.25) → HTTP 400 Bad Request.
        """
        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {"new_date": (date.today() + timedelta(days=1)).isoformat(), "new_hours": "0.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_hours_above_maximum_returns_400(self):
        """
        DoD: Horas estimadas fuera del rango permitido (> 24) → HTTP 400 Bad Request.
        """
        response = self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {"new_date": (date.today() + timedelta(days=1)).isoformat(), "new_hours": "25.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    # ── DoD 9: /hoy y detalle reflejan cambios ────────────────────────────────

    def test_today_endpoint_reflects_rescheduled_task(self):
        """
        DoD: Después de reprogramar, la tarea aparece en el día correcto.
        El endpoint /api/v1/tasks/?date=<new_date> retorna la tarea con los datos actualizados.
        """
        new_date = date.today() + timedelta(days=8)

        self.client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {"new_date": new_date.isoformat(), "new_hours": "1.50"},
            format="json",
        )

        response = self.client.get(f"/api/v1/tasks/?date={new_date.isoformat()}")
        self.assertEqual(response.status_code, 200)

        tasks = response.data.get("results", response.data)
        task_ids = [t["id"] for t in tasks]
        self.assertIn(self.task.id, task_ids)

        # Verificar que scheduled_date y estimated_hours son correctos
        task_data = next(t for t in tasks if t["id"] == self.task.id)
        self.assertEqual(task_data["scheduled_date"], new_date.isoformat())
        self.assertEqual(Decimal(task_data["estimated_hours"]), Decimal("1.50"))

    # ── DoD 10: Manejo de errores de la API ───────────────────────────────────

    def test_reschedule_requires_authentication(self):
        """
        DoD: Las operaciones están protegidas por autenticación.
        Un cliente no autenticado recibe HTTP 401 Unauthorized.
        """
        unauthenticated_client = APIClient()
        response = unauthenticated_client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {"new_date": (date.today() + timedelta(days=1)).isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_reschedule_denies_access_to_other_users_tasks(self):
        """
        DoD: Las operaciones están protegidas por permisos (IsOwner).
        Un usuario distinto no puede reprogramar tareas ajenas → HTTP 403 o 404.
        """
        other_user = User.objects.create_user(
            username="intruder",
            email="intruder@example.com",
            password="securepassword123",
        )
        intruder_client = APIClient()
        intruder_client.force_authenticate(other_user)

        response = intruder_client.post(
            f"/api/v1/tasks/{self.task.id}/reschedule/",
            {"new_date": (date.today() + timedelta(days=1)).isoformat()},
            format="json",
        )
        self.assertIn(response.status_code, [403, 404])

