import streamlit as st
import dropbox
import json
import requests

def refresh_access_token():
    """Requests a new access token using the refresh token."""
    url = "https://api.dropbox.com/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": st.secrets["dropbox"]["refresh_token"],
        "client_id": st.secrets["dropbox"]["app_key"],
        "client_secret": st.secrets["dropbox"]["app_secret"]
    }
    
    response = requests.post(url, data=data)
    
    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        st.error("Failed to refresh Dropbox access token.")
        return None

def authenticate_dropbox():
    """Authenticate Dropbox with a refreshed access token."""
    access_token = refresh_access_token()
    if access_token:
        return dropbox.Dropbox(access_token)
    return None

dbx = authenticate_dropbox()

# Upload teams.json to Dropbox
def dropbox_upload(file, json_name):
    global dbx
    try:
        """Uploads teams.json to Dropbox."""
        file_path = f"/Routes/{json_name}"
        json_data = json.dumps(file, indent=4)
    
        dbx.files_upload(json_data.encode(), file_path, mode=dropbox.files.WriteMode("overwrite"))

    except dropbox.exceptions.AuthError:
        st.warning("Dropbox authentication failed. Attempting token refresh...")
        dbx = authenticate_dropbox()
        if dbx:
            dropbox_upload(file, json_name)


# Load teams.json from Dropbox
def dropbox_load(json_name):
    global dbx
    try:
        """Retrieves teams.json from Dropbox."""
        file_path = f"/Routes/{json_name}"
    
        try:
            metadata, res = dbx.files_download(file_path)
            teams = json.loads(res.content.decode("utf-8"))
            return teams
        except dropbox.exceptions.ApiError:
            return {}  # Return an empty dictionary if file doesn't exist
    except dropbox.exceptions.AuthError:
        st.warning("Dropbox authentication failed. Attempting token refresh...")
        dbx = authenticate_dropbox()
        if dbx:
            dropbox_load(json_name)

