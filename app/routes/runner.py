"""
Runner routes — generate tests, execute them, poll progress.
"""
import threading
import uuid
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from flask import Blueprint, render_template, request, jsonify, current_app

from db import db
from db.models import Site, TestCase, TestRun

runner_bp = Blueprint("runner", __name__)

# In-memory run tracker (keyed by run_id)
_active_runs = {}
RUNNING_DELETE_GRACE_PERIOD = timedelta(hours=2)


def _upsert_generated_test_case(existing_test_case_id, situation, test_plan):
    """Create or refresh a generated test case before it is executed."""
    test_case = None
    if existing_test_case_id:
        test_case = TestCase.query.get(existing_test_case_id)

    if test_case is None:
        test_case = TestCase()
        db.session.add(test_case)

    test_case.situation_description = situation or test_plan.get("description", "")
    test_case.user_prompt = situation or None
    test_case.category = test_plan.get("category", "data_presence")
    test_case.steps = test_plan.get("steps", [])
    test_case.test_plan = test_plan

    if not test_case.name:
        test_case.name = test_plan.get("testName") or test_plan.get("description")

    db.session.flush()
    return test_case


def _is_run_actively_running(run):
    """Return True only for runs that are likely still executing right now."""
    if run.status != "running":
        return False

    worker = _active_runs.get(str(run.id))
    if worker and worker.is_alive():
        return True

    if not run.started_at:
        return False

    started_at = run.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    return datetime.now(timezone.utc) - started_at < RUNNING_DELETE_GRACE_PERIOD


def _delete_run_record(run):
    """Delete a run and its dependent rows."""
    from db.models import RunStep, ValueCapture

    RunStep.query.filter_by(run_id=run.id).delete(synchronize_session=False)
    ValueCapture.query.filter_by(run_id=run.id).delete(synchronize_session=False)
    db.session.delete(run)


def _launch_run_execution(app, test_plan, run_ids):
    """Run the executor in a background thread and track active run IDs."""
    import traceback
    import sys

    def _execute(app_obj, plan, rids):
        print(f"\n[EXECUTOR] Starting background test execution for runs: {rids}", flush=True)
        with app_obj.app_context():
            try:
                from runner.executor import execute_test_plan
                print("[EXECUTOR] Handing off to Playwright executor...", flush=True)
                execute_test_plan(plan, rids)
                print("[EXECUTOR] Execution completed successfully.", flush=True)
            except Exception as exc:
                # Log the full error so it's visible in the server console
                print(f"\n[EXECUTOR ERROR] Background test execution failed:", file=sys.stderr, flush=True)
                traceback.print_exc(file=sys.stderr)

                # Mark all runs as 'error' so the UI stops spinning
                for site_name, rid in rids.items():
                    try:
                        run = TestRun.query.get(rid)
                        if run and run.status == "running":
                            run.status = "error"
                            run.finished_at = datetime.now(timezone.utc)
                            if run.started_at:
                                run.duration_ms = int(
                                    (run.finished_at - run.started_at).total_seconds() * 1000
                                )
                            db.session.commit()
                    except Exception:
                        traceback.print_exc(file=sys.stderr)

                    # Record an error step so the UI shows the reason
                    try:
                        from db.models import RunStep
                        step = RunStep(
                            run_id=rid,
                            step_order=9999,
                            action="error",
                            description="Executor failed to start",
                            status="error",
                            error_message=str(exc),
                        )
                        db.session.add(step)
                        db.session.commit()
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
            finally:
                for rid in rids.values():
                    _active_runs.pop(str(rid), None)

    thread = threading.Thread(
        target=_execute,
        args=(app, test_plan, run_ids),
        daemon=True,
    )
    for rid in run_ids.values():
        _active_runs[str(rid)] = thread
    thread.start()


