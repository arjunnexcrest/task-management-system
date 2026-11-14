from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import User
from apps.companies.models import CompanyMembership
from apps.notifications.models import Notification
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import (
    Project,
    ProjectDepartment,
    ProjectMember,
    ProjectTeam,
)


def _validate_user_belongs_to_company(user: User, company, label: str = "User"):
    if getattr(user, "is_superuser", False) or getattr(company, "company_incharge_id", None) == user.id:
        return
    has_any = (
        CompanyMembership.objects.filter(company=company).exists()
        or CompanyMembership.objects.filter(user=user).exists()
    )
    if not has_any:
        return
    is_member = CompanyMembership.objects.filter(
        company=company,
        user=user,
        status=CompanyMembership.Status.ACTIVE,
    ).exists()
    if not is_member:
        raise ValidationError(
            f"{label} '{user.full_name}' is not an active member of company '{company.code}'."
        )


@transaction.atomic
def assign_project_manager_service(
    *,
    project: Project,
    project_manager_id,
    auto_activate: bool = False,
) -> Project:
    """
    SRS §2.2, §2.3 & §6.3 Step 4:
    Assigns or reassigns an accountable Project Manager to the project,
    validates company membership, syncs ProjectMember role history, and notifies the manager.
    """
    if not project_manager_id:
        if project.status == Project.Status.ACTIVE:
            raise ValidationError(
                "Active projects require an accountable project manager (SRS §1.4 #2 & DB Design §4.3)."
            )
        project.project_manager = None
        project.save(update_fields=["project_manager"])
        return project

    manager = User.objects.filter(id=project_manager_id).first()
    if not manager:
        raise ValidationError("Selected project manager user was not found.")

    _validate_user_belongs_to_company(manager, project.company, label="Project manager")

    # Demote previous Project Manager in project_members while preserving membership history
    ProjectMember.objects.filter(
        project=project,
        role_in_project="Project Manager",
    ).exclude(user=manager).update(role_in_project="Former Project Manager")

    # Ensure new Project Manager is an Active ProjectMember
    ProjectMember.objects.update_or_create(
        project=project,
        user=manager,
        defaults={
            "role_in_project": "Project Manager",
            "status": ProjectMember.Status.ACTIVE,
            "left_at": None,
        },
    )

    project.project_manager = manager
    update_fields = ["project_manager"]
    if auto_activate and project.status in (Project.Status.DRAFT, Project.Status.PLANNED):
        project.status = Project.Status.ACTIVE
        update_fields.append("status")
    project.save(update_fields=update_fields)

    # Notify the assigned Project Manager (SRS §13.1)
    Notification.objects.create(
        user=manager,
        notification_type="PROJECT_MANAGER_ASSIGNED",
        title=f"Assigned as Project Manager: {project.code}",
        message=f"You have been assigned as the accountable Project Manager for '{project.name}' ({project.code}).",
        related_project=project,
    )

    return project


@transaction.atomic
def map_project_departments_service(
    *,
    project: Project,
    department_ids: list,
    replace: bool = True,
    auto_add_heads: bool = True,
) -> Project:
    """
    SRS §2.3, §5.1 & §6.3 Step 5:
    Maps participating departments to a project, validates same-company ownership,
    removes orphaned team mappings when a department is unmapped, and coordinates Department Heads.
    """
    unique_dept_ids = {str(d_id) for d_id in (department_ids or []) if d_id}
    if unique_dept_ids:
        depts = list(Department.objects.filter(id__in=unique_dept_ids, company=project.company))
        if len(depts) != len(unique_dept_ids):
            raise ValidationError(
                "All participating departments must exist and belong to the project's company (DB Design §4.3 & §5)."
            )

    if replace:
        ProjectDepartment.objects.filter(project=project).exclude(
            department_id__in=unique_dept_ids
        ).delete()
        # DB Design §4.3: project_teams must belong to a participating department
        ProjectTeam.objects.filter(project=project).exclude(
            team__department_id__in=unique_dept_ids
        ).delete()

    existing_dept_ids = {
        str(d_id)
        for d_id in ProjectDepartment.objects.filter(project=project).values_list(
            "department_id", flat=True
        )
    }
    newly_added_ids = unique_dept_ids - existing_dept_ids
    for dept_id in newly_added_ids:
        ProjectDepartment.objects.create(project=project, department_id=dept_id)

    # Coordinate Department Heads for participating departments (SRS §2.3 & §6.3 Step 5)
    if auto_add_heads and unique_dept_ids:
        head_memberships = DepartmentMembership.objects.filter(
            department_id__in=unique_dept_ids,
            is_head=True,
            status=DepartmentMembership.Status.ACTIVE,
        ).select_related("user", "department")
        for hm in head_memberships:
            role = (
                "Project Manager"
                if hm.user_id == project.project_manager_id
                else f"Department Head ({hm.department.name})"
            )
            pm_obj, created = ProjectMember.objects.update_or_create(
                project=project,
                user=hm.user,
                defaults={
                    "role_in_project": role,
                    "status": ProjectMember.Status.ACTIVE,
                    "left_at": None,
                },
            )
            if str(hm.department_id) in newly_added_ids:
                Notification.objects.create(
                    user=hm.user,
                    notification_type="DEPARTMENT_MAPPED_TO_PROJECT",
                    title=f"Department Mapped to Project: {project.code}",
                    message=f"Your department '{hm.department.name}' is now participating in project '{project.name}'.",
                    related_project=project,
                )

    return project


