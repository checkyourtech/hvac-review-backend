"""Local-only synthetic photo acceptance runner; never starts HVAC analysis.

python -B mobile_photo_acceptance.py --prepare
python -B mobile_photo_acceptance.py                        # offline preflight
python -B mobile_photo_acceptance.py --live                 # explicit, <=3 calls
# --include-heic optionally adds a separate HEIC page-1 copy (<=4 calls total).

Print the generated two-page proposal, photograph it, and place the named files
in ~/Desktop/CYT_mobile_test/ (or select --input-dir). Do not use customer documents. The production
app's purchase service is never configured or overridden by this runner.
"""
import argparse
import contextlib
import getpass
import html
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
import warnings
from unittest.mock import patch


PAGES = (
    """SYNTHETIC PROPOSAL - NOT A REAL CONTRACTOR QUOTE
Page 1 of 2
Contractor: CYT Demo Mechanical
Contact: proposal@example.invalid
Proposal: DEMO-2048
Equipment brand: Fictional Air
Indoor model: DEMO-ID-012A
Outdoor model: DEMO-OD-024B
Model numbers are synthetic test identifiers.
Existing ductless mini-split has a reported indoor sensor fault.
Replace the indoor-unit thermistor only.
Retain all indoor and outdoor equipment.
No new installation, system replacement, or capacity change.
Verify operation after repair.
""",
    """SYNTHETIC PROPOSAL - NOT A REAL CONTRACTOR QUOTE
Page 2 of 2
Contractor: CYT Demo Mechanical
Proposal: DEMO-2048
Thermistor part: $125.00
Labor: $175.00
Service charge: $50.00
Total: $350.00
Warranty: 90 days on the replaced part and repair labor.
Exclusions: drywall repair, painting, and unrelated equipment repairs.
No diagnostic measurements are documented in this proposal.
No equipment capacity, refrigerant type, voltage, or efficiency rating is specified.
No other equipment failures are confirmed.
""",
)
FILENAMES = ("proposal_page_1.jpg", "proposal_page_2.jpeg", "unreadable_page_2.jpg")
HEIC_FILENAME = "proposal_page_1.heic"
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic", ".heif")
MAX_CALLS = 4


