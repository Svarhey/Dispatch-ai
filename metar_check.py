```python
import time
import requests
import streamlit as st
import math
import csv
import base64
from google import genai
from google.genai import types
from datetime import datetime, timezone, timedelta
from io import BytesIO

# --- SICHERER IMPORT DER WEBSUCHE ---
try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

# --- SICHERER IMPORT DES BACKUP-AUDIO-SYSTEMS ---
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
        reader = csv.DictReader(response.text.splitlines())
        return [row for row in reader if row['closed'] == '0']
    except:
        return []

@st.cache_data(ttl=86400)
def get_airport_city(icao_code):
    url = f"https://davidmegginson.github.io/ourairports-data/airports.csv"
    try:
        res = requests.get(url, timeout=10)
        reader = csv.DictReader(res.text.splitlines())
        for row in reader:
            if row['ident'].upper() == icao_code.upper():
                return row.get('municipality', '')
    except:
        pass
    return ""

# --- TRACKING & DEEP DATA API (5 MINUTEN CACHE) ---
@st.cache_data(ttl=300)
def fetch_deep_flight_data(flight_number, flight_date, api_key):
    flight_clean = flight_number.replace(" ", "").upper()
    date_str = flight_date.strftime("%Y-%m-%d")
    headers = {
        "X-RapidAPI-Key": api_key, 
        "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"
    }
    result = {
        "success": False, "error": "Fehler", "raw_flight": {}, 
        "aircraft": {}, "solar": {}, "traffic_density": "No Data"
    }
    try:
        res = requests.get(
            f"https://aerodatabox.p.rapidapi.com/flights/number/{flight_clean}/{date_str}", 
            headers=headers, timeout=10
        )
        if res.status_code != 200 or len(res.json()) == 0: 
            return {"success": False, "error": f"Flug {flight_clean} nicht gefunden."}
        
        f_data = res.json()[0]
        result["raw_flight"] = f_data
        dep = f_data.get("departure", {}).get("airport", {}).get("icao")
        dest = f_data.get("arrival", {}).get("airport", {}).get("icao")
        reg = f_data.get("aircraft", {}).get("reg")
        
        if not dep or not dest: 
            return {"success": False, "error": f"Keine Route für {flight_clean}."}
            
        result["success"] = True
        
        if reg:
            ac_res = requests.get(
                f"https://aerodatabox.p.rapidapi.com/aircrafts/reg/{reg}", 
                headers=headers, timeout=5
            )
            if ac_res.status_code == 200: 
                result["aircraft"] = ac_res.json()
        
        arr_time_str = f_data.get("arrival", {}).get("scheduledTimeUtc")
        if arr_time_str:
            arr_dt = datetime.fromisoformat(arr_time_str.replace("Z", "+00:00"))
            from_t = (arr_dt - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
            to_t = (arr_dt + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
            traf_res = requests.get(
                f"https://aerodatabox.p.rapidapi.com/flights/airports/icao/{dest}/{from_t}/{to_t}", 
                headers=headers, params={"withLeg": "false"}, timeout=5
            )
            if traf_res.status_code == 200: 
                result["traffic_density"] = f"{len(traf_res.json().get('arrivals', []))} Landungen im 30min Fenster."
    except Exception as e:
        result["error"] = str(e)
    return result

# --- LIVE WEBSEARCH AGENT (OSINT) ---
def search_city_events(city_name, flight_date):
    if not city_name or DDGS is None: 
        return "Keine Websuche möglich."
        
    date_str = flight_date.strftime("%Y-%m-%d")
    query = f"{city_name} events security marathon demonstration strikes political visit {date_str}"
    
    try:
        with DDGS() as ddgs:
            search_results = list(ddgs.text(query, max_results=3))
            if search_results:
                summary = f"--- OSINT für {city_name} ---\n"
                for r in search_results: 
                    summary += f"- {r.get('title')}: {r.get('body')}\n"
                return summary
    except: 
        pass
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
        if taf_res: 
            weather_info += f"TAF: {taf_res[0].get('rawTAF')}\n"
    except: 
        weather_info += "Wetter nicht verfügbar.\n"
        
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
            for item in n["notamList"]: 
                notam_info += f"- {item.get('icaoMessage', item.get('traditionalMessage', ''))}\n"
    except: 
        notam_info += "NOTAMs temporär nicht verfügbar.\n"
        
    return weather_info, notam_info

# --- ZENTRALE RENDER-FUNKTION FÜR DAS BRIEFING ---
def render_briefing_ui(data):
    st.success(f"✈️ Flugplan aktiv: {data['route_string']} | Aircraft Tail: {data['reg']}")
    st.markdown("---")
    
    # --- AUDIO PLAYER HEADLINE ---
    if data["audio_format"] == "audio/mp3" and data.get("fallback_level") == "gtts":
        st.markdown("### 🎧 Dispatch Audio (Backup-Roboterstimme aktiv)")
    elif data.get("fallback_level") == "ultra_short_success":
        st.markdown("### 🎧 Dispatch Audio (⚠️ Krise: Ultra-Short Warning aktiv)")
    else:
        st.markdown("### 🎧 Native Dispatch Audio (Google Cloud Neural2)")
        
    if data["audio_bytes"]:
        st.audio(data["audio_bytes"], format=data["audio_format"])
        with st.expander("Skript mitlesen (Denglish Phonetisch)"):
            st.write(data["audio_script"])
    else:
        st.warning("Audiospur konnte nicht generiert werden.")

    # --- TABS ---
    t1, t2, t3, t4 = st.tabs(["🤖 AI Duty-Briefing (Multi-Leg)", "🌤️ Wetter-Rohdaten", "📋 NOTAM-Rohdaten", "✈️ Telemetrie & API"])
    t1.markdown(data["briefing_text"])
    t2.code(data["all_w"], language="text")
    t3.code(data["all_n"], language="text")
    with t4:
        st.markdown("### 🌐 Live OSINT City Security & Events")
        st.code(data["combined_osint"], language="text")
        st.markdown("### ✈️ Rohdaten AeroDataBox")
        st.json(data["deep_data_list"])

# --- STREAMLIT UI INIT ---
st.set_page_config(page_title="Dispatch-AI", page_icon="✈️", layout="wide")

# Initialisierung des Session State für die History (Letzte 5 Briefings)
if "history" not in st.session_state:
    st.session_state.history = []

# --- SIDEBAR HISTORY MENU ---
with st.sidebar:
    st.header("🗂️ Dispatch Log")
    st.write("Schnellzugriff auf archivierte Briefings (Lokal im Cache gespeichert).")
    
    options = ["📝 Neues Briefing erstellen"] + [h["display_name"] for h in st.session_state.history]
    view_mode = st.radio("Modus auswählen:", options)

st.title("✈️ Dispatch-AI")
st.subheader("Professional AI-Powered Duty-Briefing (Multi-Leg)")

# Die Dual-Key Sicherheitsabfrage
gemini_key = st.secrets.get("GEMINI_API_KEY")
gcp_key = st.secrets.get("GCP_API_KEY")
rapid_key = st.secrets.get("RAPIDAPI_KEY")

if not gemini_key or not gcp_key:
    st.error("🔒 Bitte hinterlege GEMINI_API_KEY und GCP_API_KEY in den Streamlit Secrets.")
    st.stop()

# Das Gemini Gehirn initialisieren
client = genai.Client(api_key=gemini_key)

# --- LOGIK: NEUES BRIEFING VS. HISTORY ---
if view_mode != "📝 Neues Briefing erstellen":
    st.info("🕒 **Archiv-Ansicht:** Dieses Briefing wurde aus dem lokalen Cache geladen. Es werden keine neuen API-Aufrufe getätigt.")
    selected_data = next(item for item in st.session_state.history if item["display_name"] == view_mode)
    render_briefing_ui(selected_data)

else:
    # --- MODUS: NEUES BRIEFING ERSTELLEN ---
    st.markdown("Gib bis zu 5 Flugnummern in chronologischer Reihenfolge ein, um ein fortlaufendes Briefing für den gesamten Arbeitstag zu erstellen.")
    
    cols = st.columns(5)
    flight_inputs = []
    for i in range(5):
        f_in = cols[i].text_input(f"Leg {i+1} (z.B. LH149):", key=f"leg_input_{i}").upper().strip()
        if f_in:
            flight_inputs.append(f_in)
            
    flight_date = st.date_input("Flugdatum:", datetime.now().date())

    with st.expander("➕ Optionale Alternates hinzufügen"):
        a1, a2, a3, a4 = st.columns(4)
        altns = [
            a1.text_input("ALTN 1", key="a1").upper(), 
            a2.text_input("ALTN 2", key="a2").upper(), 
            a3.text_input("ALTN 3", key="a3").upper(), 
            a4.text_input("ALTN 4", key="a4").upper()
        ]

    if st.button("Duty-Briefing erstellen"):
        if not flight_inputs:
            st.warning("Bitte gib mindestens eine Flugnummer ein.")
        else:
            all_runways = load_runway_database()
            
            deep_data_list = []
            route_airports = []
            display_flight_names = ", ".join(flight_inputs)
            reg = "N/A"
            
            # 1. Alle Flüge abfragen
            has_error = False
            with st.spinner("Lade Telemetriedaten für alle Legs..."):
                for fn in flight_inputs:
                    d_data = fetch_deep_flight_data(fn, flight_date, rapid_key) if rapid_key else {"success": False, "error": "Kein API Key"}
                    if not d_data["success"]:
                        st.error(f"Fehler bei {fn}: {d_data['error']}")
                        has_error = True
                        break
                    
                    deep_data_list.append(d_data)
                    f_raw = d_data["raw_flight"]
                    dep_icao = f_raw.get("departure", {}).get("airport", {}).get("icao")
                    dest_icao = f_raw.get("arrival", {}).get("airport", {}).get("icao")
                    
                    if len(route_airports) == 0:
                        route_airports.append(dep_icao)
                    elif route_airports[-1] != dep_icao:
                        st.warning(f"Routen-Warnung: Ankunft von vorherigem Leg passt nicht zum Abflug von {fn} ({dep_icao}).")
                        route_airports.append(dep_icao)
                    
                    route_airports.append(dest_icao)
                    
                    if reg == "N/A" and f_raw.get("aircraft", {}).get("reg"):
                        reg = f_raw.get("aircraft", {}).get("reg")

            if not has_error:
                # Formatierung der Route (z.B. EDDN ➡️ EDDF ➡️ LEMD ➡️ EDDF)
                route_string = " ➡️ ".join(route_airports)
                current_utc_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                
                # 2. Eindeutige Flughäfen ermitteln (Um APIs zu sparen)
                unique_airports = list(dict.fromkeys(route_airports + [a for a in altns if a]))
                
                all_w = ""
                all_n = ""
                combined_osint = ""
                
                with st.spinner("Scanne weltweite Wetterdatenbänke, OSINT und FAA NOTAM-Server..."):
                    for code in unique_airports:
                        if code:
                            city = get_airport_city(code)
                            osint_res = search_city_events(city, flight_date)
                            combined_osint += f"{osint_res}\n\n"
                            
                            w, n = get_airport_raw_data(code, "AIRPORT", all_runways)
                            all_w += w + "\n"
                            all_n += n + "\n"

                # 3. Der Master-Prompt mit Flottenfilter (A320 LPV Ignoranz)
                prompt_text = f"""
                Du bist 'Dispatch-AI'. Aktuelle UTC-Zeit: {current_utc_time}.
                Erstelle ein schriftliches Executive Duty-Briefing (Multi-Leg) auf Deutsch. 
                
                ROUTING FÜR HEUTE: {route_string}
                FLÜGE: {display_flight_names}
                
                ALLGEMEINE REGELN FÜR MULTI-LEG:
                1. Gehe die Flughäfen entlang der Route chronologisch durch. Fasse zusammen, was für den Ablauf wichtig ist.
                2. Formatiere Critical Crosswind Alerts zwingend in ROT (:red[...]).
                3. Nutze Aviation Denglish (z.B. Runway, Crosswind, Low Vis, Holdings).
                
                A320 FLOTTEN-FILTER (EXTREM WICHTIG):
                Die eingesetzte Flotte ist NICHT für LPV oder SBAS Approaches zugelassen.
                IGNORIERE alle NOTAMs, die von "LPV suspended", "LPV approach not available", "SBAS" oder "GLS" handeln. Filtere diese komplett aus deinem Text heraus, da sie für diese Crew irrelevant sind!
                
                ZEITGEBUNDENE NOTAMs & VERSPÄTUNGEN (DELAY WARNING):
                Im JSON ('deep_data_list') findest du die 'scheduledTimeUtc' (Geplante Ankunft). 
                Prüfe die Gültigkeitszeiten der NOTAMs. Wenn ein kritisches NOTAM (z.B. Runway Closure, ILS U/S) erst NACH unserer planmäßigen Ankunftszeit aktiv wird:
                - Berechne KEINE genauen Minuten/Stunden-Differenzen (Gefahr von Rechenfehlern!).
                - Setze stattdessen eine deutliche Warnung: "⚠️ ACHTUNG: [NOTAM-Inhalt] aktiv ab [Uhrzeit]Z. Bei Verspätung zwingend prüfen!"
                
                DIE "DELTA-REGEL" FÜR RÜCKFLÜGE:
                Wenn ein Flughafen auf der Route ZUM ZWEITEN MAL (oder öfter) angeflogen wird:
                - WETTER: Briefe das Wetter für den späteren Zeitpunkt als ÄNDERUNG (Delta) zum Vormittag.
                - NOTAMs: Ignoriere Standard-NOTAMs beim zweiten Besuch. ABER: Wiederhole zwingend kurz alle kritischen "Killer-NOTAMs" (Gesperrte Runways, geschlossene Taxiways) als "Reminder".
                
                ROHDATEN ALLER STATIONEN:
                WETTER: {all_w}
                NOTAM: {all_n}
                OSINT: {combined_osint}
                JSON-DATEN (inkl. Zeiten): {deep_data_list}
                """
                
                prompt_audio = f"""
                Schreibe ein Radioskript für eine professionelle TTS-Stimme. Du rufst die Crew kurz an, um das Multi-Leg Duty-Briefing durchzugeben.
                Routing heute: {route_string}.
                Tonfall: Kollegial, kompetent. Keine Formatierung, keine Regie-Tags.
                Zahlen zwingend als englische Wörter ausschreiben (two five zero).

                MULTI-LEG FILTERREGELN (NORMAL OPS OMISSION):
                1. Fasse die Route im ersten Satz kurz zusammen.
                2. WEATHER: Ist das Wetter an einer Station gut (Wind <=10kt, Vis >=5000m, keine Phänomene), sage nur: "Wetter in [Station] ist unauffällig."
                3. DELTA FÜR RETURN-FLIGHTS: Kommt die Crew an einen Airport zurück, fasse dich extrem kurz: Nenne nur Wetterverschlechterungen und setze einen Reminder für geschlossene Runways/Taxiways.
                4. NOTAMs: Generell NUR Runways/Taxiway Closures oder ILS Ausfälle erwähnen. 
                5. A320 FLOTTEN-FILTER: Erwähne UNTER KEINEN UMSTÄNDEN Ausfälle von "LPV approaches", "SBAS" oder "GLS". Das Flugzeug kann diese ohnehin nicht fliegen. Verschweige diese NOTAMs strikt!
                6. DELAY-WARNING: Wenn ein NOTAM laut JSON erst nach der geplanten Ankunft aktiv wird, rechne NICHTS aus! Sage einfach: "[NOTAM] ist ab [Uhrzeit] Zulu aktiv. Im Falle einer Verspätung bitte das schriftliche Briefing prüfen."
                
                ROHDATEN:
                WETTER: {all_w}
                NOTAM: {all_n}
                JSON-DATEN (inkl. Zeiten): {deep_data_list}
                """
                
                with st.spinner('🧠 Generiere Text-Briefing und Cloud Audio...'):
                    # Text & Skript Generierung
                    response_text = client.models.generate_content(model='gemini-3.5-flash', contents=prompt_text)
                    briefing_text = response_text.text
                    
                    response_audio = client.models.generate_content(model='gemini-3.5-flash', contents=prompt_audio)
                    audio_script = response_audio.text
                    
                    # Audio Generierung (GCP)
                    audio_bytes = None
                    audio_format = "audio/mp3"
                    fallback_level = "primary"
                    
                    try:
                        tts_url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={gcp_key}"
                        tts_payload = {
                            "input": {"text": audio_script},
                            "voice": {"languageCode": "de-DE", "name": "de-DE-Neural2-F"},
                            "audioConfig": {"audioEncoding": "MP3"}
                        }
                        tts_res = requests.post(tts_url, json=tts_payload)
                        
                        if tts_res.status_code == 200:
                            audio_content = tts_res.json().get("audioContent")
                            if audio_content:
                                audio_bytes = base64.b64decode(audio_content)
                        else:
                            fallback_level = "gtts"
                            st.error(f"⚠️ Diagnose: Google Cloud TTS lehnte die Anfrage ab. HTTP {tts_res.status_code} - {tts_res.text}")
                    except Exception as e:
                        fallback_level = "gtts"
                        st.error(f"⚠️ Diagnose: Verbindungsfehler zu Google Cloud TTS: {e}")
                        
                    # Fallback (Roboter)
                    if fallback_level == "gtts" and gTTS:
                        st.toast("⚠️ Google Cloud API nicht erreichbar. Wechsle auf lokales Backup-System...")
                        try:
                            tts = gTTS(text=audio_script, lang='de', slow=False)
                            fp = BytesIO()
                            tts.write_to_fp(fp)
                            audio_bytes = fp.getvalue()
                        except Exception as fallback_error:
                            st.error(f"Backup-Audiosystem fehlgeschlagen: {fallback_error}")
                
                # --- DATEN IN DIE HISTORY SPEICHERN ---
                timestamp = datetime.now().strftime("%H:%M:%S")
                display_name = f"✈️ {display_flight_names[:15]}... ({timestamp})" if len(display_flight_names) > 15 else f"✈️ {display_flight_names} ({timestamp})"
                
                new_entry = {
                    "display_name": display_name,
                    "route_string": route_string,
                    "dep_icao": route_airports[0],
                    "dest_icao": route_airports[-1],
                    "reg": reg,
                    "briefing_text": briefing_text,
                    "audio_script": audio_script,
                    "audio_bytes": audio_bytes,
                    "audio_format": audio_format,
                    "fallback_level": fallback_level,
                    "all_w": all_w,
                    "all_n": all_n,
                    "combined_osint": combined_osint,
                    "deep_data_list": deep_data_list
                }
                
                st.session_state.history.insert(0, new_entry)
                if len(st.session_state.history) > 5:
                    st.session_state.history = st.session_state.history[:5]
                
                render_briefing_ui(new_entry)

            else:
                st.error(f"Fehler: {deep_data['error']}")
        else:
            st.warning("Bitte gib eine Flugnummer ein.")


```
