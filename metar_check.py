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

# --- TRACKING API: FLUGDATEN ABRUFEN (AeroDataBox) ---
def fetch_flight_metadata(flight_number, flight_date, api_key):
    """Holt DEP, DEST, Tailsign und Delays basierend auf Flugnummer und Datum"""
    flight_clean = flight_number.replace(" ", "").upper()
    date_str = flight_date.strftime("%Y-%m-%d")
    
    url = f"https://aerodatabox.p.rapidapi.com/flights/number/{flight_clean}/{date_str}"
    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200 and len(res.json()) > 0:
            flight_data = res.json()[0] 
            
            dep = flight_data.get("departure", {}).get("airport", {}).get("icao")
            dest = flight_data.get("arrival", {}).get("airport", {}).get("icao")
            tail = flight_data.get("aircraft", {}).get("reg", "UNKNOWN TAIL")
            
            dep_delay = flight_data.get("departure", {}).get("delayMinutes", 0)
            arr_delay = flight_data.get("arrival", {}).get("delayMinutes", 0)
            
            if dep and dest:
                return {
                    "success": True,
                    "dep": dep,
                    "dest": dest,
                    "tail": tail,
                    "delay": f"DEP Delay: {dep_delay}min / ARR Delay: {arr_delay}min" if (dep_delay or arr_delay) else "On Time"
                }
            else:
                return {"success": False, "error": f"API lieferte Daten, aber keine ICAO-Codes für DEP/DEST."}
        else:
            return {"success": False, "error": f"HTTP Code {res.status_code}: Flug für {date_str} nicht gefunden."}
    except Exception as e:
        return {"success": False, "error": f"Verbindungsfehler zur API: {str(e)}"}

# --- DATEN SAMMLER (Wetter, NOTAMs, Runways) ---
def fetch_raw_data(icao_code, label, all_runways):
    metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json"
    taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json"
    raw_text = f"\n--- {label}: {icao_code.upper()} ---\n"
    
    try:
        metar_res = requests.get(metar_url)
        metar_data = metar_res.json()
        if len(metar_data) > 0:
            raw_text += f"ORIGINAL_METAR: {metar_data[0].get('rawOb')}\n"
            wdir = metar_data[0].get("wdir")
            wspd = metar_data[0].get("wspd")
            if wdir and wspd and isinstance(wdir, (int, float)):
                raw_text += f"WIND DATA: {wdir} degrees at {wspd} knots.\n"
        else:
            raw_text += "ORIGINAL_METAR: None available.\n"

        taf_res = requests.get(taf_url)
        taf_data = taf_res.json()
        if len(taf_data) > 0:
            raw_text += f"ORIGINAL_TAF: {taf_data[0].get('rawTAF')}\n"
        else:
            raw_text += "ORIGINAL_TAF: None published.\n"
            
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0"}
        session.get("https://notams.aim.faa.gov/notamSearch/", headers=headers, timeout=5)
        faa_url = "https://notams.aim.faa.gov/notamSearch/search"
        notam_res = session.post(faa_url, data={"searchType": 0, "designatorsForLocation": icao_code.upper()}, headers=headers, timeout=10)
        
        if notam_res.status_code == 200:
            notam_data = notam_res.json()
            if "notamList" in notam_data and len(notam_data["notamList"]) > 0:
                raw_text += "NOTAMS:\n"
                for notam in notam_data["notamList"]:
                    raw_text += f"- {notam.get('icaoMessage', notam.get('traditionalMessage', ''))}\n"
            else:
                raw_text += "NOTAMS: None active.\n"
                
    except Exception as e:
        raw_text += f"Fehler beim Datenabruf für {icao_code}: {e}\n"
        
    return raw_text

