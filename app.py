import streamlit as st
import pandas as pd
import json
import os
import hashlib
import gpxpy
import folium
from streamlit_folium import st_folium
import random
import math
import numpy as np
from geopy.geocoders import Nominatim
import plotly.graph_objects as go
from rapidfuzz import fuzz
import datetime
import geopandas as gpd
from topojson import Topology
from shapely.geometry import LineString
from shapely.geometry import Point
from dropbox_handler import dropbox_load, dropbox_upload
import requests
import rasterio


# ==============================
# Simple local storage paths
# ==============================
DATA_DIR = "data"
GPX_DIR = os.path.join(DATA_DIR, "gpx").replace('\\', '/')
USERS_FILE = os.path.join(DATA_DIR, "users.json").replace('\\', '/')
ROUTES_FILE = os.path.join(DATA_DIR, "routes.json").replace('\\', '/')

os.makedirs(GPX_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# ==============================
# Utility: JSON storage
# ==============================

@st.cache_resource
def load_belgium_dem():
    local_tif_path = os.path.join(DATA_DIR, "belgium_elevation_30m.tif")
    dropbox_tif_path = "data/belgium_elevation_30m.tif" # The path in your Dropbox

    # 1. If the file isn't on the local (ephemeral) disk, get it from Dropbox
    if not os.path.exists(local_tif_path):
        with st.spinner("Downloading Elevation Model from Dropbox..."):
            try:
                # We assume dropbox_load returns the raw bytes for the .tif
                dem_bytes = dropbox_load_binary(dropbox_tif_path)
                
                # Write to the local data directory
                with open(local_tif_path, "wb") as f:
                    f.write(dem_bytes)
            except Exception as e:
                st.error(f"Failed to load DEM from Dropbox: {e}")
                return None

    # 2. Open the local file with rasterio
    return rasterio.open(local_tif_path)

def load_json(path, default):
    return dropbox_load(path)



def save_json(path, data):
    dropbox_upload(data, path)
# ==============================
# Load cities
# ==============================
def load_municipalities(topojson_path):
    with open(topojson_path) as f:
        topo = json.load(f)

    # your file contains this object
    topo = Topology(topo, object_name="municipalities")

    gdf = topo.to_gdf()

    # ensure correct coordinate system
    gdf = gdf.set_crs("EPSG:4326")

    return gdf

municipalities = load_municipalities("belgium.json")

# ==============================
# Authentication (LOCAL ONLY)
# ==============================

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def register_user(username, password):
    users = load_json(USERS_FILE, {})
    if username in users:
        return False
    users[username] = {"password": hash_pw(password)}
    save_json(USERS_FILE, users)
    return True


def login_user(username, password):
    users = load_json(USERS_FILE, {})
    if username not in users:
        return False
    return users[username]["password"] == hash_pw(password)

# ==============================
# Elevation profile
# ==============================

def elevation_profile(points_dict):
    df = pd.DataFrame(points_dict)

    distances = [0]
    total = 0
    for i in range(1, len(df)):
        lat1, lon1 = df.lat[i-1], df.lon[i-1]
        lat2, lon2 = df.lat[i], df.lon[i]
        total += math.sqrt((lat2-lat1)**2 + (lon2-lon1)**2) * 111000
        distances.append(total / 1000)

    df["dist_km"] = distances

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["dist_km"],
        y=df["ele"],
        mode="lines",
        fill="tozeroy"
    ))

    fig.update_layout(
        height=300,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis_title="Distance (km)",
        yaxis_title="Elevation (m)"
    )

    return fig
# ==============================
# Direction utilities
# ==============================