@transaction.atomic
def map_project_teams_service(
    *,
    project: Project,
    team_ids: list,
    replace: bool = True,
    auto_map_departments: bool = True,
    auto_add_leaders: bool = True,
) -> Project:
    """
    SRS §2.4, §5.2 & §6.3 Step 6 & DB Design §4.3:
    Maps participating teams to a project, ensures each team belongs to a participating
    company/department, and coordinates Team Leaders in ProjectMember.
    """
    unique_team_ids = {str(t_id) for t_id in (team_ids or []) if t_id}
    teams = []
    if unique_team_ids:
        teams = list(
            Team.objects.filter(
                id__in=unique_team_ids,
                department__company=project.company,
            ).select_related("department")
        )
        if len(teams) != len(unique_team_ids):
            raise ValidationError(
                "All participating teams must exist and belong to the project's company (DB Design §4.3 & §5)."
            )

        participating_dept_ids = {
            str(d_id)
            for d_id in ProjectDepartment.objects.filter(project=project).values_list(
                "department_id", flat=True
            )
        }
        for tm in teams:
            dept_id_str = str(tm.department_id)
            if dept_id_str not in participating_dept_ids:
                if auto_map_departments:
                    ProjectDepartment.objects.get_or_create(
                        project=project,
                        department_id=tm.department_id,
                    )
                    participating_dept_ids.add(dept_id_str)
                else:
                    raise ValidationError(
                        f"Team '{tm.name}' belongs to department '{tm.department.name}', which is not a participating department of this project (DB Design §4.3)."
                    )

    if replace:
        ProjectTeam.objects.filter(project=project).exclude(team_id__in=unique_team_ids).delete()

    existing_team_ids = {
        str(t_id)
        for t_id in ProjectTeam.objects.filter(project=project).values_list("team_id", flat=True)
    }
    for t_id in unique_team_ids - existing_team_ids:
        ProjectTeam.objects.create(project=project, team_id=t_id)

    # Coordinate Team Leaders (SRS §2.5, §5.2 & §6.3 Step 6)
    if auto_add_leaders and unique_team_ids:
        leader_memberships = TeamMembership.objects.filter(
            team_id__in=unique_team_ids,
            is_leader=True,
            status=TeamMembership.Status.ACTIVE,
        ).select_related("user", "team")
        for lm in leader_memberships:
            existing_pm = ProjectMember.objects.filter(project=project, user=lm.user).first()
            if existing_pm:
                if existing_pm.status != ProjectMember.Status.ACTIVE:
                    existing_pm.status = ProjectMember.Status.ACTIVE
                    existing_pm.left_at = None
                    existing_pm.save(update_fields=["status", "left_at"])
            else:
                ProjectMember.objects.create(
                    project=project,
                    user=lm.user,
                    role_in_project=f"Team Leader ({lm.team.name})",
                    status=ProjectMember.Status.ACTIVE,
                )

    return project


