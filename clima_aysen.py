import os
import sys
import random
import shutil
import requests
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google import genai
from google.genai import types  # <-- Importante para la nueva versión de la API
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from PIL import Image, ImageEnhance

# Cargamos el entorno y configuramos el cliente de IA
load_dotenv()
cliente = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

LAT, LON = -45.5752, -72.1024
TIMEZONE = "America/Santiago"

DIAS_SEMANA = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

# Apodos de la casa para los niños -- se elige uno al azar en cada corrida
# para que el mensaje no diga siempre "Roco y Milo".
APODOS_NINOS = [
    "Roco y Milo",
    "Roquito y Milito",
    "Coco y Memín",
    "Lomitos flacos",
    "chanchitos perros",
    "potos flacos",
]

# Los 3 horarios reales del día (hora local de Coyhaique). GitHub Actions
# corre este script cada hora (para no pelear con el cambio de horario de
# verano/invierno chileno), y esta tabla decide en qué hora de esas 3 ya
# estamos parados -- o si no corresponde ejecutar nada todavía.
HORARIOS_DIA = {7: "mañana", 13: "tarde", 20: "noche"}


def hora_local_coyhaique():
    return datetime.now(ZoneInfo(TIMEZONE))


def momento_del_dia(hora_local=None):
    """Devuelve 'mañana'/'tarde'/'noche' si la hora local actual (con una
    tolerancia de +/-30 min) coincide con alguno de los 3 horarios de
    HORARIOS_DIA, o None si no corresponde generar nada todavía."""
    hora_local = hora_local or hora_local_coyhaique()
    for hora_objetivo, momento in HORARIOS_DIA.items():
        diferencia_min = abs((hora_local.hour * 60 + hora_local.minute) - hora_objetivo * 60)
        if diferencia_min <= 30:
            return momento
    return None

# ---------------------------------------------------------------------------
# Helpers deterministas (sin IA) para traducir códigos meteorológicos WMO
# ---------------------------------------------------------------------------

def wmo_a_icono(codigo):
    """Traduce el weather_code de Open-Meteo (estándar WMO) a nuestros iconos."""
    if codigo is None:
        return "nubes"
    codigo = int(codigo)
    if codigo == 0:
        return "sol"
    if codigo in (1, 2):
        return "sol"
    if codigo == 3:
        return "nubes"
    if codigo in (45, 48):
        return "nubes"
    if codigo in (71, 73, 75, 77, 85, 86):
        return "nieve"
    if codigo in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99):
        return "lluvia"
    return "nubes"

def elegir_escenario(codigo, temp):
    """Elige la ilustración a mostrar según condición real, no según lo que 'crea' la IA."""
    icono = wmo_a_icono(codigo)
    try:
        temp = float(temp)
    except (TypeError, ValueError):
        temp = 10.0
    if icono in ("lluvia", "nieve"):
        return "lluvia" if icono == "lluvia" else "frio_extremo"
    if temp <= 2:
        return "frio_extremo"
    if temp >= 24:
        return "calor_playa"
    return "despejado_bici"

def grados_a_texto(angulo):
    direcciones = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    try:
        idx = round(float(angulo) / 45) % 8
    except (TypeError, ValueError):
        idx = 0
    return direcciones[idx]

def categoria_uv(uv):
    try:
        uv = float(uv)
    except (TypeError, ValueError):
        return "--"
    if uv < 3:
        return "Bajo"
    if uv < 6:
        return "Moderado"
    if uv < 8:
        return "Alto"
    if uv < 11:
        return "Muy alto"
    return "Extremo"

def categoria_aqi(aqi):
    try:
        aqi = float(aqi)
    except (TypeError, ValueError):
        return None
    if aqi <= 20:
        return "Buena"
    if aqi <= 40:
        return "Aceptable"
    if aqi <= 60:
        return "Moderada"
    if aqi <= 80:
        return "Mala"
    return "Muy mala"

# ---------------------------------------------------------------------------
# 1. Obtención de datos
# ---------------------------------------------------------------------------

