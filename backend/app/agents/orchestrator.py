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

    use_openai = bool(settings.OPENAI_API_KEY)
    use_anthropic = bool(settings.ANTHROPIC_API_KEY)

    if not use_openai and not use_anthropic:
        raise ValueError(
            "Visual perception requires either OPENAI_API_KEY or ANTHROPIC_API_KEY "
            "to be configured in settings. Mock fallbacks are strictly rejected."
        )

    detected_anomalies: List[UIAnomaly] = []
    lowest_integrity_score = 100

    for result in ingestion_results:
        viewport = result.get("viewport", "Unknown")
        screenshot_path = result.get("screenshot_path", "")
        dom_elements = result.get("dom_elements", [])

        # Load and base64-encode screenshot binary
        base64_image = _encode_image(screenshot_path)
        dom_elements_json = json.dumps(dom_elements, indent=2)

        system_prompt = (
            "You are a Senior UI/UX QA Specialist and perception module. "
            "Analyze the layout coordinates (DOM elements list) and the actual screenshot "
            "to identify layout anomalies: 'overflow', 'misalignment', 'overlap', or 'distortion'. "
            "Output your findings exactly according to the structured JSON schema."
        )

        user_prompt = (
            f"Target URL: {state.get('target_url')}\n"
            f"Viewport: {viewport}\n\n"
            "Below is the DOM layout hierarchy containing element bounding boxes:\n"
            f"{dom_elements_json}\n\n"
            "Check both the coordinate boxes and the image screenshot for overlapping text, "
            "overflows, misplaced buttons, or distortions."
        )

        if use_openai:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            
            response = await client.beta.chat.completions.parse(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{base64_image}"}
                            }
                        ]
                    }
                ],
                response_format=PerceptionAnalysis,
                temperature=0.0
            )
            analysis = response.choices[0].message.parsed
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

    is_healed = len(detected_anomalies) == 0
    logger.info(f"Visual perception complete. Identified {len(detected_anomalies)} anomalies. is_healed={is_healed}")

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

    use_openai = bool(settings.OPENAI_API_KEY)
    use_anthropic = bool(settings.ANTHROPIC_API_KEY)

    if not use_openai and not use_anthropic:
        raise ValueError(
            "Visual planning requires either OPENAI_API_KEY or ANTHROPIC_API_KEY "
            "to be configured in settings. Mock fallbacks are strictly rejected."
        )

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

    system_prompt = (
        "You are an expert Frontend/CSS Architect. "
        "Review the layout anomalies list and output a structured CSS Mutation Manifest "
        "outlining exactly what CSS rules and selectors need to be written, "
        "and in which files, to solve the conflicts."
    )

    user_prompt = (
        "Develop a mutation plan based on the following anomalies:\n\n"
        f"{anomaly_details}\n"
        "Return a precise markdown document detailing the selector and style updates."
    )

    manifest_text = ""

    if use_openai:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0
        )
        manifest_text = response.choices[0].message.content or ""

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

    logger.info("CSS mutation manifest compiled successfully.")
    return {"mutation_manifest": manifest_text}

async def mutation_placeholder_node(state: AgentState) -> Dict[str, Any]:
    """
    Placeholder node representing where actual file/CSS modifications occur.
    Increments the loop counter.
    """
    logger.info("Executing mutation_placeholder_node (incrementing iterations)...")
    return {"current_iteration": state.get("current_iteration", 0) + 1}

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
    max_iter = state.get("maximum_iterations", 1)

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
