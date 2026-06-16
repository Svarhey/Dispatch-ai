import requests
import streamlit as st
import math
import csv
import google.generativeai as genai
from io import StringIO
from datetime import datetime, timezone, timedelta

# --- RUNWAY DATENBANK ---
@st.cache_data(ttl=86400)
def load_runway_database():
    url = "https://davidmegginson.github.io/ourairports-data/runways.csv"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        reader = csv.DictReader(StringIO(response.text))
        return [row for row in reader if row['closed'] == '0']
    except:
        return []

# --- TRACKING API: FLUGDATEN ---
def fetch_flight_metadata(flight_number, flight_date, api_key):
    flight_clean = flight_number.replace(" ", "").upper()
    date_str = flight_date.strftime("%Y-%m-%d")
    url = f"https://aerodatabox.p.rapidapi.com/flights/number/{flight_clean}/{date_str}"
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200 and len(res.json()) > 0:
            flight_data = res.json()[0] 
            return {
                "success": True, 
                "dep": flight_data.get("departure", {}).get("airport", {}).get("icao"),
                "dest": flight_data.get("arrival", {}).get("airport", {}).get("icao"),
                "tail": flight_data.get("aircraft", {}).get("reg", "N/A"),
                "delay": f"DEP: {flight_data.get('departure', {}).get('delayMinutes', 0)}min"
            }
        return {"success": False, "error": "Flug nicht gefunden."}
    except Exception as e:
        return {"success": False, "error": str(e)}

# --- DATEN SAMMLER (Getrennte Rückgabe) ---
def get_airport_raw_data(icao_code, label):
    """Gibt Wetter und NOTAMs getrennt zurück"""
    metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json"
    taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json"
    
    weather_info = f"--- {label}: {icao_code.upper()} ---\n"
    notam_info = f"--- {label}: {icao_code.upper()} ---\n"
    
    # 1. Wetter
    try:
        metar_res = requests.get(metar_url).json()
        taf_res = requests.get(taf_url).json()
        weather_info += f"METAR: {metar_res[0].get('rawOb') if metar_res else 'N/A'}\n"
        weather_info += f"TAF: {taf_res[0].get('rawTAF') if taf_res else 'N/A'}\n"
    except:
        weather_info += "Wetterdaten nicht verfügbar.\n"
        
    # 2. NOTAMs
    try:
        session = requests.Session()
        session.get("https://notams.aim.faa.gov/notamSearch/", headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        faa_url = "https://notams.aim.faa.gov/notamSearch/search"
        notam_res = session.post(faa_url, data={"searchType": 0, "designatorsForLocation": icao_code.upper()}).json()
        if "notamList" in notam_res:
            for n in notam_res["notamList"]:
                notam_info += f"- {n.get('icaoMessage', n.get('traditionalMessage', ''))}\n"
        else:
            notam_info += "Keine aktiven NOTAMs.\n"
    except:
        notam_info += "NOTAM-Daten nicht verfügbar.\n"
        
    return weather_info, notam_info

# --- KI BRIEFING GENERATOR ---
def generate_ai_briefing(weather_data, notam_data, flight_meta, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    prompt = f"Du bist Dispatch-AI. Analysiere folgende Wetterdaten:\n{weather_data}\nUnd diese NOTAMs:\n{notam_data}\nErstelle ein strukturiertes Briefing für Flug {flight_meta['fn']} (Tail: {flight_meta['tail']})."
    return model.generate_content(prompt).text

# --- STREAMLIT UI ---
st.set_page_config(page_title="Dispatch-AI", layout="wide")
st.title("✈️ Dispatch-AI")

# [Logik für Eingaben bleibt gleich wie zuvor...]
# (Ich habe den Code hier auf die wesentliche Logik gekürzt)

if st.button("Executive Briefing erstellen"):
    # ... Flugdaten abrufen ...
    # ... Wetter & NOTAMs abrufen ...
    weather_dump = ""
    notam_dump = ""
    for airport in [flight_meta['dep'], flight_meta['dest']]:
        w, n = get_airport_raw_data(airport, "AIRPORT")
        weather_dump += w + "\n"
        notam_dump += n + "\n"
    
    # ... KI Briefing aufrufen mit weather_dump + notam_dump ...
    
    tab1, tab2, tab3 = st.tabs(["🤖 AI Briefing", "🌤️ Wetter Rohdaten", "📋 NOTAM Rohdaten"])
    with tab1: st.markdown(briefing_output)
    with tab2: st.code(weather_dump)
    with tab3: st.code(notam_dump)
