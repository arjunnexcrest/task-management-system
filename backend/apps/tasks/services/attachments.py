import base64
import hashlib
import mimetypes
import os
import re
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.companies.models import CompanyMembership
from apps.system_settings.models import SystemSetting
from apps.tasks.models import Attachment, CommentAttachment, Task, TaskAttachment, TaskComment
from apps.tracking.models import ProgressAttachment, ProgressUpdate

# SRS §7.8 Approved File Types:
# - Images: JPG, JPEG, PNG, WEBP
# - Documents: PDF, DOC, DOCX, XLS, XLSX, PPT, PPTX, TXT, CSV
# - Videos: MP4, WEBM, MOV
# - Archives: ZIP (if enabled)
APPROVED_EXTENSIONS = {
    # Images
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    # Documents
    ".pdf": "document",
    ".doc": "document",
    ".docx": "document",
    ".xls": "document",
    ".xlsx": "document",
    ".ppt": "document",
    ".pptx": "document",
    ".txt": "document",
    ".csv": "document",
    # Videos
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    # Archives
    ".zip": "archive",
}

BLOCKED_EXECUTABLE_EXTENSIONS = {
    ".exe",
    ".bat",
    ".cmd",
    ".sh",
    ".ps1",
    ".php",
    ".phtml",
    ".js",
    ".mjs",
    ".cjs",
    ".msi",
    ".dll",
    ".scr",
    ".vbs",
    ".jar",
    ".com",
    ".py",
    ".rb",
    ".pl",
}

ALLOWED_MIME_PREFIXES = (
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "text/plain",
    "text/csv",
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "application/zip",
    "application/x-zip-compressed",
    "application/json",
)

DEFAULT_MAX_UPLOAD_SIZE_MB = 50


def get_file_category(file_name: str, mime_type: str = "") -> str:
    ext = os.path.splitext(file_name or "")[1].lower()
    if ext in APPROVED_EXTENSIONS:
        return APPROVED_EXTENSIONS[ext]
    mime_lower = (mime_type or "").lower()
    if mime_lower.startswith("image/"):
        return "image"
    if mime_lower.startswith("video/"):
        return "video"
    if "zip" in mime_lower:
        return "archive"
    return "document"


def sanitize_filename(file_name: str) -> str:
    base = os.path.basename(str(file_name or "attachment").replace("\\", "/"))
    cleaned = re.sub(r"[^A-Za-z0-9._\- ()]", "_", base).strip()
    return cleaned[:200] or "attachment"


def get_max_upload_size_bytes(company=None) -> int:
    setting = None
    if company is not None:
        setting = SystemSetting.objects.filter(
            company=company, setting_key="max_upload_size_mb"
        ).first()
    if setting is None:
        setting = SystemSetting.objects.filter(
            company__isnull=True, setting_key="max_upload_size_mb"
        ).first()
    max_mb = DEFAULT_MAX_UPLOAD_SIZE_MB
    if setting and setting.setting_value is not None:
        try:
            max_mb = float(setting.setting_value)
        except (TypeError, ValueError):
            max_mb = DEFAULT_MAX_UPLOAD_SIZE_MB
    return int(max_mb * 1024 * 1024)


def is_zip_archive_enabled(company=None) -> bool:
    setting = None
    if company is not None:
        setting = SystemSetting.objects.filter(
            company=company, setting_key="allow_zip_archives"
        ).first()
    if setting is None:
        setting = SystemSetting.objects.filter(
            company__isnull=True, setting_key="allow_zip_archives"
        ).first()
    if setting is not None:
        return str(setting.setting_value).lower() not in ("false", "0", "no")
    return True


