import os
import requests
import urllib.parse
from flask import Flask, redirect, request, session, url_for, render_template
import certifi
import boto3
import io
import csv
import datetime

application = Flask(__name__)
application.secret_key = os.urandom(24)  # needed for session

# Your S3 bucket name (set in env variable or fallback to default)
S3_BUCKET = os.getenv("S3_BUCKET", "spotify-stats-reports-123")

# Boto3 S3 client (will use IAM role attached to EB EC2 instance)
s3_client = boto3.client("s3")

# Spotify API credentials (from Spotify Developer Dashboard)
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

# Spotify endpoints
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"

# Scopes you want (adjust depending on your use case)
SCOPE = "user-read-recently-played user-top-read"

@application.route("/")
def index():
    return render_template("login.html")


@application.route("/login")
def login():
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "show_dialog": "true"   # 👈 forces re-login each time
    }
    url = f"{SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return redirect(url)


@application.route("/callback")
def callback():
    # Step 2. Spotify redirects back with authorization code
    code = request.args.get("code")
    if code is None:
        return "Authorization failed.", 400

    # Step 3. Exchange code for access token
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    token_response = requests.post(SPOTIFY_TOKEN_URL, data=token_data, verify=certifi.where())
    token_json = token_response.json()

    # Save tokens in session
    session["access_token"] = token_json.get("access_token")
    session["refresh_token"] = token_json.get("refresh_token")

    return redirect(url_for("stats"))


@application.route("/stats")
def stats():
    token = session.get("access_token")
    if not token:
        return redirect(url_for("login"))

    # Get chosen time range, default = medium_term
    time_range = request.args.get("time_range", "medium_term")

    headers = {"Authorization": f"Bearer {token}"}

    # Top tracks
    tracks_resp = requests.get(
        f"{SPOTIFY_API_BASE_URL}/me/top/tracks?limit=10&time_range={time_range}",
        headers=headers, verify=certifi.where()
    ).json()

    top_tracks = [
        {
            "name": t["name"],
            "artist": ", ".join([a["name"] for a in t["artists"]]),
            "image": t["album"]["images"][0]["url"] if t["album"]["images"] else None
        }
        for t in tracks_resp.get("items", [])
    ]

    # Top artists
    artists_resp = requests.get(
        f"{SPOTIFY_API_BASE_URL}/me/top/artists?limit=10&time_range={time_range}",
        headers=headers, verify=certifi.where()
    ).json()

    top_artists = [
        {
            "name": a["name"],
            "image": a["images"][0]["url"] if a["images"] else None
        }
        for a in artists_resp.get("items", [])
    ]

    # Top albums → derive from tracks’ album info
    albums = {}
    for t in tracks_resp.get("items", []):
        album = t["album"]
        album_id = album["id"]
        if album_id not in albums:
            albums[album_id] = {
                "name": album["name"],
                "image": album["images"][0]["url"] if album["images"] else None,
                "count": 0
            }
        albums[album_id]["count"] += 1

    top_albums = sorted(albums.values(), key=lambda x: x["count"], reverse=True)[:10]

    return render_template(
        "stats.html",
        tracks=top_tracks,
        artists=top_artists,
        albums=top_albums,
        current_time_range=time_range
    )


@application.route("/logout")
def logout():
    session.clear()   # remove access_token, refresh_token, etc.
    return redirect(url_for("index"))

API_GATEWAY_URL = "https://uvdf0v98lb.execute-api.us-west-2.amazonaws.com/userstats"

@application.route("/recently_played")
def recently_played():
    token = session.get("access_token")
    if not token:
        return redirect(url_for("login"))

    # Call Spotify API
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{SPOTIFY_API_BASE_URL}/me/player/recently-played?limit=50", headers=headers)

    if resp.status_code != 200:
        return f"Error fetching Spotify data: {resp.text}"

    raw_data = resp.json()

    # Extract artist, album, and played_at
    tracks = [{
        "artist": item["track"]["artists"][0]["name"],
        "album": item["track"]["album"]["name"],
        "played_at": item["played_at"]
    } for item in raw_data.get("items", [])]

    payload = {"user_id": "demo", "tracks": tracks}

    # Send raw data to Lambda
    lambda_resp = requests.post(API_GATEWAY_URL, json=payload)
    stats = lambda_resp.json()

    print("Lambda response:", stats, flush=True)

    # Render HTML page
    return render_template("recently_played.html", stats=stats)

@application.route("/export")
def export():
    token = session.get("access_token")
    if not token:
        return redirect(url_for("login"))

    # ✅ carry over the time_range from query string
    time_range = request.args.get("time_range", "medium_term")

    headers = {"Authorization": f"Bearer {token}"}

    # Top tracks
    tracks_resp = requests.get(
        f"{SPOTIFY_API_BASE_URL}/me/top/tracks?limit=20&time_range={time_range}",
        headers=headers
    ).json()

    # Top artists
    artists_resp = requests.get(
        f"{SPOTIFY_API_BASE_URL}/me/top/artists?limit=20&time_range={time_range}",
        headers=headers
    ).json()

    # Convert into CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Type", "Name", "Popularity"])

    for t in tracks_resp.get("items", []):
        writer.writerow(["Track", t["name"], t["popularity"]])

    for a in artists_resp.get("items", []):
        writer.writerow(["Artist", a["name"], a["popularity"]])

    csv_data = output.getvalue()

    # ✅ Save to S3
    s3_client = boto3.client("s3")
    filename = f"spotify_report_{time_range}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
    s3_client.put_object(Bucket=S3_BUCKET, Key=filename, Body=csv_data)

    # Generate presigned URL (valid 1h)
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": filename},
        ExpiresIn=3600
    )

    return redirect(url)

# @application.route("/global_top")
# def global_top():
#     token = session.get("access_token")
#     if not token:
#         return redirect(url_for("login"))
#
#     headers = {"Authorization": f"Bearer {token}"}
#
#     # Example: use the Spotify "Top 50 – Global" playlist
#     # Playlist ID: 37i9dQZEVXbMDoHDwVN2tF
#     playlist_id = "37i9dQZEVXbMDoHDwVN2tF"
#     playlist_resp = requests.get(
#         f"{SPOTIFY_API_BASE_URL}/playlists/{playlist_id}/tracks?limit=20&market=US",
#         headers=headers
#     ).json()
#
#     tracks = playlist_resp.get("items", [])
#
#     print("Spotify response:", playlist_resp, flush=True)
#
#     # Extract top songs, artists, albums
#     top_songs = [{"name": t["track"]["name"], "artist": t["track"]["artists"][0]["name"]} for t in tracks]
#     top_artists = list({t["track"]["artists"][0]["name"]: t for t in tracks}.keys())
#     albums = list({t["track"]["album"]["name"]: t for t in tracks}.keys())
#
#     return render_template(
#         "global_top.html",
#         tracks=top_songs,
#         artists=top_artists,
#         albums=albums
#     )

if __name__ == "__main__":
    application.run(debug=True)