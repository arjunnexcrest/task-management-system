"""
organization/selectors/__init__.py

Query-optimized selectors for Organization Hierarchy APIs (SRS §1.3, §5.3, §5.4 & DB Design §4.2, §6, §7):
- build_organization_hierarchy(): returns both:
    1. Structural hierarchy: Company -> Department (with Head) -> Team (with Leader) -> Employees
    2. Reporting hierarchy: CEO -> Company In-Charge -> Project Manager -> Department Head -> Team Leader -> Employee
    with flat nodes, edges, workload/project summary counts, and search-match highlighting (preserving ancestors).
- build_hierarchy_employee_detail(): returns an employee's authorized profile details,
    direct reporting relationships, upward chain, departments, teams, assigned projects,
    and assigned tasks for the Organization Chart side panel (SRS §5.4).

All queries use bulk select_related / prefetch_related and in-memory maps (no N+1 queries)
and strictly enforce backend company & project authorization.
"""
from datetime import date
from typing import Any
import uuid

from django.db.models import Q

from apps.accounts.models import User
from apps.common.permissions import get_user_company_ids, is_ceo
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

ROLE_HIERARCHY_RANK = {
    SystemRole.CEO.upper(): 0,
    SystemRole.COMPANY_IN_CHARGE.upper(): 1,
    SystemRole.PROJECT_MANAGER.upper(): 2,
    SystemRole.DEPARTMENT_HEAD.upper(): 3,
    SystemRole.TEAM_LEADER.upper(): 4,
    SystemRole.EMPLOYEE.upper(): 5,
}


def _role_rank(role: str | None) -> int:
    if not role:
        return 99
    return ROLE_HIERARCHY_RANK.get(str(role).upper(), 50)


def _resolve_authorized_company_ids(request_user, requested_company_id=None) -> tuple[set, bool]:
    """
    Returns (scoped_company_ids, is_authorized).
    - CEO sees all active companies (or the single requested company).
    - Non-CEO sees only active companies where they hold an active CompanyMembership;
      requesting an unauthorized company returns (set(), False).
    """
    ceo = is_ceo(request_user)
    if ceo:
        qs = Company.objects.filter(is_active=True)
        if requested_company_id:
            qs = qs.filter(id=requested_company_id)
        return set(qs.values_list("id", flat=True)), True

    allowed_ids = get_user_company_ids(request_user)
    if not allowed_ids:
        return set(), False
    if requested_company_id:
        allowed_strs = {str(cid) for cid in allowed_ids}
        if str(requested_company_id) not in allowed_strs:
            return set(), False
        return {cid for cid in allowed_ids if str(cid) == str(requested_company_id)}, True
    return allowed_ids, True


def _get_viewer_authorized_project_ids(viewer, scoped_company_ids: set, target_user_id=None):
    """
    Determines which projects within `scoped_company_ids` the viewer is permitted to inspect
    for an employee in the Org Chart (SRS §2.8, §5.4 & DB Design §1):
    - CEO: all projects in scoped_company_ids.
    - Company In-Charge: all projects in companies where viewer is CompanyInCharge.
    - Self (viewer viewing own node): all projects in scoped_company_ids.
    - Other roles (PM, DeptHead, TeamLeader, Employee): projects in scoped_company_ids
      where viewer is Project Manager, an active ProjectMember, or heads a participating
      department / leads a participating team.
    Returns None when viewer has unrestricted access across `scoped_company_ids`,
    or a set of authorized Project UUIDs.
    """
    if is_ceo(viewer):
        return None
    if target_user_id is not None and str(viewer.id) == str(target_user_id):
        return None

    incharge_company_ids = set(
        CompanyMembership.objects.filter(
            user=viewer,
            company_id__in=scoped_company_ids,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
            role__iexact=SystemRole.COMPANY_IN_CHARGE,
        ).values_list("company_id", flat=True)
    )
    if incharge_company_ids == scoped_company_ids and scoped_company_ids:
        return None

    authorized_project_ids = set()
    if incharge_company_ids:
        authorized_project_ids.update(
            Project.objects.filter(company_id__in=incharge_company_ids).values_list("id", flat=True)
        )

    # Projects viewer manages or is an active member of
    authorized_project_ids.update(
        Project.objects.filter(
            company_id__in=scoped_company_ids,
            project_manager=viewer,
        ).values_list("id", flat=True)
    )
    authorized_project_ids.update(
        ProjectMember.objects.filter(
            project__company_id__in=scoped_company_ids,
            user=viewer,
            status=ProjectMember.Status.ACTIVE,
        ).values_list("project_id", flat=True)
    )

    # Projects mapped to departments viewer heads or teams viewer leads
    headed_dept_ids = set(
        DepartmentMembership.objects.filter(
            department__company_id__in=scoped_company_ids,
            user=viewer,
            status=DepartmentMembership.Status.ACTIVE,
            is_head=True,
        ).values_list("department_id", flat=True)
    )
    if headed_dept_ids:
        authorized_project_ids.update(
            Project.objects.filter(
                company_id__in=scoped_company_ids,
                project_departments__department_id__in=headed_dept_ids,
            ).values_list("id", flat=True)
        )

    led_team_ids = set(
        TeamMembership.objects.filter(
            team__department__company_id__in=scoped_company_ids,
            user=viewer,
            status=TeamMembership.Status.ACTIVE,
            is_leader=True,
        ).values_list("team_id", flat=True)
    )
    if led_team_ids:
        authorized_project_ids.update(
            Project.objects.filter(
                company_id__in=scoped_company_ids,
                project_teams__team_id__in=led_team_ids,
            ).values_list("id", flat=True)
        )

    return authorized_project_ids


