"""Quote-only air-distribution review; no OEM lookup or inferred CFM targets."""
import re

DUCT_SUBJECT = "Duct and airflow support for proposed equipment"
DUCT_AIRFLOW_RULES = """
DUCT AND AIRFLOW SUPPORT FOR PROPOSED EQUIPMENT
Use a PRIMARY TechnicalEvidenceAssessment with subject
"Duct and airflow support for proposed equipment" whenever distribution affects approval.
Keep separate assessments for separate quotes/systems and identify each in its subject.
Own indoor supply/return distribution, total/supply/return static, delivered CFM versus
submitted airflow requirements, fan-table-supported blower setup, documented filter or
accessory pressure drop, physical duct defects/leakage, grille restrictions, corrective
duct scope and material zoning constraints. Do not redo motor electrical isolation or
refrigerant diagnosis. Consume established findings from their owning modules.
Do not decide building loads/tonnage, AHRI pairing, linesets, electrical service, general
commissioning, replacement economics or pricing.

SUPPORTED: ADEQUATE/CONFIRMED + APPROPRIATE. Supply meaningful applicable measurements,
design information or a documented physical restriction/defect with a clearly scoped
correction. Explain which findings support the work. Proposed corrections can be supported
without claiming that future measured performance is guaranteed.
PARTIAL: INCOMPLETE + PARTIALLY_DEFINED for ordinary missing verification, generic "ducts
checked", same-size replacement without useful support, vague correction, or missing
material zoning/setup information. Never use ABSENT/UNSUPPORTED for missing paperwork.
BAD: CONTRADICTORY + UNSUPPORTED for actual submitted conflicts left uncorrected, such as
static above the applicable submitted equipment limit, airflow materially below the
applicable submitted requirement, or a documented restriction with no correction.
Do not condemn wholesale ductwork from weak room airflow or comfort symptoms alone.

Use only submitted equipment-specific static limits and comparable tested configurations;
0.50 in. w.c. is NOT universal. Preserve measurement, limit, mode and configuration.
No universal 400 CFM/ton rule. Do not invent CFM, grille dimensions, fan performance or
limits from nominal capacity/model memory. A static reading without a reference alone
does not condemn ducts. A blower tap/profile alone does not prove delivered airflow.
Cooling delta-T/temperature split does not prove total airflow or duct/system sizing.
Furnace temperature rise within its range supports heating operation only, not whole-duct
adequacy or cooling airflow. Heat-pump airflow uses supplied mode-specific data, not backup
heat sizing. Matching, efficiency, same tonnage, historical operation, healthy blower and
"startup included" do not independently establish distribution support.
Return/supply findings must be documented; total static alone cannot locate a restriction.
Use documented accessory pressure drop, not assumptions about filters/UV/humidifiers.
Visible disconnections, torn flex or documented leakage can justify repair without a
mandatory leakage test. Symptoms do not prove leakage. Zoning: use submitted minimum-flow,
damper/static constraints; never design or mandate a bypass or invent zone airflow.
Existing ducts with larger equipment require review, not an automatic failure.
True ductless heads have no duct assessment; ducted mini-splits may. Outdoor condenser-fan
airflow and isolated motor/component/refrigerant repair are not whole-duct review.

CUSTOMER REPORT: explain what was found, what it means, why it matters and the next step.
Missing verification: zero duct red flags, one useful missing-information explanation and
one context-specific duct question. Actual unresolved conflict: one flag per underlying
problem, include applicable measured values and correction question. A scoped correction
must not remain an unresolved flag. Credit meaningful positive evidence, not generic
installation/startup/matching praise. Use natural homeowner language, not "methodology",
"distribution infrastructure" or guarantees. Keep independent module findings intact.
"""


def duct_text(value):
    text = re.sub(r"[-_]", " ", str(value).lower())
    if re.search(r"condenser (?:fan|airflow)|blower motor|motor (?:diagnosis|failure)|refrigerant diagnosis", text):
        return False
    return bool(re.search(r"\b(?:duct\w*|air distribution|static pressure|return (?:air|side|restriction)|supply and return|airflow (?:support|verification|requirements|setup)|delivered airflow|filter pressure drop|zoning airflow)\b|airflow.*(?:new|proposed|larger) (?:system|equipment)", text))


