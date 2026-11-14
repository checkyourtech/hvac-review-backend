"""Source provenance and material-work completeness, not a new HVAC domain.

The validator is deliberately bounded. An unverified paraphrase is an internal
validation gap, not proof that a contractor failed to test a component. Source
normalizers can still recover actual results before the coverage gate is applied.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
import re
from typing import Literal

from pydantic import BaseModel, Field


class AnalysisIntegrity(BaseModel):
    status: Literal["COMPLETE", "INCOMPLETE", "LEGACY_UNVALIDATED"] = "LEGACY_UNVALIDATED"
    coverage_gaps: list[str] = Field(default_factory=list)


# Longest/specific owners first: a compressor start capacitor is a capacitor,
# not a compressor test. Whole-equipment domains may consume multiple owners.
# Scope aliases only: recognizing a proposed part never establishes its failure.
CONTROL_REPAIR_ALIASES = {
    "temperature sensor": r"(?:thermistor|temperature[- ]sensor)",
    "control board": r"(?:inverter(?:[- ]drive)?[- ](?:PCB|(?:control[- ])?board)|drive[- ](?:PCB|board))",
    "communication control": r"communicat(?:ion|ing)[- ](?:control[- ])?(?:wiring|module|controller|interface|component)s?",
}
METERING_REPAIR_ALIAS = r"(?:EEV|LEV|(?:electronic|linear)[- ]expansion[- ]valve)"
COMPONENT_REPAIR_ALIASES = {**CONTROL_REPAIR_ALIASES, "metering device": METERING_REPAIR_ALIAS}
COMPONENT_REPAIR_PATTERN = r"(?:(?:(?:indoor|outdoor)(?:[- ]unit)?|coil|room)[- ])?(?:" + "|".join(COMPONENT_REPAIR_ALIASES.values()) + r")"


def component_repair_work(text):
    """Affirmative work on the named aliases, not equipment features or test results."""
    owners = set()
    modifiers = r"(?:(?:the|a|an|new|failed|existing|(?:indoor|outdoor)(?:[- ]unit)?|coil|room|ductless|mini[- ]split|system)\s+)*"
    for clause in work_scope_clauses(text):
        for owner, pattern in COMPONENT_REPAIR_ALIASES.items():
            for match in re.finditer(r"\b(?:replace|replacing|repair(?:ing)?|install(?:ing)?|investigate|diagnose)\s+" + modifiers + pattern +
                                     r"\b|\b" + pattern + r"\s+(?:replacement|repair|diagnosis)\b", clause, re.I):
                if re.search(r"\b(?:no|not|never|declined|if needed)\b", clause[:match.start()], re.I):
                    continue
                if re.search(r"\b(?:not (?:proposed|recommended|needed)|if needed|declined)\b", clause[match.end():], re.I):
                    continue
                owners.add(owner)
    return owners


REPAIR_OWNERS = {
    "capacitor": r"(?:(?:compressor|blower(?: motor)?|fan(?: motor)?)\s+)?(?:(?:dual|run|start|starting)\s+)*capacitor",
    "blower motor": r"blower(?:[- ]motor)?|ECM(?:[- ](?:motor|module))?",
    "condenser fan motor": r"(?:condenser|outdoor)[- ]fan(?:[- ]motor)?",
    "inducer motor": r"inducer(?:[- ]motor)?",
    "transformer": r"(?:control\s+)?transformer",
    "contactor": r"contactor",
    "relay": r"relay|sequencer",
    "control board": CONTROL_REPAIR_ALIASES["control board"] + r"|(?:control|defrost|circuit|furnace)\s+board|board",
    "temperature sensor": CONTROL_REPAIR_ALIASES["temperature sensor"],
    "communication control": CONTROL_REPAIR_ALIASES["communication control"],
    "pressure switch": r"pressure[- ]switch",
    "limit switch": r"(?:high[- ]limit|limit|rollout)[- ]switch",
    "flame sensor": r"flame[- ]sensor",
    "igniter": r"ignit[eo]r",
    "heat exchanger": r"heat[- ]exchanger",
    "gas valve": r"gas[- ]valve",
    "compressor": r"compressor",
    "coil": r"(?:evaporator|condenser)\s+coil",
    "metering device": r"TXV|metering[- ]device|piston|" + METERING_REPAIR_ALIAS,
}


def repair_owners(text):
    remainder = str(text)
    result = set()
    for name, pattern in REPAIR_OWNERS.items():
        pattern = r"\b(?:" + pattern + r")\b"
        if re.search(pattern, remainder, re.I):
            result.add(name)
            remainder = re.sub(pattern, " ", remainder, flags=re.I)
    return result


def assessment_owner(subject):
    s = str(subject).lower()
    for name, pattern in (
        ("replacement basis", r"replacement basis|basis for|repair.vs|replacement decision|reason for replacement"),
        ("equipment matching", r"equipment match|matched|compatib|ahri"),
        ("system sizing", r"sizing|size selection|system capacity|capacity (?:basis|selection|justification)|load.based|load (?:calculation|assessment)|size for the home|tonnage"),
        ("duct airflow", r"duct|airflow|air distribution|static pressure"),
        ("commissioning", r"commission|startup|start.up|verification"),
    ):
        if re.search(pattern, s):
            return name
    owners = repair_owners(s)
    if len(owners) == 1:
        return next(iter(owners))
    if re.search(r"refrigerant|low charge|leak|metering|coil", s):
        return "refrigerant"
    if re.search(r"combustion|gas.heat|draft|venting", s):
        return "combustion"
    return None


@lru_cache(maxsize=8192)
def normalize_fact(value):
    s = str(value).casefold().replace("µ", "u").replace("μ", "u")
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)
    s = re.sub(r"\b\d+(?:\.\d+)?\b", lambda m: format(Decimal(m[0]).normalize(), "f"), s)
    s = re.sub(r"(\d)[ -]*(?:vac|v\b|volts?\b)", r"\1 volt", s)
    s = re.sub(r"\b(?:vac|volts?)\b", "volt", s)
    s = re.sub(r"\b(?:amps?|amperes?)\b", "amp", s)
    s = re.sub(r"\b(?:microfarads?|mfd|uf)\b", "uf", s)
    s = re.sub(r"\b(?:zero|absent|missing)\b", "zero", s)
    s = re.sub(r"\b(?:no|not present)\s+(?:secondary\s+)?output\b", "secondary output zero", s)
    s = re.sub(r"\b0\s*volt\b", "zero volt", s)
    s = re.sub(r"\binput\b", "primary", s)
    s = re.sub(r"\b(?:supply|power)\b", "voltage", s)
    s = re.sub(r"\boutput\b", "secondary", s)
    for pattern, replacement in (
        (r"\b(?:functioning|operating)\b", "working"),
        (r"\b(?:requested|requests|request)\b", "request"),
        (r"\b(?:proactive|voluntary)\b", "planned"),
        (r"\b(?:replacement|replacing|replaced)\b", "replace"),
        (r"\b(?:identifies|identify|lists|listed)\b", "list"),
        (r"\b(?:modifies|modify)\b", "modify"),
        (r"\binstalls\b", "install"),
        (r"\b(?:documentation|documents|document)\b", "document"),
        (r"\b(?:proposed|existing|exact|currently|required)\b", ""),
    ):
        s = re.sub(pattern, replacement, s)
    s = re.sub(r"\b(?:confirmed|verified|documented|observed|found|recorded|tested|testing|measured|measurement|measurements)\b", "", s)
    s = re.sub(r"\b(?:technician|contractor|quote|proposal|submitted|the|a|an|was|were|is|are|has|had|at|to|of|and|during|that|it|its|with|on|for|from|by)\b", "", s)
    return " ".join(re.findall(r"[\w]+(?:\.[\d]+)?|±", s))


def _sentences(value):
    value = re.sub(r",?\s+(?:and|but)\s+(?=(?:(?:the )?transformer )?(?:primary|input|secondary|output)\b)",
                   "\n", value, flags=re.I)
    return [s.strip(" -•\t") for s in re.split(r"\n+|(?<=[.!?;])\s+(?=[A-Za-z])", value) if s.strip()]


def work_scope_clauses(text):
    """Keep separately stated work and verification from borrowing each other's parts."""
    return [clause for sentence in _sentences(text)
            for clause in re.split(r";|\b(?:and|but)\s+(?=(?:replace|replacing|repair(?:ing)?|install(?:ing)?|check|verify|verifying|test)\b)",
                                   sentence, flags=re.I) if clause.strip()]


