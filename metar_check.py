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
    """Holt DEP, DEST, Tailsign, Parkpositionen und Delays basierend auf Flugnummer und Datum"""
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
            data = res.json()[0] 
            
            dep = data.get("departure", {}).get("airport", {}).get("icao")
            dest = data.get("arrival", {}).get("airport", {}).get("icao")
            tail = data.get("aircraft", {}).get("reg", "UNKNOWN TAIL")
            
            # Parkpositionen extrahieren (Gate & Stand)
            dep_gate = data.get("departure", {}).get("gate", "N/A")
            dep_stand = data.get("departure", {}).get("stand", "N/A")
            arr_gate = data.get("arrival", {}).get("gate", "N/A")
            arr_stand = data.get("arrival", {}).get("stand", "N/A")
            
            dep_delay = data.get("departure", {}).get("delayMinutes", 0)
            arr_delay = data.get("arrival", {}).get("delayMinutes", 0)
            
            if dep and dest:
                return {
                    "success": True, 
                    "dep": dep,
                    "dest": dest,
                    "tail": tail,
                    "dep_gate": dep_gate,
                    "dep_stand": dep_stand,
                    "arr_gate": arr_gate,
                    "arr_stand": arr_stand,
                    "delay": f"DEP Delay: {dep_delay}min / ARR Delay: {arr_delay}min" if (dep_delay or arr_delay) else "On Time",
                    "raw_json": data
                }
            else:
                return {"success": False, "error": "API lieferte Daten, aber keine ICAO-Codes für DEP/DEST."}
        return {"success": False, "error": f"HTTP Code {res.status_code}: Flug nicht gefunden."}
    except Exception as e:
        return {"success": False, "error": str(e)}

