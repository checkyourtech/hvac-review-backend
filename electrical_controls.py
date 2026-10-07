"""Component electrical evidence. No verdict, pricing, or motor-failure policy."""
import re
from decimal import Decimal
from compressor import compressor_items, compressor_question, compressor_normal_after_start_repair, partial_voltage_drop
from refrigerant_system import refrigerant_items, subject_kind, CONDITION
from pricing import MAJOR_CHARGE_ITEMIZATION_REQUEST
from evidence_sufficiency import proposed_work_facts


ELECTRICAL_CONTROLS_RULES = """
ELECTRICAL / CONTROL REPAIR ANALYSIS RULES
Does the submitted proposal provide enough evidence that the identified electrical/control
component has failed or requires the proposed work? Create one PRIMARY
TechnicalEvidenceAssessment per independently proposed component diagnosis, with subject
"Electrical control evidence: <component>". Named work is not evidence of failure.
Own capacitors, contactors/relays, control boards, transformers, low-voltage controls,
electrical switching of pressure/limit/safety switches, flame-sensor electrical signals,
and igniter electrical/physical failure. Do not assess incidental healthy components
as new primary diagnoses. Motor/ECM motor-module failure belongs to MOTORS;
compressor windings/ground faults/mechanical failure belong to COMPRESSOR. An independent
capacitor or control fault is separate even when it serves a motor or compressor.
Never let one component's failure prove another's. Combustion quality, CO, gas pressure,
draft/venting and flame characteristics belong to FURNACE_COMBUSTION/HEAT_EXCHANGER;
switch-state and flame-signal tests cannot establish combustion safety. Refrigerant,
duct capacity, full startup plans and pricing remain with their respective owners.

Evidence, not length, controls support. CONFIRMED/ADEQUATE + APPROPRIATE can follow
one sufficient direct test, not a universal checklist. Use measured capacitance compared with rated capacitance
and submitted/nameplate tolerance; do not invent a universal
percentage. Physical rupture, bulging or leakage may establish capacitor failure.
A gross loss of capacitance compared with the documented rating can also support
capacitor failure when no tolerance is supplied. Describe the actual rated/measured
values, not an invented manufacturer tolerance. Modest deviations without a supplied
tolerance remain unresolved; they are not automatically normal or failed. A failed
capacitor does not establish compressor damage, low refrigerant, or a leak.
Contactor/relay: correct control input and failed contact behavior, welded contacts,
direct failed continuity/function test, or documented excessive closed-contact voltage
drop can suffice. Visibly burned or pitted contacts are actual positive evidence when
they support the proposed contact repair.
A thermostat/control call with a contactor/relay that does not pull in is meaningful
but INCOMPLETE when voltage/control at that device's coil is unconfirmed. A call
elsewhere is not proof that the coil receives it. Confirmed control voltage at the
coil during the call with failure to pull in/close/transfer or absent switched output
can directly support replacement; do not require an invented voltage value or other
unsubmitted measurements. Generic no-start/no-cooling symptoms do not prove failure.
CONTROL BOARD REPAIRS: correct required power/input and call present, expected output
absent, confirmed damage tied to failure, or a supplied manufacturer criterion met may
isolate the board. "Board has power but unit doesn't work" alone does not.
Switches: evaluate state/continuity under the documented required condition; a fault
code alone does not prove a bad switch. Flame sensors: compare actual flame-signal/current
with submitted criteria or other direct failure evidence, not flame dropout alone.
Igniters: documented open circuit where continuity is expected, resistance/current
against a submitted criterion, or direct physical failure may suffice.

INCOMPLETE + PARTIALLY_DEFINED: meaningful but unresolved observations such as a board
fault code, intermittent contactor, weak capacitor without measurements/tolerance,
pressure-switch fault without switch/condition testing, intermittent igniter or flame
dropout without signal evidence. Generic no-start/no-heat/no-cooling symptoms alone do
not isolate any component. ABSENT + UNSUPPORTED: a replacement assertion with no
meaningful component evidence. CONTRADICTORY + UNSUPPORTED: normal submitted test
results contradict the claimed failure and no independent failure supports the work.
Do not turn absent tests into contradictions. Do not require every possible test,
part number, warranty, manufacturer tech-support call, or a pin-by-pin checklist.

Customer fields: explain the actual findings, what they do/do not establish, and the
one needed clarification. Keep good_signs factual: measured capacitance compared with
rated capacitance, visibly burned or pitted contacts, documented voltage checks, direct
input/output isolation, and documented post-repair operational verification when useful.
Never invent readings, tolerance, switch state, board output, flame current, visual
damage, model, warranty, startup result, or manufacturer criteria. Unsupported component
replacement gets one diagnosis-specific red flag, not multiple paraphrases; partial
evidence gets a clarification, not a fabricated red flag. Good supported work needs no
technical question. Unresolved work needs at most one primary technical question per
independent component issue, seeking only the unanswered evidence. Pricing is separate.
Do not diagnose an alternative fault remotely or guarantee a repair will fix everything.
"""

