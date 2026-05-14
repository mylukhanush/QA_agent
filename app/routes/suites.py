from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, current_app
from db import db
from db.models import TestCase, TestSuite, SuiteRun, TestRun, Site
import uuid

suites_bp = Blueprint("suites", __name__)

@suites_bp.route("/api/suites", methods=["GET"])
def list_suites():
    suites = TestSuite.query.order_by(TestSuite.name).all()
    return jsonify([{
        "id": str(s.id),
        "name": s.name,
        "description": s.description,
        "test_cases_count": len(s.test_cases)
    } for s in suites])

@suites_bp.route("/api/suites/<suite_id>", methods=["GET"])
def get_suite(suite_id):
    suite = TestSuite.query.get_or_404(suite_id)
    return jsonify({
        "id": str(suite.id),
        "name": suite.name,
        "description": suite.description,
        "test_cases": [{
            "id": str(tc.id),
            "name": tc.name,
            "situation_description": tc.situation_description,
            "category": tc.category
        } for tc in suite.test_cases]
    })

@suites_bp.route("/api/test-cases/<tc_id>", methods=["GET"])
def get_test_case(tc_id):
    tc = TestCase.query.get_or_404(tc_id)
    return jsonify({
        "id": str(tc.id),
        "name": tc.name,
        "situation_description": tc.situation_description,
        "category": tc.category,
        "test_plan": tc.test_plan,
        "steps": tc.steps,
        "created_at": tc.created_at.strftime('%b %d, %Y %H:%M') if tc.created_at else None
    })

@suites_bp.route("/api/test-cases/<tc_id>/site-runs", methods=["GET"])
def get_test_case_site_runs(tc_id):
    """Return latest run per site for a test case, newest first."""
    tc = TestCase.query.get_or_404(tc_id)
    runs = (
        TestRun.query
        .filter_by(test_case_id=tc.id)
        .order_by(TestRun.started_at.desc())
        .all()
    )

    latest_by_site = {}
    ordered = []
    for run in runs:
        site_name = run.site.name if run.site else None
        if not site_name or site_name in latest_by_site:
            continue
        payload = {
            "site": site_name,
            "run_id": str(run.id),
            "status": run.status,
            "started_at": run.started_at.strftime('%b %d, %Y %H:%M') if run.started_at else None,
        }
        latest_by_site[site_name] = payload
        ordered.append(payload)

    return jsonify({
        "test_case_id": str(tc.id),
        "sites": ordered
    })

@suites_bp.route("/api/suites", methods=["POST"])
def create_suite():
    data = request.json
    name = data.get("name")
    if not name:
        return jsonify({"error": "Suite name is required"}), 400
    
    suite = TestSuite(name=name, description=data.get("description"))
    db.session.add(suite)
    db.session.commit()
    return jsonify({"id": str(suite.id), "name": suite.name})

@suites_bp.route("/api/suites/<suite_id>", methods=["PUT"])
def update_suite(suite_id):
    suite = TestSuite.query.get_or_404(suite_id)
    data = request.json or {}

    name = (data.get("name") or "").strip()
    description = data.get("description")

    if not name:
        return jsonify({"error": "Suite name is required"}), 400

    existing = TestSuite.query.filter(TestSuite.name == name, TestSuite.id != suite.id).first()
    if existing:
        return jsonify({"error": "Suite name already exists"}), 400

    suite.name = name
    suite.description = description
    db.session.commit()
    return jsonify({
        "id": str(suite.id),
        "name": suite.name,
        "description": suite.description
    })

@suites_bp.route("/api/suites/<suite_id>", methods=["DELETE"])
def delete_suite(suite_id):
    suite = TestSuite.query.get_or_404(suite_id)

    # Remove suite references from historical runs before deleting suite runs.
    suite_runs = SuiteRun.query.filter_by(suite_id=suite.id).all()
    suite_run_ids = [sr.id for sr in suite_runs]
    if suite_run_ids:
        TestRun.query.filter(TestRun.suite_run_id.in_(suite_run_ids)).update(
            {"suite_run_id": None}, synchronize_session=False
        )
        for sr in suite_runs:
            db.session.delete(sr)

    suite.test_cases = []
    db.session.delete(suite)
    db.session.commit()
    return jsonify({"status": "deleted", "id": suite_id})

