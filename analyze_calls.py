#!/usr/bin/env python3
"""
Daily call quality analysis script for Velocity Shipping AI voice agent calls.
Analyzes Vani/Anjana call quality across 8 QA dimensions using Claude.

Usage:
    python analyze_calls.py
    python analyze_calls.py --date 2025-05-01
    python analyze_calls.py --date 2025-05-01 --call-type oc --limit 50
    python analyze_calls.py --call-type ndr --limit 50
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from typing import Optional

import anthropic
import requests
from dotenv import load_dotenv

# Google Sheets is written via a deployed Apps Script webhook (no GCP credentials needed)

load_dotenv()

MODEL = "claude-sonnet-4-6"

# Minimum call duration (seconds) to be worth analysing
MIN_DURATION_SECONDS = 10

# agents.key → human-readable call type
AGENT_KEY_TO_CALL_TYPE = {
    "order_and_address_confirmation": "Order Confirmation",
    "ndr_management": "NDR",
}

# CLI --call-type shortcuts → agents.key filter value
CALL_TYPE_FILTER = {
    "oc":  "order_and_address_confirmation",
    "ndr": "ndr_management",
    "all": None,  # no filter
}

STATUS_MAP = {3: "completed", 5: "voicemail", 2: "busy", 4: "failed"}

DIMENSIONS = [
    "opening_first_impression",
    "pacing_pause_handling",
    "interruption_handling",
    "language_adaptability",
    "script_naturalness_tone",
    "objection_confusion_handling",
    "call_closure",
    "overall_effectiveness",
]

DIMENSION_LABELS = {
    "opening_first_impression":     "Opening & First Impression",
    "pacing_pause_handling":        "Pacing & Pause Handling",
    "interruption_handling":        "Interruption Handling",
    "language_adaptability":        "Language Adaptability",
    "script_naturalness_tone":      "Script Naturalness & Tone",
    "objection_confusion_handling": "Objection & Confusion Handling",
    "call_closure":                 "Call Closure",
    "overall_effectiveness":        "Overall Effectiveness",
}

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily call quality analysis for Velocity Shipping AI voice agents"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=date.today().isoformat(),
        help="Date to analyze (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--call-type",
        type=str,
        default="all",
        choices=["oc", "ndr", "all"],
        help="Filter by call type: oc (Order Confirmation), ndr (NDR), all. Default: all.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of calls to analyze. Default: all.",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Metabase API
# ─────────────────────────────────────────────────────────────────────────────

METABASE_DB_ID = 31


def get_metabase_headers() -> tuple[dict, str]:
    base_url = os.environ["METABASE_URL"].rstrip("/")
    api_key = os.environ.get("METABASE_API_KEY", "")

    if api_key:
        return {"X-API-KEY": api_key, "Content-Type": "application/json"}, base_url

    username = os.environ["METABASE_USERNAME"]
    password = os.environ["METABASE_PASSWORD"]
    resp = requests.post(
        f"{base_url}/api/session",
        json={"username": username, "password": password},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()["id"]
    return {"X-Metabase-Session": token, "Content-Type": "application/json"}, base_url


def _metabase_query(headers: dict, base_url: str, sql: str) -> list[dict]:
    resp = requests.post(
        f"{base_url}/api/dataset",
        headers=headers,
        json={
            "database": METABASE_DB_ID,
            "type": "native",
            "native": {"query": sql},
            "parameters": [],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})

    cols = [c["name"] for c in data.get("cols", [])]
    rows = data.get("rows", [])

    results = []
    for row in rows:
        record = dict(zip(cols, row))
        if "goal_values" in record and isinstance(record["goal_values"], str):
            try:
                record["goal_values"] = json.loads(record["goal_values"])
            except json.JSONDecodeError:
                pass
        if "status" in record and record["status"] is not None:
            try:
                record["status"] = int(record["status"])
            except (ValueError, TypeError):
                pass
        results.append(record)

    return results


def fetch_calls(
    headers: dict,
    base_url: str,
    analysis_date: str,
    agent_key_filter: Optional[str],
    limit: Optional[int],
) -> list[dict]:
    """
    Fetch completed calls for the given date, joined with agents for call type.
    Smart filters applied at query time:
      - status = 3 (completed only — drops voicemail/busy/failed)
      - duration > MIN_DURATION_SECONDS (drops sub-10s dead-air calls)
      - transcript not null/empty
    """
    agent_key_clause = ""
    if agent_key_filter:
        # safe: value comes from our own CALL_TYPE_FILTER dict, not user input
        agent_key_clause = f"AND a.key = '{agent_key_filter}'"

    limit_clause = f"LIMIT {limit}" if limit else ""

    sql = f"""
        SELECT
            c.id,
            c.status,
            c.duration,
            c.transcript,
            c.summary,
            c.goal_values,
            c.recording_url,
            c.initiated_at,
            c.answered_at,
            c.completed_at,
            a.key        AS agent_key,
            a.agent_name,
            a.primary_language AS agent_primary_language
        FROM calls c
        LEFT JOIN agents a ON c.agent_id = a.id
        WHERE DATE(c.initiated_at) = '{analysis_date}'
          AND c.transcript IS NOT NULL
          AND TRIM(c.transcript) <> ''
          AND c.status = 3
          AND c.duration IS NOT NULL
          AND c.duration ~ '^[0-9]+(\\.[0-9]+)?$'
          AND CAST(c.duration AS FLOAT) > {MIN_DURATION_SECONDS}
          {agent_key_clause}
        ORDER BY c.initiated_at
        {limit_clause}
    """
    return _metabase_query(headers, base_url, sql)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_call_type(call: dict) -> str:
    """Determine call type from agents.key (joined in SQL)."""
    agent_key = call.get("agent_key") or ""
    return AGENT_KEY_TO_CALL_TYPE.get(agent_key, f"Unknown ({agent_key})" if agent_key else "Unknown")


def parse_goal_values(raw) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def format_goal_values(raw) -> str:
    items = parse_goal_values(raw)
    if not items:
        return "  (no goal data)"
    lines = []
    for gv in items:
        if isinstance(gv, dict):
            key = gv.get("key", "unknown")
            value = gv.get("value", "unknown")
            lines.append(f"  - {key}: {value}")
    return "\n".join(lines) if lines else "  (empty)"


def strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner.rsplit("```", 1)[0].strip()
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Per-call analysis
# ─────────────────────────────────────────────────────────────────────────────

PER_CALL_SYSTEM = """\
You are a senior QA analyst at Velocity Shipping reviewing AI voice agent calls.
The agent (Vani or Anjana) makes outbound calls in Hindi, English, or Hinglish for:
  - Order confirmation
  - NDR (non-delivery resolution)
  - COD-to-prepaid conversion

