import unittest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from app.agents.orchestrator import (
    orchestrator_graph,
    UIAnomaly,
    PerceptionAnalysis,
    should_continue,
    AgentState
)

class TestOrchestrator(unittest.TestCase):
    
    def test_graph_compilation(self):
        """Verify that the LangGraph StateGraph compiles without errors."""
        self.assertIsNotNone(orchestrator_graph)
        # Check nodes are defined
        self.assertIn("perception", orchestrator_graph.nodes)
        self.assertIn("planning", orchestrator_graph.nodes)
        self.assertIn("mutation", orchestrator_graph.nodes)

    def test_should_continue_router(self):
        """Verify the routing decisions based on AgentState."""
        # Scenario 1: No anomalies -> end
        state_no_anomalies: AgentState = {
            "job_id": "test-job",
            "mode": "light",
            "target_url": "http://example.com",
            "ingestion_results": [],
            "detected_anomalies": [],
            "current_iteration": 0,
            "maximum_iterations": 3,
            "is_healed": False,
            "mutation_manifest": None
        }
        self.assertEqual(should_continue(state_no_anomalies), "end_workflow")

        # Scenario 2: Anomalies present, under max iteration limit -> continue
        state_with_anomalies: AgentState = {
            "job_id": "test-job",
            "mode": "light",
            "target_url": "http://example.com",
            "ingestion_results": [],
            "detected_anomalies": [
                UIAnomaly(
                    element_tag="button",
                    anomaly_type="overlap",
                    severity="high",
                    description="button overlaps text",
                    target_file_hint="styles.css"
                )
            ],
            "current_iteration": 0,
            "maximum_iterations": 3,
            "is_healed": False,
            "mutation_manifest": None
        }
        self.assertEqual(should_continue(state_with_anomalies), "continue_to_mutation")

        # Scenario 3: Anomalies present, reached max iteration limit -> end
        state_max_iter: AgentState = {
            "job_id": "test-job",
            "mode": "light",
            "target_url": "http://example.com",
            "ingestion_results": [],
            "detected_anomalies": [
                UIAnomaly(
                    element_tag="button",
                    anomaly_type="overlap",
                    severity="high",
                    description="button overlaps text",
                    target_file_hint="styles.css"
                )
            ],
            "current_iteration": 3,
            "maximum_iterations": 3,
            "is_healed": False,
            "mutation_manifest": None
        }
        self.assertEqual(should_continue(state_max_iter), "end_workflow")

    @patch("google.genai.Client")
    @patch("app.agents.orchestrator.settings")
    @patch("app.agents.orchestrator._encode_image")
    def test_mock_graph_execution(self, mock_encode, mock_settings, mock_genai_client):
        """Verify a full execution step through the graph with mocked VLM client."""
        import json
        # Configure mocked settings
        mock_settings.ANTHROPIC_API_KEY = None
        mock_encode.return_value = "base64encodedimage"
        
        # Setup mock client instance
        mock_client = MagicMock()
        mock_genai_client.return_value = mock_client
        
        # Configure mocked GenAI responses
        mock_perception_response = MagicMock()
        mock_perception_response.text = json.dumps({
            "anomalies": [
                {
                    "element_tag": "header",
                    "element_id": "main-header",
                    "element_classes": "header nav",
                    "anomaly_type": "misalignment",
                    "severity": "medium",
                    "description": "Header navigation elements are out of vertical alignment",
                    "target_file_hint": "header.css"
                }
            ],
            "structural_integrity_score": 85
        })
        
        mock_planning_response = MagicMock()
        mock_planning_response.text = "Update header.css: header { align-items: center; }"
        
        mock_models = MagicMock()
        mock_client.models = mock_models
        mock_models.generate_content = MagicMock(side_effect=[mock_perception_response, mock_planning_response, mock_perception_response, mock_planning_response])

        # Setup initial state
        initial_state: AgentState = {
            "job_id": "test-job",
            "mode": "light",
            "target_url": "http://example.com",
            "ingestion_results": [{
                "viewport": "Desktop",
                "screenshot_path": "storage/screenshots/Desktop_snapshot.png",
                "dom_elements": []
            }],
            "detected_anomalies": [],
            "current_iteration": 0,
            "maximum_iterations": 1,
            "is_healed": False,
            "mutation_manifest": None
        }

        # Run orchestrator graph
        async def run_test():
            with patch.dict("os.environ", {"GEMINI_API_KEY": "test_key"}):
                result = await orchestrator_graph.ainvoke(initial_state)
                return result

        result = asyncio.run(run_test())

        # Verify that anomalies were detected, manifest compiled, and workflow terminated correctly
        self.assertEqual(len(result["detected_anomalies"]), 1)
        self.assertEqual(result["detected_anomalies"][0].element_tag, "header")
        self.assertEqual(result["detected_anomalies"][0].anomaly_type, "misalignment")
        self.assertIn("Update header.css", result["mutation_manifest"])
        self.assertEqual(result["current_iteration"], 1)

if __name__ == "__main__":
    unittest.main()