PREFIX = "Electrical control evidence: "
COMPONENTS = {
    "capacitor": r"(?:(?:dual |run |start |starting )*capacitor)",
    "contactor": r"contactor",
    "relay": r"(?:sequencer|(?:time[- ]delay )?relay)",
    "control board": r"(?:(?:furnace |defrost |fan )?control board|defrost board)",
    "transformer": r"transformer",
    "pressure switch": r"pressure[- ]switch",
    "limit switch": r"(?:high[- ]limit|limit|safety)[- ]switch",
    "flame sensor": r"flame[- ]sensor",
    "igniter": r"(?:(?:hot[- ]surface )?ignit[eo]r)",
    "low-voltage control": r"low[- ]voltage (?:control|wiring|component)",
}


def source_lines(text):
    return [line.strip(" -•\t") for line in text.splitlines() if line.strip()
            and not re.match(r"\s*(?:File Name:|Local Path:|QUOTE \d+)", line, re.I)]


def components_in(value):
    return [name for name, pattern in COMPONENTS.items()
            if re.search(r"\b" + pattern + r"\b", str(value), re.I)]


def proposed_components(text):
    """Only work/diagnosis of a discrete control, not a motor with normal inputs."""
    found = []
    for line in source_lines(text):
        if re.search(r"\b(?:not replacing|no replacement|do not replace|declined)\b", line, re.I):
            continue
        for name, pattern in COMPONENTS.items():
            if re.search(r"\b(?:replace|repair|install|investigate|diagnose)\w*\s+(?:the |a |new |failed |existing |furnace |compressor |blower motor |condenser fan motor |\d+(?:/\d+)?\s*(?:(?:µF|μF|MFD|uF) )?|dual |run |start )*"
                         + pattern + r"\b|\b" + pattern + r"\s+(?:replacement|repair|diagnosis)\b", line, re.I):
                if name not in found:
                    found.append(name)
    return found


def electrical_required(text, classification=None):
    return bool(proposed_components(text))


def motor_or_compressor_only(text):
    return (not electrical_required(text) and bool(re.search(
        r"(?:replace|repair) (?:the |failed )?(?:blower motor|condenser fan motor|inducer motor|ECM (?:motor|module)|compressor)\b|"
        r"(?:blower motor|condenser fan motor|inducer motor|ECM (?:motor|module)|compressor) replacement", text, re.I)))


def motor_work_required(text):
    return any(re.search(r"(?:replace|repair) (?:the |failed )?(?:blower motor|condenser fan motor|inducer motor|ECM (?:motor|module))\b"
                         r"(?!\s+(?:(?:run|start|starting) )?capacitor)|"
                         r"(?:blower motor|condenser fan motor|inducer motor|ECM (?:motor|module)) replacement", line, re.I)
               and not re.search(r"\b(?:do not replace|not replacing|declined|if needed)\b", line, re.I)
               for line in source_lines(text))


def component_subject(value):
    s = str(value or "")
    prefixed = re.fullmatch(r"electrical\s+control\s+evidence\s*:\s*(.+)", s.strip(), re.I)
    if prefixed:
        suffix = re.sub(r"[-_]+", " ", prefixed[1]).strip().lower()
        names = components_in(suffix)
        return names[0] if len(names) == 1 else suffix
    # A compressor capacitor is a control; compressor failure after a capacitor
    # repair, motor performance and whole-system domains are not.
    if re.search(r"compressor (?:failure|winding|work|diagnosis)|motor (?:failure|performance)|"
                 r"replacement basis|basis for|equipment match|sizing|commission|startup|warranty|pric|combustion|heat.exchanger", s, re.I):
        return None
    names = components_in(s)
    return names[0] if len(names) == 1 else None


def electrical_items(analysis):
    return [a for a in analysis.technical_assessments
            if a.materiality != "MINOR" and component_subject(a.subject)]


def _actual(line):
    return not re.search(r"^(?:verify|check|measure|test|example|hypothetical)\b|\b(?:will|would|should|could|may|might|if|suspect\w*|recommend\w*)\b|"
                         r"not (?:tested|measured|documented)|no attempt|no .{0,140}(?:readings|testing|test results).{0,25}(?:provided|documented)", line, re.I)


