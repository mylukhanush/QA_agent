"""
Smart waiting functions for Playwright.

CRITICAL: No page.wait_for_timeout() or time.sleep() anywhere.
All waits are event-driven: element visibility, network idle,
API responses, or JS function polling.
"""
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


def wait_for_element_non_empty(page: Page, selector: str, timeout: int = 60000):
    """
    Wait until an element exists, is visible, AND has non-empty text.
    Uses Playwright's wait_for_selector for visibility, then a JS poll for stable non-empty content.
    Falls back gracefully for Playwright-specific pseudo-selectors like :has-text().
    """
    # Wait for the element to be visible (supports Playwright pseudo-selectors)
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        raise TimeoutError(
            f"Element did not become visible within {timeout}ms. "
            f"Selector: {selector}. Current URL: {page.url}"
        ) from exc

    # For selectors that JS document.querySelector understands (no :has-text etc.)
    # do a stable non-empty content check
    if ":has-text" not in selector and ":text" not in selector:
        try:
            page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    // Input/textarea/select: check .value; other elements: check .textContent
                    const tag = el.tagName.toLowerCase();
                    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
                        return el.value && el.value.trim().length > 0;
                    }
                    return el.textContent && el.textContent.trim().length > 0;
                }""",
                arg=selector,
                timeout=timeout,
            )
        except PlaywrightTimeoutError as exc:
            raise TimeoutError(
                f"Element was visible but stayed empty within {timeout}ms. "
                f"Selector: {selector}. Current URL: {page.url}"
            ) from exc
        # Stability: check value doesn't change in 500ms
        try:
            page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const tag = el.tagName.toLowerCase();
                    const val = (tag === 'input' || tag === 'textarea' || tag === 'select')
                        ? (el.value || '').trim()
                        : (el.textContent || '').trim();
                    if (!val) return false;
                    if (!window.__qaStable) {
                        window.__qaStable = { val: val, ts: Date.now() };
                        return false;
                    }
                    if (Date.now() - window.__qaStable.ts < 500) return false;
                    const curVal = (tag === 'input' || tag === 'textarea' || tag === 'select')
                        ? (el.value || '').trim()
                        : (el.textContent || '').trim();
                    const stable = curVal === window.__qaStable.val;
                    window.__qaStable = null;
                    return stable;
                }""",
                arg=selector,
                timeout=timeout,
            )
        except PlaywrightTimeoutError as exc:
            raise TimeoutError(
                f"Element text did not stabilize within {timeout}ms. "
                f"Selector: {selector}. Current URL: {page.url}"
            ) from exc


def wait_for_page_data_loaded(page: Page, site_map_page: dict):
    """
    Use loading_indicators from site-map to know when to stop waiting.
    1. Wait for networkidle
    2. Wait for all loading spinners to disappear
    3. Wait for metric elements to have non-empty text
    """
    # Network idle removed — Angular SPAs never fully reach networkidle
    # Wait for the loading spinners to disappear first
    loading_indicators = site_map_page.get("loading_indicators", [])
    for indicator_sel in loading_indicators:
        try:
            locator = page.locator(indicator_sel)
            if locator.count() > 0:
                locator.first.wait_for(state="hidden", timeout=15000)
        except Exception:
            pass

    # Common spinners
    for sel in [".spinner", ".loading", "[class*='spinner']", ".skeleton"]:
        try:
            locator = page.locator(sel)
            if locator.count() > 0:
                locator.first.wait_for(state="hidden", timeout=10000)
        except Exception:
            pass

    # Metric elements non-empty
    elements = site_map_page.get("elements", [])
    for el in elements[:10]:  # Check first 10 elements
        try:
            sel = el.get("selector", "")
            if sel:
                page.wait_for_function(
                    f"""(s) => {{
                        const el = document.querySelector(s);
                        return el && el.textContent && el.textContent.trim().length > 0;
                    }}""",
                    arg=sel,
                    timeout=10000,
                )
        except Exception:
            pass


def wait_for_api_response(page: Page, endpoint_pattern: str, timeout: int = 30000):
    """
    Wait for a specific API call to complete.
    Uses page.expect_response with URL pattern matching.
    """
    with page.expect_response(
        lambda resp: endpoint_pattern in resp.url,
        timeout=timeout,
    ) as response_info:
        pass
    return response_info.value


def wait_for_login_success(page: Page, success_indicator: dict, timeout: int = 15000):
    """
    Wait for login to succeed based on the indicator from site-map.
    """
    ind_type = success_indicator.get("type", "url_contains")
    ind_value = success_indicator.get("value", "")

    if ind_type == "url_contains":
        page.wait_for_function(
            f"""() => window.location.href.includes('{ind_value}') || !window.location.href.includes('login')""",
            timeout=timeout,
        )
    elif ind_type == "element_visible":
        page.wait_for_selector(ind_value, state="visible", timeout=timeout)
    else:
        page.wait_for_load_state("networkidle")

    # Extra verification: ensure the authenticated state is stable and that
    # at least one session/csrf/token artifact exists in cookies or storage.
    # Some apps redirect briefly; wait a little until the URL is stable
    # and an auth token/cookie is present.
    try:
        page.wait_for_function(
            """() => {
                const href = window.location.href.toLowerCase();
                if (href.includes('login') || href.includes('auth')) return false;
                if ((document.cookie || '').trim().length > 0) return true;
                const lsKeys = Object.keys(window.localStorage || {});
                if (lsKeys.some(k => /token|jwt|session|auth|access|id_token/i.test(k))) return true;
                const ssKeys = Object.keys(window.sessionStorage || {});
                if (ssKeys.some(k => /token|jwt|session|auth|access|id_token/i.test(k))) return true;
                return false;
            }""",
            timeout=min(10000, timeout),
        )
    except PlaywrightTimeoutError:
        # If the JS-side checks failed to detect tokens, allow the caller
        # to inspect cookies via the Playwright context. Raise a TimeoutError
        # so the executor can decide whether to retry login.
        raise TimeoutError("Login did not stabilize or no auth cookie/localStorage token detected.")