@suites_bp.route("/api/test-cases/<tc_id>/save-to-suite", methods=["POST"])
def save_to_suite(tc_id):
    data = request.json
    tc_name = data.get("name")
    suite_id = data.get("suite_id")
    new_suite_name = data.get("new_suite_name")

    test_case = TestCase.query.get_or_404(tc_id)
    if tc_name:
        test_case.name = tc_name

    if new_suite_name:
        suite = TestSuite(name=new_suite_name)
        db.session.add(suite)
        db.session.flush()
    elif suite_id:
        suite = TestSuite.query.get(suite_id)
    else:
        db.session.commit()
        return jsonify({"status": "updated", "id": str(test_case.id)})

    if suite and test_case not in suite.test_cases:
        suite.test_cases.append(test_case)
    
    db.session.commit()
    return jsonify({"status": "saved", "id": str(test_case.id), "suite_id": str(suite.id) if suite else None})

@suites_bp.route("/api/suites/<suite_id>/run", methods=["POST"])
def run_suite(suite_id):
    suite = TestSuite.query.get_or_404(suite_id)
    if not suite.test_cases:
        return jsonify({"error": "This suite has no test cases"}), 400

    # Create a SuiteRun record
    suite_run = SuiteRun(suite_id=suite.id, status="running")
    db.session.add(suite_run)
    db.session.commit()

    from app.routes.runner import _launch_run_execution
    total_runs = 0
    used_sites = set()

    for tc in suite.test_cases:
        test_plan = tc.test_plan or {
            "description": tc.situation_description,
            "category": tc.category,
            "steps": tc.steps or [],
        }

        # Run this test case only on sites where it was previously executed.
        historical_site_ids = {
            r.site_id for r in tc.test_runs if r.site_id is not None
        }
        if not historical_site_ids:
            # If no history exists for this test case, skip it to avoid running on all sites unexpectedly.
            continue

        sites = (
            Site.query
            .filter(Site.id.in_(list(historical_site_ids)), Site.is_active.is_(True))
            .all()
        )
        if not sites:
            continue

        run_ids = {}
        for site in sites:
            tr = TestRun(
                test_case_id=tc.id,
                site_id=site.id,
                suite_run_id=suite_run.id,
                triggered_by="web",
                status="running",
            )
            db.session.add(tr)
            db.session.flush()
            run_ids[site.name] = str(tr.id)
            total_runs += 1
            used_sites.add(site.name)

        db.session.commit()
        _launch_run_execution(current_app._get_current_object(), test_plan, run_ids)

    if total_runs == 0:
        suite_run.status = "error"
        suite_run.finished_at = datetime.now(timezone.utc)
        suite_run.duration_ms = 0
        db.session.commit()
        return jsonify({
            "error": "No eligible historical site runs found for suite test cases."
        }), 400

    return jsonify({
        "status": "triggered",
        "suite_run_id": str(suite_run.id),
        "message": f"Triggered {total_runs} test runs across {len(used_sites)} sites."
    })

@suites_bp.route("/api/test-cases/<tc_id>/run", methods=["POST"])
def run_single_test_case(tc_id):
    test_case = TestCase.query.get_or_404(tc_id)
    sites = Site.query.filter_by(is_active=True).all()
    if not sites:
        return jsonify({"error": "No active sites available"}), 400

    test_plan = test_case.test_plan or {
        "description": test_case.situation_description,
        "category": test_case.category,
        "steps": test_case.steps or [],
    }

    run_ids = {}
    for site in sites:
        tr = TestRun(
            test_case_id=test_case.id,
            site_id=site.id,
            triggered_by="web",
            status="running",
        )
        db.session.add(tr)
        db.session.flush()
        run_ids[site.name] = str(tr.id)

    db.session.commit()

    from app.routes.runner import _launch_run_execution
    _launch_run_execution(current_app._get_current_object(), test_plan, run_ids)

    return jsonify({
        "status": "triggered",
        "test_case_id": str(test_case.id),
        "run_ids": run_ids,
        "message": f"Triggered {len(run_ids)} run(s) for test case."
    })

@suites_bp.route("/api/suites/<suite_id>/test-cases/<tc_id>", methods=["DELETE"])
def remove_test_case_from_suite(suite_id, tc_id):
    suite = TestSuite.query.get_or_404(suite_id)
    test_case = TestCase.query.get_or_404(tc_id)

    if test_case in suite.test_cases:
        suite.test_cases.remove(test_case)
        db.session.commit()
        return jsonify({"status": "removed", "suite_id": suite_id, "test_case_id": tc_id})

    return jsonify({"error": "Test case not found in suite"}), 404
