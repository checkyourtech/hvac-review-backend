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
- electrical_controls
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

For electrical or control repair quotes:

Include:
- electrical_controls
- electrical_diagnosis

Select electrical_controls when the proposal involves repair, replacement, diagnosis, or testing of electrical or control components such as capacitors, contactors, relays, transformers, control boards, sequencers, fuses, disconnects, breakers, control wiring, high-voltage wiring, or burned electrical connections.

Use electrical_controls for component-level electrical/control repairs.

Do not select electrical_controls merely because an HVAC system contains electrical components. Select it only when electrical or control work is part of the proposed repair or diagnosis.

Select electrical_diagnosis whenever the proposal involves diagnosing or condemning an electrical/control component and the diagnosis depends on voltage, amperage, resistance, continuity, thermostat-call, fuse, transformer, motor, board-output, or fault-code testing.

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

Do not treat the absence of additional manufacturer warranty details beyond the warranty explicitly stated in the proposal as a red flag by itself.

If the proposal states a parts or labor warranty, report that coverage as documented. You may recommend confirming whether any existing equipment warranty applies, but classify that as useful follow-up information rather than a red flag unless there is evidence that valid warranty coverage is being ignored, misrepresented, or contradicted.

WARRANTY CERTAINTY

Treat conditional warranty language such as "may apply", "might be covered", "could qualify", "potentially covered", or "eligible if registered" as unverified warranty status.

Never present conditional warranty coverage as confirmed coverage.

If the proposal says a manufacturer warranty may apply, clearly tell the homeowner that warranty eligibility should be verified using the equipment model, serial number, registration status, and manufacturer records before the repair is authorized.

EMAIL_APP_PASSWORD="" python -c "import asyncio; from main import AnalyzeRequest, UploadedQuote, analyze_hvac_quote; r=AnalyzeRequest(customerName='Test Customer', customerEmail='test@example.com', city='Reno', state='NV', files=[UploadedQuote(fileName='electrical_compressor_good_test.txt', extractedText=open('electrical_compressor_good_test.txt').read())]); a=asyncio.run(analyze_hvac_quote(r)); print(a.model_dump_json(indent=2))"

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

If the proposal gives only one flat repair price, do not automatically treat the contractor or repair as unreasonable based on that alone.

However, itemized or reasonably identifiable pricing is preferred for transparency, especially on major refrigerant-system repairs.

When pricing is not itemized:
- identify the lack of itemization as reduced pricing transparency
- explain that the total installed price can still be evaluated for scope completeness
- recommend asking for a breakdown of major parts, refrigerant/materials, labor, and diagnostic or leak-search charges
- do not claim that any individual component, labor charge, or markup is excessive unless the proposal provides enough information to support that conclusion
- do not make lack of itemization a red flag by itself
- lower confidence in conclusions about exactly where the quoted cost is coming from

When a flat-rate or lump-sum repair price is not itemized, do not combine the size of the total price with the lack of itemization to create a red flag unless verified regional pricing data or another specific pricing issue supports that conclusion.

Do not use wording such as "high repair cost with no itemized breakdown" when the only known facts are the total price and lack of itemization.

Instead, describe the issue as limited pricing transparency and recommend requesting an itemized or reasonably identifiable breakdown.
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
""",

    "electrical_controls": """
ELECTRICAL / CONTROL REPAIR ANALYSIS RULES

When a proposal recommends an electrical or control repair, evaluate the documented diagnosis, the component being replaced, and whether the proposed repair is supported by the information provided.

COMMON COMPONENTS

Electrical/control repairs may include:
- run capacitor
- dual run capacitor
- start capacitor
- hard-start kit
- contactor
- relay
- transformer
- control board
- defrost board
- fan control board
- sequencer
- time-delay relay
- pressure switch
- temperature switch
- fuse
- disconnect
- breaker
- low-voltage wiring
- high-voltage wiring
- burned or loose electrical connections

DIAGNOSIS

Do not assume that a failed electrical component proves another major component has failed.

Examples:
- a burned contactor does not by itself prove the compressor is bad
- a blown fuse does not by itself identify the cause of the electrical fault
- a tripped breaker does not by itself prove an HVAC component has failed
- a failed capacitor does not automatically prove the motor or compressor it serves is damaged
- a failed transformer does not by itself establish what caused the transformer to fail

Look for documented diagnostic evidence when applicable, such as:
- measured capacitance compared with the capacitor rating and tolerance
- incoming and outgoing r motor or inducer assembly replacement proposals,
evaluate whether the documented evidence reasonably supports failure of the
inducer itself.

Do not assume an inducer motor is defective merely because:
- the furnace does not heat
- the inducer does not run
- a pressure-switch fault is present
- ignition does not begin
- the furnace has a draft-rvoltage readings
- control-voltage readings
- amperage readings
- resistance or continuity testing
- burned, pitted, or damaged electrical contacts
- loose or overheated wiring
- visible electrical damage
- fuse test results
- board fault codes or diagnostic LEDs
- evidence of a short circuit or grounded component

Not every test is required for every repair.

CONTROL BOARD REPAIRS

For a control board replacement, focus primarily on whether the documented diagnostic evidence reasonably supports condemning the board.

Relevant evidence may include thermostat call verification, transformer and fuse checks, proper control voltage, board inputs and outputs, fault codes or diagnostic LEDs, blower motor or controlled-component testing, and other testing appropriate to the reported failure.

Do not require every one of these tests. Evaluate whether the testing documented in the proposal is sufficient to reasonably support the diagnosis.

Do not treat the absence of an exact replacement control-board model or part number as a red flag by itself. Residential repair proposals often do not list the replacement board part number.

Avoid generic statements such as "there is no guarantee the new board will fix the problem." Instead, explain specifically what diagnostic evidence supports the board diagnosis and what important verification, if any, is missing.

BLOWER MOTOR AND ECM REPAIRS