def _polarity(value):
    return bool(re.search(r"\b(?:not|never|no|without|negative)\b", value, re.I))


def _prospective(value):
    return bool(re.search(r"\b(?:will|should|would|could|may|might|if|example|hypothetical)\b|"
                          r"planned (?:test|measurement|reading)", value, re.I))


@dataclass
class SourceEvidence:
    text: str
    lines: list[str] = field(init=False)
    owners: set[str] = field(init=False)

    def __post_init__(self):
        self.lines = [s for s in _sentences(self.text) if not re.match(
            r"File Name:|Local Path:|QUOTE \d+|ARTIFICIAL SAMPLE", s, re.I)]
        self.owners = repair_owners(" ".join(self.lines))

    def grounded(self, value, owner=None):
        """Match source-supported propositions; formatting is not provenance.

        Numeric values, component identity, fault type and negation must agree.
        The lexical fallback only accepts close paraphrases, never guesses a
        measurement or transfers one from a different component.
        """
        value = str(value).strip()
        if not value:
            return False
        # Ground compound observations independently, including comparisons whose
        # measurement and published limit occur in separate parts of the quote.
        clauses = re.sub(r";\s*|\b(?:within|against) (?:the )?(?:submitted )?(?=manufacturer limit)", "\n", value, flags=re.I)
        parts = _sentences(re.sub(r"^Submitted [^:\n]+source:\s*|^Submitted return correction:\s*", "", clauses))
        if len(parts) > 1:
            return all(self.grounded(p, owner) for p in parts)
        expected_owner = owner if owner in REPAIR_OWNERS else None
        mentioned = repair_owners(value)
        if expected_owner not in {None, "coil", "metering device"} and re.search(
                r"refrigerant.{0,20}(?:pressure|suction|superheat|subcooling)", value, re.I):
            return False
        if expected_owner and mentioned and expected_owner not in mentioned:
            return False
        wanted = normalize_fact(value)
        numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", wanted))
        tokens = set(wanted.split())
        critical = tokens.intersection({"open", "ground", "short", "crack", "cracked", "leak", "leaking",
                                        "zero", "normal", "low", "high", "burned", "welded",
                                        "volt", "amp", "ohm", "ohms", "megohms", "psig", "cfm", "uf", "btu",
                                        "primary", "secondary"})
        # A generic summary of a supplied approved-combination document need not
        # repeat model identifiers. This does not establish a match absent such
        # a document and cannot validate any invented model/rating/measurement.
        if (not numbers and not expected_owner and re.fullmatch(
                r"(?:(?:manufacturer|ahri) )?document (?:list |ties? )?(?:models? )?together", wanted)):
            return bool(re.search(r"(?:manufacturer|AHRI).{0,160}(?:sheet|document|certificate).{0,160}"
                                  r"(?:lists?|identifies|ties).{0,160}(?:together|approved (?:matched )?combination)",
                                  " ".join(self.text.split()), re.I))
        proposed = set()
        for line in self.lines:
            if re.search(r"^(?:replace|proposed repair:|recommended repair:|install replacement)\b", line, re.I):
                proposed.update(repair_owners(line))
        context = self.owners if len(self.owners) == 1 else set()
        candidates = []
        for i, line in enumerate(self.lines):
            # An unlabelled continuation belongs only to the immediately preceding
            # named component, or to an unambiguous single-component document.
            line_owners = repair_owners(line)
            if line_owners:
                context = line_owners
            contextual = line_owners or (proposed if len(proposed) == 1 else context)
            if expected_owner and (expected_owner not in contextual or (not line_owners and len(contextual) > 1)):
                continue
            candidates.append(line)
        # Summaries of installation/replacement evidence may span several lines.
        # Component measurements remain local to an owned source statement.
        if not expected_owner:
            candidates += [" ".join(self.lines[i:i + 8]) for i in range(len(self.lines))]
        for line in candidates:
            candidate = normalize_fact(line)
            if _prospective(line) and not _prospective(value):
                continue
            if re.search(r"\b(?:not|never)\s+(?:been |actually )?(?:measured|tested)|unmeasured|unknown", line, re.I) and not re.search(
                    r"\b(?:not|never)\s+(?:been |actually )?(?:measured|tested)|unmeasured|unknown", value, re.I):
                continue
            if _polarity(value) != _polarity(line) and "zero" not in wanted:
                continue
            if not numbers.issubset(set(re.findall(r"\b\d+(?:\.\d+)?\b", candidate))):
                continue
            candidate_tokens = set(candidate.split())
            # Numeric zero and measured absence of output describe the same result.
            if "zero" in candidate_tokens:
                candidate_tokens.add("volt")
            if not critical.issubset(candidate_tokens):
                continue
            if wanted in candidate or (len(tokens) >= 2 and len(tokens & candidate_tokens) / len(tokens) >= .65):
                return True
        return False


