# Task #18 — QA Test Execution Report: Projects, Tasks, Subtasks & Approvals

**Deliverable**: Test results recorded  
**Execution Date**: 2026-11-25  
**Backend Branch**: `features/impliment-project`  
**Frontend Branch**: `features/project-ui`  
**Database Engine**: PostgreSQL (`tms_db` / isolated test database `test_tms_db`, 45-table official UUID schema)  
**Overall Verdict**: **PASS (69 / 69 functional checks passed — 50/50 automated Django/DRF tests + 19/19 live server RBAC & E2E API smoke checks; 0 failures, 0 errors, 0 pending migrations, 0 frontend build/lint errors)**

---

## 1. Executive Summary

| Verification Suite | Scope | Executed | Passed | Failed / Errors | Duration / Status |
| --- | --- | ---: | ---: | ---: | --- |
| Automated Backend Suite (`manage.py test`) | `apps.accounts`, `apps.tasks`, `apps.reviews`, `apps.tracking`, `apps.projects` (12 test files) | 50 | 50 | 0 | `97.865s` — **OK** |
| Live Server RBAC & E2E API Smoke (`http://127.0.0.1:8000`) | 5 seeded JWT accounts, CEO vs Role dashboards, live Project -> Task -> Subtask -> Approval -> Closure + cleanup | 19 | 19 | 0 | **19/19 PASS** |
| Schema & Migration Invariant (`makemigrations --check --dry-run`) | All 15 Django apps (45 official PostgreSQL tables) | 1 | 1 | 0 | `No changes detected` |
| Django System Check (`manage.py check`) | Configuration, URLs, models, serializers | 1 | 1 | 0 | `0 issues (0 silenced)` |
| Frontend TypeScript + Vite Build (`npm run build`) | `tsc -b && vite build` across 95 modules | 1 | 1 | 0 | `486ms` — **0 errors** |
| Frontend Static Lint (`npm run lint`) | `oxlint` across 81 files (116 rules) | 1 | 1 | 0 | `228ms` — **0 errors** (83 non-blocking React hook style warnings) |

---

## 2. QA Defects Identified & Resolved During Task #18

| Defect ID | Severity | Area | Finding | Fix Applied & Verified |
| --- | --- | --- | --- | --- |
| **QA-18-01** | High (Audit Integrity) | Approvals (`TaskReviewViewSet`) | `DELETE /api/reviews/{id}/` returned `405`, but `PUT` and `PATCH` were not disabled — allowing historical review records (`decision`, `comments`, `correction_reason`) to be overwritten. | Overrode `update()` and `partial_update()` in `backend/apps/reviews/views.py` to raise `MethodNotAllowed` (`405`), and updated `ReviewManagementPermission` in `backend/apps/common/permissions.py`. Verified in both automated (`TC-APR`) and live smoke (`LIVE-APR-IMMUTABLE`). |
| **QA-18-02** | High (RBAC Security) | Subtasks (`transition_task_status_service`) | Authenticated regular `Employee` users could pass `override_subtasks=True` with a reason to force-complete a parent task with open subtasks. | Added `ROLE_DELIVERY_LEADS` RBAC check in `backend/apps/tasks/services/__init__.py` so only Team Leaders, Department Heads, Project Managers, Company In-Charge, or CEO may override open subtasks (`403 Forbidden` for regular employees). Verified in `TC-TSK`. |
| **QA-18-03** | Medium (Test Hygiene) | Attachments & Reviews Tests | `test_reviews.py` and `test_attachments.py` wrote uploaded test files directly into live `backend/media/attachments/`. | Added isolated `tempfile.mkdtemp()` + `override_settings(MEDIA_ROOT=...)` with `shutil.rmtree` cleanup in `setUpClass`/`tearDownClass` across `test_reviews.py`, `test_attachments.py`, and `test_qa_projects_tasks_subtasks_approvals.py`. |

---

## 3. Automated Backend Test Results (50 / 50 Passed)

