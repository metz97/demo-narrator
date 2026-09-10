"""Issue-tracker providers for demo mode (list sprints, pick work items).

Server-friendly REST implementations — no interactive CLI login in the request
path. Azure DevOps is the first provider; the seam is the place Jira/GitHub
land later.
"""

from .azure_devops import AzureDevOpsTracker, Sprint, WorkItemSummary, tracker_from_profile

__all__ = ["AzureDevOpsTracker", "Sprint", "WorkItemSummary", "tracker_from_profile"]
