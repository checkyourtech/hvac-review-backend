"""Offline checks for the local acceptance command, with real photo decoders."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

import main
import image_transcription as vision
import mobile_photo_acceptance as acceptance
from document_intake import BETTER_PHOTO
from test_image_intake import photo


class MobileAcceptanceTests(unittest.TestCase):
    def setUp(self):
        # Decoder subprocess tests should not depend on host wall-clock jumps.
        clock = patch("time.time", return_value=time.time())
        clock.start()
        self.addCleanup(clock.stop)

    @classmethod
    def setUpClass(cls):
        # These are decoder fixtures, not claimed cellphone/OCR accuracy tests.
        cls.photos = dict(zip(acceptance.FILENAMES, (
            photo(marker="Acceptance page one"), photo(marker="Acceptance page two"),
            photo(marker="Acceptance unreadable control"))))

    def reader(self, *extra):
        return Mock(side_effect=[*acceptance.PAGES, *extra, HTTPException(422, BETTER_PHOTO)])

    def test_real_jpg_jpeg_preflight_never_calls_model(self):
        with patch.object(main.client.beta.chat.completions, "parse") as parse:
            result = acceptance.run_acceptance(main, self.photos)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["api_calls"], 0)
        self.assertIn("UNVERIFIED", result["accuracy"])
        self.assertTrue(result["checks"]["proposal_group_one_logical_quote"])
        parse.assert_not_called()

    def test_ground_truth_and_unreadable_control_pass_before_analysis(self):
        reader = self.reader()
        result = acceptance.run_acceptance(main, self.photos, reader)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["api_calls"], 3)
        self.assertEqual(result["hvac_analysis"], "NOT RUN")
        self.assertTrue(result["checks"]["no_analysis_reservation"])
        self.assertEqual(reader.call_count, 3)

    def test_real_heic_conversion_with_mock_transcription(self):
        photos = {**self.photos, acceptance.HEIC_FILENAME: photo("HEIF", marker="Acceptance HEIC")}
        result = acceptance.run_acceptance(main, photos, self.reader(acceptance.PAGES[0]))
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["api_calls"], 4)
        self.assertTrue(result["checks"]["heic_page1_models"])

    def test_native_heic_only_pages_and_uppercase_control(self):
        names = ("proposal_page_1.heic", "proposal_page_2.heic", "unreadable_page_2.HEIC")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for index, name in enumerate(names):
                (directory / name).write_bytes(photo("HEIF", marker=f"Native HEIC {index}"))
            photos = acceptance.load_photos(directory)
            self.assertEqual(tuple(photos), names)
            self.assertEqual(tuple(acceptance.load_photos(directory, include_heic=True)), names)
            preflight = acceptance.run_acceptance(main, photos)
            result = acceptance.run_acceptance(main, photos, self.reader())
        self.assertTrue(preflight["passed"], preflight)
        self.assertEqual(preflight["api_calls"], 0)
        self.assertEqual(preflight["maximum_api_calls"], 3)
        self.assertTrue(preflight["checks"]["proposal_group_one_logical_quote"])
        self.assertEqual(preflight["results"][0]["files"], list(names[:2]))
        self.assertEqual(preflight["results"][1]["files"], [names[2]])
        self.assertEqual(preflight["results"][1]["expected"], "retake")
        self.assertTrue(all(case["decoded"] for case in preflight["results"]))
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["checks"]["unreadable_requires_retake"])
        self.assertEqual(result["api_calls"], 3)

    def test_mixed_jpg_jpeg_png_extensions_preserved(self):
        names = ("proposal_page_1.JPG", "proposal_page_2.JPEG", "unreadable_page_2.PNG")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for index, name in enumerate(names):
                (directory / name).write_bytes(photo("PNG" if name.endswith("PNG") else "JPEG", marker=f"Mixed {index}"))
            photos = acceptance.load_photos(directory)
            self.assertEqual(tuple(photos), names)
            result = acceptance.run_acceptance(main, photos)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["api_calls"], 0)

    def test_legacy_optional_heic_copy_selection_and_ambiguity(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for name in (*acceptance.FILENAMES, "proposal_page_1.HEIC"):
                (directory / name).write_bytes(b"selection only; decoding tested separately")
            self.assertEqual(tuple(acceptance.load_photos(directory)), acceptance.FILENAMES)
            self.assertEqual(tuple(acceptance.load_photos(directory, include_heic=True)),
                             (*acceptance.FILENAMES, "proposal_page_1.HEIC"))
        with self.assertRaisesRegex(ValueError, "Multiple photo files"):
            acceptance.resolve_photo_name(["proposal_page_1.png", "proposal_page_1.heic"], acceptance.FILENAMES[0])

    def test_identical_normalized_image_reuses_cached_extraction(self):
        # Different requests may carry an identical normalized page. Do not
        # transcribe twice or mislabel the cache hit as lost provenance.
        photos = {**self.photos, acceptance.HEIC_FILENAME: photo("HEIF", marker="Acceptance HEIC")}
        prepared = {name: [{"page": 1, "image": "same-page" if name in
                    (acceptance.FILENAMES[0], acceptance.HEIC_FILENAME) else name}] for name in photos}
        def decoder(data, name, *args):
            # Only the verified acceptance requests reach this patched decoder.
            return prepared[name]
        with patch.object(main, "prepare_document", side_effect=decoder):
            result = acceptance.run_acceptance(main, photos, self.reader())
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["api_calls"], 3)

    def test_authorization_negative_cases_are_exercised(self):
        result = acceptance.run_acceptance(main, self.photos)
        for name in ("unauthorized_rejected", "forged_proof_rejected", "wrong_owner", "unpaid",
                     "tier3_disabled", "tier1_two_quotes_rejected"):
            self.assertTrue(result["checks"][name], name)

    def test_app_security_state_and_routes_are_unchanged(self):
        state = main.app.state._state.copy()
        routes = list(main.app.routes)
        dependencies = main.app.dependency_overrides.copy()
        analysis, cache, reader = main.analyze_hvac_quote, main.image_text_cache, main.transcribe_page
        acceptance.run_acceptance(main, self.photos)
        self.assertEqual(main.app.state._state, state)
        self.assertEqual(main.app.routes, routes)
        self.assertEqual(main.app.dependency_overrides, dependencies)
        self.assertIs(main.analyze_hvac_quote, analysis)
        self.assertIs(main.image_text_cache, cache)
        self.assertIs(main.transcribe_page, reader)

    def test_corrupt_files_prevent_paid_calls(self):
        reader = self.reader()
        result = acceptance.run_acceptance(main, {**self.photos, acceptance.FILENAMES[1]: b"corrupt"}, reader)
        self.assertFalse(result["passed"])
        self.assertEqual(result["api_calls"], 0)
        reader.assert_not_called()

    def test_model_error_stops_without_retry_or_partial_report(self):
        reader = Mock(side_effect=HTTPException(502, "Extraction unavailable"))
        result = acceptance.run_acceptance(main, self.photos, reader)
        self.assertFalse(result["passed"])
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(result["hvac_analysis"], "NOT RUN")
        self.assertNotIn("grouped_quote", result["results"][0])

    def test_safe_transcription_diagnostic_survives_upload_error_translation(self):
        reader = Mock(side_effect=vision.TranscriptionError("api_authentication"))
        result = acceptance.run_acceptance(main, self.photos, reader)
        self.assertFalse(result["passed"])
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(result["results"][0]["http_status"], 503)
        self.assertEqual(result["results"][0]["error"], {"stage": "transcription", "category": "api_authentication"})
        self.assertFalse(result["results"][0]["retake_requested"])
        self.assertTrue(result["checks"]["no_analysis_reservation"])

    def test_changed_model_stops_further_acceptance_spending(self):
        reader = Mock(side_effect=[acceptance.PAGES[0].replace("DEMO-ID-012A", "DEMO-ID-012B"), acceptance.PAGES[1]])
        result = acceptance.run_acceptance(main, self.photos, reader)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["proposal_group_page1_models"])
        self.assertEqual(result["api_calls"], 2)

    def test_guessing_obscured_price_fails_even_if_guess_is_correct(self):
        reader = Mock(side_effect=[*acceptance.PAGES, acceptance.PAGES[1]])
        result = acceptance.run_acceptance(main, self.photos, reader)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["unreadable_requires_retake"])

    def test_corrupt_control_is_not_mistaken_for_valid_unreadable_rejection(self):
        # The cache intentionally maps all transcription 422s to a retake.
        # A corrupt file rejected by the decoder is a different failure.
        photos = {**self.photos, acceptance.FILENAMES[2]: b"not a valid JPEG"}
        result = acceptance.run_acceptance(main, photos, self.reader())
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["unreadable_requires_retake"])
        self.assertEqual(result["results"][-1]["error"], {"stage": "document_preparation", "category": "image_processing"})

    def test_full_comparison_rejects_invented_measurements_specs_and_omissions(self):
        for altered in (acceptance.PAGES[1] + "Measured resistance: 12.3 kOhm.",
                        acceptance.PAGES[1] + "Cooling capacity: 24,000 BTU/h.",
                        acceptance.PAGES[1].replace("No diagnostic", "Diagnostic"),
                        acceptance.PAGES[1].replace("$350.00", "$3500.00"),
                        acceptance.PAGES[1].replace("$350.00", "-$350.00"),
                        acceptance.PAGES[1].replace("$350.00", "350.00")):
            with self.subTest(text=altered):
                self.assertFalse(acceptance.compare_page(altered, 1)["complete_text_no_additions_or_omissions"])

    def test_comparison_tolerates_layout_but_keeps_warranty_and_exclusions(self):
        text = acceptance.PAGES[1].upper().replace("\n", "   ")
        self.assertTrue(all(acceptance.compare_page(text, 1).values()))
        changed = text.replace("90 DAYS", "30 DAYS")
        self.assertFalse(acceptance.compare_page(changed, 1)["warranty_and_exclusions"])

    def test_call_budget_counts_failures_and_never_retries(self):
        reader = Mock(side_effect=HTTPException(502, "unavailable"))
        bounded = acceptance.BoundedReader(reader, 1)
        with self.assertRaises(HTTPException):
            bounded(None, "image")
        with self.assertRaises(RuntimeError):
            bounded(None, "image")
        self.assertEqual(reader.call_count, 1)
        with self.assertRaises(ValueError):
            acceptance.BoundedReader(reader, 5)

    def test_missing_photos_fail_before_import_or_api_key_prompt(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(acceptance.Path, "home", return_value=Path(temp)), \
                patch.object(acceptance.getpass, "getpass") as prompt, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(acceptance.cli(["--live", "--include-heic"]), 2)
        prompt.assert_not_called()
        self.assertIn("Missing regular photo file", out.getvalue())

    def test_loader_rejects_symlinks_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            target = directory / "source.jpg"
            target.write_bytes(self.photos[acceptance.FILENAMES[0]])
            first = directory / acceptance.FILENAMES[0]
            first.symlink_to(target)
            with self.assertRaises(ValueError):
                acceptance.load_photos(directory)
            first.unlink()
            with first.open("wb") as output:
                output.truncate(10 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                acceptance.load_photos(directory)

    def test_default_cli_no_dotenv_no_api_and_private_result(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "Desktop" / "CYT_mobile_test"
            directory.mkdir(parents=True)
            for name, content in self.photos.items():
                (directory / name).write_bytes(content)
            with patch.object(acceptance.Path, "home", return_value=Path(temp)), \
                    patch.object(acceptance.getpass, "getpass") as prompt, \
                    patch("dotenv.load_dotenv") as dotenv, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(acceptance.cli([]), 0)
            prompt.assert_not_called()
            dotenv.assert_not_called()
            self.assertNotIn("CYT Demo Mechanical", output.getvalue())
            report_path = Path(output.getvalue().split("Private comparison report: ")[1].splitlines()[0])
            self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(report_path.read_text())["api_calls"], 0)

    def test_generated_proposal_has_both_ground_truth_pages(self):
        directory = acceptance.prepare()
        expected = json.loads((directory / "expected.json").read_text())
        self.assertEqual(expected["pages"], list(acceptance.PAGES))
        self.assertEqual((directory / "proposal.html").read_text().count("<section class='page'>"), 2)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_explicit_live_cli_uses_only_mocked_vision_and_private_comparison(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "Desktop" / "CYT_mobile_test"
            directory.mkdir(parents=True)
            for name, content in self.photos.items():
                (directory / name).write_bytes(content)
            sdk = Mock()
            sdk.return_value.__enter__ = Mock(return_value=Mock())
            sdk.return_value.__exit__ = Mock(return_value=False)
            with patch.object(acceptance.Path, "home", return_value=Path(temp)), \
                    patch.dict(os.environ, {"OPENAI_API_KEY": "offline-explicit-key", "OPENAI_BASE_URL": "https://example.invalid"}), \
                    patch("openai.OpenAI", sdk), \
                    patch("image_transcription.transcribe_page", side_effect=[*acceptance.PAGES, HTTPException(422, BETTER_PHOTO)]) as vision, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(acceptance.cli(["--live"]), 0)
            self.assertEqual(vision.call_count, 3)
            self.assertEqual(sdk.call_args.kwargs["base_url"], "https://api.openai.com/v1")
            self.assertEqual(sdk.call_args.kwargs["max_retries"], 0)
            self.assertEqual(sdk.call_args.kwargs["timeout"], 30)
            report_path = Path(output.getvalue().split("Private comparison report: ")[1].splitlines()[0])
            report = json.loads(report_path.read_text())
            self.assertTrue(report["passed"])
            self.assertEqual(report["api_calls"], 3)
            self.assertNotIn("CYT Demo Mechanical", output.getvalue())
            self.assertNotIn("offline-explicit-key", output.getvalue() + report_path.read_text())


if __name__ == "__main__":
    unittest.main()