def duct_required(text, classification=None):
    text = str(text or "").lower()
    # A genuinely ductless system is excluded unless a separate duct issue is explicit.
    if re.search(r"ductless|non ducted", text) and not re.search(
        r"ducted mini|existing ducts|return restriction|duct (?:repair|modification|replacement)", text
    ):
        return False
    # A checklist saying that no duct review was done does not make an isolated
    # component repair an air-distribution project.
    text = re.sub(r"\bno (?:documented )?duct(?:work)? (?:review|evaluation|inspection)\b", "", text)
    return bool(re.search(
        r"ducted (?:hvac )?(?:mini|system|replacement)|(?:existing|reuse|reusing) (?:supply and return )?duct"
        r"|duct(?:work)? (?:review|evaluation|replacement|modification|redesign|repair)"
        r"|(?:replace|modify|repair|seal) (?:all |the |existing )?(?:supply and return )?duct"
        r"|(?:total external|return|supply) static pressure\s*(?:measured|reading|is|of|:)"
        r"|return(?: air)? (?:restriction|restrictive)|(?:documented|identified) (?:supply|duct) restriction"
        r"|zoning (?:airflow|static)|airflow (?:investigation|complaints)"
        r"|(?:supply|return) (?:duct|grille) (?:is |was )?(?:restrict|crush|disconnect)"
        r"|(?:replace|repair).*duct(?:work)?.*(?:weak airflow|restrict|damag)", text))


def duct_items(analysis):
    return [a for a in analysis.technical_assessments
            if a.materiality != "MINOR" and duct_text(a.subject)]


def _missing_only(value):
    return bool(re.search(r"\b(?:missing|not documented|not provided|not evaluated|no (?:static|airflow|measurement|verification|duct|conflict)|not verified|unverified|lack\w*|absence)\b", value, re.I)) and not re.search(
        r"(?:measured|documented|submitted).*(?:exceeds?|above|below)|(?:visibly|found) (?:disconnect|crush|torn|damaged)", value, re.I)


def static_comparison(evidence):
    """Compare only an unambiguous submitted pair tied to an applicable configuration.

    Multiple readings/limits or missing applicability cannot safely be collapsed.
    No default limit and no conversion from tonnage is permitted.
    """
    text = " ".join(evidence)
    if not re.search(r"(?:same|this|applicable|proposed) (?:equipment|configuration)", text, re.I):
        return None
    number = r"(\d+(?:\.\d+)?)"
    units = r"\s*in\.?\s*w\.?\s*c\.?"
    readings = re.findall(r"total external static(?: pressure)? (?:measured at|reading(?: is)?|of|is) " + number + units, text, re.I)
    limits = re.findall(r"(?:manufacturer |submitted )?(?:maximum |rated )?limit(?: of| is)? " + number + units, text, re.I)
    if len(set(readings)) != 1 or len(set(limits)) != 1:
        return None
    return float(readings[0]), float(limits[0])


def useful_support(evidence):
    """Evidence must explain support, not merely name a good practice."""
    text = " ".join(evidence)
    comparison = static_comparison(evidence)
    return bool((comparison and comparison[0] <= comparison[1]) or (
        re.search(r"(?:measured|reading|\d).*static.*(?:within|below).*\b(?:limit|maximum|rated)\b|static.*\d.*(?:within|below).*\b(?:limit|maximum|rated)\b", text, re.I)
        or re.search(r"(?:delivered|measured) airflow.*(?:target|requirement)", text, re.I)
        or re.search(r"(?:fan table|duct design|distribution design).*(?:documents?|supports?|specifies|calculat)", text, re.I)
    ))


def scoped_correction(item):
    text = " ".join(item.documented_evidence)
    return (item.scope_support == "APPROPRIATE" and not item.material_gaps
            and (bool(submitted_return_correction(text))
                 or bool(re.search(r"(?:scope|propos\w*|included|correction).*(?:replac\w*|repair\w*|seal\w*|enlarg\w*|modify\w*|reconnect\w*) (?:the |a |all )?(?:crushed |damaged |disconnected |restrictive |return |supply )*(?:duct|section|connection|return|grille)", text, re.I)))
            and bool(re.search(r"restrict|disconnect|crush|torn|damaged|leakage", text, re.I)))