For blower motor or ECM motor replacement, do not treat "blower not running" by itself as proof that the motor has failed.

Look for documented evidence such as:
- proper line voltage supplied to the blower motor or ECM module
- proper low-voltage or control command to the motor/module
- verification of thermostat or control-board fan command when relevant
- motor/module diagnostic testing
- resistance or winding testing when applicable to the motor type
- confirmation that wiring, harnesses, plugs, and connectors are intact
- confirmation that the blower wheel is not mechanically seized or obstructed
- verification that the control board is actually commanding blower operation
- manufacturer diagnostic codes or test procedures when documented
- differentiation between ECM module failure and motor failure when applicable

Do not require every test for every blower repair. Evaluate whether the documented evidence reasonably supports the proposed failure.

If the proposal only states that the blower does not run or that the motor is "bad" without supporting diagnostic evidence:
- identify the blower diagnosis as insufficiently supported
- do not assume the replacement motor will correct the problem
- recommend confirming power, control signal, wiring, and motor/module operation before authorizing replacement
- do not invent a control-board failure, thermostat failure, wiring failure, or other cause

If the proposal documents proper power and control inputs but the blower motor or ECM module fails to operate correctly, treat that as meaningful support for the diagnosis.

For ECM systems, distinguish when possible between:
- motor failure
- ECM/module failure
- missing control command
- power-supply problem
- wiring or harness problem
- mechanical blower-wheel problem

Do not claim one of these caused the failure unless the proposal supports it.

REPAIR SCOPE

For blower or ECM replacement, look for appropriate scope when applicable, such as:
- correct replacement motor/module specification
- proper mounting and blower-wheel installation
- wiring and connector verification
- airflow or speed-setting configuration when applicable
- verification of blower operation after repair
- verification of heating and cooling operation when relevant

Do not criticize, emphasize, or recommend follow-up solely because the proposal does not list the exact replacement blower motor, ECM module, model number, or part number.

If the proposal documents a well-supported blower/ECM diagnosis and states that an OEM-compatible or otherwise compatible replacement will be installed, do not treat the missing replacement part number as a concern by itself.

Only discuss replacement-part compatibility when the proposal contains conflicting equipment information, an obviously questionable substitution, or other documented facts that create a specific compatibility concern.

PRICING

Follow the existing repair pricing and transparency rules.

A flat-rate blower repair price is not proof of overcharging.

If pricing is not itemized, identify reduced pricing transparency without calling the repair overpriced unless verified regional pricing data supports that conclusion.

PROPOSAL DETAIL LIMITS

Do not treat the absence of an exact replacement part number or model number as a red flag by itself for a normal residential repair quote.

Do not treat the absence of separate parts and labor pricing as a red flag by itself. Many HVAC contractors use flat-rate repair pricing.

For control board repair proposals, do not criticize, emphasize, or recommend follow-up solely because the proposal lacks:
- an exact replacement board model or part number
- separate parts and labor pricing
- warranty language


INDUCER MOTOR / COMBUSTION DRAFT REPAIRS

For furnace draft inducer motor or inducer assembly replacement proposals,
evaluate whether the documented evidence reasonably supports failure of the
inducer itself.

Do not assume an inducer motor is defective merely because:
- the furnace does not heat
- the inducer does not run
- a pressure-switch fault is present
- ignition does not begin
- the furnace has a draft-related fault code

These symptoms can have multiple causes.

DIAGNOSTIC EVIDENCE

Relevant diagnostic evidence may include, when applicable:
- proper line voltage supplied to the inducer during a call for heat
- control-board output or inducer command verification
- inducer amperage measurements
- motor winding or resistance testing
- evidence that the inducer motor is seized, noisy, damaged, or not rotating
- inspection of the inducer wheel
- pressure-switch operation or draft-pressure measurements
- venting or combustion-air restriction inspection
- condensate drainage inspection on condensing furnaces
- wiring and connector inspection
- relevant furnace fault codes used together with supporting testing

Not every test is required for every diagnosis.

Evaluate whether the documented testing logically separates an actual inducer
motor failure from another problem that could prevent inducer operation or
proper draft.

Do not invent test results that are not documented in the proposal.

If the proposal merely states that the inducer is "bad" or needs replacement
without documenting supporting diagnostic evidence, identify the diagnosis as
insufficiently supported and recommend obtaining clarification about how the
inducer failure was confirmed.

REPAIR SCOPE

For inducer motor or inducer assembly replacement, appropriate scope may
include, when applicable:
- removal and replacement of the inducer motor or assembly
- inspection or replacement of applicable gaskets or seals
- verification of wiring and electrical connections
- verification of proper inducer operation
- confirmation that the pressure switch proves draft
- furnace operational testing through a complete heating cycle

Do not require every scope item when it is not applicable.

PROPOSAL DETAIL LIMITS

Do not treat the absence of an exact replacement inducer model or part number
as a red flag by itself.

Do not treat bundled or flat-rate parts-and-labor pricing as evidence of
overcharging by itself.

Focus primarily on whether the documented diagnostic evidence supports the
recommended repair and whether the proposed scope is reasonable.


Do not mention these omissions in equipment_analysis, pricing_review, installation_concerns, red_flags, or recommendation unless the missing information materially affects whether the repair is technically justified.

Keep the analysis focused primarily on whether the documented electrical diagnostic evidence reasonably supports the control board diagnosis.

Do not treat missing warranty language as a red flag by itself unless warranty coverage is especially important to the repair decision or the proposal makes a specific warranty claim that needs verification.

Distinguish between:
- information that is useful to ask for
- information that materially affects whether the diagnosis or repair is justified
- information that is merely not listed on a normal repair proposal

Red flags should focus on material issues such as unsupported diagnosis, contradictory scope, missing critical testing, unsafe or incomplete repair procedures, or other information that could meaningfully affect the homeowner's decision.

Separate:
- what the contractor actually documented
- what the documented evidence supports
- what remains uncertain

