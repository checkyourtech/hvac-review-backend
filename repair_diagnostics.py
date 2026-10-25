"""Shared customer evidence standard; never derives support, verdicts or pricing.

Domain modules supply evidence examples and ownership. Examples describe what a
contractor could document, never tests the homeowner should perform or a universal
checklist. Assessments remain the authority for the already-established result.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from pydantic import BaseModel, ConfigDict
from evidence_integrity import SourceEvidence, assessment_owner, material_work, repair_owners


REPAIR_EVIDENCE_HEADING = "What Evidence Should Support This Diagnosis?"


def _blower_non_finding(value):
    """Recognize clear non-findings, not a closed list of valid motor tests.

    Unknown observations stay with the existing structured assessment. In
    particular, a nonnumeric inspection result is not absence of evidence.
    """
    parts = re.split(r"[;,]\s+|(?<=[.!?])\s+|\s+but\s+", value.strip())
    if len(parts) > 1:
        return all(_blower_non_finding(part) for part in parts)
    text = value.strip(" .;:-\t").lower()
    text = re.sub(r"^(?:diagnosis|customer complaint|technician findings):\s*", "", text)
    text = re.sub(r"^(?:the )?(?:contractor|technician|customer)\s+(?:states?|stated|says?|reports?|reported|diagnosed)\s+(?:that\s+)?", "", text)
    motor = r"(?:(?:indoor|ecm|psc)\s+)*(?:blower(?:[- ]motor)?|motor|fan)(?:/module)?|ecm module"
    failure = r"bad|failed|faulty|defective|inoperative|nonfunctional"
    if re.fullmatch(r"(?:the |a |an )?(?:(?:" + failure + r")\s+(?:" + motor + r")|"
                    r"(?:" + motor + r")\s+(?:(?:is|was|has|been|found|to|be)\s+)*(?:" + failure + r"))", text):
        return True
    if re.fullmatch(r"(?:the )?(?:" + motor + r"|system)\s+(?:(?:is|was|does|did)\s+)?"
                    r"(?:not (?:run|start|operate|running|working|operating|cooling(?: properly)?|moving air)|"
                    r"(?:doesn't|won't|will not) (?:run|start|operate)|stopped)|"
                    r"no (?:airflow|air flow|cooling)(?: from (?:the )?vents)?", text):
        return True
    if re.fullmatch(r"(?:the )?(?:" + motor + r")\s+(?:(?:diagnostic )?(?:measurements|readings|test results|testing|tests)\s+)?"
                    r"(?:(?:is|are|was|were|has|have|been)\s+)*(?:not|never)\s+(?:been\s+)?"
                    r"(?:tested|measured|provided|documented|recorded|performed)|"
                    r"no\b.*\b(?:tests?|testing|measurements?|readings?|findings|evidence|results)\b"
                    r"(?:\s+(?:are|were|was|is))?\s+(?:provided|documented|listed|performed|recorded|reported|included)", text):
        # 'No voltage measured' is a result; 'no measurements documented' is not.
        return True
    if re.match(r"^(?:(?:the )?(?:contractor|technician|we)\s+)?(?:will|should|would|plan to|plans to)\b", text):
        return True
    if re.search(r"\b(?:test|testing|tests|measurement|measurements)\b.*\b(?:is|are) planned$|"
                 r"\bwill be (?:tested|measured|checked|verified)$", text):
        return True
    if re.fullmatch(r"(?:the )?(?:" + motor + r")(?: replacement)?\s+(?:(?:is|was)\s+)?"
                    r"(?:recommended|proposed|planned|required|needs replacement|requires replacement)", text):
        return True
    if (re.match(r"^(?:replace|replacement|replacing|remove|install|reassemble|test|verify|check|confirm|inspect|configure|recommend(?:ed|s)?)\b", text)
            and not re.search(r"\b(?:because|measured|tested|found|observed|recorded|showed)\b|"
                              r"\b(?:is|was) (?:present|normal|absent)\b", text)):
        return True
    parts = re.split(r"\s+and\s+", text)
    if len(parts) > 1:
        return all(_blower_non_finding(part) for part in parts)
    return False


def normalize_blower_evidence(analysis, source_text):
    """Reject assertion-only PRIMARY motor evidence before support derivation.

    Empty evidence after provenance rejection remains a validation gap; this
    guard only knows that evidence is absent when the retained statements are
    recognizably symptoms, assertions or planned/missing tests. Never promote
    unknown evidence or recalibrate another motor/component owner.
    """
    if (not source_text.strip() or len(re.findall(r"(?m)^\s*QUOTE \d+", source_text)) > 1
            or "blower motor" not in material_work(source_text)):
        return
    for item in analysis.technical_assessments:
        if (item.materiality != "PRIMARY" or assessment_owner(item.subject) != "blower motor"
                or item.contradictions or item.diagnostic_evidence_status == "CONTRADICTORY"):
            continue
        if item.documented_evidence and all(_blower_non_finding(e) for e in item.documented_evidence):
            item.diagnostic_evidence_status = "ABSENT"
            item.scope_support = "UNSUPPORTED"
            item.documented_evidence = []
            item.material_gaps = ["The quote does not document findings that establish blower motor failure."]


def normalize_blower_customer_fields(analysis, source_text):
    """Keep missing part numbers out of major flags; attribute unsupported work."""
    if not source_text.strip() or len(re.findall(r"(?m)^\s*QUOTE \d+", source_text)) > 1:
        return
    items = [a for a in analysis.technical_assessments
             if a.materiality == "PRIMARY" and assessment_owner(a.subject) == "blower motor"]
    if not items:
        return

    def omission_only(flag):
        owners = repair_owners(flag)
        motor = owners == {"blower motor"} or (not owners and re.search(r"\breplacement motor\b", flag, re.I))
        return (motor and re.search(r"model|part number|compatib|specification", flag, re.I)
                and re.search(r"\b(?:missing|omitted|unspecified|unclear|lack\w*)\b|"
                              r"not (?:provided|given|listed|documented|specified)|"
                              r"\bno\b.*\b(?:details|information|documentation|specifications?|model number|part number)\b|"
                              r"\bno\b.*\bmodel\b.*\b(?:provided|given|listed|documented|specified)\b", flag, re.I)
                and not re.search(r"incompatib|mismatch|conflict|incorrect|wrong|does not (?:fit|match)|not compatible", flag, re.I))

    # Preserve explicit conflicts, including their source-backed assessments.
    # 'No compatibility' can describe an actual conflict; only missing details
    # are omissions. An omission alone neither establishes nor overrides one.
    analysis.red_flags = [flag for flag in analysis.red_flags if not omission_only(flag)]
    if all(a.diagnostic_evidence_status == "ABSENT" and a.scope_support == "UNSUPPORTED" for a in items):
        for field in ("project_overview", "equipment_analysis"):
            value = re.sub(r"(?<!reportedly )\b(?:bad|failed|faulty|defective)(?=\s+(?:(?:ECM|PSC)\s+)?blower(?:[- ]motor)?\b)",
                           "reportedly failed", getattr(analysis, field), flags=re.I)
            value = re.sub(r"\b((?:(?:ECM|PSC)\s+)?blower(?:[- ]motor)?)\s+(?:is|has)\s+(?:bad|failed|faulty|defective)\b",
                           r"\1 is reported as failed by the contractor", value, flags=re.I)
            setattr(analysis, field, value)
        if material_work(source_text) == {"blower motor"}:
            analysis.project_overview = ("The contractor recommends replacing the blower motor, but the quote "
                                         "does not document findings that establish motor failure.")


def normalize_blower_report_details(analysis, source_text):
    """Recover documented positives and omit already-addressed generic questions.

    Planned operation testing is favorable repair scope, not diagnostic proof.
    This pass never writes assessments, support, pricing or verdicts.
    """
    if (not source_text.strip() or len(re.findall(r"(?m)^\s*QUOTE \d+", source_text)) > 1
            or material_work(source_text) != {"blower motor"}
            or not any(a.materiality == "PRIMARY" and assessment_owner(a.subject) == "blower motor"
                       for a in analysis.technical_assessments)):
        return
    source = SourceEvidence(source_text)
    affirmative = [line for line in source.lines if not re.search(
        r"\b(?:no|not|without|excluded|omitted|declined|optional|may|might|if|pending|expired)\b|subject to", line, re.I)]
    warranty_pattern = (r"\b(?:\d+|one|two|three|five|ten)[- ](?:year|month|day)s?\s+"
                        r"(?:parts(?:\s+(?:and|&)\s+labor)?|labor)\s+warranty\b")
    warranties = [match.group(0) for line in affirmative
                  for match in re.finditer(warranty_pattern, line, re.I)]
    warranty_summary = "The proposal includes " + " and ".join("a " + term for term in dict.fromkeys(warranties)) + "."
    operation_testing_included = any(re.search(
        r"\b(?:test|verify|check|confirm)\b.{0,35}\b(?:system|blower)\b.{0,25}\boperat\w*", line, re.I)
        for line in affirmative)
    operation_testing_excluded = any(
        re.search(r"\b(?:system|blower)\b", line, re.I) and re.search(r"operat\w*", line, re.I)
        and re.search(r"test\w*|verif\w*|check\w*", line, re.I)
        and re.search(r"excluded|omitted|declined|not (?:included|planned|provided|performed)", line, re.I)
        for line in source.lines)

    step_patterns = {
        "motor removal": r"\bremov(?:e|ing|al)\b.*\b(?:blower|motor)\b",
        "motor installation": r"\binstall(?:ing|ation)?\b.*\b(?:blower|motor)\b",
        "blower reassembly": r"\breassembl(?:e|ing|y)\b",
        "planned system operation testing": (
            r"\b(?:test\w*|verif\w*|check\w*)\b.*\boperat\w*|"
            r"\boperat\w*\b.*\b(?:test\w*|verif\w*|check\w*)\b"),
    }
    steps = [name for name, pattern in step_patterns.items()
             if any(re.search(pattern, line, re.I) for line in affirmative)
             and not any(re.search(pattern, line, re.I) and re.search(
                 r"\b(?:no|not|without|excluded|omitted|declined|optional|pending)\b", line, re.I)
                 for line in source.lines)]
    if not operation_testing_included or operation_testing_excluded:
        steps = [step for step in steps if step != "planned system operation testing"]
    scope_list = ", ".join(steps[:-1]) + " and " + steps[-1] if len(steps) > 1 else "".join(steps)
    scope_summary = ("The quoted repair scope includes " + scope_list + "."
                     if steps else "The proposal specifies blower motor replacement.")

    def scope_facts(text):
        facts = {name for name, pattern in step_patterns.items() if re.search(pattern, text, re.I)}
        if (re.search(r"\b(?:scope|work|repair|system|blower)\b", text, re.I)
                and re.search(r"\b(?:test\w*|verif\w*|check\w*)\b", text, re.I)
                and re.search(r"after (?:the )?(?:installation|repair|replacement)|post.repair", text, re.I)):
            facts.add("planned system operation testing")
        return facts

    def positive_key(sign):
        """Deduplicate known facts, retaining unrelated or more specialized credit."""
        text = sign.casefold().replace("-", " ")
        # These details can be independent of the routine scope/warranty facts.
        if re.search(r"airflow|speed|static pressure|\bcfm\b|safety|permit|wiring|rotation|"
                     r"vibration|commission|manufacturer|transferab|exclusion|deductible|diagnos", text):
            return None
        if "warranty" in text:
            if re.search(r"reassembl|remov|install|operat", text):
                return None  # A combined positive must not lose its scope detail.
            warranty_words = set((
                "the a an and of for to by with on both which quote quoted proposal repair "
                "includes include included provides provided has is are covered covers coverage "
                "warranty parts labor year years month months day days "
                "one two three five ten positive sign consumer protection"
            ).split())
            if set(re.findall(r"\b[a-z]+\b", text)) - warranty_words:
                return None
            numbers = {"one": "1", "two": "2", "three": "3", "five": "5", "ten": "10"}
            durations = {(numbers.get(n, n), unit) for n, unit in re.findall(
                r"\b(\d+|one|two|three|five|ten)\s+(year|month|day)s?\b", text)}
            coverage = tuple(word for word in ("parts", "labor") if re.search(r"\b" + word + r"\b", text))
            if len(durations) == 1 and coverage:
                return ("warranty", tuple(sorted(durations)), coverage)
            return None
        if re.search(r"\b(?:measured|completed|confirmed|failed|failure)\b", text):
            return None  # Actual findings are distinct from planned work.
        # Unknown details may carry independent credit (e.g. mounting hardware).
        # Only deduplicate paraphrases made entirely of these routine scope terms.
        scope_words = set((
            "the a an and of for to by with on along after afterward which are is be will has as "
            "quote quoted proposal proposed scope work repair replacement replacing "
            "replaces replace includes include included including specifies specified "
            "provides provided essential important necessary steps procedures clear involves good practice "
            "detailing documented accompanies outlines one "
            "removal remove removing existing old new installation install installing "
            "reassembly reassemble reassembling blower motor ecm section system "
            "planned post testing tests test checking checks check verification "
            "verify verifying operational operation operating properly proper "
            "function functionality ensuring ensure"
        ).split())
        if set(re.findall(r"\b[a-z]+\b", text)) - scope_words:
            return None
        facts = scope_facts(text)
        if re.search(r"\bscope\b.*\bessential (?:steps|procedures)\b", text) or (not facts and re.search(r"\bscope\b", text)):
            facts = set(steps)
        return ("scope", tuple(sorted(facts))) if facts else None

    def positive_parts(sign):
        # Separate routine scope and warranty ownership before deduplication.
        # Unknown extra facts remain untouched; never replace their whole sentence.
        terms = re.findall(warranty_pattern, sign, re.I)
        source_keys = {positive_key(term) for term in warranties}
        remainder = re.sub(warranty_pattern, "", sign, flags=re.I)
        key = positive_key(remainder)
        if (terms and source_keys and all(positive_key(term) in source_keys for term in terms)
                and key is not None and key[0] == "scope"
                and {"blower reassembly", "planned system operation testing"}.issubset(steps)):
            return [warranty_summary, scope_summary]
        return [sign]

    signs, seen = [], set()
    for sign in (part for value in analysis.good_signs for part in positive_parts(value)):
        key = positive_key(sign)
        # Routine scope descriptions are consolidated below by ownership and
        # factual coverage, rather than keeping whichever wording arrived first.
        if sign not in signs and (key is None or key[0] == "scope" or key not in seen):
            signs.append(sign)
            if key is not None:
                seen.add(key)
    if warranties and not any(re.search(r"\bwarranty\b", sign, re.I) for sign in signs):
        signs.append(warranty_summary)
    # Reassembly plus operation verification is useful scope credit. Merely
    # proposing replacement does not justify manufacturing a favorable finding.
    if ({"blower reassembly", "planned system operation testing"}.issubset(steps)
            and not any(key[0] == "scope" and len(key[1]) > 1 for key in seen)
            and not any(re.search(r"reassembl|scope.*essential (?:steps|procedures)", sign, re.I)
                        for sign in signs)):
        signs.append(scope_summary)

    scope_signs = [(i, sign, set(key[1])) for i, sign in enumerate(signs)
                   if (key := positive_key(sign)) is not None and key[0] == "scope"
                   and set(key[1]).issubset(steps)]
    if len(scope_signs) > 1 and steps:
        covered = set().union(*(facts for _, _, facts in scope_signs))
        best = max(scope_signs, key=lambda item: len(scope_facts(item[1])))
        # If complementary descriptions each omit a documented step, combine
        # their facts using the existing source-derived scope explanation.
        chosen = best[1] if covered.issubset(scope_facts(best[1])) else scope_summary
        positions = {i for i, _, _ in scope_signs}
        signs = [chosen if i == scope_signs[0][0] else sign for i, sign in enumerate(signs)
                 if i == scope_signs[0][0] or i not in positions]
    analysis.good_signs = signs

    def diagnosis_only(sentence):
        if not re.search(r"diagnos|(?:blower|motor)\s+(?:motor\s+)?failure", sentence, re.I):
            return False
        if not re.search(r"uncertain|unconfirm|unsupported|unsubstantiated|lack\w*.*confirmation|"
                         r"not\b.*(?:confirm|document|establish|support)|"
                         r"without\b.*(?:findings|evidence|confirmation)", sentence, re.I):
            return False
        if re.search(r"\b(?:and|but|also)\b|;|\b(?:missing|omitted|excluded|unsafe|incorrect|"
                     r"damaged|loose|unsecured|required)\b|not (?:included|specified)|does not include",
                     sentence, re.I):
            return False
        # When a sentence also describes an actual procedure or scope gap,
        # preserve it in full rather than risk deleting a material warning.
        return not re.search(
            r"reassembl|remov|install|wiring|airflow|speed|rotation|vibration|safety|permit|"
            r"commission|startup|post.repair|manufacturer|compatib|scope.*(?:omit|lack|unclear)|"
            r"test\w*.*operat|operat\w*.*(?:test|verif|check)", sentence, re.I)

    independent_startup_gap = any(
        a.materiality != "MINOR" and assessment_owner(a.subject) == "commissioning"
        and (a.diagnostic_evidence_status not in {"CONFIRMED", "ADEQUATE"}
             or a.scope_support != "APPROPRIATE" or a.material_gaps or a.contradictions)
        for a in analysis.technical_assessments)

    def material_verification_fact(value):
        return (re.search(r"airflow|speed setting|rotation|control (?:function|wiring)|"
                          r"post.repair|startup|commission|operation test", value, re.I)
                and re.search(r"\brequired\b|\bmust\b|excluded|omitted|not included|"
                              r"unsafe|incorrect|conflict|mismatch|failed", value, re.I))

    material_verification_gap = independent_startup_gap or any(
        material_verification_fact(value) for value in source.lines) or any(
        material_verification_fact(value)
        for item in analysis.technical_assessments if item.materiality != "MINOR"
        for value in [*item.material_gaps, *item.contradictions])

    def post_repair_context(value):
        return re.search(r"post.repair|\bafterwards?\b|"
                         r"\b(?:after|once|when|following)\b.{0,60}"
                         r"\b(?:repair|replac\w*|install\w*|complet\w*)\b", value, re.I)

    generic_operation_words = set((
        "the a an it that however would could should be is are was not no "
        "beneficial advisable recommended necessary important essential to for of "
        "ensure ensuring confirmation confirm confirming verify verifying verified "
        "verification check checking checks test tests testing proper properly normal "
        "operation operational operates operating correctly system blower motor "
        "after afterward post repair replacement installation installed following once when "
        "please needed described documented included specified new what how will you take steps"
    ).split())

    def operation_words(value):
        value = re.sub(r"\bit['’]s\b", "it is", value.casefold())
        return set(re.findall(r"\b[a-z]+\b", value))

    def redundant_operation_followup(sentence):
        if "planned system operation testing" not in steps or independent_startup_gap:
            return False
        # Unknown or specific procedural requirements remain intact. Only a
        # generic request for the already-promised operation check is redundant.
        return (not operation_words(sentence) - generic_operation_words
                and re.search(r"test\w*|verif\w*|check\w*|confirm\w*|ensur\w*", sentence, re.I)
                and re.search(r"\boperat\w*", sentence, re.I)
                and post_repair_context(sentence))

    def unsubstantiated_verification_detail_gap(sentence):
        if ("planned system operation testing" not in steps or material_verification_gap
                or source.grounded(sentence)):
            return False
        detail_words = generic_operation_words | {
            "does", "specify", "describe", "any", "specific", "details", "procedures",
            "airflow", "or", "control", "function", "completion", "quote"}
        return (not operation_words(sentence) - detail_words
                and re.search(r"\bnot\b", sentence, re.I)
                and re.search(r"verif\w*|test\w*|check\w*", sentence, re.I)
                and re.search(r"steps|procedures|details", sentence, re.I)
                and post_repair_context(sentence))

    sentences = re.split(r"(?<=[.!?])\s+", analysis.installation_concerns.strip())
    followups = [sentence for sentence in sentences if redundant_operation_followup(sentence)
                 or unsubstantiated_verification_detail_gap(sentence)]
    retained = [sentence for sentence in sentences if sentence not in followups and not diagnosis_only(sentence)]
    if followups and retained and all(
            (key := positive_key(sentence)) is not None and key[0] == "scope"
            and set(key[1]).issubset(steps) for sentence in retained):
        retained = [scope_summary]
    if retained != sentences:
        analysis.installation_concerns = " ".join(retained).strip() or scope_summary

    # A specific source/assessment-backed omission may justify a contractor
    # follow-up even though generic operation testing is already in the scope.
    if "planned system operation testing" not in steps or material_verification_gap:
        return

    def generic_operation_question(question):
        if redundant_operation_followup(question):
            return True
        # Generic operation testing does not answer a specific, material test
        # requirement such as airflow setup or a safety/commissioning check.
        if re.search(r"airflow|speed|static pressure|\bcfm\b|safety|commission|startup|manufacturer|"
                     r"readings?|temperature|amperage|voltage|wiring|rotation|vibration|"
                     r"heating|cooling|fan.only|documentation", question, re.I):
            return False
        return (re.search(r"\b(?:test\w*|verif\w*|check\w*|confirm\w*)\b", question, re.I)
                and re.search(r"\b(?:system|blower)\b", question, re.I)
                and re.search(r"\boperat\w*", question, re.I)
                and re.search(r"after (?:the )?(?:repair|replacement|work|installation)|post.repair", question, re.I))

    analysis.contractor_questions = [q for q in analysis.contractor_questions if not generic_operation_question(q)]


class RepairEvidenceSection(BaseModel):
    """Finalizer-owned presentation snapshot, shared by API/email/HTML."""
    model_config = ConfigDict(frozen=True)
    owner: str
    title: str
    paragraphs: tuple[str, ...]


REPAIR_DIAGNOSTIC_RULES = """
SHARED REPAIR-DIAGNOSTIC EVIDENCE STANDARD
For each independently proposed repair diagnosis, describe the technician-level
evidence appropriate to that component, using its owning module's definitions.
BAD: explain the unsupported diagnosis and examples of contractor findings that
could establish it. One unsupported diagnosis is one red flag, not one per absent
test. PARTIAL: acknowledge actual documented findings, identify only the unanswered
evidence, and do not request tests already documented. GOOD: credit actual evidence;
do not add a diagnostic question. Preserve independent pricing/scope questions.
Use one strong evidence question per diagnosis; do not merge different components.
Evidence examples are alternatives where applicable, not a mandatory checklist.
Never invent measurements, nominal values, completed tests, targets or alternative
faults. Symptoms alone do not identify a failed component. A capacitor finding cannot
prove motor/compressor failure; low charge cannot prove a coil leak or failed valve.
All testing is for the contractor to document or explain. Never instruct homeowners
to perform energized electrical, refrigerant, combustion, gas, pressure or mechanical
testing. Existing domain support thresholds and verdict/pricing policies still apply.
"""


@dataclass(frozen=True)
class RepairEvidenceDefinition:
    key: str
    component: str
    expected_evidence: str
    question: str
    # Legacy owners use the shared evidence section; modern owners retain their
    # dedicated domain sections and their existing finding-count rules.
    generic_section: bool = False


@dataclass(frozen=True)
class RepairEvidenceReview:
    definition: RepairEvidenceDefinition
    documented_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    evidence_explanation: str
    contractor_question: str | None
    supported: bool
    unsupported: bool
    missing_information: str = ""


def review_repair_evidence(item, definition, source_text="", question=None):
    """Consume one owner's facts without combining or changing assessments."""
    supported = (item.diagnostic_evidence_status in {"CONFIRMED", "ADEQUATE"}
                 and item.scope_support == "APPROPRIATE")
    unsupported = item.diagnostic_evidence_status in {"ABSENT", "CONTRADICTORY"} or item.scope_support == "UNSUPPORTED"
    source = SourceEvidence(source_text)
    evidence = tuple(dict.fromkeys(e.strip() for e in item.documented_evidence if e.strip()
        and not re.search(r"\b(?:you|homeowner)\b.*\b(?:test|measure|probe|open|remove|check)\b|"
                          r"^(?:test|measure|probe|check)\b|\b(?:will|should|would)\s+(?:be\s+)?(?:test|measure|probe|check|perform|inspect)", e, re.I)
        and (not source_text.strip() or source.grounded(e))))
    explanation = ""
    if not supported:
        explanation = (f"The contractor can support the {definition.component} diagnosis with "
                       + definition.expected_evidence.rstrip(".") + ".")
    missing = ""
    if definition.generic_section and not supported:
        missing = (" ".join(item.material_gaps) if item.material_gaps else
                   "The quote does not document " + definition.expected_evidence.split(";")[0].rstrip(".") + ".")
    return RepairEvidenceReview(definition, evidence, tuple(item.material_gaps), explanation,
        None if supported else question or definition.question, supported, unsupported, missing)