def submitted_return_correction(text):
    """Recognize an explicit measured return defect + designed correction, not a promise."""
    if len(re.findall(r"(?m)^\s*QUOTE \d+", text)) > 1:
        return None
    # No dependency on a heading or a particular correction verb. Keep source
    # clauses together (including design qualifiers after semicolons).
    clauses = [s.strip() for s in re.split(r"\n|(?<=[.!?])\s+(?=[A-Z])", text) if s.strip()]
    denied = r"\b(?:no|not|excluded|optional|pending)\b"
    affirmative = [s for s in clauses if not re.search(denied, s, re.I)]
    defects = [s for s in affirmative if re.search(r"\breturn\b", s, re.I)
               and re.search(r"restrict\w*|crush\w*|disconnect\w*|damag\w*", s, re.I)]
    corrections = [s for s in affirmative if re.search(r"\breturn\b", s, re.I)
                   and re.search(r"\b(?:enlarg\w*|increas\w*|replac\w*|add\w*|modif\w*|correct\w*|expand\w*)\b", s, re.I)
                   and re.search(r"\b(?:scope|includes?|propos\w*|will)\b", s, re.I)
                   and re.search(r"(?:submitted|specified|attached).{0,35}design|\bdimensions\b|from \d+.{0,20}to \d+", s, re.I)]
    verification = [s for s in affirmative if re.search(r"verif\w*|recheck\w*|measur\w*", s, re.I)
                    and re.search(r"static", s, re.I) and re.search(r"airflow", s, re.I)
                    and re.search(r"after\w*|post[- ]work|startup|commissioning", s, re.I)]
    applicability = [s for s in affirmative if re.search(
        r"(?:same|this|proposed|applicable) (?:equipment|configuration)|equipment/configuration", s, re.I)]
    if not (defects and corrections and verification and applicability):
        return None
    compact = " ".join(clauses)
    values = []
    measurements = []
    link = r"\s*(?:(?:measured|was|is|at|:)\s*)*([+-]?\d+(?:\.\d+)?)"
    for label in (r"return(?:[- ](?:air|side))? static(?: pressure)?",
                  r"supply(?: static(?: pressure)?)?",
                  r"(?:total external static(?: pressure)?|total static(?: pressure)?|total)",
                  r"submitted (?:(?:manufacturer|equipment/configuration|equipment) )?limit"):
        pattern = label + link
        found = re.findall(pattern, compact, re.I)
        if len(set(found)) != 1:
            return None
        values.append(float(found[0]))
        measurements.extend(s for s in clauses if re.search(pattern, s, re.I))
    return "\n".join(dict.fromkeys([*measurements, *applicability, *defects, *corrections, *verification])), tuple(values)


def return_scope_omission(value):
    return bool(re.search(r"(?:no|missing|not|absent|unclear).*(?:correction|corrective|return.*scope)|(?:correction|corrective|return.*scope).*(?:missing|not|absent|unclear)", value, re.I))


def return_gap_resolved_by_source(value):
    """Only evidence/scope/verification omissions covered by the source recovery."""
    if re.search(r"supply.*(?:restrict|damag|disconnect)|leakage|filter|zoning|blower|"
                 r"contradict|conflict|ineffective|failed|insufficient|below.*requirement", value, re.I):
        return False
    return bool(return_scope_omission(value) or (
        re.search(r"duct|airflow|static|return", value, re.I)
        and re.search(r"missing|not |no |absent|unclear|unverified|verif\w*|confirm\w*|needed|needs|not yet", value, re.I)))


def normalize_duct_assessments(analysis, text, assessment_type, classification=None):
    """Only called on the finalizer's deep copy; absent assessment stays incomplete."""
    items = duct_items(analysis)
    if re.search(r"\bductless\b", text, re.I) and not duct_required(text, classification):
        analysis.technical_assessments = [a for a in analysis.technical_assessments if a not in items]
        return
    if not items and duct_required(text, classification):
        item = assessment_type(subject=DUCT_SUBJECT, materiality="PRIMARY",
            diagnostic_evidence_status="INCOMPLETE", scope_support="PARTIALLY_DEFINED",
            material_gaps=["The quote does not show how the ducts support the proposed airflow."])
        analysis.technical_assessments.append(item)
        items = [item]
    for item in items:
        recovery = submitted_return_correction(text) if len(items) == 1 else None
        if recovery:
            # Source establishes the proposed work, not its future measured outcome.
            original = item
            evidence = [e for e in item.documented_evidence if not return_scope_omission(e)]
            source_fact = "Submitted return correction:\n" + recovery[0]
            remaining_gaps = [gap for gap in item.material_gaps if not return_gap_resolved_by_source(gap)]
            item = item.model_copy(update=dict(
                diagnostic_evidence_status="INCOMPLETE" if remaining_gaps else "ADEQUATE",
                scope_support="PARTIALLY_DEFINED" if remaining_gaps else "APPROPRIATE",
                material_gaps=remaining_gaps, documented_evidence=list(dict.fromkeys([*evidence, source_fact])),
            ))
            analysis.technical_assessments = [item if a is original else a for a in analysis.technical_assessments]
        conflicts = [c for c in item.contradictions if not _missing_only(c)]
        evidence_conflicts = [e for e in item.documented_evidence
                             if re.search(r"exceeds? .*limit|below .*requirement|restriction.*(?:uncorrected|no correction)", e, re.I)]
        if not (recovery or scoped_correction(item)):
            conflicts = list(dict.fromkeys([*conflicts, *evidence_conflicts]))
            comparison = static_comparison(item.documented_evidence)
            if comparison and comparison[0] > comparison[1]:
                conflicts = [c for c in conflicts if not re.search(r"static", c, re.I)]
                conflicts.append(
                    f"The total external static reading is {comparison[0]:g} in. w.c., above the submitted "
                    f"{comparison[1]:g} in. w.c. limit for this equipment/configuration. No correction is documented."
                )
        else:
            # A proposed fix is not a completed measurement. Remove only the addressed
            # restriction's historical finding, never a conflicting correction design.
            correction_text = " ".join(item.documented_evidence).lower()
            conflicts = [c for c in conflicts if not (
                re.search(r"static|return|restrict|crush|disconnect", c, re.I)
                and not (re.search(r"supply.*(?:restrict|damag|disconnect)", c, re.I)
                         and not re.search(r"(?:replac\w*|repair\w*|reconnect\w*) (?:the )?supply", correction_text))
                and not re.search(r"correction.*(?:conflict|insufficient)|design.*conflict", c, re.I))]
        supported = (item.diagnostic_evidence_status in {"ADEQUATE", "CONFIRMED"}
                     and item.scope_support == "APPROPRIATE" and not item.material_gaps
                     and (useful_support(item.documented_evidence) or scoped_correction(item)))
        if conflicts:
            status, scope = "CONTRADICTORY", "UNSUPPORTED"
        elif supported:
            status, scope = item.diagnostic_evidence_status, "APPROPRIATE"
        else:
            status, scope = "INCOMPLETE", "PARTIALLY_DEFINED"
        updated = item.model_copy(update=dict(
            subject=DUCT_SUBJECT if len(items) == 1 else item.subject,
            materiality="PRIMARY", diagnostic_evidence_status=status, scope_support=scope,
            contradictions=conflicts,
            material_gaps=[] if supported and not conflicts else (item.material_gaps or
                (["The submitted information does not establish duct support for the proposed airflow."] if not conflicts else [])),
        ))
        analysis.technical_assessments = [updated if a is item else a for a in analysis.technical_assessments]


