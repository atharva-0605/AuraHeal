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
    mode: str = Field(..., description="Healing execution pass mode.")

class HealStatusResponse(BaseModel):
    """
    API response schema indicating current job status.
    """
    job_id: str = Field(description="Unique identifier for the asynchronous job.")
    target_url: str = Field(description="The webpage target URL under analysis.")
    status: Literal["queued", "processing", "completed", "failed", "FAILED"] = Field(
        description="The execution state of the background healing pipeline."
    )
    current_iteration: int = Field(description="The number of visual repair iterations processed.")
    active_branch: Optional[str] = Field(None, description="The Git branch where changes were committed.")
    diff: Optional[str] = Field(None, description="Unified diff of the code mutations applied.")
    error: Optional[str] = Field(None, description="Detailed error message if the job failed.")

class CommitRequest(BaseModel):
    """
    API request schema for committing changes stateless.
    """
    job_id: Optional[str] = None
    target_url: str
    patch_diff: str
    repo_path: Optional[str] = "index.html"

class MergeRequest(BaseModel):
    """
    API request schema for merging pull requests stateless.
    """
    target_url: str
    pr_number: int

# =====================================================================
# 2. IN-MEMORY JOB DATABASE & APIROUTER INSTANTIATION
# =====================================================================

router = APIRouter()
jobs_db: Dict[str, Dict[str, Any]] = {}

# =====================================================================
# 3. BACKGROUND TASK HEALING PIPELINE
# =====================================================================

async def run_healing_pipeline(job_id: str, target_url: str, mode: str, issue_id: str):
    """
    Asynchronous worker task that drives the full visual ingestion,
    perception analysis, planning, and source-code mutation loop.
    """
    logger.info(f"Job {job_id}: Starting background healing process for URL '{target_url}' (mode: {mode})...")
    jobs_db[job_id]["status"] = "processing"

    try:
        # Step 1: Ingestion
        from app.services.ingestion import VisualIngestionService
        ingestion_service = VisualIngestionService()
        
        logger.info(f"Job {job_id}: Running visual ingestion...")
        ingestion_results = await ingestion_service.process_target_site(target_url, job_id=job_id)
        serialized_results = [res.model_dump() for res in ingestion_results]

        # Step 2: Agentic State initialization
        from app.agents.orchestrator import orchestrator_graph
        initial_state = {
            "job_id": job_id,
            "mode": mode,
            "target_url": target_url,
            "ingestion_results": serialized_results,
            "detected_anomalies": [],
            "current_iteration": 0,
            "maximum_iterations": 3 if mode == "deep" else 1,
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
            
            # Resolve dynamic domain and workspace path
            from urllib.parse import urlparse
            parsed_url = urlparse(target_url)
            domain = parsed_url.netloc or parsed_url.path
            clean_domain = re.sub(r'[^a-zA-Z0-9_\-]', '_', domain)

            if "localhost" in domain or "127.0.0.1" in domain:
                workspace_root = "C:/Users/DELL/Desktop/test"
            else:
                custom_path = f"C:/Users/DELL/Desktop/{clean_domain}"
                if os.path.exists(custom_path):
                    workspace_root = custom_path
                else:
                    workspace_root = "C:/Users/DELL/Desktop/test"

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
                workspace_root=workspace_root,
                issue_id=issue_id,
                target_url=target_url,
                anomalies=anomalies,
                mode=mode
            )
            active_branch = mutation_result.get("active_branch")
            logger.info(f"Job {job_id}: Mutation patches committed on branch {active_branch}")

        # Update Job Db
        db_updates = {
            "status": "completed",
            "current_iteration": final_state.get("current_iteration", 0),
        }
        if active_branch:
            db_updates.update({
                "active_branch": active_branch,
                "diff": mutation_result.get("diff"),
                "workspace_root": workspace_root,
                "pull_number": mutation_result.get("pull_number"),
                "pr_url": mutation_result.get("pr_url")
            })
        else:
            db_updates.update({
                "active_branch": None,
                "diff": final_state.get("mutation_manifest") or "",
                "workspace_root": "C:/Users/DELL/Desktop/test",
                "pull_number": None,
                "pr_url": None
            })
        jobs_db[job_id].update(db_updates)
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

    # Set branch layout fix target identifier
    issue_id = "layout-cols"

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
        request.mode,
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

@router.post("/heal/commit", status_code=200)
async def commit_and_push_job(request: CommitRequest):
    """
    Executes a direct cloud-safe commit and Pull Request generation over the GitHub REST API.
    """
    from app.services.mutation import execute_github_cloud_mutation
    try:
        res = await execute_github_cloud_mutation(
            target_url=request.target_url,
            patch_diff=request.patch_diff,
            repo_path=request.repo_path or "index.html"
        )
        
        # Store in active jobs database if job_id is provided
        if request.job_id and request.job_id in jobs_db:
            jobs_db[request.job_id]["pr_url"] = res.get("pr_url")
            jobs_db[request.job_id]["pull_number"] = res.get("pull_number")
            
        return {
            "status": "success",
            "detail": "Pull Request created successfully via GitHub REST API.",
            "pr_url": res.get("pr_url"),
            "pr_number": res.get("pull_number")
        }
    except ValueError as val_err:
        logger.error(f"GitHub cloud commit failed with ValueError: {val_err}", exc_info=True)
        return {
            "status": "FAILED",
            "error": str(val_err)
        }
    except Exception as e:
        logger.error(f"GitHub cloud commit failed with unhandled exception: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error: Commit operations failed.")

@router.post("/heal/merge", status_code=200)
async def merge_pull_request(request: MergeRequest):
    """
    Executes a direct cloud-safe merge of a Pull Request over the GitHub REST API.
    """
    from app.services.mutation import execute_github_merge
    try:
        res = await execute_github_merge(
            target_url=request.target_url,
            pr_number=request.pr_number
        )
        return {
            "status": "success",
            "detail": "Pull Request merged successfully via GitHub REST API.",
            "data": res
        }
    except ValueError as val_err:
        logger.error(f"GitHub cloud merge failed with ValueError: {val_err}", exc_info=True)
        return {
            "status": "FAILED",
            "error": str(val_err)
        }
    except Exception as e:
        logger.error(f"GitHub cloud merge failed with unhandled exception: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error: Merge operations failed.")
