"""Proposal-stage startup review; numerical results remain owned by domain modules."""
import re

COMMISSIONING_SUBJECT = "Startup and commissioning plan for proposed equipment"
COMMISSIONING_RULES = """
COMMISSIONING / STARTUP VERIFICATION
Use the existing TechnicalEvidenceAssessment with PRIMARY subject
"Startup and commissioning plan for proposed equipment" for material equipment
installations, major circuit repairs and material control conversions. Isolated
capacitor, contactor, igniter, flame-sensor and minor thermostat work does not
automatically need whole-system commissioning.

Distinguish PRE-INSTALL PROPOSAL from COMPLETED STARTUP RECORD. A promised startup
sheet is planned documentation, not completed results. An attached sheet must
actually document completed checks to support completed startup.
For a proposal evaluate the promised plan, applicable operation, documentation and
closeout. Manufacturer startup with documented final readings can be ADEQUATE /
APPROPRIATE without actual readings before installation. Generic startup/test
operation or silence is INCOMPLETE / PARTIALLY_DEFINED, not PRIMARY ABSENT.
No red flag for ordinary missing detail. Explicit exclusion of material startup,
a conflict with a supplied required check, or an unresolved material failed
completed check is CONTRADICTORY / UNSUPPORTED. Credit documented corrections.
Record the actual source evidence, gaps and contradictions. Do not invent intent.

No universal checklist: heating/cooling, controls/safeties, condensate, airflow,
refrigerant and combustion checks depend on equipment and supplied requirements.
Do not require final static, CFM, SH/SC, temperature rise, gas pressure, voltage,
amps or combustion results before installation. Do not invent pressure limits or
manufacturer requirements. PRESSURE TESTING and EVACUATION are applicable tasks,
not universal requirements: a planned vacuum target is not an achieved result.
Use equipment-specific charging procedures when supplied. No universal SH/SC,
combustion for non-combustion equipment, static for true ductless, or defrost test
in unsuitable ambient conditions. Package accessories and VRF addressing,
communications, zones and controllers apply only when included. Do not infer them.

Own planned/performed startup, operation, applicable controls/safeties, documented
readings, startup sheets and correction before closeout. DUCT_AIRFLOW interprets
duct adequacy; SYSTEM_SIZING owns building capacity; EQUIPMENT_MATCHING owns
pairing; refrigerant, electrical/motors and furnace/heat-exchanger modules own
their diagnoses and numerical interpretation. Startup does not establish any of
those conclusions, replacement justification, drainage design or pricing.
Factory/manufacturer startup does not prove representative attendance,
certification, authorization or warranty activation. Warranty is independent.

One underlying commissioning conflict gets one red flag. Meaningful planned or
completed checks/documentation can be Good Signs, never performance guarantees.
Generic startup, permits, pairing, sizing or warranty alone are not commissioning
Good Signs. Use one concise action and question about the actual unresolved plan
or failed check, not a checklist. Speak naturally: the quote includes startup,
but it doesn't say what will be checked or documented. Explain what is promised,
what is checked, what records are provided and what remains unclear.
"""


def commissioning_text(value):
    text = str(value or "").lower()
    if re.search(r"diagnos|duct adequacy|system sizing|equipment match|blower.*failure", text):
        return False
    return bool(re.search(r"\b(?:start[- ]?up|commission\w*|system checkout|final operational verification|post[- ]installation testing|final performance checks)\b", text))


def commissioning_required(text, classification=None):
    # Source scope, not merely an equipment mention or a classifier module name.
    scope = r"(?:replace\w*|install\w*|conversion)"
    equipment = r"(?:full[- ]system|complete.{0,15}system|HVAC system|furnace|air[- ]handler|heat[- ]pump|condenser|mini[- ]split|package[d]?[- ]unit|VRF|multi[- ]zone|compressor|(?:evaporator|indoor) coil|refrigerant circuit|major control)"
    for line in text.splitlines():
        if re.search(r"\b(?:not replacing|no replacement|repair only)\b", line, re.I):
            continue
        if re.search(scope + r".{0,70}" + equipment + r"|" + equipment + r".{0,45}" + scope, line, re.I):
            # A motor/component repair is not replacement of its parent equipment.
            if re.search(r"capacitor|contactor|igniter|flame.sensor|blower motor|fan motor|control board|pressure switch", line, re.I):
                continue
            return True
        if re.search(r"\bnew (?:HVAC |system )?installation\b", line, re.I):
            return True
    return False


