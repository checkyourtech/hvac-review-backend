"""Quote-only capacity evidence helpers; no equipment lookup or sizing calculator."""
import re
from decimal import Decimal

SIZING_SUBJECT = "Capacity justification for proposed HVAC system"

SYSTEM_SIZING_RULES = """
SYSTEM SIZING / CAPACITY JUSTIFICATION

Answer: does the submitted proposal provide enough building-specific information to
reasonably support the proposed HVAC capacity? Create one PRIMARY assessment per material
system/served space using subject "Capacity justification for proposed HVAC system".
Keep quote, project, system and zone identity in the evidence. Do not borrow sizing facts
from another quote, property, zone, or alternative. Do not fragment one issue into multiple
assessments or paraphrased red flags.

SUPPORTED: ADEQUATE or CONFIRMED + APPROPRIATE requires usable building-specific load
information, explicit proposed capacity, and an explanation tying the selection to the
applicable heating/cooling loads. Include relevant design conditions or backup strategy
where necessary. A usable Manual J or equivalent submitted load summary can be enough;
do not demand every report page or independently certify calculation validity/compliance.

ORDINARY INCOMPLETE: INCOMPLETE + PARTIALLY_DEFINED, never ABSENT for ordinary missing sizing
paperwork. This includes missing proposed capacity/models, missing load results, "Manual J
included" without results, a future calculation before final selection, same-size replacement
without a building-specific basis, and changed space without an updated review. The size
cannot be verified; it is not known to be wrong. Historical satisfactory comfort in an
unchanged home is useful context but ordinarily still partial without a submitted load basis.

CONTRADICTION: CONTRADICTORY + UNSUPPORTED only for concrete conflicting submitted sizing
facts. Cite both sides: e.g. same-project engineering selection differs from the proposed
capacity with no explanation, or a document claimed to support this home belongs to a
different project. Before comparing quantities, establish the same served space, operating
mode, units and design conditions. Do not invent universal oversizing percentages. A missing
calculation, complaint, model code or nominal-capacity difference is not a contradiction.

RULE OF THUMB: square footage is context, never proof. Explicit FINAL selection based ONLY
on an arbitrary area rule (1 ton per 500 square feet, 600 sq ft/ton, X BTU per square foot)
with no building-specific load review is INCOMPLETE + UNSUPPORTED: flag the unsupported
method once, not CONTRADICTORY or definitely wrong equipment. Preliminary estimates or
unclear methods remain INCOMPLETE + PARTIALLY_DEFINED. Require explicit exclusive reliance.

CAPACITY: preserve cooling/heating, input/output, nominal/design-condition, and project/zone
labels. One refrigeration ton = 12,000 Btu/h; kBtu/h and clearly identified HVAC MBH are
1,000 Btu/h. Bare BTU can be ambiguous. Conversion is not a sizing judgment. Model-number
codes 024/030/036/042/048/060 are not authoritative capacity evidence. Never infer capacity,
local design temperature, climate, elevation derating or OEM performance from model memory.

FURNACES: compare heating load with documented output, not furnace input. AFUE is not exact
design output; do not use input × AFUE as authoritative capacity. An unlabeled 60,000 BTU
furnace does not establish output. Same input rating does not prove appropriate sizing.

HEAT PUMPS: consider both loads, supplied design-condition output, intentional supplemental
electric heat or dual-fuel coverage. Do not require the heat pump alone to cover 100% of
heating load. Nominal cooling tonnage is not low-temperature heating output. Supported backup
strategy may justify selection. Do not optimize balance points, diagnose defrost, or retrieve
external performance tables here. Variable speed or multi-stage modulation does not prove
sizing; maximum nominal capacity exceeding load alone does not prove oversizing. Use submitted
operating-range/load evidence. For mini-splits preserve zone loads/selection strategy; do not
sum head ratings as simultaneous outdoor capacity or invent diversity. Material missing
zone/diversity information is partial unless real conflict is documented.

CHANGED SPACE: the basis should account for relevant additions, conditioned area, layout,
glazing, insulation, air sealing, attic/roof or conditioned garage changes. Same old capacity
without updated review is partial. Do not assume capacity must increase. Poor cooling, long
runtime, short cycling, humidity, hot/cold rooms, temperature swings and high bills are context,
not proof of oversized/undersized equipment; airflow, refrigerant, controls, envelope or failure
may explain them.

FIREWALLS: equipment_matching owns component pairing; AHRI matching and SEER2/EER2/HSPF2/AFUE
are not building-load evidence. repair_vs_replace owns replacement rationale. DUCT_AIRFLOW
owns static pressure, supply/return/duct capacity, blower setup, filter pressure drop, grilles
and delivered CFM. No universal 400 CFM/ton rule. Upsizing may make duct review relevant but
this module cannot judge it. Do not evaluate electrical breakers, linesets, commissioning,
pricing, guaranteed savings, lower bills, humidity improvement or equipment lifespan.

CUSTOMER REPORT: explain the specific submitted basis or gap naturally. Ordinary incomplete
sizing has zero sizing red flags and belongs in Important Missing Information. One real
contradiction has one sizing red flag. Credit documented building loads/selection or updated
loads and applicable backup strategy, not same size, matched equipment, efficiency, variable
speed, contractor experience or generic startup as sizing positives. Avoid "wrong size",
"oversized", "undersized" without supporting submitted evidence; avoid formal rubric language
such as "capacity methodology is insufficient" or "proposed tonnage lacks substantiation".
Use one context-specific system_sizing contractor question by default: how size was selected,
a referenced load summary, why keep the same size, whether changed space is included, or how
to reconcile conflicting documents. A second question is only for independent material backup
coverage. Do not ask a Manual J checklist. Pricing is separate. Use existing report sections.
"""

