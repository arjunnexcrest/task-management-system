"""
organization/tests/test_teams.py

Tests for Team CRUD, Team-Leader assignment, and Team member management
(/api/teams/ and /api/organization/teams/):
- Unauthenticated requests get 401
- CEO can create, search, update, soft-deactivate, and reactivate teams
- CompanyInCharge manages teams in own company only (403 on other company)
- DepartmentHead manages teams in their own department (403 on other department / company)
- Employee can view own company's teams only and gets 403 on mutations
- Case-insensitive duplicate (department, name) is rejected (400)
- Creating a team under an inactive department is rejected (400)
- Assign, reassign (demotes previous leader while keeping membership), and unassign (null)
  Team Leader via /assign_leader/ and /assign-leader/
- Cross-company user and inactive team rejected on leader assignment (400)
- Add member and remove member (marks status='Left' with left_at, preserving history)
"""
from datetime import date
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.rbac.roles import SystemRole

TEAMS_URL = "/api/teams/"
ORG_TEAMS_URL = "/api/organization/teams/"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def company_a(db):
    return Company.objects.create(
        name="Alpha Corp",
        code="ALPHA",
        email="info@alpha.com",
        is_active=True,
    )


@pytest.fixture
def company_b(db):
    return Company.objects.create(
        name="Beta Ltd",
        code="BETA",
        email="contact@beta.com",
        is_active=True,
    )


@pytest.fixture
def dept_a1(db, company_a):
    return Department.objects.create(
        company=company_a,
        name="Engineering",
        code="ENG",
        status=Department.Status.ACTIVE,
    )


@pytest.fixture
def dept_a2(db, company_a):
    return Department.objects.create(
        company=company_a,
        name="Design",
        code="DES",
        status=Department.Status.ACTIVE,
    )


@pytest.fixture
def dept_b(db, company_b):
    return Department.objects.create(
        company=company_b,
        name="Finance",
        code="FIN",
        status=Department.Status.ACTIVE,
    )


@pytest.fixture
def ceo_user(db, company_a):
    user = User.objects.create_user(
        email="ceo@alpha.com",
        username="ceo_alpha",
        full_name="CEO Alpha",
        password="CeoPassword123!",
        is_staff=True,
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.CEO,
        is_active=True,
    )
    return user


@pytest.fixture
def incharge_a(db, company_a):
    user = User.objects.create_user(
        email="incharge@alpha.com",
        username="incharge_alpha",
        full_name="InCharge Alpha",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.COMPANY_IN_CHARGE,
        is_active=True,
    )
    return user


@pytest.fixture
def dept_head_a1(db, company_a, dept_a1):
    user = User.objects.create_user(
        email="head@alpha.com",
        username="head_alpha",
        full_name="Hannah Head",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.DEPARTMENT_HEAD,
        is_active=True,
    )
    DepartmentMembership.objects.create(
        department=dept_a1,
        user=user,
        is_head=True,
        start_date=date.today(),
        status=DepartmentMembership.Status.ACTIVE,
    )
    return user


@pytest.fixture
def leader_a1(db, company_a):
    user = User.objects.create_user(
        email="lead1@alpha.com",
        username="lead1_alpha",
        full_name="Liam Leader",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.TEAM_LEADER,
        is_active=True,
    )
    return user


@pytest.fixture
def leader_a2(db, company_a):
    user = User.objects.create_user(
        email="lead2@alpha.com",
        username="lead2_alpha",
        full_name="Nora Lead",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.TEAM_LEADER,
        is_active=True,
    )
    return user


@pytest.fixture
def emp_a(db, company_a):
    user = User.objects.create_user(
        email="emp@alpha.com",
        username="emp_alpha",
        full_name="Eve Employee",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.EMPLOYEE,
        is_active=True,
    )
    return user


@pytest.fixture
def emp_b(db, company_b):
    user = User.objects.create_user(
        email="bob@beta.com",
        username="bob_beta",
        full_name="Bob Beta",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_b,
        role=SystemRole.EMPLOYEE,
        is_active=True,
    )
    return user


@pytest.fixture
def team_a1(db, dept_a1):
    return Team.objects.create(
        department=dept_a1,
        name="Backend API",
        code="BE-API",
        status=Team.Status.ACTIVE,
    )


@pytest.fixture
def team_b(db, dept_b):
    return Team.objects.create(
        department=dept_b,
        name="Treasury",
        code="TRS",
        status=Team.Status.ACTIVE,
    )


