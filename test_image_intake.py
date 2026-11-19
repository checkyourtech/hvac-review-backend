"""Synthetic photos/scans, real isolated decoders, mocked vision and HVAC calls."""
import base64
import contextlib
import io
import json
import sys
import threading
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import fitz
import httpx
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
from fastapi import HTTPException

import document_intake as intake
import image_transcription as vision
import main
import test_purchase_security as security
from test_document_intake import docx, pdf


TEXT = ("Example Contractor — proposal A\nManufacturer: Example. Model: AB-1234-X.\n"
        "Replace indoor sensor. Total $300. Parts $100; labor $200. Warranty: 1 year.\n"
        "Measured resistance: 12.3 kOhm. Verify operation. Excludes drywall repair.")

CLEAR_READABILITY = dict(text_obscured=False, text_cut_off=False,
                         text_blurred=False, content_inferred=False)


def photo(format="JPEG", marker="Page A", size=(900, 1200), orientation=None):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    for index, text in enumerate((marker, *TEXT.splitlines())):
        draw.text((30, 30 + index * 90), text, fill="black", font=font)
    output = io.BytesIO()
    options = {}
    if orientation:
        exif = Image.Exif(); exif[274] = orientation; exif[270] = "SYNTHETIC_PRIVATE_METADATA"
        options["exif"] = exif
    if format == "HEIF":
        from pillow_heif import register_heif_opener
        register_heif_opener(thumbnails=False)
    image.save(output, format=format, **options)
    return output.getvalue()


def scan(pages=1, mixed=False):
    with fitz.open() as doc:
        if mixed:
            doc.new_page().insert_text((30, 30), "Native text page: total $300.")
        for index in range(pages):
            page = doc.new_page()
            page.insert_image(page.rect, stream=photo(marker=f"Scan page {index + 1}"))
        return doc.tobytes()


def manifest(*groups):
    return json.dumps([{"pages": [{"file": i, "rotation": rotation} if isinstance(i, int) else i
                                 for i in indexes]} for indexes, rotation in ((g, 0) for g in groups)])


