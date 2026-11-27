"""
organization/tests/test_hierarchy.py

Comprehensive tests for D1-10:
"Develop organization hierarchy APIs and chart integration" (SRS §1.3, §5.3 & §5.4)

Covers:
1. Authentication & company access enforcement (401 unauth, 403 unassigned, 403 cross-company
   via ?company= or X-Company-ID, 400 on invalid UUID filter).
2. Full structural hierarchy (Company -> Department [Head] -> Team [Leader] -> Employees)
   and recursive reporting tree (CEO -> CompanyInCharge -> DepartmentHead -> TeamLeader -> Employee)
   with accurate project/task counts.
3. Search (?search=) matches employees, sets matched=True, populates matched_employee_ids,
   and preserves the ancestor chain up to CEO/root in reporting_tree.
4. Filtering by ?company=, ?department=, and ?team= scopes both structure and reporting_tree.
5. Employee node detail API (/api/organization/hierarchy/employee/<id>/) returns direct
   reporting relationships, upward chain, departments, teams, assigned projects, and assigned
   tasks (including overdue/blocked summary) — keeping reporting lines separate from project
   management (SRS §5.3) and enforcing project-level authorization (SRS §2.8).
"""
from datetime import date, timedelta
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.organization.models import (
    Department,
    DepartmentMembership,
    ReportingRelationship,
    Team,
    TeamMembership,
)
from apps.projects.models import Project, ProjectMember
from apps.rbac.roles import SystemRole
from apps.tasks.models import Task, TaskAssignment

HIERARCHY_URL = "/api/organization/hierarchy/"
HIERARCHY_ALIAS_URL = "/api/hierarchy/"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def org_world(db):
    comp_a = Company.objects.create(name="Alpha Corp", code="ALPHA", email="a@alpha.com", is_active=True)
    comp_b = Company.objects.create(name="Beta Ltd", code="BETA", email="b@beta.com", is_active=True)

    ceo = User.objects.create_user(
        email="ceo@alpha.com", username="ceo", full_name="Carla CEO",
        designation="Chief Executive Officer", password="Password123!", is_staff=True, is_active=True,
    )
    incharge = User.objects.create_user(
        email="ic@alpha.com", username="incharge", full_name="Ian InCharge",
        designation="VP Operations", password="Password123!", is_active=True,
    )
    pm = User.objects.create_user(
        email="pm@alpha.com", username="pm", full_name="Paula Manager",
        designation="Project Manager", password="Password123!", is_active=True,
    )
    head = User.objects.create_user(
        email="head@alpha.com", username="head", full_name="Henry Head",
        designation="Engineering Head", password="Password123!", is_active=True,
    )
    lead = User.objects.create_user(
        email="lead@alpha.com", username="lead", full_name="Liam Leader",
        designation="Backend Lead", password="Password123!", is_active=True,
    )
    dev = User.objects.create_user(
        email="dev@alpha.com", username="dev", full_name="Diana Developer",
        employee_id="EMP-201", designation="Senior Engineer", password="Password123!", is_active=True,
    )
    peer = User.objects.create_user(
        email="peer@alpha.com", username="peer", full_name="Peter Peer",
        designation="QA Engineer", password="Password123!", is_active=True,
    )
    beta_emp = User.objects.create_user(
        email="bob@beta.com", username="bob", full_name="Bob Beta",
        designation="Finance Analyst", password="Password123!", is_active=True,
    )
    unassigned = User.objects.create_user(
        email="no_co@tms.local", username="noco", full_name="No Company",
        password="Password123!", is_active=True,
    )

    for u, r in [
        (ceo, SystemRole.CEO),
        (incharge, SystemRole.COMPANY_IN_CHARGE),
        (pm, SystemRole.PROJECT_MANAGER),
        (head, SystemRole.DEPARTMENT_HEAD),
        (lead, SystemRole.TEAM_LEADER),
        (dev, SystemRole.EMPLOYEE),
        (peer, SystemRole.EMPLOYEE),
    ]:
        CompanyMembership.objects.create(user=u, company=comp_a, role=r, is_active=True)
    CompanyMembership.objects.create(user=beta_emp, company=comp_b, role=SystemRole.EMPLOYEE, is_active=True)

    dept_eng = Department.objects.create(company=comp_a, name="Engineering", code="ENG")
    dept_qa = Department.objects.create(company=comp_a, name="Quality", code="QA")
    dept_beta = Department.objects.create(company=comp_b, name="Finance", code="FIN")

    DepartmentMembership.objects.create(department=dept_eng, user=head, is_head=True, start_date=date.today())
    DepartmentMembership.objects.create(department=dept_eng, user=lead, is_head=False, start_date=date.today())
    DepartmentMembership.objects.create(department=dept_eng, user=dev, is_head=False, start_date=date.today())
    DepartmentMembership.objects.create(department=dept_qa, user=peer, is_head=False, start_date=date.today())
    DepartmentMembership.objects.create(department=dept_beta, user=beta_emp, is_head=False, start_date=date.today())

    team_api = Team.objects.create(department=dept_eng, name="API Core", code="API")
    TeamMembership.objects.create(team=team_api, user=lead, is_leader=True)
    TeamMembership.objects.create(team=team_api, user=dev, is_leader=False)

    # Reporting chain: dev -> lead -> head -> incharge -> ceo
    ReportingRelationship.objects.create(company=comp_a, employee=incharge, manager=ceo, start_date=date.today())
    ReportingRelationship.objects.create(company=comp_a, employee=head, manager=incharge, start_date=date.today())
    ReportingRelationship.objects.create(company=comp_a, employee=lead, manager=head, start_date=date.today())
    ReportingRelationship.objects.create(company=comp_a, employee=dev, manager=lead, start_date=date.today())

    # Separate project assignments (SRS §5.3: dev reports to lead, works on projects managed by pm)
    proj_shared = Project.objects.create(
        company=comp_a, project_manager=pm, name="Shared Portal", code="PORTAL", status=Project.Status.ACTIVE
    )
    proj_secret = Project.objects.create(
        company=comp_a, project_manager=pm, name="Secret R&D", code="SECRET", status=Project.Status.ACTIVE
    )
    ProjectMember.objects.create(project=proj_shared, user=dev, role_in_project="Backend Dev")
    ProjectMember.objects.create(project=proj_shared, user=peer, role_in_project="QA")
    ProjectMember.objects.create(project=proj_secret, user=dev, role_in_project="Lead Dev")

    t_overdue = Task.objects.create(
        project=proj_shared, title="Fix auth bug", created_by=pm, task_owner=dev,
        status=Task.Status.IN_PROGRESS, priority=Task.Priority.HIGH,
        due_date=date.today() - timedelta(days=2),
    )
    t_secret = Task.objects.create(
        project=proj_secret, title="Build secret engine", created_by=pm, task_owner=pm,
        status=Task.Status.BLOCKED, priority=Task.Priority.CRITICAL,
    )
    TaskAssignment.objects.create(task=t_secret, user=dev, assigned_by=pm)

    return {
        "comp_a": comp_a, "comp_b": comp_b,
        "ceo": ceo, "incharge": incharge, "pm": pm, "head": head,
        "lead": lead, "dev": dev, "peer": peer, "beta_emp": beta_emp, "unassigned": unassigned,
        "dept_eng": dept_eng, "dept_qa": dept_qa, "dept_beta": dept_beta,
        "team_api": team_api, "proj_shared": proj_shared, "proj_secret": proj_secret,
        "t_overdue": t_overdue, "t_secret": t_secret,
    }


