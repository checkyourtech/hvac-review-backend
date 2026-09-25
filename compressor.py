"""Compressor-failure evidence and customer ownership; no verdict policy here."""
import re

COMPRESSOR_SUBJECT = "Claimed compressor failure"
COMPRESSOR_RULES = """
COMPRESSOR EVIDENCE AND PROPOSED WORK
Does the submitted proposal provide enough evidence that the compressor has failed
and that the proposed compressor repair or replacement addresses the documented problem?
Create a PRIMARY TechnicalEvidenceAssessment with subject "Claimed compressor failure"
whenever that diagnosis/work is material. Do not let a missing assessment silently pass.

Evidence hierarchy: an isolated terminal-to-ground short, failed insulation test
directly at compressor terminals, confirmed open winding, documented electrical
winding failure, or an explicitly supplied manufacturer failure criterion can be
sufficient alone. Locked-rotor/mechanical failure needs applicable supply/start/input
isolation, not high current or "locked up" alone. Never invent limits or test results.
Do NOT require a universal checklist. A direct open winding does not require a ground
test; a direct isolated ground fault does not require extra winding measurements.
Do not infer failure from age, noise, poor cooling, fault code alone, low refrigerant,
a failed capacitor, burned contactor, low voltage, missing command or overload alone.
Meaningful starting/input observations with unresolved isolation are INCOMPLETE /
PARTIALLY_DEFINED, not contradictory. Essentially no compressor-failure evidence
supporting condemnation is ABSENT / UNSUPPORTED. Actual conflicting evidence, such
as normal compressor operation after correcting the starting component, or a negative
isolated ground test contradicting the claimed ground fault with no alternate failure,
is CONTRADICTORY / UNSUPPORTED. Do not manufacture a conflict from missing detail.
Direct supported failure and matching work are CONFIRMED or ADEQUATE / APPROPRIATE.

Own only claimed compressor failure, failure mode, isolation and whether compressor
work follows. Electrical controls owns independent capacitor/contactor/board failures;
motors owns other motors. A valid capacitor diagnosis cannot rescue compressor
condemnation. Do not duplicate the capacitor finding as a compressor red flag.
Refrigerant recovery, filter drier, evacuation and charging are scope, NOT compressor
failure evidence. Refrigerant condition/leak/metering belongs to REFRIGERANT_SYSTEM.
COMMISSIONING owns startup completeness; its commitments cannot prove compressor
failure. Replacement basis consumes, never upgrades, compressor evidence; supported
compressor failure alone does not establish whole-system replacement economics.
Matching, sizing, ducts, pricing and warranty entitlement remain independent.

Customer language: say what was measured, what failed, what remains uncertain, and
why the compressor work follows or does not. Avoid "solid evidence", "logical
conclusion", "reasonable assurance", "material technical concern", "technically
justified" and long checklists. GOOD: no compressor question or flag. PARTIAL:
one compressor evidence/isolation question, zero compressor flags. BAD: one question
and one underlying compressor flag, not several versions of the same concern.
Positive findings must be actual compressor evidence, not generic startup, warranty,
price or refrigerant procedures. Other independently material domains may still ask
their own questions. Never remotely diagnose an alternative cause.
"""


def source_lines(text):
    return [s.strip(" -\t") for s in text.splitlines() if s.strip()
            and not re.match(r"\s*(?:File Name:|Local Path:|QUOTE \d)", s, re.I)]


def compressor_subject(value):
    s = str(value or "")
    if s == COMPRESSOR_SUBJECT:
        return True
    return bool(re.search(r"compressor|winding failure|locked.rotor", s, re.I)
                and not re.search(r"replacement basis|basis for|repair.vs|econom|warranty|pric|commission|startup|refrigerant|charge|equipment match|sizing", s, re.I))


def compressor_items(analysis):
    return [a for a in analysis.technical_assessments if a.materiality != "MINOR" and compressor_subject(a.subject)]


