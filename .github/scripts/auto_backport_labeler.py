#!/usr/bin/env python3
"""
# Software License Agreement (BSD License)
#
# Copyright (c) 2026, Moveit2 Community developers, Isaac Saito
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""
"""
Automated Backport Labeler and Verifier for MoveIt 2.

Determines if a merged PR into main represents a non-feature (bug fix, chore, docs, etc.)
and verifies whether the underlying issue/code is present in each maintained target branch.
If present, adds the corresponding 'backport-<branch>' label so Mergify can open a backport pull request of it.
If not present, leaves an explanatory comment on the PR detailing which backport labels were omitted and why.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import yaml


def run_cmd(cmd, cwd=None, check=False):
    """Executes a subprocess command and returns (returncode, stdout, stderr)."""
    res = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr}")
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def get_maintained_branches(mergify_path: str, branch_filter: list[str]=None):
    """
    Extracts target backport branches from .github/mergify.yml.
    Validates against existing remote heads on origin.
    Args:
        mergify_path: Path to the .github/mergify.yml file.
        branch_filter (list[str], optional): List of branch names to filter by.

    Returns:
        list[str]: List of maintained branches.
    """
    if not os.path.exists(mergify_path):
        print(f"Warning: {mergify_path} not found.")
        return []

    with open(mergify_path, "r", encoding="utf-8") as f:
        mergify_yaml_file = yaml.safe_load(f)

    discovered_branches = []
    for rule in mergify_yaml_file.get("pull_request_rules", []):
        backport = rule.get("actions", {}).get("backport", {})
        for b in backport.get("branches", []):
            if b not in discovered_branches:
                discovered_branches.append(b)

    # Filter invalid branches e.g. non-existent on the remote origin.
    rc, stdout, _ = run_cmd(["git", "ls-remote", "--heads", "origin"])
    if rc == 0:
        remote_heads = [
            line.split("refs/heads/")[1]
            for line in stdout.splitlines()
            if "refs/heads/" in line
        ]
        discovered_branches = [b for b in discovered_branches if b in remote_heads]

    if branch_filter:
        filter_set = set(b.strip() for b in branch_filter.split(",") if b.strip())
        discovered_branches = [b for b in discovered_branches if b in filter_set]

    return discovered_branches


def is_feature_pr(title: str, labels: list[str]=None, head_branch: str=None) -> tuple[bool, str]:
    """
    Determines if a PR is a feature / capability (which should NOT be automatically backported).
    Returns (is_feature: bool, reason: str).

    Args:
        title: The title of the PR.
        labels (optional): The labels associated with the PR.
        head_branch (optional): The head branch of the PR.

    Returns:
        A tuple containing a boolean indicating if the PR is a feature PR and a reason string.
    """
    labels = [l.lower() for l in (labels or [])]
    title_lower = title.lower().strip()

    # Skip backport PRs created by e.g. bots
    if re.search(r"\(backport\s+#\d+\)", title_lower):
        return True, "PR is already a backport PR"

    # Feature labels
    feature_labels = {
        "enhancement",
        "feature",
        "feature-request",
        "new feature",
        "epic",
        "roadmap",
    }
    matched = feature_labels.intersection(set(labels))
    if matched:
        return True, f"PR is labeled with feature label(s): '{list(matched)[0]}'"

    # Release / bump PRs
    if "release" in labels or re.match(r"^\d+\.\d+\.\d+$", title.strip()):
        return True, "PR is a release or version bump"

    # Conventional commit feature prefix
    if re.match(r"^(feat|feature)(\([^\)]+\))?!?:", title_lower) or title_lower.startswith("[feature]") or title_lower.startswith("[feat]"):
        return True, f"PR title indicates a feature: '{title}'"

    # Head branch name convention
    if head_branch and (head_branch.startswith("feat/") or head_branch.startswith("feature/")):
        return True, f"PR head branch indicates a feature: '{head_branch}'"

    return False, "PR is a bugfix, maintenance, or non-feature change (eligible for backporting)"


