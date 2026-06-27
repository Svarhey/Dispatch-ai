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

# --- GLOBALE RETRY FUNKTION FÜR GEMINI SERVER ERRORS ---
def generate_with_retry(client, model_name, contents, max_retries=3):
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(model=model_name, contents=contents)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))  # Exponential Backoff: 2s, 4s...
            else:
                raise e

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
            return {"success": False, "error": f"ATC-Flugplan für {flight_clean} (noch) nicht im System."}
        
        f_data = res.json()[0]
        result["raw_flight"] = f_data
        dep = f_data.get("departure", {}).get("airport", {}).get("icao")
        dest = f_data.get("arrival", {}).get("airport", {}).get("icao")
        reg = f_data.get("aircraft", {}).get("reg")
        
        if not dep or not dest: 
            return {"success": False, "error": f"Routing unvollständig."}
            
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

# --- METAR / TAF / NOTAM SAMMLER INKL. AMPELSYSTEM ---
def get_airport_raw_data(icao_code, label, all_runways):
    weather_info = f"--- {label}: {icao_code.upper()} ---\n"
    notam_info = f"--- {label}: {icao_code.upper()} ---\n"
    wdir, wspd, metar_raw = None, None, ""
    indicator = "⚪" # Default: Keine Daten
    
    try:
        metar_res = requests.get(f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json").json()
        taf_res = requests.get(f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json").json()
        if metar_res:
            metar_raw = metar_res[0].get('rawOb', '')
            weather_info += f"METAR: {metar_raw}\n"
            wdir = metar_res[0].get("wdir")
            wspd = metar_res[0].get("wspd")
            
            # --- AMPELSYSTEM LOGIK (Flight Category) ---
            cat = metar_res[0].get('fltcat', '')
            if cat == 'VFR': indicator = "🟢"
            elif cat == 'MVFR': indicator = "🟡"
            elif cat == 'IFR': indicator = "🟠"
            elif cat == 'LIFR': indicator = "🔴"

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
        
    return weather_info, notam_info, indicator

# --- ZENTRALE RENDER-FUNKTION FÜR DAS BRIEFING ---
def render_briefing_ui(data):
    # Fallback für alte History-Einträge ohne "route_string_display"
    display_route = data.get('route_string_display', data.get('route_string', 'N/A'))
    
    st.success(f"✈️ Flugplan aktiv: {display_route} | Aircraft Tail: {data['reg']}")
    st.markdown("---")
    
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

if "history" not in st.session_state:
    st.session_state.history = []

with st.sidebar:
    st.header("🗂️ Dispatch Log")
    st.write("Schnellzugriff auf archivierte Briefings.")
    options = ["📝 Neues Briefing erstellen"] + [h["display_name"] for h in st.session_state.history]
    view_mode = st.radio("Modus auswählen:", options)

st.title("✈️ Dispatch-AI")
st.subheader("Professional AI-Powered Duty-Briefing (Multi-Leg)")

gemini_key = st.secrets.get("GEMINI_API_KEY")
gcp_key = st.secrets.get("GCP_API_KEY")
rapid_key = st.secrets.get("RAPIDAPI_KEY")

if not gemini_key or not gcp_key:
    st.error("🔒 Bitte hinterlege GEMINI_API_KEY und GCP_API_KEY in den Streamlit Secrets.")
    st.stop()

client = genai.Client(api_key=gemini_key)

if view_mode != "📝 Neues Briefing erstellen":
    st.info("🕒 **Archiv-Ansicht:** Dieses Briefing wurde aus dem lokalen Cache geladen.")
    selected_data = next(item for item in st.session_state.history if item["display_name"] == view_mode)
    render_briefing_ui(selected_data)

else:
    st.markdown("Gib bis zu 5 Flugnummern in chronologischer Reihenfolge ein.")
    
    cols = st.columns(5)
    flight_inputs = []
    for i in range(5):
        f_in = cols[i].text_input(f"Leg {i+1} (z.B. LH149):", key=f"leg_input_{i}").upper().strip()
        if f_in:
            flight_inputs.append(f_in)
            
    flight_date = st.date_input("Flugdatum:", datetime.now().date())
    
    st.markdown("---")
    st.markdown("**Notfall-Routing (Falls ATC-Plan fehlt):**")
    manual_route = st.text_input("Wenn die API Flüge nicht findet (z.B. später Rückflug), gib hier fehlende Airports ein (z.B. 'EDDF LEMD EDDF'):", placeholder="ICAO Codes mit Leerzeichen trennen")

    with st.expander("➕ Optionale Alternates hinzufügen"):
        a1, a2, a3, a4 = st.columns(4)
        altns = [
            a1.text_input("ALTN 1", key="a1").upper(), 
            a2.text_input("ALTN 2", key="a2").upper(), 
            a3.text_input("ALTN 3", key="a3").upper(), 
            a4.text_input("ALTN 4", key="a4").upper()
        ]

    if st.button("Duty-Briefing erstellen"):
        if not flight_inputs and not manual_route:
            st.warning("Bitte gib mindestens eine Flugnummer oder ein Notfall-Routing ein.")
        else:
            all_runways = load_runway_database()
            
            deep_data_list = []
            route_airports = []
            successful_flights = []
            reg = "N/A"
            
            with st.spinner("Lade Telemetriedaten für alle Legs..."):
                for fn in flight_inputs:
                    d_data = fetch_deep_flight_data(fn, flight_date, rapid_key) if rapid_key else {"success": False, "error": "Kein API Key"}
                    
                    if not d_data["success"]:
                        st.warning(f"⚠️ {fn} übersprungen: {d_data['error']} (Nutze das Notfall-Routing-Feld, falls Stationen in der Route fehlen!)")
                        continue
                    
                    successful_flights.append(fn)
                    deep_data_list.append(d_data)
                    f_raw = d_data["raw_flight"]
                    dep_icao = f_raw.get("departure", {}).get("airport", {}).get("icao")
                    dest_icao = f_raw.get("arrival", {}).get("airport", {}).get("icao")
                    
                    if len(route_airports) == 0:
                        route_airports.append(dep_icao)
                    elif route_airports[-1] != dep_icao:
                        route_airports.append(dep_icao)
                    
                    route_airports.append(dest_icao)
                    
                    if reg == "N/A" and f_raw.get("aircraft", {}).get("reg"):
                        reg = f_raw.get("aircraft", {}).get("reg")

            if manual_route:
                manual_airports = [code.upper() for code in manual_route.split()]
                for ma in manual_airports:
                    if len(route_airports) == 0 or route_airports[-1] != ma:
                        route_airports.append(ma)

            if route_airports:
                display_flight_names = ", ".join(successful_flights) if successful_flights else "Manuelles Routing"
                route_string = " ➡️ ".join(route_airports)
                current_utc_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                
                unique_airports = list(dict.fromkeys(route_airports + [a for a in altns if a]))
                
                all_w = ""
                all_n = ""
                combined_osint = ""
                airport_indicators = {}
                
                with st.spinner("Scanne weltweite Wetterdatenbänke, OSINT und FAA NOTAM-Server..."):
                    for code in unique_airports:
                        if code:
                            city = get_airport_city(code)
                            osint_res = search_city_events(city, flight_date)
                            combined_osint += f"{osint_res}\n\n"
                            
                            w, n, ind = get_airport_raw_data(code, "AIRPORT", all_runways)
                            all_w += w + "\n"
                            all_n += n + "\n"
                            airport_indicators[code] = ind

                # Formatierte Route inkl. Ampelsystem für die Anzeige generieren
                route_with_indicators = []
                for apt in route_airports:
                    route_with_indicators.append(f"{apt} {airport_indicators.get(apt, '⚪')}")
                route_string_display = " ➡️ ".join(route_with_indicators)

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
                Im JSON ('deep_data_list') findest du (falls Telemetrie verfügbar) die 'scheduledTimeUtc'. 
                Wenn ein kritisches NOTAM (z.B. Runway Closure) erst NACH unserer planmäßigen Ankunftszeit aktiv wird:
                - Setze eine deutliche Warnung: "⚠️ ACHTUNG: [NOTAM-Inhalt] aktiv ab [Uhrzeit]Z. Bei Verspätung zwingend prüfen!"
                
                DIE "DELTA-REGEL" FÜR RÜCKFLÜGE:
                Wenn ein Flughafen auf der Route ZUM ZWEITEN MAL (oder öfter) angeflogen wird:
                - WETTER: Briefe das Wetter für den späteren Zeitpunkt als ÄNDERUNG (Delta) zum Vormittag.
                - NOTAMs: Ignoriere Standard-NOTAMs beim zweiten Besuch. ABER: Wiederhole zwingend kurz alle kritischen "Killer-NOTAMs" als "Reminder".
                
                ROHDATEN ALLER STATIONEN:
                WETTER: {all_w}
                NOTAM: {all_n}
                OSINT: {combined_osint}
                JSON-DATEN: {deep_data_list}
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
                5. A320 FLOTTEN-FILTER: Erwähne UNTER KEINEN UMSTÄNDEN Ausfälle von "LPV approaches", "SBAS" oder "GLS".
                
                ROHDATEN:
                WETTER: {all_w}
                NOTAM: {all_n}
                JSON-DATEN (inkl. Zeiten): {deep_data_list}
                """
                
                with st.spinner('🧠 Generiere Text-Briefing und Cloud Audio...'):
                    try:
                        # Aufruf mit der neuen Retry-Funktion!
                        response_text = generate_with_retry(client, 'gemini-3.5-flash', prompt_text)
                        briefing_text = response_text.text
                        
                        response_audio = generate_with_retry(client, 'gemini-3.5-flash', prompt_audio)
                        audio_script = response_audio.text
                        
                    except Exception as e:
                        st.error(f"⚠️ Abbruch: Google Gemini API ServerError nach mehreren Versuchen. Server überlastet. Detail: {e}")
                        st.stop()
                    
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
                        
                    if fallback_level == "gtts" and gTTS:
                        st.toast("⚠️ Google Cloud API nicht erreichbar. Wechsle auf lokales Backup-System...")
                        try:
                            tts = gTTS(text=audio_script, lang='de', slow=False)
                            fp = BytesIO()
                            tts.write_to_fp(fp)
                            audio_bytes = fp.getvalue()
                        except Exception as fallback_error:
                            st.error(f"Backup-Audiosystem fehlgeschlagen: {fallback_error}")
                
                timestamp = datetime.now().strftime("%H:%M:%S")
                display_name = f"✈️ {display_flight_names[:15]}... ({timestamp})" if len(display_flight_names) > 15 else f"✈️ {display_flight_names} ({timestamp})"
                
                new_entry = {
                    "display_name": display_name,
                    "route_string": route_string,
                    "route_string_display": route_string_display, # NEU: Für das UI gespeichert
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
                st.error("Routenberechnung fehlgeschlagen. Bitte prüfe die Eingaben.")
