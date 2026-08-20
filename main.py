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

class QuoteClassification(BaseModel):
    quote_type: str
    system_type: str
    primary_scope: str

    repair_components: List[str] = Field(default_factory=list)
    replacement_components: List[str] = Field(default_factory=list)
    proposed_equipment: List[str] = Field(default_factory=list)
    diagnostic_evidence: List[str] = Field(default_factory=list)
    missing_information: List[str] = Field(default_factory=list)

    modules_required: List[str] = Field(default_factory=list)

    confidence: str = "moderate"

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

def classify_quotes(all_quotes_text: str) -> QuoteClassification:
    completion = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": """
You are the quote-classification stage for Check Your Tech, an independent HVAC proposal review service.

Your job is NOT to write the homeowner's final review.

Your job is to inspect the submitted HVAC proposal text and classify what kind of work is being proposed so the correct technical analysis modules can be used later.

Determine:

1. quote_type
Use one of:
- repair
- replacement
- mixed
- maintenance
- ductwork
- accessory
- unknown

2. system_type
Examples:
- split air conditioner
- split heat pump
- gas furnace
- dual fuel
- package unit
- mini split
- multi split
- air handler
- rooftop unit
- boiler
- unknown

3. primary_scope
Give a short plain-English description of the main work being proposed.

4. repair_components
List components being repaired or replaced as part of a repair.
Examples:
- compressor
- capacitor
- contactor
- condenser fan motor
- blower motor
- ECM module
- control board
- evaporator coil
- condenser coil
- TXV
- reversing valve
- refrigerant leak
- refrigerant recharge
- heat exchanger
- inducer motor
- gas valve
- thermostat

5. replacement_components
List only the generic equipment categories being replaced.

Allowed examples:
- outdoor unit
- furnace
- air handler
- evaporator coil
- heat pump
- package unit
- mini split
- thermostat
- ductwork

Never include manufacturer names, model numbers, efficiency ratings, tonnage, or equipment descriptions in replacement_components.
Those belong only in proposed_equipment.
Use "evaporator coil" when the proposal lists a cased or uncased coil paired with a furnace.
Use "air handler" only when the proposal specifically identifies an air handler or fan coil.
Do not classify an evaporator coil as an air handler.
Use "heat pump" only when the proposal specifically identifies the outdoor equipment as a heat pump.
If the proposal identifies the equipment as an air conditioner, condenser, condensing unit, or AC outdoor unit, use "outdoor unit".
Do not classify a condenser as a heat pump.
For a repair quote, leave replacement_components empty unless the proposal also recommends replacing an entire major HVAC unit or system.

Replacing an internal component such as a compressor, motor, control board, capacitor, contactor, TXV, coil component, or filter drier does not mean the outdoor unit, furnace, or air handler is being replaced.

An evaporator coil or condenser coil being replaced as a repair component does not make the quote a system replacement.

For a repair quote involving only an evaporator coil or condenser coil:
- put the coil in repair_components
- leave replacement_components empty

Only use replacement_components when an entire major HVAC unit or system is being replaced.

Example:
If a compressor inside an existing condenser is being replaced, repair_components should contain "compressor" and replacement_components should remain empty.

Example:
If the proposal says "Goodman GM9S96 furnace", replacement_components should contain "furnace", not "Goodman GM9S96 furnace".

6. proposed_equipment
List the exact equipment descriptions and model numbers actually shown in the proposal.

Examples:
- Goodman GM9S96 furnace
- Goodman GSX14 condenser
- CAPF3636 evaporator coil

Only include equipment when the proposal provides a specific manufacturer, model number, or identifiable equipment description.

Do not put generic repair parts such as compressor, capacitor, contactor, filter drier, motor, TXV, or control board in proposed_equipment.

If no specific equipment identification is provided, leave proposed_equipment empty.
Do not invent model numbers.

