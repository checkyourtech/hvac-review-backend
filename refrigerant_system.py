"""Refrigerant evidence ownership, conservative source recovery and customer prose.

No thermodynamic limits are supplied here. Submitted measurements and interpreted
results are evidence; a procedure name, proposed test or refrigerant type is not.
"""
import re

CONDITION = "Refrigerant condition supporting proposed work"
CAUSE = "Leak / refrigerant-loss cause"
COIL = "Coil refrigerant-circuit condition"
METERING = "Metering-device / refrigerant-flow condition"
VERIFICATION = "Post-repair refrigerant verification"
SUBJECTS = (CONDITION, CAUSE, COIL, METERING, VERIFICATION)

REFRIGERANT_SYSTEM_RULES = """
REFRIGERANT SYSTEM — DIAGNOSIS AND PROPOSED WORK
Answer whether submitted evidence supports the refrigerant diagnosis and repair.
Use TechnicalEvidenceAssessment subjects only when material: Refrigerant condition
supporting proposed work; Leak / refrigerant-loss cause; Coil refrigerant-circuit
condition; Metering-device / refrigerant-flow condition; Post-repair refrigerant
verification. The diagnosis justifying work is PRIMARY. A material cause/scope gap
is MATERIAL_SECONDARY; optional detail is MINOR (there is no SECONDARY schema value).

Evidence hierarchy: actual interpreted pressure/temperature relationships,
applicable SH/SC, saturation/line temperatures, approach or weighed charge in context
can support charge condition. Actual detector, bubbles, dye, oil/refrigerant or
pressure-test results tied to a location can support leakage there. No particular
method is universally required. A test name is not a test result. Symptoms, ice,
low suction or temperature split alone, age, generic recharge, refrigerant type,
unlocated oil residue and contractor assertions do not establish the diagnosis.
Meaningful but incomplete findings => INCOMPLETE / PARTIALLY_DEFINED, zero refrigerant
red flags. Supported findings and appropriate work => ADEQUATE/CONFIRMED / APPROPRIATE.
Contradicted diagnosis or unsupported condemnation => CONTRADICTORY or ABSENT /
UNSUPPORTED when evidence warrants it. Do not condemn merely brief proposals.

For a documented located leak repair, lead with the confirmed leak, not raw
pressure/SH/SC numbers. Those numbers are supporting operating observations;
charge interpretation needs submitted equipment criteria and test conditions.
Never supply targets from model memory. Describe included repair work, not an
assurance of success. Avoid "evidence is solid", "logically follows", universal
diagnostic completeness claims and warranty "assurance". Credit each fact once.

DIAGNOSTIC-ONLY FIREWALL: Investigation before deciding on a repair is not an
already-proposed repair. Do not require the cause to be known before that visit.
Evaluate what the investigation includes and what findings will be provided.
An identified flat diagnostic fee does not require hourly/internal cost detail
or a breakdown of refrigerant that is not included. Preserve independent pricing
gaps. Raw pressure and superheat observations do not establish a particular fault.

Distinguish suspected leakage, evidence of leakage, confirmed leakage and a confirmed
location. Low charge NEVER proves a coil, valve or line-set leak location. Coil repair
needs evidence tying the circuit problem to that coil, not just low charge. TXV/piston
or restriction work needs evidence isolating refrigerant flow from charge, airflow,
other restrictions, supply and control causes as applicable; no universal checklist.
High superheat alone cannot condemn a TXV. Do not remotely diagnose another cause.

An intentionally opened circuit for justified coil/compressor/valve/lineset/metering
work can need charge restoration without a separate leak. Do not automatically ask
where the leak is. Keep repair recovery, filter-drier applicability, pressure testing,
evacuation and equipment-appropriate charge restoration knowledge proportional to
actual opened-circuit work; omissions are clarifications, not proof of bad workmanship.
Do not invent vacuum/pressure/charge limits. Planned verification is NOT completed
evidence. Nitrogen or appropriate inert-gas testing, micron/standing-vacuum results
and manufacturer charging procedures are evaluated only when applicable/documented.

DUCT_AIRFLOW owns duct adequacy; motors/electrical own blower failure; compressor
owns compressor condemnation; sizing and matching own capacity and compatibility.
COMMISSIONING owns the promised startup/verification process; refrigerant analysis
interprets actual refrigerant results. Supported startup, matching, or sizing does
not prove charge. A separate domain's unresolved issue must not change refrigerant
findings. Refrigerant listing or generic manufacturer charging in new-install scope
alone does not activate refrigerant diagnosis. Warranty and pricing stay separate.

Normally one or two questions: diagnostic evidence, material loss cause, leak
location, repair scope or refrigerant verification. No mandatory leak-search question
for every recharge. PARTIAL has zero refrigerant flags; one underlying BAD concern
has one flag. Credit actual evidence and supported repairs, not age/type, generic
testing, warranty or matched equipment as refrigerant positives. Say what is known,
what remains uncertain, and whether the work follows. Use natural homeowner language.

LOW-CHARGE CUSTOMER SAFEGUARDS
Proof of low refrigerant charge is not the same as proof of a refrigerant leak.
Poor cooling and icing are symptoms, not diagnostic evidence; attribute the proposed diagnosis to the contractor.
Do not say the symptom indicates or proves the diagnosis. Evaluate whether airflow was reasonably considered,
whether the proposal explains why charge is low, whether recharge-only work addresses the unresolved cause,
and how final charge or system operation will be verified.
Do not describe a leak search as universally mandatory or categorically critical.
Explain whether leak investigation was performed or recommended when appropriate.
For a BAD underlying issue, combine insufficient evidence establishing low charge and unresolved cause;
PARTIAL findings still receive zero refrigerant red flags.
Do not reduce the missing-information explanation to "leak search results."
When evidence is unresolved, do not list the low-refrigerant diagnosis itself as a good_sign.
Credit only independently documented favorable facts. Pricing transparency must remain outside red_flags.
Do not automatically characterize an absent leak search as a critical installation failure.
"""