def compressor_required(text, classification=None):
    for line in source_lines(text):
        # A mention inside new-equipment specifications is not a failure claim.
        if re.search(r"no compressor (?:failure|repair|replacement)|not replacing (?:the )?compressor", line, re.I):
            continue
        if re.search(r"(?:replace|repair|diagnos\w*|condemn\w*) (?:the |a |failed |existing )*compressor\b|"
                     r"compressor (?:replacement|repair|failure|diagnosis)|"
                     r"(?:grounded|locked|failed|shorted|open.winding) compressor|"
                     r"compressor.{0,55}(?:failed|failure|short.to.ground|shorted|ground fault|electrically grounded|open winding|will not start|does not start|fails to start)|"
                     r"compressor (?:winding|terminal).{0,50}(?:test|ground|open|short)|open compressor winding", line, re.I):
            return True
    return False


def _actual(line):
    if re.match(r"(?:proposed|recommended|repair scope|scope includes|scope:)", line, re.I):
        return False
    return not re.search(r"\b(?:will|would|should|may|might|if|plan\w*|recommend\w*|suspect\w*)\b|not (?:tested|measured|documented)|no .{0,100}(?:test results?|readings).{0,20}(?:provided|documented)|no .{0,15}(?:test results|readings)", line, re.I)


def compressor_source_facts(text):
    """Bounded results only; no manufacturer limits or thermodynamic inference."""
    lines = source_lines(text)
    actual = [s for s in lines if _actual(s)]
    direct = []
    for index, s in enumerate(actual):
        ground_result = bool(re.search(r"short(?:ed)? to ground|terminal.to.ground (?:short|failure)|"
                                       r"failed insulation.{0,15}(?:ground )?test|continuity to ground (?:was )?(?:documented|measured|found)", s, re.I))
        context = " ".join(actual[max(0, index - 1):index + 1])
        isolated = bool(re.search(r"compressor (?:leads|wires).{0,15}(?:isolated|disconnected)|isolated compressor|directly at (?:the )?compressor terminals", context, re.I))
        # A documented test directly of the windings is distinct from a bare claim
        # that the compressor is grounded (also supports legacy submitted results).
        winding_test = bool(re.search(r"compressor windings were tested", s, re.I))
        open_result = bool(re.search(r"compressor.{0,60}(?:confirmed|measured|test\w*|documented).{0,35}open winding|"
                                     r"(?:confirmed|measured|documented) open compressor winding", s, re.I))
        negated = bool(re.search(r"\b(?:no|not|without|negative|possible|unconfirmed)\b", s, re.I))
        other_component = re.search(r"blower|fan motor|inducer|contactor|control board", s, re.I)
        if not negated and not other_component and ((ground_result and (isolated or winding_test)) or open_result):
            direct.append(context if isolated and context != s and re.search(r"compressor (?:leads|wires)", context, re.I) else s)
    normal_after = [s for s in actual if re.search(r"compressor.{0,35}(?:starts?|started|runs?|ran|operat\w*).{0,25}normally", s, re.I)
                    and re.search(r"after.{0,50}(?:capacitor|contactor|starting component).{0,25}(?:replac|correct|repair)|"
                                  r"(?:capacitor|contactor|starting component).{0,25}(?:replac|correct|repair).{0,30}compressor", s, re.I)]
    ground_claim = any(re.search(r"(?:diagnosis|claim|diagnosed).{0,60}compressor.{0,25}(?:short|ground)|"
                                 r"compressor (?:is |was )?(?:diagnosed as )?shorted to ground", s, re.I) for s in lines)
    negative_ground = [s for s in actual if re.search(r"isolated.{0,50}(?:terminal|compressor).{0,40}(?:no ground fault|no continuity to ground)|"
                        r"compressor.{0,25}isolated.{0,50}(?:no ground fault|no continuity to ground)", s, re.I)]
    proposed = any(re.search(r"replace (?:the |failed )?compressor|compressor replacement|compressor repair", s, re.I) for s in lines)
    conflicts = normal_after if proposed else []
    if ground_claim and negative_ground and not any(re.search(r"open winding", d, re.I) for d in direct):
        conflicts += negative_ground
    observations = [s for s in actual if re.search(r"compressor", s, re.I) and re.search(
        r"hums?|overload|intermittent|voltage|\bamps?\b|amperage|winding|terminal|starting|start|ground test|insulation", s, re.I)]
    if len(re.findall(r"(?m)^\s*QUOTE \d+", text)) > 1:
        # No cross-quote join of an isolated finding and proposed work.
        return None
    if conflicts:
        status, scope = "CONTRADICTORY", "UNSUPPORTED"
        capacitor_results = [s for s in actual if re.search(r"capacitor.*rated.*measured", s, re.I)] if normal_after else []
        evidence, gaps = capacitor_results + conflicts, []
    elif direct and (proposed or compressor_required(text)):
        status, scope, evidence, gaps = "CONFIRMED", "APPROPRIATE", direct, []
    elif observations:
        status, scope, evidence = "INCOMPLETE", "PARTIALLY_DEFINED", observations
        gaps = ["The submitted compressor observations do not yet separate compressor failure from an unresolved starting, input or control issue."]
    else:
        status, scope, evidence = "ABSENT", "UNSUPPORTED", []
        gaps = ["The quote does not document compressor-failure evidence supporting the proposed compressor work."]
    return dict(subject=COMPRESSOR_SUBJECT, materiality="PRIMARY", diagnostic_evidence_status=status,
                scope_support=scope, documented_evidence=list(dict.fromkeys(evidence)),
                material_gaps=gaps, contradictions=list(dict.fromkeys(conflicts)))


