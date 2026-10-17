
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Query


# --------------------------------------------------
# 1. CONFIGURATION
# --------------------------------------------------

app = FastAPI(
    title="Signalpost AI Agent",
    description=(
        "Company information from Norway's official "
        "public registry, with source references and history."
    ),
    version="1.0.0",
)

BASE_URL = (
    "https://data.brreg.no/enhetsregisteret/api/enheter"
)

DB_PATH = Path(__file__).resolve().parent / "signalpost.db"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "SignalpostAI/1.0",
}

TIMEOUT = 20


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
    )


# --------------------------------------------------
# 2. DATABASE
# --------------------------------------------------

@contextmanager
def get_db():
    connection = sqlite3.connect(
        str(DB_PATH),
        timeout=30,
    )
    connection.row_factory = sqlite3.Row

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                organization_number TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                organization_form TEXT,
                industry_code TEXT,
                industry_description TEXT,
                business_address TEXT,
                registry_last_changed TEXT,
                source_url TEXT NOT NULL,
                retrieved_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS company_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_number TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                old_data TEXT NOT NULL,
                new_data TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_history_organization_number
            ON company_history(organization_number)
        """)


initialize_database()


# --------------------------------------------------
# 3. VALIDATION AND SOURCE DATA
# --------------------------------------------------

def validate_org_number(org_number):
    number = str(org_number).strip()

    if not number.isdigit() or len(number) != 9:
        raise ValueError(
            "Norwegian organization number must contain "
            "exactly 9 digits."
        )

    return number


def normalize_company(data, source_url):
    number = str(data.get("organisasjonsnummer", "")).strip()
    name = data.get("navn")

    if not number.isdigit() or len(number) != 9:
        raise ValueError("Invalid company number in source data.")

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Company name missing from source data.")

    address = data.get("forretningsadresse") or {}
    industry = data.get("naeringskode1") or {}
    organization_form = data.get("organisasjonsform") or {}

    return {
        "organization_number": number,
        "name": name.strip(),
        "organization_form": organization_form.get("beskrivelse"),
        "industry_code": industry.get("kode"),
        "industry_description": industry.get("beskrivelse"),
        "business_address": address,
        "registry_last_changed": data.get("sistEndret"),
        "source_url": source_url,
        "retrieved_at": utc_now(),
    }


def fetch_company(org_number):
    number = validate_org_number(org_number)
    url = f"{BASE_URL}/{number}"

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT,
    )

    if response.status_code == 404:
        return None

    if response.status_code == 410:
        raise HTTPException(
            status_code=410,
            detail="This registry record is no longer available.",
        )

    response.raise_for_status()
    data = response.json()

    # Never accept a record belonging to another company.
    returned_number = str(
        data.get("organisasjonsnummer", "")
    ).strip()

    if returned_number != number:
        raise ValueError(
            "Company identity verification failed."
        )

    return normalize_company(data, url)


# --------------------------------------------------
# 4. SAVE DATA AND TRACK CHANGES
# --------------------------------------------------

def save_company(company):
    number = validate_org_number(
        company["organization_number"]
    )

    if not company.get("name"):
        raise ValueError("Company name is required.")

    address_json = canonical_json(
        company.get("business_address") or {}
    )

    new_data = {
        "organization_number": number,
        "name": company["name"],
        "organization_form": company.get("organization_form"),
        "industry_code": company.get("industry_code"),
        "industry_description": company.get(
            "industry_description"
        ),
        "business_address": address_json,
        "registry_last_changed": company.get(
            "registry_last_changed"
        ),
        "source_url": company["source_url"],
        "retrieved_at": company["retrieved_at"],
    }

    compared_fields = [
        "name",
        "organization_form",
        "industry_code",
        "industry_description",
        "business_address",
        "registry_last_changed",
    ]

    with get_db() as db:
        previous_row = db.execute(
            """
            SELECT *
            FROM companies
            WHERE organization_number = ?
            """,
            (number,),
        ).fetchone()

        change_recorded = False

        if previous_row:
            old_data = dict(previous_row)

            changed = any(
                old_data.get(field) != new_data.get(field)
                for field in compared_fields
            )

            if changed:
                db.execute(
                    """
                    INSERT INTO company_history (
                        organization_number,
                        changed_at,
                        old_data,
                        new_data
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        number,
                        utc_now(),
                        canonical_json(old_data),
                        canonical_json(new_data),
                    ),
                )
                change_recorded = True

        db.execute(
            """
            INSERT INTO companies (
                organization_number,
                name,
                organization_form,
                industry_code,
                industry_description,
                business_address,
                registry_last_changed,
                source_url,
                retrieved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(organization_number) DO UPDATE SET
                name = excluded.name,
                organization_form = excluded.organization_form,
                industry_code = excluded.industry_code,
                industry_description = excluded.industry_description,
                business_address = excluded.business_address,
                registry_last_changed = excluded.registry_last_changed,
                source_url = excluded.source_url,
                retrieved_at = excluded.retrieved_at
            """,
            (
                new_data["organization_number"],
                new_data["name"],
                new_data["organization_form"],
                new_data["industry_code"],
                new_data["industry_description"],
                new_data["business_address"],
                new_data["registry_last_changed"],
                new_data["source_url"],
                new_data["retrieved_at"],
            ),
        )

    return change_recorded


# --------------------------------------------------
# 5. RESPONSE HELPERS
# --------------------------------------------------

