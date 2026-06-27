import os
import unittest
import tempfile
import shutil
import asyncio
from unittest.mock import patch, MagicMock
from app.services.mutation import (
    CSSPatch,
    MutationManifest,
    SourceMutationService,
    check_safe_path,
    execute_workspace_mutation
)

class TestMutation(unittest.TestCase):

    def setUp(self):
        # Create a temp directory for safe path testing
        self.test_dir = tempfile.mkdtemp()
        self.service = SourceMutationService()

    def tearDown(self):
        # Cleanup temp directory
        shutil.rmtree(self.test_dir)

    def test_safe_path_boundaries(self):
        """Verify check_safe_path correctly blocks directory traversal."""
        # Clean safe path
        safe_path = os.path.join(self.test_dir, "styles.css")
        resolved = check_safe_path("styles.css", self.test_dir)
        self.assertEqual(resolved, os.path.abspath(safe_path))

        # Nested safe path
        nested_path = os.path.join(self.test_dir, "css", "theme.css")
        resolved_nested = check_safe_path("css/theme.css", self.test_dir)
        self.assertEqual(resolved_nested, os.path.abspath(nested_path))

        # Traversal attempt (relative escaping)
        with self.assertRaises(ValueError) as ctx:
            check_safe_path("../escaped.css", self.test_dir)
        self.assertIn("Traversal attempt detected", str(ctx.exception))

        # Traversal attempt (absolute outside base)
        outside_abs = os.path.abspath(os.path.join(self.test_dir, "..", "outside.css"))
        with self.assertRaises(ValueError):
            check_safe_path(outside_abs, self.test_dir)

    def test_css_rules_merging(self):
        """Verify CSS selector merging and appending logic."""
        # Case 1: Append new selector
        initial_css = "body { margin: 0; }"
        updated = self.service._merge_css_rules(
            initial_css,
            "button",
            ["background: red;", "padding: 10px;"]
        )
        self.assertIn("button {", updated)
        self.assertIn("background: red;", updated)
        self.assertIn("padding: 10px;", updated)

        # Case 2: Merge rules into existing selector
        initial_css = "button {\n    background: blue;\n    margin: 5px;\n}"
        updated = self.service._merge_css_rules(
            initial_css,
            "button",
            ["background: red;", "padding: 10px;"]
        )
        # Check background was updated to red, padding was added, margin was kept
        self.assertIn("background: red;", updated)
        self.assertIn("padding: 10px;", updated)
        self.assertIn("margin: 5px;", updated)
        self.assertNotIn("background: blue;", updated)

    @patch("git.Repo")
    def test_git_branching_workflow(self, mock_repo_class):
        """Verify create_healing_branch instantiates Repo and calls correct checkout."""
        mock_repo = MagicMock()
        mock_repo_class.return_value = mock_repo
        mock_repo.heads = {}

        # Branch name to create
        issue_id = "test-issue"
        expected_branch = f"auraheal/fix-{issue_id}"

        # Run branching method
        branch_name = self.service.create_healing_branch(self.test_dir, issue_id)

        # Verify
        self.assertEqual(branch_name, expected_branch)
        mock_repo.create_head.assert_called_once_with(expected_branch)
        mock_repo.git.checkout.assert_called_once()

    @patch("git.Repo")
    def test_git_staging_and_committing(self, mock_repo_class):
        """Verify staging and committing flow checks repo dirty status."""
        mock_repo = MagicMock()
        mock_repo_class.return_value = mock_repo
        
        # Scenario A: Repo is dirty -> commit
        mock_repo.is_dirty.return_value = True
        committed = self.service.commit_and_stage_changes(self.test_dir, "fix styling")
        self.assertTrue(committed)
        mock_repo.git.add.assert_called_once_with(A=True)
        mock_repo.index.commit.assert_called_once_with("fix styling")

        # Reset mocks
        mock_repo.git.add.reset_mock()
        mock_repo.index.commit.reset_mock()

        # Scenario B: Repo is clean -> skip commit
        mock_repo.is_dirty.return_value = False
        committed = self.service.commit_and_stage_changes(self.test_dir, "fix styling")
        self.assertFalse(committed)
        mock_repo.index.commit.assert_not_called()

    @patch("app.services.mutation.execute_github_cloud_mutation")
    @patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"})
    def test_orchestrator_execution(self, mock_cloud_mut):
        """Verify orchestrator strings workflow components cleanly."""
        mock_cloud_mut.return_value = {
            "pr_url": "https://github.com/atharva-0605/test/pull/1",
            "pull_number": 1,
            "active_branch": "auraheal-responsive-patch"
        }

        manifest = MutationManifest(
            patches=[
                CSSPatch(
                    selector="header",
                    rules=["height: 60px;"],
                    targets=["styles.css"]
                )
            ]
        )

        async def run_orchestrator():
            return await execute_workspace_mutation(manifest, self.test_dir, "visual-patch")

        result = asyncio.run(run_orchestrator())

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["active_branch"], "auraheal-responsive-patch")
        self.assertEqual(result["updated_files_count"], 1)
        self.assertTrue(result["changes_committed"])

if __name__ == "__main__":
    unittest.main()