7. diagnostic_evidence
List only diagnostic measurements, observations, fault codes, or test results actually documented in the quote.
Do not invent evidence.

8. missing_information
List important information that appears necessary to understand the proposed work but is not shown.

9. modules_required
Choose all relevant analysis modules from:
- compressor
- refrigerant_system
- electrical_diagnosis
- motors
- furnace_combustion
- heat_exchanger
- controls
- equipment_matching
- sizing
- duct_airflow
- lineset
- electrical_scope
- gas_scope
- condensate
- commissioning
- warranty
- repair_vs_replace
- pricing
- multi_quote_comparison

Only include "multi_quote_comparison" when two or more separate contractor quotes are submitted.
Never include "multi_quote_comparison" for a single quote.
For compressor replacement repairs, always include:
- compressor
- refrigerant_system
- electrical_diagnosis
- warranty
- repair_vs_replace
- pricing

Also include "commissioning" when the proposed repair includes refrigerant recovery, pressure testing, evacuation, vacuum targets, charging, or startup procedures.

Include "warranty" whenever any manufacturer or contractor warranty is mentioned or should reasonably be checked for a major repair.

Include "repair_vs_replace" for major repairs such as compressor, heat exchanger, evaporator coil, condenser coil, or other high-cost repairs where system age and repair economics matter.

Include "pricing" whenever a repair or replacement price is provided.

For refrigerant leak or evaporator/condenser coil repair quotes:

Include:
- refrigerant_system
- warranty
- repair_vs_replace
- pricing
- commissioning

Only include "compressor" if the proposal actually involves compressor diagnosis, compressor repair, or compressor replacement.

Only include "electrical_diagnosis" when the proposal documents or proposes investigation of an electrical problem.

Do not include "compressor" or "electrical_diagnosis" simply because the system has low refrigerant, a refrigerant leak, or a leaking evaporator or condenser coil.

10. confidence
Use:
- high
- moderate
- low

Rules:
- Never invent model numbers, test readings, diagnoses, or scope.
- If information is missing, say it is missing.
- A component mentioned in a quote is not automatically proven failed.
- Distinguish what the contractor documented from what would normally need verification.
"""
            },
            {
                "role": "user",
                "content": f"""
Classify the following HVAC quote or quotes:

{all_quotes_text}
"""
            }
        ],
        response_format=QuoteClassification,
    )

    return completion.choices[0].message.parsed

ANALYSIS_MODULES = {
    "compressor": """
COMPRESSOR REPAIR ANALYSIS RULES

When a proposal recommends compressor replacement, review the diagnosis, repair scope, warranty, and repair-vs-replacement economics separately.

DIAGNOSIS

Do not assume the compressor is proven failed merely because replacement is recommended.

Look for documented evidence such as:
- correct line voltage reaching the compressor
- contactor operation
- capacitor test results when applicable
- compressor winding resistance
- continuity/open winding findings
- winding-to-ground or insulation resistance testing
- starting amperage or locked-rotor behavior
- internal overload condition
- operating pressures or other mechanical evidence when the compressor can run
- fault codes or manufacturer diagnostic information when applicable

Not every test is required in every situation.

Separate:
- what the contractor actually documented
- what appears supported
- what still needs verification

If compressor winding resistance readings are provided, evaluate whether they appear internally consistent for the type of compressor being tested. Do not invent acceptable resistance values.

If no winding-to-ground or insulation test is documented on a major compressor repair, identify it as an important item to verify before authorizing the repair.

Do not describe the diagnosis as incomplete solely because this test is not listed.

If the proposal includes other meaningful evidence of a compressor starting or electrical problem, acknowledge that evidence.

Use wording such as:
"The proposal documents evidence of a compressor starting problem, but I do not see an insulation-to-ground test listed. Confirming whether the compressor windings were checked to ground would strengthen the diagnosis before authorizing a major repair."

Do not say that the compressor diagnosis is wrong unless the documented evidence actually contradicts it.

