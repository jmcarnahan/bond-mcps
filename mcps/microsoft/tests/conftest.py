"""Shared fixtures and mock data for Microsoft Graph tests."""

import os
from unittest.mock import AsyncMock

import pytest

# Tests exercise the MCP server via the FastMCP test client which triggers
# our lifespan, which runs auth.startup.verify_runtime_config(). That check
# wants a real DB and encryption key — neither of which the test suite
# stands up. Skip it for tests. NEVER set this env var in production.
os.environ.setdefault("BOND_MCPS_SKIP_STARTUP_VERIFY", "1")


@pytest.fixture
def no_sleep(monkeypatch):
    """Patch time.sleep and asyncio.sleep in ms_graph.files to avoid real waits in copy tests."""
    monkeypatch.setattr("ms_graph.files.time.sleep", lambda _: None)
    monkeypatch.setattr("ms_graph.files.asyncio.sleep", AsyncMock(return_value=None))

# ---------------------------------------------------------------------------
# Sample Graph API response payloads
# ---------------------------------------------------------------------------

SAMPLE_USER_PROFILE = {
    "id": "user-id-001",
    "displayName": "John Carnahan",
    "mail": "user@example.com",
    "userPrincipalName": "user@example.com",
    "jobTitle": None,
}

SAMPLE_MAILBOX_SETTINGS = {
    "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users('mailbox%40example.com')/mailboxSettings",
    "timeZone": "Pacific Standard Time",
    "automaticRepliesSetting": {"status": "disabled"},
}

SAMPLE_MESSAGE = {
    "id": "AAMkAGI2TG93AAA=",
    "subject": "Weekly Report",
    "receivedDateTime": "2025-12-15T10:30:00Z",
    "isRead": False,
    "from": {
        "emailAddress": {
            "name": "Alice Smith",
            "address": "alice@example.com",
        }
    },
    "toRecipients": [
        {"emailAddress": {"name": "Bob Jones", "address": "bob@example.com"}}
    ],
    "body": {
        "contentType": "text",
        "content": "Here is the weekly report.\n\nBest,\nAlice",
    },
}

SAMPLE_MESSAGE_2 = {
    "id": "AAMkAGI2TG94BBB=",
    "subject": "Re: Project Update",
    "receivedDateTime": "2025-12-14T08:00:00Z",
    "isRead": True,
    "from": {
        "emailAddress": {
            "name": "Charlie Brown",
            "address": "charlie@example.com",
        }
    },
    "toRecipients": [
        {"emailAddress": {"name": "Bob Jones", "address": "bob@example.com"}}
    ],
    "body": {
        "contentType": "html",
        "content": "<p>Looks good!</p>",
    },
}

SAMPLE_MESSAGES_RESPONSE = {"value": [SAMPLE_MESSAGE, SAMPLE_MESSAGE_2]}

SAMPLE_TEAM = {
    "id": "team-id-001",
    "displayName": "Engineering",
    "description": "Engineering team",
}

SAMPLE_TEAM_2 = {
    "id": "team-id-002",
    "displayName": "Marketing",
    "description": "Marketing team",
}

SAMPLE_TEAMS_RESPONSE = {"value": [SAMPLE_TEAM, SAMPLE_TEAM_2]}

SAMPLE_CHANNEL = {
    "id": "channel-id-001",
    "displayName": "General",
    "description": "General channel",
}

SAMPLE_CHANNEL_2 = {
    "id": "channel-id-002",
    "displayName": "Random",
    "description": "Random channel",
}

SAMPLE_CHANNELS_RESPONSE = {"value": [SAMPLE_CHANNEL, SAMPLE_CHANNEL_2]}

SAMPLE_CHANNEL_MESSAGE = {
    "id": "msg-001",
    "body": {"content": "Hello from CLI"},
    "createdDateTime": "2025-12-15T12:00:00Z",
}

GRAPH_ERROR_403 = {
    "error": {
        "code": "Authorization_RequestDenied",
        "message": "Insufficient privileges to complete the operation.",
    }
}

GRAPH_ERROR_404 = {
    "error": {
        "code": "ResourceNotFound",
        "message": "Resource could not be discovered.",
    }
}

# ---------------------------------------------------------------------------
# Drive / File sample payloads
# ---------------------------------------------------------------------------

