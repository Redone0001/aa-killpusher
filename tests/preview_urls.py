from django.contrib.auth import login
from django.contrib.auth.models import User
from django.shortcuts import redirect
from django.urls import include, path


def demo_login(request):
    login(
        request,
        User.objects.get(username="pilot1"),
        backend="django.contrib.auth.backends.ModelBackend",
    )
    return redirect("killpusher:index")


urlpatterns = [path("demo/", demo_login), path("", include("allianceauth.urls"))]