# These patterns identify the system, not internal repair components or duct dimensions.
_MAJOR = r"(?:hvac(?: system)?|complete system|full system|entire system|existing system|new system|heat[- ]pump(?: system)?|furnace|air conditioner|air conditioning(?: system)?|ac(?: system)?|condenser|outdoor unit|air handler|packaged?(?: hvac)?(?: system| unit)|mini[- ]split|ductless(?: system)?)"


def sizing_text(value):
    """Recognize building capacity language without consuming duct/electrical sizing."""
    text = re.sub(r"[-_]", " ", str(value or "").lower())
    if re.search(r"\b(?:duct|ductwork|return|supply|blower|breaker|conductor|gas pipe|lineset|register|grille)\b", text) and not re.search(r"\b(?:building load|home load|manual j|system size|tonnage)\b", text):
        return False
    return bool(re.search(
        r"\b(?:system siz\w*|size selection|proposed (?:size|tonnage)|tonnage|"
        r"hvac capacity|heat pump sizing|furnace capacity|equipment capacity|equipment size for (?:the )?(?:home|house|building)|"
        r"capacity (?:justification|selection|change|for|is|differs)|load based|"
        r"manual j|load (?:calculation|summary|review|information|results?|selection)|"
        r"(?:heating|cooling|building|zone) loads?|sizing|oversized|undersized|"
        r"same (?:nominal )?size|(?:properly|correctly) sized|(?:wrong|right|correct|selected) size|size of the (?:new )?system|system capacity)\b", text
    ))


def sizing_backup_text(value):
    text = str(value or "").lower()
    return bool(re.search(r"\b(?:backup|supplemental|dual[- ]fuel)\b", text)
                and re.search(r"\b(?:cover\w*|capacity|load|heating needs|heating demand)\b", text)
                and not re.search(r"\b(?:breaker|wire|voltage|circuit)\b", text))