def subject_kind(value):
    s = str(value or "").lower()
    if value in SUBJECTS:
        return value
    if re.search(r"equipment match|compatib|system siz|duct|airflow|blower|compressor|startup|commission|pricing|cost", s):
        return None
    if re.search(r"txv|piston|metering|refrigerant.flow|refrigerant restriction|^restriction$", s):
        return METERING
    if re.search(r"(?:coil|evaporator|condenser).*(?:leak|refrigerant|failure)|coil condition", s):
        return COIL
    if re.search(r"(?:refrigerant|charge).*verif|verif.*(?:refrigerant|charge)", s):
        return VERIFICATION
    if re.search(r"leak|refrigerant.loss", s):
        return CAUSE
    if re.search(r"refrigerant|low charge|system charge|charging issue|undercharg", s):
        return CONDITION
    return None


def refrigerant_items(analysis):
    return [a for a in analysis.technical_assessments if a.materiality != "MINOR" and subject_kind(a.subject)]


def refrigerant_required(text, classification=None):
    return bool(re.search(
        r"low (?:refrigerant|charge)|undercharg|refrigerant (?:loss|leak|restriction)|leak(?:ing)? (?:evaporator|condenser|coil|service valve)|"
        r"(?:evaporator|condenser|coil).{0,30}leak|\brecharge\b|add(?:ing)? .{0,25}refrigerant|"
        r"(?:replace|repair|restricted|stuck|failed).{0,25}(?:TXV|piston|metering device)|"
        r"(?:TXV|piston|metering device).{0,30}(?:replace|restrict|fail|diagnos|concern)|refrigerant.flow|"
        r"(?:circuit|system).{0,30}(?:opened|opening).{0,40}(?:repair|refrigerant)|"
        r"(?:replace|repair).{0,25}(?:evaporator coil|condenser coil|service valve|line.set)", text, re.I))


def evidence_lines(text):
    return [s.strip(" -\t") for s in text.splitlines() if s.strip()
            and not re.match(r"\s*(?:File Name:|QUOTE \d)", s, re.I)]


def completed_fact(s):
    return not re.search(r"\b(?:if|will|plan\w*|recommend\w*|should|need to|to be|not measured|no readings|not documented)\b", s, re.I)


