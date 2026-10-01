from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.exceptions import DailyOverloadConflict
from .models import LogisticTask, RescheduleHistory

# Constantes de configuración de límites de horas
DEFAULT_DAILY_HOUR_LIMIT: Decimal = getattr(
    settings, "DEFAULT_DAILY_HOUR_LIMIT", Decimal("6.00")
)
MIN_TASK_HOURS: Decimal = getattr(settings, "MIN_TASK_HOURS", Decimal("0.25"))
MAX_TASK_HOURS: Decimal = getattr(settings, "MAX_TASK_HOURS", Decimal("24.00"))


class TaskService:
    """
    Servicio de lógica de negocio para tareas logísticas y dashboard.
    Implementa prevención N+1 vía select_related('event', 'category')
    y validación estricta de la regla de sobrecarga diaria (> user.daily_hour_limit).
    """

    DEFAULT_DAILY_HOUR_LIMIT = DEFAULT_DAILY_HOUR_LIMIT
    MIN_TASK_HOURS = MIN_TASK_HOURS
    MAX_TASK_HOURS = MAX_TASK_HOURS

    @staticmethod
    def get_today_tasks(user, target_date=None):
        """
        Obtiene las tareas programadas para la fecha indicada (por defecto hoy)
        utilizando select_related('event', 'category') para eliminar consultas N+1.
        """
        if target_date is None:
            target_date = timezone.localdate()

        return LogisticTask.objects.filter(
            event__user=user,
            scheduled_date=target_date,
        ).select_related("event", "category")

    @staticmethod
    def get_daily_scheduled_hours(user, target_date, exclude_task_id=None) -> Decimal:
        """
        Calcula la suma de horas programadas por un usuario para una fecha determinada.
        Permite excluir una tarea (útil en actualizaciones o reprogramaciones).
        Excluye las tareas completadas del cálculo.
        """
        queryset = LogisticTask.objects.filter(
            event__user=user,
            scheduled_date=target_date,
        ).exclude(status=LogisticTask.Status.COMPLETED)
        if exclude_task_id:
            queryset = queryset.exclude(id=exclude_task_id)

        aggregate_result = queryset.aggregate(total_hours=Sum("estimated_hours"))
        total = aggregate_result["total_hours"]
        return Decimal(str(total)) if total is not None else Decimal("0.00")

    @classmethod
    def get_suggested_dates(
        cls, user, target_date, hours: Decimal, days_ahead: int = 7, exclude_task_id=None
    ) -> list[str]:
        """
        Calcula fechas alternativas a partir de target_date en las que el usuario
        cuenta con capacidad disponible suficiente para programar 'hours'.
        Retorna una lista de cadenas en formato YYYY-MM-DD (máximo 3 sugerencias).
        """
        suggestions = []
        daily_limit = (
            getattr(user, "daily_hour_limit", None) or cls.DEFAULT_DAILY_HOUR_LIMIT
        )
        for i in range(1, days_ahead + 1):
            candidate_date = target_date + timedelta(days=i)
            current_hours = cls.get_daily_scheduled_hours(
                user, candidate_date, exclude_task_id=exclude_task_id
            )
            if current_hours + Decimal(str(hours)) <= daily_limit:
                suggestions.append(candidate_date.isoformat())
                if len(suggestions) >= 3:
                    break
        return suggestions

    @classmethod
    def validate_daily_overload(
        cls, user, target_date, additional_hours: Decimal, exclude_task_id=None
    ):
        """
        Verifica si agregar 'additional_hours' a la fecha 'target_date' excede el límite
        diario permitido para el usuario (user.daily_hour_limit o DEFAULT_DAILY_HOUR_LIMIT).
        Si se sobrepasa, lanza una excepción DailyOverloadConflict (HTTP 409) con fechas sugeridas.
        """
        current_hours = cls.get_daily_scheduled_hours(
            user, target_date, exclude_task_id=exclude_task_id
        )
        additional_decimal = Decimal(str(additional_hours))
        projected_hours = current_hours + additional_decimal
        daily_limit = (
            getattr(user, "daily_hour_limit", None) or cls.DEFAULT_DAILY_HOUR_LIMIT
        )

        if projected_hours > daily_limit:
            suggested_dates = cls.get_suggested_dates(
                user=user,
                target_date=target_date,
                hours=additional_decimal,
                exclude_task_id=exclude_task_id,
            )
            raise DailyOverloadConflict(
                target_date=target_date,
                current_hours=current_hours,
                attempted_hours=additional_decimal,
                daily_limit=daily_limit,
                suggested_dates=suggested_dates,
            )

    @classmethod
    def get_daily_dashboard(cls, user, target_date=None) -> dict:
        """
        Genera el resumen de carga diaria para el dashboard del usuario.
        """
        if target_date is None:
            target_date = timezone.localdate()

        tasks_qs = cls.get_today_tasks(user, target_date)
        total_hours = cls.get_daily_scheduled_hours(user, target_date)
        daily_limit = (
            getattr(user, "daily_hour_limit", None) or cls.DEFAULT_DAILY_HOUR_LIMIT
        )
        capacity_remaining = max(Decimal("0.00"), daily_limit - total_hours)
        is_overloaded = total_hours > daily_limit

        return {
            "date": target_date,
            "daily_hour_limit": daily_limit,
            "total_hours_scheduled": total_hours,
            "capacity_remaining": capacity_remaining,
            "is_overloaded": is_overloaded,
            "tasks": tasks_qs,
        }

    @classmethod
    def reschedule_task(
        cls, task: LogisticTask, user, new_date, new_hours: Decimal, reason: str
    ) -> LogisticTask:
        """
        Reprograma una tarea logística, validando la regla de sobrecarga para la nueva fecha,
        generando la auditoría en RescheduleHistory y actualizando la tarea en una transacción atómica.
        """
        new_hours_decimal = Decimal(str(new_hours))

        # Validar sobrecarga en la nueva fecha (excluyendo la tarea si es el mismo día)
        cls.validate_daily_overload(
            user=user,
            target_date=new_date,
            additional_hours=new_hours_decimal,
            exclude_task_id=task.id,
        )

        with transaction.atomic():
            RescheduleHistory.objects.create(
                task=task,
                user=user,
                previous_date=task.scheduled_date,
                new_date=new_date,
                previous_hours=task.estimated_hours,
                new_hours=new_hours_decimal,
                reason=reason,
            )

            task.scheduled_date = new_date
            task.estimated_hours = new_hours_decimal
            task.save(
                update_fields=[
                    "scheduled_date",
                    "estimated_hours",
                    "updated_at",
                ]
            )

        return task