def sizing_required(quote_text, classification=None):
    """Conservative completeness routing from submitted scope or classified major units."""
    if classification is not None:
        if "system_sizing" in [getattr(m, "value", m) for m in classification.modules_required]:
            return True
        if any(re.fullmatch(_MAJOR, part.strip().lower()) for part in classification.replacement_components):
            return True
        if classification.quote_type in {"replacement", "installation"} and re.search(_MAJOR, classification.primary_scope.lower()):
            return True
    text = str(quote_text or "").lower().replace("-", " ")
    # Only affirmative scope/claim lines count; mentioning an option that is declined does not.
    for line in text.splitlines():
        if re.search(r"\b(?:no|not|without|declined|declines|not recommended)\b", line):
            continue
        if re.search(r"\b(?:increase|decrease|change|evaluate|investigate|review)\w*\b.{0,40}\b(?:system capacity|tonnage|hvac capacity)\b", line):
            return True
        if re.search(r"\b(?:manual j|load calculation|load summary|sizing method)\b", line):
            return True
        if sizing_text(line) and re.search(r"\b(?:oversized|undersized|tonnage change|capacity change|capacity selection|sizing)\b", line):
            return True
        if re.search(r"\b(?:replace|replacement of|install|installation of)\b[^.!?\n]{0,60}\b" + _MAJOR + r"\b", line):
            # 'Replace furnace control board' is a repair, not a furnace replacement.
            if not re.search(r"\b(?:compressor|capacitor|contactor|igniter|control board|flame sensor|thermostat|sensor|motor|coil|valve|filter|drain)\b", line) or re.search(r"\b(?:complete|entire) system\b", line):
                return True
        if re.search(r"\b" + _MAJOR + r"\b\s+(?:replacement|installation)\b", line):
            return True
        if re.search(r"\b(?:new|replacement)\s+(?:[\w.-]+\s+){0,3}" + _MAJOR + r"\b", line) and not re.search(r"\b(?:board|motor|capacitor|igniter|coil|valve|thermostat|sensor|contactor|compressor|drain)\b", line):
            return True
    return False


def normalize_capacity(value, unit, *, mode=None, rating=None, basis=None,
                       project=None, system=None, zone=None, design_conditions=None,
                       hvac_heat_rate=False):
    """Normalize an explicit value/unit while retaining all caller-supplied labels."""
    normalized = re.sub(r"\s+", "", unit.lower())
    factor = None
    if normalized in {"ton", "tons", "refrigerationton", "refrigerationtons"}:
        factor = Decimal(12000)
    elif normalized in {"btu/h", "btuh", "btu/hr"}:
        factor = Decimal(1)
    elif normalized in {"kbtu/h", "kbtuh", "kbtu/hr"} or (normalized == "mbh" and hvac_heat_rate):
        factor = Decimal(1000)
    number = Decimal(str(value).replace(",", ""))
    if not number.is_finite() or number < 0:
        raise ValueError("Capacity must be a finite nonnegative number")
    return dict(value=str(value), unit=unit, btu_per_hour=float(number * factor) if factor else None,
                mode=mode, rating=rating, basis=basis, project=project, system=system, zone=zone,
                design_conditions=design_conditions)


def exclusive_area_rule(text):
    """Only an explicitly exclusive, final rule is an unsupported selection method."""
    for sentence in re.split(r"\n|(?<=[.!?])\s+", text.lower()):
        if re.search(r"\b(?:not|preliminary|rough|estimate)\b", sentence):
            continue
        if (re.search(r"\b(?:only|solely|exclusively)\b", sentence)
                and re.search(r"\bfinal\b", sentence)
                and re.search(r"(?:ton.{0,30}(?:square feet|sq\.?\s*ft)|(?:square feet|sq\.?\s*ft).{0,20}(?:ton|/ton)|btu.{0,20}(?:square foot|sq\.?\s*ft))", sentence)):
            return True
    return False


