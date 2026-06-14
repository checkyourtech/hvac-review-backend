import os
import io
import html
import base64
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field
from pypdf import PdfReader
import fitz  # PyMuPDF

load_dotenv()

app = FastAPI(title="HVAC Quote Analyzer")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

EMAIL_USER = "reviews@checkyourtechs.com"
BUSINESS_EMAIL = "reviews@checkyourtechs.com"
WEBSITE_URL = "https://www.checkyourtech.info"
LOGO_URL = "https://static.wixstatic.com/media/9d7356_1bf8d4c42f3c489e92676cbe764366c5~mv2.png/v1/fill/w_496,h_372,al_c,q_85,usm_0.66_1.00_0.01,enc_auto/file_00000000c56c71f598c3b252c7b1d746.png"


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
    package_key = (request.packageName or request.package or "tier1").lower().strip()
    customer_name = request.customerName or request.customer_name or "Website Customer"
    customer_email = request.customerEmail or request.customer_email or ""

    if package_key not in PACKAGE_RULES:
        raise HTTPException(status_code=400, detail="Invalid package. Use tier1, tier2, or tier3.")

    if package_key == "tier1" and len(request.files) > 1:
        raise HTTPException(status_code=400, detail="Tier 1 only allows 1 quote upload.")

    if package_key in ["tier2", "tier3"] and len(request.files) > 3:
        raise HTTPException(status_code=400, detail="Tier 2 and Tier 3 allow up to 3 quote uploads.")

    quote_blocks = []
    file_names = []

    for index, uploaded_file in enumerate(request.files, start=1):
        file_name = uploaded_file.fileName or uploaded_file.originalFileName or f"quote{index}.pdf"
        download_url = uploaded_file.downloadUrl or uploaded_file.fileUrl

        file_bytes = download_file(download_url)
        quote_text = extract_file_text(file_bytes, file_name)

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
You are a professional HVAC estimator, service technician, and homeowner advocate.

Review the submitted HVAC quote or quotes for a homeowner.

Package scope:
{PACKAGE_RULES[package_key]}

Important:
- Tier 1 reviews only one quote.
- Tier 2 compares up to three quotes and recommends the best overall option.
- Tier 3 compares up to three quotes and includes contractor vetting if data is provided.
- Do not invent facts.
- If something is missing, say it is missing.
- If handwriting or OCR is unclear, state that clearly.
- Keep the tone professional and homeowner-friendly.
- Do not accuse contractors of dishonesty.
- Do not guarantee savings.

Look for:
- Missing equipment model numbers
- Missing AHRI match number
- Missing SEER2, EER2, HSPF2, or AFUE ratings
- Missing labor/material cost breakdown
- Missing permit details
- Missing ductwork scope
- Missing electrical scope
- Missing condensate drain details
- Missing thermostat details
- Missing refrigerant line set details
- Missing refrigerant pressures
- Missing superheat/subcooling data
- Missing airflow/static pressure information
- Missing warranty terms
- Suspiciously vague pricing
- Missing exclusions
- Missing cleanup/disposal details
- Missing payment schedule
- Missing startup sheet
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

    return analysis
