import requests
import streamlit as st
import math
import csv
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
    except Exception as e:
        st.error("Konnte weltweite Runway-Datenbank nicht laden.")
        return []

def get_airport_data(icao_code, label, all_runways):
    """Wetter, unlimitierte NOTAMs und automatische Runway-Analyse"""
    metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao_code}&format=json"
    taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao_code}&format=json"
    
    st.markdown(f"### 📍 {label}: {icao_code.upper()}")
    
    try:
        # 1. METAR Logik & CROSSWIND CALCULATOR
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
                
            # AUTOMATISCHER RUNWAY-SCANNER
            wdir = metar_data[0].get("wdir")
            wspd = metar_data[0].get("wspd")
            wgst = metar_data[0].get("wgst") 
            
            if wdir and wspd and isinstance(wdir, (int, float)):
                st.markdown("**🌬️ Automatische Runway Wind-Analyse**")
                
                airport_runways = [r for r in all_runways if r['airport_ident'].upper() == icao_code.upper()]
                
                if not airport_runways:
                    st.warning("Keine Infrastruktur-Daten für diesen ICAO-Code in der Datenbank gefunden.")
                else:
                    results = []
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
                                
                                results.append({
                                    "rwy": rwy_id,
                                    "headwind": headwind,
                                    "crosswind": crosswind,
                                    "hdg": rwy_hdg
                                })
                    
                    results = sorted(results, key=lambda x: x['headwind'], reverse=True)
                    
                    for res in results:
                        hw = res['headwind']
                        cw = res['crosswind']
                        
                        hw_str = f"⬇️ Head: {abs(hw):.1f} kt" if hw >= 0 else f"⬆️ Tail: {abs(hw):.1f} kt"
                        cw_dir = "v. Rechts" if cw > 0 else "v. Links"
                        cw_str = f"⬅️ Cross: {abs(cw):.1f} kt ({cw_dir})"
                        
                        if hw >= 0:
                            st.success(f"**RWY {res['rwy']}** | {hw_str} | {cw_str}")
                        else:
                            st.error(f"**RWY {res['rwy']}** | {hw_str} | {cw_str}")
                            
                    if wgst:
                         st.warning(f"⚠️ Böenwarnung (Gusts bis {wgst} kt)! Addiere Böen-Faktor auf das Final Approach Speed gemäß FCOM.")
                         
            elif wdir == "VRB":
                st.info("🌬️ Wind ist variabel (VRB). Crosswind-Berechnung nicht möglich.")

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
            
        # 3. NOTAM Logik (FAA Direct Request - Unlimitiert)
        st.info("📋 Aktuelle NOTAMs:")
        
        try:
            session = requests.Session()
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            session.get("https://notams.aim.faa.gov/notamSearch/", headers=headers, timeout=5)
            
            faa_url = "https://notams.aim.faa.gov/notamSearch/search"
            payload = {"searchType": 0, "designatorsForLocation": icao_code.upper()}
            
            notam_res = session.post(faa_url, data=payload, headers=headers, timeout=10)
            
            if notam_res.status_code == 200:
                notam_data = notam_res.json()
                if "error" in notam_data and notam_data["error"] != "":
                    st.warning(f"FAA System meldet: {notam_data['error']}")
                elif "notamList" in notam_data and len(notam_data["notamList"]) > 0:
                    notams = notam_data["notamList"]
                    
                    # NEU: Aufklappbares Menü für alle NOTAMs, um das Layout sauber zu halten
                    with st.expander(f"Alle {len(notams)} aktiven NOTAMs anzeigen", expanded=False):
                        for notam in notams: # Limitierung entfernt!
                            st.code(notam.get("icaoMessage", notam.get("traditionalMessage", "")), language="text")
                else:
                    st.success("Keine aktiven NOTAMs für diesen Platz gefunden.")
            else:
                st.error(f"Fehler beim NOTAM-Abruf via FAA.")
        except Exception as e:
            st.error("Verbindungsfehler zur FAA-Datenbank.")
            
        st.markdown("---")

    except Exception as e:
        st.error(f"Fehler bei Abfrage {icao_code}: {e}")

# --- STREAMLIT UI OBERFLÄCHE ---

st.set_page_config(page_title="Dispatch-AI", page_icon="✈️", layout="wide")

st.title("✈️ Dispatch-AI")
st.subheader("Professional Pre-Flight Briefing Tool")

all_runways = load_runway_database()

col1, col2, col3 = st.columns(3)

with col1:
    dep_icao = st.text_input("Departure (DEP) *Pflichtfeld*:", max_chars=4, placeholder="z.B. EDDB").upper()

with col2:
    dest_icao = st.text_input("Destination (DEST) *Optional*:", max_chars=4, placeholder="z.B. EDDF").upper()

with col3:
    altn_icao = st.text_input("Alternate (ALTN) *Optional*:", max_chars=4, placeholder="z.B. EDDS").upper()

if st.button("Briefing erstellen"):
    if dep_icao:
        get_airport_data(dep_icao, "DEPARTURE", all_runways)
        if dest_icao:
            get_airport_data(dest_icao, "DESTINATION", all_runways)
        if altn_icao:
            get_airport_data(altn_icao, "ALTERNATE (ALTN)", all_runways)
    else:
        st.warning("Bitte gib mindestens den Departure Airport (DEP) ein, um die Abfrage zu starten.")
