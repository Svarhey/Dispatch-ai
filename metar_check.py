import requests
import streamlit as st
import math
import csv
import google.generativeai as genai
from io import StringIO
from datetime import datetime, timezone, timedelta

# Sicheres Laden der Websuche, falls requirements.txt noch baut
try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

# --- RUNWAY & AIRPORT DATENBANK ---
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

@st.cache_data(ttl=86400)
def get_airport_city(icao_code):
    url = f"https://davidmegginson.github.io/ourairports-data/airports.csv"
    try:
        res = requests.get(url, timeout=10)
        reader = csv.DictReader(StringIO(res.text))
        for row in reader:
            if row['ident'].upper() == icao_code.upper():
                return row.get('municipality', '')
    except:
        pass
    return ""

# --- TRACKING & DEEP DATA API (JETZT 5 MINUTEN CACHE!) ---
@st.cache_data(ttl=300) # 300 Sekunden = 5 Minuten
def fetch_deep_flight_data(flight_number, flight_date, api_key):
    flight_clean = flight_number.replace(" ", "").upper()
    date_str = flight_date.strftime("%Y-%m-%d")
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
    
    result = {"success": False, "error": "Unbekannter Fehler", "raw_flight": {}, "aircraft": {}, "dep_delays": {}, "dest_delays": {}, "solar": {}, "traffic_density": "No Data"}
    
    try:
        res = requests.get(f"https://aerodatabox.p.rapidapi.com/flights/number/{flight_clean}/{date_str}", headers=headers, timeout=10)
        if res.status_code != 200 or len(res.json()) == 0:
            return {"success": False, "error": f"Flug {flight_clean} am {date_str} nicht im System."}
        
        f_data = res.json()[0]
        result["raw_flight"] = f_data
        dep = f_data.get("departure", {}).get("airport", {}).get("icao")
        dest = f_data.get("arrival", {}).get("airport", {}).get("icao")
        reg = f_data.get("aircraft", {}).get("reg")
        
        if not dep or not dest:
            return {"success": False, "error": "Keine ICAO-Codes für Route gefunden."}
            
        result["success"] = True
        
        if reg:
            ac_res = requests.get(f"https://aerodatabox.p.rapidapi.com/aircrafts/reg/{reg}", headers=headers, timeout=5)
            if ac_res.status_code == 200: result["aircraft"] = ac_res.json()
                
        dep_del = requests.get(f"https://aerodatabox.p.rapidapi.com/airports/icao/{dep}/delays", headers=headers, timeout=5)
        if dep_del.status_code == 200: result["dep_delays"] = dep_del.json()
        dest_del = requests.get(f"https://aerodatabox.p.rapidapi.com/airports/icao/{dest}/delays", headers=headers, timeout=5)
        if dest_del.status_code == 200: result["dest_delays"] = dest_del.json()
        
        sol_res = requests.get(f"https://aerodatabox.p.rapidapi.com/airports/icao/{dest}/time/solar/{date_str}", headers=headers, timeout=5)
        if sol_res.status_code == 200: result["solar"] = sol_res.json()
        
        arr_time_str = f_data.get("arrival", {}).get("scheduledTimeUtc")
        if arr_time_str:
            arr_dt = datetime.fromisoformat(arr_time_str.replace("Z", "+00:00"))
            from_t = (arr_dt - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
            to_t = (arr_dt + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
            traf_res = requests.get(f"https://aerodatabox.p.rapidapi.com/flights/airports/icao/{dest}/{from_t}/{to_t}", headers=headers, params={"withLeg": "false"}, timeout=5)
            if traf_res.status_code == 200:
                result["traffic_density"] = f"{len(traf_res.json().get('arrivals', []))} geplante Landungen im 30-Minuten-Zeitfenster."

    except Exception as e:
        result["error"] = str(e)
        
    return result

# --- LIVE WEBSEARCH AGENT (OSINT) ---
def search_city_events(city_name, flight_date):
    if not city_name or not DDGS:
        return "Keine Websuche möglich."
    
    date_str = flight_date.strftime("%Y-%m-%d")
    query = f"{city_name} events security marathon demonstration strikes political visit {date_str}"
    
    try:
        with DDGS() as ddgs:
            search_results = list(ddgs.text(query, max_results=3))
            if search_results:
                summary = f"--- Live OSINT Websearch für {city_name} ({date_str}) ---\n"
                for r in search_results:
                    summary += f"- Title: {r.get('title')}\n  Snippet: {r.get('body')}\n"
                return summary
    except:
        pass
    return f"Keine signifikanten lokalen Großereignisse für {city_name} via Kurzsuche detektiert."

# --- METAR / TAF / NOTAM SAMMLER ---
def get_airport_raw_data(icao_code, label, all_runways):
    weather_info = f"--- {label}: {icao_code.upper()} ---\n"
    notam_info = f"--- {label}: {icao_code.upper()} ---\n"
    wdir, wspd, wgst, metar_raw = None, None, None, ""
    
    try:
        metar_res = requests.get(f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json").json()
        taf_res = requests.get(f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json").json()
        if metar_res:
            metar_raw = metar_res[0].get('rawOb', '')
            weather_info += f"METAR: {metar_raw}\n"
            wdir = metar_res[0].get("wdir")
            wspd = metar_res[0].get("wspd")
            wgst = metar_res[0].get("wgst")
        if taf_res: weather_info += f"TAF: {taf_res[0].get('rawTAF')}\n"
    except:
        weather_info += "Wetterdaten nicht verfügbar.\n"
        
    # --- INTELLIGENTE WINDAUSWERTUNG (Inkl. Winter Ops Logik) ---
    if wdir and wspd and isinstance(wdir, (int, float)):
        weather_info += "\n[RUNWAY WIND ANALYSIS]\n"
        
        # Check ob das METAR Hinweise auf Schnee, Eis oder gefrierenden Niederschlag hat
        winter_ops_active = any(code in metar_raw for code in ["SN", "FZ", "PL", "GS", "GR"])
        
        airport_runways = [r for r in all_runways if r['airport_ident'].upper() == icao_code.upper()]
        for rwy in airport_runways:
            ends = [(rwy.get('le_ident'), rwy.get('le_heading_degT')), (rwy.get('he_ident'), rwy.get('he_heading_degT'))]
            for rwy_id, rwy_hdg_str in ends:
                if rwy_id:
                    rwy_hdg = float(rwy_hdg_str) if rwy_hdg_str else int(''.join(filter(str.isdigit, rwy_id))) * 10
                    angle = math.radians(wdir - rwy_hdg)
                    headwind = wspd * math.cos(angle)
                    crosswind = abs(wspd * math.sin(angle))
                    
                    hw_str = f"Headwind: {headwind:.1f} kt" if headwind >= 0 else f"Tailwind: {abs(headwind):.1f} kt"
                    
                    cw_alert = ""
                    # Dynamische Schwellenwerte je nach Winter Ops
                    if winter_ops_active:
                        if crosswind >= 20.0:
                            cw_alert = " 🔴 ⚠️ [CRITICAL CROSSWIND ALERT >= 20KT! (WINTER OPS)]"
                        elif crosswind >= 15.0:
                            cw_alert = " ⚠️ [CROSSWIND ALERT >= 15KT! (WINTER OPS)]"
                    else:
                        if crosswind >= 30.0:
                            cw_alert = " 🔴 ⚠️ [CRITICAL CROSSWIND ALERT >= 30KT!]"
                        elif crosswind >= 20.0:
                            cw_alert = " ⚠️ [CROSSWIND ALERT >= 20KT!]"
                            
                    weather_info += f"RWY {rwy_id}: {hw_str} | Crosswind: {crosswind:.1f} kt{cw_alert}\n"

    # NOTAMs
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        session.get("https://notams.aim.faa.gov/notamSearch/", timeout=5)
        n = session.post("https://notams.aim.faa.gov/notamSearch/search", data={"searchType": 0, "designatorsForLocation": icao_code.upper()}).json()
        if "notamList" in n:
            for item in n["notamList"]: notam_info += f"- {item.get('icaoMessage', item.get('traditionalMessage', ''))}\n"
    except:
        notam_info += "NOTAMs temporär nicht verfügbar.\n"
        
    return weather_info, notam_info

# --- STREAMLIT UI ---
st.set_page_config(page_title="Dispatch-AI", page_icon="✈️", layout="wide")
st.title("✈️ Dispatch-AI")
st.subheader("Professional AI-Powered Pre-Flight Briefing")

gemini_key = st.secrets.get("GEMINI_API_KEY")
rapid_key = st.secrets.get("RAPIDAPI_KEY")

if not gemini_key:
    st.error("🔒 Bitte hinterlege den GEMINI_API_KEY in den Streamlit Secrets.")
    st.stop()

col_fn, col_date = st.columns(2)
flight_input = col_fn.text_input("Flugnummer (z.B. LH94):", placeholder="LH94").upper()
flight_date = col_date.date_input("Flugdatum:", datetime.now().date())

with st.expander("➕ Optionale Alternates hinzufügen"):
    a1, a2, a3, a4 = st.columns(4)
    altns = [a1.text_input("ALTN 1", key="a1").upper(), a2.text_input("ALTN 2", key="a2").upper(), a3.text_input("ALTN 3", key="a3").upper(), a4.text_input("ALTN 4", key="a4").upper()]

if st.button("Executive Briefing erstellen"):
    if flight_input:
        all_runways = load_runway_database()
        deep_data = fetch_deep_flight_data(flight_input, flight_date, rapid_key) if rapid_key else {"success": False, "error": "Kein API Key"}
        
        if deep_data["success"]:
            f = deep_data["raw_flight"]
            dep_icao = f.get("departure", {}).get("airport", {}).get("icao")
            dest_icao = f.get("arrival", {}).get("airport", {}).get("icao")
            
            st.success(f"✈️ Flugplan aktiv: {dep_icao} ➡️ {dest_icao} | Aircraft Tail: {f.get('aircraft', {}).get('reg')}")
            
            dep_city = get_airport_city(dep_icao)
            dest_city = get_airport_city(dest_icao)
            
            st.info("🌐 Analysiere lokale Nachrichten- und Sicherheitslage in den Zielstädten...")
            dep_osint = search_city_events(dep_city, flight_date)
            dest_osint = search_city_events(dest_city, flight_date)
            combined_osint = f"{dep_osint}\n\n{dest_osint}"
            
            st.info("📡 Scanne weltweite Wetterdatenbänke und FAA NOTAM-Server...")
            all_w = ""; all_n = ""
            for code in [dep_icao, dest_icao] + [a for a in altns if a]:
                label = "DEP" if code == dep_icao else ("DEST" if code == dest_icao else "ALTN")
                w, n = get_airport_raw_data(code, label, all_runways)
                all_w += w + "\n"; all_n += n + "\n"
                
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel('gemini-2.5-flash')
            
            # --- NEUER PROMPT MIT FARBVORGABE FÜR WARNUNGEN ---
            prompt = f"""
            Du bist 'Dispatch-AI', ein Elite-Flugdienstberater für Lufthansa-Crews.
            Erstelle ein prägnantes, hochprofessionelles Executive Pre-Flight Briefing auf Deutsch basierend auf diesen Daten.
            
            REGELN FÜR DEIN GEHIRN:
            1. EXECUTIVE SUMMARY: Nenne Flug, Tail, exaktes Flugzeugalter und Triebwerkstyp (falls im JSON), Parkpositionen (Gate/Stand), Verspätungen und die Verkehrsdichte bei der Landung.
            2. WEATHER & RUNWAYS: Analysiere METAR/TAF. WICHTIG: Wenn im Block [RUNWAY WIND ANALYSIS] ein 'CROSSWIND ALERT' oder 'CRITICAL CROSSWIND ALERT' steht, musst du extrem deutlich warnen. Einen 'CRITICAL CROSSWIND ALERT' formatierst du zwingend in ROT (Nutze die Streamlit-Syntax: :red[Dein Warntext]). Besprich die operationellen Konsequenzen für die Landung/Start (besonders bei WINTER OPS)!
            3. NOTAMs: Filtere radikal nach kritischen Faktoren (ILS-Ausfall, geschlossene Pisten).
            4. LOCAL SECURITY & CITY LOGISTICS: Werte die Live-Websuchdaten (OSINT) aus. Warne die Crew explizit vor Marathons, Straßensperren oder Demonstrationen, die den Layover beeinträchtigen.
            5. THREAT & ERROR MANAGEMENT (TEM): Fasse die 3 kritischsten Risiken dieses Fluges zusammen.
            
            [AIRCRAFT & AIRPORT METADATA JSON]
            {deep_data}
            
            [WEATHER DATA]
            {all_w}
            
            [NOTAM DATA]
            {all_n}
            
            [LOCAL CITY EVENTS & SECURITY SEARCH]
            {combined_osint}
            """
            
            with st.spinner('🧠 Dispatch-AI berechnet Limits, checkt Nachrichten und schreibt Briefing...'):
                briefing_output = model.generate_content(prompt).text
                
            t1, t2, t3, t4 = st.tabs(["🤖 AI Executive Briefing", "🌤️ Wetter-Rohdaten", "📋 NOTAM-Rohdaten", "✈️ Flugzeug & OSINT-Daten"])
            t1.markdown(briefing_output)
            t2.code(all_w, language="text")
            t3.code(all_n, language="text")
            with t4:
                st.markdown("### 🌐 Live OSINT City Security & Events")
                st.code(combined_osint, language="text")
                st.markdown("### ✈️ Telemetrie & API-JSON (AeroDataBox)")
                st.json(deep_data)
        else:
            st.error(f"Fehler: {deep_data['error']}")
    else:
        st.warning("Bitte gib eine Flugnummer ein.")
