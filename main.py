import os
import io
import html
import base64
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form 
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pathlib import Path
import uuid
from openai import OpenAI
from pydantic import BaseModel, Field
from pypdf import PdfReader
import fitz  # PyMuPDF

load_dotenv()

app = FastAPI(title="HVAC Quote Analyzer")
app.mount("/static", StaticFiles(directory="static"), name="static")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

EMAIL_USER = "reviews@checkyourtechs.com"
BUSINESS_EMAIL = "reviews@checkyourtechs.com"
WEBSITE_URL = "https://www.checkyourtech.info"
LOGO_URL = "/static/logo.png"


class HVACAnalysis(BaseModel):
    project_overview: str
    equipment_analysis: str
    missing_information: str
    pricing_review: str
    installation_concerns: str
    quote_comparison: str
    best_quote_recommendation: str
    contractor_vetting: str
    red_flags: List[str]
    good_signs: List[str]
    recommendation: str


class UploadedQuote(BaseModel):
    fileName: Optional[str] = None
    originalFileName: Optional[str] = None
    downloadUrl: Optional[str] = None
    fileUrl: Optional[str] = None
    extractedText: Optional[str] = None


class AnalyzeRequest(BaseModel):
    package: Optional[str] = Field(default=None, alias="package")
    packageName: Optional[str] = "tier1"
    customer_name: Optional[str] = None
    customerName: Optional[str] = "Website Customer"
    customer_email: Optional[str] = None
    customerEmail: Optional[str] = None
    contractor_1_name: str = ""
    contractor_2_name: str = ""
    contractor_3_name: str = ""
    city: str = ""
    state: str = ""
    files: List[UploadedQuote]


PACKAGE_RULES = {
    "tier1": "Tier 1: review only one HVAC quote. Do not compare multiple quotes. Do not perform contractor vetting.",
    "tier2": "Tier 2: compare up to three HVAC quotes. Recommend the best overall quote. Do not perform contractor vetting.",
    "tier3": "Tier 3: compare up to three HVAC quotes and include contractor vetting if contractor names are provided."
}


@app.get("/")
def root():
    return {"status": "online"}


def esc(value):
    return html.escape(str(value or ""))


def make_list(items):
    if not items:
        return "<li>No major items identified.</li>"
    return "".join(f"<li>{esc(item)}</li>" for item in items)

def build_report_html(analysis):
    def section(title, body):
        return f"""
        <div class="card">
            <h2>{esc(title)}</h2>
            <p>{esc(body)}</p>
        </div>
        """

    def list_section(title, items, empty_text):
        return f"""
        <div class="card">
            <h2>{esc(title)}</h2>
            <ul>
                {make_list(items) if items else f"<li>{esc(empty_text)}</li>"}
            </ul>
        </div>
        """

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Check Your Tech HVAC Quote Review</title>

<style>
body {{
    margin: 0;
    background: #f3f4f6;
    font-family: Arial, Helvetica, sans-serif;
    color: #111827;
}}

.cyt-report {{
    max-width: 950px;
    margin: 30px auto;
    background: white;
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 8px 30px rgba(0,0,0,0.12);
}}

.header {{
    background: #111827;
    color: white;
    text-align: center;
    padding: 40px 25px 32px;
}}

.header img {{
    max-width: 170px;
    height: auto;
    display: block;
    margin:0 auto 12px;
}}

.header h1 {{
    margin: 0;
    font-size: 32px;
}}

.subtitle {{
    margin-top: 8px;
    color: #d1d5db;
    font-size: 16px;
}}

.summary {{
    background: #ecfdf5;
    border-left: 6px solid #2b7a2b;
    margin: 25px;
    padding: 20px;
    border-radius: 10px;
}}

.summary h2 {{
    margin-top: 0;
}}

.card {{
    margin: 20px 25px;
    padding: 22px;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    background: #ffffff;
}}

.card h2 {{
    margin-top: 0;
    color: #111827;
    border-bottom: 2px solid #e5e7eb;
    padding-bottom: 8px;
}}

