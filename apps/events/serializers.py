from rest_framework import serializers
from decimal import Decimal
from .models import Event
from .services import EventService

from apps.tasks.models import LogisticTask 


class NestedTaskCreateSerializer(serializers.ModelSerializer):

    """
    Serializador simplificado EXCLUSIVO para recibir tareas
    al momento de crear un Evento.

    Se utiliza cuando un evento y sus subtareas se crean
    mediante una sola petición POST /api/v1/events/.
    """
    # Permitimos recibir el ID cuando estamos editando
    # una subtarea existente.
    id = serializers.IntegerField(required=False)

    # ============================================================
    # VALIDACIÓN DE HORAS ESTIMADAS
    # ============================================================
    
    # ============================================================

    estimated_hours = serializers.DecimalField(
        max_digits=4,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )

    class Meta:
        model = LogisticTask
        fields = [
            'id',
            'title',
            'scheduled_date',
            'estimated_hours',
            'status'
        ]

     

  

class EventSerializer(serializers.ModelSerializer):
    """Serializer para modelo Event con métricas de progreso calculadas en memoria y creación anidada."""

    progress_percentage = serializers.SerializerMethodField()
    total_tasks = serializers.SerializerMethodField()
    completed_tasks = serializers.SerializerMethodField()
    
    #Campo para recibir el array de tareas en el JSON
    tasks = NestedTaskCreateSerializer(many=True, required=False)

    class Meta:
        model = Event
        fields = [
            "id",
            "title",
            "course",          
            "activity_type",   
            "description",
            "event_date",
            "progress_percentage",
            "total_tasks",
            "completed_tasks",
            "tasks",           #CREACION ANIDADA
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "progress_percentage",
            "total_tasks",
            "completed_tasks",
            "created_at",
            "updated_at",
        ]

    # --- LÓGICA DE MÉTRICAS (T4) INTACTA ---
    def _get_metrics(self, obj):
        if not hasattr(obj, "_cached_metrics"):
            obj._cached_metrics = EventService.calculate_progress(obj)
        return obj._cached_metrics

    def get_progress_percentage(self, obj) -> float:
        return self._get_metrics(obj)["progress_percentage"]

    def get_total_tasks(self, obj) -> int:
        return self._get_metrics(obj)["total_tasks"]

    def get_completed_tasks(self, obj) -> int:
        return self._get_metrics(obj)["completed_tasks"]

    def validate(self, attrs):
        user = self.context.get("request").user if "request" in self.context else None
        tasks_data = attrs.get("tasks", [])
        if user and tasks_data:
            from apps.tasks.services import TaskService
            daily_hours = {}
            for task_data in tasks_data:
                s_date = task_data.get("scheduled_date")
                e_hours = task_data.get("estimated_hours")
                if s_date and e_hours:
                    daily_hours[s_date] = daily_hours.get(s_date, Decimal("0.00")) + Decimal(str(e_hours))
            for s_date, total_add_hours in daily_hours.items():
                TaskService.validate_daily_overload(
                    user=user,
                    target_date=s_date,
                    additional_hours=total_add_hours,
                )
        return attrs

    # --- LÓGICA DE CREACIÓN  ---
    def create(self, validated_data):
        # 1. Asignamos el usuario 
        validated_data["user"] = self.context["request"].user
        
        # 2. Extraemos las tareas del JSON
        tasks_data = validated_data.pop('tasks', [])
        
        # 3. Creamos el Evento principal usando super()
        event = super().create(validated_data)
        
         # 4. Creamos cada subtarea.
        for task_data in tasks_data:

            # El frontend puede enviar un ID temporal
            # para manejar la interfaz.
            #
            # Ese ID NO debe guardarse en Django.
            task_data.pop('id', None)

            LogisticTask.objects.create(
                event=event,
                **task_data
            )


        return event
 

    def update(self, instance, validated_data):


        """
        Actualiza un evento existente y sus subtareas.

        - Si una subtarea trae ID, se actualiza esa subtarea.
        - Si una subtarea no trae ID, se crea como nueva.
        - Las subtareas existentes que no vienen en la petición
        se eliminan.
        """

        # Sacamos las subtareas antes de actualizar el evento.
        tasks_data = validated_data.pop('tasks', None)

        # Actualizamos los datos principales del evento.
        instance = super().update(instance, validated_data)

        # Si no se enviaron tareas, no hacemos nada con ellas.
        if tasks_data is None:
            return instance

        # IDs de las subtareas que siguen existiendo después de editar.
        task_ids = []

        for task_data in tasks_data:

            # Obtenemos el ID si viene desde el frontend.
            task_id = task_data.pop('id', None)

            if task_id is not None:
                # -------------------------------------------------
                # SUBTAREA EXISTENTE
                # -------------------------------------------------

                try:
                    # Buscamos la tarea únicamente dentro del evento
                    # que estamos editando.
                    task = instance.tasks.get(id=task_id)

                except LogisticTask.DoesNotExist:
                    raise serializers.ValidationError({
                        "tasks": (
                            f"La subtarea con ID {task_id} "
                            "no pertenece a este evento."
                        )
                    })

                # Actualizamos los campos de la subtarea.
                for field, value in task_data.items():
                    setattr(task, field, value)

                task.save()

                # Conservamos el ID porque la tarea sigue existiendo.
                task_ids.append(task.id)

            else:
                # -------------------------------------------------
                # SUBTAREA NUEVA
                # -------------------------------------------------

                task = LogisticTask.objects.create(
                    event=instance,
                    **task_data
                )

                task_ids.append(task.id)

        # ---------------------------------------------------------
        # ELIMINAR SUBTAREAS QUE EL USUARIO QUITÓ DEL FORMULARIO
        # ---------------------------------------------------------

        instance.tasks.exclude(id__in=task_ids).delete()

        return instance


    # NUEVO
    def validate_activity_type(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError(
                "El tipo de actividad es obligatorio."
            )

        return value