Do not invent electrical measurements or failure causes that are not stated in the proposal.

UNDERLYING CAUSE

Distinguish between the failed component and the condition that may have caused it to fail.

If an electrical component appears damaged but the proposal does not identify why it failed:
- do not automatically assume another component caused the failure
- identify the missing diagnostic information when it is important
- recommend further diagnosis only when the missing information materially affects the repair decision

A simple component failure does not always require an extensive root-cause investigation. Keep recommendations proportional to the repair.

REPAIR SCOPE

For electrical/control repairs, look for appropriate scope when applicable, such as:
- removal and replacement of the failed component
- correct replacement rating or specification
- repair of damaged terminals or wiring
- verification of proper voltage
- verification of operating amperage when relevant
- confirmation that the controlled component operates correctly after the repair
- system operational verification
- labor or parts warranty information

Do not call missing proposal language bad workmanship unless the missing information materially affects the homeowner's ability to evaluate the repair.

PRICING

Follow the regional pricing limitation rules.

Without verified regional pricing data:
- do not call the repair high, low, overpriced, cheap, fair, or unusually expensive
- do not describe the price as reasonable, unreasonable, appropriate, competitive, acceptable, good, bad, justified, or any similar judgment of price or value
- if verified regional pricing data is unavailable, explicitly state that the price cannot be judged from the available information
- evaluate pricing transparency and itemization instead
- identify what parts, labor, materials, or diagnostic charges are included when stated
- recommend another quote only for a specific non-price reason

POSITIVE FINDINGS

Give credit when the proposal documents good practices such as:
- actual electrical measurements
- clear identification of the failed component
- correct component specifications
- repair of burned or damaged wiring
- post-repair voltage or amperage verification
- operational testing after repair
- clear warranty coverage

PRESSURE SWITCH / DRAFT PROVING REPAIRS

For furnace pressure-switch replacement proposals, evaluate whether the
documented evidence reasonably supports failure of the pressure switch itself.

Do not assume the pressure switch is defective merely because:
- the furnace does not heat
- the pressure switch does not close
- a pressure-switch fault code is present
- ignition does not begin
- the inducer is running

These conditions do not by themselves prove a failed pressure switch.

DIAGNOSTIC EVIDENCE

Relevant diagnostic evidence may include, when applicable:
- verification that the inducer operates properly
- draft or pressure measurement
- comparison of measured draft to the pressure-switch rating
- pressure-switch continuity or proving behavior
- inspection of pressure tubing and pressure ports
- verification that tubing or ports are not blocked, cracked, loose, or damaged
- inspection for vent or combustion-air restriction
- inspection for condensate or drainage problems when applicable
- verification of wiring and electrical connections

Do not require every test for every repair.

The important question is whether the documented testing reasonably isolates
the pressure switch itself as the failed component.

If the proposal only states "bad pressure switch" or recommends replacement
without supporting diagnostic evidence, identify the diagnosis as insufficiently
supported.

Do not invent missing measurements or tests.

Do not assume a pressure-switch fault code proves the switch itself failed.

REPAIR SCOPE

For pressure-switch replacement, appropriate scope may include:
- removal and replacement of the pressure switch
- reconnection of tubing and wiring
- verification of proper draft proving
- furnace operational testing through a complete heating cycle

Do not require every scope item when it is not applicable.

PROPOSAL DETAIL LIMITS

Do not treat the absence of an exact replacement pressure-switch model or part
number as a red flag by itself.

Do not treat bundled or flat-rate parts-and-labor pricing as evidence of
overcharging by itself.

Keep the analysis focused primarily on whether the documented diagnostic
evidence supports replacement of the pressure switch and whether the proposed
repair is reasonable.


Keep the final homeowner explanation practical, technically grounded, and concise.

IGNITER_REPAIR_LOGIC =
HOT SURFACE IGNITER / IGNITION REPAIRS

For furnace hot-surface igniter or ignition-component replacement proposals,
evaluate whether the documented evidence reasonably supports failure of the
igniter itself.

Do not assume an igniter is defective merely because:
- the furnace does not heat
- ignition does not occur
- the burners do not light
- an ignition-related fault code is present
- the igniter does not glow

These symptoms can have multiple causes.

DIAGNOSTIC EVIDENCE

Relevant diagnostic evidence may include, when applicable:
- verification that the furnace reaches the ignition stage of the sequence
- verification that the control board supplies proper voltage to the igniter
- igniter resistance or continuity testing
- evidence that the igniter is electrically open
- visual evidence of a cracked, broken, burned, or damaged igniter
- verification of wiring and electrical connections
- confirmation that upstream safeties or controls are not preventing ignition

Do not require every test for every repair.

The important question is whether the documented testing reasonably isolates
the igniter itself as the failed component.

If the proposal only states "bad igniter", "bad hot surface igniter", or
recommends igniter replacement without supporting diagnostic evidence,
identify the diagnosis as insufficiently supported.

Do not invent missing measurements or tests.

Separate:
- what the contractor actually documented
- what that evidence supports
- what remains uncertain

REPAIR SCOPE

For igniter replacement proposals, reasonable scope may include, when applicable:
- removal and replacement of the failed igniter
- inspection of wiring and electrical connections
- verification of proper igniter operation
- verification that ignition occurs correctly
- furnace operational testing through a complete heating cycle

Do not require every scope item when it is not applicable.

PROPOSAL DETAIL LIMITS

Do not treat the absence of an exact replacement igniter model or part number
as a red flag by itself.

Do not treat bundled or flat-rate parts-and-labor pricing as evidence of
overcharging by itself.

Focus primarily on whether the documented diagnostic evidence supports the
recommended repair and whether the proposed scope is reasonable.

FLAME SENSOR / FLAME PROVING REPAIRS

For furnace flame-sensor replacement proposals, evaluate whether the documented
evidence reasonably supports failure of the flame sensor itself.

