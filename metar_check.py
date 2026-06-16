import requests
import streamlit as st
from datetime import datetime, timezone, timedelta

def get_airport_data(icao_code, label):
    """Wetter über AviationWeather.gov, NOTAMs direkt über die US FAA (mit Session-Bypass)"""
    metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json"
    taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json"
    
    st.markdown(f"### 📍 {label}: {icao_code.upper()}")
    
    try:
        # 1. METAR Logik
        metar_res = requests.get(metar_url)
        metar_data = metar_res.json()
        
        if len(metar_data) > 0:
            metar_raw = metar_data[0].get("rawOb")
            obs_time_raw = metar_data[0].get("obsTime")
            obs_time_dt = datetime.fromtimestamp(obs_time_raw, tz=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            age = now_utc - obs_time_dt
            
            if age > timedelta(hours=3):
                st.error(f"⚠️ METAR VERWORFEN (Alter: {int(age.total_seconds() // 3600)}h)")
                st.code(metar_raw, language="text")
            else:
                st.success(f"✅ METAR aktuell (Beobachtung vor {int(age.total_seconds() // 60)} Min)")
                st.code(metar_raw, language="text")
        else:
            st.warning(f"Kein METAR für {icao_code} verfügbar.")

        # 2. TAF Logik
        taf_res = requests.get(taf_url)
        taf_data = taf_res.json()
        if len(taf_data) > 0:
            st.info("🔮 TAF:")
            st.code(taf_data[0].get("rawTAF"), language="text")
        else:
            st.info(f"Kein TAF für {icao_code} publiziert.")
            
        # 3. NOTAM Logik (NEU: FAA Direct Request mit Session-Cookie Bypass)
        st.info("📋 NOTAMs (Auszug der ersten 5 Meldungen):")
        
        try:
            # Wir erstellen eine "Session", damit Python sich Cookies merken kann
            session = requests.Session()
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            # Schritt 1: Wir besuchen die Hauptseite, um das Cookie abzugreifen
            session.get("https://notams.aim.faa.gov/notamSearch/", headers=headers, timeout=5)
            
            # Schritt 2: Die eigentliche Abfrage mit dem neuen Cookie
            faa_url = "https://notams.aim.faa.gov/notamSearch/search"
            payload = {
                "searchType": 0,
                "designatorsForLocation": icao_code.upper()
            }
            
            notam_res = session.post(faa_url, data=payload, headers=headers, timeout=10)
            
            if notam_res.status_code == 200:
                notam_data = notam_res.json()
                
                # Wenn der Server ein echtes Error-Feld schickt (und es nicht leer ist!)
                if "error" in notam_data and notam_data["error"] != "":
                    st.warning(f"FAA System meldet: {notam_data['error']}")
                elif "notamList" in notam_data and len(notam_data["notamList"]) > 0:
                    notams = notam_data["notamList"]
                    for notam in notams[:5]:
                        notam_text = notam.get("icaoMessage", notam.get("traditionalMessage", "Fehler: Text nicht auslesbar."))
                        st.code(notam_text, language="text")
                    
                    if len(notams) > 5:
                        st.caption(f"... und {len(notams) - 5} weitere NOTAMs aktiv. (Auf 5 limitiert in der UI)")
                else:
                    st.success("Keine aktiven NOTAMs für diesen Platz gefunden.")
            else:
                st.error(f"Fehler beim NOTAM-Abruf via FAA. (HTTP Code: {notam_res.status_code})")
                
        except Exception as e:
            st.error(f"Verbindungsfehler zur FAA-Datenbank: {e}")
            
        st.markdown("---")

    except Exception as e:
        st.error(f"Fehler bei Abfrage {icao_code}: {e}")

# --- STREAMLIT UI OBERFLÄCHE ---

st.set_page_config(page_title="Dispatch-AI", page_icon="✈️", layout="wide")

st.title("✈️ Dispatch-AI")
st.subheader("Professional Pre-Flight Briefing Tool")

col1, col2, col3 = st.columns(3)

with col1:
    dep_icao = st.text_input("Departure (DEP) *Pflichtfeld*:", max_chars=4, placeholder="z.B. EDDB").upper()

with col2:
    dest_icao = st.text_input("Destination (DEST) *Optional*:", max_chars=4, placeholder="z.B. EDDF").upper()

with col3:
    altn_icao = st.text_input("Alternate (ALTN) *Optional*:", max_chars=4, placeholder="z.B. EDDS").upper()

if st.button("Briefing erstellen"):
    if dep_icao:
        get_airport_data(dep_icao, "DEPARTURE")
        if dest_icao:
            get_airport_data(dest_icao, "DESTINATION")
        if altn_icao:
            get_airport_data(altn_icao, "ALTERNATE (ALTN)")
    else:
        st.warning("Bitte gib mindestens den Departure Airport (DEP) ein, um die Abfrage zu starten.")