def submitted_capacity_increase(text):
    """Read a paired source statement, never infer capacity from equipment codes."""
    if len(re.findall(r"(?m)^\s*QUOTE \d+", text)) > 1:
        return None
    pairs = re.findall(
        r"existing (?:cooling )?system\s*:?\s*(\d+(?:\.\d+)?)\s*tons?"
        r"[^\n]{0,100}?proposed (?:cooling )?system\s*:?\s*(\d+(?:\.\d+)?)\s*tons?",
        text, re.I)
    if len(set(pairs)) != 1 or not re.search(r"existing (?:supply and return )?duct", text, re.I):
        return None
    old, new = map(float, pairs[0])
    return (old, new) if new > old else None


def capacity_duct_paragraph(values):
    old, new = values
    return (
        f"The proposal increases the cooling system from {old:g} tons to {new:g} tons while reusing the existing ducts. "
        "The larger equipment has different airflow needs, but the quote doesn't show whether the supply "
        "and return ducts were checked for those needs. This does not establish that the ducts are inadequate; "
        "ask for the airflow, static-pressure, or duct-design review before approving the change."
    )


def duct_paragraphs(analysis):
    paragraphs = []
    for item in duct_items(analysis):
        evidence = " ".join(customer_duct_evidence(value) for value in item.documented_evidence)
        correction_source = next((s.removeprefix("Submitted return correction:\n") for s in item.documented_evidence
                                  if s.startswith("Submitted return correction:\n")), "")
        correction = submitted_return_correction(correction_source)
        if item.scope_support == "UNSUPPORTED":
            paragraphs.append(" ".join(item.contradictions) + " The submitted information does not support leaving this air-distribution problem uncorrected. Ask what will be changed before approving the work.")
        elif item.scope_support == "PARTIALLY_DEFINED":
            if correction:
                paragraphs.append(return_correction_paragraph(correction) + " Remaining clarification: "
                                  + " ".join(item.material_gaps))
                continue
            # Sizing finalization already retains the single-project submitted
            # source. Use only that source, not model-generated capacity guesses.
            sources = [value for a in analysis.technical_assessments for value in a.documented_evidence
                       if value.startswith("Submitted sizing source:\n")]
            increase = submitted_capacity_increase(sources[0]) if len(sources) == 1 else None
            if increase and len(duct_items(analysis)) == 1:
                paragraphs.append(capacity_duct_paragraph(increase))
                continue
            larger = bool(re.search(r"larger|capacity increase", " ".join([*item.documented_evidence, *item.material_gaps]), re.I))
            context = "The proposed capacity increase makes checking the supply and return airflow especially important. " if larger else ""
            paragraphs.append(context + "The quote doesn't show whether the ducts were checked against the proposed equipment's airflow needs. This does not establish that the ducts are inadequate. Ask for the applicable airflow or duct review before approving the work.")
        else:
            if correction:
                paragraphs.append(return_correction_paragraph(correction))
                continue
            if scoped_correction(item):
                paragraphs.append(evidence + " The proposed correction addresses the documented restriction or damage. Airflow still needs to be checked after the work; these are proposed steps, not completed results.")
            else:
                paragraphs.append(evidence + " This gives a documented basis for using the proposed equipment with the ducts. Final airflow still depends on the installation and setup.")
    return paragraphs


