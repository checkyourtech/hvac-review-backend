import os
import smtplib
import html
from email.message import EmailMessage
from typing import List

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from openai import OpenAI
from pydantic import BaseModel
from serpapi import GoogleSearch


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


PACKAGE_RULES = {
    "tier1": """
TIER 1 - Quick HVAC Quote Check:
- Review only 1 quote.
- Provide a basic homeowner-friendly review.
- Identify obvious red flags.
- Identify missing information.
- Give a simple recommendation.
- Do NOT compare multiple quotes.
- Do NOT perform contractor vetting.
""",
    "tier2": """
TIER 2 - Pro HVAC Quote Comparison:
- Review up to 3 quotes.
- Compare the quotes side-by-side.
- Identify best overall value.
- Flag high labor pricing.
- Flag high material pricing.
- Flag vague or missing proposal details.
- Compare equipment, warranties, startup details, permits, and scope.
- Do NOT perform contractor vetting.
""",
    "tier3": """
TIER 3 - Full Protection Review:
- Review up to 3 quotes.
- Compare the quotes side-by-side.
- Identify best overall value.
- Flag high labor pricing.
- Flag high material pricing.
- Review equipment, warranties, startup details, permits, ductwork, electrical scope, and documentation.
- Include contractor vetting using online search results when contractor names are provided.
- Do NOT invent online reviews, complaints, licenses, or city records.
"""
}


@app.get("/")
def root():
    return {"status": "online"}


def esc(value):
    return html.escape(str(value))


def make_list(items):
    if not items:
        return "<li>No major items identified based on the submitted quote.</li>"
    return "".join(f"<li>{esc(item)}</li>" for item in items)


def search_contractor_online(contractor_name, city, state):
    serpapi_key = os.getenv("SERPAPI_API_KEY")

    if not serpapi_key:
        return "SERPAPI_API_KEY is missing. Contractor vetting could not be performed."

    if not contractor_name.strip():
        return "No contractor name provided."

    searches = [
        f"{contractor_name} {city} {state} reviews",
        f"{contractor_name} {city} {state} complaints",
        f"{contractor_name} {city} {state} BBB",
        f"{contractor_name} {city} {state} license",
        f"{contractor_name} {city} {state} permit complaints"
    ]

    results_text = []

    for query in searches:
        try:
            search = GoogleSearch({
                "q": query,
                "api_key": serpapi_key,
                "num": 5
            })

            results = search.get_dict()
            organic_results = results.get("organic_results", [])

            results_text.append(f"\nSearch Query: {query}")

            if not organic_results:
                results_text.append("No search results found.")
                continue

            for item in organic_results[:5]:
                title = item.get("title", "No title")
                link = item.get("link", "No link")
                snippet = item.get("snippet", "No snippet")

                results_text.append(
                    f"- Title: {title}\n  Link: {link}\n  Snippet: {snippet}"
                )

        except Exception as e:
            results_text.append(f"Search failed for {query}: {str(e)}")

    return "\n".join(results_text)


def send_review_email(customer_name, customer_email, package, file_names, analysis):
    email_password = os.getenv("EMAIL_APP_PASSWORD")

    if not email_password:
        raise Exception("Missing EMAIL_APP_PASSWORD")

    body = f"""
<html>
<body style="font-family: Arial, sans-serif; background-color:#f4f4f4; padding:20px;">

<div style="max-width:700px; margin:auto; background:white; border-radius:10px; overflow:hidden; border:1px solid #ddd;">

    <div style="background:#1f2937; padding:25px; text-align:center;">
        <img src="{LOGO_URL}" width="180">
    </div>

    <div style="padding:30px; color:#333;">

        <h2 style="color:#111827;">NEW CHECK YOUR TECH REVIEW</h2>

        <p style="line-height:1.6;">
            <b>Customer Name:</b> {esc(customer_name)}<br>
            <b>Customer Email:</b> {esc(customer_email)}<br>
            <b>Selected Package:</b> {esc(package)}<br>
            <b>Uploaded Files:</b> {esc(", ".join(file_names))}
        </p>

        <hr style="margin:25px 0;">

        <h3>Project Overview</h3>
        <p style="line-height:1.7;">{esc(analysis.project_overview)}</p>

        <h3>Equipment Analysis</h3>
        <p style="line-height:1.7;">{esc(analysis.equipment_analysis)}</p>

        <h3>Missing Information</h3>
        <p style="line-height:1.7;">{esc(analysis.missing_information)}</p>

        <h3>Pricing Review</h3>
        <p style="line-height:1.7;">{esc(analysis.pricing_review)}</p>

        <h3>Installation Concerns</h3>
        <p style="line-height:1.7;">{esc(analysis.installation_concerns)}</p>

        <h3>Quote Comparison</h3>
        <p style="line-height:1.7;">{esc(analysis.quote_comparison)}</p>

        <h3>Best Quote Recommendation</h3>
        <p style="line-height:1.7;">{esc(analysis.best_quote_recommendation)}</p>

        <h3>Contractor Vetting</h3>
        <p style="line-height:1.7;">{esc(analysis.contractor_vetting)}</p>

        <h3 style="color:#dc2626;">Red Flags</h3>
        <ul style="line-height:1.8;">
            {make_list(analysis.red_flags)}
        </ul>

        <h3 style="color:#16a34a;">Good Signs</h3>
        <ul style="line-height:1.8;">
            {make_list(analysis.good_signs)}
        </ul>

        <h3>Final Recommendation</h3>
        <p style="line-height:1.7;">{esc(analysis.recommendation)}</p>

        <div style="margin-top:30px;">
            <hr style="margin:25px 0;">

            <p style="font-size:14px; color:#666; line-height:1.6;">
                <b>DISCLAIMER:</b><br><br>
                This review is intended to help homeowners identify potential concerns,
                missing information, or areas that may require clarification before
                proceeding with HVAC work.<br><br>

                This review is not a substitute for an in-person inspection,
                load calculation, or licensed engineering evaluation.
                Customers are encouraged to compare multiple quotes and verify
                contractor licensing, permits, insurance, equipment specifications,
                and installation details before making a final decision.
            </p>
        </div>

    </div>

    <div style="background:#f3f4f6; padding:20px; text-align:center; font-size:14px; color:#555;">
        <b>Check Your Tech</b><br>
        HVAC Quote Review & Consumer Protection Services<br><br>

        <a href="{WEBSITE_URL}" style="color:#2563eb; text-decoration:none;">
        www.checkyourtech.info
        </a>
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
async def analyze_hvac_quote(
    package: str = Form(...),
    customer_name: str = Form(...),
    customer_email: str = Form(...),
    contractor_1_name: str = Form(""),
    contractor_2_name: str = Form(""),
    contractor_3_name: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    files: List[UploadFile] = File(...)
):
    package_key = package.lower().strip()

    if package_key not in PACKAGE_RULES:
        raise HTTPException(
            status_code=400,
            detail="Invalid package. Use tier1, tier2, or tier3."
        )

    if package_key == "tier1" and len(files) > 1:
        raise HTTPException(
            status_code=400,
            detail="Tier 1 only allows 1 quote upload."
        )

    if package_key in ["tier2", "tier3"] and len(files) > 3:
        raise HTTPException(
            status_code=400,
            detail="Tier 2 and Tier 3 allow up to 3 quote uploads."
        )

    quote_blocks = []
    file_names = []

    for index, uploaded_file in enumerate(files, start=1):
        contents = await uploaded_file.read()
        quote_text = contents.decode("utf-8", errors="ignore")
        file_names.append(uploaded_file.filename)

        quote_blocks.append(
            f"""
