# File server.py
# Referred - https://cloud.google.com/blog/topics/developers-practitioners/use-google-adk-and-mcp-with-an-external-server

import requests
import base64
from requests.exceptions import RequestException

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route, Mount

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS
from mcp.server.sse import SseServerTransport


# GMAIL
import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from typing import List


import sqlite3
import datetime

# DB_PATH = "tokens.db"
DB_PATH = r"D:\code_folder\python_scripts\tokens.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_tokens (
        user_id TEXT PRIMARY KEY,
        token TEXT,
        refresh_token TEXT,
        token_uri TEXT,
        client_id TEXT,
        client_secret TEXT,
        scopes TEXT,
        expiry TEXT
    )
    """)
    conn.commit()
    conn.close()

# Create an MCP server instance with an identifier ("wiki")
init_db()
mcp = FastMCP("google_tools")

sample_app = FastAPI(title="MCP Server Example")



# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def save_credentials(user_id: str, creds: Credentials):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO user_tokens
        (user_id, token, refresh_token, token_uri, client_id, client_secret, scopes, expiry)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        creds.token,
        creds.refresh_token,
        creds.token_uri,
        creds.client_id,
        creds.client_secret,
        " ".join(creds.scopes),
        creds.expiry.isoformat() if creds.expiry else None
    ))
    conn.commit()
    conn.close()

def load_credentials(user_id: str) -> Credentials | None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_tokens WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return Credentials(
            token=row[1],
            refresh_token=row[2],
            token_uri=row[3],
            client_id=row[4],
            client_secret=row[5],
            scopes=row[6].split(),
            expiry=datetime.datetime.fromisoformat(row[7]) if row[7] else None
        )
    return None

@sample_app.get("/login")
def authenticate() -> str:
    """
    Authenticates the user's gmail account using the GMAIL api client, and stores the token to be used for further GMAIL API usage.
    This will pop up a new browser window for the user to login and give consent to access their GMAIL via this app.
    Returns: 
        user_id: If the user authenticates successfully, it return users email / user_id

    Usage:
        authenticate()
    """
    
    creds = None
    flow = InstalledAppFlow.from_client_secrets_file(
            "credentials.json", SCOPES
        )
    creds = flow.run_local_server(port=0)

    # Get user email from gmail api
    gmail = build('gmail', 'v1', credentials=creds)
    profile = gmail.users().getProfile(userId='me').execute()

    print("Email:", profile['emailAddress'])
    # save credentials
    save_credentials(profile['emailAddress'], creds)
    return profile['emailAddress']


@mcp.tool()
def get_gmail_labels(user_id: str) -> List[str]:
    """
    Retrieves Gmail Labels from the user's gmail account based on the given user_id / user email, using the GMAIL api client.

    Returns: 
        List[str]: Containing list of labels found in the user's gmail
        or Exception: If probably user needs to reauthenticate using the `authenticate` method. 
    Usage:
        get_gmail_labels('xyz_user@abc.com')
    """

    try:
        # Check and load credentials
        creds = load_credentials(user_id)
        if not creds or not creds.valid or creds.expired:
            raise Exception("User not authenticated.. Please authenticate again...using this login link: `http://localhost:8001/sample_app/login`")

        # Call the Gmail API
        service = build("gmail", "v1", credentials=creds)
        results = service.users().labels().list(userId="me").execute()
        labels = results.get("labels", [])

        if not labels:
            print("No labels found.")
            return
        print("Labels:")
        res = []
        for label in labels:
            print(label["name"])
            res.append(label["name"])

        return res

    except Exception as e:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred: {str(e)}")) from e

def get_message_body(msg_payload):
    """Extract plain text body from Gmail message payload."""
    if 'parts' in msg_payload:
        for part in msg_payload['parts']:
            if part['mimeType'] == 'text/plain':
                data = part['body'].get('data')
                if data:
                    return base64.urlsafe_b64decode(data).decode('utf-8',errors='ignore')
    else:
        # Single-part message
        data = msg_payload['body'].get('data')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8',errors='ignore')
    return "(No plain text body found)"

@mcp.tool()
def get_emails(user_id: str, label_name: str, since_days: int) -> List[dict]:
    """
    Retrieves Gmail messages from the user's gmail account based on the given user_id / user email, , using the GMAIL api client.
    Params:
        user_id: user email
        label_name: label name to filter like INBOX, CATEGORY_UPDATES ETC..
        since_days: value to be used in the query to filter emails newer than the given number of days
    Returns: 
        List[dict]: Containing list of dict containing "subject" and "message_data" from the filtered user emails for the given user.
        or Exception: If probably user needs to reauthenticate using the `authenticate` method. 
    Usage:
        get_emails('xyz_user@abc.com', 'INBOX', 2)
    """
    # Check and load credentials
    creds = load_credentials(user_id)
    if not creds or not creds.valid or creds.expired:
        raise Exception("User not authenticated.. Please authenticate again...using this login link: `http://localhost:8001/sample_app/login`")
    # Gmail client
    service = build('gmail', 'v1', credentials=creds)
    query = f"newer_than:{since_days}d label:{label_name}"
    page_token = None
    all_messages = []

    while True:
        results = service.users().messages().list(
            userId='me',
            q=query,
            pageToken=page_token,
            maxResults=10  # adjust as needed
        ).execute()

        messages = results.get('messages', [])
        all_messages.extend(messages)

        page_token = results.get('nextPageToken')
        if not page_token:
            break
    
    res_messages = []
    print(f"Found {len(all_messages)} messages in last {since_days} days with label {label_name}:")
    for msg in all_messages:
        msg_data = service.users().messages().get(userId='me', id=msg['id']).execute()
        headers = msg_data['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "(No Subject)")
        sender = next((h['value'] for h in headers if h['name'] == 'From'), "(No Sender)")
        body = get_message_body(msg_data['payload'])
        print("="*60)
        print(f"From: {sender}\nSubject: {subject}\n\nBody:\n{body}\n")
        res_messages.append({'subject': subject, 'message_data': body})
    
    return res_messages

sse = SseServerTransport("/messages/")

async def handle_sse(request: Request) -> None:
    _server = mcp._mcp_server
    async with sse.connect_sse(
        request.scope,
        request.receive,
        request._send,
    ) as (reader, writer):
        await _server.run(reader, writer, _server.create_initialization_options())

app = Starlette(
    debug=True,
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
        Mount("/sample_app/", app=sample_app)
    ],
)

if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8001)