from allianceauth import hooks
from allianceauth.menu.hooks import MenuItemHook
from allianceauth.services.hooks import UrlHook

from . import urls
from .conf import PERMISSION


class KillpusherMenu(MenuItemHook):
    def __init__(self):
        super().__init__("Killmail Pusher", "fa-solid fa-crosshairs", "killpusher:index")

    def render(self, request):
        return super().render(request) if request.user.has_perm(PERMISSION) else ""


@hooks.register("menu_item_hook")
def register_menu():
    return KillpusherMenu()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "killpusher", r"^killpusher/")