def return_correction_paragraph(correction):
    r, s, total, limit = correction[1]
    scope = ("replacing the crushed return section and enlarging the restrictive connection"
             if re.search(r"replac\w*.*crushed return.*enlarg\w*.*return connection", correction[0], re.I | re.S)
             else "modifying the restricted return path")
    return (
        f"The documented restriction is on the return side: return static is {r:g} in. w.c. and supply static "
        f"is {s:+g} in. w.c., for {total:g} in. w.c. total against the submitted {limit:g} in. w.c. limit. "
        f"The proposal includes {scope} to the submitted design. That scope addresses the documented problem. "
        "Final static pressure and airflow still need to be verified after the work is completed."
    )


def customer_duct_evidence(value):
    """Keep evidence, remove appended promises that a proposal cannot establish."""
    cleaned = re.sub(
        r"[,;]?\s*(?:which |thereby |thus |and )?(?:ensur\w*|guarantee\w*)\b[^.]*\.?",
        ".", value, flags=re.I).strip()
    return re.sub(r"\bThis\s*\.", "", cleaned).strip()


def present_supported_duct_replacement(analysis, quote_text, sizing, matching):
    """Presentation only for a clean replacement with measured duct support.

    Do not alter decisions, evidence, actions or questions, or affect supported
    corrective duct repairs and other domain acceptance cases.
    """
    items = duct_items(analysis)
    relevant = [a for a in analysis.technical_assessments if a.materiality != "MINOR"]
    if (len(items) != 1 or not sizing or not matching
            or not re.search(r"existing (?:supply and return )?duct", quote_text, re.I)
            or analysis.decision.verdict != "PROCEED"
            or analysis.decision.technical_support != "SUPPORTED"
            or analysis.red_flags or analysis.contractor_questions
            or any(a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}
                   or a.scope_support != "APPROPRIATE" or a.material_gaps or a.contradictions
                   for a in relevant)):
        return
    evidence = items[0].documented_evidence
    static = next((s for s in evidence if re.search(r"static.*\d.*(?:within|below).*limit", s, re.I)), "")
    airflow = next((s for s in evidence if re.search(r"(?:delivered|measured) airflow.*\d.*(?:target|requirement)", s, re.I)), "")
    if not static or not airflow or scoped_correction(items[0]):
        return
    analysis.homeowner_takeaway = (
        "The load results support the proposed system size, and the submitted static-pressure "
        "and airflow measurements support using the new equipment with the existing ducts."
    )
    analysis.bottom_line = (
        "The submitted sizing, equipment match and duct measurements support the proposed system. "
        "Nothing in the reviewed sizing or duct information needs to be cleared up before approval."
    )
    analysis.missing_information = re.sub(
        r"No important sizing information is missing[^.]*\.",
        "No important sizing or duct-support information is missing from the submitted proposal.",
        analysis.missing_information,
    )
    positives = [
        "The proposed size is tied to building-specific load results.",
        "The submitted documentation shows the indoor and outdoor equipment are an approved matched combination.",
        *dict.fromkeys(customer_duct_evidence(s) for s in (static, airflow)),
    ]
    # Warranty is a coverage fact, never a reliability/security claim. Keep it
    # after the technical evidence, and only use terms actually in the quote.
    affirmative = " ".join(line for line in quote_text.splitlines()
                           if not re.search(r"\b(?:no|not|excluded|pending)\b", line, re.I))
    warranties = re.findall(r"\b\d+[- ]year (?:parts|labor) warranty\b", affirmative, re.I)
    if warranties:
        positives.append("The proposal includes " + " and ".join(dict.fromkeys(warranties)) + ".")
    analysis.good_signs = positives
    scope = []
    if re.search(r"remov\w*.*existing equipment", affirmative, re.I):
        scope.append("removal of the existing equipment")
    if re.search(r"install\w*.*(?:listed|new) (?:equipment|system)", affirmative, re.I):
        scope.append("installation of the new system")
    if re.search(r"startup.*verif|verif.*startup", affirmative, re.I):
        scope.append("startup verification")
    if scope:
        # Replace generic assurances, not independent concrete scope findings.
        retained = [s for s in re.split(r"(?<=[.!?])\s+", analysis.installation_concerns)
                    if not re.search(r"startup|no (?:specific|significant|major).*concern|important to ensure|proposal includes.*(?:removal|installation)", s, re.I)]
        analysis.installation_concerns = " ".join([
            "The proposal includes " + ", ".join(scope[:-1]) + (", and " if len(scope) > 1 else "") + scope[-1] + ".",
            *retained,
        ]).strip()