QUOTE {index}
File Name: {uploaded_file.filename}

{quote_text}
"""
        )

    all_quotes_text = "\n\n".join(quote_blocks)

    contractor_vetting_results = ""

    if package_key == "tier3":
        contractor_names = [
            contractor_1_name,
            contractor_2_name,
            contractor_3_name
        ]

        vetting_blocks = []

        for index, contractor in enumerate(contractor_names, start=1):
            if contractor.strip():
                result = search_contractor_online(contractor, city, state)
                vetting_blocks.append(
                    f"""
CONTRACTOR {index} ONLINE SEARCH RESULTS
Contractor Name: {contractor}
City: {city}
State: {state}

{result}
"""
                )

        if vetting_blocks:
            contractor_vetting_results = "\n\n".join(vetting_blocks)
        else:
            contractor_vetting_results = """
No contractor names were provided. Contractor vetting could not be performed.
"""
    else:
        contractor_vetting_results = """
Contractor vetting is only included with Tier 3.
"""

    completion = client.beta.chat.completions.parse(
        model="gpt-4o-2024-08-06",
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
- Tier 3 compares up to three quotes and includes contractor vetting using search results.
- Do not invent Google reviews, Yelp reviews, BBB complaints, license records, city complaints, or permit records.
- Only use the contractor vetting search results provided.
- If contractor vetting results are limited, unclear, or inconclusive, say that.
- If search results show complaints or negative patterns, summarize them carefully as possible concerns, not proven facts.

Look for:
- High labor rates
- High material pricing
- Missing labor/material breakdown
- Missing Manual J/load calculation
- Oversized or undersized equipment concerns
- Missing AHRI match number
- Missing equipment model numbers
- Missing SEER2, EER2, HSPF2, or AFUE ratings
- Missing permits
- Missing ductwork scope
- Missing electrical scope
- Missing condensate drain details
- Missing thermostat details
- Missing refrigerant line set details
- Missing startup sheet
- Missing refrigerant pressures
- Missing superheat/subcooling data
- Missing airflow/static pressure information
- Missing warranty terms
- Suspiciously vague pricing
- Missing exclusions
- Missing cleanup/disposal details
- Missing payment schedule
- Missing workmanship warranty

Rules:
- Do not invent facts.
- If something is missing, say it is missing.
- If something is only a concern, say it is a concern.
- Compare quote pricing, scope, equipment, warranty, documentation, and professionalism.
- Recommend the best quote based on value, transparency, completeness, and risk.
- Keep the tone professional and homeowner-friendly.
- Do not accuse contractors of dishonesty.
- Do not guarantee savings.
"""
            },
            {
                "role": "user",
                "content": f"""
Customer:
{customer_name}

Customer Email:
{customer_email}

Contractor Names:
Contractor 1: {contractor_1_name}
Contractor 2: {contractor_2_name}
Contractor 3: {contractor_3_name}

Location:
City: {city}
State: {state}

Submitted HVAC quote(s):
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
        package=package_key,
        file_names=file_names,
        analysis=analysis
    )

    return analysis
