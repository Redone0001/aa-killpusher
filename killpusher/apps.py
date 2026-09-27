from django.apps import AppConfig


class KillpusherConfig(AppConfig):
    name = "killpusher"
    verbose_name = "Killmail Pusher"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401