def validate_attachment_file(
    *,
    file_name: str,
    mime_type: str,
    file_size_bytes: int,
    company=None,
) -> None:
    """
    SRS §7.8, §20.1, §24.2 & §27.5:
    Validates file extension, MIME type, archive policy, and file-size limits.
    """
    if not file_name or not str(file_name).strip():
        raise ValidationError("File name is required.")

    ext = os.path.splitext(str(file_name).strip())[1].lower()
    if ext in BLOCKED_EXECUTABLE_EXTENSIONS:
        raise ValidationError(
            f"Executable or script file type '{ext}' is strictly prohibited for security (SRS §24.2 & §27.5)."
        )

    if ext and ext not in APPROVED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file extension '{ext}'. Approved types: JPG, JPEG, PNG, WEBP, PDF, DOC, DOCX, XLS, XLSX, PPT, PPTX, TXT, CSV, MP4, WEBM, MOV, ZIP (SRS §7.8)."
        )

    if ext == ".zip" and not is_zip_archive_enabled(company=company):
        raise ValidationError("ZIP archive uploads are currently disabled by system settings (SRS §7.8).")

    normalized_mime = (mime_type or "").lower().strip()
    if not normalized_mime or normalized_mime == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(file_name)
        if guessed:
            normalized_mime = guessed.lower()

    if not normalized_mime or not normalized_mime.startswith(ALLOWED_MIME_PREFIXES):
        raise ValidationError(
            f"Unsupported MIME type '{mime_type}'. Allowed: images (JPG/PNG/WEBP), documents (PDF/DOCX/XLSX/PPTX/TXT), videos (MP4), and ZIP archives (SRS §7.8)."
        )

    max_bytes = get_max_upload_size_bytes(company=company)
    if file_size_bytes is None or int(file_size_bytes) <= 0:
        raise ValidationError("File size must be greater than 0 bytes.")
    if int(file_size_bytes) > max_bytes:
        max_mb = round(max_bytes / (1024 * 1024), 1)
        raise ValidationError(
            f"File size ({file_size_bytes} bytes) exceeds the maximum allowed limit of {max_mb} MB (SRS §7.8 & §20.1)."
        )


def _resolve_target_company(
    *,
    task: Task | None = None,
    comment: TaskComment | None = None,
    progress_update: ProgressUpdate | None = None,
):
    if task is not None:
        return task.project.company
    if comment is not None:
        return comment.task.project.company
    if progress_update is not None:
        return progress_update.task.project.company
    return None


def _validate_uploader_in_company(user: User, company) -> None:
    if company is None or getattr(user, "is_superuser", False):
        return
    if getattr(company, "company_incharge_id", None) == user.id:
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
        raise PermissionDenied(
            f"User '{user.full_name}' is not authorized to upload or access files in company '{company.code}' (SRS §24.2 & §27.5)."
        )