def normalize_compressor_assessments(analysis, text, assessment_type, classification=None):
    items = compressor_items(analysis)
    if not items and not compressor_required(text, classification):
        return
    facts = compressor_source_facts(text) if text.strip() else None
    if not items:
        if facts is None:
            facts = dict(subject=COMPRESSOR_SUBJECT, materiality="PRIMARY", diagnostic_evidence_status="INCOMPLETE",
                         scope_support="PARTIALLY_DEFINED", documented_evidence=[], contradictions=[],
                         material_gaps=["Compressor findings must be tied to the specific proposed work."])
        analysis.technical_assessments.append(assessment_type(**facts))
        return
    # Only this domain changes. Preserve real structured conflicts or richer
    # manufacturer/mechanical evidence outside the bounded source parser.
    for a in items:
        updates = dict(subject=COMPRESSOR_SUBJECT, materiality="PRIMARY")
        if facts and facts["diagnostic_evidence_status"] == "CONTRADICTORY":
            updates.update(facts)
        elif facts and facts["diagnostic_evidence_status"] == "CONFIRMED" and not a.contradictions:
            updates.update(facts)
        elif facts and not a.contradictions and (not a.documented_evidence or all(
                re.fullmatch(r"(?:compressor bad|compressor failed|fault code|age|no cooling|failed capacitor|ground test|winding test)[ .]*", e, re.I)
                for e in a.documented_evidence)):
            updates.update(facts)
        analysis.technical_assessments = [x.model_copy(update=updates) if x is a else x for x in analysis.technical_assessments]


def compressor_question(value):
    if not re.search(r"compressor", value, re.I):
        return False
    if re.search(r"price|pricing|cost|itemiz|breakdown|warranty|coverage|evacuat|refrigerant|startup|commission|whole.system|full.system|complete system|entire system|repair.vs|after (?:the )?(?:repair|replacement)", value, re.I):
        return False
    return bool(re.search(r"fail|fault|damag|evidence|diagnos|ground|winding|isolat|start|input|power|replace|replacement|test|new compressor|\bwhy\b", value, re.I))


def _unresolved(a):
    return a.diagnostic_evidence_status not in {"CONFIRMED", "ADEQUATE"} or a.scope_support != "APPROPRIATE"


def _bad(a):
    return a.diagnostic_evidence_status in {"CONTRADICTORY", "ABSENT"} or a.scope_support == "UNSUPPORTED"