def component_non_finding(value):
    """Recognize clear non-findings, not a closed list of valid component tests.

    Unknown source-backed observations remain with the structured assessment.
    This guard never supplies a failure criterion or upgrades technical support.
    """
    parts = re.split(r";\s*|(?<=[.!?])\s+|\s+(?:and|but)\s+", value.strip())
    if len(parts) > 1:
        return all(component_non_finding(part) for part in parts)
    text = value.strip(" .;:-\t").lower()
    text = re.sub(r"^(?:diagnosis|customer complaint|technician findings):\s*", "", text)
    text = re.sub(r"^(?:the )?(?:contractor|technician|customer) (?:reports?|reported|states?|stated) (?:that )?", "", text)
    component = "|".join(REPAIR_OWNERS[owner] for owner in
                         ("control board", "temperature sensor", "communication control", "compressor", "metering device"))
    text = re.sub(r"\b(?:(?:inverter|indoor|outdoor|indoor-unit|outdoor-unit)\s+)*(?:" + component + r")\b", "component", text, flags=re.I)
    subject = r"(?:the )?(?:component|unit|system)"
    if re.fullmatch(subject + r"\s+(?:(?:is|was|reportedly|has|been|found|to|be)\s+)*"
                    r"(?:bad|failed|faulty|defective|inoperative|failure confirmed|blamed for poor cooling|"
                    r"does not (?:start|run|cool)|not (?:starting|running|cooling)|won't (?:start|run)|no-start)", text):
        return True
    if re.fullmatch(r"(?:" + subject + r"\s+(?:has|shows|reports)\s+)?(?:a )?(?:reported )?"
                    r"(?:(?:component|sensor|temperature|communication|drive)\s+)*fault(?: code)?"
                    r"(?:\s+[a-z]*\d[\w-]*)?(?:\s+(?:is|was)\s+reported)?", text):
        return True
    if re.fullmatch(subject + r"\s+(?:diagnostic )?(?:test|testing|tests)", text):
        return True
    if re.fullmatch(r"no\b.*\b(?:tests?|testing|measurements?|readings?|findings|evidence|results)\b"
                    r"(?:\s+(?:are|were|was|is))?\s+(?:provided|documented|performed|recorded|included)", text):
        return True
    if (re.match(r"^(?:(?:the )?(?:contractor|technician|we)\s+)?(?:will|should|would|plan to|plans to)\b|"
                 r"^(?:replace|verify|check|test|measure)\b", text)
            and not re.search(r"\b(?:measured|tested|found|observed|recorded|showed)\b", text)):
        return True
    return False


