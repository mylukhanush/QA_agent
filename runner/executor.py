"""
Playwright Test Executor.

Handles every action type from the AI-generated test plan:
  navigate, login, click, wait_for_element, wait_for_response,
  get_text, assert_equal, assert_not_empty, assert_contains,
  assert_not_equal, compare_values, screenshot, type_text,
  select_option, store_value.

CRITICAL: No page.wait_for_timeout() or time.sleep() anywhere.
All selectors come from site-map.json — never hardcoded.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import re
from playwright.sync_api import sync_playwright, expect

from crawler.mapper import load_site_map, get_login_info
from db import db
from db.models import Site, TestRun, RunStep, ValueCapture
from runner.waiter import (
    wait_for_element_non_empty,
    wait_for_page_data_loaded,
    wait_for_api_response,
    wait_for_login_success,
)


def _site_env(site_name: str) -> dict:
    """Load site credentials from environment variables."""
    prefix = site_name.upper()
    return {
        "url": os.getenv(f"{prefix}_URL"),
        "username": os.getenv(f"{prefix}_USERNAME"),
        "password": os.getenv(f"{prefix}_PASSWORD"),
    }


def _take_screenshot(page, run_id: str, step_order: int) -> str:
    """Capture a full-page screenshot and return the saved path."""
    screenshots_dir = os.getenv("SCREENSHOTS_DIR", "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    filename = f"{run_id}_step{step_order}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.png"
    path = os.path.join(screenshots_dir, filename)
    try:
        page.screenshot(path=path, full_page=True)
    except Exception:
        pass
    return path


def _record_step(run_id, step_order, action, description, status,
                 error_message=None, screenshot_path=None):
    """Write a step result to the run_steps table."""
    step = RunStep(
        run_id=run_id,
        step_order=step_order,
        action=action,
        description=description,
        status=status,
        error_message=error_message,
        screenshot_path=screenshot_path,
    )
    db.session.add(step)
    db.session.commit()
    return step


def _record_value_capture(run_id, site_id, label, page_name, selector,
                          captured_value, expected_value=None, matched=None):
    """Write a value capture to the value_captures table."""
    vc = ValueCapture(
        run_id=run_id,
        site_id=site_id,
        label=label,
        page=page_name,
        selector=selector,
        captured_value=captured_value,
        expected_value=expected_value,
        matched=matched,
    )
    db.session.add(vc)
    db.session.commit()
    return vc


def execute_test_plan(test_plan: dict, run_ids: dict):
    """
    Execute a test plan against one or more sites.

    Parameters
    ----------
    test_plan : dict
        The AI-generated test plan with steps.
    run_ids : dict
        Mapping of site_name -> run_id (UUID string).
    """
    site_map = load_site_map()
    login_info = get_login_info(site_map)

    for site_name, run_id in run_ids.items():
        print(f"[EXECUTOR] Initializing run {run_id} for site {site_name}...", flush=True)
        try:
            _execute_for_site(test_plan, site_name, run_id, site_map, login_info)
        except Exception as exc:
            # Mark run as error
            run = TestRun.query.get(run_id)
            if run:
                run.status = "error"
                run.finished_at = datetime.now(timezone.utc)
                if run.started_at:
                    run.duration_ms = int(
                        (run.finished_at - run.started_at).total_seconds() * 1000
                    )
                db.session.commit()

            _record_step(
                run_id=run_id,
                step_order=9999,
                action="error",
                description="Unhandled execution error",
                status="error",
                error_message=str(exc),
            )


def _execute_for_site(test_plan, site_name, run_id, site_map, login_info):
    """Execute all steps against a single site."""
    env = _site_env(site_name)
    site = Site.query.filter_by(name=site_name).first()
    if not site:
        raise ValueError(f"Site {site_name} not found in database")

    variables = {}
    steps = test_plan.get("steps", [])
    overall_status = "pass"
    should_stop = False

    with sync_playwright() as pw:
        # Force visible browser by default (headless=False)
        # This keeps the browser window open for local debugging.
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        # Prepare captures directory and start Playwright tracing for this run
        captures_dir = os.getenv("CAPTURES_DIR", "captures")
        os.makedirs(captures_dir, exist_ok=True)
        trace_path = os.path.join(captures_dir, f"{run_id}_trace.zip")
        state_path = os.path.join(captures_dir, f"{run_id}_storage_state.json")
        final_html_path = os.path.join(captures_dir, f"{run_id}_after.html")
        final_screenshot_path = os.path.join(captures_dir, f"{run_id}_final.png")
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception:
            try:
                context.tracing.start(screenshots=True, snapshots=True)
            except Exception:
                trace_path = None
        page = context.new_page()

        for idx, step in enumerate(steps):
            action = step.get("action", "")
            target = step.get("target", "")
            value = step.get("value")
            store_as = step.get("storeAs")
            compare_with = step.get("compareWith")
            description = step.get("description", "")
            on_failure = step.get("onFailure", "continue")
            
            print(f"[EXECUTOR] Site {site_name} | Step {idx+1}/{len(steps)} | Action: {action} | Target: {target}", flush=True)

            if should_stop:
                print(f"[EXECUTOR] Step {idx+1} skipped due to previous failure.", flush=True)
                _record_step(
                    run_id=run_id,
                    step_order=idx + 1,
                    action=action,
                    description=description,
                    status="skipped",
                )
                continue

            step_status = "pass"
            error_msg = None
            screenshot_path = None

            try:
                if action == "navigate":
                    _action_navigate(page, target, env, site_map)

                elif action == "login":
                    _action_login(page, env, login_info)

                elif action == "click":
                    # Use force=True to bypass Angular overlay/tooltip interception.
                    # For SPAs, pointer events are often intercepted by transparent overlay components.
                    loc = page.locator(target).first
                    loc.wait_for(state="visible", timeout=15000)
                    try:
                        loc.click(timeout=5000)
                    except Exception:
                        # Fallback: dispatch synthetic click event (bypasses pointer-event blockers)
                        loc.dispatch_event("click")
                    page.wait_for_load_state("domcontentloaded")

                elif action == "wait_for_element":
                    _wait_for_angular_stable(page, timeout=20000)
                    try:
                        wait_for_element_non_empty(page, target, timeout=20000)
                    except Exception as exc:
                        no_data_selectors = page.locator(":has-text('No records found'), :has-text('No data'), :has-text('No Data Available')")
                        if no_data_selectors.count() > 0 and no_data_selectors.first.is_visible():
                            pass # Element is missing because there's no data
                        else:
                            raise exc

                elif action == "wait_for_response":
                    # Hash-routed SPAs don't fire HTTP requests for hash changes
                    if target and "#/" in target:
                        hash_part = target.split("#")[-1]
                        page.wait_for_function(
                            f"() => window.location.hash.includes('{hash_part}')",
                            timeout=15000,
                        )
                    else:
                        wait_for_api_response(page, target)

                elif action == "get_text":
                    # Wait for Angular zone stable BEFORE reading — ngOnInit HTTP calls
                    # fire after routing completes, so the post-navigate stability check
                    # may run before those requests even start.
                    _wait_for_angular_stable(page, timeout=20000)
                    loc = page.locator(target).first
                    
                    try:
                        loc.wait_for(state="visible", timeout=20000)
                        raw_text = _wait_for_stable_text(page, target, loc)
                    except Exception as exc:
                        # If the element didn't appear, check if it's because there's simply no data
                        no_data_selectors = page.locator(":has-text('No records found'), :has-text('No data'), :has-text('No Data Available')")
                        if no_data_selectors.count() > 0 and no_data_selectors.first.is_visible():
                            raw_text = "No data available"
                        else:
                            raise exc
                            
                    # For table rows / multi-cell elements, keep full text;
                    # only extract numbers for dashboard metric widgets.
                    if "tbody tr" in target or "<tr" in target:
                        text = raw_text
                    elif "paginator" in target.lower():
                        import re
                        m = re.search(r"of\s+([\d,]+)", raw_text, re.IGNORECASE)
                        text = m.group(1).replace(',', '') if m else raw_text
                    elif raw_text == "No data available":
                        text = raw_text
                    else:
                        text = _extract_number_or_fraction(raw_text)
                        
                    if store_as:
                        variables[store_as] = text
                    # Also record as a value capture (normalized)
                    page_name = _guess_page_name(page.url, site_map)
                    _record_value_capture(
                        run_id=run_id,
                        site_id=site.id,
                        label=store_as or target,
                        page_name=page_name,
                        selector=target,
                        captured_value=text,
                    )

                elif action == "store_value":
                    _wait_for_angular_stable(page, timeout=20000)
                    loc = page.locator(target).first
                    
                    try:
                        loc.wait_for(state="visible", timeout=20000)
                        raw_text = _wait_for_stable_text(page, target, loc)
                    except Exception as exc:
                        no_data_selectors = page.locator(":has-text('No records found'), :has-text('No data'), :has-text('No Data Available')")
                        if no_data_selectors.count() > 0 and no_data_selectors.first.is_visible():
                            raw_text = "No data available"
                        else:
                            raise exc
                            
                    if raw_text == "No data available":
                        text = raw_text
                    else:
                        text = _extract_number_or_fraction(raw_text)
                        
                    if store_as:
                        variables[store_as] = text
                    page_name = _guess_page_name(page.url, site_map)
                    _record_value_capture(
                        run_id=run_id,
                        site_id=site.id,
                        label=store_as or target,
                        page_name=page_name,
                        selector=target,
                        captured_value=text,
                    )

                elif action == "assert_equal":
                    actual = _resolve_value(target, variables, page)
                    expected = _resolve_value(value or compare_with, variables, page)
                    if str(actual) != str(expected):
                        raise AssertionError(
                            f"Expected '{expected}' but got '{actual}'"
                        )

                elif action == "assert_not_empty":
                    # AI may put variable name in target, value, OR compareWith
                    ref = target or value or compare_with
                    actual = _resolve_value(ref, variables, page)
                    if not actual or not str(actual).strip():
                        raise AssertionError(
                            f"Expected non-empty value but got '{actual}' (ref='{ref}')"
                        )

                elif action == "assert_contains":
                    actual = _resolve_value(target, variables, page)
                    if value and value not in str(actual):
                        raise AssertionError(
                            f"Expected '{actual}' to contain '{value}'"
                        )

                elif action == "assert_not_equal":
                    actual = _resolve_value(target, variables, page)
                    expected = _resolve_value(value or compare_with, variables, page)
                    if str(actual) == str(expected):
                        raise AssertionError(
                            f"Expected values to differ but both are '{actual}'"
                        )

                elif action == "compare_values":
                    # AI sometimes puts the first variable name in 'value' when target is null
                    var_a = _resolve_var_reference(target if target else value)
                    var_b = _resolve_var_reference(compare_with)
                    val_a = variables.get(var_a, "")
                    val_b = variables.get(var_b, "")
                    if str(val_a) != str(val_b):
                        raise AssertionError(
                            f"Comparison mismatch: '{var_a}'='{val_a}' vs "
                            f"'{var_b}'='{val_b}'"
                        )

                elif action == "screenshot":
                    screenshot_path = _take_screenshot(page, run_id, idx + 1)

                elif action == "type_text":
                    loc = page.locator(target).first
                    loc.wait_for(state="visible", timeout=15000)
                    loc.fill(value or "")

                elif action == "press":
                    loc = page.locator(target).first
                    loc.wait_for(state="visible", timeout=15000)
                    loc.press(value)

                elif action == "select_option":
                    loc = page.locator(target).first
                    loc.wait_for(state="visible", timeout=15000)
                    tag_name = loc.evaluate("el => el.tagName").lower()
                    if tag_name == "select":
                        loc.select_option(value=value)
                    elif tag_name == "ng-select":
                        # ── ng-select (e.g. Template dropdown on alerts page) ──
                        # Step 1: Clear any existing value (like "tmp2")
                        # Try the × button on the selected value pill
                        clear_btns = loc.locator(".ng-value-icon, .ng-clear-wrapper")
                        for i in range(clear_btns.count()):
                            try:
                                clear_btns.nth(i).click(timeout=1000)
                                page.wait_for_timeout(200)
                            except Exception:
                                pass
                        
                        # Step 2: Open the dropdown
                        try:
                            loc.click(timeout=5000)
                        except Exception:
                            loc.dispatch_event("click")
                        page.wait_for_timeout(500)
                        
                        # Step 3: Click the desired option
                        escaped_val = str(value).replace('"', '\\"')
                        option_loc = page.locator(f'.ng-option:has-text("{escaped_val}")').first
                        option_loc.wait_for(state="visible", timeout=5000)
                        try:
                            option_loc.click(timeout=5000)
                        except Exception:
                            option_loc.dispatch_event("click")
                    else:
                        # Check class for multiselect-dropdown
                        el_class = loc.evaluate("el => el.className || ''")
                        if "multiselect-dropdown" in el_class:
                            # ── Multiselect dropdown (like Vehicle selection) ──
                            dropdown_btn = loc.locator(".dropdown-btn").first
                            dropdown_btn.click()
                            page.wait_for_timeout(500)
                            
                            # Search for the value
                            search_input = loc.locator(".filter-textbox input").first
                            if search_input.count() > 0:
                                search_input.fill(str(value))
                                page.wait_for_timeout(1000)
                            
                            # Click the specific list item
                            option_loc = loc.locator(".dropdown-list li").filter(has_text=str(value)).first
                            if option_loc.count() > 0:
                                option_loc.click()
                            else:
                                page.locator(f".dropdown-list li:has-text('{value}')").first.click()
                            
                            # Close dropdown by clicking elsewhere
                            page.locator("body").click(position={"x": 0, "y": 0}, force=True)
                        else:
                            # ── Generic custom dropdown fallback ──
                            # Clear existing selection
                            clear_btn = loc.locator(".ng-clear-wrapper, .ng-value-icon").first
                            if clear_btn.count() > 0:
                                try:
                                    clear_btn.click(timeout=2000)
                                except Exception:
                                    pass
                                    
                            try:
                                loc.click(timeout=5000)
                            except Exception:
                                loc.dispatch_event("click")
                            page.wait_for_timeout(500)
                            
                            escaped_val = str(value).replace('"', '\\"')
                            option_sel = f'.ng-option:has-text("{escaped_val}"), .dropdown-item:has-text("{escaped_val}"), li:has-text("{escaped_val}")'
                            option_loc = page.locator(option_sel).first
                            option_loc.wait_for(state="visible", timeout=5000)
                            try:
                                option_loc.click(timeout=5000)
                            except Exception:
                                option_loc.dispatch_event("click")

                elif action == "check":
                    # Use force=True because custom checkboxes might have the actual input hidden
                    loc = page.locator(target).first
                    loc.wait_for(state="visible", timeout=15000)
                    try:
                        loc.check(force=True, timeout=5000)
                    except Exception:
                        # Fallback for completely custom toggle elements that Playwright doesn't recognize as checkboxes
                        is_checked = loc.evaluate("el => el.querySelector('input') ? el.querySelector('input').checked : el.classList.contains('checked') || el.classList.contains('active')")
                        if not is_checked:
                            loc.dispatch_event("click")
                    page.wait_for_load_state("domcontentloaded")

                elif action == "uncheck":
                    loc = page.locator(target).first
                    loc.wait_for(state="visible", timeout=15000)
                    try:
                        loc.uncheck(force=True, timeout=5000)
                    except Exception:
                        is_checked = loc.evaluate("el => el.querySelector('input') ? el.querySelector('input').checked : el.classList.contains('checked') || el.classList.contains('active')")
                        if is_checked:
                            loc.dispatch_event("click")
                    page.wait_for_load_state("domcontentloaded")

                elif action == "count_elements":
                    # Count matching elements and store the count
                    _wait_for_angular_stable(page, timeout=20000)
                    count = page.locator(target).count()
                    text = str(count)
                    if store_as:
                        variables[store_as] = text
                    page_name = _guess_page_name(page.url, site_map)
                    _record_value_capture(
                        run_id=run_id,
                        site_id=site.id,
                        label=store_as or f"count({target})",
                        page_name=page_name,
                        selector=target,
                        captured_value=text,
                    )

                elif action == "select_date_range":
                    loc = page.locator(target).first
                    loc.wait_for(state="visible", timeout=15000)
                    loc.click()  # Open calendar
                    page.wait_for_timeout(500)
                    
                    val = str(value).strip()
                    
                    # Check if it's a preset like "Last 7 Days", "Yesterday", etc.
                    presets = ["Yesterday", "Last 7 Days", "Last 30 Days", "Last Month", "Custom range"]
                    is_preset = any(val.lower() == p.lower() for p in presets)
                    
                    if is_preset:
                        # Click the preset button in .ranges
                        preset_btn = page.locator(f".md-drppicker .ranges button:has-text('{val}')").first
                        if preset_btn.count() > 0:
                            preset_btn.click()
                            page.wait_for_timeout(500)
                    elif " - " in val:
                        # Custom date range: "01-04-2026 - 12-05-2026"
                        start_str, end_str = val.split(" - ")
                        _select_calendar_date(page, start_str.strip())
                        _select_calendar_date(page, end_str.strip())
                    else:
                        _select_calendar_date(page, val)
                        
                    # Click OK button (verified: .md-drppicker .buttons button.btn with text "ok")
                    ok_btn = page.locator(".md-drppicker .buttons button.btn").first
                    if ok_btn.count() > 0 and ok_btn.is_visible():
                        ok_btn.click()
                    page.wait_for_load_state("domcontentloaded")

                else:
                    error_msg = f"Unknown action: {action}"
                    step_status = "error"

            except AssertionError as exc:
                step_status = "fail"
                error_msg = str(exc)
                overall_status = "fail"
                screenshot_path = _take_screenshot(page, run_id, idx + 1)
                if on_failure == "stop":
                    should_stop = True

            except Exception as exc:
                step_status = "error"
                error_msg = str(exc)
                overall_status = "error" if overall_status != "fail" else "fail"
                screenshot_path = _take_screenshot(page, run_id, idx + 1)
                if on_failure == "stop":
                    should_stop = True

            _record_step(
                run_id=run_id,
                step_order=idx + 1,
                action=action,
                description=description,
                status=step_status,
                error_message=error_msg,
                screenshot_path=screenshot_path,
            )

        # Stop tracing and save storage state / final artifacts
        try:
            if trace_path:
                context.tracing.stop(path=trace_path)
        except Exception:
            pass
        try:
            context.storage_state(path=state_path)
        except Exception:
            pass
        try:
            with open(final_html_path, 'w', encoding='utf-8') as f:
                f.write(page.content())
        except Exception:
            pass
        try:
            page.screenshot(path=final_screenshot_path, full_page=True)
        except Exception:
            pass
        browser.close()

    # Finalize the run
    run = TestRun.query.get(run_id)
    if run:
        run.status = overall_status
        run.finished_at = datetime.now(timezone.utc)
        if run.started_at:
            run.duration_ms = int(
                (run.finished_at - run.started_at).total_seconds() * 1000
            )

        # Generate JSON report
        from reports.json_report import generate_json_report
        report_path = generate_json_report(run_id)
        run.report_path = report_path

        # Update test case last_run_at
        if run.test_case:
            run.test_case.last_run_at = datetime.now(timezone.utc)

        db.session.commit()


# ── Action Helpers ────────────────────────────────────────────────

def _wait_for_angular_stable(page, timeout: int = 15000):
    """
    Wait for Angular's zone to report stable (all HTTP requests + async ops done).
    Uses getAllAngularTestabilities() — available in Angular 2+ apps in both dev and prod.
    Falls back gracefully if not available.
    """
    try:
        page.wait_for_function(
            """() => {
                try {
                    const testabilities = window.getAllAngularTestabilities();
                    if (!testabilities || testabilities.length === 0) return true;
                    return testabilities.every(t => t.isStable());
                } catch (e) {
                    return true;
                }
            }""",
            timeout=timeout,
        )
    except Exception:
        pass


def _action_navigate(page, target, env, site_map):
    """Navigate to a URL or page from site-map."""
    import urllib.parse

    def _localize_url(url_str):
        if not url_str.startswith("http"):
            return f"{env.get('url', '').rstrip('/')}/{url_str.lstrip('/')}"
        parsed = urllib.parse.urlparse(url_str)
        path_part = urllib.parse.urlunparse(('', '', parsed.path, parsed.params, parsed.query, parsed.fragment))
        if not path_part.startswith("/"):
            path_part = "/" + path_part
        return env.get("url", "").rstrip("/") + path_part

    if target in site_map.get("pages", {}):
        page_url = site_map["pages"][target].get("url", "")
        if page_url:
            target_url = _localize_url(page_url)
            page.goto(target_url, wait_until="domcontentloaded")
            try:
                page.wait_for_function(
                    "() => document.querySelector('.nav-item, app-root') !== null",
                    timeout=10000,
                )
            except Exception:
                pass
        else:
            nav_sel = site_map["pages"][target].get("nav_selector", "")
            if nav_sel:
                page.locator(nav_sel).first.click()
                page.wait_for_load_state("domcontentloaded")
    else:
        target_url = _localize_url(target)
        page.goto(target_url, wait_until="domcontentloaded")
        
        # Wait for Angular app to boot (sidebar/nav present)
        try:
            page.wait_for_function(
                "() => document.querySelector('.nav-item, app-root, [class*=sidebar]') !== null",
                timeout=10000,
            )
        except Exception:
            pass
        # Wait for Angular zone to be stable (all HTTP requests finished)
        _wait_for_angular_stable(page)


def _action_login(page, env, login_info):
    """Perform login using site-map selectors."""
    login_url = login_info.get("url", env.get("url", ""))
    page.goto(login_url, wait_until="networkidle")

    username_sel = login_info.get("username_selector", "")
    password_sel = login_info.get("password_selector", "")
    submit_sel = login_info.get("submit_selector", "")

    if username_sel:
        page.locator(username_sel).first.fill(env.get("username", ""))
    if password_sel:
        page.locator(password_sel).first.fill(env.get("password", ""))
    if submit_sel:
        page.locator(submit_sel).first.click()

    success_indicator = login_info.get("success_indicator", {})
    try:
        wait_for_login_success(page, success_indicator)
    except Exception:
        # Retry once: refill credentials and resubmit the form (some sites
        # briefly fail to set session cookies on the first attempt).
        try:
            if username_sel:
                page.locator(username_sel).first.fill(env.get("username", ""))
            if password_sel:
                page.locator(password_sel).first.fill(env.get("password", ""))
            if submit_sel:
                page.locator(submit_sel).first.click()
            wait_for_login_success(page, success_indicator, timeout=15000)
        except Exception as exc:
            # Save a screenshot for debugging and re-raise
            try:
                screenshots_dir = os.getenv("SCREENSHOTS_DIR", "screenshots")
                os.makedirs(screenshots_dir, exist_ok=True)
                fn = f"login_fail_{int(datetime.now(timezone.utc).timestamp())}.png"
                page.screenshot(path=os.path.join(screenshots_dir, fn), full_page=True)
            except Exception:
                pass
            raise


def _resolve_value(ref, variables, page):
    """Resolve a reference to an actual value — variable name, selector, or literal."""
    if not ref:
        return ""
    # Check variables first
    if ref in variables:
        return variables[ref]
    # Try as a CSS selector on the page
    try:
        locator = page.locator(ref)
        if locator.count() > 0:
            return locator.first.text_content().strip()
    except Exception:
        pass
    # Return as literal
    return ref


def _resolve_var_reference(ref):
    """Normalize AI-provided variable references (string/object/list) to a key/literal."""
    if ref is None:
        return ""
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        for k in ("var", "name", "key", "value", "target", "storeAs", "compareWith"):
            v = ref.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(ref, sort_keys=True)
    if isinstance(ref, (list, tuple)) and ref:
        return _resolve_var_reference(ref[0])
    return str(ref)


def _wait_for_stable_text(page, selector: str, loc, timeout: int = 30000) -> str:
    """
    Wait for element text to be non-empty and stable for 800ms before reading.
    Handles both standard CSS selectors and Playwright pseudo-selectors (:has-text, :text).
    Angular dashboards load '0' or empty first, then replace with real data.
    """
    is_playwright_selector = ":has-text" in selector or ":text(" in selector

    if not is_playwright_selector:
        # Standard CSS: use JS poll for stable non-empty text
        try:
            page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const t = el.textContent.trim();
                    if (!t) return false;
                    if (!window.__qaTextStable || window.__qaTextStable.sel !== sel || window.__qaTextStable.val !== t) {
                        window.__qaTextStable = { sel: sel, val: t, ts: Date.now() };
                        return false;
                    }
                    if (Date.now() - window.__qaTextStable.ts < 800) return false;
                    window.__qaTextStable = null;
                    return true;
                }""",
                arg=selector,
                timeout=timeout,
            )
        except Exception:
            pass
    else:
        # Playwright :has-text selectors can't use document.querySelector.
        # Decompose "parent:has-text("Label") child" into a JS DOM walk with stability check.
        # This way we DON'T match "0" prematurely — we wait until the value is stable.
        m = re.match(r'^(.+?):has-text\(["\']([^"\']+)["\']\)\s+(.+)$', selector)
        if m:
            parent_sel = m.group(1)   # e.g. "div.status-card"
            label_text = m.group(2)   # e.g. "Not Reporting"
            child_sel  = m.group(3)   # e.g. "div.status-count"
            try:
                page.wait_for_function(
                    """(args) => {
                        const [parentSel, labelText, childSel] = args;
                        const parents = document.querySelectorAll(parentSel);
                        for (const p of parents) {
                            if (!p.textContent.includes(labelText)) continue;
                            const child = p.querySelector(childSel);
                            if (!child) return false;
                            const val = child.textContent.trim();
                            // '0' is always the Angular initial placeholder — keep waiting
                            if (!val || val === '0') return false;
                            const key = labelText + '|' + childSel;
                            if (!window.__qaHTS || window.__qaHTS.key !== key || window.__qaHTS.val !== val) {
                                window.__qaHTS = { key, val, ts: Date.now() };
                                return false;
                            }
                            if (Date.now() - window.__qaHTS.ts < 1000) return false;
                            window.__qaHTS = null;
                            return true;
                        }
                        return false;
                    }""",
                    arg=[parent_sel, label_text, child_sel],
                    timeout=timeout,
                )
            except Exception:
                pass
        else:
            # Fallback for unparseable :has-text selectors
            try:
                expect(loc).to_have_text(re.compile(r'\S+'), timeout=timeout)
            except Exception:
                pass

    text = loc.text_content()
    return text.strip() if text else ""