def source_facts(text):
    """Abstain without explicit results, interpretation and relevant proposed work."""
    lines = evidence_lines(text)
    diagnostic = [s for s in lines if completed_fact(s)]
    joined = " ".join(diagnostic)
    source = " ".join(lines)
    readings = [s for s in diagnostic if (not re.search(r"target|required|specified", s, re.I) or re.search(r"measured", s, re.I)) and re.search(
        r"(?:superheat|subcooling|suction pressure|liquid pressure|head pressure|saturation temperature|line temperature|approach)\s*(?::|was|is|measured at)?\s*\d+(?:\.\d+)?\s*(?:°?[FC]|PSIG|psi)\b", s, re.I)]
    measured_types = set(re.findall(r"superheat|subcooling|suction pressure|liquid pressure|head pressure|saturation temperature|line temperature|approach", " ".join(readings), re.I))
    interpreted = any(re.search(r"(?:consistent with|supports?|confirmed?|indicates?).{0,35}(?:undercharg|low charge)|undercharged.{0,35}(?:confirmed|established)", s, re.I)
                      and not re.search(r"\b(?:not|no|inconsistent|cannot|doesn't|do not)\b", s, re.I) for s in diagnostic)
    # A detector result and a physically named site in the same source statement.
    located = [s for s in diagnostic if re.search(r"detector.{0,25}(?:indicated|confirmed|detected)|soap bubbles.{0,20}(?:confirmed|showed)|leak detected at|pressure test.{0,35}(?:isolated|located)|dye.{0,25}(?:showed|confirmed)", s, re.I)
               and re.search(r"coil|valve|Schrader|line.set|tubing|connection", s, re.I)
               and not re.search(r"\bno\b|\bnot\b|suspect|possible|negative|inconclusive", s, re.I)]
    coil_work = bool(re.search(r"(?:replace|replacement|repair).{0,30}(?:evaporator|condenser)?\s*coil|coil.{0,20}(?:replacement|requires replacement)", source, re.I))
    meter_work = bool(re.search(r"(?:replace|replacement|restricted|failed).{0,25}(?:TXV|piston|metering)|(?:TXV|piston|metering).{0,30}(?:replacement|failure|restricted)", source, re.I))
    opened = [s for s in diagnostic if re.search(r"(?:circuit|system).{0,30}(?:was |intentionally )?opened|refrigerant (?:was )?recovered.{0,40}(?:repair|replace)", s, re.I)]
    justified_work = [s for s in diagnostic if re.search(r"(?:damaged|broken|confirmed leaking|documented failure|repaired|replaced).{0,40}(?:valve|coil|line.set|compressor|TXV)|(?:valve|coil|line.set|compressor|TXV).{0,40}(?:damaged|broken|repaired|replaced)", s, re.I)]
    restoration = bool(re.search(r"recharge|restor\w*.{0,20}(?:refrigerant|charge)|weigh.{0,20}charge", source, re.I))
    verification = bool(re.search(r"(?:verify|confirm|document).{0,35}(?:final |proper )?(?:refrigerant charge|charge|superheat|subcooling)|manufacturer charging procedure", source, re.I))
    cause_unclear = bool(re.search(r"cause.{0,30}(?:unknown|unclear|not established)|reason for.{0,20}low.{0,20}not", source, re.I))
    contradiction = [s for s in diagnostic if re.search(r"(?:charge|refrigerant).{0,55}(?:contradicts?|rules out).{0,30}(?:low charge|undercharg)|(?:normal|correct) charge.{0,50}(?:but|despite).{0,30}(?:recharge|add)|leak.{0,50}(?:valve|line.set).{0,40}(?:not|rather than).{0,15}coil|not consistent with (?:an? )?(?:undercharg|low charge)", s, re.I)]
    if coil_work and located and not any(re.search(r"coil", s, re.I) for s in located):
        contradiction.append("The proposed coil replacement is not tied to the submitted leak-location result: " + " ".join(located))
    strong_meter = meter_work and bool(re.search(r"(?:TXV|metering device).{0,40}(?:does not respond|failed to respond|stuck closed|confirmed restricted)", joined, re.I)) and bool(re.search(r"charge verified|charge was verified|refrigerant charge verified", joined, re.I)) and bool(re.search(r"(?:no|ruled out).{0,30}(?:other|filter.drier|liquid.line) restriction|adequate liquid refrigerant.{0,25}inlet", joined, re.I))
    kind = METERING if meter_work else COIL if coil_work else CONDITION
    if not meter_work and not coil_work and located and not re.search(r"undercharg|low (?:charge|refrigerant)", source, re.I):
        kind = CAUSE
    supported = ((len({v.lower() for v in measured_types}) >= 2 and interpreted and restoration and not cause_unclear)
                 or (located and kind == CAUSE and (re.search(r"replace|repair", source, re.I)))
                 or (opened and justified_work and restoration and verification))
    if meter_work:
        supported = strong_meter
    elif coil_work:
        supported = bool(located and any(re.search(r"coil", s, re.I) for s in located))
    if len(re.findall(r"(?m)^\s*QUOTE \d+", text)) > 1:
        # Source-only recovery cannot join findings across distinct proposals.
        supported, contradiction = False, []
    evidence = list(dict.fromkeys(readings + located + opened + justified_work))
    diagnostic_visit = diagnostic_only_scope(text)
    if diagnostic_visit:
        evidence.append(diagnostic_visit[0])
    if interpreted:
        evidence += [s for s in diagnostic if re.search(r"consistent with|supports? .{0,25}(?:charge|undercharg)", s, re.I)
                     and not re.search(r"\b(?:not|no|cannot)\b", s, re.I)]
    if strong_meter:
        evidence += [s for s in diagnostic if re.search(r"TXV|metering|restriction|charge verified|charge was verified", s, re.I)]
    if contradiction:
        status, scope = "CONTRADICTORY", "UNSUPPORTED"
    elif supported:
        status, scope = "ADEQUATE", "APPROPRIATE"
    else:
        status, scope = "INCOMPLETE", "PARTIALLY_DEFINED"
    return dict(subject=kind, diagnostic_evidence_status=status, scope_support=scope,
                documented_evidence=evidence, material_gaps=[] if supported or contradiction else
                (["The diagnostic scope should state what will be checked and what findings will be provided."]
                 if diagnostic_visit else [gap_for(kind)]),
                contradictions=contradiction)