def ground_assessments(analysis, source):
    """Validate the actual assessment objects before any support derivation."""
    rejected = []
    uncertain = set()
    for item in analysis.technical_assessments:
        owner = assessment_owner(item.subject)
        # Supporting controls/starting checks may be part of a motor/compressor
        # differential, but another failed component cannot establish its failure.
        has_owned = any(owner in repair_owners(e) and source.grounded(e, owner)
                        for e in item.documented_evidence) if owner else False
        def grounded(e):
            contextual = (has_owned and owner in {"blower motor", "condenser fan motor", "inducer motor", "compressor"}
                          and repair_owners(e) <= {"control board", "capacitor", "contactor", "coil", "pressure switch"})
            # An already-partial assessment may describe a general symptom.
            # Such a sentence cannot validate an approval-grade diagnosis.
            contextual = contextual or (item.diagnostic_evidence_status == "INCOMPLETE"
                and not repair_owners(e) and not re.search(r"\d|measur|winding|terminal|test", e, re.I))
            return source.grounded(e, None if contextual else owner)
        accepted = [e for e in item.documented_evidence if grounded(e)]
        removed = [e for e in item.documented_evidence if e not in accepted]
        rejected.extend(removed)
        item.documented_evidence = accepted
        # Missing-document statements are gaps, not source measurements. Preserve
        # them; independently asserted contradictions still need a source anchor.
        kept_conflicts = [c for c in item.contradictions if source.grounded(c, owner)]
        removed_conflicts = len(kept_conflicts) != len(item.contradictions)
        rejected.extend(c for c in item.contradictions if c not in kept_conflicts)
        item.contradictions = kept_conflicts
        if (owner in {"compressor", "control board", "temperature sensor", "communication control", "metering device"}
                and re.search(r"mini[- ]split|ductless|inverter", source.text, re.I)
                and accepted and not kept_conflicts
                and item.diagnostic_evidence_status in {"ADEQUATE", "CONFIRMED", "INCOMPLETE"}
                and all(component_non_finding(e) for e in accepted)):
            item.diagnostic_evidence_status = "ABSENT"
            item.scope_support = "UNSUPPORTED"
            if not item.material_gaps:
                item.material_gaps = [f"The quote does not document test findings isolating failure of the {owner}."]
        if removed and item.diagnostic_evidence_status in {"CONFIRMED", "ADEQUATE"}:
            item.diagnostic_evidence_status = "INCOMPLETE"
            item.scope_support = "PARTIALLY_DEFINED"
            uncertain.add(owner or item.subject)
        elif item.diagnostic_evidence_status == "CONTRADICTORY" and not kept_conflicts and (removed_conflicts or not accepted):
            item.diagnostic_evidence_status = "INCOMPLETE"
            item.scope_support = "PARTIALLY_DEFINED"
            uncertain.add(owner or item.subject)
    return rejected, uncertain


