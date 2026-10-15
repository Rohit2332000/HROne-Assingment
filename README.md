# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne Employee Attendance & Analytics assignment.

## Contract Notes

- All application code and route implementations are contained in `app/main.py`.
- Employee identity is `emp_code`; the attendance natural key is (`emp_code`, `date`). MongoDB `_id` is never exposed.
- API timestamps use integer epoch milliseconds; MongoDB stores BSON UTC datetimes.
- Calendar dates use `YYYY-MM-DD`, month filters use `YYYY-MM`, and shift times use `HH:MM` in IST.
- MongoDB indexes are created idempotently during application startup.
- Analytics read stored derived fields and do not recalculate seeded data.

## Requirements

- Python 3.11+
- MongoDB 6.0+

## Setup (Windows PowerShell)

Create and activate a virtual environment, then install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create the environment configuration file:

```powershell
Copy-Item .env.example .env
notepad .env
```

Configure `MONGO_URI` and `MONGO_DB` according to the environment variables expected by `app/main.py`. For a local MongoDB instance, the connection URI is typically `mongodb://localhost:27017`.

Start MongoDB before running the application.

## Run the Application

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open the interactive API documentation at:

http://127.0.0.1:8000/docs

The `/health` endpoint can be used to verify application health and MongoDB connectivity.
