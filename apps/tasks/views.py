from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import IsOwner
from .models import LogisticTask, TaskCategory
from .serializers import (
    LogisticTaskListSerializer,
    LogisticTaskSerializer,
    RescheduleHistorySerializer,
    RescheduleTaskSerializer,
    TaskCategorySerializer,
)
from .services import TaskService

from drf_spectacular.utils import extend_schema
#@extend_schema(tags=["Tasks"])

class TaskCategoryViewSet(viewsets.ModelViewSet):
    """
    CRUD para categorías de tareas pertenecientes al usuario autenticado.
    """

    serializer_class = TaskCategorySerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return TaskCategory.objects.none()
        return TaskCategory.objects.filter(user=self.request.user)


class LogisticTaskViewSet(viewsets.ModelViewSet):
    """
    CRUD para tareas logísticas con endpoints adicionales para reprogramar y consultar historial.
    Asegura optimización con select_related y permisos por usuario.
    """

    permission_classes = [permissions.IsAuthenticated, IsOwner]

    def get_serializer_class(self):
        if self.action in ["list", "retrieve"]:
            return LogisticTaskListSerializer
        return LogisticTaskSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return LogisticTask.objects.none()
        queryset = LogisticTask.objects.filter(
            event__user=self.request.user
        ).select_related("event", "category")

        # Filtro opcional por evento
        event_id = self.request.query_params.get("event")
        if event_id:
            queryset = queryset.filter(event_id=event_id)

        # Filtro opcional por fecha
        date_filter = self.request.query_params.get("date")
        if date_filter:
            queryset = queryset.filter(scheduled_date=date_filter)

        return queryset

    @action(detail=True, methods=["post"], url_path="reschedule")
    def reschedule(self, request, pk=None):
        """
        Reprograma la fecha y horas estimadas de la tarea, auditando el cambio en RescheduleHistory.
        Si la nueva fecha excede el límite diario del usuario, retorna HTTP 409 Conflict.
        """
        task = self.get_object()
        serializer = RescheduleTaskSerializer(
            data=request.data, context={"task": task, "request": request}
        )
        serializer.is_valid(raise_exception=True)

        updated_task = TaskService.reschedule_task(
            task=task,
            user=request.user,
            new_date=serializer.validated_data["new_date"],
            new_hours=serializer.validated_data["new_hours"],
            reason=serializer.validated_data["reason"],
        )

        return Response(
            LogisticTaskListSerializer(updated_task).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        """
        Consulta el historial de reprogramaciones de una tarea específica.
        """
        task = self.get_object()
        history_qs = task.reschedule_history.all().select_related("user")
        serializer = RescheduleHistorySerializer(history_qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