Do not assume the flame sensor is defective merely because:
- the burners ignite and then shut off
- the furnace locks out after ignition
- a flame-related fault code is present
- flame is not proven
- the furnace cycles repeatedly

These symptoms can have multiple causes.

DIAGNOSTIC EVIDENCE

Relevant diagnostic evidence may include, when applicable:
- verification that the burners ignite normally
- flame-signal microamp measurement
- comparison of measured flame signal to manufacturer requirements when documented
- inspection of the flame sensor for contamination, oxidation, cracking, or damage
- verification that the flame sensor is properly positioned in the burner flame
- cleaning of the flame sensor followed by retesting
- verification of flame-sensor wiring and electrical connections
- verification of proper furnace grounding
- inspection of burner flame quality when relevant
- confirmation that the control board is receiving or responding to the flame-proving signal

Do not require every test for every repair.

The important question is whether the documented testing reasonably isolates
the flame sensor itself as the cause of the flame-proving problem.

A burner-lighting-then-shutting-off symptom is consistent with a flame-proving
problem, but it is NOT sufficient evidence by itself that the flame sensor has failed.

Do not describe the flame-sensor diagnosis as:
- well-supported
- confirmed
- justified
- reasonable to proceed with
unless the proposal documents actual diagnostic evidence that isolates the
flame sensor or flame-sensing circuit.

If no flame-signal measurement, sensor inspection, cleaning/retest result,
grounding verification, wiring verification, or other meaningful flame-proving
test is documented, treat the diagnosis as insufficiently supported.

In that situation:
- add a red flag for insufficient diagnostic evidence
- explain that the symptom alone does not prove a failed flame sensor
- recommend asking what testing confirmed the flame sensor itself requires replacement
- do not recommend proceeding with the repair based solely on the symptom

When symptoms are consistent with a flame-proving problem but the proposal does
not document testing that isolates the flame sensor itself, describe the diagnosis
as plausible or consistent with the symptoms, not as well-supported, confirmed,
or proven.

Use language such as:
"The symptoms are consistent with a flame-proving problem, but the quote does
not document enough testing to confirm that the flame sensor itself has failed."

If the proposal only states "bad flame sensor" or recommends flame-sensor
replacement without supporting diagnostic evidence, identify the diagnosis
as insufficiently supported.

Do not invent missing flame-signal measurements, test results, or observations.

Separate:
- what the contractor actually documented
- what that evidence supports
- what remains uncertain

REPAIR SCOPE

For flame-sensor replacement proposals, reasonable scope may include, when applicable:
- removal and replacement of the flame sensor
- inspection of wiring and electrical connections
- verification of proper flame-sensor positioning
- verification of flame signal after repair
- furnace operational testing through a complete heating cycle

Do not require every scope item when it is not applicable.

PROPOSAL DETAIL LIMITS

Do not treat the absence of an exact flame-sensor model or part number as a red flag by itself.

Do not treat bundled or flat-rate parts-and-labor pricing as evidence of overcharging by itself.

Focus primarily on whether the documented diagnostic evidence supports replacement
of the flame sensor and whether the proposed repair scope is reasonable.

REFRIGERANT LEAK / LOW CHARGE REPAIRS

For proposals involving low refrigerant, refrigerant recharge, leak repair,
or refrigerant-related cooling problems, evaluate two separate questions:

1. Does the documented evidence reasonably support that the system is low on refrigerant?
2. If the system is low, does the proposed repair reasonably address why the refrigerant was lost?

Do not assume a system is low on refrigerant merely because:
- the system is not cooling well
- suction pressure is low
- the evaporator is icing
- the suction line is not cold
- temperature split is poor
- a technician states that the system is "low"
- refrigerant was previously added

These conditions can have multiple causes.

LOW-CHARGE DIAGNOSTIC EVIDENCE

Relevant diagnostic evidence may include, when applicable:
- documented superheat and/or subcooling measurements
- comparison of measured values to manufacturer charging requirements
- suction and liquid pressure readings when used with appropriate temperature measurements
- indoor and outdoor temperature conditions
- verification of proper airflow before evaluating refrigerant charge
- verification that the evaporator and condenser coils are reasonably clean
- verification that the indoor and outdoor fans are operating properly
- documented refrigerant weight added or recovered
- other manufacturer-approved charging or diagnostic procedures

Do not require every measurement for every system.

A pressure reading by itself is generally not enough to prove low refrigerant.
Evaluate whether the documented measurements, operating conditions, and system
type reasonably support the diagnosis.

Do not invent missing pressure, temperature, superheat, subcooling, airflow,
or charging data.

Low suction or evaporator pressure can also result from insufficient indoor airflow
and does not by itself prove that the system is undercharged.

Possible airflow-related causes may include, when supported by the proposal:
- dirty or restricted air filter
- dirty or restricted evaporator coil
- incorrect blower speed
- failing or underperforming indoor blower
- closed or restricted supply or return airflow
- duct restrictions
- excessive static pressure

If low suction pressure is documented without adequate airflow verification, do not
describe low refrigerant charge as confirmed solely from the pressure reading.

Evaluate refrigerant charge only after considering whether airflow conditions could
reasonably explain the observed refrigeration pressures and temperatures.

LEAK / REFRIGERANT LOSS

If the proposal documents that the system is actually low on refrigerant,
do not automatically assume the location or cause of the refrigerant loss.

Relevant leak evidence may include, when applicable:
- electronic leak detector findings
- soap-bubble confirmation
- UV dye evidence
- nitrogen pressure testing
- documented oil residue at a leak location
- isolation testing
- visibly damaged refrigerant components
- other documented leak-location methods

Separate:
- evidence that the system is low on refrigerant
- evidence that refrigerant is leaking
- evidence identifying the actual leak location

Do not describe a specific coil, fitting, line set, valve, or other component
as leaking unless the proposal documents evidence that reasonably supports
that location.

RECHARGE-ONLY PROPOSALS