def obtener_datos_completos():
    print("⏳ 1. Descargando clima actual de DGAC (Teniente Vidal)...")
    url_dgac = "https://climatologia.meteochile.gob.cl/application/diariob/visorDeDatosEma/450004"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        html_dgac = requests.get(url_dgac, headers=headers, timeout=15).text
        print("✅ Datos DGAC obtenidos.")
    except Exception as e:
        html_dgac = "Error al obtener DGAC"
        print(f"❌ Error DGAC: {e}")

    print("⏳ 2. Descargando pronóstico horario + diario de Open-Meteo...")
    url_om = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&hourly=temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,"
        "wind_speed_10m,wind_direction_10m,relative_humidity_2m,surface_pressure,uv_index,visibility"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset"
        "&current_weather=true"
        f"&timezone={TIMEZONE.replace('/', '%2F')}"
        "&forecast_days=6"
    )

    datos_om = None
    try:
        datos_om = requests.get(url_om, timeout=10).json()
        print("✅ Pronóstico Open-Meteo obtenido.")
    except Exception as e:
        print(f"❌ Error Open-Meteo: {e}")

    print("⏳ 3. Descargando calidad del aire (Open-Meteo Air Quality)...")
    url_aqi = (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={LAT}&longitude={LON}&hourly=european_aqi&timezone={TIMEZONE.replace('/', '%2F')}"
    )
    datos_aqi = None
    try:
        datos_aqi = requests.get(url_aqi, timeout=10).json()
        print("✅ Calidad del aire obtenida.")
    except Exception as e:
        print(f"❌ Error Air Quality: {e}")

    return html_dgac, datos_om, datos_aqi

# ---------------------------------------------------------------------------
# 2. Construcción determinista del panel a partir de Open-Meteo (sin IA)
#    Esto es lo que antes se le pedía "adivinar" a Gemini y terminaba
#    mostrando datos falsos o inventados en el panel.
# ---------------------------------------------------------------------------

def _indice_por_hora(lista_horas, fecha_str, hora_int):
    objetivo = f"{fecha_str}T{hora_int:02d}:00"
    try:
        return lista_horas.index(objetivo)
    except ValueError:
        return None

def construir_condicion_actual(om):
    hourly = om["hourly"]
    ahora = hora_local_coyhaique()
    hoy_str = ahora.strftime("%Y-%m-%d")
    idx_actual = _indice_por_hora(hourly["time"], hoy_str, ahora.hour)
    if idx_actual is None:
        idx_actual = 0

    actual = om.get("current_weather", {})
    temp = actual.get("temperature", hourly["temperature_2m"][idx_actual])
    codigo = actual.get("weathercode", hourly["weather_code"][idx_actual])

    return {
        "temp": round(float(temp), 1),
        "sensacion": round(float(hourly["apparent_temperature"][idx_actual]), 1),
        "icono": wmo_a_icono(codigo),
        "codigo": codigo,
        "humedad": round(float(hourly["relative_humidity_2m"][idx_actual])),
        "presion": round(float(hourly["surface_pressure"][idx_actual])),
        "uv": round(float(hourly["uv_index"][idx_actual]), 1),
        "uv_categoria": categoria_uv(hourly["uv_index"][idx_actual]),
        "visibilidad_km": round(float(hourly["visibility"][idx_actual]) / 1000, 1),
    }

def construir_viento_modelo(om):
    """Viento estimado por el modelo (Open-Meteo). Sirve de respaldo confiable
    si la IA no logra extraer un dato mejor desde la estación DGAC."""
    actual = om.get("current_weather", {})
    velocidad = actual.get("windspeed")
    angulo = actual.get("winddirection", 0)
    if velocidad is None:
        hourly = om["hourly"]
        velocidad = hourly["wind_speed_10m"][0]
        angulo = hourly["wind_direction_10m"][0]
    return {
        "velocidad": f"{round(float(velocidad))} km/h",
        "direccion_texto": grados_a_texto(angulo),
        "angulo": int(round(float(angulo))),
    }

def construir_sol(om):
    hoy = om["daily"]
    amanecer = hoy["sunrise"][0][-5:]
    atardecer = hoy["sunset"][0][-5:]
    return {"amanecer": amanecer, "atardecer": atardecer}

def construir_pronostico_5dias(om):
    daily = om["daily"]
    resultado = []
    # índice 0 es "hoy": mostramos los 5 días siguientes
    for i in range(1, 6):
        fecha = datetime.strptime(daily["time"][i], "%Y-%m-%d")
        resultado.append({
            "dia": DIAS_SEMANA[fecha.weekday()],
            "icono": wmo_a_icono(daily["weather_code"][i]),
            "max": round(float(daily["temperature_2m_max"][i])),
            "min": round(float(daily["temperature_2m_min"][i])),
        })
    return resultado