def _capacitor_result(text):
    """Use supplied tolerance first; otherwise recognize only gross capacitance loss.

    At or below half the stated capacitance is a conservative positive-only recovery
    boundary, not a manufacturer tolerance or a universal pass/fail specification.
    Values outside this gross-loss case cannot be called normal without a supplied
    tolerance. Dual-capacitor readings must be tied to their named sections.
    """
    unit = r"(?:µF|μF|uF|MFD|microfarads?)"
    # A proposed replacement's specification is not the tested capacitor's rating.
    text = "\n".join(s for s in text.splitlines()
                     if not re.match(r"(?:replace|install|remove|proposed)\b", s, re.I))
    number = r"(\d+(?:\.\d+)?)"
    candidates = re.finditer(number + r"(?:/" + number + r")?\s*" + unit
                             + r"\s*(rated\s*)?(?:(?:±|\+/-)\s*" + number + r"\s*%)?", text, re.I)
    rated = next((m for m in candidates if m[4] or m[3]
                  or re.search(r"(?:rating|rated|nameplate)\s*:?\s*$", text[max(0, m.start()-50):m.start()], re.I)
                  or re.match(r"\s*(?:run |dual run |start )?capacitor\b", text[m.end():], re.I)), None)
    if not rated:
        return None
    ratings = [rated[1]] + ([rated[2]] if rated[2] else [])
    if rated[2]:
        readings = []
        for label in (r"(?:Compressor/HERM|HERM(?:/compressor)?|Compressor)", r"Fan"):
            matches = re.findall(label + r"(?: side| section)?\s*(?:measured(?: at)?|:)\s*" + number + r"\s*" + unit, text, re.I)
            if len(matches) != 1:
                return None
            readings.extend(matches)
    else:
        readings = re.findall(r"(?:measured(?: capacitance)?(?: at)?)\s*[:=]?\s*" + number + r"\s*" + unit, text, re.I)
    if len(readings) != len(ratings):
        return None  # Do not guess which dual-capacitor terminal was measured.
    if any(Decimal(rating) <= 0 for rating in ratings):
        return None
    if rated[4] is None:
        return True if any(Decimal(read) <= Decimal(rating) / 2
                           for read, rating in zip(readings, ratings)) else None
    tolerance = Decimal(rated[4]) / 100
    if tolerance <= 0 or tolerance >= 1:
        return None
    return any(abs(Decimal(read) - Decimal(rating)) > Decimal(rating) * tolerance
               for read, rating in zip(readings, ratings))


def _switching_observations(lines, component):
    """Separate an upstream call from a confirmed input at the switching device."""
    call, coil, failed, normal = [], [], [], []
    for line in lines:
        # Negation of contact behavior must not negate an affirmed input in the
        # same sentence; unconfirmed/absent input must never become positive.
        clauses = re.split(r";\s*|(?<=[.!?])\s+|\bbut\b", line, flags=re.I)
        for clause in clauses:
            if any(name != component for name in components_in(clause)):
                continue  # Another device's input cannot establish this one's.
            affirmed = not re.search(r"\b(?:no|not|absent|missing|unconfirmed|unknown)\b", clause, re.I)
            if affirmed and re.search(r"(?:thermostat|control) call.*(?:present|confirmed)", clause, re.I):
                call.append(line)
            if (affirmed and re.search(r"(?:control|coil) voltage", clause, re.I)
                    and re.search(r"\b(?:present|correct|proper|rated)\b", clause, re.I)
                    and re.search(r"\b(?:coil|contactor|relay|sequencer)\b", clause, re.I)):
                coil.append(line)
            if re.search(r"(?:does not|did not|fails? to|failed to) (?:pull in|close|transfer|pass power)|"
                         r"contacts? (?:do not|did not) (?:close|transfer)|contacts? remain(?:s)? open|"
                         r"(?:switched|expected) output.*(?:absent|missing|not present)", clause, re.I):
                failed.append(line)
            if affirmed and re.search(r"contacts.*(?:close|transfer|pass power).*correctly", clause, re.I):
                normal.append(line)
    return dict(call=list(dict.fromkeys(call)), coil=list(dict.fromkeys(coil)),
                failed=list(dict.fromkeys(failed)), normal=list(dict.fromkeys(normal)))