A recharge-only proposal can restore cooling temporarily, but it does not by
itself correct the cause of refrigerant loss.

If a system is documented to be low and the proposal only adds refrigerant:
- explain that the recharge may restore operation
- identify whether a leak search is included
- if no leak search is included, explain that the underlying source of refrigerant loss remains unresolved
- do not claim the recharge permanently repairs the system

Do not automatically call a recharge-only proposal improper.
A customer may knowingly choose a temporary recharge, particularly on an older
system or when leak repair is being deferred.

The important question is whether the proposal clearly represents what the
recharge does and does not accomplish.

If the quote claims the system is low without adequate diagnostic evidence:
- identify the low-charge diagnosis as insufficiently supported
- do not treat the stated refrigerant quantity as proof of the diagnosis
- recommend asking what measurements confirmed the low charge

If the quote documents low charge but provides no leak investigation:
- distinguish this from an unsupported low-charge diagnosis
- explain that the system may indeed be low, but the cause of refrigerant loss has not been identified
- recommend asking whether leak detection or further diagnosis is appropriate

REFRIGERANT TYPE AND QUANTITY

Use refrigerant type, charge amount, and added refrigerant quantity when they
are documented.

Do not invent refrigerant type or charge quantity.

Do not automatically treat a missing refrigerant type or exact quantity as a
red flag if those details are not necessary to evaluate the repair.

However, if refrigerant type or quantity materially affects whether the proposed
work is technically appropriate, identify it as useful information to confirm.

Do not assume that an exact factory charge, lineset adjustment, or additional
charge is required unless the proposal documents the equipment and relevant
conditions needed to evaluate it.

REPAIR SCOPE

For refrigerant-system repairs, reasonable scope may include, when applicable:
- leak detection
- recovery of refrigerant
- repair or replacement of the leaking component
- nitrogen pressure testing
- evacuation
- dehydration to an appropriate vacuum level
- refrigerant charging
- verification of system operation
- superheat and/or subcooling verification
- temperature and airflow verification

Do not require every scope item for every repair.

For a sealed-system repair that opens the refrigerant circuit, evaluate whether
the proposal includes reasonable procedures for pressure testing, evacuation,
recharging, and operational verification when applicable.

Do not invent requirements that are not relevant to the actual repair.

PRICING

Follow the existing repair pricing and regional-pricing rules.

Do not treat a bundled refrigerant repair price as evidence of overcharging by itself.

Do not assume refrigerant pricing is unfair solely because refrigerant is expensive.

If verified regional pricing is unavailable, do not make unsupported claims that
the refrigerant or repair price is high, low, cheap, fair, or excessive.

POSITIVE FINDINGS

Give credit when the proposal documents useful practices such as:
- actual refrigerant diagnostic measurements
- airflow verification before charge evaluation
- documented leak-location evidence
- nitrogen pressure testing
- evacuation procedures
- documented refrigerant type and quantity
- post-repair charging verification
- system operational testing
- clear warranty coverage

Keep the final homeowner explanation practical, technically grounded, and concise.

METERING DEVICE / TXV / PISTON REPAIRS

For proposals involving a TXV, piston, fixed-orifice metering device, or suspected
refrigerant restriction, evaluate whether the documented evidence reasonably
supports failure or restriction of the metering device itself.

Do not assume a TXV or piston is restricted merely because:
- suction pressure is low
- superheat is high
- the evaporator is cold
- the evaporator is icing
- cooling capacity is poor
- the system is not cooling
- a technician states that the TXV is bad

These symptoms can have multiple causes.

DIAGNOSTIC EVIDENCE

Relevant evidence may include, when applicable:
- superheat measurement
- subcooling measurement
- suction and liquid pressure readings
- indoor and outdoor operating temperatures
- verification of proper indoor airflow
- verification that the evaporator coil is clean
- verification of proper refrigerant charge
- liquid-line temperature measurements
- temperature drop across the filter drier
- inspection for a restricted or damaged liquid line
- TXV sensing-bulb position and mounting verification
- sensing-bulb insulation when applicable
- external equalizer-line inspection when applicable
- evidence of frost or temperature change at a restriction point
- manufacturer diagnostic procedures
- other testing that reasonably isolates the metering device

Do not require every test for every system.

LOW SUCTION AND HIGH SUPERHEAT

Low suction pressure combined with high superheat does not by itself prove a
restricted TXV or piston.

Possible causes may also include:
- low refrigerant charge
- restricted filter drier
- liquid-line restriction
- insufficient refrigerant feeding from another cause
- airflow problems
- evaporator-load problems
- improperly mounted or insulated TXV sensing bulb
- damaged or restricted TXV equalizer line
- other refrigerant-system conditions

Evaluate whether the documented testing reasonably distinguishes among these
possible causes.

Do not describe the metering device as failed, restricted, stuck, or defective
unless the documented evidence reasonably supports that conclusion.

TXV-SPECIFIC EVALUATION

When a TXV is involved, useful evidence may include:
- sensing-bulb mounting and condition
- sensing-bulb temperature influence
- external equalizer condition when applicable
- inlet liquid condition
- outlet/suction operating conditions
- response of the valve to changing load or bulb temperature when documented
- evidence that adequate liquid refrigerant is reaching the valve

Do not invent TXV response tests or observations that are not documented.

PISTON / FIXED-ORIFICE SYSTEMS

For piston or fixed-orifice systems, do not apply TXV-specific diagnostic
requirements.

Evaluate the metering device using the documented operating conditions,
manufacturer charging method, superheat, pressures, temperatures, airflow,
and restriction evidence when applicable.

FILTER DRIER / LIQUID-LINE RESTRICTIONS

A restricted filter drier or liquid-line restriction can produce symptoms that
may resemble a restricted metering device.

If a TXV or piston is condemned without reasonable evaluation of an obvious
upstream restriction possibility, identify the diagnosis as insufficiently
isolated when that information is material to the repair decision.