def construir_pronostico_grafico(om):
    """9 puntos reales cada 3 horas: hoy 10-22h y mañana 01-10h,
    igual que el eje que ya usa el gráfico del panel."""
    hourly = om["hourly"]
    ahora = hora_local_coyhaique()
    hoy_str = ahora.strftime("%Y-%m-%d")
    manana_str = (ahora + timedelta(days=1)).strftime("%Y-%m-%d")

    puntos = [(hoy_str, h) for h in (10, 13, 16, 19, 22)] + [(manana_str, h) for h in (1, 4, 7, 10)]

    labels, temps, probs, precip_mm = [], [], [], []
    for fecha_str, hora in puntos:
        idx = _indice_por_hora(hourly["time"], fecha_str, hora)
        labels.append(f"{hora:02d}")
        if idx is not None:
            temps.append(round(float(hourly["temperature_2m"][idx]), 1))
            probs.append(round(float(hourly["precipitation_probability"][idx])))
            precip_mm.append(round(float(hourly["precipitation"][idx]), 1))
        else:
            temps.append(None)
            probs.append(None)
            precip_mm.append(None)

    return {
        "labels": labels,
        "temperaturas": temps,
        "probabilidad_lluvia": probs,
        "precipitacion_mm": precip_mm,
    }

def construir_calidad_aire(aqi_datos):
    if not aqi_datos:
        return None
    try:
        hourly = aqi_datos["hourly"]
        ahora = hora_local_coyhaique()
        idx = _indice_por_hora(hourly["time"], ahora.strftime("%Y-%m-%d"), ahora.hour) or 0
        valor = hourly["european_aqi"][idx]
        return {"valor": round(float(valor)), "categoria": categoria_aqi(valor)}
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 3. IA: solo para el mensaje cálido y (si puede) el viento real de la estación
#    Ya NO le pedimos a Gemini que "invente" el pronóstico ni la escena: eso
#    ahora es 100% determinista a partir de Open-Meteo.
# ---------------------------------------------------------------------------

def analizar_clima_con_ia(html_dgac, condicion, viento_modelo, momento_dia):
    print("🧠 4. Gemini 2.5 Flash está redactando el mensaje y revisando el viento local...")

    hora_actual = hora_local_coyhaique().strftime("%H:%M")
    apodo = random.choice(APODOS_NINOS)

    if momento_dia == "mañana":
        instruccion_mensaje = (
            "Es la corrida de la MAÑANA: da un consejo positivo sobre cómo abrigarse "
            "para ir al colegio, mencionando el colegio explícitamente."
        )
    else:
        instruccion_mensaje = (
            f"Es la corrida de la {momento_dia.upper()}: NO menciones el colegio "
            "ni prepararse para ir a clases bajo ninguna circunstancia. Adapta el "
            "saludo a esta hora del día y da un consejo para la tarde/noche en familia."
        )

    prompt = f"""
    Eres el asistente de un panel de tinta electrónica familiar en Coyhaique, Aysén.
    La hora actual del sistema es {hora_actual}.

    Datos REALES ya calculados para hoy (no los cambies, son la fuente de verdad):
    - Temperatura actual: {condicion['temp']}°C, sensación {condicion['sensacion']}°C
    - Condición: {condicion['icono']}
    - Viento estimado por modelo: {viento_modelo['velocidad']} desde {viento_modelo['direccion_texto']}

    Fuente adicional (HTML crudo de la estación meteorológica DGAC Teniente Vidal,
    puede tener el viento medido en terreno, más preciso que el modelo):
    {html_dgac}

    Devuelve estrictamente un JSON con esta estructura exacta:
    {{
        "mensaje": "Mensaje cálido dirigiéndote a los niños como '{apodo}' (usa exactamente ese apodo, no lo cambies ni lo traduzcas a otro), usando la temperatura y condición reales de arriba. LÍMITE ESTRICTO: máximo 100 caracteres en total (no líneas, CARACTERES -- cuenta espacios y signos). Es preferible una frase corta y directa a una completa que se pase del límite. {instruccion_mensaje}",
        "viento_actual": {{
            "velocidad": "XX km/h",
            "direccion_texto": "N, NE, E, SE, S, SW, W o NW",
            "angulo": (Número entero entre 0 y 360)
        }}
    }}

    Regla para el viento: si logras extraer un dato claro de velocidad/dirección desde el HTML de DGAC,
    úsalo. Si el HTML no trae esa información o no es confiable, repite exactamente el viento de modelo
    que te di arriba ({viento_modelo['velocidad']} desde {viento_modelo['direccion_texto']}, ángulo {viento_modelo['angulo']}).
    """

    try:
        respuesta = cliente.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2
            )
        )
        resultado_json = json.loads(respuesta.text)
        print("✅ Mensaje generado con éxito.")
        return resultado_json

    except Exception as e:
        print(f"❌ Error con Gemini: {e}")
        return {
            "mensaje": f"Hoy hace {condicion['temp']}°C en Coyhaique. ¡Abríguense bien, {apodo}!",
            "viento_actual": viento_modelo,
        }

