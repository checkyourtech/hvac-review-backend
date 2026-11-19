"""Bounded local document decoding. Vision transcription runs separately.

Work Package 6 transport: authenticated multipart POST /upload (files,
customer_name, customer_email, quote_groups), or JSON POST /analyze with extractedText and
customer contact fields. Both retain WP1 Authorization/X-Purchase-ID headers.
File URLs are unsupported until a real Wix transport is verified. Contact fields
are delivery metadata; verified purchase/customer IDs remain the authority.
Each quote_groups entry contains ordered pages with zero-based file indexes and
clockwise rotation (0/90/180/270). A group is one contractor proposal, not one
photograph. See quote_groups() for the exact manifest. The upload-page JavaScript
keeps selection/preview local; Wix still needs its verified-purchase handoff.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import warnings

from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BODY = 3 * MAX_FILE_BYTES + 64 * 1024
MAX_JSON_BODY = 2 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_TEXT_CHARS = 100_000
MAX_TOTAL_TEXT_CHARS = 200_000
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
EXTRACTION_TIMEOUT = 10
MAX_ATTACHMENTS_PER_QUOTE = 10
MAX_VISUAL_PAGES_PER_QUOTE = 10
MAX_IMAGE_PIXELS = 25_000_000
MAX_PREPARED_BYTES = 30 * 1024 * 1024
_EXTRACTION_SLOTS = threading.BoundedSemaphore(2)
ALLOWED_MIME = {
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".txt": {"text/plain"},
    ".jpg": {"image/jpeg"}, ".jpeg": {"image/jpeg"}, ".png": {"image/png"},
    ".heic": {"image/heic", "image/heif"}, ".heif": {"image/heif", "image/heic"},
}
IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".heic": "HEIF", ".heif": "HEIF"}
NO_TEXT = "No readable proposal text was found. Upload a clearer, complete photo or a readable PDF, DOCX, or UTF-8 text file."
BETTER_PHOTO = "This page cannot be read reliably. Retake it in good light, in focus, with all text and page edges visible. No review was started."


def validate_filename(filename, content_type=None):
    if (not isinstance(filename, str) or not filename.strip() or len(filename) > 180
            or any(c in filename for c in "/\\") or any(not c.isprintable() for c in filename)):
        raise HTTPException(400, "Provide a valid file name without folders or control characters.")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_MIME:
        raise HTTPException(415, "Unsupported file type. Upload PDF, DOCX, TXT, JPG, JPEG, PNG, HEIC, or HEIF.")
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in {"", "application/octet-stream"} | ALLOWED_MIME[suffix]:
        raise HTTPException(415, "The file type does not match its name. Export a supported document or photo and upload again.")
    return suffix


def validate_text(text):
    if not isinstance(text, str) or not text.strip() or not any(c.isalnum() for c in text):
        raise HTTPException(422, NO_TEXT)
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(413, "A proposal exceeds the 100,000-character text limit. Submit a smaller document.")
    if any((ord(c) < 32 and c not in "\n\r\t\f") or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise HTTPException(422, "The proposal contains unreadable or binary data. Export a readable document and try again.")
    return text


def validate_total_text(texts):
    if sum(len(text) for text in texts) > MAX_TOTAL_TEXT_CHARS:
        raise HTTPException(413, "The combined proposals exceed the 200,000-character text limit.")


def validate_customer(name, email, name_alias=None, email_alias=None):
    def resolve(first, second, label):
        values = [v.strip() for v in (first, second) if isinstance(v, str)]
        if not values or any(not v for v in values):
            raise HTTPException(422, f"Customer {label} is required.")
        if len(set(values)) > 1:
            raise HTTPException(400, f"Conflicting customer {label} fields.")
        return values[0]
    name = resolve(name, name_alias, "name")
    email = resolve(email, email_alias, "email")
    if len(name) > 120 or not any(c.isalpha() for c in name) or any(unicodedata.category(c).startswith("C") for c in name):
        raise HTTPException(422, "Enter a valid customer name of up to 120 characters.")
    # Syntax only: no DNS lookup or claim that this email authenticates ownership.
    if len(email) > 254 or email.count("@") != 1:
        raise HTTPException(422, "Enter a valid customer email address.")
    local, domain = email.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        domain = ""
    labels = domain.split(".")
    if (not 1 <= len(local) <= 64 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or local.startswith(".") or local.endswith(".") or ".." in local or len(labels) < 2
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels)):
        raise HTTPException(422, "Enter a valid customer email address.")
    return name, local + "@" + domain


class IntakeBodyLimit:
    """Bound the entire body, including chunked requests, before multipart spooling.

    Buffered bytes are request-local; excess/slow requests never reach a parser.
    Delivering one bounded chunk also lets the multipart parser check the whole
    file count before Starlette flushes any queued file data to temporary spools.
    Deployment must also enforce connection/rate limits (Work Package 6).
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").rstrip("/")
        if scope["type"] != "http" or scope.get("method") != "POST" or path not in {"/upload", "/analyze"}:
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD_BODY if path == "/upload" else MAX_JSON_BODY
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
            if declared < 0:
                raise ValueError
        except ValueError:
            return await JSONResponse({"detail": "Invalid request size."}, 400)(scope, receive, send)
        async def collect():
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, "The upload request is too large.")
                body.extend(chunk)
                if not message.get("more_body", False):
                    return bytes(body)
        try:
            if declared > limit:
                raise HTTPException(413, "The upload request is too large.")
            body = await asyncio.wait_for(collect(), timeout=30)
        except asyncio.TimeoutError:
            return await JSONResponse({"detail": "The upload timed out. Please try again."}, 408)(scope, receive, send)
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, exc.status_code)(scope, receive, send)
        if body is None:
            return
        pending = True
        async def buffered_receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, buffered_receive, send)