def _facts(component, text):
    lines = source_lines(text)
    actual = [s for s in lines if _actual(s)]
    related = [s for s in actual if component in components_in(s)]
    # Labeled numerical continuations are source evidence too (never supply units).
    if component == "capacitor":
        related += [s for s in actual if re.search(r"(?:µF|μF|uF|MFD|microfarad)", s, re.I)]
    if component == "flame sensor":
        related += [s for s in actual if re.search(r"flame signal|burners ignite|flame.*stable", s, re.I)]
    if component in {"contactor", "relay"} and set(components_in(text)) == {component}:
        # A standalone call belongs here only in an unambiguous component quote.
        related += [s for s in actual if re.fullmatch(
            r"(?:Thermostat|Control) call (?:was )?(?:confirmed )?present[.]?", s, re.I)]
    related = list(dict.fromkeys(related))
    evidence = [s for s in related if re.search(
        r"measur|test\w*|continuity|volts?|voltage|signal|current|rated|rating|tolerance|±|\+/-|micro|µF|μF|MFD|"
        r"crack|ruptur|bulg|leak|burn|pitt|weld|damage|fault code|intermittent|weak|drop|"
        r"input|output|call|pressure|does not close|changes state|remains open|energiz", s, re.I)
        and not re.search(r"^(?:bad |failed |replace |install |remove |no .*(?:provided|documented))", s, re.I)]
    joined = "\n".join(related)
    supported = conflict = False
    if component == "capacitor":
        result = _capacitor_result(joined)
        physical = any(re.search(r"capacitor.*(?:ruptured|bulging|leaking)|(?:ruptured|bulging|leaking).*capacitor", s, re.I)
                       and not re.search(r"\b(?:no|not)\b", s, re.I) for s in related)
        supported, conflict = physical or result is True, result is False and not physical
    elif component in {"contactor", "relay"}:
        switching = _switching_observations(related, component)
        supported = bool(switching["coil"] and switching["failed"])
        supported |= any(re.search(r"welded|visibly burned and pitted|failed (?:continuity|function) test", s, re.I)
                        and not re.search(r"\b(?:no|not welded)\b", s, re.I) for s in related)
        conflict = not supported and bool(switching["normal"])
        supported |= any(re.search(r"excessive.*voltage drop.*closed contacts", s, re.I)
                         and re.search(r"measured|documented", s, re.I)
                         and not re.search(r"\b(?:no|not)\b", s, re.I) for s in related)
        if supported:
            conflict = False
        evidence = list(dict.fromkeys([*evidence, *switching["call"], *switching["coil"], *switching["failed"]]))
    elif component in {"control board", "transformer", "low-voltage control"}:
        powered = re.search(r"(?:correct|required|proper|rated).*(?:power|voltage|input).*(?:present|confirmed|measured)|"
                            r"(?:power|voltage|input).*(?:correct|proper|rated)", joined, re.I)
        call = any(re.search(r"(?:required |thermostat )?(?:call|command|input signal).*(?:present|confirmed)", s, re.I)
                   and not re.search(r"\b(?:no|not|absent|missing)\b", s, re.I) for s in related)
        powered = powered and not re.search(r"(?:power|voltage|input).*(?:not present|not confirmed|absent)", joined, re.I)
        absent = re.search(r"(?:expected|required) output.*(?:absent|missing|not present)|no (?:expected|required) output", joined, re.I)
        supported = bool(powered and absent and (call or component == "transformer"))
        supported |= any(re.search(r"confirmed.*(?:board damage|burned circuit).*(?:output|failure)", s, re.I)
                         and not re.search(r"\b(?:no|not)\b", s, re.I) for s in related)
        conflict = not supported and bool(re.search(r"required input.*expected output.*(?:both )?(?:operate|function).*correctly", joined, re.I))
    elif component in {"pressure switch", "limit switch"}:
        supported = bool(re.search(r"(?:does not close|remains open|fails to (?:close|change state)).*(?:proper draft|required (?:pressure|condition)|measured draft exceeds)|"
                                   r"(?:required (?:pressure|condition)|proper draft).*(?:satisfied|met|present).*switch.*(?:remains open|does not close)", joined, re.I))
        conflict = not supported and bool(re.search(r"switch.*changes state correctly.*required condition", joined, re.I))
    elif component == "igniter":
        supported = any(re.search(r"ignit[eo]r.*(?:resistance|continuity|circuit).*(?:tested|measured|documented).*open|"
                                  r"ignit[eo]r.*(?:inspected|documented).*cracked", s, re.I)
                        and not re.search(r"\b(?:no|not)\b", s, re.I) for s in related)
    elif component == "flame sensor":
        measured = re.search(r"flame signal measured at (\d+(?:\.\d+)?) microamps", joined, re.I)
        minimum = re.search(r"manufacturer minimum flame signal is (\d+(?:\.\d+)?) microamps", joined, re.I)
        flame_present = any(re.search(r"flame.*(?:stable|established)|burners ignite normally", s, re.I)
                            and not re.search(r"\b(?:no|not)\b", s, re.I) for s in related)
        supported = bool(measured and minimum and Decimal(measured[1]) < Decimal(minimum[1]) and flame_present)
        conflict = bool(measured and minimum and Decimal(measured[1]) >= Decimal(minimum[1]) and flame_present)
    # Explicit manufacturer rejection findings are an alternative to these bounded
    # numerical patterns. No invented criterion or universal limit is supplied.
    criterion_failure = [s for s in related if re.search(r"manufacturer.*(?:criterion|criteria).*(?:met|failed)|"
                          r"failed.*manufacturer.*(?:criterion|criteria)", s, re.I)
                         and re.search(r"failure|reject|failed", s, re.I)
                         and not re.search(r"\b(?:no|not)\b", s, re.I)]
    if criterion_failure:
        supported, conflict = True, False
        evidence.extend(criterion_failure)
    meaningful = bool(evidence and re.search(r"measur|tested|\bweak\b|fault code|intermittent|signal|input|output|pitt|burn", " ".join(evidence), re.I))
    if component in {"contactor", "relay"}:
        meaningful = bool(switching["coil"] or (switching["call"] and switching["failed"]) or any(
            re.search(r"measur|tested|intermittent|pitt|burn", s, re.I)
            and not re.search(r"\b(?:no|not|unconfirmed|unknown)\b", s, re.I) for s in evidence))
    if component == "flame sensor" and re.search(r"burners.*(?:shut off|drop out)|flame drops out", text, re.I):
        observations = [s for s in actual if re.search(r"burners.*(?:shut off|drop out)|flame drops out", s, re.I)]
        evidence += observations
        meaningful |= bool(observations)
    # A switch's name or a bare rating is not an actual finding.
    if not supported and not conflict and not meaningful:
        evidence = []
    status = "CONFIRMED" if supported else "CONTRADICTORY" if conflict else "INCOMPLETE" if meaningful else "ABSENT"
    scope = "APPROPRIATE" if supported else "PARTIALLY_DEFINED" if status == "INCOMPLETE" else "UNSUPPORTED"
    gaps = [] if supported or conflict else [f"The quote does not show findings isolating failure of the {component}."]
    if component in {"contactor", "relay"} and status == "INCOMPLETE" and not switching["coil"]:
        gaps = [f"The quote does not confirm control voltage at the {component} coil during the call."]
    return dict(subject=PREFIX + component, materiality="PRIMARY", diagnostic_evidence_status=status,
                scope_support=scope, documented_evidence=list(dict.fromkeys(evidence)),
                contradictions=[f"The submitted {component} test shows normal behavior despite the failure claim."] if conflict else [],
                material_gaps=gaps)