.card p, .card li {{
    font-size: 16px;
    line-height: 1.55;
}}

ul {{
    padding-left: 22px;
}}

.final-box {{
    margin: 25px;
    padding: 24px;
    background: #f9fafb;
    border: 2px solid #2b7a2b;
    border-radius: 14px;
    text-align: center;
}}

.download-btn {{
    background: #2b7a2b;
    color: white;
    border: none;
    padding: 16px 36px;
    border-radius: 8px;
    font-size: 18px;
    cursor: pointer;
    margin-top: 18px;
}}

.disclaimer {{
    margin: 25px;
    font-size: 13px;
    color: #6b7280;
    text-align: center;
}}

@media print {{
    .download-btn {{
        display: none;
    }}

    body {{
        background: white;
    }}

    .cyt-report {{
        box-shadow: none;
        margin: 0;
    }}
}}
</style>
</head>

<body>
<div class="cyt-report">

    <div class="header">
        <img src="{LOGO_URL}" alt="Check Your Tech Logo">
        <h1>HVAC Quote Review</h1>
        <p class="subtitle">Independent HVAC proposal review before you commit.</p>
    </div>

    <div class="summary">
        <h2>Review Summary</h2>
        <p>{esc(analysis.project_overview)}</p>
    </div>

    {section("Equipment Analysis", analysis.equipment_analysis)}
    {section("Missing Information", analysis.missing_information)}
    {section("Pricing Review", analysis.pricing_review)}
    {section("Installation Concerns", analysis.installation_concerns)}
    {section("Quote Comparison", analysis.quote_comparison)}
    {section("Best Quote Recommendation", analysis.best_quote_recommendation)}
    {list_section("Red Flags", analysis.red_flags, "No major red flags identified.")}
    {list_section("Good Signs", analysis.good_signs, "No major positive items identified.")}
    {section("Final Recommendation", analysis.recommendation)}

    <div class="final-box">
        <h2>Review Complete</h2>
        <p>
            Keep this report with your proposal. Before signing, ask your contractor
            about any missing information, red flags, or unclear scope items listed above.
        </p>

        <button class="download-btn" onclick="window.print()">
            Download / Save as PDF
        </button>
    </div>

    <p class="disclaimer">
        This review is for informational purposes only and does not replace a licensed contractor inspection.
    </p>

</div>
</body>
</html>
"""
def download_file(url: str) -> bytes:
    if not url:
        raise HTTPException(status_code=400, detail="Missing downloadUrl.")

    if not (
        url.startswith("https://")
        or url.startswith("http://127.0.0.1")
        or url.startswith("http://localhost")
    ):
        raise HTTPException(status_code=400, detail="Invalid file URL. Expected HTTPS URL.")

    with urllib.request.urlopen(url) as response:
        return response.read()


def extract_pdf_text(file_bytes: bytes) -> str:
    text = ""

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text += page_text + "\n"
    except Exception as e:
        text += f"\nPDF text extraction failed: {str(e)}\n"

    return text.strip()


def pdf_pages_to_images(file_bytes: bytes, max_pages: int = 3) -> List[bytes]:
    images = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    for page_index in range(min(len(doc), max_pages)):
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        images.append(pix.tobytes("png"))

    return images


def image_to_data_url(image_bytes: bytes, mime_type: str = "image/png") -> str:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def ocr_image(image_bytes: bytes, file_name: str = "uploaded image") -> str:
    data_url = image_to_data_url(image_bytes)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You extract readable text from HVAC quotes, invoices, proposals, handwritten estimates, equipment labels, and contractor documents. Return only the extracted text. If text is unclear, say what appears unclear."
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Extract all readable quote/proposal text from this image: {file_name}"
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url}
                    }
                ]
            }
        ]
    )

    return response.choices[0].message.content or ""


def extract_file_text(file_bytes: bytes, file_name: str) -> str:
    lower_name = file_name.lower()

    if lower_name.endswith(".pdf"):
        text = extract_pdf_text(file_bytes)

        if len(text.strip()) > 100:
            return text

        ocr_text = []
        try:
            page_images = pdf_pages_to_images(file_bytes)
            for i, img in enumerate(page_images, start=1):
                ocr_text.append(f"--- OCR PAGE {i} ---\n{ocr_image(img, file_name)}")
            return "\n\n".join(ocr_text)
        except Exception as e:
            return f"Could not extract readable PDF text or OCR image pages. Error: {str(e)}"

    if lower_name.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return ocr_image(file_bytes, file_name)

    try:
        return file_bytes.decode("utf-8", errors="ignore")
    except Exception as e:
        return f"Could not read file contents: {str(e)}"


def send_review_email(customer_name, customer_email, package_key, file_names, analysis: HVACAnalysis):
    email_password = os.getenv("EMAIL_APP_PASSWORD")

    if not email_password:
        print("EMAIL_APP_PASSWORD missing. Skipping email send.")
        return

    body = f"""