class _UploadParser(MultiPartParser):
    """Close every spool on malformed/truncated multipart, including parser errors."""
    def on_part_begin(self):
        super().on_part_begin()
        self.part_bytes = 0

    def on_headers_finished(self):
        super().on_headers_finished()
        file = self._current_part.file
        if file is not None:
            validate_filename(file.filename, file.content_type)

    def on_part_data(self, data, start, end):
        self.part_bytes += end - start
        if self._current_part.file is not None and self.part_bytes > MAX_FILE_BYTES:
            raise HTTPException(413, "Each file must be 10 MiB or smaller.")
        super().on_part_data(data, start, end)

    def on_end(self):
        # The body middleware supplies one bounded chunk, so this validation
        # runs before the parser flushes any file data to temporary storage.
        from starlette.datastructures import FormData
        self.groups = quote_groups(FormData(self.items), self.quote_limit)
        self.complete = True
        super().on_end()

    async def parse(self):
        self.complete = False
        try:
            result = await super().parse()
            if not self.complete:
                raise HTTPException(400, "The upload was incomplete. Please upload the files again.")
            return result
        except BaseException:
            for file in self._files_to_close_on_error:
                file.close()
            raise


@asynccontextmanager
async def upload_form(request, max_files):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "multipart/form-data":
        raise HTTPException(415, "Upload documents using multipart/form-data.")
    parser = _UploadParser(request.headers, request.stream(), max_files=max_files * MAX_ATTACHMENTS_PER_QUOTE,
                          max_fields=6, max_part_size=4096)
    parser.quote_limit = max_files
    try:
        form = await parser.parse()
    except HTTPException:
        raise
    except MultiPartException:
        raise HTTPException(400, f"Invalid upload form. This purchase permits at most {max_files} files; check the required fields.") from None
    except Exception:
        raise HTTPException(400, "The upload form could not be read. Please try again.") from None
    try:
        fields = {"package", "packageName", "customer_name", "customer_email", "quote_groups"}
        for key in form:
            if key != "files" and (key not in fields or len(form.getlist(key)) != 1 or not isinstance(form[key], str)):
                raise HTTPException(400, "The upload form contains invalid or repeated fields.")
        if any(not isinstance(file, UploadFile) for file in form.getlist("files")):
            raise HTTPException(400, "Attach proposal files in the files field.")
        yield form
    finally:
        await form.close()


