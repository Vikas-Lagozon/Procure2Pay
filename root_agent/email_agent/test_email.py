# test_gmail.py
import os
from config import config
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = config.ALL_OAUTH_SCOPES

def get_gmail_service():
    creds = None
    if os.path.exists(config.GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(config.GMAIL_TOKEN_FILE, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(config.GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    
    return build("gmail", "v1", credentials=creds)

if __name__ == "__main__":
    service = get_gmail_service()
    print("✅ Gmail service connected successfully!")
    # Example: list labels
    results = service.users().labels().list(userId="me").execute()
    labels = results.get("labels", [])
    print(f"Found {len(labels)} labels.")