def compose_duct_summary(analysis):
    items = duct_items(analysis)
    unresolved = [a for a in items if a.scope_support != "APPROPRIATE"]
    others = [a for a in analysis.technical_assessments if a not in items and a.materiality != "MINOR"
              and (a.diagnostic_evidence_status in {"ABSENT", "INCOMPLETE", "CONTRADICTORY"}
                   or a.scope_support != "APPROPRIATE")]
    if not unresolved or others:
        return
    bad = any(a.scope_support == "UNSUPPORTED" for a in unresolved)
    analysis.banner_explanation = (
        "The submitted duct findings conflict with leaving the air-distribution system as proposed."
        if bad else "The quote needs to show that the ducts can support the proposed equipment."
    )
    analysis.homeowner_takeaway = (
        "The duct section explains the documented conflict. Get another opinion or a supported correction before approving the work."
        if bad else "The other technical findings support the proposed work, but the duct support still needs to be documented. Missing verification does not establish that the ducts are inadequate."
    )
    analysis.bottom_line = (
        "Get another opinion or have the contractor explain how the documented duct problem will be corrected before approving the work."
        if bad else "Ask the contractor to document how the ducts will support the proposed airflow before approving the work."
    )
    analysis.recommendation = analysis.decision.verdict.replace("_", " ") + " — " + analysis.banner_explanation


def only_partial_duct_gap(analysis):
    """Question/presentation scope, not a technical-support derivation."""
    items = duct_items(analysis)
    return bool(items and analysis.decision.technical_support == "PARTIALLY_SUPPORTED"
                and all(a.diagnostic_evidence_status == "INCOMPLETE"
                        and a.scope_support == "PARTIALLY_DEFINED" for a in items)
                and all(a.diagnostic_evidence_status in {"ADEQUATE", "CONFIRMED"}
                        and a.scope_support == "APPROPRIATE" and not a.material_gaps
                        and not a.contradictions
                        for a in analysis.technical_assessments
                        if a not in items and a.materiality != "MINOR"))


def only_measured_duct_conflict(analysis):
    """Return display values only when this is the sole structured technical gap."""
    items = duct_items(analysis)
    if (len(items) != 1 or analysis.decision.technical_support != "UNSUPPORTED"
            or items[0].diagnostic_evidence_status != "CONTRADICTORY"
            or items[0].scope_support != "UNSUPPORTED"
            or any(a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"}
                   or a.scope_support != "APPROPRIATE" or a.material_gaps or a.contradictions
                   for a in analysis.technical_assessments
                   if a not in items and a.materiality != "MINOR")):
        return None
    values = static_comparison(items[0].documented_evidence)
    return values if values and values[0] > values[1] else None


def present_bad_duct_replacement(analysis, sizing, matching):
    values = only_measured_duct_conflict(analysis)
    if not values or not sizing or not matching:
        return
    measured, limit = values
    comparison = (f"The measured total external static pressure is {measured:g} in. w.c., "
                  f"above the submitted {limit:g} in. w.c. limit")
    analysis.banner_explanation = (
        "The measured static pressure is above the limit listed for this equipment, "
        "and the quote doesn't show how it will be corrected."
    )
    analysis.recommendation = analysis.decision.verdict.replace("_", " ") + " — " + analysis.banner_explanation
    analysis.homeowner_takeaway = (
        "The equipment and sizing information look reasonable, but the duct measurement "
        "is above the limit submitted for this equipment. The quote does not show a correction. "
        "Have that addressed before approving the installation."
    )
    analysis.bottom_line = (comparison + ". I would not approve the installation until the "
                          "contractor explains what will be changed to correct that.")
    analysis.missing_information = (
        f"The quote does not explain what will be changed to bring the measured {measured:g} in. w.c. "
        f"static pressure back within the submitted {limit:g} in. w.c. limit."
    )
    analysis.installation_concerns = (
        comparison + ", and the quote does not show what will be changed to correct it "
        "before the new system is installed."
    )
    # Replace only duct-owned flags. Independent flags remain untouched.
    analysis.red_flags = [s for s in analysis.red_flags if not duct_text(s)]
    analysis.red_flags.append(comparison + ", and the quote does not include a correction.")
    sentences = re.split(r"(?<=[.!?])\s+", analysis.equipment_analysis)
    analysis.equipment_analysis = " ".join(s for s in sentences if not re.search(
        r"(?:match|combination|equipment|components).*(?:appropriate for.*loads|compatible with.*load|"
        r"ensur\w*|guarantee\w*|installation.*sound)", s, re.I))
    match_fact = "The submitted manufacturer documentation supports the proposed indoor/outdoor equipment combination."
    if match_fact not in analysis.equipment_analysis:
        analysis.equipment_analysis = (analysis.equipment_analysis + " " + match_fact).strip()
    warranty_facts = [s for s in analysis.good_signs
                      if re.search(r"\b\d+[- ]year\b.*\bwarranty\b", s, re.I)
                      and not re.search(r"maximiz|efficien|reliab|security|ensur|guarantee|planned|upgrade|match", s, re.I)]
    analysis.good_signs = list(dict.fromkeys([
        "The proposed size is tied to building-specific load results.",
        "The submitted manufacturer documentation shows the proposed indoor and outdoor equipment are an approved matched combination.",
        *warranty_facts,
    ]))


