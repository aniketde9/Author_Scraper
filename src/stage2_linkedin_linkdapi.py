"""Backward compatibility for ``python -m src.stage2_linkedin_linkdapi``.

Prefer: ``python -m src.csv_linkedin``
"""

from src.csv_linkedin import app

if __name__ == "__main__":
    app()
