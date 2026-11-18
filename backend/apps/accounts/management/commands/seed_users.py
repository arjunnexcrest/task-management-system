"""
Management command to seed initial user login accounts, company, roles,
departments, teams, and reporting relationships into the database.

Usage:
    python manage.py seed_users
"""
from datetime import date
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.accounts.models import User


USERS_TO_SEED = [
    {
        "email": "admin@tms.local",
        "username": "admin",
        "full_name": "System Admin",
        "employee_id": "EMP-001",
        "designation": "Chief Executive Officer",
        "phone": "+91 9876543210",
        "password": "Admin@12345",
        "is_staff": True,
        "is_superuser": True,
        "role_code": "CEO",
        "role_name": "CEO",
    },
    {
        "email": "pm@tms.local",
        "username": "pm",
        "full_name": "Arjun Nair",
        "employee_id": "EMP-002",
        "designation": "Senior Project Manager",
        "phone": "+91 9876543211",
        "password": "User@12345",
        "is_staff": True,
        "is_superuser": False,
        "role_code": "ProjectManager",
        "role_name": "Project Manager",
    },
    {
        "email": "depthead@tms.local",
        "username": "depthead",
        "full_name": "Meera Krishnan",
        "employee_id": "EMP-003",
        "designation": "Engineering Department Head",
        "phone": "+91 9876543212",
        "password": "User@12345",
        "is_staff": False,
        "is_superuser": False,
        "role_code": "DepartmentHead",
        "role_name": "Department Head",
    },
    {
        "email": "teamlead@tms.local",
        "username": "teamlead",
        "full_name": "Rahul Menon",
        "employee_id": "EMP-004",
        "designation": "Platform Team Leader",
        "phone": "+91 9876543213",
        "password": "User@12345",
        "is_staff": False,
        "is_superuser": False,
        "role_code": "TeamLeader",
        "role_name": "Team Leader",
    },
    {
        "email": "employee@tms.local",
        "username": "employee",
        "full_name": "Sneha Varma",
        "employee_id": "EMP-005",
        "designation": "Software Engineer",
        "phone": "+91 9876543214",
        "password": "User@12345",
        "is_staff": False,
        "is_superuser": False,
        "role_code": "Employee",
        "role_name": "Employee",
    },
]


class Command(BaseCommand):
    help = "Seeds default user login accounts and organization membership records into the database."

    @transaction.atomic
    def handle(self, *args, **options):
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(30) NULL;")
                cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_attempts SMALLINT NOT NULL DEFAULT 0;")
                cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ NULL;")
                cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS date_joined TIMESTAMPTZ NOT NULL DEFAULT NOW();")

        created_users: dict[str, User] = {}

        for spec in USERS_TO_SEED:
            user = (
                User.objects.filter(email__iexact=spec["email"]).first()
                or User.objects.filter(username__iexact=spec["username"]).first()
            )
            if user is None:
                user = User(
                    email=spec["email"],
                    username=spec["username"],
                    full_name=spec["full_name"],
                    employee_id=spec["employee_id"],
                    designation=spec["designation"],
                    phone=spec["phone"],
                    is_staff=spec["is_staff"],
                    is_superuser=spec["is_superuser"],
                    is_active=True,
                    account_status=User.AccountStatus.ACTIVE,
                )
            else:
                user.full_name = spec["full_name"]
                if not user.employee_id:
                    user.employee_id = spec["employee_id"]
                user.designation = spec["designation"]
                user.phone = spec["phone"]
                user.is_staff = spec["is_staff"]
                user.is_superuser = spec["is_superuser"]
                user.is_active = True
                user.account_status = User.AccountStatus.ACTIVE
                user.failed_login_attempts = 0
                user.locked_until = None

            user.set_password(spec["password"])
            user.save()
            created_users[spec["username"]] = user
            self.stdout.write(
                self.style.SUCCESS(
                    f"Seeded user: {user.email} | username: {user.username} | password: {spec['password']}"
                )
            )

        # Also seed Company, Roles, Department, Team, and Reporting Hierarchy
        try:
            from apps.companies.models import Company, CompanyMembership
            from apps.organization.models import (
                Department,
                DepartmentMembership,
                ReportingRelationship,
                Team,
                TeamMembership,
            )
            from apps.rbac.models import MembershipRole, Role

            company, _ = Company.objects.get_or_create(
                code="NXC",
                defaults={
                    "name": "Nexcrest Technologies",
                    "status": Company.Status.ACTIVE,
                    "address": "Infopark, Kochi, Kerala",
                },
            )

            dept, _ = Department.objects.get_or_create(
                company=company,
                name="Engineering",
                defaults={"code": "ENG", "status": Department.Status.ACTIVE},
            )

            team, _ = Team.objects.get_or_create(
                department=dept,
                name="Core Platform Team",
                defaults={"code": "CPT", "status": Team.Status.ACTIVE},
            )

            for spec in USERS_TO_SEED:
                u = created_users[spec["username"]]
                role, _ = Role.objects.get_or_create(
                    code=spec["role_code"],
                    defaults={"name": spec["role_name"], "is_system_role": True},
                )
                membership, _ = CompanyMembership.objects.get_or_create(
                    company=company,
                    user=u,
                    defaults={"status": CompanyMembership.Status.ACTIVE},
                )
                MembershipRole.objects.get_or_create(
                    membership=membership,
                    role=role,
                    defaults={"assigned_by": created_users["admin"]},
                )
                DepartmentMembership.objects.get_or_create(
                    department=dept,
                    user=u,
                    defaults={
                        "is_head": spec["username"] == "depthead",
                        "start_date": date.today(),
                        "status": DepartmentMembership.Status.ACTIVE,
                    },
                )
                TeamMembership.objects.get_or_create(
                    team=team,
                    user=u,
                    defaults={
                        "is_leader": spec["username"] == "teamlead",
                        "status": TeamMembership.Status.ACTIVE,
                    },
                )

            # Reporting hierarchy: employee -> teamlead -> depthead -> admin
            chain = [
                ("employee", "teamlead"),
                ("teamlead", "depthead"),
                ("pm", "admin"),
                ("depthead", "admin"),
            ]
            for emp_key, mgr_key in chain:
                ReportingRelationship.objects.get_or_create(
                    company=company,
                    employee=created_users[emp_key],
                    manager=created_users[mgr_key],
                    defaults={"start_date": date.today()},
                )
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"Note on org seeding: {exc}"))

        self.stdout.write(self.style.SUCCESS("All user login data seeded successfully."))