def quote_groups(form, quote_limit):
    """An explicit manifest assigns every attachment once, in proposal order.

    quote_groups: [{"pages": [{"file": 0, "rotation": 0}, ...]}, ...].
    Legacy document-only requests retain one document per logical quote.
    Multiple photographs require explicit grouping; identity is never guessed.
    """
    files = form.getlist("files")
    manifests = form.getlist("quote_groups")
    if not manifests:
        if len(files) > 1 and any(Path(f.filename or "").suffix.lower() in IMAGE_FORMATS
                                  for f in files if isinstance(f, UploadFile)):
            raise HTTPException(400, "Group all pages belonging to each contractor quote before submitting.")
        groups = [{"pages": [{"file": i, "rotation": 0}]} for i in range(len(files))]
    else:
        try:
            if len(manifests) != 1 or not isinstance(manifests[0], str):
                raise ValueError
            groups = json.loads(manifests[0])
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid quote-page grouping.") from None
    if not isinstance(groups, list) or not 1 <= len(groups) <= quote_limit:
        raise HTTPException(400, f"This purchase allows 1 to {quote_limit} contractor quotes, each with multiple pages.")
    indexes = []
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"pages"} or not isinstance(group["pages"], list):
            raise HTTPException(400, "Invalid quote-page grouping.")
        if not 1 <= len(group["pages"]) <= MAX_ATTACHMENTS_PER_QUOTE:
            raise HTTPException(400, "Each quote may contain up to 10 attached files or photos.")
        for page in group["pages"]:
            if (not isinstance(page, dict) or set(page) != {"file", "rotation"}
                    or type(page["file"]) is not int or type(page["rotation"]) is not int
                    or not 0 <= page["file"] < len(files) or page["rotation"] not in (0, 90, 180, 270)):
                raise HTTPException(400, "Invalid page index or rotation.")
            indexes.append(page["file"])
    if sorted(indexes) != list(range(len(files))):
        raise HTTPException(400, "Assign every file to exactly one contractor quote.")
    return groups


def extract_text_from_uploaded_file(data: bytes, filename: str, content_type=None) -> str:
    """Text-only compatibility entry point; never silently drops image pages."""
    pages = prepare_document(data, filename, content_type)
    if any("image" in page for page in pages):
        raise HTTPException(422, "This document requires image transcription through authenticated upload.")
    return validate_text("\n".join(page["text"] for page in pages))


def prepare_document(data: bytes, filename: str, content_type=None, rotation=0):
    """Validate/decode all pages locally before any paid image transcription."""
    suffix = validate_filename(filename, content_type)
    if type(rotation) is not int or rotation not in (0, 90, 180, 270):
        raise HTTPException(400, "Invalid page rotation.")
    if rotation and suffix not in IMAGE_FORMATS and suffix != ".pdf":
        raise HTTPException(400, "Rotation applies only to photographs and PDF pages.")
    if not data:
        raise HTTPException(422, "The uploaded file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Each file must be 10 MiB or smaller.")
    if suffix == ".txt":
        if data.startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8", b"\xd0\xcf\x11\xe0")):
            raise HTTPException(415, "The file contents do not match the TXT file type.")
        try:
            return [{"page": 1, "text": validate_text(data.decode("utf-8-sig"))}]
        except UnicodeError:
            raise HTTPException(422, "The text file is not readable UTF-8. Export it as UTF-8 TXT and try again.") from None
    signatures = {".pdf": b"%PDF-", ".docx": b"PK\x03\x04", ".jpg": b"\xff\xd8",
                  ".jpeg": b"\xff\xd8", ".png": b"\x89PNG\r\n\x1a\n"}
    if suffix in signatures and not data.startswith(signatures[suffix]):
        raise HTTPException(422, "The document is damaged or its contents do not match the file type.")
    if suffix in {".heic", ".heif"} and data[4:8] != b"ftyp":
        raise HTTPException(422, "The HEIC/HEIF file is damaged or does not match its file type.")
    if not _EXTRACTION_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "Document processing is busy. Please try again shortly.")
    try:
        # Isolated child: no main import, credentials, network, customer paths or
        # user-controlled command arguments. A hard timeout kills and reaps it.
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--extract", suffix, str(rotation)],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath}, timeout=EXTRACTION_TIMEOUT)
        if result.returncode or len(result.stdout) > MAX_PREPARED_BYTES:
            raise HTTPException(422, "The document could not be read within processing limits. Export a simpler document and try again.")
        response = json.loads(result.stdout)
        if "error" in response:
            raise HTTPException(response["status"], response["error"])
        return response["pages"]
    except subprocess.TimeoutExpired:
        raise HTTPException(422, "The document took too long to read. Export a simpler document and try again.") from None
    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError, OSError):
        raise HTTPException(422, "The document could not be read. Export a new PDF or DOCX and try again.") from None
    finally:
        _EXTRACTION_SLOTS.release()