def was_commit_previously_backported(commit_sha: str, target_ref: str) -> tuple[bool, str]:
    """
    Checks if commit_sha (or its associated PR) was previously backported to target_ref.
    """
    # 1. Search git log on target branch for cherry-pick metadata referencing the commit SHA
    rc, stdout, _ = run_cmd(["git", "log", target_ref, f"--grep={commit_sha}", "-n", "1", "--oneline"])
    if rc == 0 and stdout:
        return True, f"Commit {commit_sha[:9]} was previously backported in commit: {stdout}"

    # 2. Search git log on target branch for backport PR title referencing the original PR number
    rc, commit_msg, _ = run_cmd(["git", "log", "-1", "--format=%s%n%b", commit_sha])
    if rc == 0 and commit_msg:
        pr_matches = re.findall(r"#(\d+)", commit_msg)
        for pr_num in pr_matches:
            rc, stdout, _ = run_cmd(
                ["git", "log", target_ref, f"--grep=backport.*#{pr_num}", "-n", "1", "--oneline"]
            )
            if rc == 0 and stdout:
                return True, f"Original PR #{pr_num} was previously backported in commit: {stdout}"

    return False, "No previous backport found in target branch log"


def do_modified_lines_exist_in_target(target_ref: str, file_path: str, deleted_lines: list[str]) -> bool:
    """
    Checks if non-trivial lines being modified/deleted by the PR exist in target_ref:file_path.
    """
    rc, content, _ = run_cmd(["git", "show", f"{target_ref}:{file_path}"])
    if rc != 0:
        return False
    target_lines = set(line.strip() for line in content.splitlines() if line.strip())
    meaningful = [l.strip() for l in deleted_lines if len(l.strip()) > 3]
    if not meaningful:
        return False
    matches = sum(1 for l in meaningful if l in target_lines)
    return matches >= len(meaningful) * 0.5


