#!/usr/bin/env bash
# exit on error
set -o errexit

# Install dependencies
pip install -r requirements.txt

# Run database migrations safely
python manage.py migrate

# Collect static files for the Admin dashboard
python manage.py collectstatic --no-input