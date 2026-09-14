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
