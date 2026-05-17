from django.utils.translation import gettext_lazy as _

from mayan.apps.app_manager.apps import MayanAppConfig


class MatterUIOverlayApp(MayanAppConfig):
    app_namespace = 'matter_ui_overlay'
    app_url = 'matter_ui_overlay'
    has_tests = False
    name = 'mayan_extensions.matter_ui_overlay'
    verbose_name = _(message='Matter UI overlay (dashboard matter tree)')
