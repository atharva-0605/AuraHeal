import os
from pathlib import Path
from dotenv import load_dotenv

# Force find the .env file relative to this file's directory
backend_dir = Path(__file__).resolve().parents[1]
env_path = backend_dir / ".env"

if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)
else:
    print(f"CRITICAL ERROR: .env file not found at {env_path}")

import sys
import asyncio

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import logging
from fastapi import FastAPI
from app.core.config import settings
from app.services.ingestion import VisualIngestionService

# Configure logging to match settings.LOG_LEVEL
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("visual_ingestion_app")

from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.endpoints import router as api_v1_router

# FastAPI App Boilerplate
app = FastAPI(
    title="Visual Ingestion Pipeline API",
    version="1.0.0",
    description="Production-grade visual ingestion service using FastAPI, Pydantic V2, and Playwright."
)

# Configure CORS middleware to support local frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include v1 API router (declared below CORS middleware stack)
app.include_router(api_v1_router, prefix="/api/v1")

@app.on_event("startup")
async def startup_event():
    import sys
    import asyncio

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

@app.get("/")
def read_root():
    return {
        "status": "online",
        "app_env": settings.APP_ENV,
        "concurrency_limit": settings.MAX_CONCURRENCY_LIMIT
    }

async def main():
    logger.info("Initializing VisualIngestionService verification harness...")
    service = VisualIngestionService()
    
    # Target public site URL
    target_url = "https://example.com"
    logger.info(f"Target site configured for validation: {target_url}")
    
    try:
        # Log viewport configurations
        logger.info("Configured viewports for validation:")
        for vp in service._VIEWPORTS:
            logger.info(f" - {vp.name}: {vp.width}x{vp.height}")
            
        # Run visual ingestion service
        results = await service.process_target_site(target_url)
        
        # Log validation results
        logger.info("--- Validation Results ---")
        for res in results:
            logger.info(f"Viewport: {res.viewport}")
            logger.info(f"  Screenshot Path: {res.screenshot_path}")
            logger.info(f"  Total DOM elements extracted: {len(res.dom_elements)}")
            
    except Exception as e:
        logger.error(f"Validation harness encountered an error: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