CAPACITOR AND STARTING COMPONENTS

If capacitor readings are provided, compare the measured value with the capacitor's labeled rating and tolerance when available.

A weak capacitor, contactor problem, voltage problem, wiring issue, control problem, or other starting-component failure can sometimes create symptoms that resemble compressor trouble.

Do not claim one of these caused the failure unless the proposal supports it.

ELECTRICAL VS MECHANICAL FAILURE

When possible, determine whether the documented failure appears:
- electrically open
- shorted
- grounded
- locked or mechanically unable to start
- overheating/on internal overload
- otherwise unclear

If the proposal does not provide enough information to distinguish the failure type, say so.

REPAIR SCOPE

For compressor replacement, look for appropriate supporting scope such as:
- refrigerant recovery
- compressor replacement
- filter drier replacement
- brazing/refrigerant piping work
- pressure or leak testing
- evacuation
- documented vacuum target when provided
- refrigerant recharge
- final charging and startup
- operational verification

Do not automatically call missing proposal language bad workmanship. If an important procedure is not written, identify it as something the homeowner should confirm.

REFRIGERANT SYSTEM

Check:
- refrigerant type
- refrigerant quantity included
- whether additional refrigerant has an extra charge
- filter drier replacement
- pressure testing
- evacuation
- charging/startup procedures

If compressor burnout or severe contamination is specifically documented, consider whether additional cleanup procedures may be required. Do not assume a burnout occurred unless the quote says so.

WARRANTY

For a compressor repair, check whether:
- the compressor may still have manufacturer parts coverage
- registration or serial-number verification is needed
- labor is covered separately
- refrigerant is covered or excluded
- the contractor provides a labor/workmanship warranty

Do not assume that a long manufacturer parts warranty also covers labor.

If warranty status is unclear, recommend verifying it before authorizing a major compressor repair.

WARRANTY CERTAINTY

Treat conditional warranty language such as "may apply", "might be covered", "could qualify", "potentially covered", or "eligible if registered" as unverified warranty status.

Never present conditional warranty coverage as confirmed coverage.

If the proposal says a manufacturer warranty may apply, clearly tell the homeowner that warranty eligibility should be verified using the equipment model, serial number, registration status, and manufacturer records before the repair is authorized.

Separate:
- manufacturer parts coverage
- contractor labor coverage
- refrigerant coverage
- diagnostic or service charges

Do not describe a manufacturer warranty as "standard" unless that is actually established by the proposal or verified information.

Example:
If the proposal says "10-year manufacturer parts warranty may apply", say:
"The proposal indicates that manufacturer compressor coverage may apply, but it is not confirmed. Verify the serial number and warranty status before authorizing the repair."

Do not say:
"The compressor has a 10-year manufacturer warranty."

REPAIR VS REPLACEMENT

Consider:
- system age
- refrigerant type
- repair price
- warranty coverage
- prior major repairs if known
- overall system condition if documented
- remaining equipment age
- replacement alternative if provided
- labor warranty
- likelihood of additional aging-component failures

Do not use a rigid percentage rule to automatically recommend repair or replacement.

Use conclusions such as:
- repair appears economically reasonable
- replacement deserves comparison
- replacement may offer better long-term value
- insufficient information to determine

PRICING

Evaluate the complete installed repair price, not just the internet cost of the compressor.

Account for legitimate labor and scope such as:
- refrigerant recovery
- brazing
- compressor removal and installation
- filter drier
- nitrogen/pressure testing
- evacuation
- refrigerant
- startup
- warranty exposure
- difficult equipment access when documented

A high price alone is not proof of overcharging.

PARTS AND LABOR TRANSPARENCY

When evaluating repair pricing, look for separate or reasonably identifiable costs for:
- major replacement parts
- refrigerant and materials
- labor
- diagnostic or service charges
- additional required materials

