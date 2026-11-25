from datetime import date
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
            ProjectMember.objects.update_or_create(
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
    auto_add_team_members: bool = False,
) -> Project:
    """
    SRS §2.4, §5.2 & §6.3 Step 6 & DB Design §4.3:
    Maps participating teams to a project, ensures each team belongs to a participating
    company/department, coordinates Team Leaders in ProjectMember, and optionally
    onboards all active team members into ProjectMember participation.
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
    newly_added_team_ids = unique_team_ids - existing_team_ids
    for t_id in newly_added_team_ids:
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
            was_inactive = not existing_pm or existing_pm.status != ProjectMember.Status.ACTIVE
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
            if was_inactive or str(lm.team_id) in newly_added_team_ids:
                Notification.objects.create(
                    user=lm.user,
                    notification_type="PROJECT_TEAM_ASSIGNED",
                    title=f"Team Assigned to Project: {project.code}",
                    message=f"Your team '{lm.team.name}' has been assigned to project '{project.name}'.",
                    related_project=project,
                )

    # Optionally enroll all active members of mapped teams into ProjectMember participation
    if auto_add_team_members and unique_team_ids:
        sync_project_team_members_service(project=project, team_ids=list(unique_team_ids))

    return project


@transaction.atomic
def sync_project_team_members_service(
    *,
    project: Project,
    team_id=None,
    team_ids: list | None = None,
) -> dict:
    """
    SRS §5.2, §6.3 Step 6 & DB Design §4.3:
    Enrolls all active members (TeamMembership.status == 'Active') of the project's
    participating teams (or a specific participating team) into ProjectMember(status='Active').
    Reactivates historical members while preserving their role or assigning a default team role.
    """
    mapped_team_ids = set(
        str(tid)
        for tid in ProjectTeam.objects.filter(project=project).values_list("team_id", flat=True)
    )
    if team_id:
        target_ids = {str(team_id)}
        if not target_ids.issubset(mapped_team_ids):
            raise ValidationError("Selected team is not mapped as a participating team on this project.")
    elif team_ids:
        target_ids = {str(tid) for tid in team_ids if tid}
    else:
        target_ids = mapped_team_ids

    if not target_ids:
        return {"synced_count": 0, "newly_added_count": 0, "reactivated_count": 0, "team_ids": []}

    memberships = list(
        TeamMembership.objects.filter(
            team_id__in=target_ids,
            status=TeamMembership.Status.ACTIVE,
        ).select_related("user", "team")
    )

    newly_added = 0
    reactivated = 0
    for tm in memberships:
        default_role = (
            "Project Manager"
            if tm.user_id == project.project_manager_id
            else (
                f"Team Leader ({tm.team.name})"
                if tm.is_leader
                else f"Team Member ({tm.team.name})"
            )
        )
        existing = ProjectMember.objects.filter(project=project, user=tm.user).first()
        if existing:
            if existing.status != ProjectMember.Status.ACTIVE:
                existing.status = ProjectMember.Status.ACTIVE
                existing.left_at = None
                if not existing.role_in_project:
                    existing.role_in_project = default_role
                existing.save(update_fields=["status", "left_at", "role_in_project"])
                reactivated += 1
                Notification.objects.create(
                    user=tm.user,
                    notification_type="PROJECT_MEMBER_ASSIGNED",
                    title=f"Reactivated on Project: {project.code}",
                    message=f"You have been reactivated on project '{project.name}' via team '{tm.team.name}'.",
                    related_project=project,
                )
        else:
            ProjectMember.objects.create(
                project=project,
                user=tm.user,
                role_in_project=default_role,
                status=ProjectMember.Status.ACTIVE,
            )
            newly_added += 1
            Notification.objects.create(
                user=tm.user,
                notification_type="PROJECT_MEMBER_ASSIGNED",
                title=f"Added to Project Team: {project.code}",
                message=f"You have been added to project '{project.name}' as {default_role}.",
                related_project=project,
            )

    return {
        "synced_count": len(memberships),
        "newly_added_count": newly_added,
        "reactivated_count": reactivated,
        "team_ids": list(target_ids),
    }


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
            was_inactive = existing.status != ProjectMember.Status.ACTIVE
            keep_pm_role = (
                str(project.project_manager_id) == uid_str
                and existing.role_in_project == "Project Manager"
            )
            new_role = "Project Manager" if keep_pm_role else (role or existing.role_in_project)
            existing.role_in_project = new_role
            existing.status = ProjectMember.Status.ACTIVE
            existing.left_at = None
            existing.save(update_fields=["role_in_project", "status", "left_at"])
            if was_inactive:
                Notification.objects.create(
                    user_id=uid_str,
                    notification_type="PROJECT_MEMBER_ASSIGNED",
                    title=f"Project Participation Reactivated: {project.code}",
                    message=f"Your participation in project '{project.name}' ({project.code}) is now Active as {new_role}.",
                    related_project=project,
                )
        else:
            ProjectMember.objects.create(
                project=project,
                user_id=uid_str,
                role_in_project=role,
                status=ProjectMember.Status.ACTIVE,
            )
            Notification.objects.create(
                user_id=uid_str,
                notification_type="PROJECT_MEMBER_ASSIGNED",
                title=f"Assigned to Project: {project.code}",
                message=f"You have been assigned to project '{project.name}' ({project.code}) as {role}.",
                related_project=project,
            )

    return project


@transaction.atomic
def update_project_member_participation_service(
    *,
    member: ProjectMember,
    role_in_project: str | None = None,
    status_val: str | None = None,
) -> ProjectMember:
    """
    SRS §6.3 Step 6 & DB Design §1, §4.3:
    Updates a project member's role_in_project or participation status ('Active', 'Removed', 'Inactive')
    while protecting the accountable Project Manager and preserving historical timestamps.
    """
    project = member.project
    update_fields = []

    if status_val and status_val != member.status:
        if status_val not in (
            ProjectMember.Status.ACTIVE,
            ProjectMember.Status.REMOVED,
            ProjectMember.Status.INACTIVE,
        ):
            raise ValidationError(f"Invalid project member status '{status_val}'.")

        if (
            status_val in (ProjectMember.Status.REMOVED, ProjectMember.Status.INACTIVE)
            and project.project_manager_id == member.user_id
            and project.status == Project.Status.ACTIVE
        ):
            raise ValidationError(
                "Cannot remove or deactivate the accountable Project Manager of an Active project. Reassign the Project Manager first (SRS §1.4 #2 & DB Design §4.3)."
            )

        member.status = status_val
        update_fields.append("status")
        if status_val == ProjectMember.Status.ACTIVE:
            member.left_at = None
        else:
            member.left_at = timezone.now()
        update_fields.append("left_at")

    if role_in_project is not None:
        cleaned_role = role_in_project.strip() or "Team Member"
        if (
            project.project_manager_id == member.user_id
            and project.status == Project.Status.ACTIVE
            and cleaned_role != "Project Manager"
        ):
            # Keep PM accountable role clear
            cleaned_role = f"Project Manager ({cleaned_role})" if "Project Manager" not in cleaned_role else cleaned_role
        member.role_in_project = cleaned_role
        update_fields.append("role_in_project")

    if update_fields:
        member.save(update_fields=update_fields)

    return member


def get_project_assignment_hierarchy(*, project: Project) -> dict:
    """
    Returns the complete SRS §6.3 Project Assignment Hierarchy & Participation Matrix:
    Company -> Project Manager -> Participating Departments (with Department Heads) ->
    Participating Teams (with Team Leaders & Team Member Roster) -> Project Members, Workload & Multi-Project Participation.
    """
    from apps.tasks.models import TaskAssignment

    today = date.today()
    all_tasks = list(project.tasks.select_related("department", "team", "task_owner").all())
    top_tasks = [t for t in all_tasks if t.parent_task_id is None]
    task_ids = [t.id for t in all_tasks]

    # Active task assignments in this project by user_id
    assigned_task_ids_by_user: dict[str, set] = {}
    if task_ids:
        for ta in TaskAssignment.objects.filter(
            task_id__in=task_ids,
            status="Active",
            user_id__isnull=False,
        ).values("user_id", "task_id"):
            uid_s = str(ta["user_id"])
            assigned_task_ids_by_user.setdefault(uid_s, set()).add(ta["task_id"])

    for t in all_tasks:
        if t.task_owner_id:
            uid_s = str(t.task_owner_id)
            assigned_task_ids_by_user.setdefault(uid_s, set()).add(t.id)

    task_by_id = {t.id: t for t in all_tasks}

    # Current project members map by user_id
    project_members_qs = list(project.members.select_related("user").all())
    pm_by_user_id = {str(pm.user_id): pm for pm in project_members_qs}

    # Department heads and user department memberships within the project's company
    company_dept_memberships = list(
        DepartmentMembership.objects.filter(
            department__company_id=project.company_id,
            status=DepartmentMembership.Status.ACTIVE,
        ).select_related("user", "department")
    )
    head_map = {}
    user_dept_map: dict[str, dict] = {}
    for dm in company_dept_memberships:
        uid_s = str(dm.user_id)
        user_dept_map[uid_s] = {
            "department_id": str(dm.department_id),
            "department_name": dm.department.name,
            "department_code": dm.department.code,
            "is_head": dm.is_head,
        }
        if dm.is_head:
            head_map[str(dm.department_id)] = {
                "id": uid_s,
                "full_name": dm.user.full_name,
                "email": dm.user.email,
                "designation": dm.user.designation,
            }

    # Team memberships across the company
    company_team_memberships = list(
        TeamMembership.objects.filter(
            team__department__company_id=project.company_id,
            status=TeamMembership.Status.ACTIVE,
        ).select_related("user", "team", "team__department")
    )
    leader_map = {}
    team_members_count_map = {}
    team_roster_map: dict[str, list] = {}
    user_teams_map: dict[str, list] = {}

    for tm in company_team_memberships:
        tid_str = str(tm.team_id)
        uid_str = str(tm.user_id)
        team_members_count_map[tid_str] = team_members_count_map.get(tid_str, 0) + 1
        user_teams_map.setdefault(uid_str, []).append(
            {
                "team_id": tid_str,
                "team_name": tm.team.name,
                "team_code": tm.team.code,
                "department_id": str(tm.team.department_id),
                "department_name": tm.team.department.name,
                "is_leader": tm.is_leader,
            }
        )
        pm_rec = pm_by_user_id.get(uid_str)
        user_t_ids = assigned_task_ids_by_user.get(uid_str, set())
        team_roster_map.setdefault(tid_str, []).append(
            {
                "user_id": uid_str,
                "full_name": tm.user.full_name,
                "email": tm.user.email,
                "designation": tm.user.designation,
                "is_leader": tm.is_leader,
                "is_project_member": bool(pm_rec and pm_rec.status == ProjectMember.Status.ACTIVE),
                "project_member_id": str(pm_rec.id) if pm_rec else None,
                "role_in_project": pm_rec.role_in_project if pm_rec else None,
                "participation_status": pm_rec.status if pm_rec else "Not Assigned",
                "assigned_tasks_count": len(user_t_ids),
            }
        )
        if tm.is_leader:
            leader_map[tid_str] = {
                "id": uid_str,
                "full_name": tm.user.full_name,
                "email": tm.user.email,
                "designation": tm.user.designation,
            }

    # Multi-project participation lookup for all members of this project (SRS §1.3 & §27.1)
    member_user_ids = [pm.user_id for pm in project_members_qs]
    other_projects_by_user: dict[str, list] = {}
    if member_user_ids:
        other_pms = (
            ProjectMember.objects.filter(
                user_id__in=member_user_ids,
                status=ProjectMember.Status.ACTIVE,
            )
            .exclude(project=project)
            .select_related("project")
        )
        for opm in other_pms:
            other_projects_by_user.setdefault(str(opm.user_id), []).append(
                {
                    "project_id": str(opm.project_id),
                    "code": opm.project.code,
                    "name": opm.project.name,
                    "status": opm.project.status,
                    "role_in_project": opm.role_in_project,
                }
            )

    # Group participating teams by department_id
    teams_by_dept: dict[str, list] = {}
    participating_teams_flat: list = []
    for pt in project.project_teams.select_related("team", "team__department").all():
        tid_str = str(pt.team_id)
        did_str = str(pt.team.department_id)
        t_tasks = [t for t in top_tasks if t.team_id == pt.team_id]
        t_done = sum(1 for t in t_tasks if t.status == "Completed")
        t_in_prog = sum(1 for t in t_tasks if t.status in ("In Progress", "In Review", "Approved"))
        t_overdue = sum(
            1
            for t in t_tasks
            if t.due_date and t.due_date < today and t.status not in ("Completed", "Cancelled")
        )
        t_prog = round(sum(float(t.progress) for t in t_tasks) / len(t_tasks), 1) if t_tasks else 0.0
        roster = team_roster_map.get(tid_str, [])
        participating_count = sum(1 for r in roster if r["is_project_member"])
        team_entry = {
            "id": str(pt.id),
            "team_id": tid_str,
            "name": pt.team.name,
            "code": pt.team.code,
            "department_id": did_str,
            "department_name": pt.team.department.name,
            "team_leader": leader_map.get(tid_str),
            "active_team_members_count": team_members_count_map.get(tid_str, 0),
            "participating_members_count": participating_count,
            "team_members": roster,
            "tasks_count": len(t_tasks),
            "completed_tasks_count": t_done,
            "in_progress_tasks_count": t_in_prog,
            "overdue_tasks_count": t_overdue,
            "progress": t_prog,
            "created_at": pt.created_at,
        }
        teams_by_dept.setdefault(did_str, []).append(team_entry)
        participating_teams_flat.append(team_entry)

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
    for pm in project_members_qs:
        uid_s = str(pm.user_id)
        owned_tasks = [t for t in all_tasks if t.task_owner_id == pm.user_id]
        user_task_ids = assigned_task_ids_by_user.get(uid_s, set())
        user_tasks = [task_by_id[tid] for tid in user_task_ids if tid in task_by_id]
        completed_cnt = sum(1 for t in user_tasks if t.status == "Completed")
        in_prog_cnt = sum(1 for t in user_tasks if t.status in ("In Progress", "In Review", "Approved"))
        overdue_cnt = sum(
            1
            for t in user_tasks
            if t.due_date and t.due_date < today and t.status not in ("Completed", "Cancelled")
        )
        effort_hrs = round(
            sum(float(t.estimated_effort or 0) for t in user_tasks), 1
        )
        dept_info = user_dept_map.get(uid_s)
        other_projs = other_projects_by_user.get(uid_s, [])

        members_list.append(
            {
                "id": str(pm.id),
                "user": uid_s,
                "user_full_name": pm.user.full_name,
                "user_email": pm.user.email,
                "user_employee_id": pm.user.employee_id,
                "user_designation": pm.user.designation,
                "department_id": dept_info["department_id"] if dept_info else None,
                "department_name": dept_info["department_name"] if dept_info else None,
                "teams": user_teams_map.get(uid_s, []),
                "is_project_manager": pm.user_id == project.project_manager_id,
                "role_in_project": pm.role_in_project,
                "status": pm.status,
                "owned_tasks_count": len(owned_tasks),
                "assigned_tasks_count": len(user_tasks),
                "completed_tasks_count": completed_cnt,
                "in_progress_tasks_count": in_prog_cnt,
                "overdue_tasks_count": overdue_cnt,
                "estimated_effort_hours": effort_hrs,
                "other_active_projects_count": len(other_projs),
                "other_active_projects": other_projs,
                "joined_at": pm.joined_at,
                "left_at": pm.left_at,
            }
        )

    active_members_count = sum(1 for m in members_list if m["status"] == ProjectMember.Status.ACTIVE)
    historical_members_count = len(members_list) - active_members_count

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
        "summary": {
            "participating_departments_count": len(participating_departments),
            "participating_teams_count": len(participating_teams_flat),
            "active_members_count": active_members_count,
            "historical_members_count": historical_members_count,
            "total_tasks_count": len(top_tasks),
        },
        "participating_departments": participating_departments,
        "participating_teams": participating_teams_flat,
        "members": members_list,
    }


def get_user_project_participations(*, user_id, company_id=None) -> list[dict]:
    """
    SRS §1.3, §3.3, §4.3, §19 & §27.1:
    Returns all projects an employee participates in (with active or historical status),
    their role_in_project, participating teams, and task workload on each project.
    """
    qs = ProjectMember.objects.filter(user_id=user_id).select_related(
        "project", "project__company", "project__project_manager"
    )
    if company_id:
        qs = qs.filter(project__company_id=company_id)

    results = []
    for pm in qs.order_by("-joined_at"):
        proj = pm.project
        owned_tasks_count = proj.tasks.filter(task_owner_id=user_id).count()
        completed_tasks_count = proj.tasks.filter(
            task_owner_id=user_id, status="Completed"
        ).count()
        results.append(
            {
                "membership_id": str(pm.id),
                "project_id": str(proj.id),
                "project_code": proj.code,
                "project_name": proj.name,
                "project_status": proj.status,
                "project_priority": proj.priority,
                "project_progress": float(proj.progress),
                "company_id": str(proj.company_id),
                "company_name": proj.company.name,
                "company_code": proj.company.code,
                "project_manager_name": (
                    proj.project_manager.full_name if proj.project_manager else None
                ),
                "role_in_project": pm.role_in_project,
                "participation_status": pm.status,
                "owned_tasks_count": owned_tasks_count,
                "completed_tasks_count": completed_tasks_count,
                "joined_at": pm.joined_at,
                "left_at": pm.left_at,
            }
        )
    return results


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
    from apps.risks.models import Blocker, Issue, Risk

    if project.tasks.exists():
        if not force:
            raise ValidationError(
                "Project has historical tasks. Pass ?force=true to delete or use the archive endpoint to preserve history."
            )
        from apps.reviews.models import TaskReview
        from apps.tracking.models import ProgressUpdate, TimeEntry

        task_ids = list(project.tasks.values_list("id", flat=True))
        ProgressUpdate.objects.filter(task_id__in=task_ids).delete()
        TimeEntry.objects.filter(task_id__in=task_ids).delete()
        TaskReview.objects.filter(task_id__in=task_ids).delete()
        Blocker.objects.filter(task_id__in=task_ids).delete()
        Issue.objects.filter(project=project).delete()
        Risk.objects.filter(project=project).delete()
        project.tasks.filter(parent_task__isnull=False).delete()
        project.tasks.all().delete()
    else:
        Issue.objects.filter(project=project).delete()
        Risk.objects.filter(project=project).delete()

    project.delete()


@transaction.atomic
def recalculate_milestone_progress_service(*, milestone) -> object:
    """
    SRS §6.4 & DB Design §4.3:
    Recalculates a ProjectMilestone's completion progress and status from its linked
    MilestoneTask records while respecting prerequisite MilestoneDependencies and approval flags.
    """
    from decimal import Decimal
    from apps.projects.models import ProjectMilestone

    today = timezone.now().date()
    linked = [
        mt.task
        for mt in milestone.milestone_tasks.select_related("task").all()
        if mt.task and mt.task.status != "Cancelled"
    ]
    if not linked:
        return milestone

    m_avg = round(sum(float(t.progress) for t in linked) / len(linked), 2)
    new_prog = Decimal(f"{m_avg:.2f}")
    all_done = all(t.status == "Completed" for t in linked)
    has_unmet_prereqs = milestone.dependencies.exclude(
        depends_on_milestone__status=ProjectMilestone.Status.COMPLETED
    ).exists()

    update_fields = []
    if milestone.progress != new_prog:
        milestone.progress = new_prog
        update_fields.append("progress")

    new_status = milestone.status
    if all_done and not has_unmet_prereqs and not milestone.approval_required:
        new_status = ProjectMilestone.Status.COMPLETED
    elif milestone.status != ProjectMilestone.Status.COMPLETED:
        if milestone.target_date and milestone.target_date < today:
            new_status = ProjectMilestone.Status.DELAYED
        elif new_prog > Decimal("0.00"):
            new_status = ProjectMilestone.Status.IN_PROGRESS
        else:
            new_status = ProjectMilestone.Status.PENDING
    elif not all_done:
        # A linked task was reopened after milestone completion
        new_status = (
            ProjectMilestone.Status.DELAYED
            if (milestone.target_date and milestone.target_date < today)
            else ProjectMilestone.Status.IN_PROGRESS
        )

    if new_status != milestone.status:
        milestone.status = new_status
        update_fields.append("status")

    if update_fields:
        milestone.save(update_fields=update_fields)
    return milestone


@transaction.atomic
def complete_milestone_service(*, milestone, actor=None):
    """
    SRS §6.4 & §15:
    Marks a ProjectMilestone Completed after validating:
    1. All prerequisite MilestoneDependencies are Completed.
    2. All linked non-cancelled MilestoneTasks are Completed.
    Notifies the project manager and milestone owner.
    """
    from decimal import Decimal
    from apps.projects.models import ProjectMilestone

    unmet_prereqs = list(
        milestone.dependencies.select_related("depends_on_milestone")
        .exclude(depends_on_milestone__status=ProjectMilestone.Status.COMPLETED)
    )
    if unmet_prereqs:
        names = ", ".join(d.depends_on_milestone.title for d in unmet_prereqs)
        raise ValidationError(
            f"Cannot complete milestone '{milestone.title}': prerequisite milestone(s) not yet completed ({names})."
        )

    incomplete_tasks = list(
        milestone.milestone_tasks.select_related("task")
        .exclude(task__status__in=["Completed", "Cancelled"])
    )
    if incomplete_tasks:
        names = ", ".join(mt.task.title for mt in incomplete_tasks[:3])
        raise ValidationError(
            f"Cannot complete milestone '{milestone.title}': {len(incomplete_tasks)} linked task(s) are still incomplete ({names})."
        )

    milestone.status = ProjectMilestone.Status.COMPLETED
    milestone.progress = Decimal("100.00")
    milestone.save(update_fields=["status", "progress"])

    notify_users = set()
    if milestone.project.project_manager_id:
        notify_users.add(milestone.project.project_manager_id)
    if milestone.owner_id:
        notify_users.add(milestone.owner_id)
    if actor and getattr(actor, "id", None) in notify_users and len(notify_users) > 1:
        notify_users.discard(actor.id)

    for uid in notify_users:
        Notification.objects.create(
            user_id=uid,
            notification_type="MILESTONE_COMPLETED",
            title=f"Milestone Completed: {milestone.title}",
            message=f"Milestone '{milestone.title}' in project '{milestone.project.code}' has been marked Completed.",
            related_project=milestone.project,
        )
    return milestone


@transaction.atomic
def recalculate_project_progress_service(*, project: Project) -> Project:
    """
    Recalculates overall project completion percentage from top-level non-cancelled tasks
    (SRS §6.5 & §22 #13), and synchronizes linked phase & milestone progress and status rollups.
    """
    from decimal import Decimal
    from apps.projects.models import ProjectPhase

    top_tasks = list(project.tasks.filter(parent_task__isnull=True).exclude(status="Cancelled"))
    total = len(top_tasks)
    if total > 0:
        avg_progress = sum(float(t.progress) for t in top_tasks) / total
        project.progress = Decimal(f"{round(avg_progress, 2):.2f}")
    else:
        project.progress = Decimal("0.00")
    project.save(update_fields=["progress"])

    # Roll up progress & status to ProjectPhases that have linked tasks
    for phase in project.phases.all():
        phase_tasks = [t for t in top_tasks if t.phase_id == phase.id]
        if phase_tasks:
            p_avg = round(sum(float(t.progress) for t in phase_tasks) / len(phase_tasks), 2)
            new_prog = Decimal(f"{p_avg:.2f}")
            all_phase_done = all(t.status == "Completed" for t in phase_tasks)
            if all_phase_done:
                new_status = ProjectPhase.Status.COMPLETED
            elif new_prog > Decimal("0.00") or any(
                t.status in ("In Progress", "In Review", "Approved", "Blocked", "Changes Requested")
                for t in phase_tasks
            ):
                new_status = ProjectPhase.Status.IN_PROGRESS
            else:
                new_status = ProjectPhase.Status.NOT_STARTED

            upd = []
            if phase.progress != new_prog:
                phase.progress = new_prog
                upd.append("progress")
            if phase.status != new_status:
                phase.status = new_status
                upd.append("status")
            if upd:
                phase.save(update_fields=upd)

    # Roll up progress & status to ProjectMilestones that have linked MilestoneTasks
    for ms in project.milestones.prefetch_related("milestone_tasks__task", "dependencies__depends_on_milestone").all():
        recalculate_milestone_progress_service(milestone=ms)

    return project
