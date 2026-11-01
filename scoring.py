"""
Deterministic qualification rules (no LLM, no network).

The LLM only *interprets* verified evidence: it says which verified claims show what
(e.g. "claims 2 and 5 show internal tooling"). The numbers and the classification
below are computed from those cited claims, so a result is explainable and reproducible.
"""

from dataclasses import dataclass, field
from typing import Dict, List

# Tune the rules here. Documented in the README ("Scoring rules").
CONFIG = {
    "technical": {"base": 1, "agentic": 1, "production": 1, "hiring": 1, "many_signals": 1, "many_signals_min": 6, "model_provider": 1},
    "buyer": {"base": 3, "unmet_need": +1, "internal_tooling": -1, "external_tool": -1},
    "confidence": {"high_min_claims": 6, "medium_min_claims": 3},
    "routing": {"partner_min_technical": 3, "min_technical": 3, "min_buyer_for_sales": 3},
}


@dataclass
class Evidence:
    """Counts of VERIFIED claims per category, plus two flags."""
    agentic: int = 0
    production: int = 0
    hiring: int = 0
    unmet_need: int = 0
    internal_tooling: int = 0
    external_tool: int = 0
    integration: int = 0
    verified_claims: int = 0
    competitive_claims: int = 0
    model_provider: bool = False


@dataclass
class Qualification:
    technical_fit: int
    buyer_fit: int
    evidence_confidence: str
    build_vs_buy_risk: str
    partnership_potential: bool
    classification: str
    reasons: List[str] = field(default_factory=list)


def _clamp(x: int) -> int:
    return max(1, min(5, x))


def technical_fit(e: Evidence, cfg: Dict = CONFIG) -> int:
    t = cfg["technical"]
    score = t["base"]
    score += t["agentic"] if e.agentic else 0
    score += t["production"] if e.production else 0
    score += t["hiring"] if e.hiring else 0
    score += t["model_provider"] if e.model_provider else 0
    score += t["many_signals"] if (e.agentic + e.production + e.hiring) >= t["many_signals_min"] else 0
    return _clamp(score)


def buyer_fit(e: Evidence, cfg: Dict = CONFIG) -> int:
    """Starts neutral: missing information is NOT negative evidence."""
    b = cfg["buyer"]
    score = b["base"]
    score += b["unmet_need"] if e.unmet_need else 0
    score += b["internal_tooling"] if e.internal_tooling else 0
    score += b["external_tool"] if e.external_tool else 0
    return _clamp(score)


def build_vs_buy_risk(e: Evidence) -> str:
    if e.competitive_claims == 0:
        return "unknown"  # we found nothing either way
    if e.internal_tooling:
        return "high"
    if e.external_tool or e.model_provider:
        return "medium"
    return "low"


def evidence_confidence(e: Evidence, cfg: Dict = CONFIG) -> str:
    c = cfg["confidence"]
    if e.verified_claims >= c["high_min_claims"] and e.competitive_claims >= 1:
        return "high"
    if e.verified_claims >= c["medium_min_claims"]:
        return "medium"
    return "low"


def classify(e: Evidence, cfg: Dict = CONFIG) -> Qualification:
    tech, buyer = technical_fit(e, cfg), buyer_fit(e, cfg)
    risk, conf = build_vs_buy_risk(e), evidence_confidence(e, cfg)
    partner = bool(e.integration) or (e.model_provider and tech >= cfg["routing"]["partner_min_technical"])
    r = cfg["routing"]
    reasons = [f"technical={tech}, buyer={buyer}, confidence={conf}, build_vs_buy={risk}, partnership={partner}"]

    if e.verified_claims == 0:
        label = "needs_more_research"
        reasons.append("No verified evidence at all.")
    elif conf == "low" and tech >= r["min_technical"]:
        label = "needs_more_research"
        reasons.append("Technically relevant but too little verified evidence to judge buying potential.")
    elif tech < r["min_technical"] and not e.integration:
        label = "not_qualified"
        reasons.append("Not enough verified agentic-AI activity.")
    elif partner and (buyer < r["min_buyer_for_sales"] or risk == "high" or (e.model_provider and not e.unmet_need)):
        label = "partnership_opportunity"
        reasons.append("Relevant ecosystem player, but build-vs-buy risk, weak buyer fit or no stated need argues against a direct sale.")
    elif buyer >= r["min_buyer_for_sales"] and risk != "high":
        label = "sales_opportunity"
        reasons.append("Verified technical fit and no blocking internal alternative.")
    elif buyer < r["min_buyer_for_sales"] and risk == "high":
        label = "not_qualified"
        reasons.append("Weak buyer fit, internal alternative and no partnership angle.")
    elif buyer < r["min_buyer_for_sales"]:
        label = "needs_more_research"
        reasons.append("An existing tool lowers buyer fit; validate how satisfied they are before deciding.")
    else:
        label = "needs_more_research"
        reasons.append("Mixed signals: validate before reaching out.")
    return Qualification(tech, buyer, conf, risk, partner, label, reasons)


def prioritisation_key(row: Dict):
    """Batch sort: commercial value first, technical fit only as tie-break."""
    order = {"sales_opportunity": 0, "partnership_opportunity": 1, "needs_more_research": 2, "not_qualified": 3}
    conf = {"high": 0, "medium": 1, "low": 2}
    return (order[row["account_classification"]], -row["buyer_fit"], conf[row["evidence_confidence"]], -row["technical_fit"])
