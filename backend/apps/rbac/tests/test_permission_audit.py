"""
D1-12 Comprehensive Permission & Authorization Gap Verification Suite
Tests every recorded authorization defect (PERM-01 through PERM-12) across all roles:
- Anonymous (401)
- Unassigned authenticated user (403)
- Deactivated / Locked user holding unexpired JWT (403)
- Employee, TeamLeader, DepartmentHead, ProjectManager, CompanyInCharge, CEO
- Multi-company mixed-role users (DepartmentHead in Co A, Employee in Co B)
"""
from datetime import date, timedelta
from decimal import Decimal
import pytest
from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import User
from apps.common.permissions import HasRole
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
)
from apps.organization.models import (
    Department,
    DepartmentMembership,
    ReportingRelationship,
    Team,
    TeamMembership,
)
from apps.projects.models import (
    Project,
    ProjectDepartment,
    ProjectMember,
    ProjectTeam,
)
from apps.rbac.roles import SystemRole
from apps.reviews.models import TaskReview
from apps.risks.models import Blocker, Issue, Risk
from apps.system_settings.models import SystemSetting, WorkflowStatus
from apps.tasks.models import Attachment, Task, TaskAssignment, TaskAttachment
from apps.tracking.models import ProgressUpdate, TimeEntry


def _make_user(email: str, full_name: str, is_superuser: bool = False) -> User:
    username = email.split("@")[0].replace(".", "_")
    if is_superuser:
        return User.objects.create_superuser(
            email=email,
            username=username,
            full_name=full_name,
            password="Password123!",
        )
    return User.objects.create_user(
        email=email,
        username=username,
        full_name=full_name,
        password="Password123!",
    )