SAMPLE_DRIVE_ITEM_FILE = {
    "id": "file-id-001",
    "name": "report.csv",
    "size": 1024,
    "file": {"mimeType": "text/csv"},
    "lastModifiedDateTime": "2025-12-15T10:30:00Z",
    "lastModifiedBy": {
        "user": {"displayName": "Alice Smith", "id": "user-001"}
    },
    "webUrl": "https://onedrive.live.com/edit.aspx?resid=file-id-001",
    "parentReference": {
        "driveId": "drive-001",
        "path": "/drive/root:/Documents",
    },
}

SAMPLE_DRIVE_ITEM_FOLDER = {
    "id": "folder-id-001",
    "name": "Documents",
    "folder": {"childCount": 5},
    "lastModifiedDateTime": "2025-12-14T08:00:00Z",
    "lastModifiedBy": {
        "user": {"displayName": "Bob Jones", "id": "user-002"}
    },
    "webUrl": "https://onedrive.live.com/redir?resid=folder-id-001",
    "parentReference": {
        "driveId": "drive-001",
        "path": "/drive/root:",
    },
}

SAMPLE_DRIVE_ITEM_BINARY = {
    "id": "file-id-002",
    "name": "presentation.pptx",
    "size": 2_500_000,
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "lastModifiedDateTime": "2025-12-13T15:00:00Z",
    "lastModifiedBy": {
        "user": {"displayName": "Charlie Brown", "id": "user-003"}
    },
    "webUrl": "https://onedrive.live.com/edit.aspx?resid=file-id-002",
    "parentReference": {
        "driveId": "drive-001",
        "path": "/drive/root:/Documents",
    },
}

SAMPLE_DRIVE_ITEM_LARGE_TEXT = {
    "id": "file-id-003",
    "name": "huge-log.txt",
    "size": 600_000,  # Exceeds 512 KB cap
    "file": {"mimeType": "text/plain"},
    "lastModifiedDateTime": "2025-12-12T09:00:00Z",
    "lastModifiedBy": {
        "user": {"displayName": "Alice Smith", "id": "user-001"}
    },
    "webUrl": "https://onedrive.live.com/edit.aspx?resid=file-id-003",
    "parentReference": {
        "driveId": "drive-001",
        "path": "/drive/root:",
    },
}

SAMPLE_DRIVE_CHILDREN_RESPONSE = {
    "value": [SAMPLE_DRIVE_ITEM_FOLDER, SAMPLE_DRIVE_ITEM_FILE, SAMPLE_DRIVE_ITEM_BINARY]
}

SAMPLE_SITE = {
    "id": "site-id-001",
    "displayName": "Engineering Hub",
    "name": "engineering",
    "webUrl": "https://contoso.sharepoint.com/sites/engineering",
}

SAMPLE_SITE_2 = {
    "id": "site-id-002",
    "displayName": "Marketing Portal",
    "name": "marketing",
    "webUrl": "https://contoso.sharepoint.com/sites/marketing",
}

SAMPLE_SITES_RESPONSE = {"value": [SAMPLE_SITE, SAMPLE_SITE_2]}

SAMPLE_SEARCH_RESPONSE = {
    "value": [
        {
            "hitsContainers": [
                {
                    "hits": [
                        {
                            "resource": {
                                "id": "search-file-001",
                                "name": "Q4-budget.xlsx",
                                "size": 45000,
                                "webUrl": "https://contoso.sharepoint.com/sites/finance/Q4-budget.xlsx",
                                "file": {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
                                "lastModifiedDateTime": "2025-12-10T14:00:00Z",
                                "lastModifiedBy": {
                                    "user": {"displayName": "Finance Team"}
                                },
                            },
                            "summary": "Q4 <c0>budget</c0> projections for 2025",
                        },
                        {
                            "resource": {
                                "id": "search-file-002",
                                "name": "budget-notes.md",
                                "size": 2048,
                                "webUrl": "https://contoso.sharepoint.com/sites/finance/budget-notes.md",
                                "file": {"mimeType": "text/markdown"},
                                "lastModifiedDateTime": "2025-12-09T11:00:00Z",
                                "lastModifiedBy": {
                                    "user": {"displayName": "Alice Smith"}
                                },
                            },
                            "summary": "Notes on <c0>budget</c0> review meeting",
                        },
                    ],
                    "total": 2,
                    "moreResultsAvailable": False,
                }
            ],
            "searchTerms": ["budget"],
        }
    ]
}

SAMPLE_SEARCH_RESPONSE_EMPTY = {
    "value": [
        {
            "hitsContainers": [
                {
                    "hits": [],
                    "total": 0,
                    "moreResultsAvailable": False,
                }
            ],
            "searchTerms": ["nonexistent"],
        }
    ]
}


# ---------------------------------------------------------------------------
# Teams channel message payloads
# ---------------------------------------------------------------------------

SAMPLE_CHANNEL_MESSAGE_USER = {
    "id": "msg-user-001",
    "messageType": "message",
    "createdDateTime": "2025-12-15T12:00:00Z",
    "from": {"user": {"displayName": "Alice Smith"}, "application": None},
    "body": {"contentType": "text", "content": "Hello team!"},
    "attachments": [],
}

SAMPLE_CHANNEL_MESSAGE_BOT = {
    "id": "msg-bot-001",
    "messageType": "message",
    "createdDateTime": "2025-12-15T11:00:00Z",
    "from": {"application": {"displayName": "Power Automate"}, "user": None},
    "body": {"contentType": "html", "content": ""},
    "attachments": [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": '{"type":"AdaptiveCard","body":[{"type":"TextBlock","text":"Build completed successfully"},{"type":"TextBlock","text":"Pipeline: main-deploy"}]}',
        }
    ],
}

