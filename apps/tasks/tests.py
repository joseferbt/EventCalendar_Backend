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
