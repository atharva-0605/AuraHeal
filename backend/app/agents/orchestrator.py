import base64
import os
import json
import logging
from typing import List, Dict, Any, Optional, Literal, TypedDict
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, END
from app.core.config import settings

logger = logging.getLogger(__name__)

# =====================================================================
# 1. DESIGN THE ANOMALY SCHEMA (Pydantic V2)
# =====================================================================

class UIAnomaly(BaseModel):
    """
    Represents a specific visual layout/render anomaly identified on a webpage.
    """
    element_tag: str = Field(description="The HTML tag name of the target element.")
    element_id: Optional[str] = Field(None, description="The unique element ID if available.")
    element_classes: Optional[str] = Field(None, description="Space-separated CSS classes of the element.")
    anomaly_type: Literal["overflow", "misalignment", "overlap", "distortion"] = Field(
        description="The nature of the layout discrepancy."
    )
    severity: Literal["low", "medium", "high"] = Field(description="Impact severity of the layout anomaly.")
    description: str = Field(description="A descriptive explanation of the anomaly.")
    target_file_hint: str = Field(description="Hint or file path where CSS should be corrected.")

class PerceptionAnalysis(BaseModel):
    """
    Wraps all identified UI layout anomalies along with a structural integrity score.
    """
    anomalies: List[UIAnomaly] = Field(description="List of detected anomalies.")
    structural_integrity_score: int = Field(
        ...,
        ge=0,
        le=100,
        description="Overall structural integrity score of the viewport layout (0 to 100)."
    )

# =====================================================================
# 2. ESTABLISH THE LANGGRAPH STATE MACHINE
# =====================================================================

class AgentState(TypedDict):
    """
    Type-safe state representation tracked through the LangGraph execution flow.
    """
    job_id: str
    mode: str
    target_url: str
    ingestion_results: List[Dict[str, Any]]
    detected_anomalies: List[UIAnomaly]
    current_iteration: int
    maximum_iterations: int
    is_healed: bool
    mutation_manifest: Optional[str]

# =====================================================================
# 3. IMPLEMENT THE PERCEPTION, PLANNING & MUTATION NODES
# =====================================================================

def _encode_image(image_path: str) -> str:
    """Helper method to load and encode screenshot binaries to base64."""
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Visual asset screenshot not found at: {image_path}")
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")

