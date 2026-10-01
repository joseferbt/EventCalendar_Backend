from rest_framework import status
from rest_framework.exceptions import APIException


class DailyOverloadConflict(APIException):
    """
    Excepción personalizada HTTP 409 Conflict.
    Se lanza cuando la programación de tareas excede el límite de horas diarias permitido para el usuario.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "La carga de horas programadas para esta fecha excede el límite diario configurado."
    default_code = "daily_overload_conflict"

    def __init__(
        self,
        detail=None,
        code=None,
        target_date=None,
        current_hours=None,
        attempted_hours=None,
        daily_limit=None,
        suggested_dates=None,
    ):
        if detail is not None and isinstance(detail, dict):
            super().__init__(detail=detail, code=code)
        elif detail is not None:
            super().__init__(detail=detail, code=code)
        elif target_date is not None:
            message = (
                f"La fecha {target_date} excederá el límite diario de {daily_limit} horas. "
                f"Horas actuales: {current_hours}, Horas a programar: {attempted_hours}."
            )
            payload = {
                "error": "DailyOverloadConflict",
                "detail": message,
                "target_date": str(target_date),
                "current_hours": str(current_hours),
                "attempted_hours": str(attempted_hours),
                "daily_hour_limit": str(daily_limit),
                "suggested_dates": list(suggested_dates) if suggested_dates else [],
            }
            super().__init__(detail=payload, code=code)
        else:
            super().__init__(detail=self.default_detail, code=code)