def isolated_electrical_work(analysis, text):
    items = electrical_items(analysis)
    if not items or not proposed_components(text):
        return False
    if len(re.findall(r"(?m)^\s*QUOTE \d+", text)) > 1:
        return False
    if any(a not in items and a.materiality != "MINOR" for a in analysis.technical_assessments):
        return False
    # Do not take over combined projects or a diagnosis in an installation quote.
    return not re.search(r"(?:replace|install|repair) (?:the |new |failed )?(?:compressor|blower|condenser|inducer|evaporator|heat pump|air handler|(?:complete |full )?(?:HVAC )?system)\b|"
                         r"(?:compressor|motor|system|furnace|heat pump) replacement", text, re.I)


def electrical_source_facts(text):
    if len(re.findall(r"(?m)^\s*QUOTE \d+", text)) > 1:
        return []  # Never combine tests from different quotes.
    return [_facts(name, text) for name in proposed_components(text)]


def normalize_electrical_assessments(analysis, text, assessment_type, classification=None):
    if not text.strip():
        return  # Compatibility calls with no source retain structured input.
    if motor_or_compressor_only(text):
        # Do not erase independently documented control findings in a combined case.
        analysis.technical_assessments = [a for a in analysis.technical_assessments
            if a not in electrical_items(analysis) or (a.documented_evidence and all(
                e.strip().casefold() in text.casefold() for e in a.documented_evidence))]
        return
    for facts in electrical_source_facts(text):
        component = component_subject(facts["subject"])
        items = [a for a in electrical_items(analysis) if component_subject(a.subject) == component]
        # Preserve richer source-backed structured evidence outside bounded recovery.
        # Do not preserve AI-only measurements or an unsupported status assertion.
        grounded = [a for a in items if a.documented_evidence and all(
            e.strip().casefold() in text.casefold() and _actual(e) for e in a.documented_evidence)
            and re.search(r"criterion|criteria|isolat|failed.*(?:function|test)|documented.*(?:damage|physical)",
                          " ".join(a.documented_evidence), re.I)]
        if (facts["diagnostic_evidence_status"] == "INCOMPLETE" and grounded
                and any(a.diagnostic_evidence_status in {"CONFIRMED", "ADEQUATE"} for a in grounded)):
            chosen = grounded[0].model_copy(update={"subject": facts["subject"], "materiality": "PRIMARY"})
        else:
            chosen = assessment_type(**facts)
        # Replace this domain in place; repeated finalization must not move it
        # around independent compressor/refrigerant assessments.
        insert_at = next((i for i, a in enumerate(analysis.technical_assessments) if a in items),
                         len(analysis.technical_assessments))
        remaining = [a for a in analysis.technical_assessments if a not in items]
        remaining.insert(insert_at, chosen)
        analysis.technical_assessments = remaining


def electrical_question_purpose(value):
    if re.search(r"pric|cost|itemiz|breakdown|warranty|after (?:the )?(?:work|repair)|startup", value, re.I):
        return None
    if re.search(r"compressor (?:itself|winding|failure)|motor (?:itself|failure)", value, re.I):
        return None
    components = components_in(value)
    if len(components) == 1 and re.search(r"test|evidence|fail|finding|reading|measur|replace|input|output|state|diagnos|tolerance", value, re.I):
        return "electrical_" + components[0].replace(" ", "_")
    return None


def unresolved(item):
    return item.diagnostic_evidence_status not in {"ADEQUATE", "CONFIRMED"} or item.scope_support != "APPROPRIATE"