If the quote provides a parts and labor breakdown, use it to determine whether the apparent pricing concern comes primarily from:
- unusually high part pricing
- unusually high labor pricing
- refrigerant or material markup
- or the overall installed price

If the proposal gives only one flat repair price, do not automatically accuse the contractor of overcharging.

Instead:
- identify the lack of itemization as missing pricing transparency
- explain that the total installed price can still be reviewed
- recommend asking for a parts-and-labor breakdown when the repair is expensive
- lower confidence in any conclusion about exactly where the markup is occurring

Do not assume the contractor's wholesale cost is the appropriate homeowner price.

Account for legitimate contractor markup, overhead, warranty exposure, labor burden, tools, insurance, travel, and callbacks.

However, unusually high parts or labor pricing compared with reasonable installed market expectations should be identified and explained.

HOMEOWNER QUESTIONS

When appropriate, generate specific questions such as:
- What testing confirmed that the compressor itself has failed?
- Were the compressor windings checked to ground?
- Is the compressor still covered under the manufacturer's parts warranty?
- Does this price include all refrigerant needed after the repair?
- Is a new filter drier included?
- Will the system be pressure tested and evacuated after the compressor is replaced?
- What labor warranty applies to this repair?
- Given the system's age and repair cost, what would full replacement cost for comparison?

Keep the final homeowner explanation practical and concise.
""",

"refrigerant_system": """
REFRIGERANT SYSTEM AND COIL REPAIR ANALYSIS RULES

Use these rules when a proposal involves:
- refrigerant leaks
- refrigerant recharge
- evaporator coil replacement
- condenser coil replacement
- refrigerant piping repairs
- filter drier replacement
- leak search or leak detection

DIAGNOSIS AND LEAK CONFIRMATION

Do not assume a refrigerant leak has been conclusively located merely because the system is low on refrigerant.

Look for documented evidence such as:
- measured system pressures
- documented low refrigerant charge
- electronic leak detector findings
- soap bubble confirmation
- ultraviolet dye findings when applicable
- visible oil residue
- nitrogen pressure testing
- identified leak location
- documented coil or tubing damage

Separate:
- evidence that the system is low on refrigerant
- evidence that a leak exists
- evidence identifying the specific leak location

A low refrigerant charge supports the possibility of a leak but does not by itself prove where the leak is located.

When multiple pieces of evidence point to the same location, acknowledge that the diagnosis is better supported.

Do not invent leak-test results that are not documented.

RECHARGE-ONLY PROPOSALS

HVAC refrigerant normally operates in a sealed system and is not routinely consumed during normal operation.

If a proposal recommends adding a meaningful amount of refrigerant without documenting why the system is low or addressing a suspected leak, identify this as an important item to clarify.

Do not automatically call a recharge-only repair improper.

Possible legitimate explanations may include:
- a previous repair
- incomplete original charge
- known prior refrigerant loss
- recent equipment or piping work

If the reason is not documented, recommend asking why the system is low and whether a leak search is appropriate.

COIL REPLACEMENT

When an evaporator or condenser coil is being replaced, evaluate:
- whether the leak or failure location is reasonably supported
- whether the exact replacement coil is identified
- manufacturer parts warranty status
- labor warranty
- refrigerant recovery
- filter drier replacement
- brazing or piping work
- pressure testing
- evacuation
- refrigerant recharge
- final startup and operation verification

Replacing a coil inside an existing system is a repair, not automatically a full system replacement.

Do not assume that replacement of an evaporator coil means the air handler or furnace is also being replaced.

FILTER DRIER

A new liquid-line filter drier is normally an important part of refrigerant-system repairs that open the sealed system.

If the repair opens the refrigerant circuit and no filter drier is mentioned, identify it as an item to confirm rather than automatically declaring the repair improper.

PRESSURE TESTING

When pressure testing is documented, give the contractor credit for including it.

Look for:
- nitrogen or another appropriate dry inert gas
- documented test procedure when provided
- leak verification after repair

