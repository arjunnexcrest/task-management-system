from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import Project, ProjectMember
from apps.rbac.models import MembershipRole, Role


SYSTEM_ROLES = [
    ("CEO", "CEO", "Chief Executive Officer with full enterprise oversight"),
    ("CompanyInCharge", "Company In-Charge", "Company-level operational head"),
    ("ProjectManager", "Project Manager", "Manages projects, milestones, schedules, and approvals"),
    ("DepartmentHead", "Department Head", "Leads an organizational department and reviews work"),
    ("TeamLeader", "Team Leader", "Leads a delivery team and assigns/reviews tasks"),
    ("Employee", "Employee", "Executes assigned tasks, subtasks, and work logs"),
]

DEFAULT_ACCOUNTS = [
    {
        "role_code": "CEO",
        "email": "admin@tms.local",
        "username": "admin",
        "password": "Admin@12345",
        "employee_id": "EMP-001",
        "full_name": "System Administrator",
        "designation": "Chief Executive Officer",
        "is_staff": True,
        "is_superuser": True,
        "project_role": "Sponsor",
    },
    {
        "role_code": "ProjectManager",
        "email": "pm@tms.local",
        "username": "pm",
        "password": "User@12345",
        "employee_id": "EMP-002",
        "full_name": "Senior Project Manager",
        "designation": "Senior Project Manager",
        "is_staff": True,
        "is_superuser": False,
        "project_role": "Project Manager",
    },
    {
        "role_code": "DepartmentHead",
        "email": "depthead@tms.local",
        "username": "depthead",
        "password": "User@12345",
        "employee_id": "EMP-003",
        "full_name": "Engineering Dept Head",
        "designation": "Engineering Department Head",
        "is_staff": False,
        "is_superuser": False,
        "project_role": "Department Head",
    },
    {
        "role_code": "TeamLeader",
        "email": "teamlead@tms.local",
        "username": "teamlead",
        "password": "User@12345",
        "employee_id": "EMP-004",
        "full_name": "Platform Team Leader",
        "designation": "Platform Team Leader",
        "is_staff": False,
        "is_superuser": False,
        "project_role": "Team Leader",
    },
    {
        "role_code": "Employee",
        "email": "employee@tms.local",
        "username": "employee",
        "password": "User@12345",
        "employee_id": "EMP-005",
        "full_name": "Software Engineer",
        "designation": "Software Engineer",
        "is_staff": False,
        "is_superuser": False,
        "project_role": "Contributor",
    },
]


