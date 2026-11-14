from django.contrib import admin
from .models import Company, CompanyMembership


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "code")


@admin.register(CompanyMembership)
class CompanyMembershipAdmin(admin.ModelAdmin):
    list_display = ("company", "user", "status", "joined_at", "left_at")
    list_filter = ("status", "company")
    search_fields = ("user__email", "company__name", "company__code")