def tokens(text):
    # Ignore only layout/case/Markdown punctuation. Keep every word, number,
    # decimal, model character and unit; no fuzzy correction of OCR mistakes.
    return re.findall(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*|[$%°+/=−-]", text.casefold())


def compare_page(text, page):
    expected = PAGES[page]
    fields = {
        "contractor": [line for line in expected.splitlines() if line.startswith(("Contractor:", "Contact:", "Proposal:"))],
        "models": [line for line in expected.splitlines() if "model:" in line or "brand:" in line],
        "pricing": [line for line in expected.splitlines() if "$" in line],
        "scope": [line for line in expected.splitlines() if line.startswith(("Replace", "Retain", "No new", "Verify"))],
        "warranty_and_exclusions": [line for line in expected.splitlines() if line.startswith(("Warranty:", "Exclusions:"))],
        "no_invented_diagnostics_or_specs": [line for line in expected.splitlines() if line.startswith(("No diagnostic", "No equipment", "No other"))],
    }
    normalized = " " + " ".join(tokens(text)) + " "
    checks = {name: all(" " + " ".join(tokens(line)) + " " in normalized for line in lines)
              for name, lines in fields.items() if lines}
    # Field presence alone cannot detect added invented readings or conclusions.
    checks["complete_text_no_additions_or_omissions"] = tokens(text) == tokens(expected)
    return checks


def write_private(path, text):
    with path.open("x", encoding="utf-8") as output:
        os.chmod(path, 0o600)
        output.write(text)


def prepare():
    directory = Path(tempfile.mkdtemp(prefix="cyt-mobile-proposal-"))
    instructions = """Print both pages at 100% scale with print headers/footers disabled.
Use only this synthetic proposal.
Take clear phone photos with all page edges visible, good light, and no glare.
Create ~/Desktop/CYT_mobile_test/ and name the pages proposal_page_1 and
proposal_page_2, keeping their original .jpg, .jpeg, .png, .heic, or .heif
extensions (uppercase extensions also work). No JPEG copies are required.
Take another photo of page 2 with a finger obscuring the price digits (leave the
rest of the page visible); name it unreadable_page_2 with its original extension. This must require a
retake, even if a model guesses the hidden price correctly. Do not use a blank
image. Keep the instructions outside the photographs.
Run the offline preflight first. Inspect the images yourself before --live.
The live run sends these synthetic images to OpenAI and never runs HVAC analysis.
No real payment, Wix handoff, delivery, or end-to-end purchase is being tested.
"""
    document = ("<!doctype html><html lang='en'><meta charset='utf-8'><title>CYT synthetic photo acceptance</title>"
                "<style>body{font-family:Arial;margin:32px}aside{white-space:pre-wrap;background:#eef;padding:16px}"
                "pre{white-space:pre-wrap;font:16px/1.65 monospace}.page{max-width:720px;margin:40px auto}"
                "@media print{@page{size:letter;margin:.65in}aside{display:none}body{margin:0}"
                ".page{margin:0;break-after:page}.page:last-child{break-after:auto}pre{font-size:13px}}</style>"
                "<aside>" + html.escape(instructions) + "</aside>" +
                "".join("<section class='page'><pre>" + html.escape(page) + "</pre></section>" for page in PAGES))
    write_private(directory / "proposal.html", document + "</html>")
    write_private(directory / "expected.json", json.dumps({"pages": PAGES, "instructions": instructions}, indent=2))
    return directory


def resolve_photo_name(names, preferred, extensions=PHOTO_EXTENSIONS):
    matches = [name for name in names if Path(name).stem == Path(preferred).stem
               and Path(name).suffix.lower() in extensions]
    # Preserve the existing JPG/JPEG-plus-optional-HEIC acceptance arrangement.
    if preferred in matches:
        return preferred
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError("Missing regular photo file: " + Path(preferred).stem + " (JPG/JPEG/PNG/HEIC/HEIF)")
    raise ValueError("Multiple photo files match " + Path(preferred).stem + "; keep one version per required page.")


def load_photos(directory, include_heic=False):
    available = [path.name for path in directory.iterdir()] if directory.is_dir() else []
    names = [resolve_photo_name(available, preferred) for preferred in FILENAMES]
    if include_heic:
        heic = resolve_photo_name(available, HEIC_FILENAME, (".heic", ".heif"))
        if heic not in names:
            names.append(heic)
    photos = {}
    for name in names:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing regular photo file: " + name)
        with path.open("rb") as source:
            data = source.read(10 * 1024 * 1024 + 1)
        if not data or len(data) > 10 * 1024 * 1024:
            raise ValueError("Photo must be nonempty and at most 10 MiB: " + name)
        photos[name] = data
    return photos


class AnalysisStopped(Exception):
    """Intentional private harness boundary, never an application endpoint."""


class BoundedReader:
    def __init__(self, reader, limit):
        if not 0 <= limit <= MAX_CALLS:
            raise ValueError("Acceptance call budget must be at most four")
        self.reader, self.limit, self.calls = reader, limit, 0
        self.pages = []
        self.errors = []

    def __call__(self, unused_client, image, timeout=30):
        if self.calls >= self.limit:
            raise RuntimeError("Acceptance image-call budget exhausted")
        self.calls += 1  # Failures count too. Never retry automatically.
        from image_transcription import TranscriptionError
        try:
            text = self.reader(image, timeout=min(30, timeout))
        except TranscriptionError as exc:
            self.errors.append(dict(exc.diagnostic))
            raise
        self.pages.append(text)
        return text


def run_acceptance(main, photos, reader=None):
    """Exercise the real upload route/authorization in an unserved cloned app.

    reader=None means preflight only: real file validation, fake transcription.
    The only live reader is constructed by the explicitly gated CLI below.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from document_intake import BETTER_PHOTO, IntakeBodyLimit
    from image_transcription import TranscriptionCache
    from purchase_security import EntitlementService, InvalidPurchaseProof, SQLitePurchaseLedger, VerifiedPurchase

    live = reader is not None
    filenames = tuple(resolve_photo_name(photos, preferred) for preferred in FILENAMES)
    budget = BoundedReader(reader or (lambda image, timeout: "OFFLINE PREFLIGHT. No transcription performed."), len(photos))
    proof = secrets.token_urlsafe(32)
    base_purchase = dict(purchase_id="synthetic-mobile-acceptance", owner_customer_id="synthetic-owner",
                         authenticated_customer_id="synthetic-owner", tier="tier1", payment_eligible=True,
                         expires_at=int(time.time()) + 600, submission_limit=1)

    class Verifier:
        purchase = VerifiedPurchase(**base_purchase)

        async def verify(self, purchase_id, bearer_token):
            if purchase_id != self.purchase.purchase_id or bearer_token != proof:
                raise InvalidPurchaseProof()
            return self.purchase

    verifier = Verifier()
    checks, results, captured = {}, [], []

    async def stop_analysis(request, authorization):
        if authorization.tier != "tier1" or len(request.files) != 1:
            raise AssertionError("Tier 1 grouping failed")
        captured.append(request)
        raise AnalysisStopped()

    async def transcription_only_submission(http_request, authorization, fingerprint, operation, kind):
        # This explicit acceptance harness stops before analysis. It exercises
        # real purchase verification/intake without starting a report job.
        return await operation(authorization)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected analysis, report, delivery, or image call")

    with tempfile.TemporaryDirectory(prefix="cyt-mobile-ledger-") as temp, contextlib.ExitStack() as stack:
        app = FastAPI()
        app.include_router(main.app.router)
        app.add_middleware(IntakeBodyLimit)
        app.exception_handlers.update(main.app.exception_handlers)
        ledger = SQLitePurchaseLedger.initialize(Path(temp) / "purchases.sqlite3")
        app.state.entitlements = EntitlementService(verifier, ledger)
        cache = TranscriptionCache()
        stack.callback(cache.close)
        stack.enter_context(patch.object(main, "image_text_cache", cache))
        stack.enter_context(patch.object(main, "analyze_hvac_quote", stop_analysis))
        stack.enter_context(patch.object(main, "submit_report_job", transcription_only_submission))
        for name in ("classify_quotes", "request_structured_analysis", "send_review_email", "build_report_html"):
            stack.enter_context(patch.object(main, name, forbidden))
        stack.enter_context(patch.object(main.client.beta.chat.completions, "parse", forbidden))
        client = stack.enter_context(TestClient(app))

        def submit(names, groups, headers=None):
            return client.post("/upload", headers=headers if headers is not None else {
                "Authorization": "Bearer " + proof, "X-Purchase-ID": verifier.purchase.purchase_id},
                data={"customer_name": "Synthetic Acceptance", "customer_email": "test@example.invalid",
                      "quote_groups": json.dumps([{"pages": [{"file": i, "rotation": 0} for i in group]} for group in groups])},
                files=[("files", (name, photos[name], "application/octet-stream")) for name in names])

        # Prove rejection before any decoding or billable work, then enable reader.
        with patch.object(main, "prepare_document", forbidden), patch.object(main, "transcribe_page", forbidden):
            checks["unauthorized_rejected"] = submit(filenames[:2], [[0, 1]], {}).status_code == 401
            checks["forged_proof_rejected"] = submit(filenames[:2], [[0, 1]], {
                "Authorization": "Bearer forged", "X-Purchase-ID": verifier.purchase.purchase_id}).status_code == 403
            for name, change, status in (
                ("wrong_owner", {"authenticated_customer_id": "another-owner"}, 403),
                ("unpaid", {"payment_eligible": False}, 403),
                ("tier3_disabled", {"tier": "tier3"}, 503),
            ):
                verifier.purchase = VerifiedPurchase(**{**base_purchase, **change, "purchase_id": "synthetic-" + name})
                checks[name] = submit(filenames[:2], [[0, 1]]).status_code == status
            verifier.purchase = VerifiedPurchase(**base_purchase)
            checks["tier1_two_quotes_rejected"] = submit(filenames[:2], [[0], [1]]).status_code == 400
        if not all(checks.values()):
            raise AssertionError("Purchase preflight failed; no paid calls permitted")
        stack.enter_context(patch.object(main, "transcribe_page", budget))

        scenarios = [("proposal_group", filenames[:2], [0, 1])]
        extra_heic = [name for name in photos if name not in filenames and
                      Path(name).stem == Path(HEIC_FILENAME).stem and Path(name).suffix.lower() in (".heic", ".heif")]
        if extra_heic:
            scenarios.append(("heic", (extra_heic[0],), [0]))
        scenarios.append(("unreadable", (filenames[2],), []))
        for label, names, expected_pages in scenarios:
            previous_calls, previous_errors = budget.calls, len(budget.errors)
            try:
                response = submit(names, [list(range(len(names)))])
            except AnalysisStopped:
                request = captured[-1]
                source = request.files[0].extractedText
                labels = [f"File Name: {name} (page 1, image transcription)\n" for name in names]
                texts = []
                remainder = source
                for index, prefix in enumerate(labels):
                    if not remainder.startswith(prefix):
                        break
                    remainder = remainder[len(prefix):]
                    if index + 1 < len(labels):
                        text, separator, remainder = remainder.partition("\n\n" + labels[index + 1])
                        remainder = labels[index + 1] + remainder if separator else ""
                    else:
                        text = remainder
                    texts.append(text)
                checks[label + "_one_logical_quote"] = len(request.files) == 1
                checks[label + "_provenance"] = all(
                    f"File Name: {name} (page 1, image transcription)\n{text}" in request.files[0].extractedText
                    for name, text in zip(names, texts)) and len(texts) == len(names)
                if live:
                    if not expected_pages:
                        checks["unreadable_requires_retake"] = False
                    else:
                        for text, page in zip(texts, expected_pages):
                            for field, passed in compare_page(text, page).items():
                                checks[f"{label}_page{page + 1}_{field}"] = passed
                    results.append({"case": label, "transcripts": texts, "grouped_quote": request.files[0].extractedText})
                else:
                    results.append({"case": label, "files": list(names), "decoded": True,
                                    "transcription": "NOT RUN", "expected": "retake" if label == "unreadable" else "readable"})
            else:
                detail = response.json().get("detail", {})
                retake = response.status_code == 422 and isinstance(detail, dict) and detail.get("message") == BETTER_PHOTO
                checks[label + "_requires_retake" if label == "unreadable" else label + "_extracted"] = retake if label == "unreadable" else False
                if len(budget.errors) > previous_errors:
                    error = budget.errors[-1]
                elif retake:
                    error = {"stage": "image_quality", "category": "unreadable_photo"}
                elif budget.calls == previous_calls and response.status_code in (400, 413, 415, 422, 504):
                    error = {"stage": "document_preparation", "category": "image_processing"}
                else:
                    error = {"stage": "upload", "category": "unclassified_failure"}
                # Never copy arbitrary HTTP/provider details into the report.
                results.append({"case": label, "http_status": response.status_code,
                                "retake_requested": retake, "error": error})
            # Stop spending when a readable page fails its expected comparison.
            if not all(checks.values()):
                break
        with contextlib.closing(sqlite3.connect(str(ledger.path))) as db:
            checks["no_analysis_reservation"] = db.execute("SELECT SUM(used) FROM purchases").fetchone()[0] == 0
    return {"mode": "live_transcription" if live else "offline_preflight", "passed": all(checks.values()),
            "checks": checks, "results": results, "api_calls": budget.calls if live else 0,
            "maximum_api_calls": len(photos), "hvac_analysis": "NOT RUN",
            "heic_included": any(Path(name).suffix.lower() in (".heic", ".heif") for name in photos),
            "accuracy": "compare report and original photos manually" if live else "UNVERIFIED; no transcription performed"}


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prepare", action="store_true", help="Create printable synthetic pages in a private temporary directory")
    parser.add_argument("--input-dir", type=Path, default=Path.home() / "Desktop" / "CYT_mobile_test",
                        help="Photo folder (default: ~/Desktop/CYT_mobile_test)")
    parser.add_argument("--include-heic", action="store_true", help="Also test a HEIC page-1 copy, unless already selected (at most one extra call)")
    parser.add_argument("--live", action="store_true", help="Explicitly allow up to 3 image calls (4 with a separate HEIC copy); no retries or HVAC analysis")
    args = parser.parse_args(argv)
    if args.prepare:
        print("Print and photograph:", prepare() / "proposal.html")
        return 0
    try:
        photos = load_photos(args.input_dir.expanduser(), args.include_heic)
    except (ValueError, OSError) as exc:
        print(str(exc))
        return 2

    # Import the application with no .env reads, real credentials, proxy, or
    # custom API endpoints. This process does not start a web server.
    safe_environment = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG") if name in os.environ}
    safe_environment["OPENAI_API_KEY"] = "offline-mobile-acceptance-dummy"
    old_logging = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with patch.dict(os.environ, safe_environment, clear=True), patch("dotenv.load_dotenv", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import main
            preflight = run_acceptance(main, photos)
        if not preflight["passed"]:
            report = preflight
        elif not args.live:
            report = preflight
        else:
            # Supplied only by the local operator; never read from .env or argv.
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                with warnings.catch_warnings():
                    # Fail closed if this terminal cannot hide credential input.
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    key = getpass.getpass("OpenAI API key (hidden; not saved): ")
            if not key.strip():
                print("No API key supplied. No live calls made.")
                return 2
            import httpx
            from openai import OpenAI
            from image_transcription import transcribe_page
            with httpx.Client(trust_env=False, follow_redirects=False) as transport, OpenAI(
                    api_key=key, base_url="https://api.openai.com/v1", max_retries=0, timeout=30,
                    http_client=transport) as client, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                report = run_acceptance(main, photos, lambda image, timeout: transcribe_page(client, image, timeout=timeout))
    except Exception:
        # Do not print SDK exceptions, request contents, credentials or traceback.
        print("Acceptance stopped safely. No HVAC analysis was run. Inspect input files and rerun preflight.")
        return 1
    finally:
        logging.disable(old_logging)
    directory = Path(tempfile.mkdtemp(prefix="cyt-mobile-results-"))
    write_private(directory / "report.json", json.dumps(report, indent=2))
    print(("PASS" if report["passed"] else "FAIL"), report["mode"], "| API calls:", report["api_calls"], "| HVAC analysis: NOT RUN")
    print("Private comparison report:", directory / "report.json")
    if not args.live:
        print("Image accuracy is UNVERIFIED. Inspect the photos before explicitly authorizing --live.")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(cli())