Do not invent a required pressure value unless equipment or manufacturer information establishes one.

EVACUATION

When evacuation is included, evaluate whether the proposal provides:
- a vacuum target
- micron measurement
- decay or standing vacuum verification when documented

A target below 500 microns is commonly associated with proper deep evacuation practices, but do not assume the actual final vacuum was achieved until the work is completed and documented.

Do not confuse vacuum level with refrigerant pressure.

REFRIGERANT CHARGE

Check:
- refrigerant type
- quantity included in the quoted price
- whether additional refrigerant costs extra
- whether the final charge will be verified
- whether charging procedures are appropriate for the equipment when documented

Do not assume the number of pounds listed is the exact final charge unless the proposal or manufacturer information establishes it.

If the proposal includes "up to" a certain amount of refrigerant, make that distinction clear.

REFRIGERANT TYPE AND SYSTEM AGE

Consider refrigerant type when evaluating repair-versus-replacement economics.

Older or phased-down refrigerants may affect:
- refrigerant cost
- future serviceability
- long-term repair economics

Do not recommend replacement solely because a system uses an older refrigerant.

Consider system age, condition, repair cost, warranty, and available replacement alternatives together.

WARRANTY

For coil and refrigerant-system repairs, separate:
- manufacturer parts warranty
- contractor labor warranty
- refrigerant coverage
- diagnostic or service charges
- additional refrigerant charges

Treat wording such as "may qualify", "may apply", "possibly covered", or "eligible if registered" as unverified warranty status.

Do not present conditional coverage as confirmed coverage.

If manufacturer coverage may apply, recommend verifying model number, serial number, registration status, and manufacturer warranty records before authorizing an expensive repair.

REPAIR VS REPLACEMENT

For major coil repairs, consider:
- equipment age
- repair price
- refrigerant type
- warranty coverage
- overall system condition if documented
- prior major repairs if known
- condition and age of the remaining equipment
- replacement alternative if available

Do not use a rigid percentage rule.

Use conclusions such as:
- repair appears economically reasonable
- replacement deserves comparison
- replacement may offer better long-term value
- insufficient information to determine

PRICING AND TRANSPARENCY

Evaluate the complete installed repair price, including legitimate scope such as:
- coil or replacement component
- refrigerant recovery
- brazing and piping work
- filter drier
- nitrogen pressure testing
- evacuation
- refrigerant
- startup
- labor
- warranty exposure
- difficult access when documented

Look for separate or reasonably identifiable pricing for:
- replacement coil or major part
- refrigerant and materials
- labor
- diagnostic or leak-search charges

If the proposal gives only one flat repair price, do not automatically accuse the contractor of overcharging.

Instead:
- identify limited itemization as reduced pricing transparency
- explain that the total installed price can still be evaluated
- recommend asking for a parts, refrigerant, and labor breakdown on expensive repairs
- lower confidence in conclusions about exactly where markup is occurring

Do not use online wholesale equipment cost as the homeowner's expected installed price.

Contractor markup, overhead, labor burden, insurance, warranty responsibility, tools, transportation, and callbacks are legitimate business costs.

REGIONAL PRICING LIMITATION

Unless verified regional pricing data has been provided to the analysis, do not label a repair price as high, low, overpriced, below market, or above market.

Do not invent a regional price range or rely on general assumptions about what a repair "should" cost.

Without verified regional pricing data:
- evaluate whether the scope appears complete
- evaluate pricing transparency and itemization
- identify what parts, refrigerant, materials, and labor are included
- explain when limited itemization makes the source of the total cost unclear
- recommend obtaining an itemized breakdown or comparison quote when appropriate
- state that a precise market-price comparison requires verified regional pricing data

When verified regional pricing data is available from the pricing module, use that information to determine whether parts, refrigerant, labor, or the total installed price appear unusually high or low.

When verified regional pricing data is NOT available:

