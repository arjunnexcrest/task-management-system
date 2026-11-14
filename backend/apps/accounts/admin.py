from django.contrib import admin
from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "username", "full_name", "employee_id", "designation", "account_status", "is_staff")
    list_filter = ("account_status", "is_staff", "is_active")
    search_fields = ("email", "username", "full_name", "employee_id")