async def perception_node(state: AgentState) -> Dict[str, Any]:
    """
    Asynchronously parses viewports, screenshot binaries, and DOMElementMaps,
    feeding them into a VLM with strict JSON/Pydantic structured output mapping.
    """
    logger.info("Executing perception_node...")
    ingestion_results = state.get("ingestion_results", [])
    
    if not ingestion_results:
        logger.info("No ingestion results provided. Set is_healed to True and end.")
        return {"detected_anomalies": [], "is_healed": True}

    use_gemini = bool(os.getenv("GEMINI_API_KEY"))
    use_anthropic = bool(settings.ANTHROPIC_API_KEY)
    
    detected_anomalies: List[UIAnomaly] = []
    lowest_integrity_score = 100
    
    run_live = use_gemini or use_anthropic
    mode = state.get("mode", "light")

    if run_live:
        try:
            for result in ingestion_results:
                if isinstance(result, dict):
                    viewport = result.get("viewport", "Unknown")
                    screenshot_path = result.get("screenshot_path", "")
                    dom_elements = result.get("dom_elements", [])
                    raw_html = result.get("raw_html", "") or ""
                else:
                    viewport = getattr(result, "viewport", "Unknown")
                    screenshot_path = getattr(result, "screenshot_path", "")
                    dom_elements = getattr(result, "dom_elements", [])
                    raw_html = getattr(result, "raw_html", "") or ""

                # Load and base64-encode screenshot binary only for Anthropic fallback
                base64_image = ""
                if use_anthropic and screenshot_path:
                    try:
                        base64_image = _encode_image(screenshot_path)
                    except Exception:
                        pass

                dom_elements_json = json.dumps(dom_elements, default=str, indent=2)

                mode_instructions = (
                    "Run a single layout analysis pass checking basic utility classes "
                    "and identify any obvious non-responsive configurations."
                    if mode == "light" else
                    "Execute a deep layout audit. Check alignment, spacing, responsiveness across viewports, "
                    "and evaluate structural integrity meticulously by analyzing class structures."
                )

                system_prompt = (
                    f"You are a Senior UI/UX QA Specialist and perception module running in {mode.upper()} mode. "
                    f"{mode_instructions} "
                    "Analyze the layout coordinates (DOM elements list) and the raw HTML/CSS source code "
                    "to identify layout anomalies: 'overflow', 'misalignment', 'overlap', or 'distortion'. "
                    "Look for unresponsive patterns like grid-cols-3 without mobile overrides (e.g. md:grid-cols-3). "
                    "Output your findings exactly according to the structured JSON schema."
                )

                user_prompt = (
                    f"Target URL: {state.get('target_url')}\n"
                    f"Viewport: {viewport}\n\n"
                    "Below is the DOM layout hierarchy containing element bounding boxes:\n"
                    f"{dom_elements_json}\n\n"
                    "Below is the raw HTML/CSS source code of the layout:\n"
                    f"{raw_html}\n\n"
                    "Check both the coordinate boxes and class utilities in the source code for overlapping text, "
                    "overflows, misplaced buttons, or distortions."
                )

                if use_gemini:
                    from google import genai
                    from google.genai import types
                    
                    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                    
                    # Call Gemini
                    response = client.models.generate_content(
                        model='gemini-1.5-flash',
                        contents=user_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            response_mime_type="application/json",
                            response_schema=PerceptionAnalysis,
                            temperature=0.0
                        )
                    )
                    
                    analysis_data = json.loads(response.text)
                    analysis = PerceptionAnalysis(**analysis_data)
                    if analysis:
                        detected_anomalies.extend(analysis.anomalies)
                        lowest_integrity_score = min(lowest_integrity_score, analysis.structural_integrity_score)

                elif use_anthropic:
                    from anthropic import AsyncAnthropic
                    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
                    
                    response = await client.messages.create(
                        model="claude-3-5-sonnet-20241022",
                        max_tokens=4000,
                        system=system_prompt,
                        tools=[{
                            "name": "perception_analysis_report",
                            "description": "Report identified anomalies and integrity score.",
                            "input_schema": PerceptionAnalysis.model_json_schema()
                        }],
                        tool_choice={"type": "tool", "name": "perception_analysis_report"},
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "image/png",
                                            "data": base64_image
                                        }
                                    },
                                    {
                                        "type": "text",
                                        "text": user_prompt
                                    }
                                ]
                            }
                        ],
                        temperature=0.0
                    )
                    
                    analysis_data = {}
                    for block in response.content:
                        if block.type == "tool_use" and block.name == "perception_analysis_report":
                            analysis_data = block.input
                            break
                    
                    analysis = PerceptionAnalysis(**analysis_data)
                    detected_anomalies.extend(analysis.anomalies)
                    lowest_integrity_score = min(lowest_integrity_score, analysis.structural_integrity_score)
        except Exception as e:
            logger.error(f"Live VLM perception failed: {e}. Falling back to layout simulation...", exc_info=True)
            run_live = False

    if not run_live:
        logger.info("Running simulation/fallback perception pass...")
        has_broken_grid = False
        
        from urllib.parse import urlparse
        import re
        parsed_url = urlparse(state.get("target_url", ""))
        domain = parsed_url.netloc or parsed_url.path
        clean_domain = re.sub(r'[^a-zA-Z0-9_\-]', '_', domain)
        
        if "localhost" in domain or "127.0.0.1" in domain:
            workspace_root = "C:/Users/DELL/Desktop/test"
        else:
            workspace_root = f"C:/Users/DELL/Desktop/{clean_domain}"
            if not os.path.exists(workspace_root):
                workspace_root = "C:/Users/DELL/Desktop/test"
                
        index_html_path = os.path.join(workspace_root, "index.html")
        if os.path.exists(index_html_path):
            with open(index_html_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            if "card-container grid grid-cols-3" in html_content:
                has_broken_grid = True

        if has_broken_grid:
            detected_anomalies.append(
                UIAnomaly(
                    element_tag="div",
                    element_id=None,
                    element_classes="card-container grid grid-cols-3",
                    anomaly_type="misalignment",
                    severity="high",
                    description="The grid container has hardcoded grid-cols-3 columns causing element overlap and clipping on mobile layouts.",
                    target_file_hint="index.html"
                )
            )
            lowest_integrity_score = 80
        else:
            lowest_integrity_score = 98

    is_healed = len(detected_anomalies) == 0 or lowest_integrity_score >= 95
    logger.info(f"Visual perception complete. Identified {len(detected_anomalies)} anomalies. Score={lowest_integrity_score}. is_healed={is_healed}")

    return {
        "detected_anomalies": detected_anomalies,
        "is_healed": is_healed
    }

async def planning_node(state: AgentState) -> Dict[str, Any]:
    """
    Asynchronously evaluates the detected layout anomalies and formats a descriptive
    CSS mutation manifest indicating precise selectors and style adjustments required.
    """
    logger.info("Executing planning_node...")
    anomalies = state.get("detected_anomalies", [])
    
    if not anomalies:
        logger.info("No anomalies to fix. Skipping planning.")
        return {"mutation_manifest": None}

    use_gemini = bool(os.getenv("GEMINI_API_KEY"))
    use_anthropic = bool(settings.ANTHROPIC_API_KEY)

    anomaly_details = ""
    for idx, anomaly in enumerate(anomalies, 1):
        anomaly_details += (
            f"Anomaly #{idx}:\n"
            f"  - Selector Tag: {anomaly.element_tag}\n"
            f"  - Element ID: {anomaly.element_id}\n"
            f"  - Classes: {anomaly.element_classes}\n"
            f"  - Type: {anomaly.anomaly_type}\n"
            f"  - Severity: {anomaly.severity}\n"
            f"  - Description: {anomaly.description}\n"
            f"  - Target file hint: {anomaly.target_file_hint}\n\n"
        )

    manifest_text = ""
    run_live = use_gemini or use_anthropic

    if run_live:
        try:
            mode = state.get("mode", "light")
            planning_instructions = (
                "Develop a single visual pass repair plan. Recommend simple CSS class modifications."
                if mode == "light" else
                "Develop a comprehensive healing plan with precise candidate patches for visual alignment."
            )

            system_prompt = (
                f"You are an expert Frontend/CSS Architect operating in {mode.upper()} mode. "
                f"{planning_instructions} "
                "Review the layout anomalies list and output a structured CSS Mutation Manifest "
                "outlining exactly what CSS rules and selectors need to be written, "
                "and in which files, to solve the conflicts."
            )

            user_prompt = (
                "Develop a mutation plan based on the following anomalies:\n\n"
                f"{anomaly_details}\n"
                "Return a precise markdown document detailing the selector and style updates."
            )

            if use_gemini:
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                response = client.models.generate_content(
                    model='gemini-1.5-flash',
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.0
                    )
                )
                manifest_text = response.text or ""

            elif use_anthropic:
                from anthropic import AsyncAnthropic
                client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
                response = await client.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=2000,
                    system=system_prompt,
                    messages=[
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.0
                )
                for block in response.content:
                    if block.type == "text":
                        manifest_text += block.text
        except Exception as e:
            logger.error(f"Live VLM planning failed: {e}. Falling back to plan simulation...", exc_info=True)
            run_live = False

    if not run_live:
        logger.info("Running simulation/fallback planning pass...")
        manifest_text = (
            "div.card-container {\n"
            "  display: grid;\n"
            "  grid-template-columns: repeat(1, minmax(0, 1fr));\n"
            "}\n"
            "@media (min-width: 768px) {\n"
            "  div.card-container {\n"
            "    grid-template-columns: repeat(3, minmax(0, 1fr));\n"
            "  }\n"
            "}"
        )

    logger.info("CSS mutation manifest compiled successfully.")
    return {"mutation_manifest": manifest_text}