@transaction.atomic
def store_and_create_attachment_service(
    *,
    uploaded_by: User,
    uploaded_file=None,
    file_name: str | None = None,
    mime_type: str | None = None,
    file_size_bytes: int | None = None,
    file_bytes: bytes | None = None,
    file_content_base64: str | None = None,
    task: Task | None = None,
    comment: TaskComment | None = None,
    progress_update: ProgressUpdate | None = None,
) -> Attachment:
    """
    SRS §7.8 & DB Design §4.5, §6, §7 Rule #9:
    - Validates uploader company access, file extension, MIME type, and size limit.
    - Stores file bytes on disk under MEDIA_ROOT/attachments/<YYYY_MM>/<uuid>_<filename>.
    - Calculates SHA-256 checksum.
    - Creates Attachment record and links to TaskAttachment, CommentAttachment, and/or ProgressAttachment.
    """
    company = _resolve_target_company(
        task=task, comment=comment, progress_update=progress_update
    )
    if uploaded_by and company:
        _validate_uploader_in_company(uploaded_by, company)

    raw_bytes: bytes | None = None
    if uploaded_file is not None:
        raw_name = getattr(uploaded_file, "name", None) or file_name or "upload.bin"
        raw_mime = getattr(uploaded_file, "content_type", None) or mime_type or ""
        raw_bytes = uploaded_file.read()
        raw_size = len(raw_bytes)
    elif file_bytes is not None:
        raw_name = file_name or "upload.bin"
        raw_mime = mime_type or ""
        raw_bytes = file_bytes
        raw_size = len(raw_bytes)
    elif file_content_base64:
        raw_name = file_name or "upload.bin"
        raw_mime = mime_type or ""
        # Strip optional data URL prefix (e.g. "data:image/png;base64,...")
        b64_payload = file_content_base64.split(",", 1)[-1]
        try:
            raw_bytes = base64.b64decode(b64_payload)
        except Exception:
            raise ValidationError("Invalid base64 file content.")
        raw_size = len(raw_bytes)
    else:
        raw_name = file_name or ""
        raw_mime = mime_type or ""
        raw_size = int(file_size_bytes) if file_size_bytes is not None else 0

    clean_name = sanitize_filename(raw_name)
    if not raw_mime or raw_mime == "application/octet-stream":
        guessed_mime, _ = mimetypes.guess_type(clean_name)
        if guessed_mime:
            raw_mime = guessed_mime

    validate_attachment_file(
        file_name=clean_name,
        mime_type=raw_mime,
        file_size_bytes=raw_size,
        company=company,
    )

    attachment_uuid = uuid.uuid4()
    folder_month = timezone.now().strftime("%Y_%m")
    storage_key = f"attachments/{folder_month}/{attachment_uuid}_{clean_name}"

    checksum = None
    if raw_bytes is not None:
        checksum = hashlib.sha256(raw_bytes).hexdigest()
        abs_path = Path(settings.MEDIA_ROOT) / storage_key
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(raw_bytes)

    attachment = Attachment.objects.create(
        id=attachment_uuid,
        uploaded_by=uploaded_by,
        file_name=clean_name,
        storage_key=storage_key,
        mime_type=raw_mime,
        file_size_bytes=raw_size,
        checksum=checksum,
    )

    effective_task = task
    if comment is not None:
        CommentAttachment.objects.get_or_create(comment=comment, attachment=attachment)
        if effective_task is None:
            effective_task = comment.task

    if progress_update is not None:
        ProgressAttachment.objects.get_or_create(
            progress_update=progress_update, attachment=attachment
        )
        if effective_task is None:
            effective_task = progress_update.task

    if effective_task is not None:
        TaskAttachment.objects.get_or_create(task=effective_task, attachment=attachment)

    return attachment


def check_attachment_access_permission(attachment: Attachment, user: User | None) -> None:
    """
    SRS §7.8 & §27.5: Permission-controlled download & delete check.
    Verifies that an authenticated user belongs to at least one company associated with
    the attachment (or is the uploader / CEO / superuser).
    """
    from apps.common.permissions import is_ceo

    if user is None or not getattr(user, "is_authenticated", False):
        return
    if is_ceo(user) or attachment.uploaded_by_id == user.id:
        return

    companies = set()
    for tl in attachment.task_links.select_related("task__project__company").all():
        companies.add(tl.task.project.company)
    for cl in attachment.comment_links.select_related("comment__task__project__company").all():
        companies.add(cl.comment.task.project.company)
    for pl in attachment.progress_links.select_related("progress_update__task__project__company").all():
        companies.add(pl.progress_update.task.project.company)

    if not companies:
        return

    for comp in companies:
        if not getattr(comp, "is_active", True):
            continue
        if getattr(comp, "company_incharge_id", None) == user.id:
            return
        if CompanyMembership.objects.filter(
            company=comp,
            user=user,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
        ).exists():
            return

    raise PermissionDenied(
        "You do not have permission to access or modify this attachment across company boundaries (SRS §7.8 & §27.5)."
    )


def remove_physical_file_if_unreferenced(storage_key: str, exclude_attachment_id=None) -> None:
    if not storage_key:
        return
    qs = Attachment.objects.filter(storage_key=storage_key)
    if exclude_attachment_id:
        qs = qs.exclude(id=exclude_attachment_id)
    if qs.exists():
        return
    abs_path = Path(settings.MEDIA_ROOT) / storage_key
    if abs_path.exists() and abs_path.is_file():
        try:
            abs_path.unlink()
        except OSError:
            pass


@transaction.atomic
def delete_attachment_service(*, attachment: Attachment, actor: User | None = None) -> None:
    """
    SRS §7.8: Permission-checked attachment deletion and physical storage cleanup.
    """
    check_attachment_access_permission(attachment, actor)
    storage_key = attachment.storage_key
    att_id = attachment.id
    attachment.delete()
    remove_physical_file_if_unreferenced(storage_key, exclude_attachment_id=att_id)