def _bulk_employee_workload_counts(user_ids: set, scoped_company_ids: set, authorized_project_ids=None) -> dict[str, dict]:
    """
    Computes per-employee project and task summary counts in bulk (zero N+1 queries).
    """
    if not user_ids or not scoped_company_ids:
        return {}

    today = date.today()
    project_ids_by_user: dict[str, set] = {str(uid): set() for uid in user_ids}
    active_tasks_by_user: dict[str, set] = {str(uid): set() for uid in user_ids}
    overdue_tasks_by_user: dict[str, set] = {str(uid): set() for uid in user_ids}

    # 1. Projects via ProjectMember
    pm_qs = ProjectMember.objects.filter(
        project__company_id__in=scoped_company_ids,
        user_id__in=user_ids,
        status=ProjectMember.Status.ACTIVE,
    ).exclude(project__status__in=[Project.Status.CANCELLED, Project.Status.ARCHIVED])
    if authorized_project_ids is not None:
        pm_qs = pm_qs.filter(project_id__in=authorized_project_ids)
    for uid, pid in pm_qs.values_list("user_id", "project_id"):
        project_ids_by_user[str(uid)].add(pid)

    # 2. Projects via project_manager FK
    managed_qs = Project.objects.filter(
        company_id__in=scoped_company_ids,
        project_manager_id__in=user_ids,
    ).exclude(status__in=[Project.Status.CANCELLED, Project.Status.ARCHIVED])
    if authorized_project_ids is not None:
        managed_qs = managed_qs.filter(id__in=authorized_project_ids)
    for uid, pid in managed_qs.values_list("project_manager_id", "id"):
        if uid:
            project_ids_by_user[str(uid)].add(pid)

    # 3. Tasks owned by user (non-completed, non-cancelled)
    closed_task_statuses = [Task.Status.COMPLETED, Task.Status.CANCELLED]
    owned_tasks_qs = Task.objects.filter(
        project__company_id__in=scoped_company_ids,
        task_owner_id__in=user_ids,
    ).exclude(status__in=closed_task_statuses)
    if authorized_project_ids is not None:
        owned_tasks_qs = owned_tasks_qs.filter(project_id__in=authorized_project_ids)
    for uid, tid, due in owned_tasks_qs.values_list("task_owner_id", "id", "due_date"):
        uid_s = str(uid)
        active_tasks_by_user[uid_s].add(tid)
        if due and due < today:
            overdue_tasks_by_user[uid_s].add(tid)

    # 4. Tasks actively assigned to user
    assigned_qs = TaskAssignment.objects.filter(
        task__project__company_id__in=scoped_company_ids,
        user_id__in=user_ids,
        assignment_type=TaskAssignment.AssignmentType.USER,
        status__in=[TaskAssignment.Status.ACTIVE, TaskAssignment.Status.ACKNOWLEDGED],
    ).exclude(task__status__in=closed_task_statuses)
    if authorized_project_ids is not None:
        assigned_qs = assigned_qs.filter(task__project_id__in=authorized_project_ids)
    for uid, tid, due in assigned_qs.values_list("user_id", "task_id", "task__due_date"):
        uid_s = str(uid)
        active_tasks_by_user[uid_s].add(tid)
        if due and due < today:
            overdue_tasks_by_user[uid_s].add(tid)

    return {
        uid_s: {
            "projects_count": len(project_ids_by_user[uid_s]),
            "active_tasks_count": len(active_tasks_by_user[uid_s]),
            "overdue_tasks_count": len(overdue_tasks_by_user[uid_s]),
        }
        for uid_s in project_ids_by_user
    }


