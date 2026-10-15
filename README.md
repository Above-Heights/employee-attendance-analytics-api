# Employee Attendance & Analytics API

A RESTful backend application built using **Python, FastAPI, and MongoDB** to manage employee attendance, support attendance corrections, and generate attendance analytics.

## Tech Stack

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo
- Pydantic
- Uvicorn

## Features

- Employee creation and listing with filtering and pagination
- Employee punch-in and punch-out
- Attendance business rule validation
- Manual attendance regularization with audit history
- Employee monthly attendance summaries
- Department-wise attendance analytics
- Late-comers leaderboard
- Daily attendance trends and moving averages
- MongoDB indexing and query execution analysis

## Project Structure

```text
employee-attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── sample_seed.py
├── requirements.txt
├── README.md
├── REVIEW.md
└── DECISIONS.md
```

## Local Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/employee-attendance-analytics-api.git
cd employee-attendance-analytics-api
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure MongoDB

Set the following environment variables:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

MongoDB 6.0 or newer is required.

### 5. Run the application

```bash
uvicorn app.main:app --port 8000
```

API documentation:

`http://localhost:8000/docs`

Health endpoint:

`http://localhost:8000/health`

## Documentation

- `REVIEW.md` — Review findings and corrections made to the starter application.
- `DECISIONS.md` — Technical decisions involving indexes, concurrency, ranking, and scalability.

## Implementation Notes

[Add verified test results, known limitations, and any incomplete endpoints before submission.]