@transaction.atomic
def assign_project_members_service(
    *,
    project: Project,
    members: list[dict] | None = None,
    member_ids: list | None = None,
    replace: bool = False,
) -> Project:
    """
    SRS §6.1, §6.3 Step 6 & DB Design §1, §4.3:
    Assigns or updates project members and their project roles while preserving
    historical membership records (marking removed members as status='Removed' with left_at timestamp).
    """
    normalized: dict[str, str] = {}
    if member_ids:
        for uid in member_ids:
            if uid:
                normalized[str(uid)] = "Team Member"
    if members:
        for item in members:
            uid = item.get("user") or item.get("user_id")
            if uid:
                normalized[str(uid)] = (item.get("role_in_project") or "Team Member").strip()

    if project.project_manager_id:
        normalized[str(project.project_manager_id)] = "Project Manager"

    if normalized:
        users_qs = list(User.objects.filter(id__in=normalized.keys()))
        if len(users_qs) != len(normalized):
            raise ValidationError("One or more selected project members do not exist.")
        for u in users_qs:
            _validate_user_belongs_to_company(u, project.company, label="Project member")

    if replace:
        # Soft-remove members not in target set so historical membership is preserved (DB Design §1 & §4.3)
        exclude_ids = set(normalized.keys())
        if project.project_manager_id:
            exclude_ids.add(str(project.project_manager_id))
        ProjectMember.objects.filter(
            project=project,
            status=ProjectMember.Status.ACTIVE,
        ).exclude(user_id__in=exclude_ids).update(
            status=ProjectMember.Status.REMOVED,
            left_at=timezone.now(),
        )

    for uid_str, role in normalized.items():
        existing = ProjectMember.objects.filter(project=project, user_id=uid_str).first()
        if existing:
            keep_pm_role = (
                str(project.project_manager_id) == uid_str
                and existing.role_in_project == "Project Manager"
            )
            new_role = "Project Manager" if keep_pm_role else (role or existing.role_in_project)
            existing.role_in_project = new_role
            existing.status = ProjectMember.Status.ACTIVE
            existing.left_at = None
            existing.save(update_fields=["role_in_project", "status", "left_at"])
        else:
            ProjectMember.objects.create(
                project=project,
                user_id=uid_str,
                role_in_project=role,
                status=ProjectMember.Status.ACTIVE,
            )

    return project


def get_project_assignment_hierarchy(*, project: Project) -> dict:
    """
    Returns the complete SRS §6.3 Project Assignment Hierarchy:
    Company -> Project Manager -> Participating Departments (with Department Heads) ->
    Participating Teams (with Team Leaders & Members) -> Project Members & Task Counts.
    """
    all_tasks = list(project.tasks.select_related("department", "team", "task_owner").all())
    top_tasks = [t for t in all_tasks if t.parent_task_id is None]

    # Department heads lookup
    dept_ids = [pd.department_id for pd in project.project_departments.all()]
    head_map = {}
    if dept_ids:
        for dm in DepartmentMembership.objects.filter(
            department_id__in=dept_ids,
            is_head=True,
            status=DepartmentMembership.Status.ACTIVE,
        ).select_related("user"):
            head_map[str(dm.department_id)] = {
                "id": str(dm.user_id),
                "full_name": dm.user.full_name,
                "email": dm.user.email,
                "designation": dm.user.designation,
            }

    # Team leaders lookup
    team_ids = [pt.team_id for pt in project.project_teams.all()]
    leader_map = {}
    team_members_count_map = {}
    if team_ids:
        for tm in TeamMembership.objects.filter(
            team_id__in=team_ids,
            status=TeamMembership.Status.ACTIVE,
        ).select_related("user"):
            tid_str = str(tm.team_id)
            team_members_count_map[tid_str] = team_members_count_map.get(tid_str, 0) + 1
            if tm.is_leader:
                leader_map[tid_str] = {
                    "id": str(tm.user_id),
                    "full_name": tm.user.full_name,
                    "email": tm.user.email,
                    "designation": tm.user.designation,
                }

    # Group participating teams by department_id
    teams_by_dept: dict[str, list] = {}
    for pt in project.project_teams.select_related("team", "team__department").all():
        tid_str = str(pt.team_id)
        did_str = str(pt.team.department_id)
        t_tasks = [t for t in top_tasks if t.team_id == pt.team_id]
        t_done = sum(1 for t in t_tasks if t.status == "Completed")
        t_prog = round(sum(float(t.progress) for t in t_tasks) / len(t_tasks), 1) if t_tasks else 0.0
        teams_by_dept.setdefault(did_str, []).append(
            {
                "id": str(pt.id),
                "team_id": tid_str,
                "name": pt.team.name,
                "code": pt.team.code,
                "department_id": did_str,
                "department_name": pt.team.department.name,
                "team_leader": leader_map.get(tid_str),
                "active_team_members_count": team_members_count_map.get(tid_str, 0),
                "tasks_count": len(t_tasks),
                "completed_tasks_count": t_done,
                "progress": t_prog,
                "created_at": pt.created_at,
            }
        )

    participating_departments = []
    for pd in project.project_departments.select_related("department").all():
        did_str = str(pd.department_id)
        d_tasks = [t for t in top_tasks if t.department_id == pd.department_id]
        d_done = sum(1 for t in d_tasks if t.status == "Completed")
        d_prog = round(sum(float(t.progress) for t in d_tasks) / len(d_tasks), 1) if d_tasks else 0.0
        participating_departments.append(
            {
                "id": str(pd.id),
                "department_id": did_str,
                "name": pd.department.name,
                "code": pd.department.code,
                "status": pd.department.status,
                "department_head": head_map.get(did_str),
                "participating_teams": teams_by_dept.get(did_str, []),
                "tasks_count": len(d_tasks),
                "completed_tasks_count": d_done,
                "progress": d_prog,
                "created_at": pd.created_at,
            }
        )

    members_list = []
    for pm in project.members.select_related("user").all():
        owned_count = sum(1 for t in all_tasks if t.task_owner_id == pm.user_id)
        members_list.append(
            {
                "id": str(pm.id),
                "user": str(pm.user_id),
                "user_full_name": pm.user.full_name,
                "user_email": pm.user.email,
                "user_designation": pm.user.designation,
                "role_in_project": pm.role_in_project,
                "status": pm.status,
                "owned_tasks_count": owned_count,
                "joined_at": pm.joined_at,
                "left_at": pm.left_at,
            }
        )

    return {
        "project_id": str(project.id),
        "project_code": project.code,
        "project_name": project.name,
        "status": project.status,
        "priority": project.priority,
        "company": {
            "id": str(project.company_id),
            "name": project.company.name,
            "code": project.company.code,
        },
        "project_manager": (
            {
                "id": str(project.project_manager_id),
                "full_name": project.project_manager.full_name,
                "email": project.project_manager.email,
                "designation": project.project_manager.designation,
            }
            if project.project_manager_id
            else None
        ),
        "participating_departments": participating_departments,
        "members": members_list,
    }