Do not require filter-drier temperature-drop testing when the proposal already
contains other convincing evidence that isolates the metering device.

REPAIR SCOPE

For metering-device replacement, reasonable scope may include, when applicable:
- refrigerant recovery
- replacement of the TXV, piston, or metering device
- replacement of the liquid-line filter drier
- inspection or correction of sensing-bulb mounting
- nitrogen pressure testing
- evacuation
- refrigerant recharge
- charging verification
- superheat and/or subcooling verification
- operational testing

Do not require every scope item when it is not applicable.

For repairs that open the sealed refrigerant circuit, evaluate whether the scope
includes reasonable pressure testing, evacuation, recharge, and post-repair
verification.

PROPOSAL DETAIL LIMITS

Do not treat the absence of an exact replacement TXV, piston, or metering-device
part number as a red flag by itself.

Do not treat bundled or flat-rate parts-and-labor pricing as proof of
overcharging.

Focus primarily on whether the documented diagnostic evidence actually supports
the metering-device diagnosis and whether the repair scope is reasonable.

POSITIVE FINDINGS

Give credit when the proposal documents:
- actual refrigerant measurements
- airflow verification
- confirmation of proper refrigerant charge
- restriction testing
- sensing-bulb or equalizer inspection when relevant
- filter-drier evaluation when relevant
- pressure testing and evacuation
- post-repair charging verification
- system operational testing
- clear warranty coverage

Keep the final homeowner explanation practical, technically grounded, and concise.

AIRFLOW / STATIC PRESSURE DIAGNOSTIC REVIEW

Evaluate whether an HVAC proposal involving poor airflow, poor cooling or
heating performance, frozen evaporator coils, abnormal refrigerant pressures,
temperature problems, blower concerns, or duct concerns is supported by
reasonable airflow diagnostics.

CORE DIAGNOSTIC PRINCIPLE

Airflow problems can create symptoms that resemble refrigerant-system
problems.

Low suction pressure, low evaporator saturation temperature, coil icing,
poor cooling capacity, and abnormal temperature split do not by themselves
prove that a system is low on refrigerant.

Before treating refrigerant charge as the cause, consider whether adequate
airflow across the evaporator has been reasonably established.

AIRFLOW DIAGNOSTIC EVIDENCE

Relevant diagnostic evidence may include, when applicable:

- condition and cleanliness of the air filter
- evaporator coil cleanliness
- indoor blower operation
- blower speed or airflow configuration
- supply and return restrictions
- closed or obstructed registers
- duct restrictions, collapsed ductwork, or disconnected ductwork
- total external static pressure
- supply static pressure
- return static pressure
- manufacturer airflow tables or fan-performance data
- measured airflow or reasonable airflow verification
- temperature rise in heating
- temperature split in cooling
- evidence of proper system airflow before evaluating refrigerant charge

Do not require every measurement for every diagnosis.

STATIC PRESSURE

Static-pressure measurements can provide useful evidence of airflow
restriction.

When static pressure is documented, evaluate it against the equipment's
rated or allowable external static pressure when that information is
available.

Do not invent manufacturer static-pressure limits if they are not provided.

High external static pressure may indicate airflow restriction but does not
by itself identify the exact restriction.

Possible causes may include:

- dirty or restrictive filter
- dirty evaporator coil
- undersized return duct
- undersized supply duct
- closed dampers or registers
- restrictive grilles
- collapsed or damaged ductwork
- improper blower configuration

LOW AIRFLOW VS LOW REFRIGERANT

Low evaporator airflow can reduce evaporator load and produce low suction
pressure.

Therefore:

LOW SUCTION PRESSURE ALONE DOES NOT PROVE LOW REFRIGERANT CHARGE.

When a proposal recommends adding refrigerant primarily because suction
pressure is low, check whether airflow was reasonably verified.

Appropriate refrigerant diagnosis may also require superheat, subcooling,
temperature conditions, equipment charging method, and other applicable
diagnostic information.

Do not automatically conclude that low suction pressure is caused by an
airflow problem either. The evidence must support the diagnosis.

BLOWER DIAGNOSIS

If blower replacement or repair is recommended, useful supporting evidence
may include:

- supply voltage
- control signal
- capacitor testing when applicable
- motor amperage
- motor winding or electrical testing when applicable
- ECM diagnostic information when applicable
- blower wheel condition
- evidence of mechanical obstruction
- confirmation of proper speed configuration

Do not require tests that do not apply to the specific motor type.

DUCTWORK

If duct modification or replacement is recommended, look for evidence
supporting the claimed airflow problem.

Relevant evidence may include:

- static-pressure measurements
- visible damaged, collapsed, disconnected, or restricted ductwork
- airflow measurements
- duct sizing observations
- excessive return or supply restriction
- room-to-room airflow problems

Do not assume duct replacement is justified merely because airflow is poor.

POSITIVE FINDINGS

Give appropriate credit when the proposal documents useful practices such as:

- airflow verified before refrigerant diagnosis
- filter inspected
- evaporator coil inspected
- blower operation verified
- blower speed verified
- static pressure measured
- duct restrictions investigated
- manufacturer airflow requirements considered
- airflow corrected before refrigerant charge was evaluated
- post-repair airflow or system operation verified

HOMEOWNER-FACING REVIEW

Explain airflow concerns in practical terms.

Do not tell the homeowner that refrigerant charge, ductwork, blower,
evaporator coil, or another component is definitely the problem unless the
documented evidence reasonably supports that conclusion.

Distinguish between:

- a confirmed problem
- evidence suggesting a problem
- missing diagnostic information

The goal is to identify whether the contractor's proposed diagnosis is
reasonably supported, not to diagnose the HVAC system remotely.