@runner_bp.route("/")
def dashboard():
    """Main dashboard page."""
    sites = Site.query.filter_by(is_active=True).all()

    # Recent runs (last 20) retained for any existing UI dependencies
    recent_runs = (
        TestRun.query
        .order_by(TestRun.started_at.desc())
        .limit(20)
        .all()
    )
    for run in recent_runs:
        run.is_stale_running = run.status == "running" and not _is_run_actively_running(run)

    # Date-wise summary (last 30 days, including today)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs_for_summary = (
        TestRun.query
        .filter(TestRun.started_at >= since)
        .order_by(TestRun.started_at.desc())
        .all()
    )

    by_date = defaultdict(lambda: {
        "date": None,
        "total": 0,
        "pass": 0,
        "fail": 0,
        "error": 0,
        "running": 0,
    })
    for run in runs_for_summary:
        if not run.started_at:
            continue
        day_key = run.started_at.date().isoformat()
        row = by_date[day_key]
        row["date"] = run.started_at.date()
        row["total"] += 1
        status = (run.status or "").lower()
        if status in ("pass", "fail", "error", "running"):
            row[status] += 1

    daily_execution_summary = sorted(
        by_date.values(),
        key=lambda r: r["date"],
        reverse=True,
    )

    return render_template(
        "dashboard.html",
        sites=sites,
        recent_runs=recent_runs,
        daily_execution_summary=daily_execution_summary,
    )


@runner_bp.route("/run")
def run_page():
    """Test generation and execution page."""
    sites = Site.query.filter_by(is_active=True).all()
    return render_template("run.html", sites=sites)


@runner_bp.route("/api/generate", methods=["POST"])
def api_generate():
    """Generate a test plan from a situation description using Gemini."""
    data = request.json
    situation = data.get("situation", "")
    target_sites = data.get("sites", ["jhs82"])
    existing_test_case_id = data.get("test_case_id")

    if not situation:
        return jsonify({"error": "Situation description is required"}), 400

    try:
        from ai.generator import generate_test_plan
        plan = generate_test_plan(situation, target_sites)
        test_case = _upsert_generated_test_case(existing_test_case_id, situation, plan)
        db.session.commit()

        payload = dict(plan)
        payload["test_case_id"] = str(test_case.id)
        payload["test_case_name"] = test_case.name
        return jsonify(payload)
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500


@runner_bp.route("/api/run", methods=["POST"])
def api_run():
    """Execute a test plan in a background thread. Returns run_id immediately."""
    data = request.json
    test_plan = data.get("test_plan")
    situation = (data.get("situation") or "").strip()
    target_sites = data.get("sites", [])
    existing_test_case_id = data.get("test_case_id")

    if not test_plan or not target_sites:
        return jsonify({"error": "test_plan and sites are required"}), 400

    tc = _upsert_generated_test_case(existing_test_case_id, situation, test_plan)

    run_ids = {}
    for site_name in target_sites:
        site = Site.query.filter_by(name=site_name).first()
        if not site:
            continue
        tr = TestRun(
            test_case_id=tc.id,
            site_id=site.id,
            triggered_by="web",
            status="running",
        )
        db.session.add(tr)
        db.session.flush()
        run_ids[site_name] = str(tr.id)

    db.session.commit()

    _launch_run_execution(current_app._get_current_object(), test_plan, run_ids)

    return jsonify({"run_ids": run_ids, "test_case_id": str(tc.id)})


@runner_bp.route("/api/runs/<run_id>/progress")
def api_run_progress(run_id):
    """Return current step results for live HTMX polling."""
    from db.models import RunStep
    run = TestRun.query.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404

    steps = (
        RunStep.query
        .filter_by(run_id=run.id)
        .order_by(RunStep.step_order)
        .all()
    )

    return jsonify({
        "run_id": str(run.id),
        "status": run.status,
        "duration_ms": run.duration_ms,
        "steps": [
            {
                "step_order": s.step_order,
                "action": s.action,
                "description": s.description,
                "status": s.status,
                "error_message": s.error_message,
                "screenshot_path": s.screenshot_path,
            }
            for s in steps
        ],
    })