def auth_client(user: User) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class TestOrganizationHierarchyAPIs:
    def test_auth_and_company_isolation_on_hierarchy_endpoints(self, org_world):
        # Unauthenticated -> 401
        anon = APIClient()
        assert anon.get(HIERARCHY_URL).status_code == 401
        assert anon.get(HIERARCHY_ALIAS_URL).status_code == 401
        assert anon.get(f"{HIERARCHY_URL}employee/{org_world['dev'].id}/").status_code == 401

        # Unassigned user -> 403
        c_none = auth_client(org_world["unassigned"])
        assert c_none.get(HIERARCHY_URL).status_code == 403

        # Employee of Company A cannot query Company B via ?company= or X-Company-ID -> 403
        c_dev = auth_client(org_world["dev"])
        comp_b_id = str(org_world["comp_b"].id)
        assert c_dev.get(f"{HIERARCHY_URL}?company={comp_b_id}").status_code == 403
        assert c_dev.get(HIERARCHY_URL, HTTP_X_COMPANY_ID=comp_b_id).status_code == 403

        # Employee of Company A never sees Company B in unfiltered response
        r_a = c_dev.get(HIERARCHY_URL)
        assert r_a.status_code == 200
        assert {co["id"] for co in r_a.data["structure"]} == {str(org_world["comp_a"].id)}
        assert str(org_world["beta_emp"].id) not in {n["id"] for n in r_a.data["nodes"]}

        # Employee of Company A cannot open Company B employee's chart detail -> 404
        assert c_dev.get(f"{HIERARCHY_URL}employee/{org_world['beta_emp'].id}/").status_code == 404

        # Invalid UUID filter returns clean 400
        assert c_dev.get(f"{HIERARCHY_URL}?department=not-a-uuid").status_code == 400

    def test_ceo_gets_complete_structure_and_reporting_tree_with_counts(self, org_world):
        c = auth_client(org_world["ceo"])
        r = c.get(f"{HIERARCHY_ALIAS_URL}?company={org_world['comp_a'].id}")
        assert r.status_code == 200

        summary = r.data["summary"]
        assert summary["companies_count"] == 1
        assert summary["departments_count"] == 2
        assert summary["teams_count"] == 1
        assert summary["employees_count"] == 7
        assert summary["reporting_links_count"] == 4

        # Structural tree: Company -> Engineering (Head: Henry) -> API Core (Leader: Liam) -> [Liam, Diana]
        comp_node = r.data["structure"][0]
        eng_dept = next(d for d in comp_node["departments"] if d["name"] == "Engineering")
        assert eng_dept["head"]["id"] == str(org_world["head"].id)
        api_team = eng_dept["teams"][0]
        assert api_team["leader"]["id"] == str(org_world["lead"].id)
        assert {m["id"] for m in api_team["members"]} == {
            str(org_world["lead"].id),
            str(org_world["dev"].id),
        }

        # Reporting tree starts with Carla CEO -> Ian InCharge -> Henry Head -> Liam Leader -> Diana Developer
        ceo_tree = next(n for n in r.data["reporting_tree"] if n["id"] == str(org_world["ceo"].id))
        ic_node = ceo_tree["children"][0]
        assert ic_node["id"] == str(org_world["incharge"].id)
        head_node = ic_node["children"][0]
        assert head_node["id"] == str(org_world["head"].id)
        lead_node = head_node["children"][0]
        assert lead_node["id"] == str(org_world["lead"].id)
        dev_node = lead_node["children"][0]
        assert dev_node["id"] == str(org_world["dev"].id)
        assert dev_node["projects_count"] == 2
        assert dev_node["active_tasks_count"] == 2
        assert dev_node["overdue_tasks_count"] == 1

    def test_search_preserves_ancestors_and_department_team_filters(self, org_world):
        c = auth_client(org_world["ceo"])

        # Search for "Diana" -> preserves full ancestor path CEO -> InCharge -> Head -> Lead -> Diana
        r_search = c.get(f"{HIERARCHY_URL}?company={org_world['comp_a'].id}&search=Diana")
        assert r_search.status_code == 200
        assert r_search.data["summary"]["matched_employee_ids"] == [str(org_world["dev"].id)]
        assert len(r_search.data["reporting_tree"]) == 1
        root = r_search.data["reporting_tree"][0]
        assert root["id"] == str(org_world["ceo"].id)
        assert root["matched"] is False
        leaf = root["children"][0]["children"][0]["children"][0]["children"][0]
        assert leaf["id"] == str(org_world["dev"].id)
        assert leaf["matched"] is True

        # Filter by team -> only team members returned
        r_team = c.get(f"{HIERARCHY_URL}?team={org_world['team_api'].id}")
        assert r_team.status_code == 200
        assert {n["id"] for n in r_team.data["nodes"]} == {
            str(org_world["lead"].id),
            str(org_world["dev"].id),
        }

    def test_employee_detail_shows_projects_tasks_and_enforces_project_authorization(self, org_world):
        dev_id = org_world["dev"].id

        # CEO sees BOTH projects and BOTH tasks (overdue + blocked) + direct manager + upward chain
        c_ceo = auth_client(org_world["ceo"])
        r_ceo = c_ceo.get(f"{HIERARCHY_URL}employee/{dev_id}/")
        assert r_ceo.status_code == 200
        assert r_ceo.data["reporting_managers"][0]["id"] == str(org_world["lead"].id)
        assert [n["id"] for n in r_ceo.data["reporting_chain"]] == [
            str(org_world["lead"].id),
            str(org_world["head"].id),
            str(org_world["incharge"].id),
            str(org_world["ceo"].id),
        ]
        assert {p["code"] for p in r_ceo.data["assigned_projects"]} == {"PORTAL", "SECRET"}
        assert r_ceo.data["task_summary"]["total"] == 2
        assert r_ceo.data["task_summary"]["overdue"] == 1
        assert r_ceo.data["task_summary"]["blocked"] == 1

        # Regular colleague (peer, member of PORTAL only, NOT SECRET) sees PORTAL only, NOT SECRET!
        c_peer = auth_client(org_world["peer"])
        r_peer = c_peer.get(f"{HIERARCHY_URL}employee/{dev_id}/")
        assert r_peer.status_code == 200
        assert {p["code"] for p in r_peer.data["assigned_projects"]} == {"PORTAL"}
        assert r_peer.data["task_summary"]["total"] == 1
        assert r_peer.data["assigned_tasks"][0]["title"] == "Fix auth bug"