def remove_ungrounded_customer_claims(analysis, source, rejected):
    """Remove ungrounded submitted facts, not price policy or educational context."""
    rejected_normalized = {normalize_fact(s) for s in rejected if s.strip()}

    def keep(sentence):
        if normalize_fact(sentence) in rejected_normalized:
            return False
        measurement = re.search(r"\d+(?:\.\d+)?\s*(?:volts?|vac|v\b|amps?|ohms?|megohms?|psig|cfm|uf|µf|μf|mfd|°[FC])", sentence, re.I)
        submitted = re.search(r"measur|test\w*|recorded|documented|confirmed|found|reading", sentence, re.I)
        educational = re.search(r"commonly|typically|for example|a technician (?:should|may)|contractor (?:should|may)", sentence, re.I)
        return not (measurement and submitted and not educational and not source.grounded(sentence))

    for field_name in ("project_overview", "equipment_analysis", "missing_information", "installation_concerns",
                       "pricing_review", "contractor_vetting",
                       "recommendation", "banner_explanation", "homeowner_takeaway", "bottom_line",
                       "quote_comparison", "best_quote_recommendation"):
        setattr(analysis, field_name, " ".join(s for s in _sentences(getattr(analysis, field_name)) if keep(s)))
    for field_name in ("good_signs", "red_flags", "contractor_questions"):
        setattr(analysis, field_name, [s for s in getattr(analysis, field_name) if keep(s)])
    for field_name in ("required_actions", "verdict_reasons", "optional_suggestions"):
        setattr(analysis.decision, field_name, [s for s in getattr(analysis.decision, field_name) if keep(s)])
    for item in analysis.technical_assessments:
        item.material_gaps = [s for s in item.material_gaps if keep(s)]