def _pdf_pages(data, rotation=0):
    import fitz
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise HTTPException(422, "Password-protected PDFs are not supported. Upload an unlocked copy.")
        if doc.is_repaired:
            raise HTTPException(422, "The PDF is damaged. Export a new PDF and try again.")
        if doc.page_count > MAX_PDF_PAGES:
            raise HTTPException(413, "PDF proposals must contain 50 pages or fewer.")
        if doc.is_form_pdf:
            raise HTTPException(422, "Save or print the completed PDF form as a flattened PDF, then upload it again.")
        pages = []
        for page in doc:
            text = page.get_text("text", sort=True)
            # An image may contain substantive text even when a digital header
            # is readable. Transcribe that whole page once; don't append OCR to
            # its text layer (which would duplicate the same source).
            if page.get_images() or not text.strip() and page.get_drawings():
                if sum("image" in item for item in pages) >= MAX_VISUAL_PAGES_PER_QUOTE:
                    raise HTTPException(413, "A quote may contain up to 10 scanned or photographed pages.")
                if not text.strip() and page.get_images() and all(max(image[2:4]) < 300 for image in page.get_images()):
                    raise HTTPException(422, BETTER_PHOTO)
                scale = min(2.0, 2400 / max(page.rect.width, page.rect.height))
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                pages.append({"page": page.number + 1, "image": _image_page(pix.tobytes("png"), ".png", rotation)})
            else:
                pages.append({"page": page.number + 1, "text": text})
            if sum(len(item.get("text", "")) for item in pages) > MAX_TEXT_CHARS:
                raise HTTPException(413, "The PDF exceeds the 100,000-character text limit.")
            if sum(len(item.get("image", "")) for item in pages) > MAX_PREPARED_BYTES - 4096:
                raise HTTPException(413, "The scanned PDF exceeds processing limits. Submit smaller pages.")
        if not any("image" in item for item in pages):
            validate_text("\n".join(item["text"] for item in pages))
        return pages


def _image_page(data, suffix, rotation=0):
    from PIL import Image, ImageOps, ImageStat
    if suffix in {".heic", ".heif"}:
        try:
            from pillow_heif import register_heif_opener
        except ImportError:
            raise HTTPException(415, "HEIC conversion is unavailable here. Export this photo as JPG or PNG.") from None
        register_heif_opener(thumbnails=False)
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            if source.format != IMAGE_FORMATS[suffix]:
                raise HTTPException(415, "The image contents do not match the file type.")
            if getattr(source, "n_frames", 1) != 1:
                raise HTTPException(415, "Attach each photo separately; animated or multi-image containers are not supported.")
            width, height = source.size
            if width * height > MAX_IMAGE_PIXELS or max(width, height) > 12000:
                raise HTTPException(413, "Photos must be at most 25 megapixels and 12,000 pixels per side.")
            if min(width, height) < 600:
                raise HTTPException(422, BETTER_PHOTO)
            source.load()
            oriented = ImageOps.exif_transpose(source)
            if rotation:
                oriented = oriented.rotate(-rotation, expand=True)
            image = Image.new("RGB", oriented.size, "white")
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image.paste(oriented.convert("RGB"))
            image.thumbnail((2400, 2400))
            if ImageStat.Stat(image.convert("L")).stddev[0] < 4:
                raise HTTPException(422, BETTER_PHOTO)
            out = io.BytesIO()
            # Fresh RGB pixels strip EXIF/GPS and embedded profiles/metadata.
            image.save(out, format="JPEG", quality=95)
            if out.tell() > 3 * 1024 * 1024:
                raise HTTPException(413, "This photo exceeds image processing limits. Export a smaller clear image.")
            return base64.b64encode(out.getvalue()).decode("ascii")


