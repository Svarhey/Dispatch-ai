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

# --- DATEN SAMMLER ---
def fetch_raw_data(icao_code, label, all_runways):
    metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json"
    taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json"
    raw_text = f"\n--- {label}: {icao_code.upper()} ---\n"
    
    try:
        metar_res = requests.get(metar_url)
        metar_data = metar_res.json()
        if len(metar_data) > 0:
            raw_text += f"METAR: {metar_data[0].get('rawOb')}\n"
            wdir = metar_data[0].get("wdir")
            wspd = metar_data[0].get("wspd")
            if wdir and wspd and isinstance(wdir, (int, float)):
                raw_text += f"WIND DATA: {wdir} degrees at {wspd} knots.\n"
        else:
            raw_text += "METAR: None available.\n"

        taf_res = requests.get(taf_url)
        taf_data = taf_res.json()
        if len(taf_data) > 0:
            raw_text += f"TAF: {taf_data[0].get('rawTAF')}\n"
        else:
            raw_text += "TAF: None published.\n"
            
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
        raw_text += f"Fehler beim Datenabruf: {e}\n"
        
    return raw_text

# --- KI BRIEFING GENERATOR ---
def generate_ai_briefing(raw_data, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = f"""
    Du bist 'Dispatch-AI', ein professioneller, präziser Flight Dispatch Assistant für Piloten.
    Deine Aufgabe ist es, aus den folgenden kryptischen Rohdaten (METAR, TAF, NOTAMs) ein extrem klares, 
    strukturiertes und operationell sinnvolles Executive Pre-Flight Briefing auf Deutsch zu erstellen.
    
    Regeln:
    1. Fasse das Wetter verständlich zusammen. Hebe Gefahren (CB, TS, SN, FZRA, Windshear, starke Crosswinds) deutlich hervor.
    2. Filtere die NOTAMs. Lass unwichtige administrative Dinge weg. Konzentriere dich auf geschlossene Bahnen, fehlende ILS/Navaids, Taxiway-Sperrungen und operationelle Einschränkungen.
    3. Erstelle am Ende einen kurzen Abschnitt "Threat & Error Management (TEM) / Operationelles Takeaway", in dem du die 2-3 größten Herausforderungen des Fluges auf Basis der Daten zusammenfasst.
    4. Halluziniere keine Daten. Nutze nur das, was im Raw-Text steht. Wenn Daten fehlen, weise darauf hin.
    
    Hier sind die aktuellen Rohdaten des Fluges:
    {raw_data}
    """
    with st.spinner('🧠 Dispatch-AI analysiert Rohdaten und schreibt Briefing...'):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"❌ Fehler bei der KI-Generierung. Details: {e}"

# --- STREAMLIT UI OBERFLÄCHE ---
st.set_page_config(page_title="Dispatch-AI", page_icon="✈️", layout="wide")

st.title("✈️ Dispatch-AI")
st.subheader("Professional AI-Powered Pre-Flight Briefing")

# NEU: Der Tresor-Check
# Die App schaut nach, ob der Key sicher bei Streamlit in den Einstellungen hinterlegt wurde
if "GEMINI_API_KEY" in st.secrets:
    gemini_key = st.secrets["GEMINI_API_KEY"]
else:
    # Fallback: Falls kein Key im Tresor ist, wird das alte Seitenmenü angezeigt
    st.sidebar.header("⚙️ Systemeinstellungen")
    st.sidebar.warning("⚠️ Kein Key im Tresor gefunden.")
    gemini_key = st.sidebar.text_input("Gemini API Key:", type="password")

col1, col2, col3 = st.columns(3)
with col1:
    dep_icao = st.text_input("Departure (DEP) *Pflichtfeld*:", max_chars=4, placeholder="z.B. EDDB").upper()
with col2:
    dest_icao = st.text_input("Destination (DEST) *Optional*:", max_chars=4, placeholder="z.B. EDDF").upper()
with col3:
    altn_icao = st.text_input("Alternate (ALTN) *Optional*:", max_chars=4, placeholder="z.B. EDDS").upper()

if st.button("Executive Briefing generieren"):
    if not gemini_key:
        st.error("🔒 Bitte hinterlege einen API-Key in den Streamlit-Secrets oder links im Menü.")
    elif dep_icao:
        st.info("📡 Sammle Live-Daten von FAA und AviationWeather...")
        all_runways = load_runway_database()
        
        combined_raw_data = fetch_raw_data(dep_icao, "DEPARTURE", all_runways)
        if dest_icao:
            combined_raw_data += fetch_raw_data(dest_icao, "DESTINATION", all_runways)
        if altn_icao:
            combined_raw_data += fetch_raw_data(altn_icao, "ALTERNATE", all_runways)
            
        briefing_output = generate_ai_briefing(combined_raw_data, gemini_key)
        
        st.markdown("---")
        st.markdown(briefing_output)
    else:
        st.warning("Bitte gib mindestens den Departure Airport (DEP) ein.")
