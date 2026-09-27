import os
import pickle
import google.auth
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload


file_path = 'watchlist.csv'
folder_id = '1oVIHbx29Y4Cno3paPu7SzMV3LtUloH9a'

SCOPES = ['https://www.googleapis.com/auth/drive.file']

creds = None
if os.path.exists('token.pickle'):
    with open('token.pickle', 'rb') as token:
        creds = pickle.load(token)

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            'credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)

    with open('token.pickle', 'wb') as token:
        pickle.dump(creds, token)

drive_service = build('drive', 'v3', credentials=creds)

results = drive_service.files().list(
    pageSize=100, fields="files(id, name, parents, trashed, owners(emailAddress))").execute()
items = results.get('files', [])

if not items:
    print('No files found.')
else:
    print('Files:')
    for item in list(items):
        if item['name'] == "watchlist.csv":
            print(f"{item['name']} ({item['id']}): deleting...")
            drive_service.files().delete(fileId=item["id"]).execute()
            items.remove(item)
        else:
            print(f"{item['name']} ({item['id']}): untouched")

print(items)

file_metadata = {
    'name': os.path.basename(file_path),
    'parents': [folder_id]
}
media = MediaFileUpload(file_path, resumable=True)
uploaded_file = drive_service.files().create(
    body=file_metadata,
    media_body=media,
    fields='id'
).execute()

print(f"File uploaded with ID: {uploaded_file['id']}")