def electrical_question(item):
    name = component_subject(item.subject)
    if item.diagnostic_evidence_status == "CONTRADICTORY":
        return f"What other finding supports replacing the {name} given the documented normal test result?"
    evidence = " ".join(item.documented_evidence)
    if name in {"contactor", "relay"}:
        if item.diagnostic_evidence_status == "INCOMPLETE":
            if not _switching_observations(item.documented_evidence, name)["coil"]:
                return f"What control voltage was measured at the {name} coil during the call?"
            return f"What contact or switched-output testing showed that the {name} itself has failed?"
        return f"What testing showed that the {name} itself has failed?"
    if name == "capacitor":
        if re.search(r"measured.*(?:µF|μF|MFD|microfarad)", evidence, re.I):
            return "What tolerance is listed on the capacitor, and is the measured value outside it?"
        return "What capacitance did the capacitor measure, and what tolerance is listed on it?"
    return {
        "control board": "What input and output testing showed that the control board itself has failed?",
        "pressure switch": "What testing showed that the pressure switch failed rather than another condition keeping it open?",
        "flame sensor": "What flame-signal reading or other test showed that the flame sensor needs replacement?",
        "igniter": "What test showed that the igniter itself has failed?",
    }.get(name, f"What test showed that the {name} itself has failed?")


def conclusion(item):
    name = component_subject(item.subject)
    status = item.diagnostic_evidence_status
    if status == "CONTRADICTORY":
        if name == "capacitor":
            return "The submitted capacitor reading is within the listed tolerance, so that measurement does not support replacing the capacitor."
        return f"The submitted {name} test shows normal behavior, so it does not support the claimed failure."
    if status == "ABSENT":
        return f"The quote recommends replacing the {name}, but does not include findings showing that it has failed."
    if unresolved(item):
        return f"The quote points toward the {name}, but does not yet isolate it as the failed component."
    return f"The documented electrical findings support replacing the {name}."


def _supported_board_input_output_copy(item):
    """Translate documented board facts, never infer tests from support status."""
    if component_subject(item.subject) != "control board" or unresolved(item):
        return None
    patterns = {
        "power": r"(?:control )?board (?:required |correct |proper )?(?:power(?: and voltage)?|voltage) (?:was |were )?(?:confirmed |measured )?present[.]?",
        "call": r"(?:control )?board thermostat call (?:was )?(?:confirmed |measured )?present[.]?",
        "output": r"(?:control )?board (?:expected|required) output (?:was )?(?:measured |confirmed )?(?:absent|missing|not present) during (?:the |that )call[.]?",
    }
    found, additional = set(), []
    for value in item.documented_evidence:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", value.strip()):
            kind = next((name for name, pattern in patterns.items() if re.fullmatch(pattern, sentence, re.I)), None)
            if kind:
                found.add(kind)
            else:
                additional.append(sentence)
    if found != set(patterns):
        return None  # Physical damage/other evidence does not imply these checks.
    evidence = (
        "The technician confirmed power to the control board and confirmed that the thermostat was calling for operation. "
        "During that call, the expected output from the board was absent.")
    # Preserve any extra submitted measurements, terminals or findings verbatim.
    return " ".join([evidence, *additional, "Those findings support replacing the control board."])


def _supported_switching_copy(item):
    """Polish only submitted call/coil/behavior facts; preserve extra readings."""
    name = component_subject(item.subject)
    if name not in {"contactor", "relay"} or unresolved(item):
        return None
    patterns = {
        "call": rf"(?:{name} )?(?:thermostat|control) call (?:was )?(?:confirmed )?present[.]?",
        "coil": rf"{name} control voltage (?:was )?(?:confirmed |measured )?present at (?:the coil|(?:the )?{name} coil) during (?:the |that )call[.]?",
        "behavior": rf"{name} (?:does not|did not|fails? to) (pull in|close|transfer)(?: during (?:the |that )call)?[.]?",
    }
    found, additional, behavior = set(), [], None
    for value in item.documented_evidence:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", value.strip()):
            kind = next((key for key, pattern in patterns.items() if re.fullmatch(pattern, sentence, re.I)), None)
            if kind:
                found.add(kind)
                if kind == "behavior":
                    behavior = re.fullmatch(patterns[kind], sentence, re.I)[1].lower()
            else:
                additional.append(sentence)
    if found != set(patterns):
        return None  # Other supported tests must not imply these three checks.
    evidence = (
        f"The technician confirmed that the {name} was receiving the control signal during the call, "
        f"but the {name} did not {behavior} as expected. Those findings support replacing the {name}.")
    return " ".join([evidence, *additional])


def electrical_good_sign(item):
    if _supported_switching_copy(item):
        name = component_subject(item.subject)
        return f"The quote documents control voltage reaching the {name} coil and the {name} failing to respond to the call."
    if _supported_board_input_output_copy(item):
        return (
            "The quote documents the key electrical checks supporting the control board diagnosis: "
            "power at the board, a thermostat call reaching the board, and the expected board output being absent.")
    return " ".join(item.documented_evidence)


