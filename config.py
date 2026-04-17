import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

class Config:
    # ── Models ────────────────────────────────────────────────
    MODEL: str = os.getenv("MODEL", "gemini-2.5-flash")
    
    # -----------------------------
    # Gemini (API Key Mode)
    # -----------------------------
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY is required.")

    # -----------------------------
    # GCS Artifact Service
    # -----------------------------
    GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")
    GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    BUCKET_NAME = os.getenv("BUCKET_NAME")

    if not GOOGLE_APPLICATION_CREDENTIALS:
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS is required.")

    if not BUCKET_NAME:
        raise ValueError("BUCKET_NAME is required.")

    # Set credentials for GCS
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS

    # -----------------------------
    # BigQuery
    # -----------------------------
    BQ_PROJECT_ID = os.getenv("BQ_PROJECT_ID")
    BQ_DATASET = os.getenv("BQ_DATASET")
    SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")

    # -----------------------------
    # MongoDB Configuration
    # -----------------------------
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB: str = os.getenv("MONGO_DB", "procure2pay_db")

    MONGO_USER: str = os.getenv("MONGO_USER")
    MONGO_PASSWORD: str = os.getenv("MONGO_PASSWORD")

    # Construct authenticated URI if credentials provided
    if MONGO_USER and MONGO_PASSWORD:
        MONGO_URI = f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@localhost:27017"

    # -----------------------------
    # PostgreSQL Configuration
    # -----------------------------
    PG_USER: str = os.getenv("PG_USER", "procure2pay")
    PG_PASSWORD: str = os.getenv("PG_PASSWORD", "abcd1234")
    PG_HOST: str = os.getenv("PG_HOST", "localhost")
    PG_PORT: int = int(os.getenv("PG_PORT", 5432))
    PG_DB: str = os.getenv("PG_DB", "procure2pay_db")

    # ─────────────────────────────────────────────────────────────
    # 1. SESSION MANAGEMENT — SQLALCHEMY (PostgreSQL)
    # ─────────────────────────────────────────────────────────────
    DB_SCHEMA: str = os.getenv("DB_SCHEMA", "public")
    SQLALCHEMY_DATABASE_URI: str = (
        f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"
    )

    # ─────────────────────────────────────────────────────────────
    # 2. HITL (Human-In-The-Loop) — SQLITE
    # ─────────────────────────────────────────────────────────────
    HITL_DB_URL: str = "sqlite:///hitl.db"

# Create config instance
config = Config()