You review transcripts exactly as a human QA analyst would — critically, fairly, with specific \
observations grounded in the transcript text.

IMPORTANT:
- Goal outcomes from goal_values are ALREADY DETERMINED. Do NOT re-evaluate them. Use them as context only.
- Evaluate the PROCESS and DELIVERY of the call across the 8 dimensions below.
- If the customer never meaningfully engaged (no answer, voicemail, immediate hang-up), set \
  no_contact = true and score all dimensions null.

Scoring guide per dimension (1–5):
  5 = Excellent, 4 = Good, 3 = Acceptable, 2 = Needs Work, 1 = Poor

Dimensions to evaluate:
  1. opening_first_impression
     - Was the intro clear (name + brand + purpose)?
     - Did it start with an ambiguous "Hello? जी बताइये?" making it sound inbound?
     - Did it pause awkwardly before launching into the script?

  2. pacing_pause_handling
     - How many times did it loop "Hello? are you there?" — was that appropriate?
     - Did it rush through addresses, product names, or PINs?
     - Did it read PINs as individual digits ("five six zero zero") instead of naturally?

  3. interruption_handling
     - When the customer spoke mid-sentence, did the agent acknowledge or barrel through?

  4. language_adaptability
     - Did it detect Hindi/English preference and match the customer?
     - Did it switch languages when the customer switched?
     - Were there language mismatches?

  5. script_naturalness_tone
     - Did it say "I am now ending the call" literally?
     - Did it repeat "Thank you for confirming" 3+ times?
     - Did the transcript cut off mid-sentence?

  6. objection_confusion_handling
     - When the customer complained or asked something, did the agent address it or deflect?

  7. call_closure
     - Was the closing graceful and did it confirm next steps?
     - Or was it abrupt / robotic?

  8. overall_effectiveness
     - Was there a pivot moment that could have changed a "Na" goal to "Confirmed"?

Return ONLY valid JSON — no markdown fences, no text outside the JSON object.\
"""

PER_CALL_PROMPT_TEMPLATE = """\
Analyze this AI voice agent call.

## Metadata
- Call ID      : {call_id}
- Status       : {status}
- Duration     : {duration} seconds
- Call Type    : {call_type}
- Agent Name   : {agent_name}
- Agent Lang   : {agent_language}
- Initiated    : {initiated_at}
- Answered     : {answered_at}
- Completed    : {completed_at}