- Do not use the size of the quoted dollar amount itself as evidence that the price may be high, excessive, unreasonable, or concerning.
- Do not recommend additional quotes merely because the total dollar amount appears large.
- Do not imply that a specific quoted price is unusual for the market.
- Do not compare the quoted amount to an assumed normal, average, typical, fair, or expected price.
- A recommendation to obtain another quote must be based on a specific non-price reason, such as unclear scope, unsupported diagnosis, missing itemization, missing warranty information, or uncertainty about what work is included.
- If discussing the quoted total, state it factually without judging its market value.
- If appropriate, explain that verified regional pricing data would be needed to determine whether the quoted amount is competitive for the local market.

LEAK DIAGNOSIS LIMITATION

Do not treat low refrigerant charge, low suction pressure, poor cooling performance, or a recommendation to add refrigerant as proof that an evaporator coil, condenser coil, or other specific component is leaking.

A specific refrigerant leak location should only be treated as confirmed when the proposal includes supporting evidence such as:
- electronic leak detector findings
- soap bubble confirmation
- UV dye evidence
- nitrogen pressure testing with documented leak location
- visible oil residue consistent with a refrigerant leak
- documented isolation testing
- another clearly stated diagnostic method that identifies the leak location

If the proposal states that the system is low on refrigerant but does not identify how the leak was located:
- state that refrigerant loss may indicate a leak somewhere in the sealed system
- do not assume the evaporator coil or condenser coil is the failed component
- identify the missing leak-location evidence
- recommend confirming the source of the leak before approving a major coil replacement

If the contractor recommends replacing a coil without documented evidence locating the leak at that coil, flag the diagnosis as insufficiently supported rather than automatically calling the repair unnecessary.

POSITIVE FINDINGS

Give credit when the proposal clearly includes good practices such as:
- documented leak location
- multiple forms of leak evidence
- refrigerant recovery
- filter drier replacement
- nitrogen pressure testing
- deep evacuation
- documented vacuum target
- refrigerant recharge
- final operation verification
- clear labor warranty

Do not make every report negative. Good documented scope should be acknowledged.

HOMEOWNER QUESTIONS

When appropriate, generate questions such as:
- What testing confirmed the exact location of the refrigerant leak?
- Is the evaporator or condenser coil still covered under the manufacturer's parts warranty?
- Does this price include all refrigerant required after the repair?
- What is the charge if more refrigerant is needed than the amount included?
- Is a new liquid-line filter drier included?
- Will the system be pressure tested with nitrogen after the repair?
- What vacuum level will be achieved and documented before charging?
- What labor warranty applies to the replacement coil?
- Can you provide separate pricing for the coil, refrigerant/materials, and labor?
- Given the system's age and repair cost, what would replacement cost for comparison?

Keep the final homeowner explanation practical, technically grounded, and concise.
"""
}

def get_analysis_knowledge(classification: QuoteClassification) -> str:
    selected_modules = []

    for module_name in classification.modules_required:
        module_text = ANALYSIS_MODULES.get(module_name)

        if module_text:
            selected_modules.append(module_text.strip())

    if not selected_modules:
        return ""

    return "\n\n".join(selected_modules)

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

    classification = classify_quotes(all_quotes_text)
    print("QUOTE CLASSIFICATION:", classification.model_dump())

    analysis_knowledge = get_analysis_knowledge(classification)
    print("ANALYSIS KNOWLEDGE LENGTH:", len(analysis_knowledge))
    
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

SELECTED QUOTE CLASSIFICATION:

{classification.model_dump_json(indent=2)}

SELECTED TECHNICAL ANALYSIS KNOWLEDGE:

{analysis_knowledge if analysis_knowledge else "No additional module-specific knowledge was selected."}

Use the selected technical knowledge only when it applies to the submitted quote.
The quote itself remains the source of truth.
Do not invent measurements, diagnoses, model numbers, scope, warranties, or other facts that are not actually documented.s
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