def diagnostic_only_scope(text):
    """Explicit deferred repair only; never infer this from a diagnostic price alone."""
    scope = next((s for s in evidence_lines(text) if re.search(
        r"(?:proposed work|scope):.*investigat.*before deciding whether to (?:recharge|repair)", s, re.I)), None)
    if not scope or len(re.findall(r"(?m)^\s*QUOTE \d+", text)) > 1:
        return None
    if any(re.search(r"(?:recommended repair|repair scope):|(?:include|perform|replace|add)\b.*(?:refrigerant|valve|coil|TXV)", s, re.I)
           for s in evidence_lines(text) if s != scope):
        return None
    price = re.search(r"Total diagnostic price:\s*(\$[\d,]+(?:\.\d{2})?)", text, re.I)
    return scope, price.group(1) if price else ""


def gap_for(kind):
    return {
        COIL: "The submitted findings do not tie the refrigerant problem to the proposed coil repair.",
        METERING: "The refrigerant-flow findings do not yet isolate the metering device as the cause.",
        CAUSE: "The quote does not clearly establish the refrigerant-loss cause or leak location.",
        VERIFICATION: "The relevant post-repair refrigerant results are not documented.",
    }.get(kind, "The quote does not fully establish the refrigerant condition or why the proposed work addresses it.")


def coil_location_conflict(text):
    """Only a documented valve-versus-coil contradiction, not an inferred cause."""
    if (re.search(r"Electronic leak detector confirmed (?:a )?leak at the liquid-line service valve", text, re.I)
            and re.search(r"(?:replace|replacement).{0,30}(?:evaporator )?coil|coil replacement", text, re.I)
            and re.search(r"valve,? not the coil|coil replacement is not tied", text, re.I)):
        return ("The submitted findings document a leak at the liquid-line service valve, but the quote recommends "
                "evaporator-coil replacement without showing evidence that the coil is leaking.")
    return None