def present_partial_duct_replacement(analysis, quote_text, sizing, matching):
    if (not only_partial_duct_gap(analysis) or len(duct_items(analysis)) != 1 or not sizing or not matching
            or re.search(r"capacity increase|larger system", quote_text, re.I)
            or not re.search(r"existing (?:supply and return )?duct", quote_text, re.I)):
        return
    analysis.homeowner_takeaway = (
        "The equipment and system size look reasonable. The one thing this quote doesn't show "
        "is whether the existing ducts were checked for the airflow the new system needs. "
        "That doesn't mean the ducts are bad; this part still needs to be verified."
    )
    analysis.bottom_line = (
        "The equipment and sizing look reasonable, but the quote doesn't show whether the "
        "existing ducts were checked for the new system. Ask for the airflow or static-pressure "
        "review before approving the work."
    )
    analysis.missing_information = (
        "The quote does not include the airflow, static-pressure, or duct-design information used to confirm "
        "the existing ducts can support the new system."
    )
    # Keep independent replacement reasoning. Remove only matching/installation
    # overclaims rather than treating component pairing as installation proof.
    sentences = re.split(r"(?<=[.!?])\s+", analysis.equipment_analysis)
    analysis.equipment_analysis = " ".join(s for s in sentences if not re.search(
        r"planned installation.*(?:sound|support)|compatible with.*load requirements|"
        r"(?:matched|combination|components).*\b(?:ensure\w*|guarantee\w*|effectively)\b", s, re.I))
    match_fact = "The submitted manufacturer documentation supports the proposed equipment combination."
    if match_fact not in analysis.equipment_analysis:
        analysis.equipment_analysis = (analysis.equipment_analysis + " " + match_fact).strip()
    signs = [
        "The submitted documentation shows the indoor and outdoor equipment are an approved matched combination.",
        "The proposed size is tied to building-specific load results.",
    ]
    affirmative = " ".join(line for line in quote_text.splitlines()
                           if not re.search(r"\b(?:no|not|excluded|pending)\b", line, re.I))
    terms = re.findall(r"\b\d+[- ]year (?:parts|labor) warranty\b", affirmative, re.I)
    if terms:
        signs.append("The proposal includes " + " and ".join(dict.fromkeys(terms)) + ".")
    analysis.good_signs = signs


def present_capacity_increase(analysis, quote_text, sizing, matching):
    values = submitted_capacity_increase(quote_text)
    if (not values or not only_partial_duct_gap(analysis) or len(duct_items(analysis)) != 1
            or not sizing or not matching):
        return
    old, new = values
    analysis.homeowner_takeaway = (
        f"The proposed {new:g}-ton system fits the submitted load information. The open question is the existing "
        f"ductwork: with the change from {old:g} to {new:g} tons, the quote doesn't show whether the supply and return "
        "were checked for the new airflow needs. That doesn't mean the ducts are bad, but verify this before approving the change."
    )
    analysis.bottom_line = (
        f"The house-load numbers support the proposed {new:g}-ton system, but the quote doesn't show whether the "
        "existing ducts were checked for its airflow needs. Verify that before approving the equipment change."
    )
    analysis.missing_information = (
        "The quote does not show the airflow, static-pressure, or duct-design information used to confirm "
        "the existing ducts can support the larger proposed system."
    )
    analysis.good_signs = [
        "The proposed size is tied to building-specific load results.",
        "The submitted documentation shows the indoor and outdoor equipment are an approved matched combination.",
    ]
    affirmative = " ".join(line for line in quote_text.splitlines()
                           if not re.search(r"\b(?:no|not|excluded|pending)\b", line, re.I))
    terms = re.findall(r"\b\d+[- ]year (?:parts|labor) warranty\b", affirmative, re.I)
    if terms:
        analysis.good_signs.append("The proposal lists " + " and ".join(dict.fromkeys(terms)) + ".")
    if analysis.installation_concerns == "Review the documented installation scope with the contractor before approval.":
        analysis.installation_concerns = (
            "The proposal includes startup verification."
            if re.search(r"startup.*verif|verif.*startup", affirmative, re.I) else ""
        )


