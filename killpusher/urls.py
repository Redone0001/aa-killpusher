from django.urls import path

from . import views

app_name = "killpusher"
urlpatterns = [
    path("", views.index, name="index"),
    path("connect/", views.connect, name="connect"),
    path("refresh/", views.refresh_all, name="refresh"),
    path("import-characters/", views.import_characters, name="import_characters"),
    path("characters/<int:character_pk>/disconnect/", views.disconnect, name="disconnect"),
    path("killmails/<int:killmail_id>/push/", views.push, name="push"),
    path("killmails/<int:killmail_id>/check/", views.check_submission, name="check"),
]