### 3.1 Projects Domain (`apps.projects` — 14 Tests)

| # | Test File & Method | SRS / DB Coverage | Result |
| ---: | --- | --- | --- |
| 1 | `test_projects.py :: test_project_uuid_pk_and_company_scoped_code` | UUID PK, `uq_projects_company_code` duplicate rejection | **PASS** |
| 2 | `test_projects.py :: test_active_project_requires_manager_validation` | SRS §1.4 #2 Active project requires accountable PM | **PASS** |
| 3 | `test_projects.py :: test_project_phases_milestones_and_dependencies` | Phases, milestones, `MilestoneDependency`, `MilestoneTask` | **PASS** |
| 4 | `test_projects.py :: test_project_crud_api_create_read_update_close_archive_delete` | Full CRUD, filters, dept/team/member sync, close, archive, force delete | **PASS** |
| 5 | `test_projects.py :: test_project_manager_assignment_and_department_mapping_workflow` | SRS §6.3 PM reassignment, dept/team mapping, assignment hierarchy | **PASS** |
| 6 | `test_projects.py :: test_project_member_and_team_assignment_participation` | Historical membership preservation, team member sync, participation matrix | **PASS** |
| 7 | `test_m2_integration.py :: test_m2_end_to_end_project_and_task_lifecycle` | End-to-end project -> phase -> milestone -> task -> subtask -> checklist -> comment -> close | **PASS** |
| 8 | `test_project_task_integration_regression.py :: test_phase_milestone_task_dependency_and_rollup_lifecycle` | Phase/milestone/project auto-rollups, milestone sign-off, FS task deps, dashboard summary, closure | **PASS** |
| 9 | `test_project_task_integration_regression.py :: test_live_timer_one_active_rule_time_approval_notifications_and_reports` | Live timer (`uq_active_timer_per_user`), time approval RBAC, scoped notifications, filtered CSV/JSON reports | **PASS** |
| 10 | `test_project_task_integration_regression.py :: test_multi_company_isolation_across_projects_tasks_risks_and_dashboards` | Cross-company tenant isolation across projects, tasks, risks, issues, notifications, dashboards | **PASS** |
| 11 | `test_qa_projects_tasks_subtasks_approvals.py :: test_tc_prj_validation_constraints_closure_and_multi_project_participation` | **Task #18**: Active without PM (POST/PATCH), bad dates, cross-company PM/dept/team/member, cross-company code reuse, PM removal guard, closure blocked by empty remarks/open task/open subtask, multi-project participations | **PASS** |
| 12 | `test_qa_projects_tasks_subtasks_approvals.py :: test_tc_tsk_validation_rbac_boundaries_and_cancelled_exclusion` | **Task #18**: Bad task dates, progress bounds, cross-project phase, cross-company assign/bulk-assign, cancelled task rollup exclusion, Employee RBAC & `override_subtasks` 403 block | **PASS** |
| 13 | `test_qa_projects_tasks_subtasks_approvals.py :: test_tc_sub_multilevel_nesting_weighted_rollup_dependencies_and_cleanup` | **Task #18**: 3-tier nesting (`subtask_max_depth=2`), depth-3 block, deep Grandchild->Child->Parent->Phase->MS->Project weighted rollup, sibling FS dependency, cancel/delete recalculation, subtask filtering, 3-level tree delete | **PASS** |
| 14 | `test_qa_projects_tasks_subtasks_approvals.py :: test_tc_apr_multilevel_chain_midchain_reset_precedence_immutability_and_policy` | **Task #18**: 3-level & 4-level chains, Task > Project > Company precedence, mid-chain Step 2 `Changes Requested` reset to Step 1, `PUT`/`PATCH`/`DELETE` 405 immutability, `allow_reviewer_reassignment=False` lock | **PASS** |
| 15 | `test_qa_projects_tasks_subtasks_approvals.py :: test_tc_sub_apr_independent_subtask_and_parent_approval_integration` | **Task #18**: Independent approval workflows on both Subtask and Parent, parent completion blocked until approved subtask completes, full cascade to phase/milestone/project closure | **PASS** |

