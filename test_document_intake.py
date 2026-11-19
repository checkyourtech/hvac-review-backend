"""Synthetic document/API regressions; real local parsers, mocked paid services."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

import fitz
from fastapi.testclient import TestClient
import starlette.formparsers

import main
import document_intake as intake
import test_purchase_security as security
from test_blower_live_response import saved_response
from test_blower_support_normalization import BAD


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
TYPES = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')


def pdf(text=BAD, pages=1, image_page=False, encrypted=False):
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(30, 30, 570, 800), text, fontsize=10)
    if image_page:
        page = doc.new_page() if text else doc[0]
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
        pix.clear_with(255)
        page.insert_image(fitz.Rect(20, 20, 100, 100), pixmap=pix)
    result = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="synthetic", user_pw="synthetic") if encrypted else doc.tobytes()
    doc.close()
    return result


def docx(body=None, extras=None, types=TYPES):
    body = body if body is not None else '<w:p><w:r><w:t>Replace capacitor. Total $300. Verify operation.</w:t></w:r></w:p>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        for name, value in (extras or {}).items():
            z.writestr(name, value)
    return out.getvalue()


class DocumentIntakeTests(unittest.TestCase):
    def setUp(self):
        self.h = security.PurchaseSecurityTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.app.add_middleware(intake.IntakeBodyLimit)
        self.h.app.exception_handlers.update(main.app.exception_handlers)

    def send(self, files, data=None, headers=None):
        return self.h.client.post("/upload", headers=self.h.headers() if headers is None else headers,
            data={"customer_name": "Offline Customer", "customer_email": "offline@example.invalid"} if data is None else data,
            files=[("files", (name, content, mime)) for name, content, mime in files])

    def reject(self, files, status):
        response = self.send(files)
        self.assertEqual(response.status_code, status, response.text)
        self.h.assert_no_paid_work()
        self.assertEqual(list(self.h.uploads.iterdir()), [])
        self.assertNotIn("Traceback", response.text)
        return response

    def test_valid_tier1_text_pdf(self):
        response = self.send([("quote.pdf", pdf(), "application/pdf")])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("blower", self.h.classifier.call_args.args[0].lower())
        self.assertEqual(self.h.usage(), 1)
        self.assertEqual(list(self.h.uploads.iterdir()), [])

    def test_valid_tier2_multiple_pdfs(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        response = self.send([("a.pdf", pdf("Quote A repair total $300."), "application/pdf"),
                              ("b.pdf", pdf("Quote B repair total $400."), "application/pdf")])
        self.assertEqual(response.status_code, 200, response.text)
        text = self.h.classifier.call_args.args[0]
        self.assertIn("Quote A", text)
        self.assertIn("Quote B", text)
        self.assertEqual(self.h.usage(), 1)

    def test_valid_docx_paragraph_table_and_header(self):
        content = docx('<w:p><w:r><w:t>Capacitor replacement</w:t></w:r></w:p>'
                       '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Labor $100</w:t></w:r></w:p></w:tc>'
                       '<w:tc><w:p><w:r><w:t>Parts $200</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',
                       {"word/header1.xml": f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>Proposal header</w:t></w:r></w:p></w:hdr>'})
        response = self.send([("quote.docx", content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
        self.assertEqual(response.status_code, 200, response.text)
        text = self.h.classifier.call_args.args[0]
        for value in ("Capacitor replacement", "Labor $100", "Parts $200", "Proposal header"):
            self.assertIn(value, text)
        self.assertNotIn("PK\x03", text)

    def test_valid_utf8_plain_text_with_bom(self):
        response = self.send([("quote.TXT", b"\xef\xbb\xbf" + BAD.encode(), "text/plain; charset=utf-8")])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(BAD, self.h.classifier.call_args.args[0])

    def test_empty_file(self):
        self.reject([("empty.txt", b"", "text/plain")], 422)

    def test_whitespace_only_file(self):
        self.reject([("blank.txt", b" \r\n\t ", "text/plain")], 422)

    def test_corrupt_pdf_with_pdf_signature(self):
        self.reject([("broken.pdf", b"%PDF-1.7\nBROKEN", "application/pdf")], 422)

    def test_blank_pdf(self):
        self.reject([("blank.pdf", pdf(text=""), "application/pdf")], 422)

    def test_image_only_pdf(self):
        self.reject([("scan.pdf", pdf(text="", image_page=True), "application/pdf")], 422)

    def test_mixed_pdf_does_not_silently_drop_image_only_page(self):
        self.reject([("mixed.pdf", pdf(image_page=True), "application/pdf")], 422)

    def test_password_protected_pdf(self):
        self.reject([("locked.pdf", pdf(encrypted=True), "application/pdf")], 422)

    def test_pdf_page_limit(self):
        self.reject([("long.pdf", pdf(text="", pages=intake.MAX_PDF_PAGES + 1), "application/pdf")], 413)

    def test_corrupt_photos_and_legacy_doc_rejected(self):
        for ext in ("jpg", "jpeg", "png", "doc"):
            with self.subTest(ext=ext):
                self.reject([("quote." + ext, b"synthetic file", "application/octet-stream")], 415 if ext == "doc" else 422)

    def test_unsupported_extension_even_when_content_is_text(self):
        self.reject([("quote.exe", BAD.encode(), "application/octet-stream")], 415)

    def test_pdf_extension_with_text_bytes(self):
        self.reject([("quote.pdf", BAD.encode(), "application/pdf")], 422)

    def test_pdf_renamed_txt(self):
        self.reject([("quote.txt", pdf(), "text/plain")], 415)

    def test_docx_renamed_txt(self):
        self.reject([("quote.txt", docx(), "text/plain")], 415)

    def test_mime_mismatch(self):
        self.reject([("quote.txt", BAD.encode(), "image/png")], 415)

    def test_binary_or_invalid_utf8_text(self):
        for content in (b"Hello\x00proposal", b"Quote\xff\xfe", b"Quote\x7ftext"):
            with self.subTest(content=content):
                self.reject([("quote.txt", content, "text/plain")], 422)

    def test_oversized_file_rejected_before_extraction(self):
        self.assertEqual(intake.MAX_FILE_BYTES, 10 * 1024 * 1024)
        with patch.object(intake, "MAX_FILE_BYTES", 1024), patch.object(main, "extract_text_from_uploaded_file") as extract:
            self.reject([("quote.txt", b"A" * 1025, "text/plain")], 413)
            extract.assert_not_called()

    def test_text_character_limit(self):
        self.reject([("quote.txt", b"A" * (intake.MAX_TEXT_CHARS + 1), "text/plain")], 413)

    def test_combined_text_limit_before_entitlement_consumption(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        self.reject([(f"quote{i}.txt", b"A" * 70000, "text/plain") for i in range(3)], 413)

    def test_tier1_excess_files_before_extraction_or_storage(self):
        with patch.object(main, "extract_text_from_uploaded_file") as extract, \
             patch.object(intake.UploadFile, "write", new_callable=AsyncMock) as spool_write:
            self.reject([("a.txt", b"A", "text/plain"), ("b.txt", b"B", "text/plain")], 400)
            extract.assert_not_called()
            spool_write.assert_not_awaited()

    def test_tier2_excess_files(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        self.reject([(f"{i}.txt", b"Quote", "text/plain") for i in range(4)], 400)

    def test_missing_name_and_email(self):
        for field in ("customer_name", "customer_email"):
            data = {"customer_name": "Offline", "customer_email": "offline@example.invalid"}
            del data[field]
            response = self.send([("q.txt", BAD.encode(), "text/plain")], data=data)
            self.assertEqual(response.status_code, 422)
        self.h.assert_no_paid_work()

    def test_invalid_customer_names(self):
        for name in (" ", "A" * 121, "Name\nInjected", "1234"):
            response = self.send([("q.txt", BAD.encode(), "text/plain")], data={"customer_name": name, "customer_email": "a@example.invalid"})
            self.assertEqual(response.status_code, 422)
        self.h.assert_no_paid_work()

    def test_invalid_customer_email(self):
        for email in ("", "not-email", "a@", "a..b@example.com", ".a@example.com", "a@example", "a@-bad.com", "a@example.com\r\nBcc: x@y.com"):
            response = self.send([("q.txt", BAD.encode(), "text/plain")], data={"customer_name": "Offline", "customer_email": email})
            self.assertEqual(response.status_code, 422, email)
        self.h.assert_no_paid_work()

    def test_valid_unicode_customer_name(self):
        response = self.send([("q.txt", BAD.encode(), "text/plain")], data={"customer_name": "李明", "customer_email": "test@example.invalid"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.h.email.call_args.kwargs["customer_name"], "李明")

    def test_json_customer_aliases_are_resolved_without_default_shadowing(self):
        payload = self.h.payload()
        del payload["customerName"], payload["customerEmail"]
        payload.update(customer_name="Snake Case", customer_email="snake@example.invalid")
        response = self.h.client.post("/analyze", headers=self.h.headers(), json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.h.email.call_args.kwargs["customer_name"], "Snake Case")

    def test_json_missing_or_conflicting_customer_fields_rejected(self):
        for change in ({"customerName": None}, {"customerEmail": None}, {"customer_name": "Different"}, {"customer_email": "different@example.invalid"}):
            response = self.h.submit(**change)
            self.assertIn(response.status_code, (400, 422))
        self.h.assert_no_paid_work()

    def test_unauthenticated_upload_does_not_extract(self):
        with patch.object(main, "extract_text_from_uploaded_file") as extract:
            response = self.send([("q.pdf", pdf(), "application/pdf")], headers={})
            self.assertEqual(response.status_code, 401)
            extract.assert_not_called()
        self.h.assert_no_paid_work()

    def test_wrong_owner_and_ineligible_purchase(self):
        for change in ({"authenticated_customer_id": "other"}, {"payment_eligible": False}):
            self.h.purchase = self.h.make_purchase(**change)
            response = self.send([("q.txt", BAD.encode(), "text/plain")])
            self.assertEqual(response.status_code, 403)
        self.h.assert_no_paid_work()

    def test_tier3_remains_disabled(self):
        self.h.purchase = self.h.make_purchase(tier="tier3")
        self.assertEqual(self.send([("q.txt", BAD.encode(), "text/plain")]).status_code, 503)
        self.h.assert_no_paid_work()

    def test_tier_claim_cannot_upgrade_purchase(self):
        response = self.send([("q.txt", BAD.encode(), "text/plain")], data={
            "customer_name": "Offline", "customer_email": "offline@example.invalid", "package": "tier2"})
        self.assertEqual(response.status_code, 403)
        self.h.assert_no_paid_work()

    def test_second_invalid_file_prevents_entire_analysis(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        self.reject([("valid.txt", BAD.encode(), "text/plain"), ("empty.txt", b"", "text/plain")], 422)

    def test_success_and_extraction_failure_close_spools(self):
        original = starlette.formparsers.SpooledTemporaryFile
        spools = []
        def tracked(*args, **kwargs):
            file = original(*args, **kwargs)
            spools.append(file)
            return file
        with patch.object(starlette.formparsers, "SpooledTemporaryFile", side_effect=tracked):
            self.reject([("large.txt", b"A" * (1024 * 1024 + 1), "text/plain")], 413)
            response = self.send([("valid.txt", BAD.encode(), "text/plain")])
            self.assertEqual(response.status_code, 200)
        self.assertTrue(spools)
        self.assertTrue(all(file.closed for file in spools))

    def test_truncated_multipart_rejected_and_spools_closed(self):
        original = starlette.formparsers.SpooledTemporaryFile
        spools = []
        def tracked(*args, **kwargs):
            file = original(*args, **kwargs); spools.append(file); return file
        body = b'--boundary\r\nContent-Disposition: form-data; name="files"; filename="q.txt"\r\nContent-Type: text/plain\r\n\r\nINCOMPLETE'
        with patch.object(starlette.formparsers, "SpooledTemporaryFile", side_effect=tracked):
            response = self.h.client.post("/upload", headers={**self.h.headers(), "Content-Type": "multipart/form-data; boundary=boundary"}, content=body)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(all(file.closed for file in spools))
        self.h.assert_no_paid_work()

    def test_malformed_multipart(self):
        response = self.h.client.post("/upload", headers={**self.h.headers(), "Content-Type": "multipart/form-data"}, content=b"invalid")
        self.assertEqual(response.status_code, 400)
        self.h.assert_no_paid_work()

    def test_missing_quote_files(self):
        response = self.send([], headers={**self.h.headers(), "Content-Type": "multipart/form-data; boundary=empty"})
        self.assertIn(response.status_code, (400, 422))
        self.h.assert_no_paid_work()

    def test_file_url_is_disabled_even_with_valid_extracted_text(self):
        for url in ("https://example.invalid/quote.pdf", "http://127.0.0.1/private", "file:///private/file", "not a URL"):
            response = self.h.submit(files=[{"fileName": "quote.txt", "extractedText": BAD, "downloadUrl": url}])
            self.assertEqual(response.status_code, 400)
            self.assertIn("File URLs are not supported", response.text)
        self.h.assert_no_paid_work()

    def test_file_url_alias_also_disabled(self):
        self.assertEqual(self.h.submit(files=[{"fileUrl": "https://example.invalid/file"}]).status_code, 400)
        self.h.assert_no_paid_work()

    def test_invalid_json_values_are_not_echoed(self):
        response = self.h.submit(files="SYNTHETIC_PRIVATE_CONTENT")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("SYNTHETIC_PRIVATE_CONTENT", response.text)
        self.h.assert_no_paid_work()

    def test_sensitive_source_analysis_and_filename_are_not_logged(self):
        marker = "SYNTHETIC_PRIVATE_CONTENT"
        raw = saved_response()
        raw.contractor_vetting = marker
        raw.technical_assessments[0].documented_evidence.append(marker)
        self.h.analysis.side_effect = lambda **kwargs: raw.model_copy(deep=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            response = self.send([(marker + ".txt", (BAD + "\n" + marker).encode(), "text/plain")])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(marker, output.getvalue())

    def test_concurrent_uploads_with_identical_names_remain_isolated(self):
        self.h.purchase = self.h.make_purchase(submission_limit=2)
        def submit(marker):
            return self.send([("same.txt", (BAD + "\n" + marker).encode(), "text/plain")]).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, ("ONLY_ALPHA", "ONLY_BETA")))
        self.assertEqual(statuses, [200, 200])
        sources = [call.args[0] for call in self.h.classifier.call_args_list]
        self.assertEqual(sum("ONLY_ALPHA" in source for source in sources), 1)
        self.assertEqual(sum("ONLY_BETA" in source for source in sources), 1)
        self.assertFalse(any("ONLY_ALPHA" in source and "ONLY_BETA" in source for source in sources))
        self.assertEqual(list(self.h.uploads.iterdir()), [])

    def test_single_quote_html_json_and_pdf_controls_preserved(self):
        response = self.send([("quote.txt", BAD.encode(), "text/plain")])
        self.assertEqual(response.status_code, 200)
        final = self.h.email.call_args.kwargs["analysis"]
        self.assertEqual(response.text, main.build_report_html(final, 1))
        self.assertNotIn("<h2>Quote Comparison</h2>", response.text)
        self.assertIn('onclick="window.print()"', response.text)

    def test_multi_quote_comparison_preserved(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        response = self.send([("a.txt", BAD.encode(), "text/plain"), ("b.txt", BAD.encode(), "text/plain")])
        self.assertEqual(response.status_code, 200)
        self.assertIn("<h2>Quote Comparison</h2>", response.text)
        self.assertEqual(response.text, main.build_report_html(self.h.email.call_args.kwargs["analysis"], 2))

    def test_safe_filename_required(self):
        self.reject([("../quote.txt", BAD.encode(), "text/plain")], 400)

    def test_docx_corrupt_archive(self):
        self.reject([("quote.docx", b"PK\x03\x04broken", "application/octet-stream")], 422)

    def test_docx_archive_without_document_structure(self):
        self.reject([("quote.docx", docx(types="<Types/>"), "application/octet-stream")], 422)

    def test_docx_xml_entities_rejected(self):
        content = docx(extras={"word/header1.xml": '<!DOCTYPE root [<!ENTITY x "EXPAND">]><root>&x;</root>'})
        self.reject([("quote.docx", content, "application/octet-stream")], 422)

    def test_docx_expansion_limit(self):
        content = docx(extras={"large.bin": "A" * 100000})
        self.reject([("quote.docx", content, "application/octet-stream")], 413)

    def test_empty_docx(self):
        self.reject([("quote.docx", docx(body="<w:p/>"), "application/octet-stream")], 422)

    def test_docx_unresolved_edits_do_not_become_proposal_text(self):
        content = docx(body='<w:p><w:del><w:r><w:t>Obsolete price $50</w:t></w:r></w:del>'
                            '<w:r><w:t>Replacement total $300</w:t></w:r></w:p>')
        self.reject([("quote.docx", content, "application/octet-stream")], 422)

    def test_filled_pdf_form_requires_flattening_before_analysis(self):
        with fitz.open(stream=pdf(), filetype="pdf") as doc:
            field = fitz.Widget()
            field.field_name = "Repair total"
            field.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            field.field_value = "$300"
            field.rect = fitz.Rect(30, 700, 200, 740)
            doc[0].add_widget(field)
            content = doc.tobytes()
        response = self.reject([("quote.pdf", content, "application/pdf")], 422)
        self.assertIn("flattened", response.text)

    def test_busy_parser_does_not_consume_purchase(self):
        with patch.object(intake, "_EXTRACTION_SLOTS") as slots:
            slots.acquire.return_value = False
            response = self.reject([("quote.pdf", pdf(), "application/pdf")], 503)
            self.assertIn("try again", response.text)
            slots.release.assert_not_called()

    def test_processing_timeout_is_clear_and_does_not_consume(self):
        with patch.object(intake.subprocess, "run", side_effect=subprocess.TimeoutExpired("offline-parser", 10)):
            response = self.reject([("quote.pdf", pdf(), "application/pdf")], 422)
            self.assertIn("too long", response.text)
        self.assertEqual(self.send([("valid.pdf", pdf(), "application/pdf")]).status_code, 200)

    def test_processing_crash_is_clear_and_does_not_consume(self):
        with patch.object(intake.subprocess, "run", return_value=SimpleNamespace(returncode=-9, stdout=b"")):
            self.reject([("quote.pdf", pdf(), "application/pdf")], 422)

    def test_full_request_size_limit(self):
        with patch.object(intake, "MAX_UPLOAD_BODY", 256):
            response = self.send([("quote.txt", b"A" * 500, "text/plain")])
        self.assertEqual(response.status_code, 413)
        self.h.assert_no_paid_work()

    def test_chunked_request_size_limit_without_content_length(self):
        with patch.object(intake, "MAX_JSON_BODY", 256):
            response = self.h.client.post("/analyze", headers={**self.h.headers(), "Content-Type": "application/json"},
                content=iter([b"{" + b" " * 200, b" " * 200 + b"}"]))
        self.assertEqual(response.status_code, 413)
        self.h.assert_no_paid_work()

    def test_upload_page_advertises_only_supported_formats_and_limits(self):
        response = self.h.client.get("/upload-page", headers=self.h.headers())
        self.assertEqual(response.status_code, 200)
        self.assertIn('/static/quote_upload.js', response.text)
        self.assertIn('JPG, JPEG, PNG, HEIC or HEIF', response.text)
        self.assertIn("10 MiB", response.text)
        self.assertIn("10 photo/scanned pages", response.text)
        self.assertIn('data-quote-limit="1"', response.text)


if __name__ == "__main__":
    unittest.main()