SAMPLE_CHANNEL_MESSAGES_RESPONSE = {
    "value": [SAMPLE_CHANNEL_MESSAGE_USER, SAMPLE_CHANNEL_MESSAGE_BOT]
}


# ---------------------------------------------------------------------------
# Chat payloads
# ---------------------------------------------------------------------------

SAMPLE_CHAT_ONEONONE = {
    "id": "chat-1on1-001",
    "chatType": "oneOnOne",
    "topic": None,
    "lastUpdatedDateTime": "2025-12-15T14:00:00Z",
    "members": [
        {"displayName": "Alice Smith"},
        {"displayName": "Bob Jones"},
    ],
    "lastMessagePreview": {
        "createdDateTime": "2025-12-15T14:00:00Z",
        "body": {"content": "Sounds good!"},
        "from": {"user": {"displayName": "Alice Smith"}},
    },
}

SAMPLE_CHAT_GROUP = {
    "id": "chat-group-001",
    "chatType": "group",
    "topic": "Project Standup",
    "lastUpdatedDateTime": "2025-12-15T13:00:00Z",
    "members": [
        {"displayName": "Alice Smith"},
        {"displayName": "Bob Jones"},
        {"displayName": "Charlie Brown"},
    ],
    "lastMessagePreview": {
        "createdDateTime": "2025-12-15T13:00:00Z",
        "body": {"content": "Meeting at 3pm"},
        "from": {"user": {"displayName": "Bob Jones"}},
    },
}

SAMPLE_CHAT_MEETING = {
    "id": "chat-meeting-001",
    "chatType": "meeting",
    "topic": "Sprint Review",
    "lastUpdatedDateTime": "2025-12-15T10:00:00Z",
    "members": [
        {"displayName": "Alice Smith"},
        {"displayName": "Bob Jones"},
    ],
    "lastMessagePreview": {
        "createdDateTime": "2025-12-15T10:00:00Z",
        "body": {"content": "Notes attached"},
        "from": {"user": {"displayName": "Alice Smith"}},
    },
}

SAMPLE_CHATS_RESPONSE = {
    "value": [SAMPLE_CHAT_ONEONONE, SAMPLE_CHAT_GROUP, SAMPLE_CHAT_MEETING]
}

SAMPLE_CHAT_MESSAGES_RESPONSE = {
    "value": [SAMPLE_CHANNEL_MESSAGE_USER]
}

SAMPLE_CHAT_MESSAGE_SENT = {
    "id": "chat-msg-sent-001",
    "body": {"content": "Hello!"},
}


# ---------------------------------------------------------------------------
# File write / copy / rename payloads
# ---------------------------------------------------------------------------

SAMPLE_DRIVE_ITEM_WORD = {
    "id": "file-id-word-001",
    "name": "template.docx",
    "size": 25_600,
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "lastModifiedDateTime": "2025-12-15T10:00:00Z",
    "lastModifiedBy": {"user": {"displayName": "Alice Smith", "id": "user-001"}},
    "webUrl": "https://contoso.sharepoint.com/sites/engineering/template.docx",
    "parentReference": {
        "driveId": "drive-site-001",
        "id": "folder-id-root",
        "path": "/drive/root:",
    },
}

SAMPLE_UPLOADED_FILE = {
    "id": "file-id-uploaded-001",
    "name": "report.md",
    "size": 1024,
    "file": {"mimeType": "text/markdown"},
    "lastModifiedDateTime": "2025-12-15T11:00:00Z",
    "lastModifiedBy": {"user": {"displayName": "Bob Jones", "id": "user-002"}},
    "webUrl": "https://onedrive.live.com/edit.aspx?resid=file-id-uploaded-001",
    "parentReference": {"driveId": "drive-001", "id": "folder-id-001"},
}