<html>
<body style="font-family: Arial, sans-serif; background-color:#f4f4f4; padding:20px;">
<div style="max-width:700px; margin:auto; background:white; border-radius:10px; overflow:hidden; border:1px solid #ddd;">

<div style="background:#1f2937; padding:25px; text-align:center;">
<img src="{LOGO_URL}" width="180">
</div>

<div style="padding:30px; color:#333;">
<h2 style="color:#111827;">New Check Your Tech Review</h2>

<p>
<b>Customer Name:</b> {esc(customer_name)}<br>
<b>Customer Email:</b> {esc(customer_email)}<br>
<b>Package:</b> {esc(package_key)}<br>
<b>Uploaded Files:</b> {esc(", ".join(file_names))}
</p>

<h3>Project Overview</h3>
<p>{esc(analysis.project_overview)}</p>

<h3>Equipment Analysis</h3>
<p>{esc(analysis.equipment_analysis)}</p>

<h3>Missing Information</h3>
<p>{esc(analysis.missing_information)}</p>

<h3>Pricing Review</h3>
<p>{esc(analysis.pricing_review)}</p>

<h3>Installation Concerns</h3>
<p>{esc(analysis.installation_concerns)}</p>

<h3>Quote Comparison</h3>
<p>{esc(analysis.quote_comparison)}</p>

<h3>Best Quote Recommendation</h3>
<p>{esc(analysis.best_quote_recommendation)}</p>

<h3>Contractor Vetting</h3>
<p>{esc(analysis.contractor_vetting)}</p>

<h3 style="color:#dc2626;">Red Flags</h3>
<ul>{make_list(analysis.red_flags)}</ul>

<h3 style="color:#16a34a;">Good Signs</h3>
<ul>{make_list(analysis.good_signs)}</ul>

<h3>Final Recommendation</h3>
<p>{esc(analysis.recommendation)}</p>

<hr>
<p style="font-size:14px; color:#666;">
<b>Disclaimer:</b><br>
This review is intended to help homeowners identify potential concerns, missing information, or areas that may require clarification before proceeding with HVAC work.
<br><br>
This review is not a substitute for an in-person inspection, load calculation, or licensed engineering evaluation.
</p>
</div>

<div style="background:#f3f4f6; padding:20px; text-align:center; font-size:14px; color:#555;">
<b>Check Your Tech</b><br>
HVAC Quote Review & Consumer Protection Services<br>
<a href="{WEBSITE_URL}" style="color:#2563eb;">www.checkyourtech.info</a>
</div>