class Command(BaseCommand):
    help = "Idempotently seed the 5 default TMS login accounts, RBAC roles, and memberships."

    @transaction.atomic
    def handle(self, *args, **options):
        # 1. Seed system RBAC roles
        roles_by_code: dict[str, Role] = {}
        for code, name, description in SYSTEM_ROLES:
            role, _ = Role.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "description": description,
                    "is_system_role": True,
                },
            )
            roles_by_code[code] = role

        # 2. Resolve primary active company (or create default)
        company = (
            Company.objects.filter(status=Company.Status.ACTIVE).order_by("created_at").first()
            or Company.objects.order_by("created_at").first()
        )
        if company is None:
            company = Company.objects.create(
                name="Acme Enterprise Solutions",
                code="ACME-CORP",
                status=Company.Status.ACTIVE,
            )

        # 3. Resolve engineering department & platform/core team
        eng_dept = (
            Department.objects.filter(company=company, code="DEPT-ENG").first()
            or Department.objects.filter(company=company).order_by("created_at").first()
        )
        if eng_dept is None:
            eng_dept = Department.objects.create(
                company=company,
                name="Platform Engineering",
                code="DEPT-ENG",
                status=Department.Status.ACTIVE,
            )

        platform_team = (
            Team.objects.filter(department=eng_dept, code="TEAM-CORE").first()
            or Team.objects.filter(department=eng_dept).order_by("created_at").first()
        )
        if platform_team is None:
            platform_team = Team.objects.create(
                department=eng_dept,
                name="Core Backend & DB Team",
                code="TEAM-CORE",
                status=Team.Status.ACTIVE,
            )

        # 4. Upsert the 5 default login users + memberships + RBAC roles
        seeded_users: dict[str, User] = {}
        for spec in DEFAULT_ACCOUNTS:
            user = (
                User.objects.filter(email__iexact=spec["email"]).first()
                or User.objects.filter(username__iexact=spec["username"]).first()
            )
            created = user is None
            if created:
                user = User(email=spec["email"], username=spec["username"])

            user.email = spec["email"]
            user.username = spec["username"]
            user.full_name = spec["full_name"]
            user.employee_id = spec["employee_id"]
            user.designation = spec["designation"]
            user.account_status = User.AccountStatus.ACTIVE
            user.is_active = True
            user.is_staff = spec["is_staff"]
            user.is_superuser = spec["is_superuser"]
            user.set_password(spec["password"])
            user.save()
            seeded_users[spec["username"]] = user

            membership, _ = CompanyMembership.objects.update_or_create(
                company=company,
                user=user,
                defaults={"status": CompanyMembership.Status.ACTIVE},
            )
            MembershipRole.objects.get_or_create(
                membership=membership,
                role=roles_by_code[spec["role_code"]],
                defaults={"assigned_by": seeded_users.get("admin")},
            )

            action_label = "CREATED" if created else "UPDATED"
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{action_label}] {spec['role_code']:14s} | {user.email:20s} | {user.username:10s} | {user.employee_id} | {user.designation}"
                )
            )

        # 5. Wire DepartmentMembership & TeamMembership for org roles
        today = date.today()
        depthead = seeded_users["depthead"]
        teamlead = seeded_users["teamlead"]
        employee = seeded_users["employee"]

        for usr, is_head in [(depthead, True), (teamlead, False), (employee, False)]:
            dm = DepartmentMembership.objects.filter(
                department=eng_dept, user=usr, status=DepartmentMembership.Status.ACTIVE
            ).first()
            if dm is None:
                DepartmentMembership.objects.create(
                    department=eng_dept,
                    user=usr,
                    is_head=is_head,
                    start_date=today,
                    status=DepartmentMembership.Status.ACTIVE,
                )
            elif dm.is_head != is_head:
                dm.is_head = is_head
                dm.save(update_fields=["is_head"])

        for usr, is_leader in [(teamlead, True), (employee, False)]:
            tm = TeamMembership.objects.filter(
                team=platform_team, user=usr, status=TeamMembership.Status.ACTIVE
            ).first()
            if tm is None:
                TeamMembership.objects.create(
                    team=platform_team,
                    user=usr,
                    is_leader=is_leader,
                    status=TeamMembership.Status.ACTIVE,
                )
            elif tm.is_leader != is_leader:
                tm.is_leader = is_leader
                tm.save(update_fields=["is_leader"])

        # 6. Also assign RBAC roles to any existing demo memberships missing a role
        fallback_map = {
            "ceo@example.com": "CEO",
            "pm@example.com": "ProjectManager",
            "lead@example.com": "TeamLeader",
            "dev@example.com": "Employee",
        }
        for email, role_code in fallback_map.items():
            cm = CompanyMembership.objects.filter(
                user__email__iexact=email, status=CompanyMembership.Status.ACTIVE
            ).first()
            if cm and not cm.membership_roles.exists():
                MembershipRole.objects.get_or_create(
                    membership=cm,
                    role=roles_by_code[role_code],
                    defaults={"assigned_by": seeded_users.get("admin")},
                )

        # 7. Enroll the 5 default accounts on all existing company projects so every account can browse & test
        active_projects = list(Project.objects.filter(company=company))
        for proj in active_projects:
            for spec in DEFAULT_ACCOUNTS:
                usr = seeded_users[spec["username"]]
                ProjectMember.objects.get_or_create(
                    project=proj,
                    user=usr,
                    defaults={
                        "role_in_project": spec["project_role"],
                        "status": ProjectMember.Status.ACTIVE,
                    },
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: seeded 5 login accounts, {len(roles_by_code)} roles, company='{company.name}', enrolled in {len(active_projects)} project(s)."
            )
        )