def commissioning_items(analysis):
    return [a for a in analysis.technical_assessments
            if a.materiality != "MINOR" and commissioning_text(a.subject)]


def commissioning_question(value):
    if commissioning_text(value):
        return True
    if re.search(r"failed safety[- ]shutdown check", value, re.I) and re.search(r"retest|closed out|closeout", value, re.I):
        return True
    if (re.search(r"exclu\w*|omitt\w*|required", value, re.I)
            and re.search(r"final safety[- ]shutdown (?:verification|check)|required startup verification", value, re.I)):
        return True
    # Whole-system closeout, not a repair-specific charge or airflow question.
    return bool(re.search(r"(?:verif\w*|check\w*).{0,50}(?:heating and cooling operation|safety).{0,35}after installation|"
                          r"(?:final operating readings|closeout documentation)", value, re.I))


def source_plan(text):
    """Conservative source recovery; never interpret numerical domain readings."""
    lines = [s.strip(" -\t") for s in re.split(r"\n|(?<=[.!?])\s+(?=[A-Z])", text) if s.strip()]
    relevant = [s for s in lines if re.search(
        r"startup|start-up|commission|checkout|final.*(?:reading|check|verification)|manufacturer.*procedure|"
        r"verify.*(?:operation|control|safet)|deficien.*closeout|check.*(?:passed|failed)", s, re.I)]
    completed = bool(re.search(r"(?:startup|commissioning) (?:sheet|record) (?:attached|supplied)|completed startup record", text, re.I))
    failures = [s for s in relevant if re.search(r"\b(?:failed|fail)\b", s, re.I)
                and re.search(r"material|required", s, re.I)
                and not re.search(r"no (?:unresolved )?failed|corrected.*(?:passed|retest)|retest.*passed", s, re.I)]
    exclusions = [s for s in relevant if re.search(
        r"no (?:startup|start-up|commissioning|final verification)(?:/testing| testing)? (?:will be performed|will be done|is included)|"
        r"(?:startup|final verification|startup testing) (?:is |will be )?(?:excluded|not included|not performed)|"
        r"(?:excludes?|will not perform) (?:the )?(?:required startup.{0,50}check|startup testing|startup|final verification)\b", s, re.I)]
    conflict = exclusions or (failures if completed else [])
    if conflict:
        return "CONTRADICTORY", "UNSUPPORTED", relevant, conflict, completed
    affirmative = [s for s in relevant if not re.search(r"\b(?:no|not|excluded|missing|unclear)\b", s, re.I)]
    joined = " ".join(affirmative)
    procedure = bool(re.search(r"manufacturer.{0,35}(?:startup|procedure)|startup.{0,35}manufacturer", joined, re.I))
    documentation = bool(re.search(r"(?:document|record)\w*.{0,30}(?:reading|result)|(?:reading|result).{0,25}(?:document|record)|(?:startup|commissioning) (?:sheet|record).{0,25}(?:provid|suppli|attach)|provid\w*.{0,25}(?:startup|commissioning) (?:sheet|record)", joined, re.I))
    operations = bool(re.search(r"verif\w*.{0,55}(?:operation|controls|safet)|(?:checks|procedure).{0,30}completed", joined, re.I))
    clean_record = completed and bool(re.search(r"(?:applicable|required) checks.{0,30}(?:passed|completed)|startup procedure completed", joined, re.I))
    supported = clean_record or (documentation and (procedure or operations))
    return ("ADEQUATE" if supported else "INCOMPLETE", "APPROPRIATE" if supported else "PARTIALLY_DEFINED", relevant, [], completed)