def electrical_paragraphs(analysis):
    return [_supported_switching_copy(item) or _supported_board_input_output_copy(item)
            or " ".join([*item.documented_evidence, conclusion(item)])
            for item in electrical_items(analysis)]


def finalize_electrical_fields(analysis, text):
    items = electrical_items(analysis)
    if not items:
        return
    names = {component_subject(a.subject) for a in items}
    owned = lambda s: bool(names.intersection(components_in(s))) and not re.search(
        r"compressor[- ](?:specific|itself|failure|replacement|diagnosis|condition|damage)|motor failure|pric|cost|warranty|startup", s, re.I)
    # Rebuild this domain's statements even in a mixed report. Otherwise a recovered
    # supported component can retain the AI's stale missing-evidence sentence.
    for field in ("equipment_analysis", "missing_information"):
        setattr(analysis, field, " ".join(s for s in re.split(r"(?<=[.!?])\s+", getattr(analysis, field))
                                         if not owned(s)))
    for field in ("good_signs", "red_flags", "contractor_questions"):
        setattr(analysis, field, [s for s in getattr(analysis, field) if not owned(s)])
    for field in ("required_actions", "verdict_reasons"):
        setattr(analysis.decision, field, [s for s in getattr(analysis.decision, field) if not owned(s)])
    gaps = []
    for item in items:
        name = component_subject(item.subject)
        if unresolved(item):
            if name == "control board" and item.diagnostic_evidence_status == "INCOMPLETE":
                gaps.append("The quote does not show the input/output testing needed to confirm that the control board itself has failed.")
            elif name in {"contactor", "relay"} and item.diagnostic_evidence_status == "INCOMPLETE":
                gaps.extend(item.material_gaps)
            else:
                gaps.append(f"The quote does not show what finding supports replacing the {name}." if item.diagnostic_evidence_status != "CONTRADICTORY"
                            else f"The quote does not explain why the {name} needs replacement despite the normal test result.")
            analysis.decision.required_actions.append(f"Ask for the evidence supporting {name} replacement before approving that work.")
            analysis.decision.verdict_reasons.append(conclusion(item))
            analysis.contractor_questions.append(electrical_question(item))
            if item.scope_support == "UNSUPPORTED":
                analysis.red_flags.append(conclusion(item))
        elif item.documented_evidence:
            analysis.good_signs.append(electrical_good_sign(item))
    other = [a for a in analysis.technical_assessments if a not in items and a.materiality != "MINOR"]
    if not other:
        analysis.equipment_analysis = " ".join(conclusion(a) for a in items)
        analysis.missing_information = " ".join(gaps) if gaps else "No important technical information is missing from the submitted diagnosis."
    elif gaps:
        analysis.missing_information = (analysis.missing_information + " " + " ".join(gaps)).strip()
    if other:
        analysis.equipment_analysis = " ".join([analysis.equipment_analysis, *(conclusion(a) for a in items)]).strip()
        if not analysis.missing_information.strip() and not any(unresolved(a) for a in [*items, *other]):
            analysis.missing_information = "No important technical information is missing from the submitted diagnosis."
    if isolated_electrical_work(analysis, text):
        # For an isolated, recovered component diagnosis, build customer facts from
        # source instead of allowing invented AI measurements in neighboring fields.
        names_text = " and ".join(component_subject(a.subject) for a in items)
        analysis.project_overview = f"The quote proposes replacing the {names_text}."
        scope = [s for s in source_lines(text) if re.match(r"(?:replace|remove|install|reconnect|verify|operate|confirm|measure|test)\b", s, re.I)]
        analysis.installation_concerns = " ".join(scope) or f"The stated scope is {names_text} replacement."
        analysis.red_flags = [conclusion(a) for a in items if a.scope_support == "UNSUPPORTED"]
        analysis.good_signs = [electrical_good_sign(a) for a in items if not unresolved(a) and a.documented_evidence]
        analysis.decision.required_actions = [f"Ask for the evidence supporting {component_subject(a.subject)} replacement before approving that work."
                                              for a in items if unresolved(a)]
        analysis.decision.verdict_reasons = [conclusion(a) for a in items if unresolved(a)]
        # Raw optional advice must not smuggle nonexistent observations into prose.
        analysis.decision.optional_suggestions = []
        prices = [s for s in source_lines(text) if re.search(r"[$£€]\s*\d", s)]
        analysis.pricing_review = " ".join(prices) if prices else "No price is documented in the submitted quote."
        if prices and analysis.decision.pricing_transparency in {"LIMITED", "ABSENT"}:
            analysis.pricing_review += " " + MAJOR_CHARGE_ITEMIZATION_REQUEST
        analysis.quote_comparison = analysis.best_quote_recommendation = analysis.contractor_vetting = ""