def _employee_matches_search(node: dict, search_lower: str) -> bool:
    if not search_lower:
        return False
    haystacks = [
        node.get("full_name") or "",
        node.get("email") or "",
        node.get("employee_id") or "",
        node.get("designation") or "",
        node.get("role") or "",
    ]
    return any(search_lower in h.lower() for h in haystacks)


def _build_reporting_tree(nodes_by_id: dict[str, dict], matched_ids: set[str], has_search: bool) -> list[dict]:
    """
    Builds a cycle-safe recursive tree of reporting relationships.
    If `has_search` is True, retains matched nodes plus all of their ancestor nodes
    so the org chart path from CEO/root down to every search result remains intact.
    """
    children_map: dict[str, list[str]] = {}
    root_ids: list[str] = []

    for uid, node in nodes_by_id.items():
        mgr_id = node.get("manager_id")
        if mgr_id and mgr_id in nodes_by_id and mgr_id != uid:
            children_map.setdefault(mgr_id, []).append(uid)
        else:
            root_ids.append(uid)

    def sort_key(uid: str):
        n = nodes_by_id[uid]
        return (_role_rank(n.get("role")), (n.get("full_name") or "").lower())

    root_ids.sort(key=sort_key)
    for mgr_id in children_map:
        children_map[mgr_id].sort(key=sort_key)

    # Determine which nodes to retain if search is active
    keep_ids: set[str] | None = None
    if has_search:
        keep_ids = set(matched_ids)
        for uid in list(matched_ids):
            curr = nodes_by_id.get(uid, {}).get("manager_id")
            visited_up = {uid}
            while curr and curr in nodes_by_id and curr not in visited_up:
                visited_up.add(curr)
                keep_ids.add(curr)
                curr = nodes_by_id[curr].get("manager_id")

    def build_subtree(uid: str, path_seen: set[str]) -> dict | None:
        if keep_ids is not None and uid not in keep_ids:
            return None
        node = dict(nodes_by_id[uid])
        next_seen = path_seen | {uid}
        kids = []
        for child_id in children_map.get(uid, []):
            if child_id in next_seen:
                continue
            sub = build_subtree(child_id, next_seen)
            if sub is not None:
                kids.append(sub)
        node["children"] = kids
        return node

    # Handle any isolated cycle component gracefully (never drop nodes silently)
    included_in_tree: set[str] = set()

    def mark_included(tree_node: dict):
        included_in_tree.add(tree_node["id"])
        for c in tree_node["children"]:
            mark_included(c)

    roots: list[dict] = []
    for rid in root_ids:
        sub = build_subtree(rid, set())
        if sub is not None:
            mark_included(sub)
            roots.append(sub)

    for uid in sorted(nodes_by_id.keys(), key=sort_key):
        if uid not in included_in_tree:
            sub = build_subtree(uid, set())
            if sub is not None:
                mark_included(sub)
                roots.append(sub)

    return roots


def _parse_optional_uuid(value):
    """Returns (uuid_obj_or_None, is_valid)."""
    if value in (None, ""):
        return None, True
    try:
        return uuid.UUID(str(value)), True
    except (ValueError, TypeError, AttributeError):
        return None, False