def auth_client(user: User) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class TestTeamCRUDAndPermissions:
    def test_unauthenticated_gets_401(self, db):
        c = APIClient()
        assert c.get(TEAMS_URL).status_code == 401
        assert c.get(ORG_TEAMS_URL).status_code == 401

    def test_ceo_full_crud_and_soft_deactivate(self, db, ceo_user, dept_a1, leader_a1):
        c = auth_client(ceo_user)

        # Create team with initial Team Leader
        r_create = c.post(
            TEAMS_URL,
            {
                "department": str(dept_a1.id),
                "name": "Platform Core",
                "code": "CORE",
                "team_leader": str(leader_a1.id),
            },
            format="json",
        )
        assert r_create.status_code == 201
        team_id = r_create.data["id"]
        assert r_create.data["name"] == "Platform Core"
        assert r_create.data["is_active"] is True
        assert r_create.data["team_leader_id"] == str(leader_a1.id)
        assert r_create.data["team_leader_name"] == leader_a1.full_name
        assert r_create.data["team_leader_email"] == leader_a1.email

        # Search
        r_search = c.get(f"{TEAMS_URL}?search=Platform")
        assert r_search.status_code == 200
        results = r_search.data["results"] if isinstance(r_search.data, dict) else r_search.data
        assert any(t["id"] == team_id for t in results)

        # Update name & code
        r_patch = c.patch(
            f"{TEAMS_URL}{team_id}/",
            {"name": "Platform Services", "code": "PSVC"},
            format="json",
        )
        assert r_patch.status_code == 200
        assert r_patch.data["name"] == "Platform Services"
        assert r_patch.data["code"] == "PSVC"

        # DELETE performs soft-deactivation (row remains)
        r_del = c.delete(f"{TEAMS_URL}{team_id}/")
        assert r_del.status_code == 204
        team = Team.objects.get(pk=team_id)
        assert team.status == Team.Status.INACTIVE
        assert team.is_active is False

        # Activate / Deactivate actions
        r_act = c.post(f"{TEAMS_URL}{team_id}/activate/")
        assert r_act.status_code == 200
        team.refresh_from_db()
        assert team.status == Team.Status.ACTIVE

        r_deact = c.post(f"{TEAMS_URL}{team_id}/deactivate/")
        assert r_deact.status_code == 200
        team.refresh_from_db()
        assert team.status == Team.Status.INACTIVE

    def test_department_head_manages_own_department_only(
        self, db, dept_head_a1, dept_a1, dept_a2, dept_b, leader_a1
    ):
        c = auth_client(dept_head_a1)

        # Can create team under dept_a1 (which they head)
        r_ok = c.post(
            TEAMS_URL,
            {
                "department": str(dept_a1.id),
                "name": "DevOps",
                "code": "OPS",
                "team_leader": str(leader_a1.id),
            },
            format="json",
        )
        assert r_ok.status_code == 201

        # Cannot create team under dept_a2 (another department they do NOT head) -> 403
        r_other_dept = c.post(
            TEAMS_URL,
            {"department": str(dept_a2.id), "name": "Brand UI"},
            format="json",
        )
        assert r_other_dept.status_code == 403

        # Cannot create team in another company -> 403
        r_other_co = c.post(
            TEAMS_URL,
            {"department": str(dept_b.id), "name": "Shadow Finance"},
            format="json",
        )
        assert r_other_co.status_code == 403

    def test_company_incharge_manages_own_company_only(
        self, db, incharge_a, dept_a2, dept_b, company_b, team_b
    ):
        c = auth_client(incharge_a)

        # Can create in own company's departments
        r_ok = c.post(
            TEAMS_URL,
            {"department": str(dept_a2.id), "name": "UX Research"},
            format="json",
        )
        assert r_ok.status_code == 201

        # Cannot create in another company -> 403
        assert (
            c.post(
                TEAMS_URL,
                {"department": str(dept_b.id), "name": "Audit"},
                format="json",
            ).status_code
            == 403
        )
        assert c.get(f"{TEAMS_URL}?company={company_b.id}").status_code == 403
        assert c.get(f"{TEAMS_URL}{team_b.id}/").status_code in (403, 404)

    def test_employee_can_view_own_company_only_and_cannot_mutate(
        self, db, emp_a, dept_a1, company_b, team_a1, team_b, leader_a1
    ):
        c = auth_client(emp_a)

        r_list = c.get(TEAMS_URL)
        assert r_list.status_code == 200
        results = r_list.data["results"] if isinstance(r_list.data, dict) else r_list.data
        ids = {t["id"] for t in results}
        assert str(team_a1.id) in ids
        assert str(team_b.id) not in ids

        assert c.get(f"{TEAMS_URL}?company={company_b.id}").status_code == 403
        assert (
            c.post(
                TEAMS_URL,
                {"department": str(dept_a1.id), "name": "QA"},
                format="json",
            ).status_code
            == 403
        )
        assert (
            c.patch(
                f"{TEAMS_URL}{team_a1.id}/",
                {"name": "Hacked"},
                format="json",
            ).status_code
            == 403
        )
        assert c.delete(f"{TEAMS_URL}{team_a1.id}/").status_code == 403
        assert (
            c.post(
                f"{TEAMS_URL}{team_a1.id}/assign_leader/",
                {"team_leader": str(leader_a1.id)},
                format="json",
            ).status_code
            == 403
        )

    def test_duplicate_name_and_inactive_department_rejected(
        self, db, ceo_user, dept_a1, team_a1
    ):
        c = auth_client(ceo_user)

        # Case-insensitive duplicate in same department
        r_dup = c.post(
            TEAMS_URL,
            {"department": str(dept_a1.id), "name": "backend api"},
            format="json",
        )
        assert r_dup.status_code == 400
        assert "name" in r_dup.data

        # Creating a team under an inactive department is rejected
        dept_a1.deactivate()
        r_inactive_dept = c.post(
            TEAMS_URL,
            {"department": str(dept_a1.id), "name": "New Team"},
            format="json",
        )
        assert r_inactive_dept.status_code == 400
        assert "department" in r_inactive_dept.data


