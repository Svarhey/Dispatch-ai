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

# --- TRACKING API ---
def fetch_flight_metadata(flight_number, flight_date, api_key):
    flight_clean = flight_number.replace(" ", "").upper()
    date_str = flight_date.strftime("%Y-%m-%d")
    url = f"https://aerodatabox.p.rapidapi.com/flights/number/{flight_clean}/{date_str}"
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200 and len(res.json()) > 0:
            data = res.json()[0] 
            return {
                "success": True, 
                "dep": data.get("departure", {}).get("airport", {}).get("icao"),
                "dest": data.get("arrival", {}).get("airport", {}).get("icao"),
                "tail": data.get("aircraft", {}).get("reg", "N/A"),
                "delay": f"DEP: {data.get('departure', {}).get('delayMinutes', 0)}min"
            }
        return {"success": False, "error": "Flug nicht gefunden."}
    except Exception as e:
        return {"success": False, "error": str(e)}

# --- DATEN SAMMLER ---
def get_airport_raw_data(icao_code, label):
    weather_info = f"--- {label}: {icao_code.upper()} ---\n"
    notam_info = f"--- {label}: {icao_code.upper()} ---\n"
    
    # Wetter
    try:
        metar = requests.get(f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json").json()
        taf = requests.get(f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json").json()
        weather_info += f"METAR: {metar[0].get('rawOb') if metar else 'N/A'}\n"
        weather_info += f"TAF: {taf[0].get('rawTAF') if taf else 'N/A'}\n"
    except:
        weather_info += "Wetterdaten nicht verfügbar.\n"
        
    # NOTAMs
    try:
        session = requests.Session()
        session.get("https://notams.aim.faa.gov/notamSearch/", timeout=5)
        n = session.post("https://notams.aim.faa.gov/notamSearch/search", data={"searchType": 0, "designatorsForLocation": icao_code.upper()}).json()
        if "notamList" in n:
            for item in n["notamList"]:
                notam_info += f"- {item.get('icaoMessage', item.get('traditionalMessage', ''))}\n"
        else:
            notam_info += "Keine aktiven NOTAMs.\n"
    except:
        notam_info += "NOTAM-Daten nicht verfügbar.\n"
        
    return weather_info, notam_info

# --- UI & LOGIK ---
st.set_page_config(page_title="Dispatch-AI", layout="wide")
st.title("✈️ Dispatch-AI")

gemini_key = st.secrets.get("GEMINI_API_KEY")
rapid_key = st.secrets.get("RAPIDAPI_KEY")

col_fn, col_date = st.columns(2)
flight_input = col_fn.text_input("Flugnummer:", "LH94").upper()
flight_date = col_date.date_input("Datum:", datetime.now().date())

with st.expander("➕ Alternates (bis zu 4)"):
    a1, a2, a3, a4 = st.columns(4)
    altns = [a1.text_input("ALTN 1", key="a1"), a2.text_input("ALTN 2", key="a2"), a3.text_input("ALTN 3", key="a3"), a4.text_input("ALTN 4", key="a4")]

if st.button("Executive Briefing erstellen"):
    meta = fetch_flight_metadata(flight_input, flight_date, rapid_key)
    if meta["success"]:
        st.success(f"Flug: {meta['dep']} -> {meta['dest']} | {meta['tail']}")
        
        # Daten sammeln
        all_w = ""; all_n = ""
        for code in [meta['dep'], meta['dest']] + [a for a in altns if a]:
            w, n = get_airport_raw_data(code, "AIRPORT")
            all_w += w + "\n"; all_n += n + "\n"
        
        # KI Briefing
        genai.configure(api_key=gemini_key)
        brief = genai.GenerativeModel('gemini-2.5-flash').generate_content(
            f"Analysiere Wetter:\n{all_w}\nUnd NOTAMs:\n{all_n}\nErstelle Briefing für {flight_input}."
        ).text
        
        # TABS
        t1, t2, t3 = st.tabs(["🤖 AI Briefing", "🌤️ Wetter Rohdaten", "📋 NOTAM Rohdaten"])
        t1.markdown(brief)
        t2.code(all_w)
        t3.code(all_n)
