import os
import re
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
import anyio

logger = logging.getLogger(__name__)

# =====================================================================
# 1. DESIGN THE MUTATION SCHEMAS
# =====================================================================

class CSSPatch(BaseModel):
    """
    Defines a CSS patch target containing a selector, style rules,
    and a list of targets (relative stylesheet paths).
    """
    selector: str = Field(description="The CSS selector target (e.g. '.container' or 'button').")
    rules: List[str] = Field(description="List of CSS style rules (e.g. ['display: flex;', 'margin-top: 10px;']).")
    targets: List[str] = Field(description="Relative file system paths of the stylesheets to patch.")

class MutationManifest(BaseModel):
    """
    Represents the manifest compiled by the planning node containing multiple patches.
    """
    patches: List[CSSPatch] = Field(description="List of CSS patch specifications.")

# =====================================================================
# 2. DIRECTORY TRAVERSAL PROTECTION
# =====================================================================

def check_safe_path(target_path: str, base_path: str) -> str:
    """
    Verifies that the target path does not escape the base project directory structure.
    Raises ValueError if directory traversal is attempted.
    """
    base_abs = os.path.abspath(base_path)
    
    if os.path.isabs(target_path):
        target_abs = os.path.abspath(target_path)
    else:
        target_abs = os.path.abspath(os.path.join(base_abs, target_path))

    # Prevent escaping base_abs path boundaries
    if os.path.commonpath([base_abs, target_abs]) != base_abs:
        raise ValueError(
            f"Security Error: Traversal attempt detected. Target path '{target_path}' "
            f"resolves outside of allowed project root '{base_path}'."
        )
    return target_abs

# =====================================================================
# 3. SOURCE MUTATION SERVICE (FILE I/O & GITOPS WORKFLOWS)
# =====================================================================

class SourceMutationService:
    """
    Handles Phase 3 (Mutation) by updating files asynchronously and managing Git branches.
    """

    async def apply_css_patch(self, patch: CSSPatch, base_project_path: str) -> bool:
        """
        Asynchronously applies a CSSPatch to target stylesheets.
        Ensures directory safety, reads files using anyio, and writes clean modifications.
        """
        for target in patch.targets:
            safe_target_path = check_safe_path(target, base_project_path)
            
            # Read current content or start empty if the file does not exist
            if os.path.exists(safe_target_path):
                async with await anyio.open_file(safe_target_path, "r", encoding="utf-8") as f:
                    content = await f.read()
            else:
                # Ensure parents are created
                os.makedirs(os.path.dirname(safe_target_path), exist_ok=True)
                content = ""

            # Update CSS content using structural merging engine
            updated_content = self._merge_css_rules(content, patch.selector, patch.rules)

            # Write the result back to target path
            async with await anyio.open_file(safe_target_path, "w", encoding="utf-8") as f:
                await f.write(updated_content)
                logger.info(f"Applied CSS patch rules on selector '{patch.selector}' in {target}")

        return True

    def _merge_css_rules(self, content: str, selector: str, rules: List[str]) -> str:
        """
        Locates the CSS selector block, merges properties cleanly, or appends a new block.
        """
        # Clean formatting
        cleaned_selector = selector.strip()
        formatted_new_rules = [r.strip().rstrip(';') for r in rules if r.strip()]

        escaped_selector = re.escape(cleaned_selector)
        # Regex matching: selector { rules_body }
        pattern = re.compile(rf"({escaped_selector}\s*\{{\s*)([^}}]*?)(\s*\}})", re.DOTALL)
        match = pattern.search(content)

        if match:
            # Block already exists: parse and merge rules to avoid duplicates/corruption
            prefix = match.group(1)
            existing_body = match.group(2)
            suffix = match.group(3)

            properties = {}
            for line in existing_body.split(";"):
                line = line.strip()
                if ":" in line:
                    prop, val = line.split(":", 1)
                    properties[prop.strip().lower()] = val.strip()

            for rule in formatted_new_rules:
                if ":" in rule:
                    prop, val = rule.split(":", 1)
                    properties[prop.strip().lower()] = val.strip()

            # Format merged block body
            merged_body_lines = [f"    {prop}: {val};" for prop, val in properties.items()]
            merged_body = "\n" + "\n".join(merged_body_lines) + "\n"

            replacement = f"{prefix}{merged_body}{suffix}"
            return content[:match.start()] + replacement + content[match.end():]
        else:
            # Block does not exist: append to bottom
            rules_str = "\n".join(f"    {r};" for r in formatted_new_rules)
            new_block = f"\n\n{cleaned_selector} {{\n{rules_str}\n}}"
            return content.rstrip() + new_block + "\n"

    def create_healing_branch(self, repo_path: str, issue_id: str) -> str:
        """
        Creates and checkouts a new isolated git branch named 'auraheal/fix-{issue_id}' using GitPython.
        """
        from git import Repo
        logger.info(f"Opening Git repository at {repo_path}...")
        repo = Repo(repo_path)
        
        branch_name = f"auraheal/fix-{issue_id}"

        if branch_name in repo.heads:
            logger.info(f"Checking out existing branch '{branch_name}'")
            repo.git.checkout(branch_name)
        else:
            logger.info(f"Creating and checking out branch '{branch_name}'")
            new_head = repo.create_head(branch_name)
            repo.git.checkout(new_head)
            
        return branch_name

    def commit_and_stage_changes(self, repo_path: str, message: str) -> bool:
        """
        Stages all modified repository changes and commits them.
        """
        from git import Repo
        logger.info(f"Staging all changes in {repo_path}...")
        repo = Repo(repo_path)
        
        # Stage everything (A=True is equivalent to git add -A)
        repo.git.add(A=True)

        if not repo.is_dirty(untracked_files=True):
            logger.warning("No changes to commit. Working tree is clean.")
            return False

        repo.index.commit(message)
        logger.info(f"Committed healing patches. Message: '{message}'")
        return True

# =====================================================================
# 4. GRAPH INTEGRATION PREPARATION
# =====================================================================

async def execute_workspace_mutation(
    manifest: MutationManifest,
    workspace_root: str,
    issue_id: str = "visual-patch"
) -> Dict[str, Any]:
    """
    Strings together branching, patching, and staging actions in a fully async pipeline.
    Returns a status dict confirming the operation results.
    """
    logger.info("Starting workspace mutation process...")
    service = SourceMutationService()

    # Step 1: Branching
    branch_name = service.create_healing_branch(workspace_root, issue_id)

    # Step 2: Patching files
    modified_files = set()
    for patch in manifest.patches:
        await service.apply_css_patch(patch, workspace_root)
        modified_files.update(patch.targets)

    # Step 3: Staging and committing
    committed = service.commit_and_stage_changes(
        workspace_root,
        f"AuraHeal.AI auto-healing: resolved visual layout anomalies (fixes issue {issue_id})"
    )

    return {
        "status": "success",
        "active_branch": branch_name,
        "updated_files_count": len(modified_files),
        "updated_files": list(modified_files),
        "changes_committed": committed
    }