def partial_voltage_drop(analysis):
    items = compressor_items(analysis)
    evidence = " ".join(s for a in items for s in a.documented_evidence)
    return bool(items and any(_unresolved(a) for a in items) and not any(_bad(a) for a in items)
                and re.search(r"voltage.{0,20}drops?|drop.{0,20}voltage", evidence, re.I)
                and re.search(r"not (?:yet )?(?:been )?isolated|not separated|unresolved", evidence, re.I))


def compressor_normal_after_start_repair(analysis):
    return any(a.diagnostic_evidence_status == "CONTRADICTORY"
               and any(re.search(r"normally", s, re.I) and re.search(r"capacitor|starting.component", s, re.I)
                       for s in a.contradictions) for a in compressor_items(analysis))


def customer_compressor_text(value):
    replacements = {"INCOMPLETE": "not yet established", "ADEQUATE": "sufficient",
                    "CONFIRMED": "confirmed", "PARTIALLY_DEFINED": "not fully described",
                    "APPROPRIATE": "appropriate"}
    return re.sub(r"\b(?:INCOMPLETE|ADEQUATE|CONFIRMED|PARTIALLY_DEFINED|APPROPRIATE)\b",
                  lambda m: replacements[m.group()], value)


def compressor_conclusion(items):
    if any(_bad(a) for a in items):
        if any(re.search(r"normally", c, re.I) for a in items for c in a.contradictions):
            return "The quote recommends compressor replacement even though the submitted findings show normal compressor operation after the failed starting component was replaced."
        return "The submitted findings do not establish the claimed compressor failure or support the proposed compressor work."
    if any(_unresolved(a) for a in items):
        return "The compressor observations need further checking to separate compressor failure from the unresolved starting or input issue."
    return "The documented compressor-failure findings support the proposed compressor work."


def compressor_paragraphs(analysis):
    items = compressor_items(analysis)
    if not items:
        return []
    evidence = list(dict.fromkeys(s for a in items for s in a.documented_evidence))
    if compressor_normal_after_start_repair(analysis):
        return [" ".join(customer_compressor_text(s) for s in evidence)
                + " The submitted findings support the failed starting capacitor, but they do not show that the compressor itself still needs replacement."]
    if partial_voltage_drop(analysis):
        return [" ".join(customer_compressor_text(s) for s in evidence)
                + " Those observations justify further checking, but they do not yet confirm that the compressor itself is the failed component."]
    return [customer_compressor_text(s) for s in [compressor_conclusion(items), *evidence]]