class PermissionAuditTestCase(TestCase):
    def setUp(self):
        # Companies A & B
        self.company_a = Company.objects.create(name="Alpha Corp", code="ALPHA")
        self.company_b = Company.objects.create(name="Beta Corp", code="BETA")

        # Users across all SRS roles
        self.ceo = _make_user("ceo@tms.test", "Global CEO", is_superuser=True)
        self.incharge_a = _make_user("incharge.a@tms.test", "Alpha InCharge")
        self.pm_a = _make_user("pm.a@tms.test", "Alpha PM")
        self.dept_head_a = _make_user("head.a@tms.test", "Alpha DeptHead")
        self.team_lead_a = _make_user("lead.a@tms.test", "Alpha TeamLead")
        self.emp_a1 = _make_user("emp1.a@tms.test", "Alpha Employee 1")
        self.emp_a2 = _make_user("emp2.a@tms.test", "Alpha Employee 2")
        self.emp_b = _make_user("emp.b@tms.test", "Beta Employee")
        self.pm_b = _make_user("pm.b@tms.test", "Beta PM")
        self.unassigned = _make_user("unassigned@tms.test", "Unassigned User")
        self.mixed_user = _make_user("mixed@tms.test", "Mixed Role User")

        # Company Memberships
        CompanyMembership.objects.create(company=self.company_a, user=self.ceo, role=SystemRole.CEO)
        CompanyMembership.objects.create(
            company=self.company_a, user=self.incharge_a, role=SystemRole.COMPANY_IN_CHARGE
        )
        CompanyMembership.objects.create(
            company=self.company_a, user=self.pm_a, role=SystemRole.PROJECT_MANAGER
        )
        CompanyMembership.objects.create(
            company=self.company_a, user=self.dept_head_a, role=SystemRole.DEPARTMENT_HEAD
        )
        CompanyMembership.objects.create(
            company=self.company_a, user=self.team_lead_a, role=SystemRole.TEAM_LEADER
        )
        CompanyMembership.objects.create(
            company=self.company_a, user=self.emp_a1, role=SystemRole.EMPLOYEE
        )
        CompanyMembership.objects.create(
            company=self.company_a, user=self.emp_a2, role=SystemRole.EMPLOYEE
        )
        CompanyMembership.objects.create(
            company=self.company_b, user=self.emp_b, role=SystemRole.EMPLOYEE
        )
        CompanyMembership.objects.create(
            company=self.company_b, user=self.pm_b, role=SystemRole.PROJECT_MANAGER
        )
        # Mixed-role user: DepartmentHead in Company A, Employee in Company B
        CompanyMembership.objects.create(
            company=self.company_a, user=self.mixed_user, role=SystemRole.DEPARTMENT_HEAD
        )
        CompanyMembership.objects.create(
            company=self.company_b, user=self.mixed_user, role=SystemRole.EMPLOYEE
        )

        # Org structure in Company A & B
        self.dept_a = Department.objects.create(company=self.company_a, name="Engineering A", code="ENGA")
        self.dept_b = Department.objects.create(company=self.company_b, name="Engineering B", code="ENGB")
        self.team_a = Team.objects.create(department=self.dept_a, name="Core Team A", code="CORA")
        DepartmentMembership.objects.create(
            department=self.dept_a,
            user=self.dept_head_a,
            is_head=True,
            start_date=date.today(),
            status="Active",
        )
        TeamMembership.objects.create(
            team=self.team_a, user=self.team_lead_a, is_leader=True, status="Active"
        )
        TeamMembership.objects.create(
            team=self.team_a, user=self.emp_a1, is_leader=False, status="Active"
        )

        # Projects:
        # proj_a1: Alpha Project 1 (emp_a1 is member; emp_a2 is NOT member)
        # proj_a2: Alpha Secret Project 2 (emp_a1 is NOT member)
        # proj_b1: Beta Project 1 (Company B)
        self.proj_a1 = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm_a,
            name="Alpha Platform",
            code="ALP-1",
            status=Project.Status.ACTIVE,
        )
        self.proj_a2 = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm_a,
            name="Alpha Confidential",
            code="ALP-2",
            status=Project.Status.ACTIVE,
        )
        self.proj_b1 = Project.objects.create(
            company=self.company_b,
            project_manager=self.pm_b,
            name="Beta Core",
            code="BET-1",
            status=Project.Status.ACTIVE,
        )
        ProjectDepartment.objects.create(project=self.proj_a1, department=self.dept_a)
        ProjectMember.objects.create(
            project=self.proj_a1, user=self.pm_a, role_in_project="Project Manager", status="Active"
        )
        ProjectMember.objects.create(
            project=self.proj_a1, user=self.emp_a1, role_in_project="Developer", status="Active"
        )
        ProjectMember.objects.create(
            project=self.proj_a1, user=self.team_lead_a, role_in_project="Team Lead", status="Active"
        )
        ProjectMember.objects.create(
            project=self.proj_b1, user=self.emp_b, role_in_project="Developer", status="Active"
        )

        # Tasks
        self.task_a1 = Task.objects.create(
            project=self.proj_a1,
            title="Alpha Task 1",
            created_by=self.pm_a,
            task_owner=self.emp_a1,
            reviewer=self.team_lead_a,
            status=Task.Status.IN_REVIEW,
        )
        TaskAssignment.objects.create(
            task=self.task_a1,
            assignment_type=TaskAssignment.AssignmentType.USER,
            user=self.emp_a1,
            assigned_by=self.pm_a,
            status=TaskAssignment.Status.ACTIVE,
        )
        self.task_a2 = Task.objects.create(
            project=self.proj_a2,
            title="Alpha Confidential Task",
            created_by=self.pm_a,
            task_owner=self.emp_a2,
            status=Task.Status.IN_PROGRESS,
        )
        self.task_b1 = Task.objects.create(
            project=self.proj_b1,
            title="Beta Task 1",
            created_by=self.pm_b,
            task_owner=self.emp_b,
            status=Task.Status.IN_PROGRESS,
        )

    def _client_for(self, user=None) -> APIClient:
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    # ------------------------------------------------------------------
    # PERM-01: Anonymous (401) & Unassigned (403) across ALL 9+ modules
    # ------------------------------------------------------------------
    def test_perm01_anonymous_gets_401_and_unassigned_gets_403_across_all_modules(self):
        endpoints = [
            "/api/projects/",
            "/api/projects/members/",
            "/api/projects/milestones/",
            "/api/tasks/",
            "/api/tasks/assignments/",
            "/api/tasks/attachments/",
            "/api/tracking/progress-updates/",
            "/api/tracking/time-entries/",
            "/api/reviews/",
            "/api/notifications/",
            "/api/notifications/preferences/",
            "/api/dashboards/summary/",
            "/api/reports/tasks/",
            "/api/risks/blockers/",
            "/api/risks/risks/",
            "/api/risks/issues/",
            "/api/settings/",
            "/api/settings/workflow-statuses/",
            "/api/settings/workflow-transitions/",
            "/api/lookup/",
        ]
        anon = self._client_for(None)
        for url in endpoints:
            res = anon.get(url)
            self.assertEqual(res.status_code, 401, f"Expected 401 for anon on {url}, got {res.status_code}")

        # Unassigned user must receive 403 on all company-scoped endpoints
        unassigned_client = self._client_for(self.unassigned)
        company_scoped_endpoints = [u for u in endpoints if not u.startswith("/api/notifications")]
        for url in company_scoped_endpoints:
            res = unassigned_client.get(url)
            self.assertEqual(
                res.status_code, 403, f"Expected 403 for unassigned user on {url}, got {res.status_code}"
            )

    # ------------------------------------------------------------------
    # PERM-02: Notifications, Deliveries & Preferences strict per-user isolation
    # ------------------------------------------------------------------
    def test_perm02_notifications_strictly_isolated_per_user(self):
        n_a1 = Notification.objects.create(
            user=self.emp_a1, notification_type="INFO", title="Mine", message="For A1"
        )
        n_b = Notification.objects.create(
            user=self.emp_b, notification_type="INFO", title="Secret B", message="For B"
        )
        d_b = NotificationDelivery.objects.create(
            notification=n_b, channel="Email", delivery_status="Sent"
        )
        pref_b = NotificationPreference.objects.create(
            user=self.emp_b, notification_type="TASK_ASSIGNED", in_app_enabled=True, email_enabled=False
        )

        c_a1 = self._client_for(self.emp_a1)
        # List only returns own notifications
        list_res = c_a1.get("/api/notifications/")
        self.assertEqual(list_res.status_code, 200)
        items = list_res.data["results"] if "results" in list_res.data else list_res.data
        self.assertEqual([i["id"] for i in items], [str(n_a1.id)])

        # Cannot read, mark_read, or delete another user's notification
        self.assertEqual(c_a1.get(f"/api/notifications/{n_b.id}/").status_code, 404)
        self.assertEqual(c_a1.post(f"/api/notifications/{n_b.id}/mark_read/").status_code, 404)
        self.assertEqual(c_a1.delete(f"/api/notifications/{n_b.id}/").status_code, 404)

        # Cannot spoof another user on create
        spoof_res = c_a1.post(
            "/api/notifications/",
            {"user": str(self.emp_b.id), "notification_type": "SPOOF", "title": "Hi", "message": "Bad"},
            format="json",
        )
        self.assertEqual(spoof_res.status_code, 403)

        # Cannot view or mutate other user's deliveries or preferences
        self.assertEqual(c_a1.get(f"/api/notifications/deliveries/{d_b.id}/").status_code, 404)
        self.assertEqual(
            c_a1.post(
                "/api/notifications/deliveries/",
                {"notification": str(n_a1.id), "channel": "Email", "delivery_status": "Sent"},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(c_a1.get(f"/api/notifications/preferences/{pref_b.id}/").status_code, 404)
        self.assertEqual(
            c_a1.post(
                "/api/notifications/preferences/",
                {"user": str(self.emp_b.id), "notification_type": "ALERT", "in_app_enabled": False},
                format="json",
            ).status_code,
            403,
        )

        # Can mark own notification read
        self.assertEqual(c_a1.post(f"/api/notifications/{n_a1.id}/mark_read/").status_code, 200)

    # ------------------------------------------------------------------
    # PERM-03: System Settings & Workflow Configuration RBAC
    # ------------------------------------------------------------------
    def test_perm03_system_settings_and_workflow_rbac(self):
        global_setting = SystemSetting.objects.create(
            company=None, setting_key="global.theme", setting_value={"mode": "light"}
        )
        co_a_setting = SystemSetting.objects.create(
            company=self.company_a, setting_key="alpha.hours", setting_value={"hours": 8}
        )
        co_b_setting = SystemSetting.objects.create(
            company=self.company_b, setting_key="beta.hours", setting_value={"hours": 7}
        )

        # Regular Employee can read global + own company settings, NOT Company B, and CANNOT mutate
        c_emp = self._client_for(self.emp_a1)
        res_list = c_emp.get("/api/settings/")
        self.assertEqual(res_list.status_code, 200)
        keys = {
            item["setting_key"]
            for item in (res_list.data["results"] if "results" in res_list.data else res_list.data)
        }
        self.assertIn("global.theme", keys)
        self.assertIn("alpha.hours", keys)
        self.assertNotIn("beta.hours", keys)

        self.assertEqual(
            c_emp.patch(f"/api/settings/{co_a_setting.id}/", {"setting_value": {"hours": 1}}, format="json").status_code,
            403,
        )
        self.assertEqual(
            c_emp.post(
                "/api/settings/workflow-statuses/",
                {"company": str(self.company_a.id), "entity_type": "Task", "status_code": "HACK", "display_name": "Hack"},
                format="json",
            ).status_code,
            403,
        )

        # CompanyInCharge A can modify Company A settings, CANNOT modify global or Company B settings
        c_ic = self._client_for(self.incharge_a)
        self.assertEqual(
            c_ic.patch(f"/api/settings/{co_a_setting.id}/", {"setting_value": {"hours": 9}}, format="json").status_code,
            200,
        )
        self.assertEqual(
            c_ic.patch(f"/api/settings/{global_setting.id}/", {"setting_value": {"mode": "dark"}}, format="json").status_code,
            403,
        )
        self.assertEqual(
            c_ic.post(
                "/api/settings/",
                {"company": None, "setting_key": "global.hack", "setting_value": {"x": 1}},
                format="json",
            ).status_code,
            403,
        )
        self.assertIn(
            c_ic.patch(f"/api/settings/{co_b_setting.id}/", {"setting_value": {"hours": 9}}, format="json").status_code,
            (403, 404),
        )

        # CEO can modify global settings and auto-records updated_by
        c_ceo = self._client_for(self.ceo)
        ceo_patch = c_ceo.patch(
            f"/api/settings/{global_setting.id}/",
            {"setting_value": {"mode": "dark"}},
            format="json",
        )
        self.assertEqual(ceo_patch.status_code, 200)
        global_setting.refresh_from_db()
        self.assertEqual(global_setting.updated_by_id, self.ceo.id)

    # ------------------------------------------------------------------
    # PERM-04: Project-level read isolation, write RBAC, & user_participations
    # ------------------------------------------------------------------
    def test_perm04_project_isolation_and_role_mutations(self):
        # Employee A1 only sees proj_a1 (assigned), NOT proj_a2 (unassigned in same co) or proj_b1
        c_emp = self._client_for(self.emp_a1)
        list_res = c_emp.get("/api/projects/")
        self.assertEqual(list_res.status_code, 200)
        proj_ids = [
            p["id"] for p in (list_res.data["results"] if "results" in list_res.data else list_res.data)
        ]
        self.assertEqual(proj_ids, [str(self.proj_a1.id)])
        self.assertEqual(c_emp.get(f"/api/projects/{self.proj_a2.id}/").status_code, 404)

        # Employee A1 gets 403 on all project mutations (create, patch, delete, assign_manager, archive, close)
        self.assertEqual(
            c_emp.post(
                "/api/projects/",
                {"company": str(self.company_a.id), "name": "Rogue", "code": "ROG-1"},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            c_emp.patch(f"/api/projects/{self.proj_a1.id}/", {"name": "Hacked"}, format="json").status_code,
            403,
        )
        self.assertEqual(c_emp.delete(f"/api/projects/{self.proj_a1.id}/").status_code, 403)
        self.assertEqual(
            c_emp.post(
                f"/api/projects/{self.proj_a1.id}/assign_manager/",
                {"project_manager": str(self.emp_a1.id)},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(c_emp.post(f"/api/projects/{self.proj_a1.id}/archive/").status_code, 403)

        # Cross-company user_participations blocked with 403
        cross_part = c_emp.get(f"/api/projects/user_participations/?user={self.emp_b.id}")
        self.assertEqual(cross_part.status_code, 403)

        # PM can update & map_departments on own project, CANNOT assign_manager or hard-delete
        c_pm = self._client_for(self.pm_a)
        self.assertEqual(
            c_pm.patch(f"/api/projects/{self.proj_a1.id}/", {"description": "Updated by PM"}, format="json").status_code,
            200,
        )
        self.assertEqual(
            c_pm.post(
                f"/api/projects/{self.proj_a1.id}/assign_manager/",
                {"project_manager": str(self.pm_a.id)},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(c_pm.delete(f"/api/projects/{self.proj_a1.id}/").status_code, 403)

        # DeptHead can map teams on project mapped to their department, CANNOT update project fields or delete
        c_head = self._client_for(self.dept_head_a)
        self.assertEqual(
            c_head.post(
                f"/api/projects/{self.proj_a1.id}/map_teams/",
                {"team_ids": [str(self.team_a.id)], "replace": True},
                format="json",
            ).status_code,
            200,
        )
        self.assertEqual(
            c_head.patch(f"/api/projects/{self.proj_a1.id}/", {"name": "DeptHead Edit"}, format="json").status_code,
            403,
        )

        # CompanyInCharge sees all Company A projects (including proj_a2) and can assign_manager
        c_ic = self._client_for(self.incharge_a)
        ic_list = c_ic.get("/api/projects/")
        ic_pids = {
            p["id"] for p in (ic_list.data["results"] if "results" in ic_list.data else ic_list.data)
        }
        self.assertEqual(ic_pids, {str(self.proj_a1.id), str(self.proj_a2.id)})
        self.assertEqual(
            c_ic.post(
                f"/api/projects/{self.proj_a1.id}/assign_manager/",
                {"project_manager": str(self.pm_a.id)},
                format="json",
            ).status_code,
            200,
        )

    # ------------------------------------------------------------------
    # PERM-05: Task scoping, Attachment download isolation, & bulk_assign / delete RBAC
    # ------------------------------------------------------------------
    def test_perm05_task_rbac_attachment_isolation_and_bulk_assign_guard(self):
        att_b = Attachment.objects.create(
            uploaded_by=self.emp_b,
            file_name="beta_payroll.pdf",
            storage_key="beta/payroll.pdf",
            mime_type="application/pdf",
            file_size_bytes=2048,
        )
        TaskAttachment.objects.create(task=self.task_b1, attachment=att_b)

        c_emp = self._client_for(self.emp_a1)
        # Task list hides task_a2 (unassigned project) and task_b1 (Company B)
        t_list = c_emp.get("/api/tasks/")
        t_ids = [t["id"] for t in (t_list.data["results"] if "results" in t_list.data else t_list.data)]
        self.assertEqual(t_ids, [str(self.task_a1.id)])

        # Attachment list & download cannot see or download Company B's attachment
        att_list = c_emp.get("/api/tasks/attachments/")
        att_ids = [
            a["id"] for a in (att_list.data["results"] if "results" in att_list.data else att_list.data)
        ]
        self.assertNotIn(str(att_b.id), att_ids)
        self.assertIn(c_emp.get(f"/api/tasks/attachments/{att_b.id}/download/").status_code, (403, 404))

        # Employee cannot delete, assign, bulk_assign, or reschedule tasks
        self.assertEqual(c_emp.delete(f"/api/tasks/{self.task_a1.id}/").status_code, 403)
        self.assertEqual(
            c_emp.post(
                f"/api/tasks/{self.task_a1.id}/assign/",
                {"assignment_type": "User", "user": str(self.emp_a2.id)},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            c_emp.post(
                "/api/tasks/bulk_assign/",
                {"task_ids": [str(self.task_a1.id)], "user": str(self.emp_a2.id)},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            c_emp.post(
                f"/api/tasks/{self.task_a1.id}/reschedule/",
                {"due_date": str(date.today() + timedelta(days=10))},
                format="json",
            ).status_code,
            403,
        )

        # PM in Company A CANNOT bulk_assign a task belonging to Company B!
        c_pm = self._client_for(self.pm_a)
        cross_bulk = c_pm.post(
            "/api/tasks/bulk_assign/",
            {"task_ids": [str(self.task_a1.id), str(self.task_b1.id)], "priority": "Critical"},
            format="json",
        )
        self.assertEqual(cross_bulk.status_code, 403)

    # ------------------------------------------------------------------
    # PERM-06: Reviews anti-self-approval & role enforcement
    # ------------------------------------------------------------------
    def test_perm06_employee_cannot_self_approve_task_via_reviews(self):
        c_emp = self._client_for(self.emp_a1)
        # Self-approval blocked on BOTH /api/reviews/ and /api/tasks/<id>/reviews/
        res1 = c_emp.post(
            "/api/reviews/",
            {"task": str(self.task_a1.id), "decision": "Approved", "comments": "Self LGTM"},
            format="json",
        )
        self.assertEqual(res1.status_code, 403)

        res2 = c_emp.post(
            f"/api/tasks/{self.task_a1.id}/reviews/",
            {"decision": "Approved", "comments": "Self LGTM"},
            format="json",
        )
        self.assertEqual(res2.status_code, 403)

        # Designated reviewer (TeamLeader) CAN approve the task
        c_lead = self._client_for(self.team_lead_a)
        res_ok = c_lead.post(
            f"/api/tasks/{self.task_a1.id}/reviews/",
            {"decision": "Approved", "comments": "Verified deliverables"},
            format="json",
        )
        self.assertEqual(res_ok.status_code, 201)
        self.assertEqual(str(res_ok.data["reviewer"]), str(self.team_lead_a.id))

    # ------------------------------------------------------------------
    # PERM-07: TimeEntry self-approval & other-user edit/delete blocked
    # ------------------------------------------------------------------
    def test_perm07_time_entry_and_progress_update_ownership_and_approval_guards(self):
        c_emp = self._client_for(self.emp_a1)
        # Employee cannot create pre-approved time entry
        self_approve_create = c_emp.post(
            "/api/tracking/time-entries/",
            {
                "task": str(self.task_a1.id),
                "duration_minutes": 120,
                "approval_status": "Approved",
            },
            format="json",
        )
        self.assertEqual(self_approve_create.status_code, 403)

        # Employee can create Pending time entry for themselves
        ok_te = c_emp.post(
            "/api/tracking/time-entries/",
            {"task": str(self.task_a1.id), "duration_minutes": 90},
            format="json",
        )
        self.assertEqual(ok_te.status_code, 201)
        te_id = ok_te.data["id"]

        # Employee cannot PATCH their own time entry to Approved
        self_approve_patch = c_emp.patch(
            f"/api/tracking/time-entries/{te_id}/",
            {"approval_status": "Approved"},
            format="json",
        )
        self.assertEqual(self_approve_patch.status_code, 403)

        # Another member (emp_a2 added to proj_a1) cannot edit or delete emp_a1's time entry or progress update
        ProjectMember.objects.create(
            project=self.proj_a1, user=self.emp_a2, role_in_project="QA", status="Active"
        )
        pu = ProgressUpdate.objects.create(
            task=self.task_a1, author=self.emp_a1, progress_percentage=Decimal("50.00")
        )
        c_emp2 = self._client_for(self.emp_a2)
        self.assertEqual(
            c_emp2.patch(f"/api/tracking/time-entries/{te_id}/", {"duration_minutes": 10}, format="json").status_code,
            403,
        )
        self.assertEqual(c_emp2.delete(f"/api/tracking/time-entries/{te_id}/").status_code, 403)
        self.assertEqual(
            c_emp2.patch(f"/api/tracking/progress-updates/{pu.id}/", {"remarks": "Tampered"}, format="json").status_code,
            403,
        )
        self.assertEqual(c_emp2.delete(f"/api/tracking/progress-updates/{pu.id}/").status_code, 403)

        # Manager (PM) CAN approve emp_a1's time entry and approved_by is auto-recorded
        c_pm = self._client_for(self.pm_a)
        mgr_approve = c_pm.patch(
            f"/api/tracking/time-entries/{te_id}/",
            {"approval_status": "Approved"},
            format="json",
        )
        self.assertEqual(mgr_approve.status_code, 200)
        te_obj = TimeEntry.objects.get(pk=te_id)
        self.assertEqual(te_obj.approval_status, TimeEntry.ApprovalStatus.APPROVED)
        self.assertEqual(te_obj.approved_by_id, self.pm_a.id)

    # ------------------------------------------------------------------
    # PERM-08: Risks, Issues & Blockers scoping and write RBAC
    # ------------------------------------------------------------------
    def test_perm08_risks_issues_blockers_permissions(self):
        risk_a1 = Risk.objects.create(
            project=self.proj_a1,
            title="Scale Risk",
            probability="Medium",
            impact="High",
            severity="High",
            owner=self.pm_a,
        )
        c_emp = self._client_for(self.emp_a1)

        # Employee can report a Blocker & Issue on their authorized project, but CANNOT create/delete Risks or delete Blockers
        blocker_res = c_emp.post(
            "/api/risks/blockers/",
            {
                "task": str(self.task_a1.id),
                "description": "Waiting on API credentials",
                "category": "External Dependency",
                "impact": "High",
            },
            format="json",
        )
        self.assertEqual(blocker_res.status_code, 201, blocker_res.content)
        blocker_id = blocker_res.data["id"]

        self.assertEqual(
            c_emp.post(
                "/api/risks/risks/",
                {
                    "project": str(self.proj_a1.id),
                    "title": "Unauthorized Risk",
                    "probability": "Low",
                    "impact": "Low",
                    "severity": "Low",
                },
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(c_emp.delete(f"/api/risks/risks/{risk_a1.id}/").status_code, 403)
        self.assertEqual(c_emp.delete(f"/api/risks/blockers/{blocker_id}/").status_code, 403)

        # PM can create & delete Risks and delete Blockers
        c_pm = self._client_for(self.pm_a)
        pm_risk = c_pm.post(
            "/api/risks/risks/",
            {
                "project": str(self.proj_a1.id),
                "title": "PM Risk",
                "probability": "Medium",
                "impact": "Medium",
                "severity": "Medium",
            },
            format="json",
        )
        self.assertEqual(pm_risk.status_code, 201, pm_risk.content)
        self.assertEqual(c_pm.delete(f"/api/risks/blockers/{blocker_id}/").status_code, 204)

    # ------------------------------------------------------------------
    # PERM-09: Dashboards & Reports scoping + CSV export role restriction
    # ------------------------------------------------------------------
    def test_perm09_dashboards_scoping_and_csv_export_role_gate(self):
        c_emp = self._client_for(self.emp_a1)
        # Dashboard counts only authorized projects/tasks (1 project, 1 task — NOT proj_a2 or proj_b1)
        dash = c_emp.get("/api/dashboards/summary/")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.data["projects"]["total_projects"], 1)
        self.assertEqual(dash.data["tasks"]["total_tasks_and_subtasks"], 1)

        # Reports & CSV export are restricted to leadership/reporting roles (403 for regular Employee)
        rep_json = c_emp.get("/api/reports/tasks/")
        self.assertEqual(rep_json.status_code, 403)

        rep_csv_emp = c_emp.get("/api/reports/tasks/?format=csv")
        self.assertEqual(rep_csv_emp.status_code, 403)

        # PM can view JSON report (scoped) and export CSV
        c_pm = self._client_for(self.pm_a)
        rep_json_pm = c_pm.get("/api/reports/tasks/")
        self.assertEqual(rep_json_pm.status_code, 200)
        rep_csv_pm = c_pm.get("/api/reports/tasks/?format=csv")
        self.assertEqual(rep_csv_pm.status_code, 200)
        self.assertIn("text/csv", rep_csv_pm["Content-Type"])

    # ------------------------------------------------------------------
    # PERM-10: Deactivated user with unexpired JWT is immediately blocked everywhere
    # ------------------------------------------------------------------
    def test_perm10_deactivated_user_with_live_jwt_blocked_immediately(self):
        token = str(AccessToken.for_user(self.emp_a1))
        self.emp_a1.deactivate()

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        for url in (
            "/api/companies/my-companies/",
            "/api/departments/",
            "/api/projects/",
            "/api/tasks/",
            "/api/notifications/",
            "/api/dashboards/summary/",
            "/api/lookup/",
        ):
            res = client.get(url)
            self.assertIn(
                res.status_code,
                (401, 403),
                f"Deactivated user should be blocked on {url}, got {res.status_code}",
            )

    # ------------------------------------------------------------------
    # PERM-11: Self-update privilege escalation (is_staff, is_active, account_status, company, role) blocked
    # ------------------------------------------------------------------
    def test_perm11_user_self_update_cannot_escalate_privileges(self):
        c_emp = self._client_for(self.emp_a1)
        for bad_payload in (
            {"is_staff": True},
            {"account_status": "Suspended"},
            {"is_active": False},
            {"role": "CEO"},
            {"company": str(self.company_b.id)},
        ):
            res = c_emp.patch(f"/api/users/{self.emp_a1.id}/", bad_payload, format="json")
            self.assertEqual(
                res.status_code,
                403,
                f"Expected 403 for privilege escalation payload {bad_payload}, got {res.status_code}",
            )
        self.emp_a1.refresh_from_db()
        self.assertFalse(self.emp_a1.is_staff)

        # Non-privileged self-update still succeeds
        ok_res = c_emp.patch(
            f"/api/users/{self.emp_a1.id}/",
            {"designation": "Senior Developer"},
            format="json",
        )
        self.assertEqual(ok_res.status_code, 200)

    # ------------------------------------------------------------------
    # PERM-12: HasRole(roles) enforces per-company object-level role check
    # ------------------------------------------------------------------
    def test_perm12_has_role_enforces_object_level_company_role(self):
        perm = HasRole([SystemRole.DEPARTMENT_HEAD])()
        factory = APIRequestFactory()
        req = factory.patch("/api/departments/dummy/")
        req.user = self.mixed_user  # DepartmentHead in Company A, Employee in Company B

        # Passes on Company A's department
        self.assertTrue(perm.has_object_permission(req, None, self.dept_a))
        # Fails on Company B's department (where mixed_user is only Employee!)
        self.assertFalse(perm.has_object_permission(req, None, self.dept_b))
        # Also verify multi-company ReportingRelationship endpoint does not leak Company B direct reports to Company A viewer
        ReportingRelationship.objects.create(
            company=self.company_b,
            employee=self.emp_b,
            manager=self.mixed_user,
            start_date=date.today(),
        )
        c_a1 = self._client_for(self.emp_a1)
        dr_res = c_a1.get(f"/api/employees/{self.mixed_user.id}/direct-reports/")
        self.assertEqual(dr_res.status_code, 200)
        self.assertEqual(dr_res.data["count"], 0)