def normalize_refrigerant_assessments(analysis, text, assessment_type, classification=None):
    items = refrigerant_items(analysis)
    if not items and not refrigerant_required(text, classification):
        return
    if not text.strip():
        return
    facts = source_facts(text)
    if coil_location_conflict(text):
        # A generic AI leak-cause label must not hide an explicit source mismatch.
        affected = [a for a in items if subject_kind(a.subject) in {CAUSE, CONDITION, COIL}]
        analysis.technical_assessments = [a for a in analysis.technical_assessments if a not in affected]
        analysis.technical_assessments.append(assessment_type(materiality="PRIMARY", **facts))
        return
    if not items:
        analysis.technical_assessments.append(assessment_type(materiality="PRIMARY", **facts))
        return
    # Preserve independently material subconditions; recovery cannot wipe out a
    # real contradiction, gap or a diagnosis belonging to another module.
    for item in items:
        kind = subject_kind(item.subject)
        updates = {"subject": kind}
        visit = diagnostic_only_scope(text)
        if visit and kind == CONDITION and item.scope_support == "PARTIALLY_DEFINED" and item.diagnostic_evidence_status == "INCOMPLETE":
            updates["documented_evidence"] = list(dict.fromkeys(item.documented_evidence + facts["documented_evidence"]))
            updates["material_gaps"] = facts["material_gaps"]
        relevant = kind == facts["subject"]
        explicit_conflicts = [c for c in item.contradictions if not re.search(r"not documented|not provided|no evidence|missing", c, re.I)]
        if relevant and facts["contradictions"]:
            updates.update(facts)
            updates["contradictions"] = list(dict.fromkeys(explicit_conflicts + facts["contradictions"]))
        elif relevant and facts["scope_support"] == "APPROPRIATE" and not explicit_conflicts and not item.material_gaps and item.scope_support != "UNSUPPORTED":
            updates.update(facts)
        elif (relevant and facts["scope_support"] == "PARTIALLY_DEFINED"
              and item.scope_support == "APPROPRIATE" and item.diagnostic_evidence_status in {"ADEQUATE", "CONFIRMED"}
              and (not item.documented_evidence or all(re.fullmatch(
                  r"(?:superheat|subcooling|pressures?|leak test|TXV|refrigerant type)[ .]*", s, re.I)
                  for s in item.documented_evidence))):
            # Naming a procedure is not a completed result, even inside an AI
            # assessment marked supported. Other meaningful structured facts stand.
            updates.update(facts)
        analysis.technical_assessments = [a.model_copy(update=updates) if a is item else a for a in analysis.technical_assessments]


def refrigerant_question_purpose(value):
    if re.search(r"price|pricing|cost|itemiz|breakdown|warranty|coverage|startup|commissioning", value, re.I):
        return None
    if not re.search(r"refrigerant|low charge|leak|TXV|piston|metering|coil", value, re.I):
        return None
    if re.search(r"where|location|locat", value, re.I):
        return "leak_location"
    if re.search(r"verif|after|post.repair|retest", value, re.I):
        return "refrigerant_verification"
    if re.search(r"why|cause|reason|investigat", value, re.I):
        return "refrigerant_cause"
    if re.search(r"scope|repair includes|work include", value, re.I):
        return "refrigerant_repair_scope"
    return "refrigerant_evidence"


def refrigerant_owned(value):
    return bool(refrigerant_question_purpose(value) or re.search(r"why the system is low|cause of low charge", value, re.I))


def refrigerant_paragraphs(analysis):
    result = []
    for item in refrigerant_items(analysis):
        kind = subject_kind(item.subject)
        if item.scope_support == "UNSUPPORTED" or item.diagnostic_evidence_status in {"ABSENT", "CONTRADICTORY"}:
            mismatch = coil_location_conflict("\n".join(item.documented_evidence + item.contradictions))
            result.append((mismatch + " That disconnect is why the proposed refrigerant repair is not supported yet.") if mismatch else
                          "The proposed refrigerant work is not supported by the submitted findings. " + " ".join(item.contradictions or item.material_gaps or [gap_for(kind)]))
        elif item.scope_support == "PARTIALLY_DEFINED" or item.diagnostic_evidence_status == "INCOMPLETE":
            if diagnostic_only_scope("\n".join(item.documented_evidence)):
                readings = [s.replace(" F.", " °F.") for s in item.documented_evidence
                            if re.search(r"(?:suction pressure|superheat)\s*:\s*\d", s, re.I)]
                result.append(" ".join(readings) + " These are measured observations, not proof of a particular refrigerant fault. "
                              "The quoted work is for further diagnosis; the cause should be documented before a repair is authorized.")
            else:
                result.append(gap_for(kind) + " " + " ".join(item.documented_evidence[:2]))
        else:
            leak = located_leak_presentation(item.documented_evidence)
            result.append(leak[1] if leak else "The submitted refrigerant findings support the proposed work. " + " ".join(item.documented_evidence))
    return result


