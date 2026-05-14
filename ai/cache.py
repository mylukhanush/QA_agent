"""
Gemini context caching for site-map.json.

Since the site-map is sent with every generation request and rarely changes,
we cache its string representation and use Gemini's context caching
to reduce token costs across multiple calls.
"""
import json
import hashlib
from typing import Optional

# In-memory cache
_cached_content: Optional[str] = None
_cached_hash: Optional[str] = None


def get_cached_site_map_content(site_map: dict) -> str:
    """
    Return the site-map as a JSON string, caching for reuse.
    If the site-map hasn't changed (by hash), return the cached version.
    """
    global _cached_content, _cached_hash

    current_json = json.dumps(site_map, indent=2, default=str)
    current_hash = hashlib.sha256(current_json.encode()).hexdigest()

    if _cached_hash == current_hash and _cached_content is not None:
        return _cached_content

    _cached_content = current_json
    _cached_hash = current_hash

    return _cached_content


def invalidate_cache():
    """Clear the cached site-map content (e.g., after a new crawl)."""
    global _cached_content, _cached_hash
    _cached_content = None
    _cached_hash = None