def material_work(text, classification=None):
    """Return existing domain owners for affirmative material proposed work."""
    owners = set()
    for line in work_scope_clauses(text):
        if re.search(r"\b(?:not replacing|no replacement|do not replace|declined|if needed)\b|"
                     r"\bno (?:new )?installation\b|"
                     r"\bno\b.*(?:provided|documented|included)|not recommended|"
                      r"\bno (?:\w+ ){0,3}(?:replacement|repair)\b|not (?:proposed|needed)", line, re.I):
            continue
        if re.search(r"^(?:verify|check|confirm|test)\b", line, re.I):
            continue
        scope = re.split(r"\b(?:with|using|includes|featuring|after)\b", line, maxsplit=1, flags=re.I)[0]
        if re.search(r"\b(?:replace(?:ment|d|s)?|replacing|repair(?:s|ed)?|condemn\w*|diagnos(?:is|e[ds]?|ing))\b", scope, re.I):
            owners.update(repair_owners(scope))
        # Component replacements are not automatically whole-unit replacement.
        if not repair_owners(scope) and re.search(
            r"(?:replace\w*|install\w*).{0,25}\b(?:furnace|air conditioner|AC|heat[- ]pump|air[- ]handler|"
            r"(?:complete |full )?(?:HVAC |heating and cooling )?system|package[d]? unit|mini[- ]split)\b|"
            r"\b(?:furnace|air conditioner|AC|heat[- ]pump|air[- ]handler|(?:HVAC |full[- ])system|package[d]? unit) replacement", scope, re.I):
            owners.add("replacement basis")
    if classification:
        for name in classification.repair_components:
            # Structured scope may restore a source-stated owner, never invent it.
            owners.update(repair_owners(name) & repair_owners(text))
        if classification.replacement_components and classification.quote_type in {"replacement", "mixed"}:
            if re.search(r"replacement|replace|install", text, re.I) and not all(
                    re.search(r"capacitor|motor|board|contactor|transformer|igniter|sensor|switch", s, re.I)
                    for s in _sentences(text) if re.search(r"replace|replacement|install", s, re.I)):
                owners.add("replacement basis")
    # Installation charge allowances do not imply an independent repair owner.
    # Use the refrigerant owner's scope boundary; evidence grounding is unchanged.
    from refrigerant_system import refrigerant_diagnostic_scope
    if re.search(r"low (?:refrigerant|charge)|refrigerant (?:leak|loss)|recharge|add.{0,15}refrigerant", refrigerant_diagnostic_scope(text), re.I):
        owners.add("refrigerant")
    # A classifier label or diagnostic reference is not independent capacitor
    # work. Use the electrical owner's scope/failure boundary for both coverage
    # and routing. Import at call time because electrical presentation imports
    # SourceEvidence from this module.
    if "capacitor" in repair_owners(text):
        from electrical_controls import proposed_components
        if "capacitor" in proposed_components(text):
            owners.add("capacitor")
        else:
            owners.discard("capacitor")
    from system_sizing import new_mini_split_installation
    if new_mini_split_installation(text):
        owners.discard("replacement basis")
    return owners


def coverage_gaps(analysis, expected):
    relevant = [a for a in analysis.technical_assessments if a.materiality != "MINOR"]
    covered = {assessment_owner(a.subject) for a in relevant
               if a.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"} or a.documented_evidence}
    # Refrigerant diagnoses may be represented by their more specific coil/valve owner.
    if covered & {"coil", "metering device"}:
        covered.add("refrigerant")
    missing = expected - covered
    if not relevant or (not expected and not any(covered)):
        missing = missing or {"proposed work"}
    return sorted(missing)


def recovery_modules(expected):
    mapping = {name: "electrical_controls" for name in REPAIR_OWNERS}
    mapping.update({name: "motors" for name in ("blower motor", "condenser fan motor", "inducer motor")})
    mapping.update({"compressor": "compressor", "heat exchanger": "heat_exchanger",
                    "gas valve": "furnace_combustion", "combustion": "furnace_combustion",
                    "coil": "refrigerant_system", "metering device": "refrigerant_system",
                    "refrigerant": "refrigerant_system", "replacement basis": "repair_vs_replace"})
    return {mapping[owner] for owner in expected if owner in mapping}