SAMPLE_COPY_IN_PROGRESS = {
    "status": "inProgress",
    "percentageComplete": 50.0,
    "operation": "ItemCopy",
}

SAMPLE_COPY_COMPLETED = {
    "status": "completed",
    "percentageComplete": 100.0,
    "resourceId": "file-id-copy-001",
    "operation": "ItemCopy",
}

SAMPLE_COPY_FAILED = {
    "status": "failed",
    "percentageComplete": 0.0,
    "error": {
        "code": "accessDenied",
        "message": "Insufficient privileges to copy the file.",
    },
}


# ---------------------------------------------------------------------------
# Power BI sample payloads
# ---------------------------------------------------------------------------

SAMPLE_PBI_WORKSPACE = {
    "id": "ws-id-001",
    "name": "Analytics Hub",
    "type": "Workspace",
    "isReadOnly": False,
    "isOnDedicatedCapacity": True,
}

SAMPLE_PBI_WORKSPACE_2 = {
    "id": "ws-id-002",
    "name": "Finance Reports",
    "type": "Workspace",
    "isReadOnly": False,
    "isOnDedicatedCapacity": False,
}

SAMPLE_PBI_WORKSPACES_RESPONSE = {"value": [SAMPLE_PBI_WORKSPACE, SAMPLE_PBI_WORKSPACE_2]}

SAMPLE_PBI_DATASET = {
    "id": "ds-id-001",
    "name": "Sales",
    "webUrl": "https://app.powerbi.com/datasets/ds-id-001",
    "addRowsAPIEnabled": False,
    "configuredBy": "alice@example.com",
    "isRefreshable": True,
    "isOnPremGatewayRequired": False,
}

SAMPLE_PBI_DATASET_2 = {
    "id": "ds-id-002",
    "name": "Marketing KPIs",
    "webUrl": "https://app.powerbi.com/datasets/ds-id-002",
    "addRowsAPIEnabled": False,
    "configuredBy": "bob@example.com",
    "isRefreshable": True,
    "isOnPremGatewayRequired": False,
}

SAMPLE_PBI_DATASETS_RESPONSE = {"value": [SAMPLE_PBI_DATASET, SAMPLE_PBI_DATASET_2]}

SAMPLE_PBI_REPORT = {
    "id": "rpt-id-001",
    "name": "Q4 Dashboard",
    "datasetId": "ds-id-001",
    "webUrl": "https://app.powerbi.com/reports/rpt-id-001",
    "embedUrl": "https://app.powerbi.com/reportEmbed?reportId=rpt-id-001",
    "reportType": "PowerBIReport",
}

SAMPLE_PBI_REPORT_2 = {
    "id": "rpt-id-002",
    "name": "Monthly Revenue",
    "datasetId": "ds-id-002",
    "webUrl": "https://app.powerbi.com/reports/rpt-id-002",
    "embedUrl": "https://app.powerbi.com/reportEmbed?reportId=rpt-id-002",
    "reportType": "PowerBIReport",
}

SAMPLE_PBI_REPORTS_RESPONSE = {"value": [SAMPLE_PBI_REPORT, SAMPLE_PBI_REPORT_2]}

SAMPLE_PBI_DASHBOARD = {
    "id": "dash-id-001",
    "displayName": "Executive Overview",
    "webUrl": "https://app.powerbi.com/dashboards/dash-id-001",
    "embedUrl": "https://app.powerbi.com/dashboardEmbed?dashboardId=dash-id-001",
    "isReadOnly": False,
}

SAMPLE_PBI_DASHBOARDS_RESPONSE = {"value": [SAMPLE_PBI_DASHBOARD]}

SAMPLE_PBI_DAX_RESULT = {
    "results": [
        {
            "tables": [
                {
                    "rows": [
                        {"[Region]": "West", "[Sales Amount]": 1234567.89, "[Units]": 4200},
                        {"[Region]": "East", "[Sales Amount]": 987654.32, "[Units]": 3100},
                        {"[Region]": "Central", "[Sales Amount]": 543210.00, "[Units]": 1800},
                    ]
                }
            ]
        }
    ]
}

SAMPLE_PBI_DAX_EMPTY = {
    "results": [{"tables": [{"rows": []}]}]
}


# ---------------------------------------------------------------------------
# Calendar sample payloads
# ---------------------------------------------------------------------------