def explicit_capacity_values(text):
    """Read only explicit rate tokens; retain source text rather than inventing labels.

    The caller must keep project/zone and input/output context. Values are never summed
    or compared here, and model codes and bare BTU do not become heat rates.
    """
    values = []
    for match in re.finditer(
        r"(?<![\w.])(?P<value>\d[\d,]*(?:\.\d+)?)\s*[-–]?\s*"
        r"(?P<unit>refrigeration tons?|tons?|k?btu/(?:hr|h)|k?btuh|mbh)\b",
        text, re.IGNORECASE,
    ):
        unit = match.group("unit")
        # MBH is accepted only when this source identifies a load/capacity/output/input.
        context = bool(re.search(r"\b(?:load|capacity|output|input)\b", text, re.IGNORECASE))
        value = normalize_capacity(match.group("value"), unit, hvac_heat_rate=context)
        value.update(source_text=text, start=match.start(), end=match.end())
        values.append(value)
    return values


def usable_load_basis(evidence):
    """A support floor, not an independent load calculation or equipment approval."""
    text = " ".join(evidence).lower()
    positive = " ".join(line for line in re.split(r"\n|(?<=[.!?])\s+", text)
                        if not re.search(r"\b(?:no|not|missing|pending|absent|without)\b", line))
    rate = r"\d[\d,]*(?:\.\d+)?\s*[-–]?\s*(?:refrigeration tons?|tons?|btu/(?:hr|h)|btuh|kbtu/(?:hr|h)|kbtuh|mbh)\b"
    # Assess the roles expressed in the evidence, not one fixed noun/value order.
    # Both 'cooling load: 34,000 Btu/h' and '34,000 Btu/h cooling load' are usable.
    load_role = r"(?:(?:heating|cooling|building|zone)(?: design)? loads?|load (?:calculation|summary|results))"
    load = re.search(load_role + r"[^.;]{0,100}" + rate, positive) or re.search(
        rate + r"[^.;]{0,45}(?:heating|cooling)(?: design)?(?: loads?)?\b", positive
    )
    equipment_role = r"(?:capacity|output|(?:selected|proposed) (?:system|equipment|ac|air conditioner|furnace|heat pump)|(?:ac|furnace|heat pump) (?:delivers?|provides?))"
    capacity = re.search(equipment_role + r"[^.;]{0,100}" + rate, positive) or re.search(
        r"(?:selected|proposed)\s+" + rate + r"\s+(?:ac|air conditioner|system|furnace|heat pump)\b", positive
    )
    linkage = re.search(r"\b(?:support\w*|based on|tied to|ties|meets?|covers?|selected for|consistent with|fits?|selection)\b", positive)
    rates = [value for value in explicit_capacity_values(positive)
             if value["btu_per_hour"] is not None]
    # Never elevate known shortcuts to documented design output.
    inferred = re.search(r"\b(?:infer\w*|model (?:code|number|pattern)|input.{0,20}afue|afue.{0,20}input)\b", positive)
    furnace_input_only = ("furnace" in positive and "heating load" in positive
                          and "input" in positive and not re.search(r"(?:furnace|heating) output\s*[:=]?\s*\d", positive))
    nominal_heat_pump_only = ("heat pump" in positive and "heating load" in positive
                              and "nominal" in positive
                              and not re.search(r"\b(?:design|supplemental|backup|dual fuel|heating output)\b", positive))
    return bool(load and re.search(load_role, positive) and capacity and linkage and len(rates) >= 2
                and not inferred and not furnace_input_only and not nominal_heat_pump_only)


# Display values are taken from explicit submitted text, never decoded from models.
_DISPLAY_RATE = r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*[-–]?\s*(?P<unit>refrigeration tons?|tons?|k?btu/(?:hr|h)|k?btuh|mbh)\b"


def submitted_sizing_excerpt(quote_text):
    """Keep sizing source blocks together with their project and operating context."""
    blocks = re.split(r"\n\s*\n", quote_text.strip())
    return "\n\n".join(block for block in blocks if re.search(
        r"\b(?:project:|(?:cooling|heating|building|zone) load|capacity:|output|"
        r"load.selection summary|conditioned addition|conditioned space|proposed equipment:)",
        block, re.IGNORECASE,
    ) and not re.match(r"\s*(?:installation scope|scope of work|total installed price)", block, re.IGNORECASE))