def finalize_compressor_fields(analysis, source_text=""):
    items = compressor_items(analysis)
    if not items:
        return
    for name in ("equipment_analysis", "missing_information"):
        if name == "equipment_analysis" and any(re.search(r"replacement basis|basis for|repair.vs", a.subject, re.I)
                                                 for a in analysis.technical_assessments if a not in items):
            continue  # Replacement-basis synthesis may legitimately cite the diagnosis.
        sentences = re.split(r"(?<=[.!?])\s+", getattr(analysis, name))
        setattr(analysis, name, " ".join(s for s in sentences if not compressor_question(s)))
    for name in ("red_flags", "good_signs", "contractor_questions"):
        setattr(analysis, name, [s for s in getattr(analysis, name) if not compressor_question(s)])
    for name in ("required_actions", "verdict_reasons"):
        setattr(analysis.decision, name, [s for s in getattr(analysis.decision, name) if not compressor_question(s)])
    unresolved = any(_unresolved(a) for a in items)
    conclusion = compressor_conclusion(items)
    if unresolved:
        if re.match(r"No .*missing|No (?:material|important|critical)", analysis.missing_information, re.I):
            analysis.missing_information = ""
        analysis.missing_information = (analysis.missing_information + " " + conclusion).strip()
        analysis.decision.required_actions.insert(0, "Ask for evidence supporting the compressor diagnosis before approving the compressor work.")
        analysis.decision.verdict_reasons.append(conclusion)
        analysis.contractor_questions.append(
            "What evidence shows the compressor itself has failed and still needs replacement?" if any(_bad(a) for a in items) else
            "What testing separates a compressor failure from the starting-component or power issue?")
        if any(_bad(a) for a in items):
            analysis.red_flags.append(conclusion)
    else:
        evidence = list(dict.fromkeys(s for a in items for s in a.documented_evidence))
        analysis.good_signs.extend(evidence[:1])
        if re.search(r"ensur\w*|guarantee\w*", analysis.installation_concerns, re.I):
            scope = [s for s in source_lines(source_text) if re.match(r"(?:repair )?scope includes", s, re.I)
                     and not re.search(r"ensur\w*|guarantee\w*", s, re.I)]
            if scope:
                independent = [s for s in re.split(r"(?<=[.!?])\s+", analysis.installation_concerns)
                               if not re.search(r"ensur\w*|guarantee\w*", s, re.I)]
                analysis.installation_concerns = " ".join([*independent, *scope])
    if partial_voltage_drop(analysis):
        analysis.contractor_questions = [q for q in analysis.contractor_questions if not compressor_question(q)]
        analysis.contractor_questions.insert(0, "What testing will identify whether the voltage drop is caused by the compressor, a starting component, or the incoming power supply?")
        others = [a for a in analysis.technical_assessments if a not in items and a.materiality != "MINOR"]
        if not any(_unresolved(a) for a in others):
            analysis.equipment_analysis = (
                "The compressor is having trouble starting, and a voltage drop was recorded during the start attempt. "
                "The quote does not show whether that drop is caused by the compressor itself, a starting component, "
                "or the incoming electrical supply. That needs to be isolated before compressor replacement is confirmed.")
            analysis.missing_information = "The quote does not show what is causing the voltage drop during the compressor start attempt."
    if compressor_normal_after_start_repair(analysis):
        others = [a for a in analysis.technical_assessments if a not in items and a.materiality != "MINOR"]
        if not any(_unresolved(a) for a in others):
            analysis.missing_information = "The quote does not show a compressor-specific failure finding after the failed starting capacitor was replaced."


def compose_compressor_summary(analysis):
    items = compressor_items(analysis)
    if not items:
        return
    others = [a for a in analysis.technical_assessments if a not in items and a.materiality != "MINOR"]
    # Other domains own their own conclusions (especially whole-system economics).
    if any(_unresolved(a) or re.search(r"replacement basis|basis for|repair.vs", a.subject, re.I) for a in others):
        return
    conclusion = compressor_conclusion(items)
    if any(_unresolved(a) for a in items):
        analysis.banner_explanation = conclusion
        analysis.homeowner_takeaway = conclusion + " Ask the contractor what evidence supports the compressor work before approving it."
        analysis.bottom_line = ("Get a second opinion on the compressor work before approving it." if analysis.decision.verdict == "GET_A_SECOND_OPINION" else
                                "Have the contractor clarify the compressor findings before approving the proposed work.")
        if partial_voltage_drop(analysis):
            analysis.banner_explanation = "The source of the compressor's startup voltage drop has not been isolated."
            analysis.homeowner_takeaway = "The compressor is showing a starting problem, but the cause of the voltage drop has not been isolated yet. Have that checked before approving compressor replacement."
            analysis.bottom_line = "Have the contractor isolate the cause of the startup voltage drop before approving compressor replacement."
        if compressor_normal_after_start_repair(analysis):
            analysis.homeowner_takeaway = "The documented starting-component problem was corrected and the compressor then operated normally. Before approving compressor replacement, ask what separate evidence shows the compressor itself has failed."
            analysis.bottom_line = "The submitted findings support the failed starting capacitor, not compressor replacement. Have the contractor show compressor-specific failure evidence or get a second opinion before approving the repair."
    elif analysis.decision.verdict == "PROCEED":
        analysis.homeowner_takeaway = conclusion
        analysis.bottom_line = "The documented compressor failure supports moving forward with the proposed compressor work."