def build_organization_hierarchy(
    request_user,
    company_id=None,
    department_id=None,
    team_id=None,
    search: str = "",
    include_inactive: bool = False,
) -> tuple[dict[str, Any] | None, int]:
    """
    Main selector for GET /api/organization/hierarchy/ and GET /api/hierarchy/.
    Returns (payload, http_status_code).
    """
    parsed_company, ok_c = _parse_optional_uuid(company_id)
    parsed_dept, ok_d = _parse_optional_uuid(department_id)
    parsed_team, ok_t = _parse_optional_uuid(team_id)
    if not (ok_c and ok_d and ok_t):
        return None, 400
    company_id = parsed_company
    department_id = parsed_dept
    team_id = parsed_team

    scoped_company_ids, authorized = _resolve_authorized_company_ids(request_user, company_id)
    if not authorized:
        return None, 403

    search_clean = (search or "").strip()
    search_lower = search_clean.lower()

    companies = list(Company.objects.filter(id__in=scoped_company_ids).order_by("name"))
    company_map = {c.id: c for c in companies}

    # Departments in scope
    dept_qs = Department.objects.filter(company_id__in=scoped_company_ids).select_related("company")
    if not include_inactive:
        dept_qs = dept_qs.filter(status=Department.Status.ACTIVE)
    if department_id:
        dept_qs = dept_qs.filter(id=department_id)
    departments = list(dept_qs.order_by("name"))
    dept_ids = {d.id for d in departments}

    # Teams in scope
    team_qs = Team.objects.filter(department__company_id__in=scoped_company_ids).select_related(
        "department", "department__company"
    )
    if not include_inactive:
        team_qs = team_qs.filter(status=Team.Status.ACTIVE, department__status=Department.Status.ACTIVE)
    if department_id:
        team_qs = team_qs.filter(department_id=department_id)
    if team_id:
        team_qs = team_qs.filter(id=team_id)
    teams = list(team_qs.order_by("name"))
    team_ids = {t.id for t in teams}

    # If team_id was explicitly passed, narrow departments to that team's department
    if team_id:
        team_dept_ids = {t.department_id for t in teams}
        departments = [d for d in departments if d.id in team_dept_ids]
        dept_ids = {d.id for d in departments}

    # Company memberships in scope
    cm_qs = CompanyMembership.objects.filter(
        company_id__in=scoped_company_ids,
        is_active=True,
        status=CompanyMembership.Status.ACTIVE,
        user__is_active=True,
    ).select_related("user", "company")

    # Department memberships & team memberships in scope (bulk fetch)
    dm_all = list(
        DepartmentMembership.objects.filter(
            department__company_id__in=scoped_company_ids,
            status=DepartmentMembership.Status.ACTIVE,
            user__is_active=True,
        ).select_related("department", "user")
    )
    tm_all = list(
        TeamMembership.objects.filter(
            team__department__company_id__in=scoped_company_ids,
            status=TeamMembership.Status.ACTIVE,
            user__is_active=True,
        ).select_related("team", "team__department", "user")
    )

    # Filter memberships by department/team if requested
    filter_user_ids: set | None = None
    if department_id:
        filter_user_ids = {dm.user_id for dm in dm_all if str(dm.department_id) == str(department_id)}
    if team_id:
        t_uids = {tm.user_id for tm in tm_all if str(tm.team_id) == str(team_id)}
        filter_user_ids = t_uids if filter_user_ids is None else (filter_user_ids & t_uids)

    if filter_user_ids is not None:
        cm_qs = cm_qs.filter(user_id__in=filter_user_ids)

    memberships = list(cm_qs.order_by("company__name", "user__full_name"))
    user_ids = {m.user_id for m in memberships}

    # Bulk workload/project counts respecting viewer authorization
    authorized_project_ids = _get_viewer_authorized_project_ids(request_user, scoped_company_ids)
    workload_map = _bulk_employee_workload_counts(user_ids, scoped_company_ids, authorized_project_ids)

    # Index user departments & teams per company
    user_depts_global: dict[str, list[str]] = {}
    user_teams_global: dict[str, list[str]] = {}
    dept_members_map: dict[Any, list[DepartmentMembership]] = {}
    dept_head_map: dict[Any, DepartmentMembership] = {}
    for dm in dm_all:
        if dm.user_id not in user_ids:
            continue
        uid_s = str(dm.user_id)
        if dm.department.name not in user_depts_global.setdefault(uid_s, []):
            user_depts_global[uid_s].append(dm.department.name)
        if dm.department_id in dept_ids:
            dept_members_map.setdefault(dm.department_id, []).append(dm)
            if dm.is_head and dm.department_id not in dept_head_map:
                dept_head_map[dm.department_id] = dm

    team_members_map: dict[Any, list[TeamMembership]] = {}
    team_leader_map: dict[Any, TeamMembership] = {}
    for tm in tm_all:
        if tm.user_id not in user_ids:
            continue
        uid_s = str(tm.user_id)
        if tm.team.name not in user_teams_global.setdefault(uid_s, []):
            user_teams_global[uid_s].append(tm.team.name)
        if tm.team_id in team_ids:
            team_members_map.setdefault(tm.team_id, []).append(tm)
            if tm.is_leader and tm.team_id not in team_leader_map:
                team_leader_map[tm.team_id] = tm

    # Active reporting relationships in scope
    rel_qs = ReportingRelationship.objects.filter(
        company_id__in=scoped_company_ids,
        end_date__isnull=True,
    ).select_related("company", "employee", "manager")
    active_rels = [
        r for r in rel_qs if r.employee_id in user_ids and r.manager_id in user_ids
    ]

    direct_reports_count_map: dict[str, int] = {}
    for r in active_rels:
        mgr_s = str(r.manager_id)
        direct_reports_count_map[mgr_s] = direct_reports_count_map.get(mgr_s, 0) + 1

    # Build canonical flat employee nodes (preferring highest-ranked role when a user is in multiple companies)
    memberships_by_company: dict[Any, list[CompanyMembership]] = {}
    nodes_by_id: dict[str, dict] = {}
    for m in memberships:
        memberships_by_company.setdefault(m.company_id, []).append(m)
        uid_s = str(m.user_id)
        wl = workload_map.get(uid_s, {"projects_count": 0, "active_tasks_count": 0, "overdue_tasks_count": 0})
        candidate = {
            "id": uid_s,
            "full_name": m.user.full_name,
            "email": m.user.email,
            "employee_id": m.user.employee_id,
            "designation": m.user.designation,
            "profile_image": m.user.profile_image,
            "role": m.role,
            "company_id": str(m.company_id),
            "company_name": m.company.name,
            "company_code": m.company.code,
            "departments": user_depts_global.get(uid_s, []),
            "teams": user_teams_global.get(uid_s, []),
            "manager_id": None,
            "manager_name": None,
            "relationship_id": None,
            "direct_reports_count": direct_reports_count_map.get(uid_s, 0),
            "projects_count": wl["projects_count"],
            "active_tasks_count": wl["active_tasks_count"],
            "overdue_tasks_count": wl["overdue_tasks_count"],
            "matched": False,
        }
        if uid_s not in nodes_by_id or _role_rank(m.role) < _role_rank(nodes_by_id[uid_s]["role"]):
            nodes_by_id[uid_s] = candidate

    edges = []
    for r in active_rels:
        emp_s = str(r.employee_id)
        mgr_s = str(r.manager_id)
        if emp_s in nodes_by_id and nodes_by_id[emp_s]["manager_id"] is None:
            nodes_by_id[emp_s]["manager_id"] = mgr_s
            nodes_by_id[emp_s]["manager_name"] = r.manager.full_name
            nodes_by_id[emp_s]["relationship_id"] = str(r.id)
        edges.append(
            {
                "id": str(r.id),
                "company_id": str(r.company_id),
                "employee_id": emp_s,
                "manager_id": mgr_s,
                "start_date": r.start_date.isoformat(),
            }
        )

    matched_employee_ids: set[str] = set()
    if search_lower:
        for uid_s, node in nodes_by_id.items():
            if _employee_matches_search(node, search_lower):
                node["matched"] = True
                matched_employee_ids.add(uid_s)

    # Helper to format a company-contextual employee summary for the structural tree
    def make_member_card(cm: CompanyMembership, is_head: bool = False, is_leader: bool = False) -> dict:
        uid_s = str(cm.user_id)
        base = nodes_by_id[uid_s]
        return {
            **base,
            "role": cm.role,
            "company_id": str(cm.company_id),
            "company_name": cm.company.name,
            "company_code": cm.company.code,
            "is_head": is_head,
            "is_leader": is_leader,
        }

    # Build Structural Hierarchy: Company -> Department -> Team -> Employee
    depts_by_company: dict[Any, list[Department]] = {}
    for d in departments:
        depts_by_company.setdefault(d.company_id, []).append(d)

    teams_by_dept: dict[Any, list[Team]] = {}
    for t in teams:
        teams_by_dept.setdefault(t.department_id, []).append(t)

    structure = []
    total_depts_out = 0
    total_teams_out = 0

    for comp in companies:
        comp_cms = memberships_by_company.get(comp.id, [])
        cm_by_user = {cm.user_id: cm for cm in comp_cms}

        ceos = [
            make_member_card(cm)
            for cm in comp_cms
            if cm.role.upper() == SystemRole.CEO.upper()
        ]
        incharges = [
            make_member_card(cm)
            for cm in comp_cms
            if cm.role.upper() == SystemRole.COMPANY_IN_CHARGE.upper()
        ]
        project_managers = [
            make_member_card(cm)
            for cm in comp_cms
            if cm.role.upper() == SystemRole.PROJECT_MANAGER.upper()
        ]

        placed_in_company_depts: set = set()
        dept_nodes = []
        comp_teams_count = 0

        for dept in depts_by_company.get(comp.id, []):
            dept_dms = dept_members_map.get(dept.id, [])
            dept_user_ids = {dm.user_id for dm in dept_dms if dm.user_id in cm_by_user}
            placed_in_company_depts.update(dept_user_ids)

            head_dm = dept_head_map.get(dept.id)
            head_card = (
                make_member_card(cm_by_user[head_dm.user_id], is_head=True)
                if (head_dm and head_dm.user_id in cm_by_user)
                else None
            )

            placed_in_dept_teams: set = set()
            team_nodes = []

            for team in teams_by_dept.get(dept.id, []):
                team_tms = team_members_map.get(team.id, [])
                leader_tm = team_leader_map.get(team.id)
                leader_card = (
                    make_member_card(cm_by_user[leader_tm.user_id], is_leader=True)
                    if (leader_tm and leader_tm.user_id in cm_by_user)
                    else None
                )

                team_member_cards = []
                for tm in team_tms:
                    if tm.user_id not in cm_by_user:
                        continue
                    placed_in_dept_teams.add(tm.user_id)
                    placed_in_company_depts.add(tm.user_id)
                    team_member_cards.append(
                        make_member_card(cm_by_user[tm.user_id], is_leader=tm.is_leader)
                    )
                team_member_cards.sort(
                    key=lambda x: (not x["is_leader"], _role_rank(x["role"]), x["full_name"].lower())
                )

                team_matches_self = bool(
                    search_lower
                    and (search_lower in team.name.lower() or search_lower in (team.code or "").lower())
                )
                if search_lower and not team_matches_self:
                    filtered_members = [m for m in team_member_cards if m["matched"]]
                    if not filtered_members:
                        continue
                    team_member_cards = filtered_members

                team_nodes.append(
                    {
                        "id": str(team.id),
                        "type": "team",
                        "department_id": str(dept.id),
                        "company_id": str(comp.id),
                        "name": team.name,
                        "code": team.code,
                        "status": team.status,
                        "leader": leader_card,
                        "members_count": len(team_member_cards),
                        "members": team_member_cards,
                    }
                )

            # Department members not placed in any team of this department
            unassigned_dept_cards = [
                make_member_card(cm_by_user[dm.user_id], is_head=dm.is_head)
                for dm in dept_dms
                if dm.user_id in cm_by_user and dm.user_id not in placed_in_dept_teams
            ]
            unassigned_dept_cards.sort(
                key=lambda x: (not x["is_head"], _role_rank(x["role"]), x["full_name"].lower())
            )
            dept_matches_self = bool(
                search_lower
                and (search_lower in dept.name.lower() or search_lower in (dept.code or "").lower())
            )
            if search_lower and not dept_matches_self:
                unassigned_dept_cards = [m for m in unassigned_dept_cards if m["matched"]]
                if not team_nodes and not unassigned_dept_cards:
                    continue

            comp_teams_count += len(team_nodes)
            dept_nodes.append(
                {
                    "id": str(dept.id),
                    "type": "department",
                    "company_id": str(comp.id),
                    "name": dept.name,
                    "code": dept.code,
                    "status": dept.status,
                    "head": head_card,
                    "teams_count": len(team_nodes),
                    "employees_count": len(dept_user_ids),
                    "teams": team_nodes,
                    "unassigned_members": unassigned_dept_cards,
                }
            )

        # Company employees not assigned to any department in scope
        unassigned_company_cards = [
            make_member_card(cm)
            for cm in comp_cms
            if cm.user_id not in placed_in_company_depts
        ]
        unassigned_company_cards.sort(
            key=lambda x: (_role_rank(x["role"]), x["full_name"].lower())
        )
        comp_matches_self = bool(
            search_lower
            and (search_lower in comp.name.lower() or search_lower in (comp.code or "").lower())
        )
        if search_lower and not comp_matches_self:
            unassigned_company_cards = [m for m in unassigned_company_cards if m["matched"]]
            if not dept_nodes and not unassigned_company_cards:
                continue

        total_depts_out += len(dept_nodes)
        total_teams_out += comp_teams_count
        structure.append(
            {
                "id": str(comp.id),
                "type": "company",
                "name": comp.name,
                "code": comp.code,
                "status": comp.status,
                "ceos": ceos,
                "incharges": incharges,
                "project_managers": project_managers,
                "departments_count": len(dept_nodes),
                "teams_count": comp_teams_count,
                "employees_count": len(comp_cms),
                "departments": dept_nodes,
                "unassigned_employees": unassigned_company_cards,
            }
        )

    reporting_tree = _build_reporting_tree(
        nodes_by_id,
        matched_ids=matched_employee_ids,
        has_search=bool(search_lower),
    )

    payload = {
        "summary": {
            "companies_count": len(structure),
            "departments_count": total_depts_out,
            "teams_count": total_teams_out,
            "employees_count": len(nodes_by_id),
            "reporting_links_count": len(edges),
            "matched_employee_ids": sorted(matched_employee_ids),
        },
        "structure": structure,
        "reporting_tree": reporting_tree,
        "nodes": list(nodes_by_id.values()),
        "edges": edges,
    }
    return payload, 200