def verify_issue_presence_in_branch(commit: str, target_branch: str) -> tuple[bool, str]:
    """
    Verifies if the issue/problem addressed by commit is present in origin/<target_branch>.

    The verification performs a multi-stage check against the target branch:
    1. Target branch existence:
       Ensures 'origin/<target_branch>' exists locally.
    2. File-level compatibility:
       - For newly added files ('A'): verifies their target parent directories exist
         in '<target_branch>'.
       - For modified ('M') or deleted ('D') files: verifies that every affected
         file exists in '<target_branch>'.
    3. Line-level code ancestry (issue presence):
       Checks whether the code being patched actually exists in '<target_branch>':
       - Finds the fork point ('merge_base') where '<target_branch>' split from 'main'.
       - Inspects the lines modified by this PR ('git diff -U0').
       - Traces back who originally wrote those lines ('git blame').
       - If those lines were added to 'main' AFTER '<target_branch>' split off:
         * Checks if the commit that introduced them was previously backported to '<target_branch>'.
         * Checks if the lines being changed actually exist in '<target_branch>'.
         * If neither is true, '<target_branch>' never had that code (and thus never had the bug),
           so backporting is skipped.

    Args:
        commit: The commit hash to verify.
        target_branch: The target branch name. Validity of the branch name is NOT checked i.e. it must be validated by the caller.

    Returns:
        A tuple containing a boolean indicating if the issue is present and a reason string.
    """
    ref = f"origin/{target_branch}"

    # Verify target branch exists in local git
    rc, _, _ = run_cmd(["git", "rev-parse", "--verify", ref])
    if rc != 0:
        return False, f"Target branch '{ref}' does not exist."

    # Check modified/deleted files in commit
    rc, stdout, stderr = run_cmd(
        ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", f"{commit}~1", commit]
    )
    if rc != 0:
        return False, f"Could not inspect diff for commit {commit}: {stderr}"

    file_entries = [line.split("\t") for line in stdout.splitlines() if line]
    modified_files = [f for status, f in file_entries if status in ("M", "D")]
    added_files = [f for status, f in file_entries if status == "A"]

    # Verify newly added files (e.g. new test cases): their parent directory must exist in target branch
    for f in added_files:
        dir_name = os.path.dirname(f)
        if dir_name:
            rc, _, _ = run_cmd(["git", "cat-file", "-e", f"{ref}:{dir_name}"])
            if rc != 0:
                return (
                    False,
                    f"Target directory '{dir_name}' for newly added file '{f}' does not exist in '{target_branch}'.",
                )

    if not modified_files:
        # Commit only adds new files into existing directories
        return True, f"New file(s) can be added to existing directory in '{target_branch}'."

    # Verify that pre-existing modified/deleted files exist in target branch
    missing_files = []
    for f in modified_files:
        rc, _, _ = run_cmd(["git", "cat-file", "-e", f"{ref}:{f}"])
        if rc != 0:
            missing_files.append(f)

    if missing_files:
        return False, f"Modified file(s) do not exist in '{target_branch}': {', '.join(missing_files)}"

    # Check code ancestry on modified lines
    rc, merge_base, _ = run_cmd(["git", "merge-base", f"{commit}~1", ref])
    if rc == 0 and merge_base:
        for f in modified_files:
            rc, diff_out, _ = run_cmd(["git", "diff", "-U0", f"{commit}~1", commit, "--", f])
            if rc != 0:
                continue

            # Extract deleted lines in diff
            deleted_lines = [
                line[1:] for line in diff_out.splitlines() if line.startswith("-") and not line.startswith("---")
            ]

            hunks = re.findall(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff_out)
            for h_start, h_count, _, _ in hunks:
                start_line = int(h_start)
                count = int(h_count) if h_count else 1
                if count == 0:
                    continue

                rc, blame_out, _ = run_cmd(
                    ["git", "blame", "-l", f"-L{start_line},{start_line+count-1}", f"{commit}~1", "--", f]
                )
                if rc != 0:
                    continue

                line_commits = [line.split()[0] for line in blame_out.splitlines() if line]
                for c in set(line_commits):
                    rc_anc, _, _ = run_cmd(["git", "merge-base", "--is-ancestor", c, ref])
                    if rc_anc != 0:
                        # c is not in target_branch ancestry. Was it introduced after merge_base?
                        rc_after, _, _ = run_cmd(["git", "merge-base", "--is-ancestor", merge_base, c])
                        if rc_after == 0 and c != merge_base:
                            # Check if commit c was previously backported to target branch
                            was_bp, bp_detail = was_commit_previously_backported(c, ref)
                            if was_bp:
                                print(f"  Target '{target_branch}': {bp_detail}")
                                continue

                            # Check if the modified lines exist in target branch file
                            if do_modified_lines_exist_in_target(ref, f, deleted_lines):
                                continue

                            return (
                                False,
                                f"Modified code in '{f}' (lines {start_line}-{start_line+count-1}) was introduced in commit {c[:9]}, "
                                f"which post-dates the '{target_branch}' branch point, was not previously backported, and does not exist in '{target_branch}'.",
                            )

    return True, f"Codebase and modified lines verified present in '{target_branch}'."


def fetch_pr_info(pr_number):
    """Fetches PR metadata using GitHub CLI."""
    rc, stdout, stderr = run_cmd(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,title,labels,headRefName,mergeCommit,mergedAt,baseRefName",
        ]
    )
    if rc != 0:
        raise RuntimeError(f"Failed to fetch PR #{pr_number}: {stderr}")
    return json.loads(stdout)


def add_pr_label(pr_number: int, label: str, dry_run=False) -> bool:
    """Adds a label to the PR."""
    if dry_run:
        print(f"[DRY-RUN] Adding label '{label}' to PR #{pr_number}")
        return True
    rc, _, stderr = run_cmd(["gh", "pr", "edit", str(pr_number), "--add-label", label])
    if rc != 0:
        print(f"Error adding label '{label}' to PR #{pr_number}: {stderr}", file=sys.stderr)
        return False
    print(f"Successfully added label '{label}' to PR #{pr_number}")
    return True


