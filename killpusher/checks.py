from django.core.checks import Error, register
from django.core.exceptions import ImproperlyConfigured

from .conf import user_agent


@register()
def configuration_check(app_configs, **kwargs):
    try:
        user_agent()
    except ImproperlyConfigured as exc:
        return [Error(str(exc), id="killpusher.E001")]
    return []
