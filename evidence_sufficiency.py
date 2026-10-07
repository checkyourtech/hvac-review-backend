"""Source-presence safeguards for explicit work/price statements, not quote length.

Unknown content always abstains: it may contain evidence or project intent that
belongs to a domain module. This does not derive technical support or verdicts.
"""
import re
from dataclasses import dataclass
from pricing import lump_sum_price_review


EVIDENCE_SUFFICIENCY_RULES = """
A named repair is not evidence that it is needed. For material proposed work with
no diagnostic justification, supply the owning PRIMARY technical assessment with
ABSENT evidence and UNSUPPORTED scope. Mere omission is not a contradiction.
Do not infer tests, results, equipment identity, compatibility, intent, warranty,
startup procedures or manufacturer requirements from the repair name or price.
Strong direct evidence can support a very short quote; never use document length.
For system replacement, the reason for replacement belongs to replacement basis,
unless an elective intent is actually documented. Prioritize that question over a
checklist of omitted domains. A sole repair total gives LIMITED pricing transparency;
ask for an itemized breakdown of parts, labor and other major charges, not for profit,
markup, hourly rates, wholesale invoices or every incidental fitting or material.
Keep technical evidence separate from pricing and market comparisons.
"""

OWNERS = {
    "control board": ("electrical_controls", "Electrical control evidence: control board",
                      "What input and output testing showed that the control board itself has failed?"),
    "capacitor": ("electrical_controls", "Electrical control evidence: capacitor",
                  "What capacitance did the capacitor measure, and what tolerance is listed on it?"),
    "pressure switch": ("electrical_controls", "Electrical control evidence: pressure switch",
                        "What testing showed that the pressure switch itself failed rather than another condition keeping it open?"),
    "igniter": ("electrical_controls", "Electrical control evidence: igniter",
                "What test showed that the igniter itself has failed?"),
    "flame sensor": ("electrical_controls", "Electrical control evidence: flame sensor",
                    "What flame-signal reading or other test showed that the flame sensor needs replacement?"),
    "compressor": ("compressor", "Claimed compressor failure",
                   "What test results show that the compressor has failed and needs replacement?"),
    "blower motor": ("motors", "Claimed blower motor failure",
                     "What findings show that the blower motor itself has failed and needs replacement?"),
    "evaporator coil": ("refrigerant_system", "Coil refrigerant-circuit condition",
                        "What evidence shows that the evaporator coil is leaking or has otherwise failed and needs replacement?"),
    "complete HVAC system": ("repair_vs_replace", "Basis for full-system replacement",
                             "What condition or failure is the reason for replacing the existing HVAC system?"),
}


@dataclass(frozen=True)
class ProposedWork:
    component: str
    amount: str
    remaining_content: tuple

    @property
    def scope_only(self):
        return not self.remaining_content

    @property
    def module(self):
        return OWNERS[self.component][0]

    @property
    def subject(self):
        return OWNERS[self.component][1]

    @property
    def question(self):
        return OWNERS[self.component][2]


def proposed_work_facts(text):
    """Recognize explicit scope and a single total; retain all other source content.

    No character/word/sentence-count thresholds. A single unknown clause prevents
    the no-evidence inference, however long or short the document is.
    """
    if len(re.findall(r"(?m)^\s*QUOTE \d+", text)) > 1:
        return None
    components, amounts, remaining = [], [], []
    for line in text.splitlines():
        s = line.strip(" \t-•")
        if not s or re.match(r"^(?:File Name:|Local Path:|QUOTE \d+)", s, re.I):
            continue
        if re.fullmatch(r"(?:ARTIFICIAL )?SAMPLE(?: HVAC)? (?:QUOTE|PROPOSAL)(?: — NOT FOR REAL WORK)?[.]?", s, re.I):
            continue
        money = re.findall(r"\$\s*\d[\d,]*(?:\.\d{2})?", s)
        amounts.extend(m.replace(" ", "") for m in money)
        s = re.sub(r"\$\s*\d[\d,]*(?:\.\d{2})?", "", s).strip(" \t—–-:;.•")
        s = re.sub(r"^(?:proposed work|proposed repair|recommended repair|scope):\s*", "", s, flags=re.I)
        components_pattern = r"compressor|blower motor|evaporator coil|(?:complete )?HVAC system|control board|capacitor|pressure switch|igniter|flame sensor"
        work = re.fullmatch(r"(?:replace (?:the |failed )?(" + components_pattern + r")|(" + components_pattern + r") replacement)", s, re.I)
        if work:
            component = (work.group(1) or work.group(2)).lower()
            components.append("complete HVAC system" if "hvac system" in component else component)
        elif not s or re.fullmatch(r"(?:total|total (?:repair )?price|price|quoted total)", s, re.I):
            continue
        elif re.fullmatch(r"No (?:diagnostic (?:findings|evidence)|test results|testing) (?:provided|documented)", s, re.I):
            continue
        else:
            remaining.append(line.strip())
    if len(set(components)) != 1 or len(set(amounts)) != 1:
        return None
    return ProposedWork(components[0], amounts[0], tuple(remaining))