SAMPLE_CALENDAR_EVENT = {
    "id": "AAMkAGI2-event-001",
    "subject": "Sprint Planning",
    "start": {"dateTime": "2026-05-08T10:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-05-08T11:00:00.0000000", "timeZone": "UTC"},
    "location": {"displayName": "Conference Room A"},
    "organizer": {"emailAddress": {"name": "Alice Smith", "address": "alice@example.com"}},
    "isAllDay": False,
    "isCancelled": False,
    "isOnlineMeeting": True,
    "onlineMeetingUrl": "https://teams.microsoft.com/meet/123",
    "bodyPreview": "Let's plan the sprint.",
    "attendees": [
        {
            "emailAddress": {"name": "Bob Jones", "address": "bob@example.com"},
            "type": "required",
            "status": {"response": "accepted"},
        }
    ],
    "body": {"contentType": "text", "content": "Let's plan the sprint.\n\nAgenda:\n1. Review backlog"},
    "recurrence": None,
}

SAMPLE_CALENDAR_EVENT_ALLDAY = {
    "id": "AAMkAGI2-event-002",
    "subject": "Company Holiday",
    "start": {"dateTime": "2026-05-25T00:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-05-26T00:00:00.0000000", "timeZone": "UTC"},
    "location": {"displayName": ""},
    "organizer": {"emailAddress": {"name": "HR Team", "address": "hr@example.com"}},
    "isAllDay": True,
    "isCancelled": False,
    "isOnlineMeeting": False,
    "onlineMeetingUrl": "",
    "bodyPreview": "",
    "attendees": [],
    "body": {"contentType": "text", "content": ""},
    "recurrence": None,
}

SAMPLE_CALENDAR_EVENTS_RESPONSE = {"value": [SAMPLE_CALENDAR_EVENT, SAMPLE_CALENDAR_EVENT_ALLDAY]}

SAMPLE_SCHEDULE_RESPONSE = {
    "value": [
        {
            "scheduleId": "alice@example.com",
            "availabilityView": "0000220000220000",
            "scheduleItems": [
                {
                    "subject": "Sprint Planning",
                    "start": {"dateTime": "2026-05-08T10:00:00.0000000", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-05-08T11:00:00.0000000", "timeZone": "UTC"},
                    "status": "busy",
                },
            ],
        },
        {
            "scheduleId": "bob@example.com",
            "availabilityView": "0000000000000000",
            "scheduleItems": [],
        },
    ]
}

SAMPLE_CREATED_EVENT = {
    "id": "AAMkAGI2-event-new-001",
    "subject": "Design Review",
    "start": {"dateTime": "2026-05-09T14:00:00.0000000", "timeZone": "America/Los_Angeles"},
    "end": {"dateTime": "2026-05-09T15:00:00.0000000", "timeZone": "America/Los_Angeles"},
    "onlineMeetingUrl": "https://teams.microsoft.com/meet/789",
}

SAMPLE_PBI_REFRESH_HISTORY = {
    "value": [
        {
            "refreshType": "OnDemand",
            "startTime": "2026-05-04T10:00:00Z",
            "endTime": "2026-05-04T10:05:30Z",
            "status": "Completed",
        },
        {
            "refreshType": "Scheduled",
            "startTime": "2026-05-04T06:00:00Z",
            "endTime": "2026-05-04T06:04:15Z",
            "status": "Completed",
        },
    ]
}

SAMPLE_PBI_EXPORT_IN_PROGRESS = {
    "id": "export-id-001",
    "createdDateTime": "2026-05-04T10:00:00Z",
    "lastActionDateTime": "2026-05-04T10:00:05Z",
    "reportId": "rpt-id-001",
    "status": "Running",
    "percentComplete": 40,
    "resourceFileExtension": ".pdf",
}

SAMPLE_PBI_EXPORT_SUCCEEDED = {
    "id": "export-id-001",
    "createdDateTime": "2026-05-04T10:00:00Z",
    "lastActionDateTime": "2026-05-04T10:00:15Z",
    "reportId": "rpt-id-001",
    "status": "Succeeded",
    "percentComplete": 100,
    "resourceFileExtension": ".pdf",
    "resourceLocation": "https://api.powerbi.com/v1.0/myorg/groups/ws-id-001/reports/rpt-id-001/exports/export-id-001/file",
}

SAMPLE_PBI_EXPORT_FAILED = {
    "id": "export-id-002",
    "status": "Failed",
    "percentComplete": 0,
    "error": {
        "code": "PowerBIEntityNotFound",
        "message": "Report not found.",
    },
}