class TestTeamLeaderAssignmentAndMembership:
    def test_assign_reassign_and_unassign_team_leader(
        self, db, ceo_user, team_a1, leader_a1, leader_a2
    ):
        c = auth_client(ceo_user)

        # Assign leader_a1 via /assign_leader/
        r1 = c.post(
            f"{TEAMS_URL}{team_a1.id}/assign_leader/",
            {"team_leader": str(leader_a1.id)},
            format="json",
        )
        assert r1.status_code == 200
        assert r1.data["team_leader_id"] == str(leader_a1.id)
        assert r1.data["team_leader_name"] == leader_a1.full_name

        # Reassign to leader_a2 via hyphenated /assign-leader/ with {"user": ...}
        r2 = c.post(
            f"{ORG_TEAMS_URL}{team_a1.id}/assign-leader/",
            {"user": str(leader_a2.id)},
            format="json",
        )
        assert r2.status_code == 200
        assert r2.data["team_leader_id"] == str(leader_a2.id)

        # Previous leader's active membership is preserved with is_leader=False
        m1 = TeamMembership.objects.get(
            team=team_a1, user=leader_a1, status=TeamMembership.Status.ACTIVE
        )
        m2 = TeamMembership.objects.get(
            team=team_a1, user=leader_a2, status=TeamMembership.Status.ACTIVE
        )
        assert m1.is_leader is False
        assert m2.is_leader is True

        # Unassign with null (no UUID error)
        r3 = c.post(
            f"{TEAMS_URL}{team_a1.id}/assign_leader/",
            {"team_leader": None},
            format="json",
        )
        assert r3.status_code == 200
        assert r3.data["team_leader_id"] is None
        m2.refresh_from_db()
        assert m2.is_leader is False

    def test_cross_company_and_inactive_team_leader_rejected(
        self, db, ceo_user, team_a1, emp_b, leader_a1
    ):
        c = auth_client(ceo_user)

        # User from Company B cannot lead a Company A team
        r_cross = c.post(
            f"{TEAMS_URL}{team_a1.id}/assign_leader/",
            {"team_leader": str(emp_b.id)},
            format="json",
        )
        assert r_cross.status_code == 400
        assert "team_leader" in r_cross.data

        # Deactivate team -> assigning leader rejected
        team_a1.deactivate()
        r_inactive = c.post(
            f"{TEAMS_URL}{team_a1.id}/assign_leader/",
            {"team_leader": str(leader_a1.id)},
            format="json",
        )
        assert r_inactive.status_code == 400
        assert "team_leader" in r_inactive.data

    def test_add_and_remove_team_member_preserves_history(
        self, db, ceo_user, team_a1, emp_a, emp_b
    ):
        c = auth_client(ceo_user)

        # Add emp_a as member + leader
        r_add = c.post(
            f"{TEAMS_URL}{team_a1.id}/add_member/",
            {"user": str(emp_a.id), "is_leader": True},
            format="json",
        )
        assert r_add.status_code == 201
        assert r_add.data["is_leader"] is True
        assert r_add.data["status"] == TeamMembership.Status.ACTIVE

        # Cross-company member add rejected
        r_cross = c.post(
            f"{TEAMS_URL}{team_a1.id}/add_member/",
            {"user": str(emp_b.id)},
            format="json",
        )
        assert r_cross.status_code == 400

        # Remove emp_a -> marks Left with left_at timestamp and is_leader=False
        r_rem = c.post(
            f"{TEAMS_URL}{team_a1.id}/remove_member/",
            {"user": str(emp_a.id)},
            format="json",
        )
        assert r_rem.status_code == 200
        m = TeamMembership.objects.get(team=team_a1, user=emp_a)
        assert m.status == TeamMembership.Status.LEFT
        assert m.left_at is not None
        assert m.is_leader is False