# ---------------------------------------------------------------------------
# 4. Ensamblado del JSON final que consume index.html
# ---------------------------------------------------------------------------

def exportar_resultado(resultado):
    nombre_archivo = "clima_exportado.json"
    with open(nombre_archivo, "w", encoding="utf-8") as archivo:
        json.dump(resultado, archivo, indent=4, ensure_ascii=False)

    print("\n--- RESUMEN FINAL ---")
    print(f"Mensaje : {resultado.get('mensaje', '')}")
    print(f"Imagen  : {resultado.get('escenario_imagen', '')}.png")
    v = resultado.get('viento_actual', {})
    print(f"Viento  : {v.get('direccion_texto', '')} a {v.get('velocidad', '')} (Ángulo: {v.get('angulo', '')}°)")
    print(f"Actual  : {resultado.get('condicion_actual', {}).get('temp', '--')}°C")
    print(f"5 días  : {[d['dia'] for d in resultado.get('pronostico_5dias', [])]}")
    print(f"Aire    : {resultado.get('calidad_aire')}")
    print("---------------------\n")

# ---------------------------------------------------------------------------
# 5. Captura + conversión a 1-bit para el ESP32
# ---------------------------------------------------------------------------

ANCHO_PANEL, ALTO_PANEL = 800, 480
FACTOR_SUPERMUESTREO = 3  # renderizamos a 3x y reducimos: texto mucho más nítido
UMBRAL_BLANCO_NEGRO = 150  # 0-255, sobre la imagen YA reducida a tamaño final

def generar_imagen_panel():
    print("📸 5. Tomando fotografía del panel web (con supermuestreo 3x)...")
    url = "http://localhost:8000/index.html"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": ANCHO_PANEL, "height": ALTO_PANEL},
                device_scale_factor=FACTOR_SUPERMUESTREO,
            )

            page.goto(url, wait_until="networkidle")
            # Esperamos a que las fuentes web (Inter, Material Symbols) terminen
            # de cargar de verdad antes de disparar la captura; si no, el texto
            # puede salir con la tipografía de respaldo del sistema, más fea
            # y con métricas distintas (letras cortadas/desalineadas).
            try:
                page.wait_for_function("document.fonts.status === 'ready'", timeout=8000)
            except Exception:
                print("⚠️  Timeout esperando fuentes web, se continúa de todas formas.")
            page.evaluate("document.body.style.overflow = 'hidden';")
            page.screenshot(path="screenshot_temp.png")
            browser.close()

        print("🎨 6. Reduciendo a tamaño real y optimizando para 1-bit...")

        # 1. Cargamos la captura en alta resolución (2400x1440 con factor 3x)
        #    y la reducimos con LANCZOS al tamaño real del panel. Este paso de
        #    "supermuestreo" es lo que da bordes de letra limpios: es mucho
        #    mejor reducir una imagen nítida que binarizar un render 1:1 con
        #    antialiasing crudo del navegador.
        img_hires = Image.open("screenshot_temp.png").convert('L')
        img = img_hires.resize((ANCHO_PANEL, ALTO_PANEL), Image.LANCZOS)

        # 2. Contraste moderado para acentuar negros sin generar halos.
        img_contraste = ImageEnhance.Contrast(img).enhance(1.3)

        # 3. Umbral final. Al venir de un LANCZOS de una imagen ya nítida,
        #    un umbral cercano al punto medio (150) funciona mejor que el
        #    210 anterior, que estaba compensando el ruido del método viejo.
        img_bn = img_contraste.point(lambda p: 255 if p < UMBRAL_BLANCO_NEGRO else 0, mode='L')
        img_final = img_bn.convert('1')

        # OJO: PIL guarda las imágenes modo '1' con una paleta tal que, al
        # abrirlas en un visor normal (GIMP, Windows, feh, etc.), se ven con
        # los colores INVERTIDOS (texto blanco sobre negro) respecto a lo que
        # realmente dibuja el ESP32 con drawBitmap(..., GxEPD_BLACK) sobre un
        # fillScreen(WHITE). En img_bn, el texto ya vale 255 y el fondo 0;
        # invertimos esa escala de grises antes de guardar para que el bmp
        # de verificación sea idéntico a lo que verán tus hijos en el panel.
        from PIL import ImageOps
        ImageOps.invert(img_bn).convert('RGB').save("panel_eink.bmp")

        with open("imagen_hex.h", "w") as f:
            f.write("#include <pgmspace.h>\n")
            f.write("const unsigned char imagen_pantalla[] PROGMEM = {\n")
            hex_data = [f"0x{b:02X}" for b in img_final.tobytes()]
            for i in range(0, len(hex_data), 16):
                f.write(", ".join(hex_data[i:i+16]) + ",\n")
            f.write("};\n")

        # docs/panel.bin: los mismos bytes 1-bit, pero crudos (no texto hex).
        # Esto es lo que GitHub Pages publica y lo que el ESP32 descarga por
        # WiFi con un HTTP GET simple -- 48000 bytes exactos para 800x480 a
        # 1bpp, nada que parsear en el firmware.
        os.makedirs("docs", exist_ok=True)
        with open("docs/panel.bin", "wb") as f:
            f.write(img_final.tobytes())
        shutil.copy("clima_exportado.json", "docs/clima_exportado.json")
        shutil.copy("panel_eink.bmp", "docs/panel_eink.bmp")

        print("✅ ¡Archivos generados!")

        # Copia local solo para el flujo antiguo (USB + PlatformIO en este
        # mismo computador). En GitHub Actions esa ruta no existe -- no es
        # un error, simplemente no aplica en ese entorno.
        ruta_destino = "/home/cmento/Documentos/PlatformIO/Projects/test_pantalla/src/imagen_hex.h"
        if os.path.isdir(os.path.dirname(ruta_destino)):
            shutil.copy("imagen_hex.h", ruta_destino)
            print("🚀 ¡imagen_hex.h copiado y actualizado automáticamente en test_pantalla!")

        if os.path.exists("screenshot_temp.png"):
            os.remove("screenshot_temp.png")

    except Exception as e:
        print(f"❌ Error al generar la imagen: {e}")
        raise

