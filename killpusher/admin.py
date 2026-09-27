from django.contrib import admin

from .models import Submission, TrackedCharacter


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Submission)
class SubmissionAdmin(ReadOnlyAdmin):
    list_display = ("killmail_id", "state", "attempted_at", "submitted_at", "submitted_by")
    list_filter = ("state",)
    search_fields = ("=killmail_id",)


@admin.register(TrackedCharacter)
class TrackedCharacterAdmin(ReadOnlyAdmin):
    list_display = ("ownership", "last_sync", "next_poll", "next_page", "error")
    exclude = ("token",)