def normalize_commissioning_assessments(analysis, text, assessment_type, classification=None):
    items = commissioning_items(analysis)
    material = commissioning_required(text, classification)
    if not items and not material:
        return
    status, scope, evidence, conflicts, completed = source_plan(text)
    # Retain meaningful structured conflicts (including source requirements too
    # complex for conservative recovery); an absent checklist alone is not one.
    real_conflicts = [c for a in items for c in a.contradictions
                     if re.search(r"exclud|failed|will not|not performed", c, re.I)]
    if real_conflicts:
        status, scope, conflicts = "CONTRADICTORY", "UNSUPPORTED", list(dict.fromkeys(conflicts + real_conflicts))
    if not text.strip() and items:
        return  # Compatibility callers without source cannot be source-recalibrated.
    assessment = assessment_type(
        subject=COMMISSIONING_SUBJECT,
        materiality="PRIMARY" if material else items[0].materiality,
        diagnostic_evidence_status=status, scope_support=scope,
        documented_evidence=evidence,
        material_gaps=["The startup checks and documentation are not clearly described."] if scope == "PARTIALLY_DEFINED" else [],
        contradictions=conflicts,
    )
    analysis.technical_assessments = [a for a in analysis.technical_assessments if a not in items] + [assessment]


def customer_source_text(value):
    """Presentation only: keep document titles/models, not transport/test metadata."""
    text = re.sub(r"(?im)^\s*(?:File Name|Fixture(?: ID)?|Internal Test ID|Temporary Path|Local Path):\s*[^\n]*", "", str(value or ""))
    text = re.sub(r"(?:[A-Za-z]:[\\/]|/(?:Users|private|tmp|var)/)[^\s<>]+", "", text)
    text = re.sub(r"\b[\w.-]+_test\.(?:txt|pdf|docx?)\b", "", text, flags=re.I)
    return text.strip()


def planned_startup_clauses(item):
    """Describe only affirmative submitted commitments, never add a checklist."""
    evidence = " ".join(customer_source_text(s) for s in item.documented_evidence
                        if not re.search(r"\b(?:no|not|excluded|missing|unclear)\b", s, re.I))
    clauses = []
    if re.search(r"manufacturer.{0,35}(?:startup|procedure)|startup.{0,35}manufacturer", evidence, re.I):
        clauses.append("follow the manufacturer startup procedure")
    if re.search(r"verif\w*.{0,35}heating and cooling operation", evidence, re.I):
        clauses.append("verify heating and cooling operation")
    if re.search(r"verif\w*.{0,90}controls.{0,10}safet", evidence, re.I):
        clauses.append("check applicable controls and safeties")
    if re.search(r"document\w*.{0,25}(?:final )?operating readings", evidence, re.I):
        clauses.append("document the final operating readings")
    if re.search(r"correct.{0,30}startup deficiencies.{0,20}closeout", evidence, re.I):
        clauses.append("correct startup deficiencies before closeout")
    if re.search(r"startup sheet.{0,25}provid|provid\w*.{0,25}startup sheet", evidence, re.I):
        clauses.append("provide a startup sheet")
    return clauses


def excluded_startup_check(analysis):
    """Presentation of the documented exclusion, not a new safety judgment."""
    items = commissioning_items(analysis)
    if len(items) != 1 or items[0].scope_support != "UNSUPPORTED":
        return ""
    evidence = " ".join(items[0].documented_evidence)
    if source_plan(evidence)[-1]:
        return ""
    if (re.search(r"safety[- ]shutdown", evidence, re.I)
            and re.search(r"require", evidence, re.I)
            and any(re.search(r"exclu\w*.*safety[- ]shutdown", c, re.I) for c in items[0].contradictions)):
        return "The submitted startup documentation requires a safety-shutdown check, but the proposal explicitly excludes it."
    return ""


def material_warranty_gap(analysis):
    facts = list(analysis.decision.required_actions)
    for item in analysis.technical_assessments:
        if item.materiality in {"PRIMARY", "MATERIAL_SECONDARY"}:
            facts.extend(item.material_gaps + item.contradictions)
    return any(re.search(r"warranty|coverage", fact, re.I) for fact in facts)


def failed_shutdown_record(analysis):
    """Identify this documented failed-check presentation case, without recalibration."""
    items = commissioning_items(analysis)
    if len(items) != 1 or items[0].scope_support != "UNSUPPORTED":
        return False
    item = items[0]
    return bool(source_plan("\n".join(item.documented_evidence))[-1]
                and any(re.search(r"required.*safety[- ]shutdown.*(?:check )?failed", c, re.I)
                        and re.search(r"no correction documented", c, re.I)
                        for c in item.contradictions))