</div>
</body>
</html>
"""

    msg = EmailMessage()
    msg["Subject"] = f"New HVAC Quote Review - {customer_name}"
    msg["From"] = EMAIL_USER
    msg["To"] = BUSINESS_EMAIL
    msg.set_content("This email requires an HTML-compatible email client.")
    msg.add_alternative(body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_USER, email_password)
        smtp.send_message(msg)


@app.post("/analyze", response_model=HVACAnalysis)
async def analyze_hvac_quote(request: AnalyzeRequest):
    raw_package = (request.packageName or request.package or "tier1").lower().strip()

    package_map = {
        "basic": "tier1",
        "standard": "tier2",
        "premium": "tier3",
        "tier1": "tier1",
        "tier2": "tier2",
        "tier3": "tier3",
    }

    package_key = package_map.get(raw_package, raw_package)
    customer_name = request.customerName or request.customer_name or "Website Customer"
    customer_email = request.customerEmail or request.customer_email or ""

    if package_key not in {"tier1", "tier2", "tier3"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid package."
        )

    if package_key == "tier1" and len(request.files) > 1:
        raise HTTPException(
            status_code=400,
            detail="Basic Review allows 1 quote upload."
        )

    if package_key in {"tier2", "tier3"} and len(request.files) > 3:
        raise HTTPException(
            status_code=400,
            detail="Standard and Premium allow up to 3 quote uploads."
        )

    quote_blocks = []
    file_names = []

    for index, uploaded_file in enumerate(request.files, start=1):
        file_name = uploaded_file.fileName or uploaded_file.originalFileName or f"quote{index}.pdf"
        download_url = uploaded_file.downloadUrl or uploaded_file.fileUrl

        if uploaded_file.extractedText:
            quote_text = uploaded_file.extractedText
        elif download_url:
            file_bytes = download_file(download_url)
            quote_text = extract_file_text(file_bytes, file_name)
        else:
            raise HTTPException(status_code=400, detail="Missing quote text or downloadUrl.")

        file_names.append(file_name)

        quote_blocks.append(
            f"""
    QUOTE {index}
    File Name: {file_name}

    {quote_text}
    """
        )

    all_quotes_text = "\n\n".join(quote_blocks)

    contractor_vetting_results = "Contractor vetting is only included with Tier 3."

    if package_key == "tier3":
        contractor_vetting_results = "Contractor vetting placeholder: online contractor search can be added here."

    completion = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"""
            You are a senior residential HVAC installation and service foreman with over 20 years of field experience.

Review residential HVAC proposals, estimates, and repair quotes like a homeowner handed them to you and asked: "Would you approve this?"

Write like a real foreman talking to a homeowner, not like AI, a lawyer, or a salesman.

You can review:
- full HVAC system replacements
- furnace replacements
- AC replacements
- heat pump replacements
- mini split replacements
- compressor replacements
- contactor replacements
- capacitor replacements
- fan motor repairs
- blower motor repairs
- control board repairs
- refrigerant leak repairs
- refrigerant recharge quotes
- ductwork repairs
- thermostat replacements
- maintenance-related repair recommendations

For replacement quotes, focus on:
- equipment details
- model numbers
- AHRI match if listed
- warranty
- installation scope
- line set
- electrical
- condensate drain
- thermostat
- permits
- startup/commissioning
- missing information
- pricing transparency
- red flags
- good signs
- final recommendation

For repair quotes, focus on:
- diagnosis clarity
- whether the failed part is clearly identified
- whether testing results are shown
- whether repair vs replacement makes sense
- part warranty
- labor warranty
- refrigerant type and amount if applicable
- whether additional failure risks were explained
- whether the repair price is transparent
- whether the customer should ask for a second opinion

If one quote is provided, review it on its own.

If two or three quotes are provided, compare them and choose a Foreman's Pick. Do not automatically choose the cheapest quote.

Never invent missing information. If something is not shown, say: "I don't see that listed."

Never accuse a contractor of dishonesty.

Keep the review practical, homeowner-friendly, and honest.
                """
            },
            {
                "role": "user",
                "content": f"""
Customer:
{customer_name}

Customer Email:
{customer_email}

Location:
City: {request.city}
State: {request.state}

Submitted HVAC Quote(s):
{all_quotes_text}

