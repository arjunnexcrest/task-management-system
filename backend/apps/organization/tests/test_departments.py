"""
organization/tests/test_departments.py

Tests for Department CRUD and Department-Head assignment (/api/departments/ and
/api/organization/departments/):
- Unauthenticated requests get 401
- CEO and CompanyInCharge can create, list, search, update, soft-deactivate, and reactivate departments
- Regular Employee / DepartmentHead gets 403 on create/update/delete/assign-head
- Non-member cannot see or mutate another company's departments (403 / company isolation)
- Case-insensitive duplicate (company, name) is rejected (400)
- Department-Head assignment works on create, PATCH, POST /assign_head/, and POST /assign-head/
- Reassigning Department Head demotes previous head (is_head=False) while preserving membership
- Unassigning Department Head with null succeeds cleanly without UUID errors
- Cross-company or inactive user rejected as Department Head (400)
- Assigning head to inactive department rejected (400)
- DELETE soft-deactivates (status='Inactive') without deleting the row
- Transferring an employee preserves previous DepartmentMembership history (status='Transferred')
"""
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.organization.models import Department, DepartmentMembership
from apps.rbac.roles import SystemRole

DEPARTMENTS_URL = "/api/departments/"
ORG_DEPARTMENTS_URL = "/api/organization/departments/"


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
def head_a1(db, company_a):
    user = User.objects.create_user(
        email="head1@alpha.com",
        username="head1_alpha",
        full_name="Alice Head",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.DEPARTMENT_HEAD,
        is_active=True,
    )
    return user


