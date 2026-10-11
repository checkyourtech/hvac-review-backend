"""Shared customer evidence standard; never derives support, verdicts or pricing.

Domain modules supply evidence examples and ownership. Examples describe what a
contractor could document, never tests the homeowner should perform or a universal
checklist. Assessments remain the authority for the already-established result.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from pydantic import BaseModel, ConfigDict


REPAIR_EVIDENCE_HEADING = "What Evidence Should Support This Diagnosis?"


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
    normalized_source = " ".join(source_text.casefold().split())
    evidence = tuple(dict.fromkeys(e.strip() for e in item.documented_evidence if e.strip()
        and not re.search(r"\b(?:you|homeowner)\b.*\b(?:test|measure|probe|open|remove|check)\b|"
                          r"^(?:test|measure|probe|check)\b|\b(?:will|should|would)\s+(?:be\s+)?(?:test|measure|probe|check|perform|inspect)", e, re.I)
        and (not source_text.strip() or " ".join(e.casefold().split()) in normalized_source)))
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
