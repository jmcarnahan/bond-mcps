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
    "displayName": "Test User",
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
    "bodyPreview": "Here is the weekly report. Best, Alice",
    "from": {
        "emailAddress": {
            "name": "Alice Smith",
            "address": "alice@example.com",
        }
    },
    "toRecipients": [{"emailAddress": {"name": "Bob Jones", "address": "bob@example.com"}}],
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
    "bodyPreview": "Looks good!",
    "from": {
        "emailAddress": {
            "name": "Charlie Brown",
            "address": "charlie@example.com",
        }
    },
    "toRecipients": [{"emailAddress": {"name": "Bob Jones", "address": "bob@example.com"}}],
    "body": {
        "contentType": "html",
        "content": "<p>Looks good!</p>",
    },
}

SAMPLE_MESSAGES_RESPONSE = {"value": [SAMPLE_MESSAGE, SAMPLE_MESSAGE_2]}

# Inbox rule (messageRule) payloads — mirror the verified Graph doc shapes.
SAMPLE_MESSAGE_RULE = {
    "id": "AQABBQ==-rule-001",
    "displayName": "From partner",
    "sequence": 2,
    "isEnabled": True,
    "hasError": False,
    "isReadOnly": False,
    "conditions": {"senderContains": ["adele"]},
    "actions": {"moveToFolder": "AQMkAGfolder", "stopProcessingRules": True},
}

SAMPLE_MESSAGE_RULE_2 = {
    "id": "AQABBQ==-rule-002",
    "displayName": "Newsletters to read later",
    "sequence": 3,
    "isEnabled": False,
    "hasError": False,
    "isReadOnly": False,
    "conditions": {"subjectContains": ["newsletter"]},
    "actions": {"markAsRead": True},
}

SAMPLE_MESSAGE_RULES_RESPONSE = {"value": [SAMPLE_MESSAGE_RULE, SAMPLE_MESSAGE_RULE_2]}

SAMPLE_MAIL_FOLDER = {
    "id": "AQMkAGfolder-001",
    "displayName": "Projects",
    "parentFolderId": "AQMkAGroot",
    "childFolderCount": 2,
    "totalItemCount": 42,
    "unreadItemCount": 3,
}

SAMPLE_MAIL_FOLDER_2 = {
    "id": "AQMkAGfolder-002",
    "displayName": "Receipts",
    "parentFolderId": "AQMkAGroot",
    "childFolderCount": 0,
    "totalItemCount": 10,
    "unreadItemCount": 0,
}

SAMPLE_MAIL_FOLDERS_RESPONSE = {"value": [SAMPLE_MAIL_FOLDER, SAMPLE_MAIL_FOLDER_2]}

SAMPLE_MESSAGES_PAGE1 = {
    "value": [SAMPLE_MESSAGE],
    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skip=1&$top=999",
}

SAMPLE_MESSAGES_PAGE2 = {
    "value": [SAMPLE_MESSAGE_2],
}

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
    "lastModifiedBy": {"user": {"displayName": "Alice Smith", "id": "user-001"}},
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
    "lastModifiedBy": {"user": {"displayName": "Bob Jones", "id": "user-002"}},
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
    "file": {
        "mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    },
    "lastModifiedDateTime": "2025-12-13T15:00:00Z",
    "lastModifiedBy": {"user": {"displayName": "Charlie Brown", "id": "user-003"}},
    "webUrl": "https://onedrive.live.com/edit.aspx?resid=file-id-002",
    "parentReference": {
        "driveId": "drive-001",
        "path": "/drive/root:/Documents",
    },
}

