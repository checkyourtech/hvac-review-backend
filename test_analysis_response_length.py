"""Offline SDK/request regressions for completion exhaustion, using synthetic data.

The failed live completion was not saved. These tests reproduce its SDK finish
reason and token usage, not its unknown generated content. Network I/O is mocked;
the real SDK parser, application finalizer and HTML renderer remain in use.
"""
import contextlib
import html
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openai import LengthFinishReasonError
from openai.lib._parsing._completions import parse_chat_completion
from openai.lib._pydantic import to_strict_json_schema
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

import main
from commissioning import commissioning_items
from evidence_integrity import assessment_owner
from test_mini_split_installation import case
from test_mini_split_startup_regression import SOURCE as REPAIR_SOURCE, saved_response as repair_response
from test_system_sizing import analysis


RETROFIT = (
    "Replace the outdoor inverter heat-pump unit of the existing mini-split. "
    "Retain existing indoor heads. Reuse the existing line sets, subject to "
    "manufacturer compatibility verification. Verify retained indoor/outdoor "
    "equipment matching, refrigerant compatibility, pipe sizes, electrical "
    "connections, communication wiring and condensate drainage. Follow "
    "manufacturer startup procedure and document final operating readings. "
    "$8,000 lump sum."
)
MODULES = [main.AnalysisModule.EQUIPMENT_MATCHING, main.AnalysisModule.SYSTEM_SIZING,
           main.AnalysisModule.PRICING, main.AnalysisModule.COMMISSIONING,
           main.AnalysisModule.REPAIR_VS_REPLACE]
PARTIAL_CONTENT = '{"project_overview":"OFFLINE_UNFINISHED_PRIVATE_CONTENT'


def classified(quote_type="replacement", modules=None):
    return main.QuoteClassification(
        quote_type=quote_type, system_type="ductless inverter mini-split",
        primary_scope="Replace outdoor unit; retain indoor heads" if quote_type == "replacement" else "Submitted work",
        modules_required=MODULES if modules is None else modules,
    )


def parsed_response(value=None, *, finish="stop", content=None, usage=True, refusal=None):
    """Exercise the installed SDK's actual structured completion parser."""
    schema = type(value) if value is not None else main.HVACAnalysis
    message = {"role": "assistant", "content": content if content is not None else
               (value.model_dump_json() if value is not None else PARTIAL_CONTENT)}
    if refusal:
        message["refusal"] = refusal
        message["content"] = None
    response = ChatCompletion(
        id="offline-response-length", created=0, model="gpt-4o-mini", object="chat.completion",
        choices=[{"index": 0, "finish_reason": finish, "message": message}],
        usage={"prompt_tokens": 12000, "completion_tokens": 16384,
               "total_tokens": 28384} if usage else None,
    )
    return parse_chat_completion(response_format=schema, input_tools=[], chat_completion=response)


def endpoint(source, raw, classification, outcomes=("success",), route="/analyze"):
    """Real report pipeline with only external analysis/delivery substituted."""
    attempts = iter(outcomes)
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        if kwargs["response_format"] is main.QuoteClassification:
            return parsed_response(classification)
        outcome = next(attempts)
        return parsed_response(raw) if outcome == "success" else parsed_response(finish="length")

    with TemporaryDirectory(prefix="cyt-response-length-") as directory, contextlib.redirect_stdout(io.StringIO()), \
            patch.object(main, "UPLOAD_DIR", Path(directory)), \
            patch.object(main.client.beta.chat.completions, "parse", side_effect=respond), \
            patch.object(main, "send_review_email") as email, \
            patch.object(main, "finalize_customer_analysis", wraps=main.finalize_customer_analysis) as finalize, \
            patch.object(main, "build_report_html", wraps=main.build_report_html) as render:
        client = TestClient(main.app, raise_server_exceptions=False)
        if route == "/upload":
            response = client.post(route,
                data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
                files={"files": ("synthetic-quote.txt", source.encode(), "text/plain")})
        else:
            response = client.post(route, json={"package": "tier1", "files": [
                {"fileName": "synthetic-quote.txt", "extractedText": source}]})
    return response, requests, email, finalize, render


class StructuredAnalysisLengthTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.messages = [{"role": "system", "content": "All applicable technical rules."},
                         {"role": "user", "content": RETROFIT}]

    def test_success_preserves_strict_schema_and_uses_one_request(self):
        raw, _ = case()
        original = json.dumps(self.messages)
        with patch.object(main.client.beta.chat.completions, "parse", return_value=parsed_response(raw)) as api:
            result = main.request_structured_analysis(self.messages)
        self.assertEqual(result.model_dump(), raw.model_dump())
        api.assert_called_once()
        request = api.call_args.kwargs
        self.assertIs(request["response_format"], main.HVACAnalysis)
        self.assertEqual(request["model"], "gpt-4o-mini")
        self.assertEqual(request["max_completion_tokens"], 16384)
        self.assertEqual(json.dumps(self.messages), original)
        schema = to_strict_json_schema(request["response_format"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])

    def test_sdk_length_finish_rejects_even_syntactically_complete_json(self):
        with self.assertRaises(LengthFinishReasonError):
            parsed_response(analysis(), finish="length")

    def test_length_retry_retains_inputs_and_discards_partial_content(self):
        raw, _ = case()
        attempts = iter(("length", "success"))

        def respond(**kwargs):
            return parsed_response(raw) if next(attempts) == "success" else parsed_response(finish="length")

        with patch.object(main.client.beta.chat.completions, "parse", side_effect=respond) as api, \
                self.assertLogs("main", level="WARNING") as logs:
            result = main.request_structured_analysis(self.messages)
        self.assertEqual(result.model_dump(), raw.model_dump())
        self.assertEqual(api.call_count, 2)
        first, second = (call.kwargs for call in api.call_args_list)
        self.assertEqual(second["messages"][:-1], first["messages"])
        self.assertEqual(first["messages"][:2], self.messages)
        for request in (first, second):
            self.assertIs(request["response_format"], main.HVACAnalysis)
            self.assertEqual(request["max_completion_tokens"], 16384)
            self.assertNotIn("OFFLINE_UNFINISHED_PRIVATE_CONTENT", json.dumps(request["messages"]))
        self.assertNotIn("OFFLINE_UNFINISHED_PRIVATE_CONTENT", " ".join(logs.output))
        self.assertEqual(len(self.messages), 2)

    def test_repeated_length_stops_after_two_attempts_with_clear_error(self):
        with patch.object(main.client.beta.chat.completions, "parse",
                          side_effect=lambda **kwargs: parsed_response(finish="length")) as api, \
                self.assertRaises(main.HTTPException) as caught:
            main.request_structured_analysis(self.messages)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("No report was generated", caught.exception.detail)
        self.assertNotIn("OFFLINE_UNFINISHED_PRIVATE_CONTENT", caught.exception.detail)

    def test_length_error_without_usage_still_has_bounded_recovery(self):
        with patch.object(main.client.beta.chat.completions, "parse",
                          side_effect=lambda **kwargs: parsed_response(finish="length", usage=False)) as api, \
                self.assertRaises(main.HTTPException):
            main.request_structured_analysis(self.messages)
        self.assertEqual(api.call_count, 2)

    def test_long_valid_structured_output_is_not_truncated(self):
        raw, _ = case()
        raw.project_overview = "Distinct submitted project context. " * 2000
        self.assertGreater(len(raw.model_dump_json()), 65000)
        with patch.object(main.client.beta.chat.completions, "parse", return_value=parsed_response(raw)) as api:
            result = main.request_structured_analysis(self.messages)
        api.assert_called_once()
        self.assertEqual(result.model_dump(), raw.model_dump())
        self.assertEqual(main.HVACAnalysis.model_validate_json(result.model_dump_json()), result)

    def test_invalid_json_is_not_fabricated_or_retried_as_length(self):
        with patch.object(main.client.beta.chat.completions, "parse",
                          side_effect=lambda **kwargs: parsed_response(content='{"project_overview":')) as api, \
                self.assertRaises(ValidationError):
            main.request_structured_analysis(self.messages)
        api.assert_called_once()

    def test_other_api_errors_are_not_retried_by_length_recovery(self):
        with patch.object(main.client.beta.chat.completions, "parse", side_effect=RuntimeError("Offline failure")) as api, \
                self.assertRaisesRegex(RuntimeError, "Offline failure"):
            main.request_structured_analysis(self.messages)
        api.assert_called_once()

    def test_refusal_or_missing_parsed_result_does_not_become_analysis(self):
        refusal = parsed_response(refusal="Offline refusal")
        for response in (refusal, refusal.model_copy(update={"choices": []})):
            with self.subTest(empty=not response.choices), \
                    patch.object(main.client.beta.chat.completions, "parse", return_value=response) as api, \
                    self.assertRaises(main.HTTPException) as caught:
                main.request_structured_analysis(self.messages)
            api.assert_called_once()
            self.assertEqual(caught.exception.status_code, 502)
            self.assertIn("No report was generated", caught.exception.detail)

    def test_retrofit_keeps_all_selected_knowledge_once(self):
        response, requests, _, _, _ = endpoint(RETROFIT, analysis(pricing="LIMITED"), classified())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(requests), 2)
        prompt = "\n".join(message["content"] for message in requests[-1]["messages"])
        self.assertIn(RETROFIT, prompt)
        for module in MODULES:
            rule = main.UNIVERSAL_PRICING_RULES if module == main.AnalysisModule.PRICING else main.ANALYSIS_MODULES[module]
            self.assertEqual(prompt.count(rule.strip()), 1, module)
        final = main.HVACAnalysis.model_validate(response.json())
        self.assertTrue(main.sizing_assessments(final))
        self.assertTrue(commissioning_items(final))
        self.assertTrue(any(a.subject == "Quoted indoor/outdoor equipment compatibility" for a in final.technical_assessments))
        self.assertFalse(any(assessment_owner(a.subject) in {"compressor", "control board"}
                             for a in final.technical_assessments))
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")

    def test_retrofit_retry_preserves_full_analysis_and_only_then_finalizes(self):
        raw = analysis(pricing="LIMITED")
        normal, _, _, _, _ = endpoint(RETROFIT, raw, classified())
        recovered, requests, email, finalize, _ = endpoint(RETROFIT, raw, classified(), ("length", "success"))
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json(), normal.json())
        self.assertEqual(len(requests), 3)  # Classification plus two analysis attempts.
        self.assertEqual(requests[-1]["messages"][:-1], requests[-2]["messages"])
        email.assert_called_once()
        finalize.assert_called_once()

    def test_exhausted_retrofit_never_finalizes_or_generates_json_report(self):
        response, requests, email, finalize, render = endpoint(
            RETROFIT, analysis(), classified(), ("length", "length"))
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(set(response.json()), {"detail"})
        self.assertEqual(len(requests), 3)
        email.assert_not_called()
        finalize.assert_not_called()
        render.assert_not_called()

    def test_exhausted_upload_never_generates_partial_html(self):
        response, requests, email, finalize, render = endpoint(
            RETROFIT, analysis(), classified(), ("length", "length"), route="/upload")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(len(requests), 3)
        self.assertIn("No report was generated", response.json()["detail"])
        self.assertNotIn("OFFLINE_UNFINISHED_PRIVATE_CONTENT", response.text)
        email.assert_not_called()
        finalize.assert_not_called()
        render.assert_not_called()

    def test_successful_retry_html_matches_finalized_json(self):
        raw = analysis(pricing="LIMITED")
        response, _, email, finalize, _ = endpoint(
            RETROFIT, raw, classified(), ("length", "success"), route="/upload")
        self.assertEqual(response.status_code, 200, response.text)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(response.text, main.build_report_html(final, 1))
        self.assertEqual(final.model_dump(), main.HVACAnalysis.model_validate_json(final.model_dump_json()).model_dump())
        for question in final.contractor_questions:
            self.assertIn(question, html.unescape(response.text))
        finalize.assert_called_once()
        email.assert_called_once()

    def test_normal_component_repair_keeps_scope_without_retry(self):
        classification = classified("repair", [main.AnalysisModule.ELECTRICAL_CONTROLS, main.AnalysisModule.PRICING])
        response, requests, _, _, _ = endpoint(REPAIR_SOURCE, repair_response(), classification)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(requests), 2)
        final = main.HVACAnalysis.model_validate(response.json())
        self.assertFalse(main.sizing_assessments(final))
        self.assertFalse(commissioning_items(final))
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")

    def test_normal_installation_keeps_complete_evidence_without_retry(self):
        raw, source = case()
        response, requests, _, _, _ = endpoint(source, raw, classified("installation"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(requests), 2)
        final = main.HVACAnalysis.model_validate(response.json())
        self.assertTrue(main.sizing_assessments(final))
        self.assertTrue(commissioning_items(final))
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        matching = next(a for a in final.technical_assessments
                        if a.subject == "Quoted indoor/outdoor equipment compatibility")
        self.assertEqual(matching.documented_evidence, raw.technical_assessments[1].documented_evidence)


if __name__ == "__main__":
    unittest.main()