def commissioning_paragraphs(analysis):
    items = commissioning_items(analysis)
    if not items:
        return []
    item = items[0]
    if item.scope_support == "UNSUPPORTED":
        if failed_shutdown_record(analysis):
            return ["The submitted completed startup record marks the required safety-shutdown check as failed. "
                    "No correction is documented for that check."]
        if excluded_startup_check(analysis):
            return [excluded_startup_check(analysis) + " The startup plan does not meet that submitted requirement."]
        completed = source_plan("\n".join(item.documented_evidence))[-1]
        return [("The supplied startup record shows an unresolved failed check: " if completed
                 else "The proposed startup work excludes a documented requirement: ") + " ".join(customer_source_text(s) for s in item.contradictions)]
    if item.scope_support == "PARTIALLY_DEFINED":
        return ["The quote doesn't clearly say what startup checks will be completed or what results you'll receive.",
                "Confirm the startup checks and documentation before approving the work."]
    completed = source_plan("\n".join(item.documented_evidence))[-1]
    introduction = ("The supplied startup record documents completed checks, with no unresolved material failed check identified."
                    if completed else "The quote includes a startup plan and documentation after installation; final readings are not expected before the work.")
    clauses = planned_startup_clauses(item)
    if not completed and clauses:
        joined = ", ".join(clauses[:-1]) + ", and " + clauses[-1] if len(clauses) > 1 else clauses[0]
        return ["The proposal includes a defined startup plan after installation. The contractor will " + joined + ".",
                "The final readings are not expected before the work because the system has not been installed yet."]
    return [introduction, *[clean for s in item.documented_evidence if (clean := customer_source_text(s))]]


def present_supported_commissioning(analysis, quote_text, sizing, matching, ducts):
    """GOOD whole-report presentation only; never change decisions or evidence."""
    items = commissioning_items(analysis)
    if (len(items) != 1 or not sizing or not matching or not ducts
            or analysis.decision.technical_support != "SUPPORTED"
            or analysis.decision.verdict != "PROCEED"
            or analysis.decision.pricing_transparency != "ADEQUATE"
            or analysis.red_flags or analysis.contractor_questions
            or analysis.decision.required_actions
            or any(a.scope_support != "APPROPRIATE" or a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}
                   or a.material_gaps or a.contradictions for a in analysis.technical_assessments if a.materiality != "MINOR")
            or source_plan("\n".join(items[0].documented_evidence))[-1]):
        return
    clauses = planned_startup_clauses(items[0])
    if "document the final operating readings" not in clauses:
        return
    evidence = [s for a in ducts for s in a.documented_evidence]
    static = next((s for s in evidence if re.search(r"static.*\d.*(?:within|below).*limit", s, re.I)), "")
    airflow = next((s for s in evidence if re.search(r"(?:delivered|measured) airflow.*\d.*(?:target|requirement)", s, re.I)), "")
    if not static or not airflow:
        return
    analysis.homeowner_takeaway = (
        "The submitted sizing and duct measurements support the proposed system, and the quote includes "
        "a defined startup plan with final operating readings to be documented after installation.")
    analysis.bottom_line = (
        "The equipment match, sizing, duct measurements and startup plan are supported by the submitted proposal. "
        "The contractor also plans to document the final startup results after installation. "
        "Nothing material in these reviewed areas needs to be cleared up before approval.")
    analysis.missing_information = "No material sizing, duct or startup information needs to be cleared up before approval."
    scope = []
    if re.search(r"remove (?:the )?existing equipment", quote_text, re.I):
        scope.append("removal of the existing equipment")
    if re.search(r"install (?:the )?(?:listed|new) equipment", quote_text, re.I):
        scope.append("installation of the new equipment")
    scope.extend(["startup", "documentation of the final operating readings"])
    analysis.installation_concerns = "The proposal includes " + ", ".join(scope[:-1]) + " and " + scope[-1] + "."
    analysis.good_signs = [
        "The proposed size is tied to building-specific load results.",
        "The submitted manufacturer documentation supports the matched indoor and outdoor equipment combination.",
        customer_source_text(static), customer_source_text(airflow),
        "The proposal includes a defined startup procedure and documentation of the final operating readings.",
    ]
    warranty = re.findall(r"\b\d+[- ]year (?:parts|labor) warranty\b", " ".join(
        line for line in quote_text.splitlines() if not re.search(r"\b(?:no|not|excluded)\b", line, re.I)), re.I)
    if warranty:
        analysis.good_signs.append("The proposal includes " + " and ".join(dict.fromkeys(warranty)) + ".")


