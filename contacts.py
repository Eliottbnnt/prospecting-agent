"""Optional Lusha lookup: real people (name, title, LinkedIn) at a company. Never guesses, never reveals emails/phones."""

import os
import re
from typing import List

import requests

LUSHA_URL = "https://api.lusha.com/v3/contacts/prospecting"
# Highest first: we want decision makers, never interns or juniors.
SENIORITY = [("chief", "ceo", "cto", "cio", "cpo", "founder", "président", "president"),
             ("vp ", "vice president", "vice-president", "svp", "evp"),
             ("head", "director", "directeur", "directrice", "developer relations", "partnership"),
             ("lead", "principal", "manager", "responsable")]
EXCLUDED = ("intern", "stagiaire", "stage", "apprenti", "alternan", "student", "assistant", "junior", "trainee")
# Fallback titles when the persona's own titles match nobody
FALLBACK_TITLES = {
    "sales": ["VP Engineering", "Head of Engineering", "CTO"],
    "partnership": ["Head of Developer Relations", "Head of Partnerships", "Director of Partnerships"],
}


def seniority_rank(title: str):
    """0 = C-level/founder ... 3 = lead/manager, None = excluded or not senior enough."""
    t = f" {title.lower()} "
    if any(w in t for w in EXCLUDED):
        return None
    for rank, words in enumerate(SENIORITY):
        if any(w in t for w in words):
            return rank
    return None


def find_contacts(company: str, persona_titles: List[str], mode: str = "sales") -> List[dict]:
    """Returns up to 3 senior contacts; [] if no key, no match or any error."""
    key = os.getenv("LUSHA_API_KEY")
    if not key:
        return []
    # Lusha may list "Mistral AI" as "Mistral": also try the name without generic suffixes
    stripped = re.sub(r"\s+(ai|inc\.?|sas|sa|group|labs?|technologies)$", "", company, flags=re.I).strip()
    names = list(dict.fromkeys([company, stripped]))
    results = []
    for titles in (persona_titles, FALLBACK_TITLES.get(mode, FALLBACK_TITLES["sales"])):
        try:
            r = requests.post(
                LUSHA_URL,
                headers={"api_key": key, "Content-Type": "application/json"},
                json={
                    "pagination": {"page": 0, "size": 10},  # smallest page = fewest credits
                    "filters": {"contacts": {"include": {"jobTitles": titles}},
                                "companies": {"include": {"names": names}}},
                    "options": {"maxContactsPerCompany": 10},
                },
                timeout=30,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:  # never kill the run for an enrichment problem
            print(f"  ! Lusha lookup failed: {e}")
            return []
        if any(seniority_rank((c.get("jobTitle") or {}).get("title", "")) is not None for c in results):
            break
    found = []
    for c in results:
        cname = (c.get("company") or {}).get("name", "").lower()
        if not any(n.lower() in cname or cname in n.lower() for n in names):
            continue  # names like "Alan" are ambiguous
        title = (c.get("jobTitle") or {}).get("title", "")
        rank = seniority_rank(title)
        if rank is None:
            continue
        found.append((rank, {"name": f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
                             "title": title, "linkedin": (c.get("socialLinks") or {}).get("linkedin", "")}))
    return [c for _, c in sorted(found, key=lambda x: x[0])][:3]