def sizing_display_values(text):
    """Extract distinct heating/cooling roles, preserving nominal versus output ratings."""
    text = re.sub(r"\s+", " ", text).replace("heat-pump", "heat pump")

    def value(*patterns):
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                normalized = normalize_capacity(match['number'], match['unit'], hvac_heat_rate=True)
                amount = float(match['number'].replace(',', ''))
                unit = match['unit'].lower()
                if 'ton' in unit:
                    label = f"{amount:g}-ton"
                else:
                    label = f"{normalized['btu_per_hour']:,.0f} Btu/h"
                return dict(label=label, btu_per_hour=normalized['btu_per_hour'],
                            source=match.group(0), nominal='ton' in unit)
        return None

    result = {
        'cooling_load': value(r'cooling (?:design )?load\s*[:=]?\s*' + _DISPLAY_RATE,
                              _DISPLAY_RATE + r'\s+cooling load'),
        'heating_load': value(r'heating (?:design )?load\s*[:=]?\s*' + _DISPLAY_RATE,
                              _DISPLAY_RATE + r'\s+heating load'),
        'cooling_capacity': value(r'(?:quoted |proposed |nominal )?cooling capacity\s*[:=]?\s*(?:nominal\s+)?' + _DISPLAY_RATE,
                                 r'(?:selected|proposed)\s+' + _DISPLAY_RATE + r'\s+(?:ac|cooling system|air conditioner)'),
        'cooling_output': value(r'cooling output\s*(?:of|:|=)?\s*' + _DISPLAY_RATE,
                               r'(?:provides?|gives?|delivers?)\s+' + _DISPLAY_RATE + r'\s+cooling'),
        'furnace_output': value(r'furnace (?:heating )?output\s*(?:of|:|=)?\s*' + _DISPLAY_RATE,
                               r'furnace delivers\s+' + _DISPLAY_RATE + r'\s+heating'),
        'furnace_input': value(r'furnace (?:heating )?input\s*(?:of|:|=)?\s*' + _DISPLAY_RATE),
        'heat_pump_output': value(r'heat pump heating output\s*(?:of|:|=)?\s*' + _DISPLAY_RATE,
                                 _DISPLAY_RATE + r'\s+heat pump heating output'),
        'backup_output': None,
        'selection_capacity': value(r"(?:engineer.s final selection|load.based selection|sizing summary)\s*(?:specifies|calls for|selects)[^.;]{0,70}?" + _DISPLAY_RATE),
    }
    backup_documented = bool(re.search(
        r'(?:intentionally uses|design (?:uses|documents)|simultaneous).*?(?:supplemental|backup|dual.fuel)|'
        r'(?:supplemental|backup).{0,90}(?:covers|coverage|simultaneous)', text, re.IGNORECASE))
    if backup_documented:
        result['backup_output'] = value(r'(?:supplemental|backup)[^.;]{0,100}?output\s*(?:of|:|=)?\s*' + _DISPLAY_RATE)
    result['backup_documented'] = backup_documented
    result['heat_pump'] = bool(re.search(r'\bheat pump\b', text, re.IGNORECASE))
    result['furnace'] = bool(re.search(r'\bfurnace\b', text, re.IGNORECASE))
    result['changed_space'] = bool(re.search(r'conditioned addition|added or changed space|changed window|adds conditioned space', text, re.IGNORECASE))
    return result