def present_completed_commissioning(analysis, quote_text, sizing, matching, ducts):
    """Clean completed-record presentation; domain assessments remain authoritative."""
    items = commissioning_items(analysis)
    if (len(items) != 1 or not sizing or not matching or not ducts
            or analysis.decision.technical_support != "SUPPORTED"
            or analysis.decision.verdict != "PROCEED"
            or analysis.decision.pricing_transparency != "ADEQUATE"
            or analysis.red_flags or analysis.contractor_questions or analysis.decision.required_actions
            or any(a.scope_support != "APPROPRIATE" or a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}
                   or a.material_gaps or a.contradictions
                   for a in analysis.technical_assessments if a.materiality != "MINOR")):
        return
    evidence = "\n".join(items[0].documented_evidence)
    status, scope, _, conflicts, completed = source_plan(evidence)
    if (not completed or scope != "APPROPRIATE" or conflicts
            or not re.search(r"(?:applicable checks|startup procedure).{0,30}completed", evidence, re.I)
            or not re.search(r"final operating readings.{0,25}documented|document\w*.{0,25}final operating readings", evidence, re.I)):
        return
    analysis.homeowner_takeaway = (
        "The submitted sizing and duct measurements support the system, and the supplied startup record "
        "documents completion of the applicable startup checks and final operating readings, "
        "with no unresolved material failed startup check identified.")
    analysis.bottom_line = (
        "The submitted sizing, equipment match, duct information and completed startup record support the system. "
        "No material issue in these reviewed areas needs to be cleared up.")
    analysis.missing_information = (
        "No material sizing, duct-support or startup information needs to be cleared up in the submitted documents.")
    analysis.installation_concerns = (
        "The submitted documents include the equipment replacement scope and a completed startup record."
        if re.search(r"replace\w*|replacement", quote_text, re.I)
        else "The submitted documents include a completed startup record.")
    sign = "The supplied startup record documents completion of the applicable startup checks and final operating readings."
    analysis.good_signs = [s for s in analysis.good_signs if not commissioning_text(s)] + [sign]


def present_partial_commissioning(analysis, quote_text, sizing, matching, ducts):
    """Present an isolated startup-plan gap, without suppressing other domains."""
    items = commissioning_items(analysis)
    if (len(items) != 1 or items[0].diagnostic_evidence_status != "INCOMPLETE"
            or items[0].scope_support != "PARTIALLY_DEFINED"
            or not sizing or not matching or not ducts
            or analysis.decision.technical_support != "PARTIALLY_SUPPORTED"
            or analysis.decision.verdict != "REVIEW_BEFORE_APPROVING"
            or analysis.decision.pricing_transparency != "ADEQUATE"
            or analysis.red_flags
            or source_plan("\n".join(items[0].documented_evidence))[-1]
            or any(a.scope_support != "APPROPRIATE" or a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}
                   or a.material_gaps or a.contradictions
                   for a in analysis.technical_assessments if a not in items and a.materiality != "MINOR")
            or any(not commissioning_question(q) for q in analysis.contractor_questions)
            or any(not commissioning_question(a) for a in analysis.decision.required_actions)):
        return
    analysis.homeowner_takeaway = (
        "The equipment, sizing and duct information are supported. The remaining question is how the contractor "
        "will start up the new system and what final results will be documented after installation.")
    analysis.bottom_line = (
        "The proposed equipment, sizing and duct support look reasonable. Before approving the quote, confirm "
        "what startup checks will be completed and what documentation you'll receive after installation.")
    analysis.missing_information = (
        "The quote says startup is included, but it does not explain what checks will be completed or what startup documentation will be provided."
        if items[0].documented_evidence else
        "The quote does not describe the startup checks or the documentation that will be provided after installation.")
    scope = []
    if re.search(r"remove (?:the )?existing equipment", quote_text, re.I):
        scope.append("removal of the existing equipment")
    if re.search(r"install (?:the )?(?:listed|new) equipment", quote_text, re.I):
        scope.append("installation of the new equipment")
    if scope:
        analysis.installation_concerns = "The proposal includes " + " and ".join(scope) + "."
    elif analysis.installation_concerns == "Review the documented installation scope with the contractor before approval.":
        analysis.installation_concerns = ""
    analysis.good_signs = [s for s in analysis.good_signs if not commissioning_text(s)
                          and not re.search(r"(?:planned|voluntary|homeowner[- ]requested).{0,70}(?:upgrade|replacement)|replacement.{0,70}homeowner[- ]requested", s, re.I)]


