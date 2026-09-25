"""Quote price facts and the application-owned boundary for future market comparisons.

Future benchmarks need project market, repair/replacement scope, equipment, capacity,
efficiency/fuel type, matching, accessories and documented installation complexity.
Reuse QuoteClassification and technical_assessments for those facts. Benchmark
source/date/confidence must come from the application, never model memory.
"""
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MarketPriceStatus(str, Enum):
    NOT_EVALUATED = "not_evaluated"
    BELOW_EXPECTED = "below_expected"
    WITHIN_EXPECTED = "within_expected"
    ABOVE_EXPECTED = "above_expected"
    CANNOT_COMPARE = "cannot_compare"


class MarketPriceContext(BaseModel):
    status: MarketPriceStatus = MarketPriceStatus.NOT_EVALUATED
    project_zip: Optional[str] = None
    market_area: Optional[str] = None
    expected_low: Optional[float] = None
    expected_high: Optional[float] = None
    currency: str = "USD"
    data_source: Optional[str] = None
    source_date: Optional[str] = None
    confidence: Optional[str] = None


class QuotePriceItem(BaseModel):
    category: str
    label: str
    amount: float
    currency: str = "USD"
    source_text: str


class QuotePriceFacts(BaseModel):
    quote_index: int
    # Source lines retain option/allowance/rebate conditions and per-unit qualifiers.
    # Items are never summed: bundled charges and alternate options can overlap.
    items: list[QuotePriceItem] = Field(default_factory=list)


def extract_quote_price_facts(text: str) -> list[QuotePriceFacts]:
    """Retain explicitly labeled currency amounts without estimating missing prices."""
    blocks = re.split(r"(?m)^\s*QUOTE \d+[^\n]*\n", text)
    if len(blocks) > 1:
        blocks = blocks[1:]
    facts = []
    for index, block in enumerate(blocks, 1):
        items = []
        for line in block.splitlines():
            for match in re.finditer(r"(?P<currency>[$£€])\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)", line):
                label = line[:match.start()].strip(" -:\t")
                if not label:
                    label = line[match.end():].strip(" -:\t")
                if not label:
                    continue
                lower = label.lower()
                category = "other"
                for name, pattern in (
                    ("repair_option_total", r"repair (?:option|alternative)|(?:option|alternative).*repair"),
                    ("replacement_total", r"total replacement|replacement (?:total|price)"),
                    ("total", r"\btotal\b"),
                    ("discount_rebate", r"discount|rebate"),
                    ("warranty", r"warranty"),
                    ("permit_fee", r"permit|fees?"),
                    ("refrigerant", r"refrigerant"),
                    ("accessory_addon", r"accessor|add.on|optional"),
                    ("labor_installation_materials", r"labor.*materials|materials.*labor"),
                    ("installation_materials", r"materials"),
                    ("labor", r"labor"),
                    ("equipment", r"equipment"),
                ):
                    if re.search(pattern, lower):
                        category = name
                        break
                items.append(QuotePriceItem(
                    category=category, label=label,
                    amount=float(match['amount'].replace(',', '')),
                    currency={"$": "USD", "£": "GBP", "€": "EUR"}[match['currency']],
                    source_text=line.strip(),
                ))
        if items:
            facts.append(QuotePriceFacts(quote_index=index, items=items))
    return facts


def evaluate_market_price(
    quote_facts: list[QuotePriceFacts], *, project_zip: Optional[str] = None,
    market_area: Optional[str] = None,
) -> MarketPriceContext:
    """Future verified-benchmark service boundary; no benchmark exists in production yet.

    Deliberately ignores any AI status, expected range or claimed source. A future
    application-owned implementation can consume these facts and verified benchmarks.
    This result is never an input to the current decision policy.
    """
    return MarketPriceContext(project_zip=project_zip, market_area=market_area)


def compressor_category_breakdown(text: str) -> Optional[str]:
    """Accept explicit, nonoverlapping repair categories, not internal job costing.

    Unlike general price facts (which may overlap), these two bounded layouts have
    one total and disjoint fixed charges. Abstain on options, allowances or extras.
    """
    facts = extract_quote_price_facts(text)
    if len(facts) != 1 or re.search(
        r"allowance|per (?:hour|pound)|price to be determined|additional.{0,30}(?:charge|fee)|"
        r"(?:charge|fee|tax).{0,25}(?:excluded|not included|unknown)|optional|alternative", text, re.I
    ):
        return None
    layouts = (
        {"total repair price", "compressor", "labor", "refrigerant", "materials"},
        {"total repair price", "compressor and materials", "labor"},
    )
    values = {}
    for item in facts[0].items:
        label = item.label.lower().strip()
        if label == "total price":
            label = "total repair price"
        if label in values or item.currency != "USD":
            return None
        values[label] = item.amount
    if set(values) not in layouts or any(v < 0 for v in values.values()):
        return None
    total = values.pop("total repair price")
    if round(sum(values.values()), 2) != round(total, 2):
        return None
    def money(amount):
        return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"
    parts = [f"{money(amount)} for {label}" for label, amount in values.items()]
    detail = ", ".join(parts[:-1]) + ", and " + parts[-1]
    return f"The {money(total)} repair price is broken into {detail}. That gives a clear breakdown of the quoted repair cost."


