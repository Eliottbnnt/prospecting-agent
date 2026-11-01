"""
Prospecting Agent — built with LangChain + LangGraph
----------------------------------------------------
Give it a company name (or a list of companies). It researches the company
on the web, scores its fit for an "AI agents in production" platform like
LangChain/LangSmith, and drafts a personalised outreach email + LinkedIn note.

Usage:
    python agent.py "Mistral AI"
    python agent.py --file companies.txt          # batch mode -> results.csv
"""

import argparse
import csv
import os
import re
import sys
from datetime import date
from typing import List, TypedDict

import requests
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from prompts_v1 import ANALYZE_PROMPT, EMAIL_PROMPT, QUERIES_PROMPT, SELLER_CONTEXT, VERIFY_PROMPT

load_dotenv()

# --------------------------------------------------------------------------
# Model & search tool
# --------------------------------------------------------------------------
MODEL = os.getenv("MODEL", "anthropic:claude-haiku-4-5-20251001")
llm = init_chat_model(MODEL)


def get_search_tool():
    """Tavily if a key is set (better results), otherwise free DuckDuckGo."""
    if os.getenv("TAVILY_API_KEY"):
        from langchain_tavily import TavilySearch

        tool = TavilySearch(max_results=4)

        def run(q: str) -> List[dict]:
            res = tool.invoke({"query": q})
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
                for r in res.get("results", [])
            ]

        return run

    from langchain_community.tools import DuckDuckGoSearchResults

    from langchain_community.utilities import DuckDuckGoSearchAPIWrapper

    # The default "wt-wt" region sometimes hits a non-existent wt.wikipedia.org host,
    # so retry with explicit regions when a search fails.
    tools = [
        DuckDuckGoSearchResults(
            output_format="list", num_results=4, api_wrapper=DuckDuckGoSearchAPIWrapper(region=r)
        )
        for r in ("wt-wt", "fr-fr", "uk-en")
    ]

    def run(q: str) -> List[dict]:
        res, err = [], None
        for tool in tools:
            try:
                res = tool.invoke(q)
                break
            except Exception as e:  # network hiccups should not kill the run
                err = e
        else:
            print(f"  ! search failed for '{q}': {err}")
            return []
        return [
            {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
            for r in res
        ]

    return run


search = get_search_tool()


# --------------------------------------------------------------------------
# Structured outputs
# --------------------------------------------------------------------------
class SearchQueries(BaseModel):
    queries: List[str] = Field(description="3 short web search queries", min_length=1, max_length=4)


class AccountBrief(BaseModel):
    company_summary: str = Field(description="2 sentences: what the company does, size, market")
    ai_signals: List[str] = Field(description="Concrete signals of AI / LLM / agent initiatives, with dates if known")
    likely_pain_points: List[str] = Field(description="Problems they probably face taking AI agents to production")
    target_persona: str = Field(description="Best person to contact (title) and why")
    persona_job_titles: List[str] = Field(description="1-3 simple, common job titles to search for, e.g. 'VP Engineering', 'Head of AI'", max_length=3)
    fit_score: int = Field(ge=1, le=5, description="1 = no fit, 5 = perfect fit")
    fit_reasoning: str = Field(description="One sentence explaining the score")
    sources: List[str] = Field(description="URLs used as evidence")


class SignalCheck(BaseModel):
    signal: str = Field(description="The AI signal, copied exactly from the brief")
    supported: bool = Field(description="True only if the evidence explicitly states this signal")
    source_url: str = Field(default="", description="URL from the evidence that states it (empty if unsupported)")


class SignalChecks(BaseModel):
    checks: List[SignalCheck]


class Outreach(BaseModel):
    email_subject: str = Field(description="Max 7 words, no clickbait")
    email_body: str = Field(description="Max 120 words, plain text, one clear CTA")
    linkedin_note: str = Field(description="Max 300 characters")


# --------------------------------------------------------------------------
# Graph state
# --------------------------------------------------------------------------
class State(TypedDict, total=False):
    company: str
    queries: List[str]
    findings: List[dict]
    brief: AccountBrief
    outreach: Outreach
    contacts: List[dict]


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------
def plan_queries(state: State) -> State:
    out = llm.with_structured_output(SearchQueries).invoke(
        QUERIES_PROMPT.format(company=state["company"], today=date.today().isoformat())
    )
    print(f"  > searching: {out.queries}")
    return {"queries": out.queries}


def research(state: State) -> State:
    findings = []
    for q in state["queries"]:
        findings.extend(search(q))
    # de-duplicate by URL
    seen, unique = set(), []
    for f in findings:
        if f["url"] and f["url"] not in seen:
            seen.add(f["url"])
            unique.append(f)
    print(f"  > {len(unique)} sources found")
    return {"findings": unique}


def analyze(state: State) -> State:
    evidence = "\n".join(f"- {f['title']} ({f['url']}): {f['snippet']}" for f in state["findings"]) or "No results."
    brief = llm.with_structured_output(AccountBrief).invoke(
        ANALYZE_PROMPT.format(company=state["company"], evidence=evidence, seller=SELLER_CONTEXT)
    )
    print(f"  > fit score: {brief.fit_score}/5")
    return {"brief": brief}


def verify(state: State) -> State:
    """Fact-check the brief: keep only AI signals the evidence really supports, and only real URLs."""
    b = state["brief"]
    urls = {f["url"] for f in state["findings"]}
    evidence = "\n".join(f"- {f['title']} ({f['url']}): {f['snippet']}" for f in state["findings"]) or "No results."
    result = llm.with_structured_output(SignalChecks).invoke(
        VERIFY_PROMPT.format(company=state["company"], evidence=evidence, signals="\n".join(f"- {x}" for x in b.ai_signals))
    )
    verified = [f"{c.signal} ({c.source_url})" for c in result.checks if c.supported and c.source_url in urls]
    sources = [u for u in dict.fromkeys(b.sources) if u in urls]
    score = b.fit_score if verified else min(b.fit_score, 2)  # no verified AI signal -> no outreach
    print(f"  > verified {len(verified)}/{len(b.ai_signals)} AI signals, {len(sources)}/{len(b.sources)} sources kept")
    return {"brief": b.model_copy(update={"ai_signals": verified, "sources": sources, "fit_score": score})}


LUSHA_URL = "https://api.lusha.com/v3/contacts/prospecting"
# Highest first: we want decision makers, never interns or juniors.
SENIORITY = [("chief", "ceo", "cto", "cio", "cpo", "founder", "président", "president"),
             ("vp ", "vice president", "vice-president", "svp", "evp"),
             ("head", "director", "directeur", "directrice"),
             ("lead", "principal", "manager", "responsable")]
EXCLUDED = ("intern", "stagiaire", "stage", "apprenti", "alternan", "student", "assistant", "junior", "trainee")


def seniority_rank(title: str):
    """0 = C-level/founder ... 3 = lead/manager, None = excluded or not senior enough."""
    t = f" {title.lower()} "
    if any(w in t for w in EXCLUDED):
        return None
    for rank, words in enumerate(SENIORITY):
        if any(w in t for w in words):
            return rank
    return None


def find_contacts(state: State) -> State:
    """Optional: look up real people (name, title, LinkedIn) at the company with Lusha.
    Only runs if LUSHA_API_KEY is set. Never guesses: no match -> empty list."""
    key = os.getenv("LUSHA_API_KEY")
    if not key:
        return {"contacts": []}
    company = state["company"]
    # Lusha may list "Mistral AI" as "Mistral": also try the name without generic suffixes
    stripped = re.sub(r"\s+(ai|inc\.?|sas|sa|group|labs?|technologies)$", "", company, flags=re.I).strip()
    names = list(dict.fromkeys([company, stripped]))
    # persona titles first, then broader fallbacks: exact titles often match nobody
    attempts = [state["brief"].persona_job_titles, ["VP Engineering", "Head of Engineering", "CTO"]]
    results = []
    for titles in attempts:
        try:
            r = requests.post(
                LUSHA_URL,
                headers={"api_key": key, "Content-Type": "application/json"},
                json={
                    "pagination": {"page": 0, "size": 10},  # smallest page = fewest credits
                    "filters": {
                        "contacts": {"include": {"jobTitles": titles}},
                        "companies": {"include": {"names": names}},
                    },
                    "options": {"maxContactsPerCompany": 10},
                },
                timeout=30,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:  # never kill the run for an enrichment problem
            print(f"  ! Lusha lookup failed: {e}")
            return {"contacts": []}
        if any(seniority_rank((c.get("jobTitle") or {}).get("title", "")) is not None for c in results):
            break
    contacts = []
    for c in results:
        # keep only people whose company really matches (names like "Alan" are ambiguous)
        cname = (c.get("company") or {}).get("name", "").lower()
        if not any(n.lower() in cname or cname in n.lower() for n in names):
            continue
        title = (c.get("jobTitle") or {}).get("title", "")
        rank = seniority_rank(title)
        if rank is None:
            continue
        contacts.append((rank, {
            "name": f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
            "title": title,
            "linkedin": (c.get("socialLinks") or {}).get("linkedin", ""),
        }))
    contacts = [c for _, c in sorted(contacts, key=lambda x: x[0])]  # most senior first
    print(f"  > Lusha: {len(contacts)} senior contact(s) found")
    return {"contacts": contacts[:3]}


def write_outreach(state: State) -> State:
    b = state["brief"]
    out = llm.with_structured_output(Outreach).invoke(
        EMAIL_PROMPT.format(company=state["company"], brief=b.model_dump_json(indent=2), seller=SELLER_CONTEXT)
    )
    return {"outreach": out}


def route_on_fit(state: State) -> str:
    """Only spend time on outreach for accounts that are worth it."""
    return "find_contacts" if state["brief"].fit_score >= 3 else END


# --------------------------------------------------------------------------
# Build the graph:  plan -> research -> analyze -> verify -> (fit >= 3 ? contacts -> write : end)
# --------------------------------------------------------------------------
def build_graph():
    g = StateGraph(State)
    g.add_node("plan_queries", plan_queries)
    g.add_node("research", research)
    g.add_node("analyze", analyze)
    g.add_node("verify", verify)
    g.add_node("find_contacts", find_contacts)
    g.add_node("write_outreach", write_outreach)
    g.add_edge(START, "plan_queries")
    g.add_edge("plan_queries", "research")
    g.add_edge("research", "analyze")
    g.add_edge("analyze", "verify")
    g.add_conditional_edges("verify", route_on_fit, ["find_contacts", END])
    g.add_edge("find_contacts", "write_outreach")
    g.add_edge("write_outreach", END)
    return g.compile()


graph = build_graph()


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
def to_markdown(state: State) -> str:
    b, o = state["brief"], state.get("outreach")
    md = [f"# {state['company']} — Account brief", "",
          f"**Fit score:** {b.fit_score}/5 — {b.fit_reasoning}", "",
          "## Company", b.company_summary, "",
          "## AI signals", *[f"- {s}" for s in b.ai_signals], "",
          "## Likely pain points", *[f"- {p}" for p in b.likely_pain_points], "",
          "## Who to contact", b.target_persona, ""]
    if state.get("contacts"):
        md += ["### Named contacts (Lusha)",
               *[f"- {c['name']} — {c['title']} {c['linkedin']}".rstrip() for c in state["contacts"]], ""]
    if o:
        md += ["## Email", f"**Subject:** {o.email_subject}", "", o.email_body, "",
               "## LinkedIn note", o.linkedin_note, ""]
    else:
        md += ["_Low fit: no outreach drafted._", ""]
    md += ["## Sources", *[f"- {s}" for s in b.sources]]
    return "\n".join(md)


def run_one(company: str) -> State:
    print(f"\n=== {company} ===")
    return graph.invoke({"company": company})


def main():
    p = argparse.ArgumentParser(description="LangGraph prospecting agent")
    p.add_argument("company", nargs="?", help="Company name")
    p.add_argument("--file", help="Text file with one company per line (batch mode)")
    args = p.parse_args()

    os.makedirs("output", exist_ok=True)

    if args.file:
        companies = [l.strip() for l in open(args.file, encoding="utf-8") if l.strip()]
        rows = []
        for c in companies:
            s = run_one(c)
            b, o = s["brief"], s.get("outreach")
            rows.append({"company": c, "fit_score": b.fit_score, "persona": b.target_persona,
                         "contacts": "; ".join(f"{c['name']} ({c['title']})" for c in s.get("contacts", [])),
                         "reason": b.fit_reasoning, "subject": o.email_subject if o else "",
                         "email": o.email_body if o else ""})
            open(f"output/{c.replace(' ', '_')}.md", "w", encoding="utf-8").write(to_markdown(s))
        rows.sort(key=lambda r: r["fit_score"], reverse=True)  # prioritised list
        with open("output/results.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        print("\nDone -> output/results.csv (sorted by fit score)")
    elif args.company:
        s = run_one(args.company)
        md = to_markdown(s)
        path = f"output/{args.company.replace(' ', '_')}.md"
        open(path, "w", encoding="utf-8").write(md)
        print("\n" + md + f"\n\nSaved -> {path}")
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
