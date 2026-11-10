# Project & Task Management System — Backend

Django REST Framework API for the Project & Task Management System.  
The React frontend is a **separate** project and is not included here.

## Requirements

- Python 3.11+
- PostgreSQL
- Git

## Project structure

```text
backend/
  apps/                 # Domain apps (accounts, companies, projects, tasks, ...)
  config/               # Django settings, urls, wsgi/asgi
  manage.py
  requirements.txt
  .env.example          # Environment variable template
  .env                  # Local secrets (do not commit)
```

## Setup

### 1. Clone the repository

```bash
git clone <repository-url>
cd "<project-folder>"
```

### 2. Create and activate a virtual environment

**Windows (PowerShell):**

```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**macOS / Linux:**

```bash
cd backend
python -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

Install packages **only inside the venv**. Do not install project dependencies globally.

### 4. Configure environment variables

Copy the example env file and edit values for your machine:

```powershell
copy .env.example .env
```

```bash
cp .env.example .env
```

#### `.env` model

| Variable | Required | Description | Example |
|---|---|---|---|
| `POSTGRES_DB` | Yes | PostgreSQL database name | `magnusdb-14-10-2026` |
| `POSTGRES_USER` | Yes | Database user | `postgres` |
| `POSTGRES_PASSWORD` | Yes | Database password | `your_password` |
| `POSTGRES_HOST` | Yes | Database host | `127.0.0.1` |
| `POSTGRES_PORT` | Yes | Database port | `5432` |
| `SECRET_KEY` | Yes | Django secret key | long random string |
| `PLATFORM` | Yes | Runtime mode (`DEV` / `PROD`) | `DEV` |
| `ALLOWED_HOSTS` | Yes | Comma-separated hosts; `*` for local | `*` |

Example file content:

```env
POSTGRES_DB='your_database_name'
POSTGRES_USER='postgres'
POSTGRES_PASSWORD='your_password'
POSTGRES_HOST='127.0.0.1'
POSTGRES_PORT='5432'
SECRET_KEY='replace-with-a-long-random-secret-key'
PLATFORM='DEV'
ALLOWED_HOSTS='*'
```

Never commit the real `.env` file. Only `.env.example` belongs in git.

### 5. Create the PostgreSQL database

Create a database that matches `POSTGRES_DB` in `.env`, then confirm PostgreSQL is running on `POSTGRES_HOST`:`POSTGRES_PORT`.

### 6. Run migrations

After models are implemented by the team:

```bash
python manage.py makemigrations
python manage.py migrate
```

### 7. Create a superuser (optional)

```bash
python manage.py createsuperuser
```

### 8. Start the API server

```bash
python manage.py runserver
```

API base URL (local): `http://127.0.0.1:8000/`

## Apps (SRS mapping)

| App | Responsibility |
|---|---|
| `accounts` | Authentication and user management |
| `companies` | Multi-company management |
| `organization` | Departments, teams, reporting hierarchy |
| `rbac` | Roles and permissions |
| `projects` | Projects, phases, milestones |
| `tasks` | Tasks, subtasks, checklists, comments, attachments |
| `tracking` | Progress updates and time tracking |
| `reviews` | Review and approval |
| `notifications` | Notifications and delivery |
| `dashboards` | CEO / role dashboards and productivity views |
| `reports` | Reports and analytics |
| `risks` | Risks, issues, blockers |
| `system_settings` | System administration and settings |
| `common` | Shared helpers |

Models, views, and serializers are intentionally stubbed for other developers to implement.

## Notes

- Backend stack: Django + Django REST Framework + PostgreSQL
- Frontend: React (separate repository)
- SRS / design documents are local-only and ignored by git
