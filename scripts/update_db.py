import sys
import os
from dotenv import load_dotenv

# Add the project root to sys.path so we can import 'app' and 'db'
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from app import create_app
from db import db
from db.models import * # Ensure all models are loaded
from sqlalchemy import text

def update_database():
    app = create_app()
    with app.app_context():
        print("Initializing new database tables...")
        try:
            # 1. Create new tables (test_suites, suite_runs, suite_test_cases)
            db.create_all()
            
            # 2. Add columns to existing tables if they don't exist
            # Postgres specific syntax
            print("Adding missing columns to existing tables...")
            
            # Add 'name' to test_cases
            db.session.execute(text("ALTER TABLE test_cases ADD COLUMN IF NOT EXISTS name VARCHAR(255)"))
            
            # Add 'suite_run_id' to test_runs
            # Note: We use the variant logic from the model. In Postgres it's UUID.
            db.session.execute(text("ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS suite_run_id UUID REFERENCES suite_runs(id)"))
            
            db.session.commit()
            print("Successfully updated database schema and columns.")
        except Exception as e:
            db.session.rollback()
            print(f"Error updating database: {e}")


if __name__ == "__main__":
    update_database()
