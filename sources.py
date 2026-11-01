"""Web search: Tavily if a key is set, otherwise free DuckDuckGo (with region fallbacks)."""

import os
from typing import List


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
