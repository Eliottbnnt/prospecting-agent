"""
Evidence tracking and deterministic checks (no LLM, no network).

An LLM proposes claims WITH a verbatim excerpt and a URL. Here we check, in plain code, that the URL was
really retrieved and that the excerpt really appears in what was retrieved. A claim that fails is never
used for scoring or outreach.
"""

import re
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

Category = Literal["agentic_ai", "production", "hiring", "unmet_need", "internal_tooling",
                   "external_tool", "integration", "company_fact", "other"]
Status = Literal["supported", "partially_supported", "unsupported", "unverifiable"]


class RawClaim(BaseModel):
    claim: str = Field(description="One factual statement about the company")
    category: Category
    source_url: str = Field(description="Exact URL of the search result that states it")
    excerpt: str = Field(description="Short VERBATIM excerpt from that result supporting the claim")
    event_date: Optional[str] = Field(default=None, description="Date of the event itself, only if the excerpt states it")


class RawClaims(BaseModel):
    claims: List[RawClaim]


class Claim(RawClaim):
    id: int
    source_title: str = ""
    source_type: str = "unknown"  # official | job_board | reference | third_party
    status: Status = "unverifiable"
    reason: str = ""
    publication_date: Optional[str] = None  # search snippets rarely carry it: we say so instead of guessing
    retrieved_at: str = ""


def _norm(text: str) -> str:
    text = re.sub(r"[‘’“”\"'`]", "", text.lower())
    text = re.sub(r"\.\.\.|…", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def source_type(url: str, company: str) -> str:
    host = urlparse(url).netloc.lower()
    slug = re.sub(r"[^a-z0-9]", "", company.lower().split()[0]) if company.split() else ""
    if slug and slug in re.sub(r"[^a-z0-9]", "", host):
        return "official"
    if any(j in host for j in ("lever.co", "greenhouse.io", "ashbyhq", "workable", "welcometothejungle", "linkedin.com/jobs")) \
            or "jobs" in host or "careers" in host:
        return "job_board"
    if "wikipedia" in host or "crunchbase" in host:
        return "reference"
    return "third_party"


def validate_claims(raw: List[RawClaim], findings: List[dict], company: str, now: Optional[str] = None) -> List[Claim]:
    """Deterministic check of every proposed claim against what was actually retrieved."""
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    by_url: Dict[str, dict] = {f["url"]: f for f in findings if f.get("url")}
    out: List[Claim] = []
    for i, r in enumerate(raw, start=1):
        c = Claim(**r.model_dump(), id=i, retrieved_at=now)
        f = by_url.get(r.source_url)
        if not f:
            c.status, c.reason = "unsupported", "URL was not among the retrieved results"
        elif not r.excerpt.strip():
            c.status, c.reason = "unverifiable", "No excerpt given"
        else:
            c.source_title, c.source_type = f.get("title", ""), source_type(r.source_url, company)
            haystack, needle = _norm(f.get("title", "") + " " + f.get("snippet", "")), _norm(r.excerpt)
            if needle and needle in haystack:
                c.status, c.reason = "supported", "Excerpt found verbatim in the retrieved content"
            else:
                words = set(needle.split())
                overlap = len(words & set(haystack.split())) / max(1, len(words))
                if overlap >= 0.8:
                    c.status, c.reason = "partially_supported", f"Excerpt only approximately matches ({overlap:.0%} of words)"
                else:
                    c.status, c.reason = "unsupported", "Excerpt not found in the retrieved content"
        # An event date is only kept if the excerpt itself states it (never inferred from an article date).
        if c.event_date and _norm(c.event_date) not in _norm(r.excerpt):
            c.reason += "; event date dropped (not stated in the excerpt)"
            c.event_date = None
        out.append(c)
    return out


def verified(claims: List[Claim]) -> List[Claim]:
    """Only fully supported claims may be used for scoring and outreach."""
    return [c for c in claims if c.status == "supported"]


# --------------------------------------------------------------------------
# Outreach review (deterministic)
# --------------------------------------------------------------------------
BANNED_PHRASES = ("becomes critical", "as you scale", "game-changer", "revolutionize", "cutting-edge",
                  "leverage", "synergy", "unlock", "supercharge")


def review_outreach(subject: str, body: str, linkedin: str, claims_used: List[int], claims: List[Claim]) -> List[str]:
    """Return a list of problems (empty = passed)."""
    issues: List[str] = []
    words = len(body.split())
    if not 60 <= words <= 130:
        issues.append(f"Email is {words} words; aim for 70-120.")
    if "?" not in body:
        issues.append("No clear CTA phrased as a question.")
    if "eliott bonnet" not in body.lower():
        issues.append("Email must be signed 'Eliott Bonnet'.")
    if len(linkedin) > 300:
        issues.append("LinkedIn note is over 300 characters.")
    low = (subject + " " + body + " " + linkedin).lower()
    issues += [f"Generic/hype phrase: '{p}'." for p in BANNED_PHRASES if p in low]
    ok = {c.id: c for c in verified(claims)}
    if not claims_used:
        issues.append("No verified claim cited.")
    bad = [i for i in claims_used if i not in ok]
    if bad:
        issues.append(f"Cites claims that are not verified: {bad}.")
    # Every number in the message must come from a cited claim (15-minute call style numbers are allowed).
    source_text = " ".join(ok[i].claim + " " + ok[i].excerpt for i in claims_used if i in ok)
    for num in re.findall(r"\d[\d,.]*", body + " " + subject):
        n = num.strip(".,")
        if n in ("15", "20", "30") and re.search(rf"{n}[- ]?min", low):
            continue
        if n not in source_text:
            issues.append(f"Number '{n}' is not in the cited claims.")
    return issues