def apply_repair_evidence_guidance(analysis, reviews, question_owner):
    """Last customer-text pass. No assessment, decision, action or price writes.

    Evidence explanations belong in finalized evidence sections, not appended to
    diagnosis prose. Replace owned missing-evidence text when a domain supplies a
    more precise version. Independent cause, scope and pricing purposes remain.
    """
    by_key = {}
    for review in reviews:
        current = by_key.get(review.definition.key)
        if current is None or (review.unsupported, not review.supported) > (current.unsupported, not current.supported):
            by_key[review.definition.key] = review
    if not by_key:
        return
    legacy_only = (all(r.definition.generic_section for r in by_key.values())
                   and len(reviews) == sum(a.materiality != "MINOR" for a in analysis.technical_assessments))
    if legacy_only:
        analysis.equipment_analysis = ""
        unresolved = [r for r in by_key.values() if not r.supported]
        analysis.missing_information = " ".join(
            r.missing_information
            for r in unresolved) if unresolved else "No important technical information is missing from the submitted diagnosis."
    for review in by_key.values():
        definition = review.definition
        if definition.generic_section:
            # Facts now live in the final evidence section. Keep the diagnosis
            # short instead of repeating every observation in both sections.
            if legacy_only:
                conclusion = (f"The documented findings support the {definition.component} repair." if review.supported else
                    f"The quote does not establish the claimed {definition.component} failure." if review.unsupported else
                    f"The documented observations point toward the {definition.component}, but do not yet isolate it as the failed component.")
                analysis.equipment_analysis = (analysis.equipment_analysis + " " + conclusion).strip()
            # Legacy owners have no deterministic per-diagnosis red-flag pass.
            # Replace only their own duplicate claims; unrelated issues survive.
            analysis.red_flags = [flag for flag in analysis.red_flags if question_owner(flag) != definition.key]
            if review.unsupported:
                analysis.red_flags.append(f"The quote recommends {definition.component} work without findings that establish the claimed failure.")
            if review.supported and review.documented_evidence and not any(
                    question_owner(sign) == definition.key for sign in analysis.good_signs):
                analysis.good_signs.append(review.documented_evidence[0])
        if review.missing_information:
            # Remove only this recognized owner's statements, never another
            # component's gaps or distinct scope/verification requirements.
            sentences = re.split(r"(?<=[.!?])\s+", analysis.missing_information.strip())
            kept = [s for s in sentences if s and question_owner(s) != definition.key
                    and not re.match(r"No (?:important|material|critical).*missing|No .*missing information", s, re.I)]
            analysis.missing_information = " ".join(dict.fromkeys([*kept, review.missing_information]))
        if (not review.supported and (not analysis.missing_information.strip()
                or re.match(r"No (?:important|material|critical).*missing|No .*missing information", analysis.missing_information, re.I))):
            analysis.missing_information = (
                f"The quote does not yet show the findings needed to confirm the {definition.component} diagnosis. "
                + review.contractor_question)

    questions, seen = [], set()
    for question in analysis.contractor_questions:
        key = question_owner(question)
        if key in by_key:
            if key not in seen and by_key[key].contractor_question:
                questions.append(by_key[key].contractor_question)
            seen.add(key)
        else:
            questions.append(question)
    # Add an omitted owner's diagnostic question before independent pricing.
    for key, review in by_key.items():
        if key not in seen and review.contractor_question:
            price_index = next((i for i, q in enumerate(questions) if re.search(
                r"pric|cost|itemiz|breakdown|\btotal\b", q, re.I)), len(questions))
            questions.insert(price_index, review.contractor_question)
    analysis.contractor_questions = list(dict.fromkeys(questions))


def present_isolated_repair_summary(analysis, review, generic_takeaway, generic_bottom_line):
    """Copy-only pass for an already-established single repair owner.

    Keep specialized interpretation unless it is literally covered elsewhere.
    Generic summaries add no facts beyond the existing decision/findings. Never
    infer a result or alter questions, actions, evidence or finding counts here.
    """
    name = review.definition.component
    if analysis.bottom_line == generic_bottom_line:
        analysis.bottom_line = (analysis.bottom_line
            .replace("main diagnosis or repair", f"{name} diagnosis")
            .replace("diagnosis and planned work", f"{name} diagnosis and planned work")
            .replace("remaining questions", f"remaining {name} questions"))
    elsewhere = " ".join([
        analysis.banner_explanation, analysis.equipment_analysis, analysis.bottom_line,
        *(p for section in analysis.repair_evidence_sections for p in section.paragraphs),
    ])
    normalized = " ".join(elsewhere.casefold().split())
    sentences = re.split(r"(?<=[.!?])\s+", analysis.homeowner_takeaway.strip())
    if (analysis.homeowner_takeaway == generic_takeaway or all(
            " ".join(sentence.casefold().split()) in normalized for sentence in sentences)):
        analysis.homeowner_takeaway = ""