def assemble_electrical_questions(analysis, questions):
    """One unanswered purpose per component; retain independent domain questions."""
    items = electrical_items(analysis)
    if not items:
        return questions
    names = {component_subject(a.subject) for a in items}
    other = [a for a in analysis.technical_assessments if a not in items and a.materiality != "MINOR"]
    owned_purposes = {"electrical_" + name.replace(" ", "_") for name in names}
    kept = [q for q in questions if electrical_question_purpose(q) not in owned_purposes] if other else []
    if supported_capacitor_unresolved_compressor(analysis):
        kept = ["What testing shows that the compressor itself has failed and needs replacement?"
                if compressor_question(q) else q for q in kept]
    return [electrical_question(a) for a in items if unresolved(a)] + kept


def supported_capacitor_unresolved_compressor(analysis):
    """Presentation context only; never upgrade or recalculate either diagnosis."""
    capacitors = [a for a in electrical_items(analysis) if component_subject(a.subject) == "capacitor"]
    compressors = compressor_items(analysis)
    return bool(capacitors and all(not unresolved(a) for a in capacitors)
                and compressors and any(unresolved(a) for a in compressors)
                and not compressor_normal_after_start_repair(analysis) and not partial_voltage_drop(analysis))


def compose_electrical_summary(analysis, quote_text=""):
    items = electrical_items(analysis)
    if supported_capacitor_unresolved_compressor(analysis):
        compressors = compressor_items(analysis)
        refrigerant = refrigerant_items(analysis)
        # Summarize this mixed repair only when these are the unresolved domains.
        # Independent commissioning, other controls, and replacement issues keep
        # their existing summary ownership.
        if (all(component_subject(a.subject) == "capacitor" for a in items)
                and refrigerant and all(subject_kind(a.subject) == CONDITION and unresolved(a) for a in refrigerant)
                and all(a.materiality == "MINOR" or a in [*items, *compressors, *refrigerant]
                        for a in analysis.technical_assessments)):
            analysis.homeowner_takeaway = (
                "The documented capacitor readings support replacing the capacitor. "
                "The quote does not yet show that the compressor is damaged or establish why the proposed refrigerant work is needed.")
            analysis.bottom_line = (
                "The capacitor replacement is supported by the documented readings. "
                "Before approving the compressor and refrigerant work, have the contractor show what evidence supports those additional repairs.")
            if analysis.decision.verdict == "GET_A_SECOND_OPINION":
                analysis.bottom_line += " Get a second opinion before approving the unsupported work."
            return
    if not items or any(a not in items and a.materiality != "MINOR" for a in analysis.technical_assessments):
        return
    if (len(items) == 1 and _supported_switching_copy(items[0])
            and isolated_electrical_work(analysis, quote_text)
            and analysis.decision.technical_support == "SUPPORTED"):
        name = component_subject(items[0].subject)
        action = (" Before approving the work, ask for an itemized breakdown of the quoted total."
                  if analysis.decision.verdict == "REVIEW_BEFORE_APPROVING"
                  and analysis.decision.pricing_transparency in {"LIMITED", "ABSENT"} else "")
        analysis.homeowner_takeaway = f"The documented control-signal and response checks support replacing the {name}." + action
        analysis.bottom_line = f"The {name} replacement is technically supported." + action
        return
    if (len(items) == 1 and _supported_board_input_output_copy(items[0])
            and isolated_electrical_work(analysis, quote_text)
            and analysis.decision.technical_support == "SUPPORTED"):
        # This isolated supported repair has no technical action; the canonical
        # pricing state/verdict still controls the approval wording.
        if (analysis.decision.verdict == "REVIEW_BEFORE_APPROVING"
                and analysis.decision.pricing_transparency in {"LIMITED", "ABSENT"}):
            facts = proposed_work_facts(quote_text)
            total = f"the {facts.amount} total" if facts and facts.component == "control board" else "the quoted total"
            action = f"Before approving the work, ask for an itemized breakdown of {total}."
        elif analysis.decision.verdict == "PROCEED":
            action = ""
        else:
            return  # Do not hide a different canonical approval concern.
        analysis.homeowner_takeaway = (
            "The control-board diagnosis is well supported by the testing shown in the quote. "
            "The board had power and received the thermostat call, but the expected output was missing. "
            + action).strip()
        analysis.bottom_line = ("The control-board replacement is technically supported. " + action).strip()
        return
    if any(unresolved(a) for a in items):
        analysis.banner_explanation = " ".join(conclusion(a) for a in items if unresolved(a))
        analysis.homeowner_takeaway = analysis.banner_explanation + " Ask to see the findings before approving the component replacement."
        analysis.bottom_line = ("Ask for the missing component-failure evidence or get a second opinion before approving the work."
                               if analysis.decision.verdict == "GET_A_SECOND_OPINION" else
                               "Have the contractor confirm the component diagnosis before approving the repair.")
