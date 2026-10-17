
# SignalpostAI — Norwegian Company Research Agent

## Overview
SignalpostAI collects and tracks company information from Norway's official Brønnøysund Register Centre public API.

## Features
- Look up Norwegian companies by organisation number
- Collect and store company profiles in SQLite
- Validate company identity using the organisation number
- Track changes between successive data fetches
- Access company information through a FastAPI REST API

## Technology Stack
- Python
- FastAPI
- Requests
- SQLite
- Brønnøysund Register Centre API

## Data Source
Official Norwegian company registry:
https://data.brreg.no/enhetsregisteret/api/

## Run Locally
Activate your Python virtual environment and install dependencies:

```bash
pip install requests fastapi uvicorn sqlalchemy pydantic python-dotenv
```

Start the API:

```bash
uvicorn main:app --reload
```

Open the interactive API documentation:
http://127.0.0.1:8000/docs

Health check:
http://127.0.0.1:8000/health

## AI Model
This version uses deterministic API integration and data comparison. It does not currently call an external LLM.

## Cost
No paid LLM API is required by the current implementation. Hosting or other infrastructure may incur costs.

## Limitations
- Company change history depends on repeated successful fetches.
- Data accuracy depends on the official source and the fields returned by its API.
- Bulk collection is subject to API availability and rate limits.