def clear_submitted_sizing_support(text):
    """Recover an explicit, single-project selection summary, not a new sizing calculation.

    This deliberately requires both loads, explicit outputs and the submitted author's
    selection explanation. It cannot approve from area, nominal tonnage, model patterns,
    a future calculation, or documentation belonging to another project.
    """
    text = re.sub(r'\s+', ' ', text)
    values = sizing_display_values(text)
    affirmative = re.sub(r'\bno (?:conflicting|contradictory) sizing information\b', '', text, flags=re.I)
    if re.search(r'\b(?:instead|rather than|conflict\w*|contradict\w*|different (?:home|project)|only|pending)\b', affirmative, re.I):
        return False
    project = re.search(r'\bproject:\s*(?P<name>[^,.]+)', text, re.I)
    if not project or len(re.findall(re.escape(project['name'].strip()), text, re.I)) < 2:
        return False
    if not re.search(r'(?:submitted|project.specific|building.specific).{0,60}(?:load|selection).{0,30}summary', text, re.I):
        return False
    if not re.search(r'(?:selection summary ties|selection supports|supports the proposed equipment selection)', text, re.I):
        return False
    if not all(values[k] for k in ('cooling_load', 'heating_load', 'cooling_capacity', 'cooling_output')):
        return False
    if values['heat_pump']:
        return bool(values['heat_pump_output'] and 'design' in text.lower()
                    and (values['backup_documented'] and values['backup_output']))
    return bool(values['furnace_output']) if values['furnace'] else False


def comparable_heat_pump_plan(text):
    """Display-only arithmetic for an explicit single-project simultaneous design."""
    normalized = re.sub(r'\s+', ' ', text).replace('heat-pump', 'heat pump')
    values = sizing_display_values(text)
    if (not clear_submitted_sizing_support(text)
            or len(re.findall(r'\bProject:', text, re.I)) != 1
            or not all(values[key] for key in ('heating_load', 'heat_pump_output', 'backup_output'))
            or not re.search(r'heat pump heating output.{0,70}at (?:the )?stated heating design conditions', normalized, re.I)
            or not re.search(r'permitting simultaneous operation', normalized, re.I)):
        return None
    load = values['heating_load']['btu_per_hour']
    output = values['heat_pump_output']['btu_per_hour']
    backup = values['backup_output']['btu_per_hour']
    remaining = max(0, load - output)
    if backup < remaining:
        return None
    return values, remaining