# --- DATEN SAMMLER (Getrennte Rückgabe von Wetter & NOTAMs) ---
def get_airport_raw_data(icao_code, label, all_runways):
    """Sammelt Wetter (inkl. Runway-Analyse) und NOTAMs strikt getrennt"""
    weather_info = f"--- {label}: {icao_code.upper()} ---\n"
    notam_info = f"--- {label}: {icao_code.upper()} ---\n"
    
    wdir, wspd, wgst = None, None, None
    
    # 1. Wetter abrufen (AviationWeather)
    try:
        metar_res = requests.get(f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json").json()
        taf_res = requests.get(f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json").json()
        
        if metar_res:
            metar_raw = metar_res[0].get('rawOb', 'N/A')
            weather_info += f"METAR: {metar_raw}\n"
            wdir = metar_res[0].get("wdir")
            wspd = metar_res[0].get("wspd")
            wgst = metar_res[0].get("wgst")
        else:
            weather_info += "METAR: Nicht verfügbar.\n"
            
        if taf_res:
            weather_info += f"TAF: {taf_res[0].get('rawTAF', 'N/A')}\n"
        else:
            weather_info += "TAF: Nicht verfügbar.\n"
    except Exception as e:
        weather_info += f"Fehler beim Wetterabruf: {e}\n"
        
    # Integrierte Wind-Vektor Analyse für die Pisten
    if wdir and wspd and isinstance(wdir, (int, float)):
        weather_info += "\n[RUNWAY WIND ANALYSIS]\n"
        airport_runways = [r for r in all_runways if r['airport_ident'].upper() == icao_code.upper()]
        for rwy in airport_runways:
            ends = [
                (rwy.get('le_ident'), rwy.get('le_heading_degT')),
                (rwy.get('he_ident'), rwy.get('he_heading_degT'))
            ]
            for rwy_id, rwy_hdg_str in ends:
                if rwy_id:
                    rwy_hdg = float(rwy_hdg_str) if rwy_hdg_str else int(''.join(filter(str.isdigit, rwy_id))) * 10
                    angle = math.radians(wdir - rwy_hdg)
                    headwind = wspd * math.cos(angle)
                    crosswind = wspd * math.sin(angle)
                    
                    hw_str = f"Headwind: {headwind:.1f} kt" if headwind >= 0 else f"Tailwind: {abs(headwind):.1f} kt"
                    cw_dir = "from Right" if crosswind > 0 else "from Left"
                    weather_info += f"RWY {rwy_id}: {hw_str} | Crosswind: {abs(crosswind):.1f} kt ({cw_dir})\n"
        if wgst:
            weather_info += f"⚠️ Gusts active: up to {wgst} kt\n"

    # 2. NOTAMs abrufen (FIX: Reparierter Session-Header-Bypass!)
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        session.headers.update(headers)
        # Cookie initialisieren
        session.get("https://notams.aim.faa.gov/notamSearch/", timeout=5)
        
        faa_url = "https://notams.aim.faa.gov/notamSearch/search"
        payload = {"searchType": 0, "designatorsForLocation": icao_code.upper()}
        res = session.post(faa_url, data=payload, timeout=10).json()
        
        if "notamList" in res and len(res["notamList"]) > 0:
            for item in res["notamList"]:
                notam_info += f"- {item.get('icaoMessage', item.get('traditionalMessage', ''))}\n"
        else:
            notam_info += "Keine aktiven NOTAMs für diesen Platz.\n"
    except Exception as e:
        notam_info += f"NOTAM-Abruf fehlgeschlagen: {e}\n"
        
    return weather_info, notam_info

# --- STREAMLIT UI OBERFLÄCHE ---
st.set_page_config(page_title="Dispatch-AI", page_icon="✈️", layout="wide")

st.title("✈️ Dispatch-AI")
st.subheader("Professional AI-Powered Pre-Flight Briefing")

gemini_key = st.secrets.get("GEMINI_API_KEY")
rapid_key = st.secrets.get("RAPIDAPI_KEY")

if not gemini_key:
    st.error("🔒 Bitte hinterlege den GEMINI_API_KEY in den Streamlit Secrets.")
    st.stop()

# Das bewährte iPhone-Layout
col_fn, col_date = st.columns(2)
with col_fn:
    flight_input = st.text_input("Flugnummer (z.B. LH94):", value="LH94").upper()
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
        flight_meta = {"fn": flight_input, "tail": "UNKNOWN", "delay": "On Time", "dep": None, "dest": None, "raw_json": {}}
        
        # Flugdaten via Tracking API laden
        if rapid_key:
            st.info(f"🔍 Tracke Flug {flight_input} für den {flight_date.strftime('%d.%m.%Y')}...")
            track_res = fetch_flight_metadata(flight_input, flight_date, rapid_key)
            
            if track_res["success"]:
                flight_meta["dep"] = track_res["dep"]
                flight_meta["dest"] = track_res["dest"]
                flight_meta["tail"] = track_res["tail"]
                flight_meta["delay"] = track_res["delay"]
                flight_meta["dep_gate"] = track_res["dep_gate"]
                flight_meta["dep_stand"] = track_res["dep_stand"]
                flight_meta["arr_gate"] = track_res["arr_gate"]
                flight_meta["arr_stand"] = track_res["arr_stand"]
                flight_meta["raw_json"] = track_res["raw_json"]
                st.success(f"✈️ Flug gefunden: {flight_meta['dep']} ➡️ {flight_meta['dest']} | Aircraft: {flight_meta['tail']}")
            else:
                st.warning(f"⚠️ Tracking fehlgeschlagen: {track_res['error']}")
        
        # Manueller Fallback
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
        
        weather_dump = ""
        notam_dump = ""
        
        # Dynamische Liste aller aktiven Plätze bauen
        active_airports = [flight_meta["dep"], flight_meta["dest"]] + [a for a in [altn1, altn2, altn3, altn4] if a]
        
        for code in active_airports:
            label = "DEPARTURE" if code == flight_meta["dep"] else ("DESTINATION" if code == flight_meta["dest"] else "ALTERNATE")
            w, n = get_airport_raw_data(code, label, all_runways)
            weather_dump += w + "\n"
            notam_dump += n + "\n"
            
        # Generierung des KI Prompts mit den neuen Parkpositionen
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        prompt = f"""
        Du bist 'Dispatch-AI', ein professioneller, präziser Flight Dispatch Assistant für Lufthansa-Piloten.
        Deine Aufgabe ist es, aus den folgenden Rohdaten ein extrem klares, operationell sinnvolles Executive Pre-Flight Briefing auf Deutsch zu erstellen.
        
        Flug-Metadaten:
        - Flugnummer: {flight_meta.get('fn')}
        - Geplantes Flugzeug (Tail): {flight_meta.get('tail')}
        - Status/Verspätung: {flight_meta.get('delay')}
        - DEP Gate/Stand: {flight_meta.get('dep_gate', 'N/A')} / {flight_meta.get('dep_stand', 'N/A')}
        - DEST Gate/Stand: {flight_meta.get('arr_gate', 'N/A')} / {flight_meta.get('arr_stand', 'N/A')}
        
        Regeln für die Struktur des Briefings:
        1. Beginne mit einer kurzen 'Executive Summary' (Flugnummer, Registrierung, Parkpositionen am Gate/Stand und eventuelle Verspätungen).
        2. Fasse das Wetter für DEP, DEST und alle Alternates verständlich zusammen. Weist anhand des [RUNWAY WIND ANALYSIS] Blocks kurz auf die operationell bevorzugte Bahn hin.
        3. Filtere die NOTAMs kritisch (Bahnsperrungen, Ausfälle von ILS/Navaids, wichtige Taxiway-Einschränkungen).
        4. Erstelle am Ende einen Abschnitt "Threat & Error Management (TEM) / Operationelles Takeaway".
        
        [WEATHER & RUNWAY RAW DATA]
        {weather_dump}
        
        [NOTAM RAW DATA]
        {notam_dump}
        """
        
        with st.spinner('🧠 Dispatch-AI analysiert Flugdaten und schreibt Briefing...'):
            try:
                response = model.generate_content(prompt)
                briefing_output = response.text
            except Exception as e:
                briefing_output = f"❌ Fehler bei der KI-Generierung. Details: {e}"
        
        # --- ERSTELLUNG DER REITER (TABS) ---
        st.markdown("---")
        tab1, tab2, tab3, tab4 = st.tabs(["🤖 AI Executive Briefing", "🌤️ Wetter Rohdaten", "📋 NOTAM Rohdaten", "✈️ Flugzeug Rohdaten"])
        
        with tab1:
            st.markdown(briefing_output)
            
        with tab2:
            st.code(weather_dump, language="text")
            
        with tab3:
            st.code(notam_dump, language="text")
            
        with tab4:
            st.markdown("### ✈️ Flugzeug-spezifische Echtzeitdaten")
            st.markdown(f"**Registration (Tail):** `{flight_meta.get('tail')}`")
            st.markdown(f"**Departure Position:** Gate `{flight_meta.get('dep_gate')}` / Stand `{flight_meta.get('dep_stand')}`")
            st.markdown(f"**Destination Position:** Gate `{flight_meta.get('arr_gate')}` / Stand `{flight_meta.get('arr_stand')}`")
            st.markdown(f"**Status:** {flight_meta.get('delay')}")
            st.markdown("---")
            st.markdown("#### Vollständiger JSON-API Response (AeroDataBox):")
