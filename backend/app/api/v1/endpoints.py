import uuid
import re
import os
import logging
from typing import Literal, Optional, Dict, Any
from pydantic import BaseModel, Field
from fastapi import APIRouter, BackgroundTasks, HTTPException

logger = logging.getLogger(__name__)

# =====================================================================
# 1. DESIGN THE API SCHEMAS
# =====================================================================

class HealRequest(BaseModel):
    """
    API request schema for trigger visual healing.
    """
    url: str = Field(..., description="The URL of the webpage to capture and analyze.")
    repository: Optional[str] = Field(None, description="Repository connection identifier.")
    branch: Optional[str] = Field(None, description="Target repository branch.")
    mode: Optional[str] = Field(None, description="Healing execution pass mode.")

class HealStatusResponse(BaseModel):
    """
    API response schema indicating current job status.
    """
    job_id: str = Field(description="Unique identifier for the asynchronous job.")
    target_url: str = Field(description="The webpage target URL under analysis.")
    status: Literal["queued", "processing", "completed", "failed"] = Field(
        description="The execution state of the background healing pipeline."
    )
    current_iteration: int = Field(description="The number of visual repair iterations processed.")
    active_branch: Optional[str] = Field(None, description="The Git branch where changes were committed.")

# =====================================================================
# 2. IN-MEMORY JOB DATABASE & APIROUTER INSTANTIATION
# =====================================================================

router = APIRouter()
jobs_db: Dict[str, Dict[str, Any]] = {}

# =====================================================================
# 3. BACKGROUND TASK HEALING PIPELINE
# =====================================================================

async def run_healing_pipeline(job_id: str, target_url: str, issue_id: str):
    """
    Asynchronous worker task that drives the full visual ingestion,
    perception analysis, planning, and source-code mutation loop.
    """
    logger.info(f"Job {job_id}: Starting background healing process for URL '{target_url}'...")
    jobs_db[job_id]["status"] = "processing"

    try:
        # Step 1: Ingestion
        from app.services.ingestion import VisualIngestionService
        ingestion_service = VisualIngestionService()
        
        logger.info(f"Job {job_id}: Running visual ingestion...")
        ingestion_results = await ingestion_service.process_target_site(target_url)
        serialized_results = [res.model_dump() for res in ingestion_results]

        # Step 2: Agentic State initialization
        from app.agents.orchestrator import orchestrator_graph
        initial_state = {
            "target_url": target_url,
            "ingestion_results": serialized_results,
            "detected_anomalies": [],
            "current_iteration": 0,
            "maximum_iterations": 3,
            "is_healed": False,
            "mutation_manifest": None
        }

        # Step 3: Run Perception & Planning Orchestrator
        logger.info(f"Job {job_id}: Invoking LangGraph perception state machine...")
        final_state = await orchestrator_graph.ainvoke(initial_state)

        # Step 4: Source-code Mutation
        active_branch = None
        anomalies = final_state.get("detected_anomalies", [])
        manifest_text = final_state.get("mutation_manifest")

        if anomalies and manifest_text:
            logger.info(f"Job {job_id}: Layout anomalies detected. Compiling mutation patches...")
            from app.services.mutation import MutationManifest, CSSPatch, execute_workspace_mutation
            
            # Resolve backend root path
            current_dir = os.path.dirname(os.path.abspath(__file__))
            backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))

            patches = []
            for anomaly in anomalies:
                # Compile selector
                selector = ""
                if anomaly.element_id:
                    selector = f"#{anomaly.element_id}"
                elif anomaly.element_classes:
                    selector = f".{anomaly.element_classes.split()[0]}"
                else:
                    selector = anomaly.element_tag

                # Target file path (relative to workspace)
                target_file = anomaly.target_file_hint or "storage/screenshots/styles.css"

                # Parse rules from LLM manifest or build fallback rules
                rule_matches = re.findall(r"([^{}\n]+)\s*\{\s*([^}]+)\s*\}", manifest_text)
                rules = []
                if rule_matches:
                    for sel, body in rule_matches:
                        if sel.strip().lower() == selector.lower():
                            rules = [r.strip() for r in body.split(";") if r.strip()]
                            break

                # Fallback to standard block rules if none parsed
                if not rules:
                    rules = ["display: block;"]

                patches.append(CSSPatch(selector=selector, rules=rules, targets=[target_file]))

            mutation_manifest = MutationManifest(patches=patches)
            
            # Apply changes & commit
            mutation_result = await execute_workspace_mutation(
                manifest=mutation_manifest,
                workspace_root=backend_dir,
                issue_id=issue_id
            )
            active_branch = mutation_result.get("active_branch")
            logger.info(f"Job {job_id}: Mutation patches committed on branch {active_branch}")

        # Update Job Db
        jobs_db[job_id].update({
            "status": "completed",
            "current_iteration": final_state.get("current_iteration", 0),
            "active_branch": active_branch
        })
        logger.info(f"Job {job_id} successfully completed.")

    except Exception as e:
        logger.error(f"Job {job_id} encountered an unhandled error: {e}", exc_info=True)
        jobs_db[job_id]["status"] = "failed"

# =====================================================================
# 4. API ENDPOINTS
# =====================================================================

@router.post("/heal/analyze", response_model=HealStatusResponse, status_code=202)
async def analyze_and_heal(request: HealRequest, background_tasks: BackgroundTasks):
    """
    Submits a target URL to the healing pipeline and starts the background task runner.
    """
    job_id = str(uuid.uuid4())
    logger.info(f"Received heal request for target: {request.url}. Generated Job ID: {job_id}")

    # Use branch as standard issue suffix, fallback to uuid if empty
    issue_suffix = request.branch.strip() if request.branch else uuid.uuid4().hex[:8]
    # Clean branch prefix for git branch name
    issue_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', issue_suffix)

    # Set initial state in DB
    jobs_db[job_id] = {
        "job_id": job_id,
        "target_url": request.url,
        "status": "queued",
        "current_iteration": 0,
        "active_branch": None
    }

    # Queue background task execution
    background_tasks.add_task(
        run_healing_pipeline,
        job_id,
        request.url,
        issue_id
    )

    return jobs_db[job_id]

@router.get("/heal/status/{job_id}", response_model=HealStatusResponse)
async def get_healing_status(job_id: str):
    """
    Retrieves the execution status of a submitted visual healing job.
    """
    if job_id not in jobs_db:
        logger.warning(f"Status query for non-existent Job ID: {job_id}")
        raise HTTPException(status_code=404, detail="Job ID not found.")
    
    return jobs_db[job_id]