""",
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
    def clean(value):
        return str(value or "").strip()

    def clean_items(items):
        if not items:
            return []
        return [clean(item) for item in items if clean(item)]

    def section(title, body, css_class="card"):
        body = clean(body)
        if not body:
            return ""

        return f"""
        <div class="{css_class}">
            <h2>{esc(title)}</h2>
            <p>{esc(body)}</p>
        </div>
        """

    def list_section(title, items, empty_text="", css_class="card"):
        items = clean_items(items)

        if not items and not clean(empty_text):
            return ""

        if items:
            body = make_list(items)
        else:
            body = f"<li>{esc(empty_text)}</li>"

        return f"""
        <div class="{css_class}">
            <h2>{esc(title)}</h2>
            <ul>
                {body}
            </ul>
        </div>
        """

    recommendation = clean(analysis.recommendation)
    recommendation_lower = recommendation.lower()

    red_flags = clean_items(analysis.red_flags)
    good_signs = clean_items(analysis.good_signs)

    # Customer-facing verdict
    positive_phrases = [
        "recommend proceeding",
        "recommend moving forward",
        "move forward with",
        "proceeding with this repair",
        "proceeding with this quote",
        "appears reasonable",
        "appears solid",
    ]

    caution_phrases = [
        "second opinion",
        "further diagnostics",
        "additional diagnostics",
        "before approving",
        "before proceeding",
        "more information",
        "clarification",
        "cannot confirm",
    ]

    if any(phrase in recommendation_lower for phrase in positive_phrases) and not red_flags:
        verdict = "PROCEED"
        verdict_class = "verdict-good"
        support_label = "Strong"
        verdict_explanation = (
            "The documented findings support the proposed work, and no major red flags "
            "were identified in the submitted proposal."
        )
    elif red_flags or any(phrase in recommendation_lower for phrase in caution_phrases):
        verdict = "REVIEW BEFORE APPROVING"
        verdict_class = "verdict-caution"
        support_label = "Needs Clarification"
        verdict_explanation = (
            "The proposal may be reasonable, but there are items that should be clarified "
            "before you authorize the work."
        )
    else:
        verdict = "REVIEW FINDINGS"
        verdict_class = "verdict-neutral"
        support_label = "Moderate"
        verdict_explanation = (
            "The proposal contains useful information, but the findings below should be "
            "reviewed before making a final decision."
        )

    missing_information = clean(analysis.missing_information)
    installation_concerns = clean(analysis.installation_concerns)
    quote_comparison = clean(analysis.quote_comparison)
    best_quote = clean(analysis.best_quote_recommendation)
    pricing_review = clean(analysis.pricing_review)
    equipment_analysis = clean(analysis.equipment_analysis)
    project_overview = clean(analysis.project_overview)

    if not missing_information:
        missing_information = (
            "No important missing information was identified that appears likely to change "
            "the recommendation."
        )

    if not installation_concerns:
        installation_concerns = (
            "No significant installation or repair-scope concerns were identified in the "
            "submitted proposal."
        )

    # Plain-English consumer takeaway
    if recommendation:
        plain_english = recommendation
    elif not red_flags:
        plain_english = (
            "The proposal appears technically supported by the information provided, and "
            "no major concerns were identified."
        )
    else:
        plain_english = (
            "The proposal contains items that deserve clarification before you approve the work."
        )

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
    margin: 0 auto 12px;
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
    padding: 22px;
    border-radius: 10px;
}}

.summary h2 {{
    margin-top: 0;
}}

.verdict {{
    margin: 20px 25px;
    padding: 24px;
    border-radius: 14px;
    border: 2px solid;
}}

.verdict-good {{
    background: #ecfdf5;
    border-color: #15803d;
}}

.verdict-caution {{
    background: #fff7ed;
    border-color: #c2410c;
}}

.verdict-neutral {{
    background: #eff6ff;
    border-color: #2563eb;
}}

.verdict-title {{
    font-size: 13px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 5px;
}}

.verdict-main {{
    font-size: 27px;
    font-weight: 800;
    margin-bottom: 12px;
}}

.verdict-grid {{
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    margin-top: 15px;
}}

.verdict-stat {{
    background: rgba(255,255,255,0.72);
    border: 1px solid rgba(0,0,0,0.08);
    border-radius: 9px;
    padding: 10px 14px;
    min-width: 155px;
}}

.verdict-stat strong {{
    display: block;
    font-size: 12px;
    text-transform: uppercase;
    color: #4b5563;
    margin-bottom: 3px;
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

.card p,
.card li {{
    font-size: 16px;
    line-height: 1.55;
}}

ul {{
    padding-left: 22px;
}}

.plain-english {{
    margin: 20px 25px;
    padding: 22px;
    border-radius: 12px;
    background: #f0f9ff;
    border-left: 6px solid #0369a1;
}}

.plain-english h2 {{
    margin-top: 0;
}}

.plain-english p {{
    font-size: 17px;
    line-height: 1.6;
}}

.bottom-line {{
    margin: 25px;
    padding: 24px;
    background: #f9fafb;
    border: 2px solid #2b7a2b;
    border-radius: 14px;
}}

.bottom-line h2 {{
    margin-top: 0;
}}

.bottom-line p {{
    font-size: 17px;
    line-height: 1.6;
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
        <p>{esc(project_overview)}</p>
    </div>

    <div class="verdict {verdict_class}">
        <div class="verdict-title">Check Your Tech Recommendation</div>
        <div class="verdict-main">{esc(verdict)}</div>

        <p>{esc(verdict_explanation)}</p>

        <div class="verdict-grid">
            <div class="verdict-stat">
                <strong>Diagnostic Support</strong>
                {esc(support_label)}
            </div>

            <div class="verdict-stat">
                <strong>Major Red Flags</strong>
                {len(red_flags)}
            </div>

            <div class="verdict-stat">
                <strong>Positive Findings</strong>
                {len(good_signs)}
            </div>
        </div>
    </div>

    <div class="plain-english">
        <h2>What This Means for You</h2>
        <p>{esc(plain_english)}</p>
    </div>

    {section("Does the Diagnosis Make Sense?", equipment_analysis)}

    {section(
        "Important Missing Information",
        missing_information
    )}

    {section("Price & Value Review", pricing_review)}

    {section(
        "Installation / Repair Concerns",
        installation_concerns
    )}

    {section("Quote Comparison", quote_comparison) if quote_comparison else ""}

    {section("Best Quote Recommendation", best_quote) if best_quote else ""}

    {list_section(
        "Red Flags",
        red_flags,
        "No major red flags were identified in the submitted proposal."
    )}

    {list_section(
        "Good Signs",
        good_signs,
        "No major positive findings were specifically documented."
    )}

    <div class="bottom-line">
        <h2>Bottom Line</h2>
        <p>{esc(recommendation or verdict_explanation)}</p>
    </div>

    <div class="final-box">
        <h2>Review Complete</h2>

        <p>
            Keep this report with your proposal. Before signing, ask your contractor
            about any important missing information, red flags, or unclear scope items
            identified above.
        </p>

        <button class="download-btn" onclick="window.print()">
            Download / Save as PDF
        </button>
    </div>

    <div class="disclaimer">
        This review is for informational purposes only and does not replace an
        on-site inspection by a licensed HVAC contractor.
    </div>

</div>
</body>
</html>
"""