@pytest.fixture
def head_a2(db, company_a):
    user = User.objects.create_user(
        email="head2@alpha.com",
        username="head2_alpha",
        full_name="Charlie Lead",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.DEPARTMENT_HEAD,
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
def dept_a(db, company_a):
    return Department.objects.create(
        company=company_a,
        name="Engineering",
        code="ENG",
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


def auth_client(user: User) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class TestDepartmentCRUDAndPermissions:
    def test_unauthenticated_gets_401(self, db):
        c = APIClient()
        assert c.get(DEPARTMENTS_URL).status_code == 401
        assert c.get(ORG_DEPARTMENTS_URL).status_code == 401

    def test_ceo_full_crud_and_soft_deactivate(self, db, ceo_user, company_a, head_a1):
        c = auth_client(ceo_user)

        # Create with initial Department Head
        r_create = c.post(
            DEPARTMENTS_URL,
            {
                "company": str(company_a.id),
                "name": "Cloud Infrastructure",
                "code": "CLOUD",
                "department_head": str(head_a1.id),
            },
            format="json",
        )
        assert r_create.status_code == 201
        dept_id = r_create.data["id"]
        assert r_create.data["name"] == "Cloud Infrastructure"
        assert r_create.data["is_active"] is True
        assert r_create.data["department_head_id"] == str(head_a1.id)
        assert r_create.data["department_head_name"] == head_a1.full_name
        assert r_create.data["department_head_email"] == head_a1.email

        # Search
        r_search = c.get(f"{DEPARTMENTS_URL}?search=Cloud")
        assert r_search.status_code == 200
        results = r_search.data["results"] if isinstance(r_search.data, dict) else r_search.data
        assert any(d["id"] == dept_id for d in results)

        # Update name & code
        r_patch = c.patch(
            f"{DEPARTMENTS_URL}{dept_id}/",
            {"name": "Cloud Platform", "code": "CPLT"},
            format="json",
        )
        assert r_patch.status_code == 200
        assert r_patch.data["name"] == "Cloud Platform"
        assert r_patch.data["code"] == "CPLT"

        # Soft delete (DELETE sets status=Inactive, row remains in DB)
        r_del = c.delete(f"{DEPARTMENTS_URL}{dept_id}/")
        assert r_del.status_code == 204
        dept = Department.objects.get(pk=dept_id)
        assert dept.status == Department.Status.INACTIVE
        assert dept.is_active is False

        # Reactivate action
        r_act = c.post(f"{DEPARTMENTS_URL}{dept_id}/activate/")
        assert r_act.status_code == 200
        dept.refresh_from_db()
        assert dept.status == Department.Status.ACTIVE

        # Deactivate action
        r_deact = c.post(f"{DEPARTMENTS_URL}{dept_id}/deactivate/")
        assert r_deact.status_code == 200
        dept.refresh_from_db()
        assert dept.status == Department.Status.INACTIVE

    def test_company_incharge_can_manage_own_company_only(
        self, db, incharge_a, company_a, company_b, dept_b
    ):
        c = auth_client(incharge_a)

        # Can create in own company
        r_ok = c.post(
            DEPARTMENTS_URL,
            {"company": str(company_a.id), "name": "Operations", "code": "OPS"},
            format="json",
        )
        assert r_ok.status_code == 201

        # Cannot create in another company -> 403
        r_other = c.post(
            DEPARTMENTS_URL,
            {"company": str(company_b.id), "name": "Shadow Ops", "code": "SHD"},
            format="json",
        )
        assert r_other.status_code == 403

        # Cannot access another company's departments via filter -> 403
        assert c.get(f"{DEPARTMENTS_URL}?company={company_b.id}").status_code == 403
        # Cannot retrieve another company's department (not in scoped queryset)
        assert c.get(f"{DEPARTMENTS_URL}{dept_b.id}/").status_code in (403, 404)

    def test_employee_can_view_own_company_only_and_cannot_mutate(
        self, db, emp_a, company_a, company_b, dept_a, dept_b, head_a1
    ):
        c = auth_client(emp_a)

        # Can list own company's departments (sees dept_a, NOT dept_b)
        r_list = c.get(DEPARTMENTS_URL)
        assert r_list.status_code == 200
        results = r_list.data["results"] if isinstance(r_list.data, dict) else r_list.data
        ids = {d["id"] for d in results}
        assert str(dept_a.id) in ids
        assert str(dept_b.id) not in ids

        # Cannot query another company -> 403
        assert c.get(f"{DEPARTMENTS_URL}?company={company_b.id}").status_code == 403

        # Cannot create, update, delete, or assign head -> 403
        assert (
            c.post(
                DEPARTMENTS_URL,
                {"company": str(company_a.id), "name": "QA"},
                format="json",
            ).status_code
            == 403
        )
        assert (
            c.patch(
                f"{DEPARTMENTS_URL}{dept_a.id}/",
                {"name": "Hacked"},
                format="json",
            ).status_code
            == 403
        )
        assert c.delete(f"{DEPARTMENTS_URL}{dept_a.id}/").status_code == 403
        assert (
            c.post(
                f"{DEPARTMENTS_URL}{dept_a.id}/assign_head/",
                {"department_head": str(head_a1.id)},
                format="json",
            ).status_code
            == 403
        )

    def test_duplicate_department_name_case_insensitive_rejected(
        self, db, ceo_user, company_a, dept_a
    ):
        c = auth_client(ceo_user)
        r = c.post(
            DEPARTMENTS_URL,
            {"company": str(company_a.id), "name": "engineering"},
            format="json",
        )
        assert r.status_code == 400
        assert "name" in r.data


class TestDepartmentHeadAssignmentAndTransfer:
    def test_assign_reassign_and_unassign_department_head(
        self, db, ceo_user, dept_a, head_a1, head_a2
    ):
        c = auth_client(ceo_user)

        # Assign head_a1 via /assign_head/
        r1 = c.post(
            f"{DEPARTMENTS_URL}{dept_a.id}/assign_head/",
            {"department_head": str(head_a1.id)},
            format="json",
        )
        assert r1.status_code == 200
        assert r1.data["department_head_id"] == str(head_a1.id)
        assert r1.data["department_head_name"] == head_a1.full_name

        # Reassign to head_a2 via hyphenated /assign-head/ with {"user": ...}
        r2 = c.post(
            f"{ORG_DEPARTMENTS_URL}{dept_a.id}/assign-head/",
            {"user": str(head_a2.id)},
            format="json",
        )
        assert r2.status_code == 200
        assert r2.data["department_head_id"] == str(head_a2.id)

        # Previous head's membership is preserved with is_head=False
        m1 = DepartmentMembership.objects.get(
            department=dept_a, user=head_a1, status=DepartmentMembership.Status.ACTIVE
        )
        m2 = DepartmentMembership.objects.get(
            department=dept_a, user=head_a2, status=DepartmentMembership.Status.ACTIVE
        )
        assert m1.is_head is False
        assert m2.is_head is True

        # Unassign Department Head with null (must not raise UUID validation error)
        r3 = c.post(
            f"{DEPARTMENTS_URL}{dept_a.id}/assign_head/",
            {"department_head": None},
            format="json",
        )
        assert r3.status_code == 200
        assert r3.data["department_head_id"] is None
        m2.refresh_from_db()
        assert m2.is_head is False

    def test_cross_company_and_inactive_department_head_rejected(
        self, db, ceo_user, dept_a, emp_b, head_a1
    ):
        c = auth_client(ceo_user)

        # User from Company B cannot be assigned head of Company A department
        r_cross = c.post(
            f"{DEPARTMENTS_URL}{dept_a.id}/assign_head/",
            {"department_head": str(emp_b.id)},
            format="json",
        )
        assert r_cross.status_code == 400
        assert "department_head" in r_cross.data

        # Deactivate department, then try assigning head -> rejected (400)
        dept_a.deactivate()
        r_inactive = c.post(
            f"{DEPARTMENTS_URL}{dept_a.id}/assign_head/",
            {"department_head": str(head_a1.id)},
            format="json",
        )
        assert r_inactive.status_code == 400
        assert "department_head" in r_inactive.data

    def test_transfer_employee_preserves_history_and_validates_company(
        self, db, ceo_user, company_a, dept_a, emp_a, emp_b
    ):
        dept_a2 = Department.objects.create(
            company=company_a,
            name="Product Design",
            code="DES",
            status=Department.Status.ACTIVE,
        )
        c = auth_client(ceo_user)

        # Place emp_a in dept_a first
        r1 = c.post(
            f"{DEPARTMENTS_URL}{dept_a.id}/transfer_employee/",
            {"user": str(emp_a.id), "is_head": True},
            format="json",
        )
        assert r1.status_code == 200
        assert r1.data["is_head"] is True

        # Transfer emp_a into dept_a2
        r2 = c.post(
            f"{DEPARTMENTS_URL}{dept_a2.id}/transfer_employee/",
            {"user": str(emp_a.id), "is_head": False},
            format="json",
        )
        assert r2.status_code == 200

        # Old membership in dept_a is marked Transferred with end_date set and is_head=False
        old_m = DepartmentMembership.objects.get(department=dept_a, user=emp_a)
        assert old_m.status == DepartmentMembership.Status.TRANSFERRED
        assert old_m.end_date is not None
        assert old_m.is_head is False

        # Cross-company transfer rejected
        r_cross = c.post(
            f"{DEPARTMENTS_URL}{dept_a2.id}/transfer_employee/",
            {"user": str(emp_b.id)},
            format="json",
        )
        assert r_cross.status_code == 400