def decode_company_row(row):
    result = dict(row)

    try:
        result["business_address"] = json.loads(
            result["business_address"] or "{}"
        )
    except (json.JSONDecodeError, TypeError):
        result["business_address"] = {}

    return result


# --------------------------------------------------
# 6. HOME AND HEALTH
# --------------------------------------------------

@app.get("/")
def home():
    return {
        "project": "Signalpost AI Agent",
        "status": "running",
        "data_source": "Brønnøysund Register Centre",
        "database": str(DB_PATH),
        "documentation": "/docs",
    }


@app.get("/health")
def health():
    with get_db() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]

        history_total = db.execute(
            "SELECT COUNT(*) FROM company_history"
        ).fetchone()[0]

    return {
        "status": "healthy",
        "database_connected": True,
        "stored_company_profiles": total,
        "recorded_changes": history_total,
    }


# --------------------------------------------------
# 7. LOOK UP AND SAVE ONE COMPANY
# --------------------------------------------------

@app.get("/company/{org_number}")
def company_lookup(org_number: str):
    try:
        company = fetch_company(org_number)

        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Company not found in the official registry.",
            )

        changed = save_company(company)

        return {
            "company": company,
            "saved_to_database": True,
            "previous_data_changed": changed,
            "evidence": {
                "source_url": company["source_url"],
                "retrieved_at": company["retrieved_at"],
                "registry_last_changed": (
                    company["registry_last_changed"]
                ),
            },
        }

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    except requests.RequestException as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "Official registry request failed. "
                "Retry after checking the network."
            ),
        ) from error


# --------------------------------------------------
# 8. LIST STORED COMPANIES
# --------------------------------------------------

@app.get("/companies")
def list_companies(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    with get_db() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]

        rows = db.execute(
            """
            SELECT *
            FROM companies
            ORDER BY organization_number
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    return {
        "total_profiles": total,
        "limit": limit,
        "offset": offset,
        "companies": [
            decode_company_row(row) for row in rows
        ],
    }


# --------------------------------------------------
# 9. BULK COLLECTION: UP TO 1,000 RECORDS PER REQUEST
# --------------------------------------------------

@app.get("/companies/collect")
def collect_companies(
    target: int = Query(default=1000, ge=1, le=1000),
):
    processed = 0
    saved_or_updated = 0
    pages_requested = 0
    errors = []
    page = 0
    page_size = 100

    while processed < target:
        batch_size = min(page_size, target - processed)

        try:
            response = requests.get(
                BASE_URL,
                params={
                    "size": batch_size,
                    "page": page,
                },
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            pages_requested += 1

            if response.status_code in (429, 503):
                errors.append({
                    "page": page,
                    "error": (
                        f"Registry temporarily returned "
                        f"HTTP {response.status_code}."
                    ),
                })
                break

            response.raise_for_status()
            payload = response.json()

        except (requests.RequestException, ValueError) as error:
            errors.append({
                "page": page,
                "error": str(error),
            })
            break

        records = (
            payload.get("_embedded", {}).get("enheter", [])
        )

        if not records:
            break

        for record in records:
            if processed >= target:
                break

            try:
                number = validate_org_number(
                    record.get("organisasjonsnummer", "")
                )

                if str(record.get("organisasjonsnummer")) != number:
                    raise ValueError(
                        "Returned organization number mismatch."
                    )

                source_url = f"{BASE_URL}/{number}"

                company = normalize_company(
                    record,
                    source_url,
                )

                save_company(company)

                processed += 1
                saved_or_updated += 1

            except (ValueError, sqlite3.Error) as error:
                errors.append({
                    "organization_number": record.get(
                        "organisasjonsnummer"
                    ),
                    "error": str(error),
                })

        next_link = payload.get("_links", {}).get("next")

        if not next_link:
            break

        page += 1

        # Avoid making unnecessarily rapid requests.
        time.sleep(0.1)

    with get_db() as db:
        total_profiles = db.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]

    return {
        "requested_target": target,
        "records_processed": processed,
        "records_saved_or_updated": saved_or_updated,
        "total_unique_profiles_in_database": total_profiles,
        "collection_target_reached": processed >= target,
        "at_least_1000_profiles_stored": total_profiles >= 1000,
        "pages_requested": pages_requested,
        "errors_count": len(errors),
        "errors": errors[:10],
        "note": (
            "Only records returned by the official registry "
            "are processed. Results depend on registry "
            "availability and pagination."
        ),
    }


# --------------------------------------------------
# 10. COMPANY CHANGE HISTORY
# --------------------------------------------------

@app.get("/company/{org_number}/history")
def company_history(org_number: str):
    try:
        number = validate_org_number(org_number)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    with get_db() as db:
        rows = db.execute(
            """
            SELECT changed_at, old_data, new_data
            FROM company_history
            WHERE organization_number = ?
            ORDER BY id DESC
            """,
            (number,),
        ).fetchall()

    history = []

    for row in rows:
        try:
            old_data = json.loads(row["old_data"])
            new_data = json.loads(row["new_data"])
        except json.JSONDecodeError:
            continue

        for item in (old_data, new_data):
            try:
                item["business_address"] = json.loads(
                    item.get("business_address") or "{}"
                )
            except (json.JSONDecodeError, TypeError):
                pass

        history.append({
            "changed_at": row["changed_at"],
            "old_data": old_data,
            "new_data": new_data,
        })

    return {
        "organization_number": number,
        "changes_found": len(history),
        "history": history,
    }