def build_hierarchy_employee_detail(
    request_user,
    target_user_id,
    company_id=None,
) -> tuple[dict[str, Any] | None, int]:
    """
    Selector for GET /api/organization/hierarchy/employee/<user_id>/ (SRS §5.4):
    Returns profile summary, memberships, departments, teams, direct reporting relationships,
    upward reporting chain, AND authorized assigned projects & tasks.
    """
    parsed_company, ok_c = _parse_optional_uuid(company_id)
    if not ok_c:
        return None, 400
    company_id = parsed_company

    scoped_company_ids, authorized = _resolve_authorized_company_ids(request_user, company_id)
    if not authorized:
        return None, 403

    target_user = User.objects.filter(pk=target_user_id).first()
    if target_user is None:
        return None, 404

    target_memberships = list(
        CompanyMembership.objects.filter(
            user=target_user,
            company_id__in=scoped_company_ids,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
        ).select_related("company")
    )
    if not target_memberships and not is_ceo(request_user):
        return None, 404

    shared_company_ids = {m.company_id for m in target_memberships} or scoped_company_ids
    authorized_project_ids = _get_viewer_authorized_project_ids(
        request_user, shared_company_ids, target_user_id=target_user.id
    )

    # Departments & Teams
    departments = [
        {
            "id": str(dm.id),
            "department_id": str(dm.department_id),
            "department_name": dm.department.name,
            "department_code": dm.department.code,
            "company_id": str(dm.department.company_id),
            "company_name": dm.department.company.name,
            "is_head": dm.is_head,
            "start_date": dm.start_date.isoformat() if dm.start_date else None,
        }
        for dm in DepartmentMembership.objects.filter(
            user=target_user,
            department__company_id__in=shared_company_ids,
            status=DepartmentMembership.Status.ACTIVE,
        ).select_related("department", "department__company")
    ]

    teams = [
        {
            "id": str(tm.id),
            "team_id": str(tm.team_id),
            "team_name": tm.team.name,
            "team_code": tm.team.code,
            "department_id": str(tm.team.department_id),
            "department_name": tm.team.department.name,
            "company_id": str(tm.team.department.company_id),
            "is_leader": tm.is_leader,
        }
        for tm in TeamMembership.objects.filter(
            user=target_user,
            team__department__company_id__in=shared_company_ids,
            status=TeamMembership.Status.ACTIVE,
        ).select_related("team", "team__department")
    ]

    # Reporting managers & direct reports
    reporting_managers = [
        {
            "relationship_id": str(rel.id),
            "id": str(rel.manager_id),
            "full_name": rel.manager.full_name,
            "email": rel.manager.email,
            "designation": rel.manager.designation,
            "company_id": str(rel.company_id),
            "company_name": rel.company.name,
            "start_date": rel.start_date.isoformat() if rel.start_date else None,
        }
        for rel in ReportingRelationship.objects.filter(
            employee=target_user,
            company_id__in=shared_company_ids,
            end_date__isnull=True,
        ).select_related("manager", "company")
    ]

    direct_reports = [
        {
            "relationship_id": str(rel.id),
            "id": str(rel.employee_id),
            "full_name": rel.employee.full_name,
            "email": rel.employee.email,
            "employee_id": rel.employee.employee_id,
            "designation": rel.employee.designation,
            "company_id": str(rel.company_id),
            "company_name": rel.company.name,
            "start_date": rel.start_date.isoformat() if rel.start_date else None,
        }
        for rel in ReportingRelationship.objects.filter(
            manager=target_user,
            company_id__in=shared_company_ids,
            end_date__isnull=True,
        )
        .select_related("employee", "company")
        .order_by("employee__full_name")
    ]

    # Upward reporting chain (cycle-safe)
    reporting_chain = []
    visited = {target_user.id}
    curr_id = target_user.id
    level = 1
    while level <= 20:
        rel = (
            ReportingRelationship.objects.filter(
                employee_id=curr_id,
                company_id__in=shared_company_ids,
                end_date__isnull=True,
            )
            .select_related("manager", "company")
            .order_by("-start_date")
            .first()
        )
        if not rel or rel.manager_id in visited:
            break
        visited.add(rel.manager_id)
        reporting_chain.append(
            {
                "level": level,
                "id": str(rel.manager_id),
                "full_name": rel.manager.full_name,
                "email": rel.manager.email,
                "designation": rel.manager.designation,
                "company_id": str(rel.company_id),
                "company_name": rel.company.name,
            }
        )
        curr_id = rel.manager_id
        level += 1

    # Authorized assigned projects (ProjectMember OR project_manager)
    proj_map: dict[Any, dict] = {}
    pm_qs = ProjectMember.objects.filter(
        user=target_user,
        project__company_id__in=shared_company_ids,
        status=ProjectMember.Status.ACTIVE,
    ).select_related("project", "project__company")
    if authorized_project_ids is not None:
        pm_qs = pm_qs.filter(project_id__in=authorized_project_ids)
    for pm in pm_qs:
        p = pm.project
        proj_map[p.id] = {
            "id": str(p.id),
            "code": p.code,
            "name": p.name,
            "company_id": str(p.company_id),
            "company_name": p.company.name,
            "status": p.status,
            "priority": p.priority,
            "progress": float(p.progress),
            "role_in_project": pm.role_in_project or "Member",
            "is_project_manager": p.project_manager_id == target_user.id,
        }

    managed_qs = Project.objects.filter(
        project_manager=target_user,
        company_id__in=shared_company_ids,
    ).select_related("company")
    if authorized_project_ids is not None:
        managed_qs = managed_qs.filter(id__in=authorized_project_ids)
    for p in managed_qs:
        if p.id not in proj_map:
            proj_map[p.id] = {
                "id": str(p.id),
                "code": p.code,
                "name": p.name,
                "company_id": str(p.company_id),
                "company_name": p.company.name,
                "status": p.status,
                "priority": p.priority,
                "progress": float(p.progress),
                "role_in_project": "Project Manager",
                "is_project_manager": True,
            }
        else:
            proj_map[p.id]["is_project_manager"] = True

    assigned_projects = sorted(proj_map.values(), key=lambda x: x["name"].lower())

    # Authorized assigned tasks (task_owner OR active TaskAssignment)
    assigned_task_ids = set(
        TaskAssignment.objects.filter(
            user=target_user,
            task__project__company_id__in=shared_company_ids,
            assignment_type=TaskAssignment.AssignmentType.USER,
            status__in=[TaskAssignment.Status.ACTIVE, TaskAssignment.Status.ACKNOWLEDGED],
        ).values_list("task_id", flat=True)
    )
    tasks_qs = (
        Task.objects.filter(
            project__company_id__in=shared_company_ids,
        )
        .filter(Q(task_owner=target_user) | Q(id__in=assigned_task_ids))
        .exclude(status=Task.Status.CANCELLED)
        .select_related("project")
        .distinct()
        .order_by("due_date", "-updated_at")
    )
    if authorized_project_ids is not None:
        tasks_qs = tasks_qs.filter(project_id__in=authorized_project_ids)

    today = date.today()
    all_tasks = list(tasks_qs)
    summary_total = len(all_tasks)
    summary_completed = 0
    summary_active = 0
    summary_overdue = 0
    summary_blocked = 0
    assigned_tasks_out = []

    for t in all_tasks:
        is_completed = t.status == Task.Status.COMPLETED
        is_overdue = bool(not is_completed and t.due_date and t.due_date < today)
        if is_completed:
            summary_completed += 1
        else:
            summary_active += 1
            if is_overdue:
                summary_overdue += 1
            if t.status == Task.Status.BLOCKED:
                summary_blocked += 1

        if len(assigned_tasks_out) < 25:
            relation = "Owner & Assignee" if (t.task_owner_id == target_user.id and t.id in assigned_task_ids) else (
                "Owner" if t.task_owner_id == target_user.id else "Assignee"
            )
            assigned_tasks_out.append(
                {
                    "id": str(t.id),
                    "title": t.title,
                    "project_id": str(t.project_id),
                    "project_code": t.project.code,
                    "project_name": t.project.name,
                    "status": t.status,
                    "priority": t.priority,
                    "due_date": t.due_date.isoformat() if t.due_date else None,
                    "progress": float(t.progress),
                    "is_overdue": is_overdue,
                    "relation": relation,
                }
            )

    return {
        "employee": {
            "id": str(target_user.id),
            "full_name": target_user.full_name,
            "email": target_user.email,
            "username": target_user.username,
            "employee_id": target_user.employee_id,
            "designation": target_user.designation,
            "profile_image": target_user.profile_image,
            "account_status": target_user.account_status,
            "roles": list({m.role for m in target_memberships}),
            "memberships": [
                {
                    "id": str(m.id),
                    "company_id": str(m.company_id),
                    "company_name": m.company.name,
                    "company_code": m.company.code,
                    "role": m.role,
                }
                for m in target_memberships
            ],
        },
        "departments": departments,
        "teams": teams,
        "reporting_managers": reporting_managers,
        "direct_reports": direct_reports,
        "reporting_chain": reporting_chain,
        "assigned_projects": assigned_projects,
        "assigned_tasks": assigned_tasks_out,
        "task_summary": {
            "total": summary_total,
            "active": summary_active,
            "completed": summary_completed,
            "overdue": summary_overdue,
            "blocked": summary_blocked,
        },
    }, 200