def unnecessary_cost_detail(value: str) -> bool:
    return bool(re.search(r"itemiz|breakdown|markup|labor hours|hourly labor|labor rate|internal (?:equipment )?cost|profit|wholesale|every fitting|consumable|pricing transparency|pricing (?:is )?(?:limited|insufficient|unclear)", value, re.I))


def normalize_compressor_pricing(analysis, text: str) -> None:
    """Source-calibrate meaningful repair itemization before canonical verdicts."""
    review = compressor_category_breakdown(text)
    if not review:
        return
    concerns = [*analysis.decision.required_actions, *analysis.decision.verdict_reasons]
    # Do not erase an independent pricing approval issue or unresolved charge.
    if any(re.search(r"price|pricing|cost|fee|charge|tax|payment", s, re.I) and not unnecessary_cost_detail(s)
           for s in concerns):
        return
    if re.search(r"(?:additional|undisclosed|unknown|excluded).{0,30}(?:fees?|charges?|tax)|"
                 r"(?:fees?|charges?|tax).{0,30}(?:unclear|unknown|excluded|not included)", " ".join([analysis.pricing_review, *concerns]), re.I):
        return
    analysis.decision.pricing_transparency = "ADEQUATE"
    for name in ("required_actions", "verdict_reasons"):
        setattr(analysis.decision, name, [s for s in getattr(analysis.decision, name) if not unnecessary_cost_detail(s)])
    for name in ("missing_information", "installation_concerns"):
        setattr(analysis, name, " ".join(s for s in re.split(r"(?<=[.!?])\s+", getattr(analysis, name))
                                         if not unnecessary_cost_detail(s)))
    analysis.pricing_review = review
    analysis.good_signs = [s for s in analysis.good_signs if not re.search(r"pric|cost|breakdown|itemiz|markup", s, re.I)]
    analysis.good_signs.append("The quoted repair cost is separated into meaningful categories.")


PRICING_MARKET_RULES = """
PRICING FACTS AND REGIONAL PRICE FIREWALL
Keep quote clarity separate from regional price comparisons. No verified regional
benchmark is supplied by this application yet. Market context must be NOT_EVALUATED,
with no expected range, source or confidence. Never use training-data memory, generic
internet knowledge, national averages or remembered contractor prices to call a quote
cheap, fair, expensive, above market, below market or competitive. Preserve quoted
amounts as facts, not benchmarks. Python retains price facts from the submitted text.
Write homeowner pricing prose: explain how the total is divided or what remains bundled.
Avoid high-level breakdown, cost allocation, pricing components, transparency
classification, itemization level, market positioning and material pricing detail.
Do not add an empty market-comparison notice to reports.
"""


def customer_pricing_text(value: str) -> str:
    """Remove unsupported price judgments and translate pricing jargon only."""
    value = value.replace(
        "That is a meaningful high-level breakdown for a replacement quote.",
        "That makes it easier to see how the quote is divided.",
    )
    value = value.replace("total is broken into", "total is split into")
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", value)
    retained = []
    for sentence in sentences:
        price_context = re.search(r"price|pricing|cost|market|quote|[$£€]", sentence, re.I)
        judgment = re.search(
            r"\b(?:cheap|expensive|overpriced|underpriced|fair|competitive)\b|"
            r"\b(?:above|below|within)[- ](?:average|market|expected)|"
            r"\b(?:expected|typical|average|market) (?:price|cost|range)|"
            r"\b(?:price|cost)\b.{0,25}\b(?:high|low|reasonable|excessive)\b",
            sentence, re.I,
        )
        if price_context and judgment:
            continue
        for pattern, replacement in (
            (r"meaningful high-level breakdown|high-level breakdown", "clear split of the total"),
            (r"pricing transparency is adequate", "the quote shows how the total is divided"),
            (r"pricing components", "charges"),
            (r"allocation of costs|cost allocation", "how the total is divided"),
            (r"itemization enhances transparency", "a breakdown shows what is included"),
            (r"material pricing detail", "important price details"),
        ):
            sentence = re.sub(pattern, replacement, sentence, flags=re.I)
        retained.append(sentence)
    return " ".join(retained)