def sizing_review_paragraphs(evidence, status, scope):
    """Interpret the accepted assessment without changing its sizing decision."""
    text = '\n'.join(evidence)
    values = sizing_display_values(text)
    facts = []
    modes = ['cooling', 'heating']
    if values['furnace'] and not values['heat_pump'] and not any(values[key] for key in ('cooling_load', 'cooling_capacity', 'cooling_output')):
        modes = ['heating']
    elif not values['furnace'] and not values['heat_pump'] and not values['heating_load'] and values['cooling_capacity']:
        modes = ['cooling']
    for mode in modes:
        load = values[mode + '_load']
        part = f"{mode.title()}: "
        part += f"the submitted home load is {load['label']}" if load else "the home's load is not shown in the submitted information"
        if mode == 'cooling':
            capacity, output = values['cooling_capacity'], values['cooling_output']
            if capacity:
                if capacity['nominal']:
                    part += f"; the proposal lists a {capacity['label']} system ({capacity['btu_per_hour']:,.0f} Btu/h nominal cooling capacity)"
                else:
                    part += f"; proposed cooling capacity is {capacity['label']}"
            if output and status != 'CONTRADICTORY':
                part += f"; documented cooling output is {output['label']}"
        elif values['heat_pump']:
            output = values['heat_pump_output']
            part += (f"; documented heat-pump heating output is {output['label']} at the stated design condition" if output
                     else "; heat-pump output at the heating design condition is not documented")
            if values['backup_output']:
                part += f", with {values['backup_output']['label']} of documented backup heat"
        elif values['furnace']:
            output = values['furnace_output']
            if output:
                part += f"; the proposed furnace output is {output['label']}"
            else:
                part += "; furnace output is not documented, so the heating side cannot be fully verified"
                if values['furnace_input']:
                    part += f" (the listed {values['furnace_input']['label']} is input, not output)"
        facts.append(part + '.')
    if status == 'CONTRADICTORY':
        selection, proposal = values['selection_capacity'], values['cooling_capacity']
        if selection and proposal:
            conclusion = (f"The cooling numbers don't line up. The load-based selection calls for a {selection['label']} system, "
                          f"but the quote proposes a {proposal['label']} system. Nothing in the paperwork explains that change. "
                          "This is the part I'd want explained before signing.")
            if values['heating_load'] and values['furnace_output'] and not values['heat_pump']:
                conclusion += " The conflict identified here is on the cooling side; it does not establish a problem with the furnace size."
            # Educational context only for an explicitly comparable upward selection
            # conflict. Nominal tonnage alone never establishes a performance problem.
            if (values['cooling_load']
                    and proposal['btu_per_hour'] > selection['btu_per_hour']
                    and proposal['btu_per_hour'] > values['cooling_load']['btu_per_hour']
                    and re.search(r'same.*(?:cooling selection|design conditions)', text, re.I)
                    and not re.search(r'(?:different|another|wrong) (?:home|house|project|property)', text, re.I)):
                conclusion += (" A cooling system substantially larger than the calculated load can run in shorter cycles "
                               "and may make temperature or humidity control harder.")
            conclusion += " Before approving, ask why the cooling size changed or request updated load-based sizing results supporting the quoted system."
        else:
            conclusion = "The sizing numbers don't line up with the quote. That leaves the equipment choice in doubt. Before approving, ask the contractor to explain the difference or correct the sizing information for this home."
    elif scope == 'UNSUPPORTED':
        conclusion = "The final selection relies only on an area rule. Floor area alone does not tell us this home's heating and cooling needs. Before approving, ask for a load review for this house."
    elif status == 'INCOMPLETE' or scope == 'PARTIALLY_DEFINED':
        if values['changed_space']:
            conclusion = ("The project changes the conditioned space, but the quote does not show an updated sizing review. "
                          "The old equipment size carries less weight when the house changes. That does not mean the old size is wrong "
                          "or the new system has to be larger; the sizing should reflect the remodeled house. "
                          "Before approving, ask whether the load review includes the added or changed space.")
        elif not values['cooling_load'] and not values['heating_load']:
            conclusion = ("The quote tells you what they're installing, but not what heating and cooling load was calculated for the house. "
                          "Without those numbers, we cannot tell whether the equipment fits this home's needs. "
                          "Before approving, ask for the load calculation or sizing results used to select it.")
        else:
            conclusion = "The proposed size cannot be verified yet: the available figures do not fully connect this home's needs to the equipment's output. Before approving, ask for the missing load or selection results."
    else:
        conclusion = ("The sizing looks reasonable: the contractor tied the equipment's output to this home's calculated loads. "
                      "That is more useful than nominal tonnage alone, which does not have to equal the load exactly. "
                      "The output figures reasonably track the calculated loads in this selection; nothing stands out as a sizing concern.")
        if values['heat_pump'] and values['backup_documented']:
            conclusion = ("The heat pump is not being asked to carry the entire heating load by itself on the coldest design day. "
                          "Backup heat covers the remaining load as an intentional part of this design. "
                          "Using backup this way does not by itself mean the heat pump is undersized; the load and selection results support the combined heating plan.")
            plan = comparable_heat_pump_plan(text)
            if plan:
                _, remaining = plan
                explanation = (
                    f"At the stated design condition, the heat pump covers about {values['heat_pump_output']['label']} "
                    f"of the home's {values['heating_load']['label']} heating load, leaving roughly "
                    f"{remaining:,.0f} Btu/h for backup heat. The proposal includes "
                    f"{values['backup_output']['label']} of backup heat, so the submitted heating plan "
                    "has enough combined capacity to cover the documented load. "
                )
                conclusion = explanation + conclusion
                if values['cooling_output'] and values['cooling_load']:
                    conclusion += (
                        f" On the cooling side, the documented {values['cooling_output']['label']} output "
                        f"is close to the {values['cooling_load']['label']} calculated load in this submitted "
                        "selection; nothing in those cooling numbers stands out as a sizing concern."
                    )
    return [' '.join(facts), conclusion]