def located_leak_presentation(evidence):
    """Presentation only: distinguish a documented location from operating data."""
    text = "\n".join(evidence)
    match = re.search(r"Electronic leak detector confirmed (?:a )?leak at (?:the )?([^\n.]+)", text, re.I)
    if not match:
        return None
    location = match.group(1).strip()
    observations = []
    for label in ("suction pressure", "subcooling", "superheat"):
        reading = re.search(rf"{label}\s*:\s*(\d+(?:\.\d+)?)\s*(PSIG|°?F)\b", text, re.I)
        if reading:
            unit = "PSIG" if reading.group(2).upper() == "PSIG" else "°F"
            observations.append(f"Recorded {label}: {reading.group(1)} {unit}.")
    paragraph = (f"An electronic leak detector confirmed a leak at the {location}. "
                 + " ".join(observations)
                 + " The confirmed leak supports the proposed repair. The readings provide additional operating data; "
                 "their exact interpretation depends on the equipment and test conditions, not universal charge limits.")
    return location, paragraph, observations


def refrigerant_customer_fields(analysis, source_text=""):
    items = refrigerant_items(analysis)
    if not items:
        return
    for name in ("equipment_analysis", "missing_information", "installation_concerns"):
        sentences = re.split(r"(?<=[.!?])\s+", getattr(analysis, name))
        setattr(analysis, name, " ".join(s for s in sentences if not refrigerant_owned(s)))
    for name in ("red_flags", "good_signs", "contractor_questions"):
        setattr(analysis, name, [s for s in getattr(analysis, name) if not refrigerant_owned(s)])
    for name in ("required_actions", "verdict_reasons"):
        setattr(analysis.decision, name, [s for s in getattr(analysis.decision, name) if not refrigerant_owned(s)])
    unresolved = [a for a in items if a.scope_support != "APPROPRIATE" or a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}]
    bad = [a for a in unresolved if a.scope_support == "UNSUPPORTED" or a.diagnostic_evidence_status in {"ABSENT", "CONTRADICTORY"}]
    if bad:
        prefix = ("The low-charge diagnosis and proposed refrigerant repair are not supported by the submitted evidence. "
                  if all(subject_kind(a.subject) == CONDITION for a in bad) else
                  "The proposed refrigerant repair is not supported by the submitted evidence. ")
        analysis.red_flags.append(prefix + " ".join(dict.fromkeys(c for a in bad for c in a.contradictions)))
    questions = {
        CONDITION: "What readings establish the refrigerant condition and support the proposed repair?",
        CAUSE: "What is known about the refrigerant loss, and where was any leak actually confirmed?",
        COIL: "What evidence ties the refrigerant leak or failure to the coil being replaced?",
        METERING: "What test showed the metering device is the cause of the refrigerant-flow problem?",
        VERIFICATION: "What post-repair refrigerant results verify the repair?",
    }
    for item in unresolved[:2]:
        kind = subject_kind(item.subject)
        analysis.contractor_questions.append(questions[kind])
    if unresolved:
        concerns = list(dict.fromkeys(gap_for(subject_kind(a.subject)) for a in unresolved))
        if re.search(r"^No .*missing|^No material", analysis.missing_information):
            analysis.missing_information = ""
        analysis.missing_information = " ".join([analysis.missing_information, *concerns]).strip()
        action = "Clarify the documented refrigerant evidence and proposed repair before approving the work."
        analysis.decision.required_actions.insert(0, action)
        analysis.decision.verdict_reasons.extend(concerns)
    else:
        analysis.good_signs.append("Documented refrigerant findings support the proposed work.")
    if diagnostic_only_scope(source_text) and unresolved and not bad:
        finalize_diagnostic_visit(analysis, source_text)