Contractor vetting search results:
{contractor_vetting_results}
                """
            }
        ],
        response_format=HVACAnalysis,
    )

    analysis = completion.choices[0].message.parsed

    send_review_email(
        customer_name=customer_name,
        customer_email=customer_email,
        package_key=package_key,
        file_names=file_names,
        analysis=analysis
    )

    print("ANALYSIS RESULT:", analysis)
    return analysis

def extract_text_from_uploaded_file(filepath: str) -> str:
    text = ""

    if filepath.lower().endswith(".pdf"):
        reader = PdfReader(filepath)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
        return text

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

@app.post("/upload")
async def upload_file(
    files: List[UploadFile] = File(...),
    package: str = Form(...),
    customer_name: str = Form(...),
    customer_email: str = Form(...),
):
    print("PACKAGE RECEIVED:", package)
    print("NUMBER OF FILES:", len(files))
    if len(files) > 3:
        raise HTTPException(
            status_code=400,
            detail="You may upload a maximum of 3 quotes."
        )

    uploaded_quotes = []

    for file in files:
        file_id = str(uuid.uuid4())
        filename = f"{file_id}_{file.filename}"
        filepath = UPLOAD_DIR / filename

        with open(filepath, "wb") as buffer:
            buffer.write(await file.read())

        quote_text = extract_text_from_uploaded_file(str(filepath))

        print("QUOTE FILE:", file.filename)
        print("QUOTE LENGTH:", len(quote_text))

        uploaded_quotes.append(
            UploadedQuote(
                fileName=file.filename,
                originalFileName=file.filename,
                downloadUrl="",
                fileUrl="",
                extractedText=quote_text,
            )
        )

    request = AnalyzeRequest(
        package=package,
        packageName=package,
        customerName=customer_name,
        customer_email=customer_email,
        customerEmail=customer_email,
        contractor_1_name="",
        contractor_2_name="",
        contractor_3_name="",
        city="",
        state="",
        files=uploaded_quotes,
    )

    analysis = await analyze_hvac_quote(request)

    return HTMLResponse(build_report_html(analysis))


 

("/uploads/{filename}")
async def get_upload(filename: str):
    return FileResponse(UPLOAD_DIR / filename)

@app.get("/upload-page", response_class=HTMLResponse)
async def upload_page(package: str = "basic"):
    if package not in {"basic", "standard"}: package = "basic"
    return """
<!DOCTYPE html>
<html>
<head>
    <title>Upload Your HVAC Quote | Check Your Tech</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #f4f6f8;
            margin: 0;
            padding: 40px;
        }
        .card {
            max-width: 760px;
            margin: auto;
            background: white;
            padding: 35px;
            border-radius: 14px;
            box-shadow: 0 8px 30px rgba(0,0,0,0.12);
        }
        h1, h2 {
            text-align: center;
            margin-top: 10px;
            margin-bottom: 20px;
            color: #111827;
        }
        p {
            color: #374151;
            text-align:center;
            line-height: 1.6;
        }
        label {
            font-weight: bold;
            display: block;
            margin-top: 18px;
        }
        input, select {
            width: 100%;
            padding: 12px;
            margin-top: 6px;
            font-size: 16px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
        }
        button {
            width: 100%;
            margin-top: 24px;
            padding: 14px;
            background: #111827;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 18px;
            cursor: pointer;
        }
        button:hover {
            background: #374151;
        }
        .small {
            font-size: 13px;
            color: #6b7280;
        }
    </style>
</head>
<body>
    <div class="card">
        <img
    src="/static/logo.png"
    alt="Check Your Tech"
    style="
        display:block;
        width:260px;
        margin:0 auto 15px auto;
    ">
        <h2>Upload Your HVAC Proposal</h2>

        <p>
         Get a second opinion from the people who install and service HVAC systems.
         We'll review your proposal for pricing, equipment, missing details, and red
         flags—before you commit.
        </p>

        <form action="/upload" method="post" enctype="multipart/form-data">
            <label>Your Name</label>
            <input name="customer_name" required>

            <label>Email Address</label>
            <input name="customer_email" type="email" required>

            <input type="hidden" name="package" value="{package}">

            <label>Upload Quote File</label>
            <input name="files" type="file" accept=".pdf,.doc,.docx,.txt,.jpg,.jpeg,.png" multiple required>

            <p class="small">
                By uploading, you agree that this review is for informational purposes only
                and does not replace a licensed contractor inspection.
            </p>

            <button type="submit">Upload Quote</button>
        </form>
    </div>
</body>
</html>
    """.replace("{package}",package) 