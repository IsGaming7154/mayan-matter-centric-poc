from django.utils.translation import gettext_lazy as _

from mayan.apps.app_manager.apps import MayanAppConfig


class FileMetadataMSGExtendedApp(MayanAppConfig):
    app_namespace = 'file_metadata_msg_extended'
    app_url = 'file_metadata_msg_extended'
    has_tests = False
    name = 'mayan_extensions.file_metadata_msg_extended'
    verbose_name = _(message='File metadata MSG (extended: body/date/attachments)')
