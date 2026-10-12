# Canefield
Canefield: a Flask and PostgreSQL sugarcane advisory web app. It turns farm soil, crop-stage and irrigation data into quantified irrigation, fertilizer and alert recommendations, with a farm dashboard, yield estimate and field-check history. DBMS course project.
# Canefield: Sugarcane Advisory

A web app that helps sugarcane farmers decide what to do on each field. It combines
farm profile data (area, soil type, pH, soil moisture, crop stage, irrigation method)
with a research dataset to produce clear, quantified actions.

Built as a DBMS course project with Flask and PostgreSQL.

## Features
- **Recommendations feed**: prioritised irrigation, fertilizer and alert actions with
  severity, quantities (kL, pump hours, kg) and a "why this recommendation?" explanation.
  Mark as done, dismiss, undo, and re-check all fields.
- **Dashboard**: moisture and pH status per farm, crop-stage tracker, pending actions,
  yield trend and research-dataset charts.
- **Field check**: weather and nutrient inputs give a yield estimate (scikit-learn model
  trained on the dataset) and a disease-risk screen.
- **History and export**: searchable field-check history with CSV/PDF export.
- **Admin panel**: users, farms, feedback and dataset summary.
- **Interactive intro page** with a live field simulator, and a redesigned login/register page.

## Tech stack
Python, Flask, PostgreSQL (psycopg 3), pandas, scikit-learn, Chart.js, gunicorn.

## Database design
Tables: `users`, `farms`, `advisories`, `recommendations`, `feedback`, plus the imported
`sugarcane_dataset` table. Farms belong to users, and advisories and recommendations
belong to both a user and a farm, with cascading deletes.

## Run locally
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
createdb sugarcane_advisory
export DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_PASSWORD=yourpassword
python3 import_dataset.py
python3 app.py
```
Open http://127.0.0.1:5000

## Environment variables
| Variable | Purpose |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | PostgreSQL connection |
| `SECRET_KEY` | Flask session key (required in production) |
| `FLASK_ENV=production` | Enables the production safety check |

## Deploying on Render
Create a PostgreSQL database and a Web Service from this repo
(start command `gunicorn app:app`), then set the variables above.

## Disclaimer
Quantities are guideline estimates from the data entered, not live sensor readings.
Verify with a soil test and local agricultural advice.
