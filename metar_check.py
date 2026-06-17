import requests
import streamlit as st
import math
import csv
import google.generativeai as genai
from io import BytesIO
from datetime import datetime, timezone, timedelta

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

try:
    from gtts import gTTS
except ImportError:
    gTTS = None

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

# --- TRACKING & DEEP DATA API ---
@st.cache_data(ttl=300)
def fetch_deep_flight_data(flight_number, flight_date, api_key):
    flight_clean = flight_number.replace(" ", "").upper()
    date_str = flight_date.strftime("%Y-%m-%d")
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"}
    result = {"success": False, "error": "Fehler", "raw_flight": {}, "aircraft": {}, "solar": {}, "traffic_density": "No Data"}
    try:
        res = requests.get(f"https://aerodatabox.p.rapidapi.com/flights/number/{flight_clean}/{date_str}", headers=headers, timeout=10)
        if res.status_code != 200 or len(res.json()) == 0: return {"success": False, "error": "Flug nicht gefunden."}
        f_data = res.json()[0]
        result["raw_flight"] = f_data
        dep = f_data.get("departure", {}).get("airport", {}).get("icao")
        dest = f_data.get("arrival", {}).get("airport", {}).get("icao")
        reg = f_data.get("aircraft", {}).get("reg")
        if not dep or not dest: return {"success": False, "error": "Keine Route."}
        result["success"] = True
        
        if reg:
            ac_res = requests.get(f"https://aerodatabox.p.rapidapi.com/aircrafts/reg/{reg}", headers=headers, timeout=5)
            if ac_res.status_code == 200: result["aircraft"] = ac_res.json()
        
        arr_time_str = f_data.get("arrival", {}).get("scheduledTimeUtc")
        if arr_time_str:
            arr_dt = datetime.fromisoformat(arr_time_str.replace("Z", "+00:00"))
            from_t = (arr_dt - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
            to_t = (arr_dt + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
            traf_res = requests.get(f"https://aerodatabox.p.rapidapi.com/flights/airports/icao/{dest}/{from_t}/{to_t}", headers=headers, params={"withLeg": "false"}, timeout=5)
            if traf_res.status_code == 200: result["traffic_density"] = f"{len(traf_res.json().get('arrivals', []))} Landungen im 30min Fenster."
    except Exception as e:
        result["error"] = str(e)
    return result

# --- LIVE WEBSEARCH AGENT (OSINT) ---
def search_city_events(city_name, flight_date):
    if not city_name or not DDGS: return "Keine Websuche möglich."
    date_str = flight_date.strftime("%Y-%m-%d")
    query = f"{city_name} events security marathon demonstration strikes political visit {date_str}"
    try:
        with DDGS() as ddgs:
            search_results = list(ddgs.text(query, max_results=3))
            if search_results:
                summary = f"--- OSINT für {city_name} ---\n"
                for r in search_results: summary += f"- {r.get('title')}: {r.get('body')}\n"
                return summary
    except: pass
    return f"Keine lokalen Großereignisse für {city_name} detektiert."

# --- METAR / TAF / NOTAM SAMMLER ---
def get_airport_raw_data(icao_code, label, all_runways):
    weather_info = f"--- {label}: {icao_code.upper()} ---\n"
    notam_info = f"--- {label}: {icao_code.upper()} ---\n"
    wdir, wspd, metar_raw = None, None, ""
    
    try:
        metar_res = requests.get(f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json").json()
        taf_res = requests.get(f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json").json()
        if metar_res:
            metar_raw = metar_res[0].get('rawOb', '')
            weather_info += f"METAR: {metar_raw}\n"
            wdir = metar_res[0].get("wdir")
            wspd = metar_res[0].get("wspd")
        if taf_res: weather_info += f"TAF: {taf_res[0].get('rawTAF')}\n"
    except: weather_info += "Wetter nicht verfügbar.\n"
        
    if wdir and wspd and isinstance(wdir, (int, float)):
        weather_info += "\n[RUNWAY WIND ANALYSIS]\n"
        winter_ops = any(code in metar_raw for code in ["SN", "FZ", "PL", "GS", "GR"])
        airport_runways = [r for r in all_runways if r['airport_ident'].upper() == icao_code.upper()]
        for rwy in airport_runways:
            ends = [(rwy.get('le_ident'), rwy.get('le_heading_degT')), (rwy.get('he_ident'), rwy.get('he_heading_degT'))]
            for rwy_id, rwy_hdg_str in ends:
                if rwy_id:
                    rwy_hdg = float(rwy_hdg_str) if rwy_hdg_str else int(''.join(filter(str.isdigit, rwy_id))) * 10
                    angle = math.radians(wdir - rwy_hdg)
                    crosswind = abs(wspd * math.sin(angle))
                    cw_alert = ""
                    if winter_ops:
                        if crosswind >= 20.0: cw_alert = " 🔴 ⚠️ [CRITICAL CROSSWIND ALERT >= 20KT! (WINTER OPS)]"
                        elif crosswind >= 15.0: cw_alert = " ⚠️ [CROSSWIND ALERT >= 15KT! (WINTER OPS)]"
                    else:
                        if crosswind >= 30.0: cw_alert = " 🔴 ⚠️ [CRITICAL CROSSWIND ALERT >= 30KT!]"
                        elif crosswind >= 20.0: cw_alert = " ⚠️ [CROSSWIND ALERT >= 20KT!]"
                    weather_info += f"RWY {rwy_id} Crosswind: {crosswind:.1f} kt{cw_alert}\n"

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        session.get("https://notams.aim.faa.gov/notamSearch/", timeout=5)
        n = session.post("https://notams.aim.faa.gov/notamSearch/search", data={"searchType": 0, "designatorsForLocation": icao_code.upper()}).json()
        if "notamList" in n:
            for item in n["notamList"]: notam_info += f"- {item.get('icaoMessage', item.get('traditionalMessage', ''))}\n"
    except: notam_info += "NOTAMs temporär nicht verfügbar.\n"
        
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
            current_utc_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            
            st.success(f"✈️ Flugplan aktiv: {dep_icao} ➡️ {dest_icao} | Aircraft Tail: {f.get('aircraft', {}).get('reg')}")
            
            dep_city = get_airport_city(dep_icao)
            dest_city = get_airport_city(dest_icao)
            
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
            
            # --- GEHIRN 1: DAS TEXT BRIEFING ---
            prompt_text = f"""
            Du bist 'Dispatch-AI'. Aktuelle UTC-Zeit: {current_utc_time}.
            Erstelle ein schriftliches Executive Pre-Flight Briefing auf Deutsch. Beachte zeitliche Limits (METAR vs TAF). Formatiere Critical Crosswind Alerts zwingend in ROT (:red[...]).
            Nutze Aviation Denglish (z.B. Runway, Crosswind, Low Vis, Holdings).
            
            JSON: {deep_data}
            WETTER: {all_w}
            NOTAM: {all_n}
            OSINT: {combined_osint}
            """
            
            # --- GEHIRN 2: DAS AUDIO SKRIPT FÜR DEN DISPATCHER ANRUF ---
            prompt_audio = f"""
            Schreibe ein Radioskript, das von einer TTS-Stimme vorgelesen wird. 
            Szenario: Du bist ein Kollege vom Lufthansa Dispatch und rufst die Crew kurz an.
            Tonfall: Entspannt, kollegial, direkt. ("Hallo Crew, hier ist euer Dispatcher für die LH94...")
            
            SPRACH-REGELN (EXTREM WICHTIG):
            - Nutze durchgehend Aviation Denglish! (Takeoff, Approach, Gear, RVR, Low Vis, Gusts, Crosswind).
            - ACHTUNG PHONETIK: Damit die deutsche Stimme Zahlen richtig ausspricht, musst du Zahlen als ENGLISCHE WÖRTER ausschreiben!
              Beispiel FALSCH: "Wind aus 250 mit 15."
              Beispiel RICHTIG: "Der Wind am Dest kommt aus two five zero at one five knots, gusting two five."
              Beispiel RICHTIG: "Runway two five right."
            - Mache kurze, klare Sätze. Lass rohe NOTAM-Codes und unwichtige Details weg. Fokussiere dich auf TEM (Threats).
            - Keine Sternchen (*), keine Markdown-Formatierung.
            
            JSON: {deep_data}
            WETTER: {all_w}
            NOTAM: {all_n}
            OSINT: {combined_osint}
            """
            
            with st.spinner('🧠 Generiere Text- und Audio-Briefings...'):
                briefing_text = model.generate_content(prompt_text).text
                audio_script = model.generate_content(prompt_audio).text
                
                # Audio Datei via gTTS generieren
                audio_bytes = BytesIO()
                if gTTS:
                    tts = gTTS(text=audio_script, lang='de', slow=False)
                    tts.write_to_fp(audio_bytes)
                
            st.markdown("---")
            
            # --- DER AUDIO PLAYER ---
            st.markdown("### 🎧 Audio Dispatch Briefing")
            if gTTS:
                st.audio(audio_bytes, format="audio/mp3")
                with st.expander("Skript mitlesen (Denglish Phonetisch)"):
                    st.write(audio_script)
            else:
                st.error("Bitte 'gTTS' in die requirements.txt eintragen!")

            # --- DIE BEKANNTEN REITER ---
            t1, t2, t3, t4 = st.tabs(["🤖 AI Executive Briefing", "🌤️ Wetter-Rohdaten", "📋 NOTAM-Rohdaten", "✈️ Flugzeug & OSINT-Daten"])
            t1.markdown(briefing_text)
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