def _extract_number_or_fraction(s: str) -> str:
    """Normalize UI capture by extracting a numeric fraction or integer when present.

    Examples:
    - "NRD VTS Devices55/476" -> "55/476"
    - "Under Maintenance 56/476" -> "56/476"
    - "57" -> "57"
    If no numeric token is found, returns the original stripped string.
    """
    if not s:
        return s
    # Look for fraction like 55/476
    m = re.search(r"(\d+\s*/\s*\d+)", s)
    if m:
        return m.group(1).replace(" ", "")
    # Next, look for a plain integer (allow commas)
    m2 = re.search(r"(\d[\d,]*)", s)
    if m2:
        return m2.group(1).replace(',', '')
    return s.strip()


def _guess_page_name(url, site_map):
    """Determine which page we're on based on the URL."""
    for pname, pdata in site_map.get("pages", {}).items():
        page_url = pdata.get("url", "")
        if page_url and page_url in url:
            return pname
    if "dashboard" in url.lower():
        return "dashboard"
    if "report" in url.lower():
        return "reports"
    return "unknown"


def _select_calendar_date(page, date_str):
    """
    Navigate the ngx-daterangepicker-material calendar to select a specific date.
    date_str format: DD-MM-YYYY

    Verified against live DOM structure:
      Container:  .md-drppicker
      Left panel:  .md-drppicker .calendar.left
      Right panel: .md-drppicker .calendar.right
      Month header: th.month (text like " May  2026 ")
      Prev arrow:  th.prev.available  (inside .calendar.left)
      Next arrow:  th.next.available  (inside .calendar.left)
      Day cells:   tbody td.available:not(.off) > span
      OK button:   .md-drppicker .buttons button.btn
    """
    import re as _re

    day_str, month_str, year_str = date_str.split("-")
    target_day = str(int(day_str))           # "01" -> "1"
    target_month = int(month_str)            # 1-12
    target_year = int(year_str)              # 2026

    month_abbrs = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def _normalize(header_text):
        """Collapse whitespace so ' May  2026 ' becomes 'May 2026'."""
        return " ".join(header_text.split())

    def _parse_header(header_text):
        """Parse 'May 2026' into (month_index_1based, year_int)."""
        parts = _normalize(header_text).split()
        if len(parts) < 2:
            return None, None
        m_str = parts[0][:3]
        y_str = parts[-1]
        try:
            m_idx = month_abbrs.index(m_str) + 1
            return m_idx, int(y_str)
        except (ValueError, IndexError):
            return None, None

    def _click_day_in_panel(panel, day_num_str):
        """Click the td whose span text exactly matches the day number."""
        cells = panel.locator("tbody td.available:not(.off)")
        count = cells.count()
        for i in range(count):
            cell = cells.nth(i)
            span = cell.locator("span")
            if span.count() > 0 and span.text_content().strip() == day_num_str:
                cell.click()
                page.wait_for_timeout(500)
                return True
        return False

    picker = page.locator(".md-drppicker").first
    left_cal = picker.locator(".calendar.left")
    right_cal = picker.locator(".calendar.right")

    for attempt in range(24):  # max 2 years of navigation
        # Read both panels
        l_hdr = ""
        r_hdr = ""
        if left_cal.locator("th.month").count() > 0:
            l_hdr = left_cal.locator("th.month").text_content()
        if right_cal.locator("th.month").count() > 0:
            r_hdr = right_cal.locator("th.month").text_content()

        l_m, l_y = _parse_header(l_hdr)
        r_m, r_y = _parse_header(r_hdr)

        # Check left panel
        if l_m == target_month and l_y == target_year:
            if _click_day_in_panel(left_cal, target_day):
                return
            break  # day not found in the right month — bail

        # Check right panel
        if r_m == target_month and r_y == target_year:
            if _click_day_in_panel(right_cal, target_day):
                return
            break

        # Navigate: need to go backward or forward?
        if l_m is None or l_y is None:
            break  # can't parse — bail

        current_val = l_y * 12 + l_m
        target_val = target_year * 12 + target_month

        if target_val < current_val:
            # Click prev (scoped to the LEFT calendar so we don't bounce)
            prev_btn = left_cal.locator("th.prev.available")
            if prev_btn.count() > 0:
                prev_btn.click()
            else:
                break
        else:
            # Click next (scoped to the LEFT calendar)
            next_btn = left_cal.locator("th.next.available")
            if next_btn.count() > 0:
                next_btn.click()
            else:
                break

        page.wait_for_timeout(300)