def bearing(lat1, lon1, lat2, lon2):
    φ1 = math.radians(lat1)
    φ2 = math.radians(lat2)
    Δλ = math.radians(lon2 - lon1)

    x = math.sin(Δλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(Δλ)

    θ = math.atan2(x, y)
    return (math.degrees(θ) + 360) % 360


def circular_mean(deg_list):
    if len(deg_list) == 0:
        return None
    radians = np.radians(deg_list)
    sin_sum = np.sum(np.sin(radians))
    cos_sum = np.sum(np.cos(radians))
    mean = math.degrees(math.atan2(sin_sum, cos_sum))
    return (mean + 360) % 360


def half_directions(points_dict):
    df = pd.DataFrame(points_dict)
    bearings = []

    for i in range(1, len(df)):
        b = bearing(df.lat[i-1], df.lon[i-1], df.lat[i], df.lon[i])
        bearings.append(b)

    mid = len(bearings) // 2
    first = circular_mean(bearings[:mid])
    second = circular_mean(bearings[mid:])

    return first, second


def deg_to_compass(d):
    dirs = ["N","NE","E","SE","S","SW","W","NW"]
    ix = round(d / 45) % 8
    return dirs[ix]


# ==============================
# City detection
# ==============================

def route_to_linestring(points_dict):

    coords = list(zip(points_dict["lon"], points_dict["lat"]))

    return LineString(coords)

def detect_cities(points_dict, municipalities_gdf):

    route_line = route_to_linestring(points_dict)

    # find municipalities intersecting route
    matches = municipalities_gdf[municipalities_gdf.intersects(route_line)]

    cities = []

    # detect start city
    start_point = Point(points_dict["lon"][0], points_dict["lat"][0])
    start_match = municipalities_gdf[municipalities_gdf.contains(start_point)]

    start_city = None
    if not start_match.empty:
        start_city = start_match.iloc[0]["name_nl"]

    # collect all cities crossed
    for _, row in matches.iterrows():
        cities.append(row["name_nl"])

    cities = sorted(set(cities))

    return start_city, cities


# ==============================
# GPX parsing
# ==============================

def parse_gpx(file):
    gpx = gpxpy.parse(file)
    dem = load_belgium_dem()
    
    points = []
    total_distance = 0
    elevation_gain = 0
    
    # 3.0m threshold helps match Hammerhead/Garmin "Real Feel" 
    # and ignores the 30m DEM grid noise
    ASCENT_THRESHOLD = 5.0 

    for track in gpx.tracks:
        for segment in track.segments:
            segment.simplify(10) 
            # (Uncomment above to further smooth noisy GPS files)
            
            prev = None
            checkpoint_ele = None
            
            for p in segment.points:
                # 1. If elevation is missing, sample from our TIF
                if p.elevation is None and dem is not None:
                    coords = [(p.longitude, p.latitude)]
                    for val in dem.sample(coords):
                        p.elevation = float(val[0])
                
                # 2. Basic Distance
                if prev:
                    total_distance += p.distance_3d(prev)
                
                # 3. Realistic Elevation Gain Logic
                if p.elevation is not None:
                    if checkpoint_ele is None:
                        checkpoint_ele = p.elevation
                    
                    gain = p.elevation - checkpoint_ele
                    if gain >= ASCENT_THRESHOLD:
                        elevation_gain += gain
                        checkpoint_ele = p.elevation
                    elif p.elevation < checkpoint_ele:
                        # Reset checkpoint if we go lower, to catch the next climb
                        checkpoint_ele = p.elevation

                points.append((p.latitude, p.longitude, p.elevation))
                prev = p

    df = pd.DataFrame(points, columns=["lat", "lon", "ele"])

    return {
        "distance_km": total_distance / 1000,
        "elevation_m": elevation_gain,
        "points": df.to_dict(orient="list"),
        "gpx_object": gpx # Return the object so we can save the modified XML
    }


# ==============================
# Route storage
# ==============================

def load_routes():
    return load_json(ROUTES_FILE, [])


def save_routes(routes):
    save_json(ROUTES_FILE, routes)


# ==============================
# Map rendering
# ==============================

def route_map(points_dict, height=250, key=None):
    df = pd.DataFrame(points_dict)
    m = folium.Map(location=[df.lat.mean(), df.lon.mean()], zoom_start=10)
    folium.PolyLine(df[["lat", "lon"]].values.tolist(), weight=4).add_to(m)
    return st_folium(m, height=height, width=None, key=key)


# ==============================
# Session state init
# ==============================
if "user" not in st.session_state:
    st.session_state.user = None

if "selected_route" not in st.session_state:
    st.session_state.selected_route = None


# ==============================
# Login / Register UI
# ==============================
if not st.session_state.user:
    st.title("🚴 Cycling Routes Hub")

    tab1, tab2 = st.tabs(["Login", "Register"])

    with tab1:
        u = st.text_input("Username", key="login_user")
        p = st.text_input("Password", type="password", key="login_pw")
        if st.button("Login"):
            if login_user(u, p):
                st.session_state.user = u
                st.rerun()
            else:
                st.error("Invalid credentials")

    with tab2:
        u = st.text_input("Username", key="reg_user")
        p = st.text_input("Password", type="password", key="reg_pw")
        if st.button("Register"):
            if register_user(u, p):
                st.success("User created")
            else:
                st.error("User exists")

    st.stop()


# ==============================
# Main app
# ==============================

st.sidebar.write(f"👤 {st.session_state.user}")
if st.sidebar.button("Logout"):
    st.session_state.user = None
    st.rerun()

page = st.sidebar.radio("Navigate", ["Library", "Upload"])
routes = load_routes()


# ==============================
# Upload page
# ==============================
if page == "Upload":
    st.title("Upload GPX")

    file = st.file_uploader("GPX file", type=["gpx"])
    name = st.text_input("Route name")
    tags = st.text_input("Tags (comma separated)")

    if st.button("Save") and file:
        meta = parse_gpx(file)
        dir_out, dir_back = half_directions(meta["points"])
        start_city, cities = detect_cities(meta["points"], municipalities)

        route_id = len(routes) + 1
        filename = f"route_{route_id}.gpx"
        # The 'path' here is the virtual path inside your Dropbox
        path = f"{GPX_DIR}/{filename}" 

        # --- DROPBOX UPLOAD INSTEAD OF LOCAL WRITE ---
        # We save the enriched XML string generated by gpxpy
        dropbox_upload(meta["gpx_object"].to_xml(), path)

        routes.append({
            "id": route_id,
            "name": name or file.name,
            "tags": tags,
            "distance_km": meta["distance_km"],
            "elevation_m": meta["elevation_m"],
            "points": meta["points"],
            "dir_out": dir_out,
            "dir_back": dir_back,
            "start_city": start_city,
            "cities": cities,
            "filename": filename,
            "added_by": st.session_state.user,
        })

        save_routes(routes)
        st.success("Route saved with elevation data!")


# ==============================
# Library page
# ==============================
if page == "Library":
    st.title("Route Library")

    search = st.text_input("Search (name, tags, cities)")
    min_km, max_km = st.slider("Distance", 0, 200, (0, 200))
    min_ele, max_ele = st.slider("Elevation", 0, 2000, (0, 2000))
    bearing_filter = st.selectbox("Filter by bearing (inbound)", ["All","N","NE","E","SE","S","SW","W","NW"])

    filtered = []

    for r in routes:
        searchable_text = " ".join([
            r.get("name", ""),
            r.get("tags", ""),
            r.get("start_city", ""),
            " ".join(r.get("cities", []))
        ]).lower()

        if search.strip():
            score = fuzz.token_set_ratio(search.lower(), searchable_text)
            matches_search = score > 60
        else:
            matches_search = True

        matches_distance = min_km <= r["distance_km"] <= max_km
        matches_elevation = min_ele <= r["elevation_m"] <= max_ele

        matches_bearing = True
        if bearing_filter != "All" and r.get("dir_back"):
            matches_bearing = deg_to_compass(r["dir_back"]) == bearing_filter

        if matches_search and matches_distance and matches_bearing and matches_elevation:
            filtered.append(r)

    # Keep previous natural ordering (by id)
    filtered = sorted(filtered, key=lambda x: x["id"])

    for r in filtered:
        with st.container():
            cols = st.columns([1, 2])

            with cols[0]:
                route_map(r["points"], key=f"preview_{r['id']}")

            with cols[1]:
                st.subheader(r["name"])
                st.write(f"📏 {r['distance_km']:.1f} km")
                st.write(f"⛰️ {r['elevation_m']:.0f} m")
                if r.get("dir_out"):
                    st.write(f"🧭 {deg_to_compass(r['dir_out'])} → {deg_to_compass(r['dir_back'])}")
                st.write(f"📍 Start: {r.get('start_city')}")
                st.write(f"🏙️ Cities: {', '.join(r.get('cities', []))}")

                ratings = r.get("ratings",{})
                if ratings:
                    avg = sum(ratings.values()) / len(ratings)
                    st.write(f"⭐ {avg:.1f} ({len(ratings)} ratings)")

                st.markdown(
    f"""
    <div style="text-align: right; font-style: italic; color: #D3D3D3; font-size: 0.9em;">
        Added by {r['added_by']}
    </div>
    """, 
    unsafe_allow_html=True
)
                if st.button("View", key=f"view_{r['id']}"):
                    st.session_state.selected_route = r

    # ==============================
    # Detail View
    # ==============================
    if st.session_state.selected_route:
        r = st.session_state.selected_route
        st.divider()
        st.header(f"Route: {r['name']}")

        route_map(r["points"], height=500, key=f"detail_{r['id']}")

        st.subheader("Elevation Profile")
        st.plotly_chart(elevation_profile(r["points"]), use_container_width=True)

        # Ridden toggle
        if st.session_state.user not in r.get("ridden_by",[]):
            if st.button("Mark as ridden"):
                r.setdefault("ridden_by",[]).append(st.session_state.user)
                for i, route in enumerate(routes):
                    if route["id"] == r["id"]:
                        routes[i] = r  # Sync the change back to the list
                        break
                save_routes(routes)
                st.rerun()
        else:
            st.success("You have ridden this route")

        # Rating
        st.subheader("Your Rating")
        user_rating = r.get("ratings",{}).get(st.session_state.user,3)
        new_rating = st.slider("Rate this route",1,10,user_rating)
        if st.button("Save Rating"):
            r.setdefault("ratings",{})[st.session_state.user] = new_rating
            for i, route in enumerate(routes):
                if route["id"] == r["id"]:
                    routes[i] = r  # Sync the change back to the list
                    break
            save_routes(routes)
            st.success("Rating saved")

        # Comments
        st.subheader("Comments")
        for c in r.get("comments",[]):
            st.markdown(f"**{c['user']}** ({c['timestamp']}):  ")
            st.write(c['text'])
            st.divider()

        comment_text = st.text_area("Add comment (max 256 characters)", max_chars=256)
        if st.button("Post Comment"):
            if comment_text.strip():
                r.setdefault("comments",[]).append({
                    "user": st.session_state.user,
                    "text": comment_text.strip(),
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
                })
                for i, route in enumerate(routes):
                    if route["id"] == r["id"]:
                        routes[i] = r  # Sync the change back to the list
                        break
                save_routes(routes)
                st.rerun()

        # Download
        file_path = f"{GPX_DIR}/{r['filename']}"
        gpx_data = dropbox_load(file_path)

        if gpx_data:
            st.download_button(
                label="Download GPX",
                data=gpx_data,
                file_name=r["filename"],
                mime="application/gpx+xml",
            )
        else:
            st.error("Could not retrieve GPX file from Dropbox.")

        if st.button("Close"):
            st.session_state.selected_route = None
            st.rerun()