## Goal Outcomes (context only — do NOT re-evaluate)
{goal_values_str}

## Auto-generated Summary
{summary}

## Transcript
{transcript}

---

Return ONLY this JSON (no markdown):

{{
  "overall_quality": "good|average|poor",
  "overall_score": <integer 1-10>,
  "no_contact": <true if customer never meaningfully engaged>,
  "dimensions": {{
    "opening_first_impression": {{
      "score": <1-5 or null if no_contact>,
      "observation": "<specific transcript-grounded observation>",
      "fix": "<one actionable fix for the prompt/flow engineer>"
    }},
    "pacing_pause_handling": {{
      "score": <1-5 or null>,
      "observation": "<observation>",
      "fix": "<fix>"
    }},
    "interruption_handling": {{
      "score": <1-5 or null>,
      "observation": "<observation>",
      "fix": "<fix>"
    }},
    "language_adaptability": {{
      "score": <1-5 or null>,
      "observation": "<observation>",
      "fix": "<fix>"
    }},
    "script_naturalness_tone": {{
      "score": <1-5 or null>,
      "observation": "<observation>",
      "fix": "<fix>"
    }},
    "objection_confusion_handling": {{
      "score": <1-5 or null>,
      "observation": "<observation>",
      "fix": "<fix>"
    }},
    "call_closure": {{
      "score": <1-5 or null>,
      "observation": "<observation>",
      "fix": "<fix>"
    }},
    "overall_effectiveness": {{
      "score": <1-5 or null>,
      "observation": "<observation — note any missed pivot moments>",
      "fix": "<fix>"
    }}
  }},
  "top_issues": ["<issue 1>", "<issue 2>", "<issue 3>"],
  "best_moments": ["<moment 1>"],
  "key_recommendation": "<single most impactful change for this call>"
}}
"""


def analyze_call(client: anthropic.Anthropic, call: dict) -> dict:
    call_id = str(call["id"])
    status_str = STATUS_MAP.get(call.get("status"), f"unknown({call.get('status')})")
    call_type = get_call_type(call)

    prompt = PER_CALL_PROMPT_TEMPLATE.format(
        call_id=call_id,
        status=status_str,
        duration=call.get("duration") or "N/A",
        call_type=call_type,
        agent_name=call.get("agent_name") or "N/A",
        agent_language=call.get("agent_primary_language") or "N/A",
        initiated_at=call.get("initiated_at") or "N/A",
        answered_at=call.get("answered_at") or "N/A",
        completed_at=call.get("completed_at") or "N/A",
        goal_values_str=format_goal_values(call.get("goal_values")),
        summary=call.get("summary") or "(no summary)",
        transcript=call.get("transcript") or "(no transcript)",
    )

    base_result = {
        "call_id": call_id,
        "call_type": call_type,
        "agent_key": call.get("agent_key"),
        "agent_name": call.get("agent_name"),
        "status": status_str,
        "duration": call.get("duration"),
        "recording_url": call.get("recording_url"),
        "initiated_at": str(call.get("initiated_at") or ""),
    }

    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=2048,
            system=PER_CALL_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            final = stream.get_final_message()

        text = next((b.text for b in final.content if b.type == "text"), "")
        text = strip_markdown_fences(text)
        analysis = json.loads(text)
        return {**base_result, **analysis}

    except json.JSONDecodeError as e:
        return {
            **base_result,
            "error": f"JSON parse error: {e}",
            "overall_quality": "error",
            "overall_score": 0,
            "no_contact": False,
            "dimensions": {},
            "top_issues": [],
            "best_moments": [],
            "key_recommendation": "",
        }
    except Exception as e:
        return {
            **base_result,
            "error": str(e),
            "overall_quality": "error",
            "overall_score": 0,
            "no_contact": False,
            "dimensions": {},
            "top_issues": [],
            "best_moments": [],
            "key_recommendation": "",
        }


# ─────────────────────────────────────────────────────────────────────────────
# EOD Summary Report
# ─────────────────────────────────────────────────────────────────────────────

SUMMARY_SYSTEM = """\
You are the QA lead at Velocity Shipping. You receive individual call analyses and write a \
consolidated end-of-day QA report for the engineering and operations teams.

Be specific and actionable. Cluster similar issues semantically (do not just list them verbatim). \
Prioritize fixes by impact × effort (highest impact + lowest effort = top priority).