def post_pr_comment(pr_number: int, comment_body: str, dry_run=False):
    """Posts a comment on the PR."""
    if dry_run:
        print(f"[DRY-RUN] Posting comment on PR #{pr_number}:\n{comment_body}")
        return True
    rc, _, stderr = run_cmd(["gh", "pr", "comment", str(pr_number), "--body", comment_body])
    if rc != 0:
        print(f"Error posting comment to PR #{pr_number}: {stderr}", file=sys.stderr)
        return False
    print(f"Successfully posted comment on PR #{pr_number}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Auto backport labeler and verifier.")
    parser.add_argument("--pr-number", type=int, required=True, help="PR number")
    parser.add_argument("--commit", type=str, default=None, help="PR merge commit SHA")
    parser.add_argument("--mergify-config", type=str, default=".github/mergify.yml", help="Path to mergify.yml")
    parser.add_argument("--filter-branches", type=str, default=None, help="Optional comma-separated list of target branch names to evaluate (e.g. 'jazzy,humble' or 'humble'). If omitted, all maintained branches are evaluated.",)
    parser.add_argument("--dry-run", action="store_true", help="Dry run without modifying labels or commenting")

    args = parser.parse_args()

    # Fetch PR details
    pr_data = fetch_pr_info(args.pr_number)
    title = pr_data.get("title", "")
    labels = [l["name"] for l in pr_data.get("labels", [])]
    head_branch = pr_data.get("headRefName", "")
    base_branch = pr_data.get("baseRefName", "")
    merge_commit = args.commit or (pr_data.get("mergeCommit") or {}).get("oid")

    if not pr_data.get("mergedAt"):
        print(f"PR #{args.pr_number} is not merged. Skipping.")
        sys.exit(0)

    if base_branch != "main":
        print(f"PR #{args.pr_number} targeted '{base_branch}', not 'main'. Skipping backport check.")
        sys.exit(0)

    if not merge_commit:
        print(f"Error: Could not determine merge commit for PR #{args.pr_number}", file=sys.stderr)
        sys.exit(1)

    print(f"Evaluating PR #{args.pr_number}: '{title}' (Merge commit: {merge_commit[:9]})")

    # Check if PR is a feature/capability
    is_feat, feat_reason = is_feature_pr(title, labels, head_branch)
    if is_feat:
        print(f"PR #{args.pr_number} is classified as a feature/capability: {feat_reason}. No backports added.")
        sys.exit(0)

    print(f"PR #{args.pr_number} is NOT a feature ({feat_reason}). Evaluating target branches...")

    # Discover target maintained branches
    target_branches = get_maintained_branches(args.mergify_config, args.filter_branches)
    if not target_branches:
        print("No maintained branches discovered.")
        sys.exit(0)

    print(f"Discovered target branches: {target_branches}")

    # Fetch latest branches on origin to ensure accurate git ancestry
    run_cmd(["git", "fetch", "origin", "--depth=200"] + target_branches)

    # Verify presence in each target branch
    branch_results = {}
    labels_to_add = []
    skipped_branches = []

    for b in target_branches:
        label_name = f"backport-{b}"
        if label_name in labels:
            print(f"Branch '{b}' already has label '{label_name}'. Skipping check.")
            branch_results[b] = (True, "Label already present on PR", True)
            continue

        is_present, reason = verify_issue_presence_in_branch(merge_commit, b)
        branch_results[b] = (is_present, reason, False)

        if is_present:
            labels_to_add.append((b, label_name))
        else:
            skipped_branches.append((b, label_name, reason))

    # Apply labels for branches where issue is present
    for b, label_name in labels_to_add:
        add_pr_label(args.pr_number, label_name, dry_run=args.dry_run)

    # If any branch was skipped due to issue not being present, post comment.
    if skipped_branches:
        table_rows = []
        for b, (is_present, reason, already_had) in branch_results.items():
            lbl = f"`backport-{b}`"
            if already_had:
                status = "ℹ️ Already present"
                detail = "Label was already attached to the PR."
            elif is_present:
                status = "✅ Label added"
                detail = f"Verified present. Added {lbl} label."
            else:
                status = "⏭️ Skipped"
                detail = f"**Issue not present**: {reason}"
            table_rows.append(f"| `{b}` | {status} | {detail} |")

        comment_body = (
            f"### 🤖 Auto-Backport Verification Report\n\n"
            f"This PR was merged into `main` and evaluated for backporting to maintained branches:\n\n"
            f"| Target Branch | Status | Details |\n"
            f"| :--- | :--- | :--- |\n"
            + "\n".join(table_rows)
            + "\n\n"
            f"*Note: For skipped branches, the issue or code being addressed was not present. "
            f"If this fix is still desired on a skipped branch, maintainers can apply the backport label manually.*"
        )
        post_pr_comment(args.pr_number, comment_body, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
