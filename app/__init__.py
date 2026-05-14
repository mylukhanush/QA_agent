"""
Flask application factory.
"""
import os
from flask import Flask
from db import db, migrate


def create_app(config_overrides=None):
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # ── Core config ───────────────────────────────────────────────
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SCREENSHOTS_DIR"] = os.getenv("SCREENSHOTS_DIR", "screenshots")
    app.config["REPORTS_DIR"] = os.getenv("REPORTS_DIR", "reports")
    app.config["SITE_MAP_PATH"] = os.getenv("SITE_MAP_PATH", "site-map.json")

    if config_overrides:
        app.config.update(config_overrides)

    # ── Extensions ────────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'db', 'migrations'))

    # ── Ensure directories exist ──────────────────────────────────
    os.makedirs(app.config["SCREENSHOTS_DIR"], exist_ok=True)
    os.makedirs(app.config["REPORTS_DIR"], exist_ok=True)

    # ── Register blueprints ───────────────────────────────────────
    from app.routes.crawler import crawler_bp
    from app.routes.runner import runner_bp
    from app.routes.results import results_bp
    from app.routes.history import history_bp
    from app.routes.suites import suites_bp

    app.register_blueprint(crawler_bp)
    app.register_blueprint(runner_bp)
    app.register_blueprint(results_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(suites_bp)

    # ── Register CLI commands ─────────────────────────────────────
    from cli.commands import register_cli
    register_cli(app)

    return app