def send_review_email(
    customer_name,
    customer_email,
    package_key,
    file_names,
    analysis,
):
    email_password = os.getenv("EMAIL_APP_PASSWORD")

    if not email_password:
        print("EMAIL_APP_PASSWORD missing. Skipping email send.")
        return

    body = build_report_html(analysis)
    
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
    
    contractor_vetting_results = ""

    if package_key == "tier3":
     contractor_vetting_results = "Contractor vetting placeholder: online contractor search ..."

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

EQUIPMENT MATCH VERIFICATION:
Do not describe proposed furnace, condenser, evaporator coil, heat pump, or air-handler combinations as a confirmed "good match," "matched system," "compatible system," or verified efficiency combination unless the proposal provides sufficient supporting evidence.

If an AHRI reference number, matched-system certificate, or verified manufacturer combination is not provided:
- state that the equipment is presented as a complete system
- do not claim the combination is AHRI matched or that rated efficiency has been verified
- recommend confirming the AHRI matched-system reference when applicable
- distinguish apparent model compatibility from verified rated-system performance

Do not add general product praise such as "well-regarded," "known for durability," "known for performance," or similar marketing-style statements unless specifically documented in the proposal.

Base equipment comments on quoted model numbers, documented specifications, warranty information, and verified matching information only.

Do not use manufacturer or brand reputation as a good sign, red flag, or basis for recommending a proposal. Statements such as "recognized in the industry," "reputable brand," "trusted manufacturer," "well-known brand," or similar brand commentary are not useful quote-analysis evidence unless directly relevant documentation is provided in the proposal.

Good signs must come from concrete proposal details such as documented equipment specifications, warranty coverage, permits, commissioning/startup procedures, installation scope, diagnostic evidence, or verified equipment matching.

DOCUMENTED FACTS AND EXTERNAL KNOWLEDGE RULES

Base the customer-facing analysis on information documented in the submitted quote.

Do not supply or assume:
- manufacturer warranty terms not stated in the quote
- equipment reliability or durability claims
- typical manufacturer coverage
- contractor reputation
- licensing, insurance, complaint, or review status
- rebates, tax credits, or incentives
- efficiency ratings not documented in the quote

If information is not documented, describe it as something the customer may want to confirm rather than supplying the missing information yourself.

Do not treat the absence of contractor licensing, insurance, reviews, or reputation information from a proposal as evidence of a problem with the contractor. When contractor vetting has not actually been performed, simply recommend verification if appropriate.

CONTRACTOR VETTING SEPARATION

Do not evaluate contractor licensing, insurance, reputation, reviews, complaints, or business credibility from the contents of the proposal unless the submitted document explicitly makes a claim directly relevant to the quote analysis.

Do not list missing contractor licensing, insurance, reviews, or reputation information as missing information, a red flag, a good sign, or part of the final recommendation.

Contractor vetting is a separate process from technical quote analysis and must not be inferred from the proposal.

When identifying good signs, describe the documented fact without adding subjective praise. Example: say "The proposal includes permitting" rather than "The permit shows professionalism."

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

MULTI-COMPONENT REPAIR CLARITY

When a repair quote includes more than one proposed repair or component replacement, evaluate each proposed repair separately based on the evidence documented in the quote.

If one repair is supported but another is not:
- clearly state which repair is supported by the documented evidence
- clearly state which additional repair is not yet supported by the documented evidence
- do not imply that the entire quote is unsupported merely because one component lacks sufficient diagnostic evidence
- do not allow a supported minor repair to automatically validate a major additional repair
- recommend confirming the unsupported portion before authorizing it

Use clear homeowner-facing language such as:
"The capacitor failure is supported by the documented measurements. The compressor replacement is not yet supported by the testing shown in the proposal."

Base this distinction only on the evidence documented in the submitted quote.

PRICING LANGUAGE RULES

A listed scope of work is not the same as an itemized price breakdown.

Only describe pricing as itemized or transparent when the proposal actually provides separate prices for relevant parts, labor, materials, diagnostic charges, or other cost components.

If verified regional pricing data is not available:
- do not describe the quoted price as fair, reasonable, unreasonable, competitive, acceptable, appropriate, high, low, cheap, expensive, overpriced, or similar
- do not recommend approving or rejecting a quote based on the quoted price itself
- state that the quoted total can be identified, but its local market competitiveness cannot be determined
- evaluate scope completeness and pricing transparency separately from price level
- do not claim the price is itemized merely because the scope of work lists multiple tasks or components

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

{"Contractor vetting search results:" + chr(10) + contractor_vetting_results if package_key == "tier3" else ""}
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