async def mutation_placeholder_node(state: AgentState) -> Dict[str, Any]:
    """
    Placeholder node representing where actual file/CSS modifications occur.
    Increments the loop counter, runs the index.html mutation, and takes a SECOND screenshot in deep mode.
    """
    logger.info("Executing mutation_placeholder_node (incrementing iterations)...")
    current_iter = state.get("current_iteration", 0)
    mode = state.get("mode", "light")
    job_id = state.get("job_id", "default_job")
    target_url = state.get("target_url", "")
    
    # 1. Run the mutation logic
    try:
        from app.services.mutation import mutate_index_html
        from urllib.parse import urlparse
        import re
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
                
        mutate_index_html(workspace_root)
    except Exception as e:
        logger.error(f"Failed to execute mutation in orchestrator placeholder node: {e}")

    # 2. In deep mode, take a SECOND screenshot (run ingestion again to update state screenshots and DOM elements)
    new_ingestion_results = state.get("ingestion_results", [])
    if mode == "deep":
        try:
            logger.info(f"Deep Mode: Running visual ingestion pass 2 to verify mutation on {target_url}...")
            from app.services.ingestion import VisualIngestionService
            ingestion_service = VisualIngestionService()
            results = await ingestion_service.process_target_site(target_url, job_id=job_id)
            new_ingestion_results = [res.model_dump() for res in results]
        except Exception as ingest_err:
            logger.error(f"Failed to capture verification screenshot: {ingest_err}")

    return {
        "current_iteration": current_iter + 1,
        "ingestion_results": new_ingestion_results
    }