@runner_bp.route("/api/runs/<run_id>")
def api_run_detail(run_id):
    """Full run data as JSON."""
    from db.models import RunStep, ValueCapture
    run = TestRun.query.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404

    steps = RunStep.query.filter_by(run_id=run.id).order_by(RunStep.step_order).all()
    captures = ValueCapture.query.filter_by(run_id=run.id).all()

    return jsonify({
        "run_id": str(run.id),
        "test_case_id": str(run.test_case_id),
        "site": run.site.name,
        "status": run.status,
        "triggered_by": run.triggered_by,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": run.duration_ms,
        "report_path": run.report_path,
        "user_prompt": run.test_case.user_prompt if run.test_case else None,
        "test_plan": run.test_case.test_plan if run.test_case else None,
        "steps": [
            {
                "step_order": s.step_order,
                "action": s.action,
                "description": s.description,
                "status": s.status,
                "error_message": s.error_message,
                "screenshot_path": s.screenshot_path,
                "executed_at": s.executed_at.isoformat() if s.executed_at else None,
            }
            for s in steps
        ],
        "value_captures": [
            {
                "label": c.label,
                "page": c.page,
                "selector": c.selector,
                "captured_value": c.captured_value,
                "expected_value": c.expected_value,
                "matched": c.matched,
                "captured_at": c.captured_at.isoformat() if c.captured_at else None,
            }
            for c in captures
        ],
    })


@runner_bp.route("/api/runs/<run_id>/rerun", methods=["POST"])
def api_rerun(run_id):
    """Create and start a new run from an existing run's saved test plan."""
    source_run = TestRun.query.get(run_id)
    if not source_run or not source_run.test_case:
        return jsonify({"error": "Run or test case not found"}), 404

    data = request.get_json() or {}
    site_name = (data.get("site") or "").strip()
    if not site_name:
        return jsonify({"error": "site is required"}), 400

    allowed_sites = {"jhs81", "jhs82", "jhs83", "jhs84"}
    if site_name not in allowed_sites:
        return jsonify({"error": f"Choose one of: {', '.join(sorted(allowed_sites))}"}), 400

    site = Site.query.filter_by(name=site_name, is_active=True).first()
    if not site:
        return jsonify({"error": f"Unknown or inactive site: {site_name}"}), 400

    test_case = source_run.test_case
    test_plan = test_case.test_plan or {
        "description": test_case.situation_description,
        "category": test_case.category,
        "steps": test_case.steps,
    }

    new_run = TestRun(
        test_case_id=test_case.id,
        site_id=site.id,
        triggered_by="web",
        status="running",
    )
    db.session.add(new_run)
    db.session.commit()

    run_ids = {site.name: str(new_run.id)}
    _launch_run_execution(current_app._get_current_object(), test_plan, run_ids)

    return jsonify({
        "run_id": str(new_run.id),
        "run_ids": run_ids,
        "detail_url": f"/runs/{new_run.id}",
    })


@runner_bp.route("/api/sites")
def api_sites():
    """List all sites with status."""
    sites = Site.query.all()
    return jsonify([
        {
            "id": str(s.id),
            "name": s.name,
            "base_url": s.base_url,
            "is_active": s.is_active,
        }
        for s in sites
    ])


@runner_bp.route("/api/runs/<run_id>", methods=["DELETE"])
def api_delete_run(run_id):
    """Delete a single test run and its related steps/captures."""
    run = TestRun.query.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404

    if _is_run_actively_running(run):
        return jsonify({"error": "Cannot delete a run that is still active or started less than 2 hours ago"}), 400

    _delete_run_record(run)
    db.session.commit()

    return jsonify({"deleted": [str(run.id)]})


@runner_bp.route("/api/runs/delete", methods=["POST"])
def api_delete_runs():
    """Bulk delete multiple runs. Expects JSON {"run_ids": [id,...]}."""
    data = request.get_json() or {}
    run_ids = data.get("run_ids") or []
    if not isinstance(run_ids, (list, tuple)) or not run_ids:
        return jsonify({"error": "run_ids list required"}), 400

    # fetch runs
    runs = TestRun.query.filter(TestRun.id.in_(run_ids)).all()
    found_ids = {str(r.id): r for r in runs}

    running = [rid for rid, r in found_ids.items() if _is_run_actively_running(r)]
    if running:
        return jsonify({
            "error": "Cannot delete runs that are still active or started less than 2 hours ago",
            "running": running,
        }), 400

    deleted = []
    for rid, r in found_ids.items():
        _delete_run_record(r)
        deleted.append(rid)

    db.session.commit()

    skipped = [rid for rid in run_ids if rid not in deleted]
    return jsonify({"deleted": deleted, "skipped": skipped})