@transaction.atomic
def close_project_service(*, project: Project, closure_remarks: str) -> Project:
    """
    Atomically closes a project after verifying closure remarks and task completion (SRS §6.6 & DB Design §7).
    """
    remarks = (closure_remarks or "").strip()
    if not remarks:
        raise ValidationError("Closure remarks are required before closing a project (SRS §6.6).")

    incomplete = project.tasks.exclude(status__in=["Completed", "Cancelled"]).count()
    if incomplete > 0:
        raise ValidationError(f"Cannot close project: {incomplete} tasks are still incomplete.")

    project.status = Project.Status.COMPLETED
    project.closure_remarks = remarks
    project.closed_at = timezone.now()
    project.progress = 100.00
    project.save(update_fields=["status", "closure_remarks", "closed_at", "progress"])
    return project


@transaction.atomic
def archive_project_service(*, project: Project) -> Project:
    """
    Archives a project while preserving all historical tasks, logs, and reviews (SRS §6.2 & §20.3).
    """
    project.status = Project.Status.ARCHIVED
    project.save(update_fields=["status"])
    return project


@transaction.atomic
def delete_project_service(*, project: Project, force: bool = True) -> None:
    """
    Deletes a project cleanly inside an atomic transaction.
    If the project has tasks and force=False, raises ValidationError advising archival.
    When force=True (default for DELETE /api/projects/{id}/), cleans up protected child task records first.
    """
    if project.tasks.exists():
        if not force:
            raise ValidationError(
                "Project has historical tasks. Pass ?force=true to delete or use the archive endpoint to preserve history."
            )
        from apps.reviews.models import TaskReview
        from apps.risks.models import Blocker
        from apps.tracking.models import ProgressUpdate, TimeEntry

        task_ids = list(project.tasks.values_list("id", flat=True))
        ProgressUpdate.objects.filter(task_id__in=task_ids).delete()
        TimeEntry.objects.filter(task_id__in=task_ids).delete()
        TaskReview.objects.filter(task_id__in=task_ids).delete()
        Blocker.objects.filter(task_id__in=task_ids).delete()
        project.tasks.filter(parent_task__isnull=False).delete()
        project.tasks.all().delete()

    project.delete()


@transaction.atomic
def recalculate_project_progress_service(*, project: Project) -> Project:
    """
    Recalculates overall project completion percentage from top-level non-cancelled tasks (SRS §6.5 & §22 #13).
    """
    top_tasks = project.tasks.filter(parent_task__isnull=True).exclude(status="Cancelled")
    total = top_tasks.count()
    if total > 0:
        avg_progress = sum(float(t.progress) for t in top_tasks) / total
        project.progress = round(avg_progress, 2)
        project.save(update_fields=["progress"])
    return project