def discard_ungrounded_scope_only_claims(analysis, facts, direct_source_evidence=()):
    """Validate AI provenance before domain recovery on the finalized copy only.

    If every source clause is scope/price (or explicitly absent testing), there
    cannot be a source-grounded test result, equipment specification or intent in
    the AI output. Domain normalizers then recover only what the source supports.
    Unknown source content never enters this guard.
    """
    complete_direct_record = bool(facts and direct_source_evidence and all(
        line in " ".join(direct_source_evidence) for line in facts.remaining_content))
    if not facts or not (facts.scope_only or complete_direct_record):
        return
    analysis.technical_assessments = []
    analysis.replacement_context = type(analysis.replacement_context)("unknown")
    for name in ("required_actions", "optional_suggestions", "verdict_reasons"):
        setattr(analysis.decision, name, [])
    for name in ("good_signs", "red_flags", "contractor_questions"):
        setattr(analysis, name, [])
    for name in ("project_overview", "equipment_analysis", "missing_information", "installation_concerns",
                 "pricing_review", "quote_comparison", "best_quote_recommendation", "contractor_vetting"):
        setattr(analysis, name, "")
    if complete_direct_record:
        analysis.project_overview = f"The quote proposes replacing the {facts.component} for {facts.amount}."
        analysis.equipment_analysis = " ".join(direct_source_evidence)
        analysis.installation_concerns = f"The listed work is {facts.component} replacement."


def ensure_primary_evidence_assessment(analysis, facts, assessment_type):
    """Last-resort completeness, after canonical domain recovery; never override it."""
    if not facts or not facts.scope_only:
        return
    if any(a.subject == facts.subject and a.materiality == "PRIMARY" for a in analysis.technical_assessments):
        return
    analysis.technical_assessments.append(assessment_type(
        subject=facts.subject, materiality="PRIMARY", diagnostic_evidence_status="ABSENT",
        scope_support="UNSUPPORTED", documented_evidence=[], contradictions=[],
        material_gaps=[f"The quote does not explain why the {facts.component} needs replacement."],
    ))


def prepare_scope_only_customer_fields(analysis, facts):
    """Prioritize the owning unsupported proposition, not duplicate absence lists."""
    if not facts or not facts.scope_only:
        return
    primary = next((a for a in analysis.technical_assessments if a.subject == facts.subject), None)
    if primary is None or primary.diagnostic_evidence_status != "ABSENT":
        return
    component = facts.component
    analysis.project_overview = f"The quote proposes replacing the {component} for {facts.amount}."
    analysis.equipment_analysis = f"The quote names the {component} replacement, but does not include findings showing why it is needed."
    analysis.missing_information = f"The quote does not show what findings support replacing the {component}."
    analysis.installation_concerns = f"The listed work is {component} replacement; further repair or installation steps are not described."
    analysis.pricing_review = lump_sum_price_review(facts.amount)
    analysis.red_flags = [f"Replacement of the {component} is recommended without documented findings showing why it is needed."]
    analysis.good_signs = []
    analysis.decision.required_actions = [f"Ask for the findings supporting {component} replacement before approving the work."]
    analysis.decision.verdict_reasons = [analysis.red_flags[0]]
    analysis.contractor_questions = [facts.question]


def scope_only_questions(analysis, facts):
    if not facts or not facts.scope_only:
        return None
    primary = next((a for a in analysis.technical_assessments if a.subject == facts.subject), None)
    if primary is None or primary.diagnostic_evidence_status != "ABSENT":
        return None
    questions = [facts.question]
    if analysis.decision.pricing_transparency in {"LIMITED", "ABSENT"}:
        questions.append(f"What is included in the {facts.amount} total?")
    return questions


def summarize_scope_only(analysis, facts):
    """Final prose guard, using the existing canonical decision, never deriving one."""
    if not facts or not facts.scope_only or analysis.decision.verdict != "GET_A_SECOND_OPINION":
        return
    analysis.banner_explanation = f"The quote does not show why the {facts.component} needs replacement."
    analysis.homeowner_takeaway = (f"The quote tells you what the contractor wants to replace, but it does not show "
                                  f"why the {facts.component} needs to be replaced.")
    analysis.bottom_line = (f"The quote does not provide enough evidence to support {facts.component} replacement. "
                            "Ask for the diagnostic findings or get a second opinion before approving the work.")
