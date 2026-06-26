import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.api.v1.endpoints import jobs_db

class TestAPI(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)
        # Clear jobs database before each test
        jobs_db.clear()

    def test_read_root(self):
        """Verify the root healthcheck endpoint returns successfully."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "online")

    @patch("app.api.v1.endpoints.BackgroundTasks.add_task")
    def test_heal_analyze_endpoint(self, mock_add_task):
        """Verify POST /api/v1/heal/analyze queues job and returns status 202."""
        payload = {
            "url": "https://example.com",
            "repository": "GitHub: auraheal-demo-repo",
            "branch": "main",
            "mode": "light"
        }
        response = self.client.post("/api/v1/heal/analyze", json=payload)
        
        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertIn("job_id", data)
        self.assertEqual(data["target_url"], "https://example.com")
        self.assertEqual(data["status"], "queued")

        # Verify that background task was indeed queued
        mock_add_task.assert_called_once()
        job_id = data["job_id"]
        self.assertIn(job_id, jobs_db)
        self.assertEqual(jobs_db[job_id]["status"], "queued")

    def test_heal_status_not_found(self):
        """Verify GET /api/v1/heal/status/{job_id} returns 404 for missing jobs."""
        response = self.client.get("/api/v1/heal/status/non-existent-id")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Job ID not found.")

    def test_heal_status_found(self):
        """Verify GET /api/v1/heal/status/{job_id} returns status for existing jobs."""
        job_id = "test-job-id"
        jobs_db[job_id] = {
            "job_id": job_id,
            "target_url": "https://example.com",
            "status": "processing",
            "current_iteration": 1,
            "active_branch": "auraheal/fix-test"
        }
        response = self.client.get(f"/api/v1/heal/status/{job_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["job_id"], job_id)
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["current_iteration"], 1)
        self.assertEqual(data["active_branch"], "auraheal/fix-test")

if __name__ == "__main__":
    unittest.main()
