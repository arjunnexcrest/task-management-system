import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import MilestoneTask, Project, ProjectMember, ProjectMilestone, ProjectPhase
from apps.rbac.models import MembershipRole, Role
from apps.reviews.models import TaskReview
from apps.system_settings.models import SystemSetting
from apps.tasks.models import Task, TaskDependency


class Task18QAProjectsTasksSubtasksApprovalsTestCase(APITestCase):
    """
    Task #18 — QA Verification Suite: Projects, Tasks, Subtasks & Approvals.
    Covers SRS §6.1–§6.6, §7.1–§7.11, §8.1–§8.4, §14.1–§14.4, §20.2, §22 & DB Design §4–§8.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._temp_media_dir = tempfile.mkdtemp(prefix="tms_test_qa18_")
        cls._media_override = override_settings(MEDIA_ROOT=cls._temp_media_dir)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._temp_media_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.today = timezone.now().date()

        self.ceo = User.objects.create_user(
            email="qa18_ceo@acme.com",
            username="qa18_ceo",
            full_name="QA18 CEO",
            designation="Chief Executive Officer",
            password="Password123!",
            is_superuser=True,
            is_staff=True,
        )
        self.incharge = User.objects.create_user(
            email="qa18_incharge@acme.com",
            username="qa18_incharge",
            full_name="QA18 Company In-Charge",
            designation="Company In-Charge",
            password="Password123!",
        )
        self.pm = User.objects.create_user(
            email="qa18_pm@acme.com",
            username="qa18_pm",
            full_name="QA18 Project Manager",
            designation="Project Manager",
            password="Password123!",
        )
        self.head = User.objects.create_user(
            email="qa18_head@acme.com",
            username="qa18_head",
            full_name="QA18 Dept Head",
            designation="Department Head",
            password="Password123!",
        )
        self.lead = User.objects.create_user(
            email="qa18_lead@acme.com",
            username="qa18_lead",
            full_name="QA18 Team Leader",
            designation="Team Leader",
            password="Password123!",
        )
        self.dev = User.objects.create_user(
            email="qa18_dev@acme.com",
            username="qa18_dev",
            full_name="QA18 Employee Dev",
            designation="Software Engineer",
            password="Password123!",
        )
        self.outsider = User.objects.create_user(
            email="qa18_outsider@beta.com",
            username="qa18_outsider",
            full_name="QA18 Beta PM",
            designation="Project Manager",
            password="Password123!",
        )

        self.company_a = Company.objects.create(name="QA18 Alpha Corp", code="QA18-A")
        self.company_b = Company.objects.create(name="QA18 Beta Corp", code="QA18-B")

        role_ceo, _ = Role.objects.get_or_create(
            code="CEO", defaults={"name": "CEO", "is_system_role": True}
        )
        role_incharge, _ = Role.objects.get_or_create(
            code="COMPANY_INCHARGE", defaults={"name": "Company In-Charge", "is_system_role": True}
        )
        role_pm, _ = Role.objects.get_or_create(
            code="ProjectManager", defaults={"name": "Project Manager", "is_system_role": True}
        )
        role_head, _ = Role.objects.get_or_create(
            code="DepartmentHead", defaults={"name": "Department Head", "is_system_role": True}
        )
        role_lead, _ = Role.objects.get_or_create(
            code="TeamLeader", defaults={"name": "Team Leader", "is_system_role": True}
        )
        role_emp, _ = Role.objects.get_or_create(
            code="Employee", defaults={"name": "Employee", "is_system_role": True}
        )

        memberships_a = [
            (self.ceo, role_ceo),
            (self.incharge, role_incharge),
            (self.pm, role_pm),
            (self.head, role_head),
            (self.lead, role_lead),
            (self.dev, role_emp),
        ]
        for user_obj, role_obj in memberships_a:
            cm = CompanyMembership.objects.create(company=self.company_a, user=user_obj, status="Active")
            MembershipRole.objects.create(membership=cm, role=role_obj)

        cm_b_ceo = CompanyMembership.objects.create(company=self.company_b, user=self.ceo, status="Active")
        MembershipRole.objects.create(membership=cm_b_ceo, role=role_ceo)
        cm_b_out = CompanyMembership.objects.create(company=self.company_b, user=self.outsider, status="Active")
        MembershipRole.objects.create(membership=cm_b_out, role=role_pm)

        self.dept_a = Department.objects.create(company=self.company_a, name="Engineering A", code="ENGA")
        self.dept_b = Department.objects.create(company=self.company_b, name="Engineering B", code="ENGB")
        DepartmentMembership.objects.create(
            department=self.dept_a, user=self.head, is_head=True, start_date=self.today, status="Active"
        )
        DepartmentMembership.objects.create(
            department=self.dept_a, user=self.lead, is_head=False, start_date=self.today, status="Active"
        )
        DepartmentMembership.objects.create(
            department=self.dept_a, user=self.dev, is_head=False, start_date=self.today, status="Active"
        )

        self.team_a = Team.objects.create(department=self.dept_a, name="Platform A", code="PLATA")
        self.team_b = Team.objects.create(department=self.dept_b, name="Platform B", code="PLATB")
        TeamMembership.objects.create(team=self.team_a, user=self.lead, is_leader=True, status="Active")
        TeamMembership.objects.create(team=self.team_a, user=self.dev, is_leader=False, status="Active")

    # ------------------------------------------------------------------
    # TC-PRJ: Projects validation, constraints, closure & multi-project participation
    # ------------------------------------------------------------------
    def test_tc_prj_validation_constraints_closure_and_multi_project_participation(self):
        self.client.force_authenticate(user=self.ceo)

        # 1. Active project without PM rejected on POST
        no_pm_active = self.client.post(
            "/api/projects/",
            {
                "company": str(self.company_a.id),
                "name": "No PM Project",
                "code": "PRJ-NOPM",
                "status": "Active",
            },
            format="json",
        )
        self.assertEqual(no_pm_active.status_code, status.HTTP_400_BAD_REQUEST)

        # 2. Invalid date range (planned_completion_date < start_date) rejected
        bad_dates = self.client.post(
            "/api/projects/",
            {
                "company": str(self.company_a.id),
                "project_manager": str(self.pm.id),
                "name": "Bad Dates Project",
                "code": "PRJ-DATES",
                "status": "Active",
                "start_date": str(self.today + timedelta(days=10)),
                "planned_completion_date": str(self.today + timedelta(days=2)),
            },
            format="json",
        )
        self.assertEqual(bad_dates.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. Cross-company PM / Department / Team / Member rejected
        for field, val in [
            ("project_manager", str(self.outsider.id)),
            ("department_ids", [str(self.dept_b.id)]),
            ("team_ids", [str(self.team_b.id)]),
            ("member_ids", [str(self.outsider.id)]),
        ]:
            payload = {
                "company": str(self.company_a.id),
                "name": f"Cross {field}",
                "code": f"PRJ-X-{field[:4].upper()}",
                "status": "Draft",
                field: val,
            }
            resp = self.client.post("/api/projects/", payload, format="json")
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, f"Expected 400 for cross-company {field}")

        # 4. Same project code allowed across DIFFERENT companies (company-scoped uniqueness)
        p_a = self.client.post(
            "/api/projects/",
            {
                "company": str(self.company_a.id),
                "project_manager": str(self.pm.id),
                "name": "Alpha Shared Code Project",
                "code": "PRJ-SHARED",
                "status": "Active",
                "member_ids": [str(self.dev.id)],
            },
            format="json",
        )
        p_b = self.client.post(
            "/api/projects/",
            {
                "company": str(self.company_b.id),
                "project_manager": str(self.outsider.id),
                "name": "Beta Shared Code Project",
                "code": "PRJ-SHARED",
                "status": "Active",
            },
            format="json",
        )
        self.assertEqual(p_a.status_code, status.HTTP_201_CREATED, p_a.content)
        self.assertEqual(p_b.status_code, status.HTTP_201_CREATED, p_b.content)
        proj_a_id = p_a.data["id"]

        # 5. Activating a Draft project via PATCH without PM is rejected
        draft_p = self.client.post(
            "/api/projects/",
            {"company": str(self.company_a.id), "name": "Draft Only", "code": "PRJ-DRAFT", "status": "Draft"},
            format="json",
        )
        self.assertEqual(draft_p.status_code, status.HTTP_201_CREATED)
        bad_activate = self.client.patch(
            f"/api/projects/{draft_p.data['id']}/", {"status": "Active"}, format="json"
        )
        self.assertEqual(bad_activate.status_code, status.HTTP_400_BAD_REQUEST)

        # 6. Removing accountable PM of an Active project via ProjectMember PATCH is rejected
        pm_member = ProjectMember.objects.get(project_id=proj_a_id, user=self.pm)
        rem_pm = self.client.patch(
            f"/api/projects/members/{pm_member.id}/", {"status": "Removed"}, format="json"
        )
        self.assertEqual(rem_pm.status_code, status.HTTP_400_BAD_REQUEST)

        # 7. Project closure validation: empty remarks, open top-level task, AND open subtask
        parent = Task.objects.create(
            project_id=proj_a_id,
            title="Parent Deliverable",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
        )
        sub = Task.objects.create(
            project_id=proj_a_id,
            parent_task=parent,
            title="Open Subtask Deliverable",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
        )
        # Empty closure remarks -> 400
        self.assertEqual(
            self.client.post(
                f"/api/projects/{proj_a_id}/close_project/", {"closure_remarks": "   "}, format="json"
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        # Open top-level task -> 400
        self.assertEqual(
            self.client.post(
                f"/api/projects/{proj_a_id}/close_project/",
                {"closure_remarks": "Trying early close"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        # Complete parent via authorized override while subtask remains open -> project closure STILL blocked!
        self.client.force_authenticate(user=self.pm)
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{parent.id}/transition-status/",
                {
                    "status": "Completed",
                    "override_subtasks": True,
                    "override_reason": "Parent marked done, verifying project closure guard",
                },
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.post(
                f"/api/projects/{proj_a_id}/close_project/",
                {"closure_remarks": "Subtask still open"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        # Complete subtask -> project closure succeeds cleanly
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{sub.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            status.HTTP_200_OK,
        )
        close_ok = self.client.post(
            f"/api/projects/{proj_a_id}/close_project/",
            {"closure_remarks": "All tasks and subtasks verified complete."},
            format="json",
        )
        self.assertEqual(close_ok.status_code, status.HTTP_200_OK, close_ok.content)
        self.assertEqual(close_ok.data["status"], "Completed")
        self.assertEqual(Decimal(str(close_ok.data["progress"])), Decimal("100.00"))

        # 8. Multi-project participation matrix across 2 projects for self.dev
        p_a2 = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm,
            name="Alpha Secondary Project",
            code="PRJ-A2",
            status=Project.Status.ACTIVE,
        )
        ProjectMember.objects.create(project=p_a2, user=self.dev, role_in_project="QA Engineer", status="Active")
        Task.objects.create(project=p_a2, title="Secondary Task", created_by=self.pm, task_owner=self.dev)

        part_res = self.client.get(f"/api/projects/user_participations/?user={self.dev.id}&company={self.company_a.id}")
        self.assertEqual(part_res.status_code, status.HTTP_200_OK)
        participations = part_res.data if isinstance(part_res.data, list) else part_res.data.get("participations", [])
        self.assertEqual(len(participations), 2)
        roles_by_code = {p["project_code"]: p["role_in_project"] for p in participations}
        self.assertEqual(roles_by_code["PRJ-A2"], "QA Engineer")

    # ------------------------------------------------------------------
    # TC-TSK: Tasks validation, RBAC boundaries & cancelled-task exclusion
    # ------------------------------------------------------------------
    def test_tc_tsk_validation_rbac_boundaries_and_cancelled_exclusion(self):
        project_1 = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm,
            name="Task Validation Project 1",
            code="PRJ-TV1",
            status=Project.Status.ACTIVE,
        )
        project_2 = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm,
            name="Task Validation Project 2",
            code="PRJ-TV2",
            status=Project.Status.ACTIVE,
        )
        phase_p1 = ProjectPhase.objects.create(project=project_1, name="P1 Phase")
        phase_p2 = ProjectPhase.objects.create(project=project_2, name="P2 Phase")
        ms_p1 = ProjectMilestone.objects.create(
            project=project_1, title="P1 Milestone", target_date=self.today + timedelta(days=10)
        )

        self.client.force_authenticate(user=self.pm)

        # 1. Invalid due_date < start_date rejected
        self.assertEqual(
            self.client.post(
                "/api/tasks/",
                {
                    "project": str(project_1.id),
                    "title": "Bad Date Task",
                    "start_date": str(self.today + timedelta(days=5)),
                    "due_date": str(self.today + timedelta(days=1)),
                },
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # 2. Out-of-range progress (-5 and 150) rejected
        for bad_prog in ("-5.00", "150.00"):
            self.assertEqual(
                self.client.post(
                    "/api/tasks/",
                    {"project": str(project_1.id), "title": "Bad Prog", "progress": bad_prog},
                    format="json",
                ).status_code,
                status.HTTP_400_BAD_REQUEST,
            )

        # 3. Cross-project phase assignment rejected
        self.assertEqual(
            self.client.post(
                "/api/tasks/",
                {"project": str(project_1.id), "phase": str(phase_p2.id), "title": "Wrong Phase Task"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # 4. Create two tasks in phase_p1 and ms_p1, verify cancelled task exclusion from rollups
        t1 = Task.objects.create(
            project=project_1,
            phase=phase_p1,
            title="Active Execution Task",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
            progress=Decimal("80.00"),
        )
        t2 = Task.objects.create(
            project=project_1,
            phase=phase_p1,
            title="Cancelled Scope Task",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.TO_DO,
            progress=Decimal("0.00"),
        )
        MilestoneTask.objects.create(milestone=ms_p1, task=t1)
        MilestoneTask.objects.create(milestone=ms_p1, task=t2)
        project_1.recalculate_progress()

        project_1.refresh_from_db()
        phase_p1.refresh_from_db()
        ms_p1.refresh_from_db()
        self.assertEqual(project_1.progress, Decimal("40.00"))
        self.assertEqual(phase_p1.progress, Decimal("40.00"))
        self.assertEqual(ms_p1.progress, Decimal("40.00"))

        # Cancel t2 -> project, phase, and milestone all exclude cancelled task and jump to 80.00%
        cancel_res = self.client.post(
            f"/api/tasks/{t2.id}/transition-status/",
            {"status": "Cancelled", "reason": "Descope per client"},
            format="json",
        )
        self.assertEqual(cancel_res.status_code, status.HTTP_200_OK)
        project_1.refresh_from_db()
        phase_p1.refresh_from_db()
        ms_p1.refresh_from_db()
        self.assertEqual(project_1.progress, Decimal("80.00"))
        self.assertEqual(phase_p1.progress, Decimal("80.00"))
        self.assertEqual(ms_p1.progress, Decimal("80.00"))

        # 5. Cross-company assignee on /assign/ and /bulk_assign/ rejected
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t1.id}/assign/",
                {"assignment_type": "User", "user": str(self.outsider.id)},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                "/api/tasks/bulk_assign/",
                {"task_ids": [str(t1.id)], "user": str(self.outsider.id)},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # 6. Employee RBAC boundaries on Tasks (cannot create/edit/delete/assign/reschedule/override subtasks)
        open_sub = Task.objects.create(
            project=project_1,
            parent_task=t1,
            title="Employee Cannot Override Subtask",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
        )
        self.client.force_authenticate(user=self.dev)
        self.assertEqual(
            self.client.post(
                "/api/tasks/", {"project": str(project_1.id), "title": "Unauthorized Create"}, format="json"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.patch(f"/api/tasks/{t1.id}/", {"title": "Unauthorized Edit"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.delete(f"/api/tasks/{t1.id}/").status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t1.id}/reschedule/", {"due_date": str(self.today + timedelta(days=30))}, format="json"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        # Employee attempting to force-complete parent with override_subtasks=True -> 403 Forbidden!
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t1.id}/transition-status/",
                {
                    "status": "Completed",
                    "override_subtasks": True,
                    "override_reason": "Employee trying unauthorized override",
                },
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        # Employee CAN log progress & post comments on their task
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{open_sub.id}/progress-updates/",
                {"progress_percentage": 50, "work_completed": "Halfway done"},
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t1.id}/comments/",
                {"content": "Status update from assignee"},
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )

    # ------------------------------------------------------------------
    # TC-SUB: Multi-level Subtasks, weighted rollups, sibling dependencies & tree cleanup
    # ------------------------------------------------------------------
    def test_tc_sub_multilevel_nesting_weighted_rollup_dependencies_and_cleanup(self):
        SystemSetting.objects.update_or_create(
            company=self.company_a, setting_key="subtask_max_depth", defaults={"setting_value": 2}
        )
        proj = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm,
            name="Subtask Deep Hierarchy Project",
            code="PRJ-SUB18",
            status=Project.Status.ACTIVE,
        )
        other_proj = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm,
            name="Other Project",
            code="PRJ-OTH18",
            status=Project.Status.ACTIVE,
        )
        phase = ProjectPhase.objects.create(project=proj, name="Phase Subtasks")
        ms = ProjectMilestone.objects.create(
            project=proj, title="MS Subtasks", target_date=self.today + timedelta(days=15)
        )

        self.client.force_authenticate(user=self.pm)

        # Depth 0: Parent (weighted Subtasks progress)
        parent_res = self.client.post(
            "/api/tasks/",
            {
                "project": str(proj.id),
                "phase": str(phase.id),
                "title": "L0: Payment Module",
                "task_owner": str(self.dev.id),
                "progress_method": "Subtasks",
                "status": "In Progress",
            },
            format="json",
        )
        self.assertEqual(parent_res.status_code, status.HTTP_201_CREATED, parent_res.content)
        parent_id = parent_res.data["id"]
        MilestoneTask.objects.create(milestone=ms, task_id=parent_id)

        # Depth 1: Child1 (6h, Subtasks) & Child2 (4h, Manual)
        c1_res = self.client.post(
            "/api/tasks/",
            {
                "project": str(proj.id),
                "parent_task": parent_id,
                "title": "L1: Gateway Adapter",
                "task_owner": str(self.dev.id),
                "progress_method": "Subtasks",
                "estimated_effort": "6.00",
                "status": "In Progress",
            },
            format="json",
        )
        c2_res = self.client.post(
            "/api/tasks/",
            {
                "project": str(proj.id),
                "parent_task": parent_id,
                "title": "L1: Billing Invoices",
                "task_owner": str(self.dev.id),
                "progress_method": "Manual",
                "estimated_effort": "4.00",
                "status": "To Do",
            },
            format="json",
        )
        self.assertEqual(c1_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(c2_res.status_code, status.HTTP_201_CREATED)
        c1_id, c2_id = c1_res.data["id"], c2_res.data["id"]

        # Depth 2: Grandchild1 (2h) & Grandchild2 (2h) under Child1
        gc1_res = self.client.post(
            "/api/tasks/",
            {
                "project": str(proj.id),
                "parent_task": c1_id,
                "title": "L2: Token Signing",
                "task_owner": str(self.dev.id),
                "estimated_effort": "2.00",
                "status": "In Progress",
            },
            format="json",
        )
        gc2_res = self.client.post(
            "/api/tasks/",
            {
                "project": str(proj.id),
                "parent_task": c1_id,
                "title": "L2: Webhook Verification",
                "task_owner": str(self.dev.id),
                "estimated_effort": "2.00",
                "status": "To Do",
            },
            format="json",
        )
        self.assertEqual(gc1_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(gc2_res.status_code, status.HTTP_201_CREATED)
        gc1_id, gc2_id = gc1_res.data["id"], gc2_res.data["id"]

        # Depth 3 rejected when subtask_max_depth=2
        gc3_res = self.client.post(
            "/api/tasks/",
            {"project": str(proj.id), "parent_task": gc1_id, "title": "L3: Too Deep"},
            format="json",
        )
        self.assertEqual(gc3_res.status_code, status.HTTP_400_BAD_REQUEST)

        # Cross-project parent_task & circular reparenting rejected
        self.assertEqual(
            self.client.post(
                "/api/tasks/",
                {"project": str(other_proj.id), "parent_task": parent_id, "title": "Cross Project Sub"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.patch(f"/api/tasks/{parent_id}/", {"parent_task": gc1_id}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # Sibling subtask FS dependency: GC2 depends on GC1
        TaskDependency.objects.create(
            task_id=gc2_id,
            depends_on_task_id=gc1_id,
            dependency_type=TaskDependency.DependencyType.FINISH_TO_START,
        )
        # GC2 cannot start before GC1 completes
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{gc2_id}/transition-status/", {"status": "In Progress"}, format="json"
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # Complete GC1 -> deep cascade rollup:
        # GC1=100%, GC2=0% -> C1=50% -> Parent=(50*6 + 0*4)/10=30% -> Phase=30%, MS=30%, Project=30%
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{gc1_id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            status.HTTP_200_OK,
        )
        c1_obj = Task.objects.get(id=c1_id)
        parent_obj = Task.objects.get(id=parent_id)
        phase.refresh_from_db()
        ms.refresh_from_db()
        proj.refresh_from_db()
        self.assertEqual(c1_obj.progress, Decimal("50.00"))
        self.assertEqual(parent_obj.progress, Decimal("30.00"))
        self.assertEqual(phase.progress, Decimal("30.00"))
        self.assertEqual(ms.progress, Decimal("30.00"))
        self.assertEqual(proj.progress, Decimal("30.00"))

        # GC2 can now start after GC1 completed
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{gc2_id}/transition-status/", {"status": "In Progress"}, format="json"
            ).status_code,
            status.HTTP_200_OK,
        )

        # Cancel C2 -> Parent recalculates over C1 only (50%), Project becomes 50%
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{c2_id}/transition-status/", {"status": "Cancelled"}, format="json"
            ).status_code,
            status.HTTP_200_OK,
        )
        parent_obj.refresh_from_db()
        proj.refresh_from_db()
        self.assertEqual(parent_obj.progress, Decimal("50.00"))
        self.assertEqual(proj.progress, Decimal("50.00"))

        # Delete GC2 -> C1 recalculates over GC1 only (100%) -> Parent 100% -> Project 100%
        self.assertEqual(
            self.client.delete(f"/api/tasks/{gc2_id}/").status_code, status.HTTP_204_NO_CONTENT
        )
        c1_obj.refresh_from_db()
        parent_obj.refresh_from_db()
        proj.refresh_from_db()
        self.assertEqual(c1_obj.progress, Decimal("100.00"))
        self.assertEqual(parent_obj.progress, Decimal("100.00"))
        self.assertEqual(proj.progress, Decimal("100.00"))

        # Verify subtask filtering endpoints
        subs_list = self.client.get(f"/api/tasks/{parent_id}/subtasks/").json()
        subs_items = subs_list["results"] if isinstance(subs_list, dict) and "results" in subs_list else subs_list
        self.assertEqual(len(subs_items), 2)

        top_only = self.client.get(f"/api/tasks/?project={proj.id}&is_subtask=false").json()
        top_items = top_only["results"] if isinstance(top_only, dict) and "results" in top_only else top_only
        self.assertEqual(len(top_items), 1)

        # Complete C1 -> Parent has C1=Completed & C2=Cancelled -> Parent completes WITHOUT override!
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{c1_id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{parent_id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            status.HTTP_200_OK,
        )

        # Deleting entire 3-level nested task tree cleans up all descendants and resets project progress to 0%
        self.assertEqual(
            self.client.delete(f"/api/tasks/{parent_id}/").status_code, status.HTTP_204_NO_CONTENT
        )
        self.assertEqual(Task.objects.filter(project=proj).count(), 0)
        proj.refresh_from_db()
        self.assertEqual(proj.progress, Decimal("0.00"))

    # ------------------------------------------------------------------
    # TC-APR: 3/4-level approval chain, mid-chain reset, precedence, immutability & policy
    # ------------------------------------------------------------------
    def test_tc_apr_multilevel_chain_midchain_reset_precedence_immutability_and_policy(self):
        proj = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm,
            name="Enterprise Approval Chain Project",
            code="PRJ-APR18",
            status=Project.Status.ACTIVE,
        )
        self.client.force_authenticate(user=self.pm)

        # 1. Company 3-level default chain: Team Leader -> Department Head -> Project Manager
        self.assertEqual(
            self.client.post(
                "/api/reviews/approval-chain/",
                {
                    "scope": "company",
                    "company": str(self.company_a.id),
                    "levels": ["Team Leader", "Department Head", "Project Manager"],
                    "allow_reviewer_reassignment": True,
                },
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )

        task_3lvl = Task.objects.create(
            project=proj,
            department=self.dept_a,
            team=self.team_a,
            title="3-Level Governance Release",
            created_by=self.pm,
            task_owner=self.dev,
            priority=Task.Priority.HIGH,
            status=Task.Status.IN_PROGRESS,
            approval_required=True,
        )

        # Verify chain precedence: Project 4-level overrides Company; Task custom overrides Project
        proj_4lvl = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm,
            name="4-Level Governance Project",
            code="PRJ-APR4",
            status=Project.Status.ACTIVE,
        )
        self.assertEqual(
            self.client.post(
                "/api/reviews/approval-chain/",
                {
                    "scope": "project",
                    "project": str(proj_4lvl.id),
                    "levels": ["Team Leader", "Department Head", "Project Manager", "Company In-Charge"],
                },
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        task_4lvl = Task.objects.create(
            project=proj_4lvl,
            department=self.dept_a,
            team=self.team_a,
            title="4-Level Executive Signoff",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
            approval_required=True,
        )
        c4 = self.client.get(f"/api/tasks/{task_4lvl.id}/review-chain/").json()["approval_chain"]
        self.assertEqual(c4["chain_source"], "Project Policy")
        self.assertEqual(c4["total_steps"], 4)
        self.assertEqual(c4["levels"], ["Team Leader", "Department Head", "Project Manager", "Company In-Charge"])

        # Task custom override overrides Project Policy
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{task_4lvl.id}/review-chain/",
                {"levels": ["Project Manager"], "user_overrides": {"1": str(self.pm.id)}},
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        c_custom = self.client.get(f"/api/tasks/{task_4lvl.id}/review-chain/").json()["approval_chain"]
        self.assertEqual(c_custom["chain_source"], "Task Custom")
        self.assertEqual(c_custom["total_steps"], 1)

        # 2. Execute 3-level chain on task_3lvl with mid-chain Changes Requested reset at Step 2!
        self.client.force_authenticate(user=self.dev)
        self.assertEqual(
            self.client.post(f"/api/tasks/{task_3lvl.id}/submit-for-review/", {"notes": "v1 ready"}, format="json").status_code,
            status.HTTP_200_OK,
        )
        task_3lvl.refresh_from_db()
        self.assertEqual(task_3lvl.reviewer_id, self.lead.id)

        # Step 1 (Team Leader) approves -> forwards to Step 2 (Dept Head)
        self.client.force_authenticate(user=self.lead)
        r1 = self.client.post(
            "/api/reviews/submit-decision/",
            {"task": str(task_3lvl.id), "decision": "Approved", "comments": "TL Round 1 OK"},
            format="json",
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED, r1.content)
        review_id = r1.data["id"]
        task_3lvl.refresh_from_db()
        self.assertEqual(task_3lvl.status, Task.Status.IN_REVIEW)
        self.assertEqual(task_3lvl.reviewer_id, self.head.id)

        # Step 3 (PM) cannot skip ahead while Step 2 (Dept Head) is pending -> 403
        self.client.force_authenticate(user=self.pm)
        self.assertEqual(
            self.client.post(
                "/api/reviews/submit-decision/",
                {"task": str(task_3lvl.id), "decision": "Approved", "comments": "Skip head"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Step 2 (Dept Head) requests Changes Requested -> resets approval cycle!
        self.client.force_authenticate(user=self.head)
        r2 = self.client.post(
            "/api/reviews/submit-decision/",
            {
                "task": str(task_3lvl.id),
                "decision": "Changes Requested",
                "correction_reason": "Add security audit evidence",
                "comments": "Needs audit log export",
            },
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        task_3lvl.refresh_from_db()
        self.assertEqual(task_3lvl.status, Task.Status.CHANGES_REQUESTED)

        # Dev resubmits -> cycle MUST reset back to Step 1 (Team Leader)!
        self.client.force_authenticate(user=self.dev)
        self.assertEqual(
            self.client.post(f"/api/tasks/{task_3lvl.id}/submit-for-review/", {"notes": "v2 with audit evidence"}, format="json").status_code,
            status.HTTP_200_OK,
        )
        task_3lvl.refresh_from_db()
        self.assertEqual(task_3lvl.reviewer_id, self.lead.id)

        # Dept Head cannot jump ahead of reset Step 1 -> 403
        self.client.force_authenticate(user=self.head)
        self.assertEqual(
            self.client.post(
                "/api/reviews/submit-decision/",
                {"task": str(task_3lvl.id), "decision": "Approved", "comments": "Skip TL after reset"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Complete all 3 steps sequentially: TL -> Dept Head -> PM
        for reviewer_user, msg in [
            (self.lead, "TL v2 approved"),
            (self.head, "Dept Head v2 approved"),
            (self.pm, "PM final approval"),
        ]:
            self.client.force_authenticate(user=reviewer_user)
            res = self.client.post(
                "/api/reviews/submit-decision/",
                {"task": str(task_3lvl.id), "decision": "Approved", "comments": msg},
                format="json",
            )
            self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)

        task_3lvl.refresh_from_db()
        self.assertEqual(task_3lvl.status, Task.Status.APPROVED)
        self.assertEqual(task_3lvl.reviews.count(), 5)

        # 3. Strict TaskReview audit immutability on PUT, PATCH, and DELETE (all -> 405)
        self.client.force_authenticate(user=self.pm)
        self.assertEqual(
            self.client.put(
                f"/api/reviews/{review_id}/",
                {"task": str(task_3lvl.id), "decision": "Rejected", "correction_reason": "Tampered"},
                format="json",
            ).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/reviews/{review_id}/", {"comments": "Tampered comment"}, format="json"
            ).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.delete(f"/api/reviews/{review_id}/").status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        orig_rev = TaskReview.objects.get(id=review_id)
        self.assertEqual(orig_rev.decision, TaskReview.Decision.APPROVED)
        self.assertEqual(orig_rev.comments, "TL Round 1 OK")

        # 4. allow_reviewer_reassignment=False policy enforcement
        self.assertEqual(
            self.client.post(
                "/api/reviews/approval-chain/",
                {
                    "scope": "company",
                    "company": str(self.company_a.id),
                    "levels": ["Reviewer"],
                    "allow_reviewer_reassignment": False,
                },
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        locked_task = Task.objects.create(
            project=proj,
            title="Reassignment Locked Task",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.lead,
            status=Task.Status.IN_REVIEW,
            approval_required=True,
        )
        self.client.force_authenticate(user=self.pm)
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{locked_task.id}/reassign-reviewer/",
                {"new_reviewer": str(self.head.id), "reason": "Try reassign under lock"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    # ------------------------------------------------------------------
    # TC-SUB-APR: Independent Subtask Approval + Parent Approval + Project Closure Integration
    # ------------------------------------------------------------------
    def test_tc_sub_apr_independent_subtask_and_parent_approval_integration(self):
        proj = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm,
            name="Integrated Subtask Approval Project",
            code="PRJ-SUBAPR",
            status=Project.Status.ACTIVE,
        )
        phase = ProjectPhase.objects.create(project=proj, name="Delivery Phase")
        ms = ProjectMilestone.objects.create(
            project=proj,
            title="Release Gate Milestone",
            target_date=self.today + timedelta(days=10),
            approval_required=False,
        )

        parent = Task.objects.create(
            project=proj,
            phase=phase,
            department=self.dept_a,
            team=self.team_a,
            title="Parent Release Package",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.pm,
            status=Task.Status.IN_PROGRESS,
            progress_method=Task.ProgressMethod.SUBTASKS,
            approval_required=True,
        )
        MilestoneTask.objects.create(milestone=ms, task=parent)

        subtask = Task.objects.create(
            project=proj,
            parent_task=parent,
            department=self.dept_a,
            team=self.team_a,
            title="Security Penetration Subtask",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.lead,
            status=Task.Status.IN_PROGRESS,
            approval_required=True,
        )

        # 1. Subtask with approval_required=True cannot jump straight to Completed -> 400
        self.client.force_authenticate(user=self.dev)
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{subtask.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # 2. Subtask undergoes full independent review cycle: Submit -> Changes Requested -> Resubmit -> Approved
        self.assertEqual(
            self.client.post(f"/api/tasks/{subtask.id}/submit-for-review/", {"notes": "Pentest draft"}, format="json").status_code,
            status.HTTP_200_OK,
        )
        self.client.force_authenticate(user=self.lead)
        self.assertEqual(
            self.client.post(
                "/api/reviews/submit-decision/",
                {
                    "task": str(subtask.id),
                    "decision": "Changes Requested",
                    "correction_reason": "Missing OWASP checklist",
                },
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )
        self.client.force_authenticate(user=self.dev)
        self.assertEqual(
            self.client.post(f"/api/tasks/{subtask.id}/submit-for-review/", {"notes": "OWASP added"}, format="json").status_code,
            status.HTTP_200_OK,
        )
        self.client.force_authenticate(user=self.lead)
        self.assertEqual(
            self.client.post(
                "/api/reviews/submit-decision/",
                {"task": str(subtask.id), "decision": "Approved", "comments": "Subtask approved"},
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )

        # 3. Parent task gets approved, but CANNOT complete while Subtask is Approved (not yet Completed)
        self.client.force_authenticate(user=self.dev)
        self.assertEqual(
            self.client.post(f"/api/tasks/{parent.id}/submit-for-review/", {}, format="json").status_code,
            status.HTTP_200_OK,
        )
        self.client.force_authenticate(user=self.pm)
        self.assertEqual(
            self.client.post(
                "/api/reviews/submit-decision/",
                {"task": str(parent.id), "decision": "Approved", "comments": "Parent approved"},
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{parent.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # 4. Complete Subtask -> Parent rolls up to 100% -> Parent completes -> Phase & Milestone auto-complete -> Project closes!
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{subtask.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            status.HTTP_200_OK,
        )
        parent.refresh_from_db()
        self.assertEqual(parent.progress, Decimal("100.00"))

        self.assertEqual(
            self.client.post(
                f"/api/tasks/{parent.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            status.HTTP_200_OK,
        )
        phase.refresh_from_db()
        ms.refresh_from_db()
        proj.refresh_from_db()
        self.assertEqual(phase.status, ProjectPhase.Status.COMPLETED)
        self.assertEqual(ms.status, ProjectMilestone.Status.COMPLETED)
        self.assertEqual(proj.progress, Decimal("100.00"))

        close_res = self.client.post(
            f"/api/projects/{proj.id}/close_project/",
            {"closure_remarks": "Subtask approval, parent approval, phase & milestone verified complete."},
            format="json",
        )
        self.assertEqual(close_res.status_code, status.HTTP_200_OK)
        self.assertEqual(close_res.data["status"], "Completed")