def _xml(data):
    text = data.decode("utf-8-sig")
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise HTTPException(422, "This DOCX contains unsupported XML. Save a new document and try again.")
    return ET.fromstring(text)


def _docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 256 or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, "The DOCX exceeds the document expansion limit.")
        if len(names) != len(set(names)) or any(name.startswith("/") or ".." in name.split("/") for name in names):
            raise HTTPException(422, "The DOCX archive is malformed.")
        for entry in entries:
            if entry.flag_bits & 1:
                raise HTTPException(422, "Encrypted DOCX files are not supported.")
            if entry.file_size > max(1, entry.compress_size) * 200:
                raise HTTPException(413, "The DOCX exceeds the document expansion limit.")
        def read_xml(name):
            if archive.getinfo(name).file_size > MAX_XML_BYTES:
                raise HTTPException(413, "The DOCX contains an oversized document section.")
            with archive.open(name) as source:
                return _xml(source.read(MAX_XML_BYTES + 1))
        types = read_xml("[Content_Types].xml")
        if not any(e.get("PartName") == "/word/document.xml" and e.get("ContentType") ==
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml" for e in types):
            raise HTTPException(422, "This file is not a supported DOCX proposal.")
        parts = ["word/document.xml"] + sorted(name for name in names if re.fullmatch(
            r"word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml", name))
        text = []
        namespaces = {"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                      "http://purl.oclc.org/ooxml/wordprocessingml/main"}
        def walk(element):
            namespace, _, tag = element.tag.lstrip("{").partition("}")
            if namespace in namespaces:
                if tag in {"altChunk", "object", "del", "ins"}:
                    raise HTTPException(422, "The DOCX contains embedded documents or unresolved edits. Upload a finalized text document.")
                if tag == "t":
                    text.append(element.text or "")
                elif tag == "tab":
                    text.append("\t")
                elif tag in {"br", "cr"}:
                    text.append("\n")
            for child in element:
                walk(child)
            if namespace in namespaces and tag in {"p", "tc"}:
                text.append("\n" if tag == "p" else "\t")
        for part in parts:
            root = read_xml(part)
            if part == "word/document.xml" and root.tag not in {"{" + ns + "}document" for ns in namespaces}:
                raise HTTPException(422, "The DOCX document structure is invalid.")
            walk(root)
            if sum(map(len, text)) > MAX_TEXT_CHARS:
                raise HTTPException(413, "The DOCX exceeds the 100,000-character text limit.")
        return validate_text("".join(text))


def _worker():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    # macOS rejects finite DATA/AS limits. CPU, wall time, input size, page
    # count and XML expansion remain bounded there; production must also set
    # its container memory/concurrency limits (Work Package 6).
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    def no_io(event, args):
        if event.startswith("socket."):
            raise RuntimeError("Network disabled")
        if event == "open" and isinstance(args[0], (str, bytes)):
            name = os.fsdecode(args[0])
            if Path(name).name == ".env":
                raise RuntimeError("Environment access disabled")
    sys.addaudithook(no_io)
    try:
        data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        if not data or len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "The document exceeds processing limits.")
        suffix = sys.argv[2]
        rotation = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        if suffix == ".pdf":
            pages = _pdf_pages(data, rotation)
        elif suffix == ".docx":
            pages = [{"page": 1, "text": _docx_text(data)}]
        else:
            pages = [{"page": 1, "image": _image_page(data, suffix, rotation)}]
        result = {"pages": pages}
    except HTTPException as exc:
        result = {"error": exc.detail, "status": exc.status_code}
    except Exception:
        result = {"error": "The document is damaged or cannot be read. Export a new document and try again.", "status": 422}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__" and sys.argv[1:2] == ["--extract"]:
    _worker()