class ImageIntakeTests(unittest.TestCase):
    def setUp(self):
        self.h = security.PurchaseSecurityTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.app.add_middleware(intake.IntakeBodyLimit)
        self.h.app.exception_handlers.update(main.app.exception_handlers)
        self.cache = vision.TranscriptionCache()
        self.addCleanup(self.cache.close)
        self.h.stack.enter_context(patch.object(main, "image_text_cache", self.cache))
        self.reader = self.h.stack.enter_context(patch.object(main, "transcribe_page", return_value=TEXT))

    def send(self, files, groups=None, headers=None, **data):
        fields = {"customer_name": "Offline Customer", "customer_email": "offline@example.invalid", **data}
        if groups is not None:
            fields["quote_groups"] = groups
        return self.h.client.post("/upload", headers=self.h.headers() if headers is None else headers,
            data=fields, files=[("files", (name, content, "application/octet-stream")) for name, content in files])

    def assert_rejected(self, response, status, calls=0):
        self.assertEqual(response.status_code, status, response.text)
        self.h.assert_no_paid_work()
        self.assertEqual(self.reader.call_count, calls)
        self.assertEqual(list(self.h.uploads.iterdir()), [])
        self.assertNotIn("Traceback", response.text)

    def test_jpg_proposal(self):
        response = self.send([("proposal.jpg", photo())], manifest([0]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(TEXT, self.h.classifier.call_args.args[0])
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(self.h.usage(), 1)

    def test_jpeg_extension(self):
        self.assertEqual(self.send([("proposal.jpeg", photo())], manifest([0])).status_code, 200)

    def test_png_proposal(self):
        self.assertEqual(self.send([("proposal.png", photo("PNG"))], manifest([0])).status_code, 200)

    def test_real_heic_conversion(self):
        self.assertEqual(self.send([("proposal.heic", photo("HEIF"))], manifest([0])).status_code, 200)
        normalized = base64.b64decode(self.reader.call_args.args[1])
        with Image.open(io.BytesIO(normalized)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (900, 1200))

    def test_real_heif_conversion(self):
        self.assertEqual(self.send([("proposal.heif", photo("HEIF"))], manifest([0])).status_code, 200)

    def test_scanned_pdf(self):
        self.assertEqual(self.send([("scan.pdf", scan(2))]).status_code, 200)
        self.assertEqual(self.reader.call_count, 2)
        source = self.h.classifier.call_args.args[0]
        self.assertIn("File Name: scan.pdf (page 1, image transcription)", source)
        self.assertIn("File Name: scan.pdf (page 2, image transcription)", source)
        self.assertEqual(self.h.usage(), 1)

    def test_mixed_pdf_keeps_native_and_scanned_pages(self):
        self.assertEqual(self.send([("mixed.pdf", scan(mixed=True))]).status_code, 200)
        source = self.h.classifier.call_args.args[0]
        self.assertIn("Native text page", source)
        self.assertIn(TEXT, source)
        self.assertEqual(self.reader.call_count, 1)

    def test_multi_page_photo_proposal_is_one_quote(self):
        files = [("one.jpg", photo()), ("two.png", photo("PNG", marker="Page B"))]
        response = self.send(files, manifest([0, 1]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.h.email.call_args.kwargs["file_names"]), 1)
        self.assertNotIn("<h2>Quote Comparison</h2>", response.text)
        self.assertEqual(self.h.usage(), 1)

    def test_three_quotes_with_two_pages_each(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        files = [(f"page{i}.jpg", photo(marker=f"Page {i}")) for i in range(6)]
        response = self.send(files, manifest([0, 1], [2, 3], [4, 5]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.h.email.call_args.kwargs["file_names"]), 3)
        self.assertIn("<h2>Quote Comparison</h2>", response.text)
        self.assertEqual(self.reader.call_count, 6)
        self.assertEqual(self.h.usage(), 1)

    def test_tier1_rejects_two_logical_quotes_before_spool_writes(self):
        with patch.object(intake.UploadFile, "write") as write:
            self.assert_rejected(self.send([("a.jpg", photo()), ("b.jpg", photo(marker="B"))], manifest([0], [1])), 400)
            write.assert_not_called()

    def test_tier2_rejects_four_logical_quotes(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        self.assert_rejected(self.send([(f"{i}.jpg", photo(marker=str(i))) for i in range(4)], manifest([0], [1], [2], [3])), 400)

    def test_multiple_photos_without_grouping_are_not_separate_quotes(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        self.assert_rejected(self.send([("a.jpg", photo()), ("b.jpg", photo(marker="B"))]), 400)

    def test_manifest_must_assign_every_page_once(self):
        files = [("a.jpg", photo()), ("b.jpg", photo(marker="B"))]
        for groups in (manifest([0, 0]), manifest([0]), manifest([0, 2]), "not JSON", "{}", "[]"):
            with self.subTest(groups=groups):
                self.assert_rejected(self.send(files, groups), 400)

    def test_invalid_manifest_types_and_rotation(self):
        for page in ({"file": True, "rotation": 0}, {"file": 0, "rotation": 45}, {"file": 0}, {"file": "0", "rotation": 0}):
            self.assert_rejected(self.send([("a.jpg", photo())], json.dumps([{"pages": [page]}])), 400)

    def test_blank_photo_rejected_locally(self):
        out = io.BytesIO(); Image.new("RGB", (900, 1200), "white").save(out, "PNG")
        self.assert_rejected(self.send([("blank.png", out.getvalue())], manifest([0])), 422)

    def test_blurry_or_partial_extraction_is_never_analysis_evidence(self):
        self.reader.side_effect = HTTPException(422, intake.BETTER_PHOTO)
        response = self.send([("blur.jpg", photo())], manifest([0]))
        self.assert_rejected(response, 422, calls=1)
        self.assertEqual(response.json()["detail"]["file"], 0)
        self.assertIn("Retake", response.text)

    def test_empty_transcription_stops_all_analysis(self):
        self.reader.return_value = "  "
        self.assert_rejected(self.send([("empty.jpg", photo())], manifest([0])), 422, calls=1)

    def test_rotated_exif_is_corrected_and_metadata_removed(self):
        self.assertEqual(self.send([("rotated.jpg", photo(orientation=6))], manifest([0])).status_code, 200)
        data = base64.b64decode(self.reader.call_args.args[1])
        self.assertNotIn(b"SYNTHETIC_PRIVATE_METADATA", data)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (1200, 900))
            self.assertFalse(image.getexif())

    def test_customer_rotation_applied_to_actual_image(self):
        groups = json.dumps([{"pages": [{"file": 0, "rotation": 90}]}])
        self.assertEqual(self.send([("rotate.png", photo("PNG"))], groups).status_code, 200)
        with Image.open(io.BytesIO(base64.b64decode(self.reader.call_args.args[1]))) as image:
            self.assertEqual(image.size, (1200, 900))

    def test_corrupt_images_never_reach_vision(self):
        for name, content in (("bad.jpg", b"\xff\xd8BROKEN"), ("bad.png", b"\x89PNG\r\n\x1a\nBROKEN"), ("bad.heic", b"\x00\x00\x00\x18ftypBROKEN")):
            self.assert_rejected(self.send([(name, content)], manifest([0])), 422)

    def test_wrong_image_content_format(self):
        self.assert_rejected(self.send([("wrong.png", photo())], manifest([0])), 422)

    def test_image_resolution_too_low(self):
        self.assert_rejected(self.send([("tiny.jpg", photo(size=(200, 300)))], manifest([0])), 422)

    def test_excessive_image_dimensions(self):
        out = io.BytesIO(); Image.new("RGB", (12001, 601), "white").save(out, "PNG")
        self.assert_rejected(self.send([("huge.png", out.getvalue())], manifest([0])), 413)

    def test_too_many_attachments(self):
        files = [(f"{i}.jpg", b"unparsed") for i in range(11)]
        self.assert_rejected(self.send(files, manifest(list(range(11)))), 400)

    def test_scanned_page_count_limit(self):
        self.assert_rejected(self.send([("long.pdf", scan(11))]), 413)

    def test_file_size_limit_stops_image_processing(self):
        with patch.object(intake, "MAX_FILE_BYTES", 100):
            self.assert_rejected(self.send([("large.jpg", photo())], manifest([0])), 413)

    def test_all_files_prevalidated_before_paid_vision(self):
        self.assert_rejected(self.send([("valid.jpg", photo()), ("broken.pdf", b"bad")], manifest([0, 1])), 422)

    def test_authentication_required_for_photos(self):
        self.assert_rejected(self.send([("a.jpg", photo())], manifest([0]), headers={}), 401)

    def test_wrong_owner_no_photo_processing(self):
        self.h.purchase = self.h.make_purchase(authenticated_customer_id="other")
        self.assert_rejected(self.send([("a.jpg", photo())], manifest([0])), 403)

    def test_tier3_disabled_for_photos(self):
        self.h.purchase = self.h.make_purchase(tier="tier3")
        self.assert_rejected(self.send([("a.jpg", photo())], manifest([0])), 503)

    def test_tier_claim_cannot_override_group_limit(self):
        self.assert_rejected(self.send([("a.jpg", photo())], manifest([0]), package="tier2"), 403)

    def test_independent_source_pages_keep_order_and_details(self):
        self.reader.side_effect = [TEXT, "Page two: 5-year parts warranty; permit included."]
        response = self.send([("first.jpg", photo()), ("second.jpg", photo(marker="second"))], manifest([1, 0]))
        self.assertEqual(response.status_code, 200)
        source = self.h.classifier.call_args.args[0]
        self.assertLess(source.index("second.jpg"), source.index("first.jpg"))
        for detail in ("AB-1234-X", "$300", "12.3 kOhm", "Excludes drywall", "5-year"):
            self.assertIn(detail, source)
        self.assertEqual(source.count("AB-1234-X"), 1)

    def test_existing_native_documents_avoid_vision(self):
        response = self.send([("text.pdf", pdf()), ("quote.docx", docx()), ("notes.txt", b"Warranty: one year.")], manifest([0, 1, 2]))
        self.assertEqual(response.status_code, 200, response.text)
        self.reader.assert_not_called()

    def test_duplicate_photo_rejected_before_paid_extraction(self):
        content = photo()
        self.assert_rejected(self.send([("a.jpg", content), ("copy.jpg", content)], manifest([0, 1])), 400)

    def test_replacement_reuses_successful_pages(self):
        first, failed, replacement = photo(), photo(marker="bad page"), photo(marker="replacement")
        self.reader.side_effect = [TEXT, HTTPException(422, intake.BETTER_PHOTO), "Replacement page: permits included."]
        self.assert_rejected(self.send([("a.jpg", first), ("b.jpg", failed)], manifest([0, 1])), 422, calls=2)
        response = self.send([("a.jpg", first), ("new.jpg", replacement)], manifest([0, 1]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.reader.call_count, 3)
        self.assertEqual(self.h.usage(), 1)

    def test_same_unreadable_page_is_not_charged_again(self):
        self.reader.side_effect = HTTPException(422, intake.BETTER_PHOTO)
        files = [("bad.jpg", photo())]
        for _ in range(2):
            self.assert_rejected(self.send(files, manifest([0])), 422, calls=1)

    def test_partial_extraction_does_not_deliver_incomplete_report(self):
        self.reader.side_effect = [TEXT, HTTPException(502, "Extraction unavailable")]
        self.assert_rejected(self.send([("a.jpg", photo()), ("b.jpg", photo(marker="B"))], manifest([0, 1])), 502, calls=2)

    def test_customer_contents_and_images_are_not_logged(self):
        self.reader.return_value = TEXT + " SYNTHETIC_PRIVATE_CONTENT"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            response = self.send([("SYNTHETIC_PRIVATE_CONTENT.jpg", photo())], manifest([0]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("SYNTHETIC_PRIVATE_CONTENT", output.getvalue())
        self.assertNotIn("data:image", output.getvalue())

    def test_no_upload_files_retained_after_image_failure(self):
        self.reader.side_effect = HTTPException(422, intake.BETTER_PHOTO)
        self.assert_rejected(self.send([("bad.jpg", photo())], manifest([0])), 422, calls=1)

    def test_pdf_image_and_text_layer_are_not_duplicated(self):
        with fitz.open(stream=scan(), filetype="pdf") as doc:
            doc[0].insert_text((30, 30), "A native header should not be appended to whole-page OCR.")
            content = doc.tobytes()
        self.assertEqual(self.send([("quote.pdf", content)]).status_code, 200)
        self.assertEqual(self.reader.call_count, 1)
        source = self.h.classifier.call_args.args[0]
        self.assertEqual(source.count(TEXT), 1)
        self.assertNotIn("A native header", source)


class VisionBoundaryTests(unittest.TestCase):
    def cache(self):
        cache = vision.TranscriptionCache()
        self.addCleanup(cache.close)
        return cache

    def client(self, **changes):
        parsed = vision.PageTranscript(readability=CLEAR_READABILITY, status="readable", complete=True, text=TEXT, uncertain_regions=[])
        choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(parsed=parsed, refusal=None))
        for name, value in changes.items():
            if name == "finish_reason": choice.finish_reason = value
            else: setattr(choice.message, name, value)
        client = Mock()
        client.with_options.return_value.beta.chat.completions.parse.return_value = SimpleNamespace(choices=[choice])
        return client

    def test_bounded_high_detail_transcription_uses_existing_model(self):
        client = self.client()
        self.assertEqual(vision.transcribe_page(client, "synthetic-base64"), TEXT)
        client.with_options.assert_called_once_with(timeout=30, max_retries=0)
        args = client.with_options.return_value.beta.chat.completions.parse.call_args.kwargs
        self.assertEqual(args["model"], "gpt-4o-mini")
        self.assertEqual(args["max_completion_tokens"], 8192)
        self.assertEqual(args["messages"][1]["content"][1]["image_url"]["detail"], "high")
        self.assertIn("Do not summarize", args["messages"][0]["content"])

    def test_uncertainty_in_critical_fields_is_rejected(self):
        for changes in ({"status": "retake"}, {"complete": False}, {"uncertain_regions": ["Model number"]}, {"text": ""}):
            values = dict(readability=CLEAR_READABILITY, status="readable", complete=True, text=TEXT, uncertain_regions=[]); values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(HTTPException) as error:
                vision.transcribe_page(self.client(parsed=vision.PageTranscript(**values)), "image")
            self.assertEqual(error.exception.status_code, 422)

    def test_truncated_transcription_is_not_accepted(self):
        with self.assertRaises(HTTPException) as error:
            vision.transcribe_page(self.client(finish_reason="length"), "image")
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(error.exception.diagnostic["category"], "response_length")

    def test_refusal_is_not_proposal_text(self):
        with self.assertRaises(HTTPException) as error:
            vision.transcribe_page(self.client(refusal="declined"), "image")
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(error.exception.diagnostic["category"], "response_refusal")

    def test_missing_structured_result_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            vision.transcribe_page(self.client(parsed=None), "image")
        self.assertEqual(error.exception.status_code, 502)

    def test_provider_failure_no_retry_or_private_error_details(self):
        client = self.client()
        parse = client.with_options.return_value.beta.chat.completions.parse
        parse.side_effect = RuntimeError("PRIVATE_PROVIDER_BODY")
        with self.assertRaises(HTTPException) as error:
            vision.transcribe_page(client, "image")
        self.assertEqual(error.exception.status_code, 502)
        self.assertNotIn("PRIVATE_PROVIDER", error.exception.detail)
        self.assertEqual(parse.call_count, 1)

    def sdk(self, handler):
        transport = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(transport.close)
        client = OpenAI(api_key="SYNTHETIC_PRIVATE_KEY", base_url="https://api.openai.com/v1",
                        max_retries=0, http_client=transport)
        self.addCleanup(client.close)
        return client

    def completion(self, content=None, finish="stop"):
        return {"id": "synthetic-completion", "object": "chat.completion", "created": 1,
                "model": "gpt-4o-mini", "choices": [{"index": 0, "finish_reason": finish,
                "message": {"role": "assistant", "content": content if content is not None else json.dumps({
                    "readability": CLEAR_READABILITY,
                    "status": "readable", "complete": True, "text": TEXT, "uncertain_regions": []})}}]}

    def test_real_sdk_serializes_and_parses_current_vision_schema_offline(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=self.completion())
        result = vision.transcribe_page(self.sdk(respond), "SYNTHETIC_PRIVATE_IMAGE")
        self.assertEqual(result, TEXT)
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://api.openai.com/v1/chat/completions")
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "gpt-4o-mini")
        self.assertEqual(body["max_completion_tokens"], 8192)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["messages"][1]["content"][1]["image_url"]["url"],
                         "data:image/jpeg;base64,SYNTHETIC_PRIVATE_IMAGE")

    def test_provider_status_categories_are_distinct_without_sensitive_details(self):
        cases = (
            (401, "invalid_api_key", "invalid_request_error", None, "api_authentication", 503),
            (429, "insufficient_quota", "insufficient_quota", None, "api_billing", 503),
            (429, "project_spend_limit_exceeded", "insufficient_quota", None, "api_billing", 503),
            (429, None, "insufficient_quota", None, "api_billing", 503),
            (429, "rate_limit_exceeded", "rate_limit_error", None, "api_rate_limit", 429),
            (403, None, "permission_error", None, "api_access", 503),
            (404, "model_not_found", "invalid_request_error", None, "api_model", 502),
            (400, None, "invalid_request_error", "model", "api_model", 502),
            (400, "invalid_image_format", "invalid_request_error", None, "api_image", 502),
            (400, "unsupported_parameter", "invalid_request_error", "response_format", "api_request", 502),
            (500, "server_error", "server_error", None, "api_service", 502),
        )
        for status, code, kind, param, category, public_status in cases:
            with self.subTest(category=category, code=code):
                requests = []
                def respond(request):
                    requests.append(request)
                    return httpx.Response(status, headers={"x-request-id": "req_" + "a" * 32}, json={"error": {
                        "message": "SYNTHETIC_PRIVATE_BODY", "code": code, "type": kind, "param": param}})
                with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr, \
                        self.assertRaises(vision.TranscriptionError) as error:
                    vision.transcribe_page(self.sdk(respond), "SYNTHETIC_PRIVATE_IMAGE")
                exc = error.exception
                self.assertEqual(exc.status_code, public_status)
                self.assertEqual(exc.diagnostic["category"], category)
                self.assertEqual(exc.diagnostic["provider_status"], status)
                self.assertEqual(exc.diagnostic["request_id"], "req_" + "a" * 32)
                self.assertEqual(len(requests), 1)
                self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(exc.diagnostic) + exc.detail + stdout.getvalue() + stderr.getvalue())

    def test_unknown_provider_fields_cannot_leak_into_diagnostics(self):
        def respond(request):
            return httpx.Response(400, headers={"x-request-id": "SYNTHETIC_PRIVATE_HEADER"}, json={"error": {
                "message": "SYNTHETIC_PRIVATE_BODY", "code": "SYNTHETIC_PRIVATE_CODE", "param": "SYNTHETIC_PRIVATE_PARAM"}})
        with self.assertRaises(vision.TranscriptionError) as error:
            vision.transcribe_page(self.sdk(respond), "SYNTHETIC_PRIVATE_IMAGE")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(error.exception.diagnostic) + error.exception.detail)
        self.assertNotIn("provider_code", error.exception.diagnostic)
        self.assertNotIn("request_id", error.exception.diagnostic)

    def test_sdk_timeout_and_connection_failure_are_not_retakes(self):
        for exception, category, status in ((httpx.ReadTimeout, "api_timeout", 504), (httpx.ConnectError, "api_connection", 503)):
            with self.subTest(category=category):
                requests = []
                def respond(request):
                    requests.append(request)
                    raise exception("SYNTHETIC_PRIVATE_NETWORK", request=request)
                with self.assertRaises(vision.TranscriptionError) as error:
                    vision.transcribe_page(self.sdk(respond), "image")
                self.assertEqual(error.exception.diagnostic["category"], category)
                self.assertEqual(error.exception.status_code, status)
                self.assertNotIn("provider_status", error.exception.diagnostic)
                self.assertEqual(len(requests), 1)

    def test_sdk_parse_length_and_filter_errors_are_not_retakes(self):
        for content, finish, category in (("{invalid SYNTHETIC_PRIVATE_JSON", "stop", "response_parse"),
                                          ("partial", "length", "response_length"),
                                          (None, "content_filter", "response_refusal")):
            with self.subTest(category=category), self.assertRaises(vision.TranscriptionError) as error:
                vision.transcribe_page(self.sdk(lambda request: httpx.Response(200, json=self.completion(content, finish))), "image")
            self.assertEqual(error.exception.status_code, 502)
            self.assertEqual(error.exception.diagnostic["category"], category)
            self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(error.exception.diagnostic) + error.exception.detail)

    def test_provider_errors_are_not_cached_as_unreadable_images(self):
        cache = self.cache()
        purchase = security.PurchaseSecurityTests().make_purchase()
        for category in vision.TRANSCRIPTION_ERRORS:
            reader = Mock(side_effect=vision.TranscriptionError(category))
            with self.subTest(category=category), self.assertRaises(vision.TranscriptionError):
                cache.read(purchase, "image", reader)
            self.assertFalse(cache.entries)

    def test_cached_text_is_scoped_to_verified_owner_and_purchase(self):
        cache, reader = self.cache(), Mock(return_value=TEXT)
        purchase = security.PurchaseSecurityTests().make_purchase()
        for identity in (purchase, purchase, purchase.model_copy(update={"purchase_id": "another"}),
                         purchase.model_copy(update={"owner_customer_id": "another"})):
            cache.read(identity, "image", reader)
        self.assertEqual(reader.call_count, 3)

    def test_expired_cache_reextracts_without_retaining_documents_forever(self):
        cache, reader = self.cache(), Mock(return_value=TEXT)
        purchase = security.PurchaseSecurityTests().make_purchase()
        cache.read(purchase, "image", reader)
        with patch.object(vision.time, "time", return_value=time.time() + cache.TTL + 1):
            cache.read(purchase, "image", reader)
        self.assertEqual(reader.call_count, 2)

    def test_extraction_attempt_budget_cannot_spend_indefinitely(self):
        cache, reader = self.cache(), Mock(return_value=TEXT)
        cache.MAX_ATTEMPTS = 1
        purchase = security.PurchaseSecurityTests().make_purchase()
        cache.read(purchase, "first", reader)
        with self.assertRaises(HTTPException) as error:
            cache.read(purchase, "second", reader)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(reader.call_count, 1)

    def test_cache_is_bounded_and_has_no_image_payloads(self):
        cache, reader = self.cache(), Mock(return_value=TEXT)
        cache.MAX_ENTRIES = 2
        purchase = security.PurchaseSecurityTests().make_purchase()
        for index in range(3): cache.read(purchase, f"raw image {index}", reader)
        self.assertEqual(len(cache.entries), 2)
        self.assertNotIn("raw image", str(cache.entries))

    def test_expired_private_text_is_removed_without_another_request(self):
        cache = self.cache()
        purchase = security.PurchaseSecurityTests().make_purchase()
        cache.read(purchase, "image", lambda _: TEXT)
        cache.cleanup_timer.cancel()
        with patch.object(vision.time, "time", return_value=time.time() + cache.TTL + 1):
            cache._expire()
        self.assertFalse(cache.entries)
        self.assertIsNone(cache.cleanup_timer)

    def test_concurrent_same_purchase_does_not_duplicate_vision_work(self):
        cache, started, release = self.cache(), threading.Event(), threading.Event()
        purchase = security.PurchaseSecurityTests().make_purchase()
        def slow(image):
            started.set()
            release.wait(3)
            return TEXT
        reader = Mock(side_effect=slow)
        thread = threading.Thread(target=cache.read, args=(purchase, "image", reader))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            with self.assertRaises(HTTPException) as error:
                cache.read(purchase, "image", reader)
            self.assertEqual(error.exception.status_code, 503)
        finally:
            release.set(); thread.join(3)
        self.assertEqual(cache.read(purchase, "image", reader), TEXT)
        self.assertEqual(reader.call_count, 1)

    def test_heic_codec_unavailable_has_clear_conversion_fallback(self):
        content = photo("HEIF")
        with patch.dict(sys.modules, {"pillow_heif": None}), self.assertRaises(HTTPException) as error:
            intake._image_page(content, ".heic")
        self.assertEqual(error.exception.status_code, 415)
        self.assertIn("JPG or PNG", error.exception.detail)


if __name__ == "__main__":
    unittest.main()