def finalize_diagnostic_visit(analysis, text):
    scope = diagnostic_only_scope(text)
    if not scope:
        return
    _, amount = scope
    visit = f"the {amount} diagnostic visit" if amount else "the diagnostic visit"
    concern = f"The quote should clarify what {visit} includes and what findings will be provided before additional repair work is approved."
    # Keep unrelated domain gaps/questions; replace the refrigerant-owned issue only.
    for name in ("required_actions", "verdict_reasons"):
        values = [s for s in getattr(analysis.decision, name) if not refrigerant_owned(s)
                  and "diagnostic visit" not in s.lower()]
        setattr(analysis.decision, name, values + [concern])
    analysis.contractor_questions = [q for q in analysis.contractor_questions if not refrigerant_owned(q)]
    analysis.contractor_questions.append(f"What does {visit} include, and will you document the refrigerant problem before recommending any additional repair?")
    # A documented all-labor diagnostic fee is not a charge for unquoted refrigerant.
    labor = re.search(r"(?m)^Labor:\s*(\$[\d,]+(?:\.\d{2})?)", text, re.I)
    pricing_text = " ".join([analysis.pricing_review, *analysis.decision.required_actions, *analysis.decision.verdict_reasons])
    if amount and labor and labor.group(1) == amount and not re.search(
            r"(?:unclear|missing|undisclosed|additional|excluded).{0,25}(?:fee|tax|charge)|(?:fee|tax|charge).{0,25}(?:unclear|undisclosed)", pricing_text, re.I):
        analysis.decision.pricing_transparency = "ADEQUATE"
        for name in ("required_actions", "verdict_reasons"):
            setattr(analysis.decision, name, [s for s in getattr(analysis.decision, name)
                    if not re.search(r"itemiz|breakdown|separate pricing|hourly", s, re.I)])
        analysis.pricing_review = f"The {amount} total is labeled as diagnostic labor. No refrigerant or component repair is included; any additional work needs separate authorization."
    unrelated = [a for a in analysis.technical_assessments if a not in refrigerant_items(analysis)
                 and a.materiality != "MINOR" and (a.scope_support != "APPROPRIATE" or a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"})]
    if not unrelated:
        analysis.missing_information = concern
        analysis.installation_concerns = "The quoted scope is for further refrigerant diagnosis; any repair should be based on what that investigation finds."
        analysis.equipment_analysis = "The recorded refrigerant-side readings leave the cause unresolved. The contractor is quoting further investigation, not a specific repair."


def compose_refrigerant_summary(analysis, source_text=""):
    items = refrigerant_items(analysis)
    if not items:
        return
    unrelated = [a for a in analysis.technical_assessments if a not in items and a.materiality != "MINOR"
                 and (a.scope_support != "APPROPRIATE" or a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"})]
    if unrelated:
        return
    mismatch = coil_location_conflict(source_text)
    if mismatch and analysis.decision.verdict == "GET_A_SECOND_OPINION":
        analysis.red_flags = [mismatch]
        analysis.missing_information = "The quote does not show evidence that the evaporator coil is leaking or has failed enough to justify replacement."
        analysis.equipment_analysis = mismatch
        analysis.installation_concerns = "The proposed evaporator-coil replacement is not tied to the documented leak finding."
        analysis.homeowner_takeaway = "The quote recommends replacing the evaporator coil, but the submitted findings identify the service valve as the leak location. Ask what evidence ties the coil replacement to the leak before approving that repair."
        analysis.bottom_line = "Have the contractor show what evidence ties the evaporator coil to the refrigerant leak, or get a second opinion before approving the repair."
        analysis.banner_explanation = mismatch
        analysis.recommendation = "GET A SECOND OPINION — " + mismatch
        analysis.contractor_questions = ["What evidence shows that the evaporator coil is the source of the refrigerant leak and needs replacement?"] + [q for q in analysis.contractor_questions if re.search(r"itemiz|breakdown", q, re.I)]
        analysis.good_signs = [s for s in analysis.good_signs if not refrigerant_owned(s) and not re.search(r"startup|start-up|elective|commission", s, re.I)]
        # Keep submitted amounts independent of the unsupported repair conclusion.
        amounts = [re.search(pattern, source_text, re.I) for pattern in (
            r"Total price:\s*(\$[\d,]+)", r"Equipment and refrigerant:\s*(\$[\d,]+)",
            r"Labor and installation materials:\s*(\$[\d,]+)")]
        if all(amounts) and analysis.decision.pricing_transparency == "ADEQUATE":
            analysis.pricing_review = (f"The quoted total is {amounts[0].group(1)}: {amounts[1].group(1)} for equipment and refrigerant "
                                       f"and {amounts[2].group(1)} for labor and installation materials. Pricing is separate from whether the proposed repair is supported.")
        return
    unresolved = [a for a in items if a.scope_support != "APPROPRIATE" or a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}]
    if unresolved:
        bad = any(a.scope_support == "UNSUPPORTED" or a.diagnostic_evidence_status in {"ABSENT", "CONTRADICTORY"} for a in unresolved)
        analysis.homeowner_takeaway = ("The proposal recommends refrigerant work that is not supported by the submitted findings."
                                       if bad else "The refrigerant findings leave an important question about the condition or proposed repair. Clarify that before approving the work.")
        analysis.bottom_line = ("Get a second opinion on the proposed refrigerant repair before approving it."
                                if analysis.decision.verdict == "GET_A_SECOND_OPINION" else "Clarify the refrigerant evidence and repair scope before approving the work.")
        visit = diagnostic_only_scope(source_text)
        if visit and not bad:
            amount = f"{visit[1]} " if visit[1] else ""
            analysis.homeowner_takeaway = "The quote does not yet establish the refrigerant fault, but the current charge is for further diagnosis, not a repair. Have the contractor document the findings before approving additional refrigerant work."
            analysis.bottom_line = f"Confirm what the {amount}diagnostic visit includes and ask for the findings before approving any additional repair."
            analysis.banner_explanation = "The quoted investigation needs a clearer description of the checks and findings you will receive."
            analysis.recommendation = "REVIEW BEFORE APPROVING — " + analysis.banner_explanation
    elif analysis.decision.verdict == "PROCEED":
        analysis.homeowner_takeaway = "The submitted refrigerant findings support the proposed repair."
        analysis.bottom_line = "The documented refrigerant evidence supports moving forward with the proposed work."
        present_supported_leak_repair(analysis, source_text, items)


def present_supported_leak_repair(analysis, source_text, items):
    """Narrow final copy pass; never changes assessments, actions or decisions."""
    if any(a not in items and a.materiality != "MINOR"
           and not re.search(r"startup|commission", a.subject, re.I)
           for a in analysis.technical_assessments):
        return
    leak = located_leak_presentation([s for a in items for s in a.documented_evidence])
    if not leak or not source_text or len(re.findall(r"(?m)^\s*QUOTE \d+", source_text)) > 1:
        return
    location, _, observations = leak
    if not re.search(r"repair|replace", source_text, re.I):
        return
    analysis.equipment_analysis = (f"The quote documents an actual leak at the {location}, so repairing that leak makes sense. "
                                   "The technician also recorded refrigerant-side measurements." if observations else
                                   f"The quote documents an actual leak at the {location}, so repairing that leak makes sense.")
    analysis.missing_information = "No material information about the identified leak or proposed repair needs to be cleared up before approval."
    analysis.installation_concerns = f"The proposal identifies the leaking {location} and includes repair of that leak."
    restoration = bool(re.search(r"restor\w*.{0,20}(?:refrigerant|charge)", source_text, re.I))
    if restoration:
        analysis.installation_concerns += " Restoration of the refrigerant charge is included."
    # Replace only refrigerant/warranty boilerplate; retain independent positives.
    analysis.good_signs = [s for s in analysis.good_signs if not refrigerant_owned(s)
                           and not re.search(r"warranty.*assurance|assurance.*warranty", s, re.I)]
    analysis.good_signs.insert(0, f"An electronic leak detector documented the leak at the {location}.")
    if observations:
        analysis.good_signs.append("Refrigerant-side measurements are recorded in the quote.")
    warranty = re.search(r"\b\d+[- ]year parts and labor warranty\b", source_text, re.I)
    if warranty:
        analysis.good_signs = [s for s in analysis.good_signs if "warranty" not in s.lower()]
        analysis.good_signs.append(f"The proposal includes a {warranty.group(0)}.")
    analysis.homeowner_takeaway = (f"The contractor documented an actual leak at the {location}, "
                                   "which gives a clear reason for the proposed refrigerant repair.")
    analysis.bottom_line = (f"The documented leak supports repairing the {location}"
                            + (" and restoring the refrigerant charge." if restoration else ".")
                            + " Nothing material in the submitted refrigerant evidence needs to be cleared up before approval.")
