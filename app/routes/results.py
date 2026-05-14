"""
Results routes — run history list and run detail page.
"""
import os

from flask import Blueprint, abort, current_app, render_template, request, send_from_directory, url_for

from db import db
from db.models import Site, TestRun, RunStep, ValueCapture
from app.routes.runner import _is_run_actively_running

results_bp = Blueprint("results", __name__)


def _screenshot_filename(path):
    """Return the screenshot filename from a DB path stored with either slash style."""
    if not path:
        return None
    return path.replace("\\", "/").split("/")[-1]


@results_bp.route("/screenshots/<path:filename>")
def screenshot_file(filename):
    """Serve captured failure screenshots from the configured screenshots directory."""
    safe_filename = _screenshot_filename(filename)
    if not safe_filename:
        abort(404)
    screenshot_dir = os.path.abspath(current_app.config["SCREENSHOTS_DIR"])
    return send_from_directory(screenshot_dir, safe_filename)


@results_bp.route("/runs")
def runs_list():
    """Redesigned history page focusing on Test Suites."""
    from db.models import TestSuite, SuiteRun
    
    # Fetch all suites
    suites = TestSuite.query.order_by(TestSuite.name).all()
    
    # Fetch recent suite runs
    suite_runs = SuiteRun.query.order_by(SuiteRun.started_at.desc()).limit(50).all()
    
    # Individual runs (legacy/backup view)
    site_filter = request.args.get("site")
    status_filter = request.args.get("status")
    query = TestRun.query.order_by(TestRun.started_at.desc())
    if site_filter:
        site = Site.query.filter_by(name=site_filter).first()
        if site: query = query.filter_by(site_id=site.id)
    if status_filter: query = query.filter_by(status=status_filter)
    
    runs = query.limit(50).all()
    for run in runs:
        run.is_stale_running = run.status == "running" and not _is_run_actively_running(run)

    sites = Site.query.filter_by(is_active=True).all()

    return render_template(
        "runs.html",
        suites=suites,
        suite_runs=suite_runs,
        runs=runs,
        sites=sites,
        site_filter=site_filter,
        status_filter=status_filter
    )


@results_bp.route("/runs/<run_id>")
def run_detail(run_id):
    """Detailed view of a single test run."""
    run = TestRun.query.get_or_404(run_id)
    steps = RunStep.query.filter_by(run_id=run.id).order_by(RunStep.step_order).all()
    captures = ValueCapture.query.filter_by(run_id=run.id).all()
    sites = (
        Site.query
        .filter(Site.name.in_(["jhs81", "jhs82", "jhs83", "jhs84"]), Site.is_active.is_(True))
        .order_by(Site.name)
        .all()
    )
    screenshot_dir = os.path.abspath(current_app.config["SCREENSHOTS_DIR"])
    for step in steps:
        filename = _screenshot_filename(step.screenshot_path)
        step.screenshot_filename = filename
        step.screenshot_url = url_for("results.screenshot_file", filename=filename) if filename else None
        step.screenshot_exists = (
            bool(filename) and os.path.exists(os.path.join(screenshot_dir, filename))
        )

    test_case = run.test_case
    prompt_text = ""
    test_plan_json = {}

    if test_case:
        prompt_text = test_case.user_prompt or test_case.situation_description
        test_plan_json = test_case.test_plan or {
            "description": test_case.situation_description,
            "category": test_case.category,
            "steps": test_case.steps,
        }

    return render_template(
        "run_detail.html",
        run=run,
        steps=steps,
        captures=captures,
        sites=sites,
        prompt_text=prompt_text,
        test_plan_json=test_plan_json,
    )
