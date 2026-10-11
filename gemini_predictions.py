"""API-free football fixture discovery and pre-match analysis using Gemini web search.

This module does NOT call a football-data API. Gemini's Google Search grounding
finds public web information. Results are best-effort and must not be treated as
verified odds or guaranteed predictions.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from google import genai

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CACHE_SECONDS = int(os.getenv("GEMINI_PREDICTIONS_CACHE_SECONDS", "900"))
_cache = {"key": None, "at": 0, "value": None}
WAT = timezone(timedelta(hours=1))

SYSTEM_RULES = """You are SportyTips, a cautious pre-match football analyst.
Use Google Search grounding to find upcoming football fixtures and research
available public historical evidence. Never claim to have verified information
that you did not find. Do not invent fixtures, kickoff times, statistics, injuries,
lineups, odds, or sources. Only include matches that have NOT started and are
scheduled for the requested Nigeria (WAT) calendar date. Prefer reliable sources.
For each match, use exactly ONE main market, or return NO PICK if evidence is weak.
Allowed markets include outright winner/1UP, positive handicap, over goals, BTTS,
team goals, corners, and shots only when credible data supports that market.
Never recommend Draw No Bet, double chance, any under market, yellow cards, or
negative handicaps. Do not state or imply any pick is guaranteed. Avoid claiming
70% accuracy without verified tracked results. Return valid JSON only, using this
schema: {"date_wat":"YYYY-MM-DD","matches":[{"home":"","away":"","competition":"","kickoff_wat":"YYYY-MM-DD HH:MM","status":"upcoming","market":"","selection":"","confidence":0,"reason":"","evidence":[{"claim":"","source":"https://..."}],"no_pick":false}],"notes":""}. Confidence is an evidence-strength score from 1 to 100, not a guaranteed probability. Keep source URLs real and relevant. If a field cannot be verified, say so or omit that match."""


def get_upcoming_predictions(date_wat=None, force_refresh=False):
    """Return grounded pre-match predictions for the requested WAT date."""
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is not configured")
    date_wat = date_wat or datetime.now(WAT).strftime("%Y-%m-%d")
    cache_key = date_wat
    if (not force_refresh and _cache["key"] == cache_key and
            time.time() - _cache["at"] < CACHE_SECONDS and _cache["value"] is not None):
        return _cache["value"]

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    prompt = f"""Today's requested date in Nigeria (WAT) is {date_wat}. Current Nigeria time is {datetime.now(WAT).strftime('%Y-%m-%d %H:%M')} WAT.
Search the web for upcoming association-football fixtures on this date, across competitions where you can verify reliable fixture details. Then research recent form, home/away performance, goals and relevant head-to-head evidence for a selective shortlist of up to 10 matches. Use only pre-match information available now. Prefer fewer well-supported picks over filling the list. Do not use bookmaker odds to decide the prediction. Return the required JSON object only."""
    response = client.models.generate_content(
        model=MODEL,
        contents=SYSTEM_RULES + "\n\n" + prompt,
        config={"tools": [{"google_search": {}}], "temperature": 0.2},
    )
    text = (getattr(response, "text", None) or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("Gemini did not return valid JSON. Try again later.")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
        raise RuntimeError("Gemini returned an unexpected prediction format.")
    # Guardrails: reject clearly malformed entries and forbidden markets.
    forbidden = ("draw no bet", "double chance", "under ", "yellow card", "handicap -")
    clean = []
    for item in data["matches"]:
        if not isinstance(item, dict):
            continue
        market = str(item.get("market", "")).strip()
        selection = str(item.get("selection", "")).strip()
        if any(word in (market + " " + selection).lower() for word in forbidden):
            item["market"] = "NO PICK"
            item["selection"] = "NO PICK"
            item["no_pick"] = True
        try:
            item["confidence"] = max(1, min(100, int(item.get("confidence", 1))))
        except (TypeError, ValueError):
            item["confidence"] = 1
        item["home"] = str(item.get("home", ""))[:100]
        item["away"] = str(item.get("away", ""))[:100]
        item["reason"] = str(item.get("reason", ""))[:1000]
        clean.append(item)
    data["matches"] = clean[:10]
    data["date_wat"] = date_wat
    data["generated_at_wat"] = datetime.now(WAT).strftime("%Y-%m-%d %H:%M:%S")
    _cache.update(key=cache_key, at=time.time(), value=data)
    return data
