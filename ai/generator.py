"""
Gemini AI Test Generator.

Accepts a plain-English situation description and the site-map.json,
generates a structured test plan with exact selectors and steps.
Uses Gemini context caching for the site-map to save tokens.
"""
import json
import builtins
import importlib
import os
from typing import List, Optional

# Must be set before google.generativeai imports protobuf internals.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

_original_import_module = importlib.import_module
_original_import = builtins.__import__


def _protobuf_safe_import_module(name, package=None):
    """Let protobuf fall back when Python 3.14 rejects its native extension."""
    try:
        return _original_import_module(name, package)
    except TypeError as exc:
        is_protobuf_native_probe = name in {
            "google._upb._message",
            "google.protobuf.pyext._message",
        }
        if is_protobuf_native_probe and "custom tp_new" in str(exc):
            raise ImportError(name) from exc
        raise


def _protobuf_safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Convert protobuf native-extension TypeErrors into normal import misses."""
    try:
        return _original_import(name, globals, locals, fromlist, level)
    except TypeError as exc:
        is_protobuf_native_probe = (
            name in {"google._upb", "google._upb._message", "google.protobuf.pyext"}
            or name.startswith("google._upb.")
            or name.startswith("google.protobuf.pyext.")
        )
        if is_protobuf_native_probe and "custom tp_new" in str(exc):
            raise ImportError(name) from exc
        raise


importlib.import_module = _protobuf_safe_import_module
builtins.__import__ = _protobuf_safe_import
try:
    import google.generativeai as genai
finally:
    builtins.__import__ = _original_import
    importlib.import_module = _original_import_module

from ai.prompt import build_prompt
from ai.cache import get_cached_site_map_content
from crawler.mapper import load_site_map

# ── Gemini Setup ──────────────────────────────────────────────────

def _get_model():
    """Configure and return the Gemini model."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set in environment")

    genai.configure(api_key=api_key)

    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return model


def _load_site_credentials(target_sites: List[str]) -> dict:
    """Load site URLs and credentials from environment variables."""
    creds = {}
    for site in ["jhs81", "jhs82", "jhs83", "jhs84"]:
        url = os.getenv(f"{site.upper()}_URL", "")
        username = os.getenv(f"{site.upper()}_USERNAME", "")
        password = os.getenv(f"{site.upper()}_PASSWORD", "")
        if url:
            creds[site] = {"url": url, "username": username, "password": password}
    # Only return creds for the target sites (keep all if not filtering)
    if target_sites:
        return {k: v for k, v in creds.items() if k in target_sites} or creds
    return creds


# ── Generator ─────────────────────────────────────────────────────

def generate_test_plan(
    situation: str,
    target_sites: List[str],
    recent_tests: Optional[List[dict]] = None,
) -> dict:
    """
    Generate a test plan from a situation description.

    Parameters
    ----------
    situation : str
        Plain English description of what to test.
    target_sites : list of str
        Site names to target (jhs81, jhs82, etc.)
    recent_tests : list of dict, optional
        Recent test cases for dedup reference.

    Returns
    -------
    dict — structured test plan with steps, selectors, assertions.
    """
    model = _get_model()

    # Load site map
    site_map = load_site_map()
    site_map_json = get_cached_site_map_content(site_map)

    # Load credentials
    site_credentials = _load_site_credentials(target_sites)

    # Build the full prompt
    prompt = build_prompt(
        situation=situation,
        target_sites=target_sites,
        site_map_json=site_map_json,
        recent_tests=recent_tests or [],
        site_credentials=site_credentials,
    )

    # Call Gemini
    response = model.generate_content(prompt)

    # Parse the JSON response (already guaranteed JSON by response_mime_type)
    try:
        raw_text = response.text
        if raw_text.startswith("```json"):
            raw_text = raw_text.strip("`").replace("json\n", "", 1)
        plan = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned invalid JSON: {exc}\nRaw: {response.text[:500]}")

    # Sometimes Gemini wraps the response in an outer object or array
    if isinstance(plan, list) and len(plan) > 0:
        plan = plan[0]

    if isinstance(plan, dict) and "testName" not in plan:
        if "plan" in plan and isinstance(plan["plan"], dict):
            plan = plan["plan"]
        elif "testPlan" in plan and isinstance(plan["testPlan"], dict):
            plan = plan["testPlan"]

    # Validate required fields
    required_fields = ["testName", "category", "steps"]
    for field in required_fields:
        if field not in plan or not isinstance(plan, dict):
            keys = list(plan.keys()) if isinstance(plan, dict) else type(plan)
            raise ValueError(f"Generated plan missing required field: {field}. Available keys/type: {keys}\nRaw response snippet: {str(plan)[:200]}")

    # Ensure targetSites is set
    if "targetSites" not in plan:
        plan["targetSites"] = target_sites

    return plan
