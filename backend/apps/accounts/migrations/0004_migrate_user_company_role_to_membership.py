# Data migration: move company and role from accounts.User into companies.CompanyMembership
from django.db import migrations


def copy_user_company_role_to_memberships(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    CompanyMembership = apps.get_model("companies", "CompanyMembership")

    for user in User.objects.exclude(company_id__isnull=True):
        role_val = getattr(user, "role", None) or "Employee"
        is_active_val = bool(user.is_active)
        status_val = "Active" if is_active_val else "Inactive"

        membership, created = CompanyMembership.objects.get_or_create(
            user_id=user.id,
            company_id=user.company_id,
            defaults={
                "role": role_val,
                "is_active": is_active_val,
                "status": status_val,
            },
        )
        if not created:
            membership.role = role_val
            membership.is_active = is_active_val
            membership.status = status_val
            membership.save(update_fields=["role", "is_active", "status"])


def copy_memberships_back_to_user(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    CompanyMembership = apps.get_model("companies", "CompanyMembership")

    for membership in CompanyMembership.objects.filter(is_active=True).order_by("joined_at"):
        User.objects.filter(pk=membership.user_id, company_id__isnull=True).update(
            company_id=membership.company_id,
            role=membership.role,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_user_company_user_role"),
        ("companies", "0003_companymembership_is_active_companymembership_role_and_more"),
    ]

    operations = [
        migrations.RunPython(
            copy_user_company_role_to_memberships,
            reverse_code=copy_memberships_back_to_user,
        ),
    ]