# --- KI BRIEFING GENERATOR ---
def generate_ai_briefing(raw_data, flight_meta, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = f"""
    Du bist 'Dispatch-AI', ein professioneller, präziser Flight Dispatch Assistant für Lufthansa-Piloten.
    Deine Aufgabe ist es, aus den folgenden Rohdaten ein extrem klares, operationell sinnvolles Executive Pre-Flight Briefing auf Deutsch zu erstellen.
    
    Flug-Metadaten:
    - Flugnummer: {flight_meta.get('fn', 'UNKNOWN')}
    - Geplantes Flugzeug (Tail): {flight_meta.get('tail', 'UNKNOWN')}
    - Status/Verspätung: {flight_meta.get('delay', 'No Data')}
    
    Regeln für die Struktur des Briefings:
    1. Beginne mit einer kurzen 'Executive Summary' zum Flug (Flugnummer, Flugzeug-Registrierung und eventuelle Verspätungen).
    2. Erstelle für jeden Flughafen (DEP, DEST und alle ALTN) eine eigene Sektion. 
       WICHTIGSTE REGEL FÜR JEDEN FLUGHAFEN:
       - Blende als ALLERERSTES das originale, unveränderte 'ORIGINAL_METAR' und 'ORIGINAL_TAF' exakt so wie es im Text steht in einem Markdown-Code-Block (```text ... ```) ein.
       - Schreibe DIREKT ERST DARUNTER deine verständliche Übersetzung und Interpretation für diesen Platz.
       - Hebe meteorologische Gefahren (CB, TS, Icing, starke Crosswinds) deutlich hervor.
    3. Filtere die NOTAMs kritisch. Konzentriere dich auf geschlossene Bahnen, fehlende ILS/Navaids, Taxiway-Sperrungen und operationelle Einschränkungen.
    4. Erstelle am Ende einen Abschnitt "Threat & Error Management (TEM) / Operationelles Takeaway" mit den 2-3 größten Herausforderungen für die Crew.
    
    Hier sind die aktuellen Rohdaten des Fluges:
    {raw_data}
    """
    with st.spinner('🧠 Dispatch-AI analysiert Flugdaten und schreibt Briefing...'):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"❌ Fehler bei der KI-Generierung. Details: {e}"

# --- STREAMLIT UI OBERFLÄCHE ---
st.set_page_config(page_title="Dispatch-AI", page_icon="✈️", layout="wide")

st.title("✈️ Dispatch-AI")
st.subheader("Professional AI-Powered Pre-Flight Briefing")

gemini_key = st.secrets.get("GEMINI_API_KEY")
rapid_key = st.secrets.get("RAPIDAPI_KEY")

if not gemini_key:
    st.error("🔒 Bitte hinterlege den GEMINI_API_KEY in den Streamlit Secrets.")
    st.stop()

# Spalten für Flugnummer UND Datum
col_fn, col_date = st.columns(2)
with col_fn:
    flight_input = st.text_input("Flugnummer (z.B. LH94):", placeholder="LH94").upper()
with col_date:
    flight_date = st.date_input("Flugdatum:", datetime.now().date())

with st.expander("➕ Optionale Alternates hinzufügen (bis zu 4 Plätze)", expanded=False):
    alt_col1, alt_col2, alt_col3, alt_col4 = st.columns(4)
    with alt_col1:
        altn1 = st.text_input("ALTN 1:", max_chars=4, placeholder="z.B. EDDS", key="a1").upper()
    with alt_col2:
        altn2 = st.text_input("ALTN 2:", max_chars=4, placeholder="z.B. EDDM", key="a2").upper()
    with alt_col3:
        altn3 = st.text_input("ALTN 3:", max_chars=4, placeholder="z.B. LFPG", key="a3").upper()
    with alt_col4:
        altn4 = st.text_input("ALTN 4:", max_chars=4, placeholder="z.B. EGLL", key="a4").upper()

if st.button("Executive Briefing erstellen"):
    if flight_input:
        flight_meta = {"fn": flight_input, "tail": "UNKNOWN", "delay": "On Time", "dep": None, "dest": None}
        
        if rapid_key:
            st.info(f"🔍 Tracke Flug {flight_input} für den {flight_date.strftime('%d.%m.%Y')} via AeroDataBox...")
            track_res = fetch_flight_metadata(flight_input, flight_date, rapid_key)
            
            if track_res["success"]:
                flight_meta["dep"] = track_res["dep"]
                flight_meta["dest"] = track_res["dest"]
                flight_meta["tail"] = track_res["tail"]
                flight_meta["delay"] = track_res["delay"]
                st.success(f"✈️ Flug gefunden: {flight_meta['dep']} ➡️ {flight_meta['dest']} | Aircraft: {flight_meta['tail']}")
            else:
                st.warning(f"⚠️ Tracking fehlgeschlagen: {track_res['error']}")
        
        if not flight_meta["dep"] or not flight_meta["dest"]:
            st.error("Konnte Route nicht automatisch bestimmen.")
            manual_dep = st.text_input("Manueller DEP (ICAO):", max_chars=4, key="m_dep").upper()
            manual_dest = st.text_input("Manueller DEST (ICAO):", max_chars=4, key="m_dest").upper()
            if manual_dep and manual_dest:
                flight_meta["dep"] = manual_dep
                flight_meta["dest"] = manual_dest
            else:
                st.stop()

        st.info("📡 Sammle meteorologische Daten und NOTAMs...")
        all_runways = load_runway_database()
        
        combined_raw_data = fetch_raw_data(flight_meta["dep"], "DEPARTURE (DEP)", all_runways)
        combined_raw_data += fetch_raw_data(flight_meta["dest"], "DESTINATION (DEST)", all_runways)
        
        for idx, altn in enumerate([altn1, altn2, altn3, altn4], start=1):
            if altn:
                combined_raw_data += fetch_raw_data(altn, f"ALTERNATE {idx} (ALTN)", all_runways)
                
        briefing_output = generate_ai_briefing(combined_raw_data, flight_meta, gemini_key)
        
        # --- NEU: TABS FÜR KI UND ROHDATEN ---
        st.markdown("---")
        tab1, tab2 = st.tabs(["🤖 AI Executive Briefing", "📡 API Rohdaten"])
        
        with tab1:
            # Hier landet das formatierte KI-Ergebnis
            st.markdown(briefing_output)
            
        with tab2:
            # Hier landen die ungefilterten Metadaten und der komplette Text-Dump der APIs
            st.info("Dieser Bereich zeigt die ungefilterten Daten aller angebundenen APIs (AviationWeather, FAA NOTAMs, AeroDataBox). Dies ist exakt das Datenpaket, welches die KI zur Analyse erhält.")
            
            st.markdown("### ✈️ Flug & Tracking Metadaten")
            st.json(flight_meta)
            
            st.markdown("### 🌤️ Wetter & 📋 NOTAMs")
            st.code(combined_raw_data, language="text")

    else:
        st.warning("Bitte gib eine Flugnummer ein.")