*(Note: 15 tests reside under `apps.projects`, covering Projects plus the Task #18 cross-domain QA suite for Projects, Tasks, Subtasks & Approvals.)*

### 3.2 Tasks, Subtasks, Dependencies & Attachments Domain (`apps.tasks` — 17 Tests)

| # | Test File & Method | SRS / DB Coverage | Result |
| ---: | --- | --- | --- |
| 16 | `test_tasks.py :: test_unified_task_and_subtask_hierarchy_and_progress_rollup` | Unified `tasks` table `parent_task_id` self-FK & subtask progress rollup | **PASS** |
| 17 | `test_tasks.py :: test_checklist_auto_progress_calculation` | Checklist items auto-calculating task progress (`Checklist` method) | **PASS** |
| 18 | `test_tasks.py :: test_task_assignment_user_and_team_and_reassignment_history` | User/team assignment check constraint & historical reassignment preservation | **PASS** |
| 19 | `test_tasks.py :: test_task_dependencies_tags_and_comments` | Dependencies, tags, and comment creation | **PASS** |
| 20 | `test_tasks.py :: test_cross_company_validation_in_task_serializer` | Cross-company department/owner rejection on Task creation | **PASS** |
| 21 | `test_tasks.py :: test_dependency_cycle_detection_and_paginated_history_endpoints` | Dependency cycle rejection & paginated comments/progress/reviews/dashboard APIs | **PASS** |
| 22 | `test_tasks.py :: test_task_crud_create_read_update_delete_api` | Full Task & Subtask CRUD, filters, tags, checklists, reassignment & cascading delete | **PASS** |
| 23 | `test_tasks.py :: test_task_assignment_priority_and_due_dates_workflow` | Team leader coordination, acknowledge, bulk assign, overdue/due-soon & reschedule | **PASS** |
| 24 | `test_tasks.py :: test_task_status_workflow_and_validation` | Transition rules, owner requirement, approval gate, subtask completion gate + override, DB custom `WorkflowTransition` | **PASS** |
| 25 | `test_dependencies_and_overdue.py :: test_dependency_validation_self_cross_project_and_multi_hop_cycle` | Self, cross-project & multi-hop circular dependency rejection | **PASS** |
| 26 | `test_dependencies_and_overdue.py :: test_all_four_dependency_types_enforcement_and_authorized_override` | FS, SS, FF, SF dependency enforcement + authorized override | **PASS** |
| 27 | `test_dependencies_and_overdue.py :: test_working_calendar_and_holiday_aware_overdue_calculation` | Company working days & holiday-aware overdue calculation | **PASS** |
| 28 | `test_dependencies_and_overdue.py :: test_downstream_delay_warnings_reschedule_notifications_and_graph` | Downstream delay ripple notifications & project dependency DAG | **PASS** |
| 29 | `test_dependencies_and_overdue.py :: test_overdue_summary_check_deadlines_deduplication_dashboard_and_csv_report` | Overdue summary, deduplicated deadline alerts & CSV report | **PASS** |
| 30 | `test_attachments.py :: test_multiple_file_upload_storage_checksum_and_explicit_associations` | Multi-file upload, SHA-256 checksum, `storage_key` hidden, explicit join tables | **PASS** |
| 31 | `test_attachments.py :: test_file_type_executable_rejection_and_configurable_size_and_zip_limits` | `.exe`/`.sh` rejection, `max_upload_size_mb` & `allow_zip_archives` enforcement | **PASS** |
| 32 | `test_attachments.py :: test_permission_controlled_download_inline_preview_and_delete_cleanup` | Auth download, inline preview, 403 outsider block & physical disk cleanup | **PASS** |

### 3.3 Approvals & Reviews Domain (`apps.reviews` — 7 Tests)

| # | Test File & Method | SRS / DB Coverage | Result |
| ---: | --- | --- | --- |
| 33 | `test_reviews.py :: test_configurable_multi_level_approval_chain_and_completion_rules` | Priority-based 2-step chain (`Team Leader -> PM`), self-approve 403, step-skip 403, final completion gate | **PASS** |
| 34 | `test_reviews.py :: test_correction_rejection_attachments_resubmission_and_immutable_history` | Mandatory `correction_reason`, reference PDF attachment, `DELETE` 405 immutability, multi-round rework | **PASS** |
| 35 | `test_reviews.py :: test_reviewer_reassignment_sla_escalation_and_review_queue_analytics` | Outsider/employee reassignment blocks, PM reassignment, 48h SLA escalation, queue & KPI analytics | **PASS** |
| 36 | `test_workflow_approval_integration.py :: test_transition_status_cannot_bypass_reviewer_permissions_or_multilevel_chain` | Blocks reviewer bypass on `transition-status` & `PATCH`; preserves mid-chain reviewer on note update | **PASS** |
| 37 | `test_workflow_approval_integration.py :: test_reopening_completed_task_resets_approval_cycle_and_syncs_audit_reviews` | Syncs `TaskReview` audit row on workflow approve; blocks double approval; resets cycle on reopen | **PASS** |
| 38 | `test_workflow_approval_integration.py :: test_dependencies_enforced_on_submit_for_review_and_progress_updates` | Blocks submit-for-review on unsatisfied FS dep; blocks auto-start on blocked task; delay warnings | **PASS** |
| 39 | `test_workflow_approval_integration.py :: test_reviewer_reassignment_updates_policy_chain_and_role_scoped_transitions_and_rollups` | Policy chain override persistence, role-scoped `WorkflowTransition`, empty-project 0% progress reset | **PASS** |

### 3.4 Progress Tracking & Work Logs (`apps.tracking` — 3 Tests)

| # | Test File & Method | SRS / DB Coverage | Result |
| ---: | --- | --- | --- |
| 40 | `test_progress_updates.py :: test_manual_progress_update_remarks_time_entry_and_project_rollup` | Append-only progress updates, auto `TimeEntry` creation & project rollup | **PASS** |
| 41 | `test_progress_updates.py :: test_manual_progress_not_overwritten_by_auto_calculations_and_weighted_subtasks` | Manual progress preservation & effort-weighted subtask progress | **PASS** |
| 42 | `test_progress_updates.py :: test_blocker_reporting_in_progress_update_and_work_log_review_summary` | Blocker reporting, notifications & manager work-log review analytics | **PASS** |

### 3.5 Authentication, RBAC & Role Dashboards (`apps.accounts` — 8 Tests)

| # | Test File & Method | SRS / DB Coverage | Result |
| ---: | --- | --- | --- |
| 43 | `test_auth.py :: test_all_five_seeded_accounts_login_by_email_and_username` | JWT login by both email & username across all 5 default RBAC accounts | **PASS** |
| 44 | `test_auth.py :: test_me_refresh_change_password_and_logout` | `/me/`, refresh token rotation, password change & token blacklist logout | **PASS** |
| 45 | `test_auth.py :: test_invalid_password_and_inactive_user_rejected` | Invalid password & deactivated account rejected cleanly (`401`) | **PASS** |
| 46 | `test_rbac_and_dashboards.py :: test_ceo_dashboard_restricted_to_ceo_only` | `/api/dashboards/ceo/` strictly CEO-only (`403` for PM/Head/Lead/Employee) | **PASS** |
| 47 | `test_rbac_and_dashboards.py :: test_role_dashboard_returns_role_specific_scope_and_capabilities` | `/api/dashboards/role/` returns role-scoped KPIs & capability matrix | **PASS** |
| 48 | `test_rbac_and_dashboards.py :: test_company_crud_strictly_ceo_only` | Company create/update/soft-delete strictly CEO-only | **PASS** |
| 49 | `test_rbac_and_dashboards.py :: test_employee_cannot_modify_projects_tasks_departments_teams_users_or_reports` | Full Employee read-only boundary enforcement across management endpoints | **PASS** |
| 50 | `test_rbac_and_dashboards.py :: test_pm_can_manage_projects_and_tasks_but_not_delete_projects_or_manage_companies` | PM scope boundaries (projects/tasks write allowed; project delete & company admin blocked) | **PASS** |

---

## 4. Live Server E2E & RBAC Smoke Results (19 / 19 Passed)

Executed against live Django backend (`http://127.0.0.1:8000`) connected to PostgreSQL `tms_db` with automatic cleanup of the temporary smoke project:

| Case ID | Scenario Verified | Observed HTTP / Value | Result |
| --- | --- | --- | --- |
| `LIVE-AUTH-CEO` | JWT login as CEO (`admin@tms.local`) | `HTTP 200` | **PASS** |
| `LIVE-AUTH-PROJECTMANAGER` | JWT login as ProjectManager (`pm@tms.local`) | `HTTP 200` | **PASS** |
| `LIVE-AUTH-DEPARTMENTHEAD` | JWT login as DepartmentHead (`depthead@tms.local`) | `HTTP 200` | **PASS** |
| `LIVE-AUTH-TEAMLEADER` | JWT login as TeamLeader (`teamlead@tms.local`) | `HTTP 200` | **PASS** |
| `LIVE-AUTH-EMPLOYEE` | JWT login as Employee (`employee@tms.local`) | `HTTP 200` | **PASS** |
| `LIVE-RBAC-CEO-DASH-OK` | CEO accesses `/api/dashboards/ceo/` | `HTTP 200` | **PASS** |
| `LIVE-RBAC-PROJECTMANAGER-DASH` | PM blocked on CEO dashboard & allowed Role dashboard | `ceo=403, role=200` | **PASS** |
| `LIVE-RBAC-DEPARTMENTHEAD-DASH` | DeptHead blocked on CEO dashboard & allowed Role dashboard | `ceo=403, role=200` | **PASS** |
| `LIVE-RBAC-TEAMLEADER-DASH` | TeamLeader blocked on CEO dashboard & allowed Role dashboard | `ceo=403, role=200` | **PASS** |
| `LIVE-RBAC-EMPLOYEE-DASH` | Employee blocked on CEO dashboard & allowed Role dashboard | `ceo=403, role=200` | **PASS** |
| `LIVE-RBAC-EMP-NO-COMPANY` | Employee blocked from creating company | `HTTP 403` | **PASS** |
| `LIVE-PRJ-CREATE` | CEO creates live Active project (`QA18-SMOKE-01`) | `HTTP 201` | **PASS** |
| `LIVE-RBAC-EMP-NO-TASK` | Employee blocked from creating task | `HTTP 403` | **PASS** |
| `LIVE-TSK-CREATE` | PM creates parent task (`Subtasks` rollup + `approval_required=True`) | `HTTP 201` | **PASS** |
| `LIVE-SUB-CREATE` | PM creates subtask (`approval_required=True`) linked to parent | `HTTP 201` | **PASS** |
| `LIVE-SUB-APPROVAL` | Employee submits subtask (200), self-approve blocked (403), TeamLeader approves (201) | `200 / 403 / 201` | **PASS** |
| `LIVE-APR-IMMUTABLE` | Historical review immutable on `PATCH` and `DELETE` | `patch=405, delete=405` | **PASS** |
| `LIVE-E2E-ROLLUP-CLOSURE` | Subtask completed -> Parent 100.00% rollup -> Parent approved & completed -> Project closed | `parent_prog=100.00, close=200` | **PASS** |
| `LIVE-CLEANUP` | Temporary live smoke project deleted cleanly | `HTTP 204` | **PASS** |

---

## 5. Sign-Off

- **Projects, Tasks, Subtasks, and Approvals QA Gate**: **APPROVED**
- **Database Schema Compliance**: Official 45-table UUID PostgreSQL design preserved (`No changes detected`).
- **Roadmap Status**: **18 / 21 tasks completed (85.7%)**.