SAMPLE_DRIVE_ITEM_LARGE_TEXT = {
    "id": "file-id-003",
    "name": "huge-log.txt",
    "size": 3_000_000,  # Exceeds 2 MB cap
    "file": {"mimeType": "text/plain"},
    "lastModifiedDateTime": "2025-12-12T09:00:00Z",
    "lastModifiedBy": {"user": {"displayName": "Alice Smith", "id": "user-001"}},
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
                                "file": {
                                    "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                },
                                "lastModifiedDateTime": "2025-12-10T14:00:00Z",
                                "lastModifiedBy": {"user": {"displayName": "Finance Team"}},
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
                                "lastModifiedBy": {"user": {"displayName": "Alice Smith"}},
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
    "viewpoint": {"lastMessageReadDateTime": "2025-12-15T14:00:00Z"},
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
    "viewpoint": {"lastMessageReadDateTime": "2025-12-15T12:00:00Z"},
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
    "viewpoint": None,
}

SAMPLE_CHATS_RESPONSE = {"value": [SAMPLE_CHAT_ONEONONE, SAMPLE_CHAT_GROUP, SAMPLE_CHAT_MEETING]}

SAMPLE_CHAT_MESSAGES_RESPONSE = {"value": [SAMPLE_CHANNEL_MESSAGE_USER]}

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

SAMPLE_SHARED_DRIVE_ITEM = {
    "id": "shared-file-001",
    "name": "Q4-Presentation.pptx",
    "size": 3_500_000,
    "file": {
        "mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    },
    "lastModifiedDateTime": "2025-12-20T09:30:00Z",
    "lastModifiedBy": {"user": {"displayName": "Sajith P", "id": "user-ext-001"}},
    "webUrl": "https://mcafee-my.sharepoint.com/personal/sajith_pilakkavil/Documents/Q4-Presentation.pptx",
    "parentReference": {"driveId": "drive-ext-001", "id": "folder-shared-root"},
}

SAMPLE_SHARED_TEXT_FILE = {
    "id": "shared-file-002",
    "name": "notes.md",
    "size": 256,
    "file": {"mimeType": "text/markdown"},
    "lastModifiedDateTime": "2025-12-21T14:00:00Z",
    "lastModifiedBy": {"user": {"displayName": "Sajith P", "id": "user-ext-001"}},
    "webUrl": "https://mcafee-my.sharepoint.com/personal/sajith_pilakkavil/Documents/notes.md",
    "parentReference": {"driveId": "drive-ext-001", "id": "folder-shared-root"},
}

SAMPLE_SHARED_FOLDER_CHILDREN = {
    "value": [
        {
            "id": "shared-child-001",
            "name": "file1.docx",
            "size": 12000,
            "file": {
                "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            },
            "lastModifiedDateTime": "2025-12-22T08:00:00Z",
            "lastModifiedBy": {"user": {"displayName": "Sajith P"}},
            "webUrl": "https://mcafee-my.sharepoint.com/personal/sajith_pilakkavil/Documents/file1.docx",
        },
        {
            "id": "shared-child-002",
            "name": "data.csv",
            "size": 2048,
            "file": {"mimeType": "text/csv"},
            "lastModifiedDateTime": "2025-12-22T09:00:00Z",
            "lastModifiedBy": {"user": {"displayName": "Sajith P"}},
            "webUrl": "https://mcafee-my.sharepoint.com/personal/sajith_pilakkavil/Documents/data.csv",
        },
    ]
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

SAMPLE_PBI_DAX_EMPTY = {"results": [{"tables": [{"rows": []}]}]}


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
    "body": {
        "contentType": "text",
        "content": "Let's plan the sprint.\n\nAgenda:\n1. Review backlog",
    },
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


# ---------------------------------------------------------------------------
# Desktop JSON sample payloads
# ---------------------------------------------------------------------------

# Graph nextLink/deltaLink values are absolute URLs carrying an opaque token.
# They must be fetched verbatim, which is what makes them worth pinning here.
SAMPLE_DELTA_NEXT_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"
    "?$skiptoken=skip%2Btoken%2Fvalue"
)
SAMPLE_DELTA_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"
    "?$deltatoken=delta%2Btoken%2Fvalue"
)

SAMPLE_DELTA_MESSAGE = {
    "id": "AAMkAGI2delta001=",
    "internetMessageId": "<abc123@example.com>",
    "conversationId": "conv-001",
    "subject": "Budget question",
    "from": {"emailAddress": {"name": "Alice Smith", "address": "alice@example.com"}},
    "toRecipients": [{"emailAddress": {"name": "Bob Jones", "address": "bob@example.com"}}],
    "receivedDateTime": "2026-01-05T09:00:00Z",
    "isRead": False,
    "isDraft": False,
    "bodyPreview": "Quick question about the Q1 budget",
}

# A tombstone: Graph reports deletions as a bare id plus @removed.
SAMPLE_DELTA_TOMBSTONE = {
    "id": "AAMkAGI2delta002=",
    "@removed": {"reason": "deleted"},
}

SAMPLE_DELTA_PAGE_NEXT = {
    "@odata.nextLink": SAMPLE_DELTA_NEXT_LINK,
    "value": [SAMPLE_DELTA_MESSAGE],
}

SAMPLE_DELTA_PAGE_FINAL = {
    "@odata.deltaLink": SAMPLE_DELTA_LINK,
    "value": [SAMPLE_DELTA_TOMBSTONE],
}

GRAPH_ERROR_410 = {
    "error": {
        "code": "resyncRequired",
        "message": "Resync required. Replace local state with the server state.",
    }
}

GRAPH_ERROR_400 = {
    "error": {
        "code": "ErrorInvalidParameter",
        "message": "The Prefer header value is not supported.",
    }
}

# Graph message IDs routinely contain '/' and '+', which is why the new mail
# ops percent-encode them into the path.
SAMPLE_AWKWARD_MESSAGE_ID = "AAMkA/GI2+TG93AAA="

SAMPLE_MESSAGE_WITH_ATTACHMENTS = {**SAMPLE_MESSAGE, "hasAttachments": True}

SAMPLE_MESSAGE_DETAIL_NO_BODY = {
    "id": SAMPLE_MESSAGE["id"],
    "hasAttachments": False,
    "internetMessageHeaders": [],
}

# SAMPLE_MESSAGE_DETAIL lives below, with the attachment samples it $expands.

SAMPLE_REPLY_DRAFT = {
    "id": "AAMkAGI2draft001=",
    "isDraft": True,
    "webLink": "https://outlook.office.com/mail/deeplink/AAMkAGI2draft001",
}

SAMPLE_CHATS_PAGE_NEXT_LINK = "https://graph.microsoft.com/v1.0/me/chats?$skiptoken=chat%2Bskip"

# lastMessagePreview is null on a chat that has never carried a message.
SAMPLE_CHAT_NO_PREVIEW = {
    "id": "chat-empty-001",
    "chatType": "group",
    "topic": "Newly Created",
    "lastMessagePreview": None,
}

SAMPLE_CHATS_PAGE = {
    "@odata.nextLink": SAMPLE_CHATS_PAGE_NEXT_LINK,
    "value": [SAMPLE_CHAT_ONEONONE, SAMPLE_CHAT_GROUP, SAMPLE_CHAT_NO_PREVIEW],
}

SAMPLE_CHAT_MEMBERS_RESPONSE = {
    "value": [
        {
            "id": "member-001",
            "userId": "user-id-001",
            "displayName": "Test User",
            "email": "user@example.com",
        },
        {
            "id": "member-002",
            "userId": "user-id-002",
            "displayName": "Alice Smith",
            "email": "alice@example.com",
        },
    ]
}

SAMPLE_CHAT_MESSAGE_FULL = {
    "id": "chat-msg-001",
    "messageType": "message",
    "createdDateTime": "2026-01-05T14:00:00Z",
    "lastModifiedDateTime": "2026-01-05T14:05:00Z",
    "from": {"user": {"id": "user-id-002", "displayName": "Alice Smith"}, "application": None},
    "body": {"contentType": "html", "content": "<p>Sounds good!</p>"},
}

SAMPLE_CHAT_MESSAGE_FROM_APP = {
    "id": "chat-msg-002",
    "messageType": "message",
    "createdDateTime": "2026-01-05T13:00:00Z",
    "lastModifiedDateTime": "2026-01-05T13:00:00Z",
    "from": {"user": None, "application": {"id": "app-id-001", "displayName": "Power Automate"}},
    "body": {"contentType": "text", "content": "Build finished"},
}

# System events (member added, chat renamed) have no sender at all.
SAMPLE_CHAT_MESSAGE_SYSTEM = {
    "id": "chat-msg-003",
    "messageType": "systemEventMessage",
    "createdDateTime": "2026-01-05T12:00:00Z",
    "lastModifiedDateTime": "2026-01-05T12:00:00Z",
    "from": None,
    "body": None,
}

SAMPLE_CHAT_MESSAGES_PAGE = {
    "value": [
        SAMPLE_CHAT_MESSAGE_FULL,
        SAMPLE_CHAT_MESSAGE_FROM_APP,
        SAMPLE_CHAT_MESSAGE_SYSTEM,
    ]
}


# ---------------------------------------------------------------------------
# Attachments (mail attachment metadata, upload sessions, sink payloads)
# ---------------------------------------------------------------------------

SAMPLE_FILE_ATTACHMENT = {
    "@odata.type": "#microsoft.graph.fileAttachment",
    "id": "AAMkAttachFile001=",
    "name": "report.pdf",
    "contentType": "application/pdf",
    "size": 1_258_291,
    "isInline": False,
    "contentId": None,
    "lastModifiedDateTime": "2025-12-15T10:30:00Z",
}

SAMPLE_INLINE_ATTACHMENT = {
    "@odata.type": "#microsoft.graph.fileAttachment",
    "id": "AAMkAttachInline002=",
    "name": "logo.png",
    "contentType": "image/png",
    "size": 4096,
    "isInline": True,
    "contentId": "logo@company",
    "lastModifiedDateTime": "2025-12-15T10:30:00Z",
}

SAMPLE_ITEM_ATTACHMENT = {
    "@odata.type": "#microsoft.graph.itemAttachment",
    "id": "AAMkAttachItem003=",
    "name": "FW: Budget",
    "contentType": None,
    "size": 32_768,
    "isInline": False,
    "lastModifiedDateTime": "2025-12-15T10:30:00Z",
}

SAMPLE_REFERENCE_ATTACHMENT = {
    "@odata.type": "#microsoft.graph.referenceAttachment",
    "id": "AAMkAttachRef004=",
    "name": "Q4 Plan.docx",
    "contentType": None,
    "size": 0,
    "isInline": False,
    "sourceUrl": "https://contoso.sharepoint.com/:w:/s/team/Q4Plan",
}

SAMPLE_ATTACHMENTS_RESPONSE = {
    "value": [SAMPLE_FILE_ATTACHMENT, SAMPLE_INLINE_ATTACHMENT, SAMPLE_REFERENCE_ATTACHMENT]
}

# get_mail_detail $expands attachments, so one request returns body, headers,
# and this list together.
SAMPLE_MESSAGE_DETAIL = {
    "id": SAMPLE_MESSAGE["id"],
    "hasAttachments": True,
    "uniqueBody": {
        "contentType": "text",
        "content": "Here is the weekly report.\n\nBest,\nAlice",
    },
    "internetMessageHeaders": [
        {"name": "Message-ID", "value": "<abc123@example.com>"},
        {"name": "In-Reply-To", "value": "<parent@example.com>"},
        {"name": "Received", "value": "from mx1.example.com"},
        # Duplicate header — the first occurrence is the one that wins.
        {"name": "received", "value": "from mx2.example.com"},
    ],
    "attachments": [
        SAMPLE_FILE_ATTACHMENT,
        SAMPLE_INLINE_ATTACHMENT,
        SAMPLE_REFERENCE_ATTACHMENT,
    ],
}

SAMPLE_ATTACHMENTS_NEXT_LINK = (
    "https://graph.microsoft.com/v1.0/me/messages/AAMkAGI2TG93AAA%3D/attachments"
    "?$skiptoken=attach%2Bskip"
)

SAMPLE_ATTACHMENTS_PAGE_NEXT = {
    "value": [SAMPLE_FILE_ATTACHMENT],
    "@odata.nextLink": SAMPLE_ATTACHMENTS_NEXT_LINK,
}

SAMPLE_ATTACHMENTS_PAGE_FINAL = {"value": [SAMPLE_REFERENCE_ATTACHMENT]}

SAMPLE_CREATED_ATTACHMENT = {
    "@odata.type": "#microsoft.graph.fileAttachment",
    "id": "AAMkAttachNew005=",
    "name": "notes.txt",
    "contentType": "text/plain",
    "size": 11,
}

# Outlook attachment upload sessions live on outlook.office.com and are
# pre-authenticated; OneDrive sessions live on an up.*.1drv.com host.
SAMPLE_ATTACHMENT_UPLOAD_URL = (
    "https://outlook.office.com/api/v2.0/Users('user-id-001')/Messages('msg-1')"
    "/AttachmentSessions('sess-1')?authtoken=abc"
)

SAMPLE_ATTACHMENT_UPLOAD_SESSION = {
    "uploadUrl": SAMPLE_ATTACHMENT_UPLOAD_URL,
    "expirationDateTime": "2025-12-16T10:30:00Z",
    "nextExpectedRanges": ["0"],
}

SAMPLE_ATTACHMENT_LOCATION = (
    "https://outlook.office.com/api/v2.0/Users('user-id-001')/Messages('msg-1')"
    "/Attachments('AAMkAttachBig006%3D')"
)

SAMPLE_DRIVE_UPLOAD_URL = "https://sn3302.up.1drv.com/up/abcdef0123456789"

SAMPLE_DRIVE_UPLOAD_SESSION = {
    "uploadUrl": SAMPLE_DRIVE_UPLOAD_URL,
    "expirationDateTime": "2025-12-16T10:30:00Z",
}

SAMPLE_DRAFT_MESSAGE = {
    "id": "AAMkAGI2draft777=",
    "isDraft": True,
    "subject": "Hello",
    "webLink": "https://outlook.office.com/mail/deeplink/AAMkAGI2draft777",
}