def present_supported_return_correction(analysis, quote_text, sizing, matching):
    items = duct_items(analysis)
    if (len(items) != 1 or not submitted_return_correction(quote_text) or not sizing or not matching
            or analysis.decision.technical_support != "SUPPORTED" or analysis.decision.verdict != "PROCEED"
            or analysis.red_flags or analysis.contractor_questions):
        return
    analysis.homeowner_takeaway = (
        "The sizing looks reasonable, and the static-pressure readings identified a return-side restriction. "
        "The proposal includes work to correct that restriction, so it is addressed in the planned scope. "
        "Final static pressure and airflow still need to be verified after the work is completed."
    )
    analysis.bottom_line = (
        "The submitted sizing supports the proposed system, and the documented return restriction is addressed "
        "in the proposed work. Nothing in the submitted sizing, equipment match, or duct scope needs to be "
        "cleared up before approval."
    )
    analysis.missing_information = "No material sizing or duct-support information needs to be cleared up before approval."
    affirmative = " ".join(line for line in quote_text.splitlines()
                           if not re.search(r"\b(?:no|not|excluded|pending)\b", line, re.I))
    scope = []
    if re.search(r"remov\w*.*existing equipment", affirmative, re.I):
        scope.append("removal of the existing equipment")
    if re.search(r"install\w*.*(?:listed|new) (?:equipment|system)", affirmative, re.I):
        scope.append("installation of the new system")
    scope.append("the return-duct correction")
    if re.search(r"startup.*verif|verif.*startup", affirmative, re.I):
        scope.append("startup verification")
    analysis.installation_concerns = (
        "The proposal includes " + ", ".join(scope) + ". "
        "Static pressure and delivered airflow verification are included after the work."
    )
    analysis.equipment_analysis = (
        "This is a homeowner-requested planned replacement. "
        if analysis.replacement_context == "elective" else ""
    ) + "The submitted manufacturer documentation supports the proposed equipment combination."
    analysis.good_signs = [
        "The proposed size is tied to building-specific load results.",
        "The submitted manufacturer documentation supports the proposed equipment combination.",
        "The proposal ties the documented return-side restriction to a specific return-duct correction.",
    ]
    terms = re.findall(r"\b\d+[- ]year (?:parts|labor) warranty\b", affirmative, re.I)
    if terms:
        analysis.good_signs.append("The proposal lists " + " and ".join(dict.fromkeys(terms)) + ".")


def duct_question(analysis, text=""):
    unresolved = [a for a in duct_items(analysis) if a.scope_support != "APPROPRIATE"]
    if not unresolved:
        return ""
    if any(a.scope_support == "UNSUPPORTED" for a in unresolved):
        return "What will be changed to correct the documented duct or static-pressure problem?"
    increase = submitted_capacity_increase(text)
    if increase:
        return (f"Did you check whether the existing supply and return ducts can support the airflow "
                f"needed by the larger system being proposed ({increase[1]:g} tons)?")
    if re.search(r"(?:larger|capacity increase|increas\w*.*(?:ton|capacity))", text, re.I):
        return "Did you check whether the supply and return ducts can support the airflow needed by the larger system?"
    return "What airflow measurements or duct design information show that the ducts can support the proposed equipment?"


def finalize_duct_fields(analysis, text=""):
    """Own duct prose after other domain cleanup; leave independent findings intact."""
    items = duct_items(analysis)
    if not items:
        return
    for name in ("project_overview", "equipment_analysis", "missing_information", "installation_concerns"):
        sentences = re.split(r"(?<=[.!?])\s+", getattr(analysis, name))
        setattr(analysis, name, " ".join(s for s in sentences if not duct_text(s)))
    analysis.red_flags = [s for s in analysis.red_flags if not duct_text(s)]
    analysis.good_signs = [s for s in analysis.good_signs if not duct_text(s)]
    for item, paragraph in zip(items, duct_paragraphs(analysis)):
        if item.scope_support == "UNSUPPORTED":
            analysis.red_flags.append(paragraph)
        elif item.scope_support == "PARTIALLY_DEFINED":
            analysis.missing_information = re.sub(
                r"No (?:important|critical|material|significant)[^.]*missing[^.]*\.?|No important sizing information is missing[^.]*\.?",
                "", analysis.missing_information, flags=re.I).strip()
            analysis.missing_information = (analysis.missing_information + " " + paragraph).strip()
        else:
            analysis.good_signs.extend(customer_duct_evidence(value) for value in item.documented_evidence[:1])
    analysis.contractor_questions = [q for q in analysis.contractor_questions if not duct_text(q)]
    question = duct_question(analysis, text)
    if question:
        analysis.contractor_questions.append(question)
    analysis.decision.required_actions = [a for a in analysis.decision.required_actions if not duct_text(a)]
    if question:
        analysis.decision.required_actions.append("Resolve the documented duct/airflow question before approving the work.")