def finalize_commissioning_fields(analysis):
    items = commissioning_items(analysis)
    if not items:
        return
    item = items[0]
    if excluded_startup_check(analysis) and not material_warranty_gap(analysis):
        analysis.contractor_questions = [q for q in analysis.contractor_questions if not re.search(r"warranty|coverage", q, re.I)]
        analysis.missing_information = " ".join(s for s in re.split(r"(?<=[.!?])\s+", analysis.missing_information)
                                                if not re.search(r"warranty|coverage", s, re.I))
    for name in ("missing_information", "installation_concerns"):
        sentences = re.split(r"(?<=[.!?])\s+", getattr(analysis, name))
        setattr(analysis, name, " ".join(s for s in sentences if not commissioning_text(s)))
    for name in ("red_flags", "good_signs", "contractor_questions"):
        owned = commissioning_question if name in {"contractor_questions", "red_flags"} else commissioning_text
        setattr(analysis, name, [s for s in getattr(analysis, name) if not owned(s)])
    analysis.decision.required_actions = [s for s in analysis.decision.required_actions if not commissioning_text(s)]
    analysis.decision.verdict_reasons = [s for s in analysis.decision.verdict_reasons if not commissioning_text(s)]
    if item.scope_support == "APPROPRIATE":
        completed = source_plan("\n".join(item.documented_evidence))[-1]
        analysis.good_signs.append("The supplied startup record documents completed checks."
                                   if completed else "The proposed startup work includes documentation of the checks after installation.")
        return
    if item.scope_support == "UNSUPPORTED":
        concern = excluded_startup_check(analysis) or commissioning_paragraphs(analysis)[0]
        completed = source_plan("\n".join(item.documented_evidence))[-1]
        action = ("Correct the documented failed startup check before closeout." if completed
                  else "Include the documented startup verification step that the quote excludes.")
        question = ("What will be done to correct the failed startup check before closing out the job?" if completed
                    else "Will you include the startup check required by the submitted documentation?")
        if excluded_startup_check(analysis):
            question = ("The submitted startup documentation requires the safety-shutdown check, but the proposal excludes it. "
                        "Will that check be included before the job is closed out?")
        analysis.red_flags.append(concern)
    else:
        concern = "The quote doesn't clearly say what startup checks will be completed or documented."
        action = "Confirm what startup checks will be completed and documented."
        question = "What startup checks will be completed and documented before the job is closed out?"
    if re.search(r"^No (?:important|material|critical).*missing|^No important information", analysis.missing_information, re.I):
        analysis.missing_information = ""
    analysis.missing_information = " ".join(filter(None, [analysis.missing_information, concern]))
    analysis.decision.required_actions.append(action)
    analysis.decision.verdict_reasons.append(concern)
    analysis.contractor_questions.insert(0, question)


def compose_commissioning_summary(analysis):
    items = commissioning_items(analysis)
    if not items or items[0].scope_support == "APPROPRIATE":
        return
    if any(a not in items and a.materiality != "MINOR" and (
            a.diagnostic_evidence_status not in {"CONFIRMED", "ADEQUATE"} or a.scope_support != "APPROPRIATE")
           for a in analysis.technical_assessments):
        return
    partial = items[0].scope_support == "PARTIALLY_DEFINED"
    explanation = ("The remaining question is how the contractor will start up and document the installed system."
                   if partial else commissioning_paragraphs(analysis)[0])
    analysis.banner_explanation = explanation
    analysis.homeowner_takeaway = "The other reviewed technical areas are supported. " + explanation
    if partial:
        analysis.bottom_line = "Before approving the quote, confirm what startup checks will be completed and what documentation you'll receive."
    elif analysis.decision.verdict == "GET_A_SECOND_OPINION":
        analysis.bottom_line = "Get a second opinion on the unresolved startup requirement before approving the work."
    else:
        analysis.bottom_line = "Before approving the quote, resolve the documented startup requirement."
    analysis.recommendation = analysis.decision.verdict.replace("_", " ") + " — " + explanation