Return ONLY valid JSON — no markdown fences, no text outside the JSON object.\
"""

SUMMARY_PROMPT_TEMPLATE = """\
Generate an end-of-day QA report for {date}.

## Aggregate Statistics
{stats_json}

## All Individual Analyses
{analyses_json}

---

Return ONLY this JSON:

{{
  "date": "{date}",
  "executive_summary": "<3-4 sentences summarising the day's call quality, key wins, and main pain points>",
  "call_volume": {{
    "total": <int>,
    "no_contact": <int>,
    "contactable": <int>,
    "good": <int>,
    "average": <int>,
    "poor": <int>
  }},
  "dimension_scorecard": {{
    "opening_first_impression":     {{"avg_score": <float|null>, "trend": "strong|ok|weak", "note": "<one-line insight>"}},
    "pacing_pause_handling":        {{"avg_score": <float|null>, "trend": "strong|ok|weak", "note": "<one-line insight>"}},
    "interruption_handling":        {{"avg_score": <float|null>, "trend": "strong|ok|weak", "note": "<one-line insight>"}},
    "language_adaptability":        {{"avg_score": <float|null>, "trend": "strong|ok|weak", "note": "<one-line insight>"}},
    "script_naturalness_tone":      {{"avg_score": <float|null>, "trend": "strong|ok|weak", "note": "<one-line insight>"}},
    "objection_confusion_handling": {{"avg_score": <float|null>, "trend": "strong|ok|weak", "note": "<one-line insight>"}},
    "call_closure":                 {{"avg_score": <float|null>, "trend": "strong|ok|weak", "note": "<one-line insight>"}},
    "overall_effectiveness":        {{"avg_score": <float|null>, "trend": "strong|ok|weak", "note": "<one-line insight>"}}
  }},
  "issues_breakdown": [
    {{
      "cluster": "<semantic cluster name>",
      "count": <int>,
      "severity": "high|medium|low",
      "example": "<representative verbatim or paraphrased example from calls>",
      "root_cause": "<why this pattern occurs in the agent>",
      "prompt_fix": "<specific instruction for the prompt/flow engineer to fix this>"
    }}
  ],
  "what_went_well": ["<observation 1>", "<observation 2>", "<observation 3>"],
  "top_3_priority_fixes": [
    {{
      "rank": 1,
      "fix": "<clear description of the change>",
      "impact": "high|medium|low",
      "effort": "high|medium|low",
      "rationale": "<why this is the top priority>"
    }},
    {{
      "rank": 2,
      "fix": "<description>",
      "impact": "high|medium|low",
      "effort": "high|medium|low",
      "rationale": "<why>"
    }},
    {{
      "rank": 3,
      "fix": "<description>",
      "impact": "high|medium|low",
      "effort": "high|medium|low",
      "rationale": "<why>"
    }}
  ],
  "call_type_breakdown": {{
    "NDR": {{
      "total": <int>, "good": <int>, "average": <int>, "poor": <int>, "no_contact": <int>,
      "key_issue": "<main problem for this call type>"
    }},
    "Order Confirmation": {{
      "total": <int>, "good": <int>, "average": <int>, "poor": <int>, "no_contact": <int>,
      "key_issue": "<main problem>"
    }},
    "COD-to-Prepaid": {{
      "total": <int>, "good": <int>, "average": <int>, "poor": <int>, "no_contact": <int>,
      "key_issue": "<main problem>"
    }}
  }}
}}
"""


def _compute_stats(analyses: list[dict]) -> dict:
    total = len(analyses)
    no_contact = sum(1 for a in analyses if a.get("no_contact"))
    contactable = total - no_contact

    valid = [
        a for a in analyses
        if not a.get("no_contact") and a.get("overall_quality") not in ("error", None)
    ]
    good    = sum(1 for a in valid if a.get("overall_quality") == "good")
    average = sum(1 for a in valid if a.get("overall_quality") == "average")
    poor    = sum(1 for a in valid if a.get("overall_quality") == "poor")

    dim_scores: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
    for a in valid:
        for dim in DIMENSIONS:
            score = a.get("dimensions", {}).get(dim, {}).get("score")
            if isinstance(score, (int, float)):
                dim_scores[dim].append(float(score))

    dim_avgs = {
        dim: round(sum(s) / len(s), 2) if s else None
        for dim, s in dim_scores.items()
    }

    call_types: dict[str, dict] = {}
    for a in analyses:
        ct = a.get("call_type", "Unknown")
        if ct not in call_types:
            call_types[ct] = {"total": 0, "good": 0, "average": 0, "poor": 0, "no_contact": 0}
        call_types[ct]["total"] += 1
        q = a.get("overall_quality")
        if a.get("no_contact"):
            call_types[ct]["no_contact"] += 1
        elif q == "good":
            call_types[ct]["good"] += 1
        elif q == "average":
            call_types[ct]["average"] += 1
        elif q == "poor":
            call_types[ct]["poor"] += 1

    return {
        "totals": {
            "total": total, "no_contact": no_contact, "contactable": contactable,
            "good": good, "average": average, "poor": poor,
        },
        "dimension_averages": dim_avgs,
        "call_type_breakdown": call_types,
    }


def generate_summary_report(
    client: anthropic.Anthropic,
    analyses: list[dict],
    analysis_date: str,
) -> dict:
    stats = _compute_stats(analyses)

    compact_analyses = [
        {
            "call_id":            a.get("call_id"),
            "call_type":          a.get("call_type"),
            "agent_name":         a.get("agent_name"),
            "status":             a.get("status"),
            "overall_quality":    a.get("overall_quality"),
            "overall_score":      a.get("overall_score"),
            "no_contact":         a.get("no_contact"),
            "top_issues":         a.get("top_issues", []),
            "best_moments":       a.get("best_moments", []),
            "key_recommendation": a.get("key_recommendation", ""),
            "dimension_scores": {
                dim: a.get("dimensions", {}).get(dim, {}).get("score")
                for dim in DIMENSIONS
            },
            "dimension_fixes": {
                dim: a.get("dimensions", {}).get(dim, {}).get("fix")
                for dim in DIMENSIONS
            },
        }
        for a in analyses
    ]

    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        date=analysis_date,
        stats_json=json.dumps(stats, ensure_ascii=False, indent=2),
        analyses_json=json.dumps(compact_analyses, ensure_ascii=False, indent=2),
    )

    t = stats["totals"]
    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            final = stream.get_final_message()

        text = next((b.text for b in final.content if b.type == "text"), "")
        text = strip_markdown_fences(text)
        return json.loads(text)

    except Exception as e:
        print(f"  [!] Summary generation error: {e}", file=sys.stderr)
        return {
            "date": analysis_date,
            "executive_summary": f"Automated summary failed ({e}). Manual review required.",
            "call_volume": {
                "total": t["total"], "no_contact": t["no_contact"],
                "contactable": t["contactable"], "good": t["good"],
                "average": t["average"], "poor": t["poor"],
            },
            "dimension_scorecard": {
                dim: {"avg_score": stats["dimension_averages"].get(dim), "trend": "ok", "note": ""}
                for dim in DIMENSIONS
            },
            "issues_breakdown": [],
            "what_went_well": [],
            "top_3_priority_fixes": [],
            "call_type_breakdown": stats["call_type_breakdown"],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets — via Apps Script webhook (no GCP credentials needed)
# ─────────────────────────────────────────────────────────────────────────────

DIM_KEYS = [
    "opening_first_impression",
    "pacing_pause_handling",
    "interruption_handling",
    "language_adaptability",
    "script_naturalness_tone",
    "objection_confusion_handling",
    "call_closure",
    "overall_effectiveness",
]


def _post_to_webhook(url: str, secret: str, payload: dict) -> bool:
    payload["secret"] = secret
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("status") != "ok":
            print(f"  [!] Sheets webhook error: {result.get('message', result)}")
            return False
        return True
    except Exception as e:
        print(f"  [!] Sheets webhook request failed: {e}")
        return False


def write_to_sheets(analyses: list[dict], summary: dict, analysis_date: str):
    """POST analyses and summary to the deployed Apps Script webhook."""
    url    = os.environ.get("GOOGLE_APPS_SCRIPT_URL", "").strip()
    secret = os.environ.get("GOOGLE_APPS_SCRIPT_SECRET", "").strip()

    if not url:
        print("  [!] GOOGLE_APPS_SCRIPT_URL not set — skipping Sheets upload.")
        return

    # ── Tab 1: Call Analysis rows ─────────────────────────────────────────────
    rows = []
    for a in analyses:
        dims = a.get("dimensions", {})
        scores       = [dims.get(dk, {}).get("score", "")       for dk in DIM_KEYS]
        observations = [dims.get(dk, {}).get("observation", "") for dk in DIM_KEYS]
        fixes        = [dims.get(dk, {}).get("fix", "")         for dk in DIM_KEYS]
        top_issues   = "; ".join(a.get("top_issues", []))

        row = [
            analysis_date,
            a.get("call_id", ""),
            a.get("call_type", ""),
            a.get("agent_name", ""),
            a.get("agent_key", ""),
            a.get("status", ""),
            a.get("duration", ""),
            a.get("recording_url", ""),
            a.get("_transcript_preview", ""),
            a.get("overall_quality", ""),
            a.get("overall_score", ""),
            "Yes" if a.get("no_contact") else "No",
            *scores,
            *observations,
            *fixes,
            top_issues,
            a.get("key_recommendation", ""),
        ]
        rows.append(row)

    ok1 = _post_to_webhook(url, secret, {"type": "calls", "rows": rows})

    # ── Tab 2: Daily Summary row ──────────────────────────────────────────────
    cv = summary.get("call_volume", {})
    sc = summary.get("dimension_scorecard", {})

    summary_row = [
        analysis_date,
        cv.get("total", ""),
        cv.get("no_contact", ""),
        cv.get("contactable", ""),
        cv.get("good", ""),
        cv.get("average", ""),
        cv.get("poor", ""),
        sc.get("opening_first_impression", {}).get("avg_score", ""),
        sc.get("pacing_pause_handling", {}).get("avg_score", ""),
        sc.get("interruption_handling", {}).get("avg_score", ""),
        sc.get("language_adaptability", {}).get("avg_score", ""),
        sc.get("script_naturalness_tone", {}).get("avg_score", ""),
        sc.get("objection_confusion_handling", {}).get("avg_score", ""),
        sc.get("call_closure", {}).get("avg_score", ""),
        sc.get("overall_effectiveness", {}).get("avg_score", ""),
        summary.get("executive_summary", ""),
    ]

    ok2 = _post_to_webhook(url, secret, {"type": "summary", "row": summary_row})

    if ok1 and ok2:
        print(f"  Sheets updated: Call Analysis ({len(rows)} rows) + Daily Summary (1 row)")


# ─────────────────────────────────────────────────────────────────────────────
# Terminal output
# ─────────────────────────────────────────────────────────────────────────────

def _bar(score: float, max_score: float, width: int = 18) -> str:
    filled = round((score / max_score) * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _wrap(text: str, width: int = 68, indent: str = "  ") -> str:
    words = text.split()
    lines, current = [], indent
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current.rstrip())
            current = indent + word
        else:
            current += ("" if current == indent else " ") + word
    if current.strip():
        lines.append(current.rstrip())
    return "\n".join(lines)


QUALITY_ICON = {"good": "✅", "average": "⚠️ ", "poor": "❌", "error": "💥"}
TREND_ICON   = {"strong": "🟢", "ok": "🟡", "weak": "🔴"}
SEV_ICON     = {"high": "🔴", "medium": "🟡", "low": "🟢"}
IMPACT_ICON  = {"high": "🔴 HIGH", "medium": "🟡 MED", "low": "🟢 LOW"}
EFFORT_ICON  = {"high": "⬆  HIGH", "medium": "➡  MED", "low": "⬇  LOW"}


def print_report(summary: dict):
    W = 72
    SEP  = "═" * W
    THIN = "─" * W

    def section(title: str):
        print(f"\n  {title}")
        print(f"  {THIN}")

    print(f"\n{SEP}")
    print(f"  VELOCITY SHIPPING — AI VOICE AGENT DAILY QA REPORT")
    print(f"  Date: {summary.get('date', 'N/A')}   Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{SEP}")

    section("EXECUTIVE SUMMARY")
    print(_wrap(summary.get("executive_summary", "N/A"), width=70, indent="  "))

    section("CALL VOLUME BREAKDOWN")
    cv = summary.get("call_volume", {})
    total       = cv.get("total", 0)
    no_contact  = cv.get("no_contact", 0)
    contactable = cv.get("contactable", 0)
    good        = cv.get("good", 0)
    average     = cv.get("average", 0)
    poor        = cv.get("poor", 0)

    def pct(n):
        return f"{n / contactable * 100:.0f}%" if contactable else "—"

    print(f"  Total analyzed   : {total:>4}")
    print(f"  No-contact       : {no_contact:>4}  (filtered out before analysis)")
    print(f"  Contactable      : {contactable:>4}")
    print(f"  ✅ Good          : {good:>4}  {pct(good):>4}")
    print(f"  ⚠️  Average       : {average:>4}  {pct(average):>4}")
    print(f"  ❌ Poor          : {poor:>4}  {pct(poor):>4}")

    section("DIMENSION SCORECARD  (avg out of 5)")
    scorecard = summary.get("dimension_scorecard", {})
    for dim, label in DIMENSION_LABELS.items():
        data  = scorecard.get(dim, {})
        avg   = data.get("avg_score")
        trend = data.get("trend", "ok")
        note  = data.get("note", "")
        icon  = TREND_ICON.get(trend, "⚪")
        if avg is not None:
            bar = _bar(avg, 5.0)
            print(f"  {icon} {label:<36} {avg:.2f}  {bar}")
            if note:
                print(f"       {note}")
        else:
            print(f"  ⚪ {label:<36}  N/A")

    issues = summary.get("issues_breakdown", [])
    if issues:
        section("ISSUES BREAKDOWN  (semantic clusters)")
        sorted_issues = sorted(
            issues,
            key=lambda x: ({"high": 0, "medium": 1, "low": 2}.get(x.get("severity", "low"), 3),
                            -x.get("count", 0)),
        )
        for iss in sorted_issues:
            sev   = iss.get("severity", "low")
            count = iss.get("count", 0)
            print(f"\n  {SEV_ICON.get(sev, '⚪')} {iss.get('cluster', 'Unknown')}  "
                  f"[{count} occurrence{'s' if count != 1 else ''}]")
            print(f"     Example     : {iss.get('example', 'N/A')}")
            print(f"     Root cause  : {iss.get('root_cause', 'N/A')}")
            print(f"     Prompt fix  : {iss.get('prompt_fix', 'N/A')}")

    positives = summary.get("what_went_well", [])
    if positives:
        section("WHAT WENT WELL")
        for p in positives:
            print(f"  ✓  {p}")

    fixes = summary.get("top_3_priority_fixes", [])
    if fixes:
        section("TOP 3 PRIORITY FIXES  (ranked by impact × effort)")
        for fix in sorted(fixes, key=lambda x: x.get("rank", 99)):
            rank      = fix.get("rank", "?")
            impact    = fix.get("impact", "?")
            effort    = fix.get("effort", "?")
            fix_desc  = fix.get("fix", "N/A")
            rationale = fix.get("rationale", "")
            print(f"\n  #{rank}  {fix_desc}")
            print(f"       Impact: {IMPACT_ICON.get(impact, impact.upper())}   "
                  f"Effort: {EFFORT_ICON.get(effort, effort.upper())}")
            if rationale:
                print(_wrap(rationale, width=70, indent="       "))

    ct_breakdown = summary.get("call_type_breakdown", {})
    if ct_breakdown:
        section("BREAKDOWN BY CALL TYPE")
        for ct_name, data in ct_breakdown.items():
            if not isinstance(data, dict):
                continue
            ct_total = data.get("total", 0)
            if ct_total == 0:
                continue
            ct_good  = data.get("good", 0)
            ct_avg   = data.get("average", 0)
            ct_poor  = data.get("poor", 0)
            ct_nc    = data.get("no_contact", 0)
            key_issue = data.get("key_issue", "")
            print(f"\n  {ct_name}")
            print(f"    Total {ct_total}  ✅ {ct_good}  ⚠️  {ct_avg}  ❌ {ct_poor}  No-contact {ct_nc}")
            if key_issue:
                print(f"    Key issue: {key_issue}")

    print(f"\n{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Slack notification
# ─────────────────────────────────────────────────────────────────────────────

def send_slack_notification(summary: dict, analysis_date: str, call_type_label: str):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return

    cv  = summary.get("call_volume", {})
    total       = cv.get("total", 0)
    contactable = cv.get("contactable", 0)
    good        = cv.get("good", 0)
    average     = cv.get("average", 0)
    poor        = cv.get("poor", 0)
    no_contact  = cv.get("no_contact", 0)

    # Quality bar (visual)
    def pct(n): return f"{round(n / contactable * 100)}%" if contactable else "—"

    # Top 2 issue clusters
    issues = summary.get("issues_breakdown", [])
    issues_sorted = sorted(issues, key=lambda x: ({"high": 0, "medium": 1, "low": 2}.get(x.get("severity", "low"), 3), -x.get("count", 0)))
    top_issues_text = "\n".join(
        f"  • {iss.get('cluster', '?')} ({iss.get('count', 0)} calls, {iss.get('severity', '?')} severity)"
        for iss in issues_sorted[:2]
    ) or "  None identified"

    # Top priority fix
    fixes = summary.get("top_3_priority_fixes", [])
    top_fix = fixes[0].get("fix", "") if fixes else ""

    exec_summary = (summary.get("executive_summary", "") or "")[:280]

    message = (
        f"📊 *Velocity QA — {call_type_label} | {analysis_date}*\n\n"
        f"*Calls fetched:* {total} → *Analyzed:* {contactable} (skipped {no_contact} no-contact)\n"
        f"✅ Good: {good} ({pct(good)})  ⚠️ Average: {average} ({pct(average)})  ❌ Poor: {poor} ({pct(poor)})\n\n"
        f"_{exec_summary}_\n\n"
        f"*Top issues:*\n{top_issues_text}\n\n"
        f"*#1 fix:* {top_fix}"
    )

    try:
        resp = requests.post(webhook_url, json={"text": message}, timeout=10)
        resp.raise_for_status()
        print("  Slack notification sent.")
    except Exception as e:
        print(f"  [!] Slack notification failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    analysis_date = args.date
    agent_key_filter = CALL_TYPE_FILTER.get(args.call_type)

    call_type_label = {
        "oc": "Order Confirmation only",
        "ndr": "NDR only",
        "all": "All types",
    }.get(args.call_type, args.call_type)

    print(f"\nVelocity Shipping — Call Quality Analyzer")
    print(f"  Date      : {analysis_date}")
    print(f"  Call type : {call_type_label}")
    print(f"  Limit     : {args.limit or 'all'}")
    print(f"  Model     : {MODEL}")
    print(f"  Filter    : status=completed, duration>{MIN_DURATION_SECONDS}s, transcript not empty")
    print()

    # 1. Metabase auth + fetch
    print("Connecting to Metabase...")
    try:
        headers, base_url = get_metabase_headers()
    except KeyError as e:
        print(f"  Missing env var: {e}. Check your .env file.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"  Metabase auth failed: {e}", file=sys.stderr)
        sys.exit(1)
    print("  Connected.\n")

    print(f"Fetching calls for {analysis_date}...")
    try:
        calls = fetch_calls(headers, base_url, analysis_date, agent_key_filter, args.limit)
    except Exception as e:
        print(f"  Failed to fetch calls: {e}", file=sys.stderr)
        sys.exit(1)

    if not calls:
        print(f"  No qualifying calls found for {analysis_date} (check filters).")
        sys.exit(0)

    print(f"  Found {len(calls)} qualifying call(s).\n")

    # 2. Claude client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ANTHROPIC_API_KEY not set in environment.", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    # 3. Per-call analysis
    print(f"Analyzing calls via Claude ({MODEL})...")
    print(f"  {'─'*65}")
    analyses: list[dict] = []

    for i, call in enumerate(calls, 1):
        call_id_short = str(call["id"])[:8]
        status_str    = STATUS_MAP.get(call.get("status"), "unknown")
        duration      = call.get("duration") or "?"
        call_type     = get_call_type(call)
        agent_name    = (call.get("agent_name") or "")[:14]

        print(
            f"  [{i:>3}/{len(calls)}] {call_id_short}  {status_str:<11} "
            f"{str(duration):>5}s  {call_type:<22} {agent_name:<14}",
            end="",
            flush=True,
        )

        analysis = analyze_call(client, call)
        # attach transcript preview for Sheets (not stored in analysis dict by default)
        analysis["_transcript_preview"] = (call.get("transcript") or "")[:500]
        analyses.append(analysis)

        quality = analysis.get("overall_quality", "error")
        score   = analysis.get("overall_score", 0)
        icon    = QUALITY_ICON.get(quality, "💥")

        if quality == "error":
            err = analysis.get("error", "")[:40]
            print(f"  💥 error: {err}")
        else:
            print(f"  {icon} {quality:<8}  {score}/10")

        if i < len(calls):
            time.sleep(0.3)

    print(f"\n  {len(analyses)} call(s) analyzed.\n")

    # 4. EOD summary
    print("Generating end-of-day summary report...")
    summary = generate_summary_report(client, analyses, analysis_date)
    print("  Done.\n")

    # 5. Print to terminal
    print_report(summary)

    # 6. Save JSON
    output_file = f"call_report_{analysis_date}.json"
    output_data = {
        "generated_at": datetime.now().isoformat(),
        "date": analysis_date,
        "call_type_filter": args.call_type,
        "model": MODEL,
        "total_calls_fetched": len(calls),
        "summary": summary,
        "individual_analyses": analyses,
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"Full JSON report saved to: {output_file}")

    # 7. Write to Google Sheets (if configured)
    print("\nWriting to Google Sheets...")
    write_to_sheets(analyses, summary, analysis_date)

    # 8. Slack notification
    send_slack_notification(summary, analysis_date, call_type_label)

    print()


if __name__ == "__main__":
    main()