if __name__ == "__main__":
    # GitHub Actions corre este script cada hora (para no pelear con el
    # cambio de horario de verano/invierno chileno). Si no estamos en una
    # ventana de +/-30 min de 7am/1pm/8pm hora Coyhaique, no hacemos nada:
    # ni gastamos la cuota de Gemini ni republicamos el panel de más.
    # "--forzar" (o FORZAR_EJECUCION=1) salta este chequeo para pruebas locales.
    forzar = "--forzar" in sys.argv or os.getenv("FORZAR_EJECUCION") == "1"
    momento = momento_del_dia()
    if momento is None and not forzar:
        print(f"⏭️  Son las {hora_local_coyhaique().strftime('%H:%M')} en Coyhaique, "
              f"no toca ninguno de los 3 horarios (7/13/20h). Saliendo sin hacer nada.")
        raise SystemExit(0)
    momento = momento or "mañana"  # con --forzar y ninguna hora real, usamos 'mañana' de ejemplo

    print(f"🚀 Iniciando el script completo (corrida de {momento})...")
    html_crudo, om, aqi = obtener_datos_completos()

    if om is None:
        raise SystemExit("❌ No se pudo obtener Open-Meteo, no hay datos reales con qué construir el panel.")

    condicion = construir_condicion_actual(om)
    viento_modelo = construir_viento_modelo(om)
    ia = analizar_clima_con_ia(html_crudo, condicion, viento_modelo, momento)

    # Red de seguridad final: si Gemini igual se pasa del límite pedido en
    # el prompt, recortamos acá con puntos suspensivos. Así el layout nunca
    # depende 100% de que la IA "obedezca" -- la caja en el HTML también
    # recorta por su cuenta (overflow:hidden), pero un corte con "…" en una
    # palabra completa se ve mejor que uno crudo a media palabra.
    LARGO_MAXIMO_MENSAJE = 140
    mensaje_ia = ia.get("mensaje", "")
    if len(mensaje_ia) > LARGO_MAXIMO_MENSAJE:
        mensaje_ia = mensaje_ia[:LARGO_MAXIMO_MENSAJE].rsplit(" ", 1)[0] + "…"

    resultado_final = {
        "generado": hora_local_coyhaique().isoformat(timespec="minutes"),
        "mensaje": mensaje_ia,
        "escenario_imagen": elegir_escenario(condicion["codigo"], condicion["temp"]),
        "viento_actual": ia.get("viento_actual", viento_modelo),
        "condicion_actual": condicion,
        "sol": construir_sol(om),
        "pronostico_5dias": construir_pronostico_5dias(om),
        "pronostico_grafico": construir_pronostico_grafico(om),
        "calidad_aire": construir_calidad_aire(aqi),
    }

    exportar_resultado(resultado_final)
    generar_imagen_panel()