def present_bad_commissioning(analysis):
    conflict = excluded_startup_check(analysis)
    if not conflict or analysis.decision.verdict != "GET_A_SECOND_OPINION":
        return
    items = commissioning_items(analysis)
    if (material_warranty_gap(analysis)
            or any(a.materiality != "MINOR" and a not in items and (
                a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}
                or a.scope_support != "APPROPRIATE" or a.material_gaps or a.contradictions)
                   for a in analysis.technical_assessments)):
        return
    analysis.missing_information = conflict
    analysis.installation_concerns = "The submitted startup documentation calls for a safety-shutdown check, but the proposal excludes that check."
    analysis.homeowner_takeaway = (
        "The other reviewed technical areas are supported. The problem is the startup plan: "
        + conflict + " I would have that corrected before approving the work.")
    analysis.good_signs = [s for s in analysis.good_signs
        if not re.search(r"(?:planned|voluntary|homeowner[- ]requested).{0,70}(?:upgrade|replacement)|replacement.{0,70}homeowner[- ]requested", s, re.I)]
    if any(re.search(r"building[- ]specific load", s, re.I) for s in analysis.good_signs):
        analysis.good_signs = [s for s in analysis.good_signs if not re.search(r"capacity ratings.*align|align.*cooling and heating requirements", s, re.I)]


def present_failed_commissioning(analysis, quote_text):
    """Give each section a distinct job for an isolated documented failed check."""
    items = commissioning_items(analysis)
    if (not failed_shutdown_record(analysis)
            or analysis.decision.technical_support != "UNSUPPORTED"
            or analysis.decision.verdict != "GET_A_SECOND_OPINION"
            or analysis.decision.pricing_transparency != "ADEQUATE"
            or any(a.materiality != "MINOR" and a not in items and (
                a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}
                or a.scope_support != "APPROPRIATE" or a.material_gaps or a.contradictions)
                   for a in analysis.technical_assessments)
            or any(not commissioning_question(a) for a in analysis.decision.required_actions)
            or any(not commissioning_question(flag) for flag in analysis.red_flags)):
        return
    analysis.banner_explanation = (
        "The completed startup record shows a required safety-shutdown check failed and no correction is documented.")
    analysis.recommendation = "GET A SECOND OPINION — " + analysis.banner_explanation
    analysis.homeowner_takeaway = (
        "The other reviewed technical areas are supported, but the completed startup record still has one unresolved "
        "failed safety check. I would have that corrected and documented before accepting the installation.")
    analysis.missing_information = "The startup record does not show that the failed safety-shutdown check was corrected and retested."
    analysis.red_flags = ["The required safety-shutdown check is marked failed, with no correction documented in the startup record."]
    analysis.contractor_questions = ["What will be done to correct and retest the failed safety-shutdown check before the job is closed out?"]
    analysis.bottom_line = (
        "Have the failed required safety-shutdown check corrected and retested—or get a second opinion—before accepting the work.")
    scope = []
    if re.search(r"remove (?:the )?existing equipment", quote_text, re.I):
        scope.append("removal of the existing equipment")
    if re.search(r"install (?:the )?(?:listed|new) equipment", quote_text, re.I):
        scope.append("installation of the new equipment")
    analysis.installation_concerns = "The submitted scope includes " + " and ".join(scope) + "." if scope else ""
    signs = [s for s in analysis.good_signs if not commissioning_text(s)
             and not re.search(r"(?:planned|voluntary|homeowner[- ]requested).{0,70}(?:upgrade|replacement)|replacement.{0,70}homeowner[- ]requested|comprehensive documentation", s, re.I)]
    if any(re.search(r"building[- ]specific load", s, re.I) for s in signs):
        signs = [s for s in signs if not re.search(r"capacity ratings.*align|appropriate.*loads|align.*cooling and heating requirements", s, re.I)]
    analysis.good_signs = list(dict.fromkeys(signs))
