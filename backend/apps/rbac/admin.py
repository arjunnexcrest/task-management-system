from django.contrib import admin
from .models import MembershipRole, Permission, Role, RolePermission


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_system_role")
    search_fields = ("name", "code")
    list_filter = ("is_system_role",)


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("code", "description")
    search_fields = ("code",)


@admin.register(MembershipRole)
class MembershipRoleAdmin(admin.ModelAdmin):
    list_display = ("membership", "role", "assigned_by", "assigned_at")
    list_filter = ("role",)


@admin.register(RolePermission)
class RolePermissionAdmin(admin.ModelAdmin):
    list_display = ("role", "permission")
    list_filter = ("role",)
