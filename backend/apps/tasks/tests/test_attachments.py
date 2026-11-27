import hashlib
import shutil
import tempfile
from pathlib import Path
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.projects.models import Project
from apps.system_settings.models import SystemSetting
from apps.tasks.models import Attachment, CommentAttachment, Task, TaskAttachment, TaskComment
from apps.tracking.models import ProgressAttachment, ProgressUpdate


class AttachmentsAndFileUploadHandlingTestCase(APITestCase):
    """
    Task #13: Implement attachments and file upload handling (Document storage, validation, download & delete).
    Validates SRS §7.7, §7.8, §20.1, §24.2, §27.3, §27.5 & DB Design §4.5, §5, §6, §7 Rule #9.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._temp_media_dir = tempfile.mkdtemp(prefix="tms_test_attachments_")
        cls._media_override = override_settings(MEDIA_ROOT=cls._temp_media_dir)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._temp_media_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.pm = User.objects.create_user(
            email="pm_att@acme.com",
            username="pm_att",
            full_name="PM Attachments",
            password="Password123!",
        )
        self.dev = User.objects.create_user(
            email="dev_att@acme.com",
            username="dev_att",
            full_name="Dev Attachments",
            password="Password123!",
        )
        self.outsider = User.objects.create_user(
            email="outsider_att@other.com",
            username="outsider_att",
            full_name="Outsider User",
            password="Password123!",
        )

        self.company = Company.objects.create(name="Attachment Corp", code="ATT-CO")
        self.other_company = Company.objects.create(name="Other Corp", code="OTH-CO")

        CompanyMembership.objects.create(company=self.company, user=self.pm, status="Active")
        CompanyMembership.objects.create(company=self.company, user=self.dev, status="Active")
        CompanyMembership.objects.create(company=self.other_company, user=self.outsider, status="Active")

        self.project = Project.objects.create(
            company=self.company,
            project_manager=self.pm,
            name="Media Storage Platform",
            code="PRJ-ATT-01",
            status=Project.Status.ACTIVE,
        )
        self.task = Task.objects.create(
            project=self.project,
            title="Implement Secure Document Storage",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
        )
        self.client.force_authenticate(user=self.dev)

    def test_multiple_file_upload_storage_checksum_and_explicit_associations(self):
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        mp4_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"

        pdf_file = SimpleUploadedFile("architecture_spec.pdf", pdf_bytes, content_type="application/pdf")
        png_file = SimpleUploadedFile("ui_mockup.png", png_bytes, content_type="image/png")
        mp4_file = SimpleUploadedFile("demo_walkthrough.mp4", mp4_bytes, content_type="video/mp4")

        comment = TaskComment.objects.create(
            task=self.task,
            author=self.dev,
            content="Attached spec, UI mockup, and walkthrough video.",
        )
        prog = ProgressUpdate.objects.create(
            task=self.task,
            author=self.dev,
            progress_percentage="50.00",
            work_completed="Uploaded design and video deliverables.",
        )

        res = self.client.post(
            "/api/tasks/attachments/upload/",
            {
                "task": str(self.task.id),
                "comment": str(comment.id),
                "progress_update": str(prog.id),
                "files": [pdf_file, png_file, mp4_file],
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(res.data["uploaded_count"], 3)

        attachments_data = res.data["attachments"]
        categories = {a["file_name"]: a["file_category"] for a in attachments_data}
        self.assertEqual(categories["architecture_spec.pdf"], "document")
        self.assertEqual(categories["ui_mockup.png"], "image")
        self.assertEqual(categories["demo_walkthrough.mp4"], "video")

        # Verify storage_key is NOT exposed on read (DB Design §7 Rule #9)
        for a in attachments_data:
            self.assertNotIn("storage_key", a)
            self.assertIn("download_url", a)
            self.assertIn("preview_url", a)

        # Verify SHA-256 checksum and physical file storage on disk
        pdf_att = Attachment.objects.get(file_name="architecture_spec.pdf")
        expected_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        self.assertEqual(pdf_att.checksum, expected_sha256)
        abs_path = Path(settings.MEDIA_ROOT) / pdf_att.storage_key
        self.assertTrue(abs_path.exists())
        self.assertEqual(abs_path.read_bytes(), pdf_bytes)

        # Verify explicit association tables (task_attachments, comment_attachments, progress_attachments)
        self.assertEqual(TaskAttachment.objects.filter(task=self.task).count(), 3)
        self.assertEqual(CommentAttachment.objects.filter(comment=comment).count(), 3)
        self.assertEqual(ProgressAttachment.objects.filter(progress_update=prog).count(), 3)

    def test_file_type_executable_rejection_and_configurable_size_and_zip_limits(self):
        # 1. Executable file (.exe) must be rejected (SRS §24.2 & §27.5)
        exe_file = SimpleUploadedFile("malware.exe", b"MZ\x90\x00", content_type="application/octet-stream")
        exe_res = self.client.post(
            f"/api/tasks/{self.task.id}/attachments/",
            {"files": [exe_file]},
            format="multipart",
        )
        self.assertEqual(exe_res.status_code, status.HTTP_400_BAD_REQUEST)

        # 2. Shell script (.sh) must be rejected
        sh_file = SimpleUploadedFile("deploy.sh", b"#!/bin/bash\nrm -rf /", content_type="text/plain")
        sh_res = self.client.post(
            f"/api/tasks/{self.task.id}/attachments/",
            {"files": [sh_file]},
            format="multipart",
        )
        self.assertEqual(sh_res.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. Configurable file size limit via SystemSetting (`max_upload_size_mb`)
        SystemSetting.objects.create(
            company=self.company,
            setting_key="max_upload_size_mb",
            setting_value=0.001,  # ~1 KB limit
        )
        large_pdf = SimpleUploadedFile("large_report.pdf", b"A" * 4096, content_type="application/pdf")
        size_res = self.client.post(
            f"/api/tasks/{self.task.id}/attachments/",
            {"files": [large_pdf]},
            format="multipart",
        )
        self.assertEqual(size_res.status_code, status.HTTP_400_BAD_REQUEST)

        # 4. Configurable ZIP archive policy (`allow_zip_archives = False`)
        SystemSetting.objects.filter(company=self.company, setting_key="max_upload_size_mb").delete()
        SystemSetting.objects.create(
            company=self.company,
            setting_key="allow_zip_archives",
            setting_value=False,
        )
        zip_file = SimpleUploadedFile("bundle.zip", b"PK\x03\x04", content_type="application/zip")
        zip_res = self.client.post(
            f"/api/tasks/{self.task.id}/attachments/",
            {"files": [zip_file]},
            format="multipart",
        )
        self.assertEqual(zip_res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_permission_controlled_download_inline_preview_and_delete_cleanup(self):
        doc_bytes = b"Confidential project specification content"
        doc_file = SimpleUploadedFile("confidential_spec.txt", doc_bytes, content_type="text/plain")

        up_res = self.client.post(
            f"/api/tasks/{self.task.id}/attachments/",
            {"files": [doc_file]},
            format="multipart",
        )
        self.assertEqual(up_res.status_code, status.HTTP_201_CREATED, up_res.content)
        task_att_id = up_res.data[0]["id"]
        att_id = up_res.data[0]["attachment"]
        att_obj = Attachment.objects.get(id=att_id)
        abs_path = Path(settings.MEDIA_ROOT) / att_obj.storage_key
        self.assertTrue(abs_path.exists())

        # 1. Authorized download returns binary content + attachment Content-Disposition + SHA256 header
        dl_res = self.client.get(f"/api/tasks/attachments/{att_id}/download/")
        self.assertEqual(dl_res.status_code, status.HTTP_200_OK)
        self.assertEqual(dl_res.content, doc_bytes)
        self.assertIn('attachment; filename="confidential_spec.txt"', dl_res["Content-Disposition"])
        self.assertEqual(dl_res["X-Content-SHA256"], hashlib.sha256(doc_bytes).hexdigest())

        # 2. Inline preview (`?inline=1`) returns inline Content-Disposition
        preview_res = self.client.get(f"/api/tasks/attachments/{att_id}/download/?inline=1")
        self.assertEqual(preview_res.status_code, status.HTTP_200_OK)
        self.assertIn('inline; filename="confidential_spec.txt"', preview_res["Content-Disposition"])

        # 3. Cross-company outsider is blocked from downloading or deleting (403 Forbidden)
        self.client.force_authenticate(user=self.outsider)
        forbidden_dl = self.client.get(f"/api/tasks/attachments/{att_id}/download/")
        self.assertEqual(forbidden_dl.status_code, status.HTTP_403_FORBIDDEN)
        forbidden_del = self.client.delete(f"/api/tasks/attachments/{att_id}/")
        self.assertEqual(forbidden_del.status_code, status.HTTP_403_FORBIDDEN)

        # 4. Authorized delete via TaskAttachment unlinks and cleans up unreferenced Attachment + disk file
        self.client.force_authenticate(user=self.dev)
        del_res = self.client.delete(f"/api/tasks/task-attachments/{task_att_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(TaskAttachment.objects.filter(id=task_att_id).exists())
        self.assertFalse(Attachment.objects.filter(id=att_id).exists())
        self.assertFalse(abs_path.exists())