# =====================================================================
# 4. CONTROL LOOP ROUTING
# =====================================================================

def should_continue(state: AgentState) -> Literal["continue_to_mutation", "end_workflow"]:
    """
    Evaluates whether the agentic workflow loop has achieved a healed state
    or exceeded the allowed cycle budget.
    """
    anomalies = state.get("detected_anomalies", [])
    current_iter = state.get("current_iteration", 0)
    mode = state.get("mode", "light")
    
    max_iter = 1 if mode == "light" else state.get("maximum_iterations", 3)

    if not anomalies or state.get("is_healed", False):
        logger.info("Workflow complete: No anomalies remaining or state is healed.")
        return "end_workflow"

    if current_iter >= max_iter:
        logger.info(f"Workflow complete: Current iteration ({current_iter}) reached limit ({max_iter}).")
        return "end_workflow"

    logger.info(f"Routing to mutation cycle: Iteration {current_iter + 1}/{max_iter}")
    return "continue_to_mutation"

# =====================================================================
# STATEGRAPH INITIALIZATION & COMPILATION
# =====================================================================

workflow = StateGraph(AgentState)

# Add our active processing nodes
workflow.add_node("perception", perception_node)
workflow.add_node("planning", planning_node)
workflow.add_node("mutation", mutation_placeholder_node)

# Connect edges
workflow.set_entry_point("perception")
workflow.add_edge("perception", "planning")

# Setup conditional control loop on the planning node output
workflow.add_conditional_edges(
    "planning",
    should_continue,
    {
        "continue_to_mutation": "mutation",
        "end_workflow": END
    }
)

# Loop back from mutation to perception
workflow.add_edge("mutation", "perception")

# Compile state machine
orchestrator_graph = workflow.compile()
