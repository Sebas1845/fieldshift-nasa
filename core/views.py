import os
import io
import json
import base64
import requests
from PIL import Image, ImageDraw, ImageOps

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

from .models import (
    ComentarioProyecto,
    ConsultaTerreno,
    PlanSiembra,
    EscaneoPlanta,
    PlanRecuperacion,
)
from .nasa_service import obtener_datos_oficiales_terreno

# TU LLAVE REAL DE GROQ CONFIGURADA
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

def es_usuario_invitado(request):
    """Determina si el visitante actual está en Modo Invitado (no autenticado)."""
    return not request.user.is_authenticated


def acceso_view(request):
    # Si el usuario pulsa "Entrar como Invitado", cierra cualquier sesión previa y entra como invitado
    if request.GET.get("modo") == "invitado":
        logout(request)
        return redirect("index")
    if request.user.is_authenticated:
        return redirect("index")
    return render(request, "login.html")


def index_view(request):
    """
    Acceso público directo (cumple requisito de NASA Space Apps de no exigir clave para la demo).
    Si el usuario no ha iniciado sesión, entra en Modo Invitado (es_invitado=True) sin historial.
    """
    es_invitado = es_usuario_invitado(request)
    return render(request, "index.html", {"es_invitado": es_invitado})


def registro_view(request):
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()

        if not username or not password:
            return render(request, "login.html", {"error": "Todos los campos son obligatorios."})
        if User.objects.filter(username=username).exists():
            return render(request, "login.html", {"error": "Ese nombre de usuario ya está en uso."})

        user = User.objects.create_user(username=username, password=password)
        login(request, user)
        return redirect("index")
    return redirect("acceso")


def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect("index")
        return render(request, "login.html", {"error": "Usuario o contraseña incorrectos."})
    return redirect("acceso")


def logout_view(request):
    logout(request)
    return redirect("acceso")


def limpiar_json_ia(texto):
    """Extrae y limpia el bloque JSON de la respuesta de Groq."""
    texto = texto.strip()
    if "```" in texto:
        partes = texto.split("```")
        for p in partes:
            p_limpio = p.strip()
            if p_limpio.startswith("json"):
                p_limpio = p_limpio[4:].strip()
            if p_limpio.startswith("{") and p_limpio.endswith("}"):
                return json.loads(p_limpio)
    inicio = texto.find("{")
    fin = texto.rfind("}")
    if inicio != -1 and fin != -1:
        return json.loads(texto[inicio:fin + 1])
    return json.loads(texto)


def listar_modelos_cuenta_groq(headers):
    """Devuelve la lista exacta de modelos habilitados en tu cuenta de Groq."""
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=10)
        if r.status_code == 200:
            return [m["id"] for m in r.json().get("data", [])]
    except Exception as e:
        print("Aviso al consultar modelos Groq:", e)
    return []


def obtener_modelo_groq_activo(headers, es_vision=False):
    """Selecciona únicamente modelos que existan realmente en tu cuenta de Groq para evitar HTTP 404."""
    modelos = listar_modelos_cuenta_groq(headers)
    if es_vision:
        candidatos_vision = [
            m for m in modelos
            if any(k in m.lower() for k in ["vision", "scout", "maverick", "llava", "pixtral"])
        ]
        if candidatos_vision:
            return candidatos_vision[0]
        return None

    preferidos_texto = [
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
        "qwen/qwen3-32b",
        "llama-3.1-8b-instant",
        "gemma2-9b-it",
    ]
    for pref in preferidos_texto:
        if pref in modelos:
            return pref
    for m in modelos:
        if not any(x in m.lower() for x in ["whisper", "guard", "tts", "embed"]):
            return m
    return "openai/gpt-oss-120b"


def analizar_imagen_vegetal_cv(imagen_file):
    """
    Escáner de Visión por Computador (Pillow):
    1. Descarta capturas de pantalla (WhatsApp, chats, páginas web) midiendo píxeles digitales planos
       y diferenciando la clorofila real de los colores pastel de interfaz.
    2. Escanea la hoja en una cuadrícula de 16x16 para localizar las coordenadas REALES [x1, y1, x2, y2]
       donde están las manchas cafés (necrosis) y amarillas (clorosis).
    """
    imagen_file.seek(0)
    img = Image.open(imagen_file)
    img = ImageOps.exif_transpose(img).convert("RGB")
    imagen_file.seek(0)

    W, H = 160, 160
    img_small = img.resize((W, H))
    pix = img_small.load()

    total_pixeles = W * H
    pixeles_clorofila = 0
    pixeles_necrosis = 0
    pixeles_clorosis = 0
    pixeles_planos_digitales = 0
    pixeles_blanco_interfaz = 0

    G_SIZE = 16
    celda_w = W // G_SIZE
    celda_h = H // G_SIZE
    grid_verde = [[0] * G_SIZE for _ in range(G_SIZE)]
    grid_necrosis = [[0] * G_SIZE for _ in range(G_SIZE)]
    grid_clorosis = [[0] * G_SIZE for _ in range(G_SIZE)]

    for y in range(H):
        gy = min(G_SIZE - 1, y // celda_h)
        for x in range(W):
            gx = min(G_SIZE - 1, x // celda_w)
            r, g, b = pix[x, y]

            if x > 0:
                r_prev, g_prev, b_prev = pix[x - 1, y]
                if abs(r - r_prev) + abs(g - g_prev) + abs(b - b_prev) <= 2:
                    pixeles_planos_digitales += 1

            if r > 185 and g > 185 and b > 170:
                pixeles_blanco_interfaz += 1
                continue

            if 35 <= g <= 215 and (g - r) >= 14 and (g - b) >= 18 and r < 175 and b < 150:
                pixeles_clorofila += 1
                grid_verde[gy][gx] += 1
            elif r > 115 and g > 95 and b < 95 and (g - b) >= 30 and abs(r - g) <= 45:
                pixeles_clorosis += 1
                grid_clorosis[gy][gx] += 1
            elif 55 <= r <= 185 and 30 <= g <= 140 and b < 90 and (r - g) >= 12 and (g - b) >= 12:
                pixeles_necrosis += 1
                grid_necrosis[gy][gx] += 1

    pct_clorofila = round((pixeles_clorofila / total_pixeles) * 100, 1)
    pct_clorosis = round((pixeles_clorosis / total_pixeles) * 100, 1)
    pct_necrosis = round((pixeles_necrosis / total_pixeles) * 100, 1)
    pct_plano_digital = round((pixeles_planos_digitales / total_pixeles) * 100, 1)
    pct_interfaz = round((pixeles_blanco_interfaz / total_pixeles) * 100, 1)

    es_planta_valida = True
    motivo_rechazo = ""
    if pct_clorofila < 3.5:
        es_planta_valida = False
        motivo_rechazo = (
            f"🚫 Imagen rechazada: Solo se detectó {pct_clorofila}% de clorofila vegetal orgánica. "
            "La imagen corresponde a una captura de pantalla, chat u objeto que no es una planta."
        )
    elif pct_plano_digital > 55.0 and pct_interfaz > 50.0 and pct_clorofila < 12.0:
        es_planta_valida = False
        motivo_rechazo = (
            f"🚫 Captura de pantalla detectada ({pct_plano_digital}% de píxeles digitales planos). "
            "Por favor sube una fotografía real de una hoja, planta o cultivo."
        )

    candidatos_dano = []
    for gy in range(G_SIZE):
        for gx in range(G_SIZE):
            tiene_vecino_verde = False
            for dy in (-2, -1, 0, 1, 2):
                for dx in (-2, -1, 0, 1, 2):
                    ny, nx = gy + dy, gx + dx
                    if 0 <= ny < G_SIZE and 0 <= nx < G_SIZE and grid_verde[ny][nx] >= 8:
                        tiene_vecino_verde = True
                        break
            if not tiene_vecino_verde:
                continue

            pts_nec = grid_necrosis[gy][gx]
            pts_clo = grid_clorosis[gy][gx]
            puntaje = (pts_nec * 1.4) + (pts_clo * 1.1)
            if puntaje >= 12:
                tipo = (
                    "Necrosis / Tejido seco foliar"
                    if pts_nec >= pts_clo
                    else "Clorosis / Amarillamiento foliar"
                )
                candidatos_dano.append((puntaje, gx, gy, tipo))

    candidatos_dano.sort(reverse=True, key=lambda x: x[0])
    zonas_reales = []
    celdas_usadas = []

    for puntaje, gx, gy, tipo in candidatos_dano:
        if any(abs(gx - ux) <= 3 and abs(gy - uy) <= 3 for ux, uy in celdas_usadas):
            continue
        celdas_usadas.append((gx, gy))

        x1 = round(max(0.04, (gx - 1.5) / G_SIZE), 3)
        y1 = round(max(0.06, (gy - 1.5) / G_SIZE), 3)
        x2 = round(min(0.94, (gx + 2.5) / G_SIZE), 3)
        y2 = round(min(0.94, (gy + 2.5) / G_SIZE), 3)
        confianza = round(min(0.98, 0.82 + (puntaje / 150.0)), 2)

        zonas_reales.append({
            "etiqueta": tipo,
            "confianza": confianza,
            "box": [x1, y1, x2, y2]
        })
        if len(zonas_reales) >= 3:
            break

    if es_planta_valida and not zonas_reales:
        xs_verdes = [gx for gy in range(G_SIZE) for gx in range(G_SIZE) if grid_verde[gy][gx] >= 15]
        ys_verdes = [gy for gy in range(G_SIZE) for gx in range(G_SIZE) if grid_verde[gy][gx] >= 15]
        if xs_verdes and ys_verdes:
            cx = (min(xs_verdes) + max(xs_verdes)) / 2.0
            cy = (min(ys_verdes) + max(ys_verdes)) / 2.0
            zonas_reales.append({
                "etiqueta": "Lámina foliar inspeccionada",
                "confianza": 0.95,
                "box": [
                    round(max(0.08, (cx - 2.5) / G_SIZE), 2),
                    round(max(0.08, (cy - 2.5) / G_SIZE), 2),
                    round(min(0.92, (cx + 2.5) / G_SIZE), 2),
                    round(min(0.92, (cy + 2.5) / G_SIZE), 2),
                ]
            })

    return {
        "es_planta_valida": es_planta_valida,
        "motivo_rechazo": motivo_rechazo,
        "telemetria_optica": {
            "clorofila_sana_pct": pct_clorofila,
            "clorosis_amarillamiento_pct": pct_clorosis,
            "necrosis_tejido_seco_pct": pct_necrosis,
            "focos_detectados": len(zonas_reales),
        },
        "zonas_dano_cv": zonas_reales
    }


def dibujar_recuadros_dano(ruta_imagen_original, zonas_dano):
    """
    Dibuja recuadros en las coordenadas exactas detectadas y añade lupas circulares
    conectadas con líneas punteadas al estilo de infografía agronómica.
    """
    try:
        img = Image.open(ruta_imagen_original)
        img = ImageOps.exif_transpose(img).convert("RGB")
        ancho, alto = img.size
        draw = ImageDraw.Draw(img)
        grosor = max(3, int(min(ancho, alto) * 0.006))
        radio_lupa = int(min(ancho, alto) * 0.11)

        for idx, zona in enumerate(zonas_dano[:3]):
            box = zona.get("box")
            if not box or len(box) != 4:
                continue

            x1 = max(10, min(ancho - 30, int(float(box[0]) * ancho)))
            y1 = max(25, min(alto - 30, int(float(box[1]) * alto)))
            x2 = max(x1 + 35, min(ancho - 10, int(float(box[2]) * ancho)))
            y2 = max(y1 + 35, min(alto - 10, int(float(box[3]) * alto)))

            draw.rectangle([x1, y1, x2, y2], outline="#ff3232", width=grosor)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill="#ffffff", outline="#ff3232", width=2)

            if radio_lupa >= 28:
                recorte = img.crop((x1, y1, x2, y2)).resize((radio_lupa * 2, radio_lupa * 2))
                mascara = Image.new("L", (radio_lupa * 2, radio_lupa * 2), 0)
                ImageDraw.Draw(mascara).ellipse((0, 0, radio_lupa * 2, radio_lupa * 2), fill=255)

                lx = ancho - (radio_lupa * 2) - 18
                ly = 24 + idx * (radio_lupa * 2 + 22)
                if ly + (radio_lupa * 2) < alto - 10:
                    pasos = 18
                    for p in range(pasos):
                        if p % 2 == 0:
                            px1 = int(cx + (lx - cx) * (p / pasos))
                            py1 = int(cy + (ly + radio_lupa - cy) * (p / pasos))
                            px2 = int(cx + (lx - cx) * ((p + 1) / pasos))
                            py2 = int(cy + (ly + radio_lupa - cy) * ((p + 1) / pasos))
                            draw.line([(px1, py1), (px2, py2)], fill="#ffffff", width=max(2, grosor - 1))

                    img.paste(recorte, (lx, ly), mascara)
                    draw.ellipse([lx, ly, lx + radio_lupa * 2, ly + radio_lupa * 2], outline="#00e5ff", width=grosor + 1)

            conf_pct = int(float(zona.get("confianza", 0.93)) * 100)
            texto_etiqueta = f" {idx + 1}. {zona.get('etiqueta', 'Lesion')[:24]} ({conf_pct}%) "
            y_fondo = max(0, y1 - 22)
            draw.rectangle([x1, y_fondo, min(ancho, x1 + len(texto_etiqueta) * 7), y1], fill="#ff3232")
            draw.text((x1 + 4, y_fondo + 4), texto_etiqueta, fill="#ffffff")

        directorio, nombre_archivo = os.path.split(ruta_imagen_original)
        nombre_base, _ = os.path.splitext(nombre_archivo)
        nombre_anotado = f"anotada_{nombre_base}.jpg"
        ruta_anotada = os.path.join(directorio, nombre_anotado)

        img.save(ruta_anotada, "JPEG", quality=92)
        return f"/media/hojas_escaneadas/{nombre_anotado}"
    except Exception as e:
        print("Error al dibujar infografía en Pillow:", e)
        return None


def llamar_ia_json(prompt, datos_nasa=None, area_ha=1.0, enfoque="AUTO"):
    if GROQ_API_KEY and "PEGA_AQUI" not in GROQ_API_KEY:
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "GeoSync-FieldShift-NASA/1.0"
            }
            modelo_activo = obtener_modelo_groq_activo(headers, es_vision=False)

            payload = {
                "model": modelo_activo,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Eres un ingeniero agrónomo, zootecnista y ambientalista experto de la misión GeoSync. "
                            "Responde SIEMPRE en español y ÚNICAMENTE con un objeto JSON válido."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.35
            }
            r = requests.post(url, headers=headers, json=payload, timeout=25)
            if r.status_code != 200:
                detalle = r.json().get("error", {}).get("message", r.text)
                raise ValueError(f"HTTP {r.status_code}: {detalle}")

            texto = r.json()["choices"][0]["message"]["content"]
            resultado = limpiar_json_ia(texto)
            resultado["generado_por_ia"] = True
            resultado["fuente_motor"] = f"🤖 Sugerido por IA Sintética en Vivo (Groq: {modelo_activo})"
            return resultado
        except Exception as e:
            print("Detalle Error Groq Texto:", e)

    return {"generado_por_ia": False, "fuente_motor": "⚙️ Error de conexión con Groq", "cultivos_recomendados": []}


def llamar_ia_vision_json(imagen_file, prompt):
    analisis_cv = analizar_imagen_vegetal_cv(imagen_file)
    if not analisis_cv["es_planta_valida"]:
        return {
            "generado_por_ia": True,
            "fuente_motor": "🛡️ Escáner Óptico de Clorofila GeoSync (Imagen Rechazada)",
            "es_hoja_valida": False,
            "pertenece_al_sistema": False,
            "planta_detectada": "NO ES UNA PLANTA",
            "mensaje": analisis_cv["motivo_rechazo"],
            "diagnostico": "",
            "severidad": "N/A",
            "zonas_dano": [],
            "plan_recuperacion": []
        }

    zonas_reales_cv = analisis_cv["zonas_dano_cv"]
    tele_optica = analisis_cv["telemetria_optica"]

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY.strip()}",
        "Content-Type": "application/json",
        "User-Agent": "GeoSync-FieldShift-NASA/1.0"
    }
    url = "https://api.groq.com/openai/v1/chat/completions"

    modelo_vision = obtener_modelo_groq_activo(headers, es_vision=True)
    if modelo_vision:
        try:
            imagen_file.seek(0)
            img_pil = ImageOps.exif_transpose(Image.open(imagen_file)).convert("RGB")
            img_pil.thumbnail((1024, 1024))
            buffer = io.BytesIO()
            img_pil.save(buffer, format="JPEG", quality=85)
            b64_img = base64.b64encode(buffer.getvalue()).decode("utf-8")
            imagen_file.seek(0)

            payload = {
                "model": modelo_vision,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                    ]
                }],
                "temperature": 0.2
            }
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                resultado = limpiar_json_ia(r.json()["choices"][0]["message"]["content"])
                if zonas_reales_cv:
                    for i, z_cv in enumerate(zonas_reales_cv):
                        if i < len(resultado.get("zonas_dano", [])):
                            resultado["zonas_dano"][i]["box"] = z_cv["box"]
                        else:
                            resultado.setdefault("zonas_dano", []).append(z_cv)
                resultado["generado_por_ia"] = True
                resultado["fuente_motor"] = f"🤖 Diagnosticado por IA Sintética Visión ({modelo_vision})"
                return resultado
        except Exception as e:
            print("Aviso modelo visión:", e)

    modelo_texto_activo = obtener_modelo_groq_activo(headers, es_vision=False)
    prompt_citometria = f"""
    {prompt}

    DATOS ÓPTICOS REALES EXTRAÍDOS DE LOS PÍXELES DE LA FOTOGRAFÍA SUBIDA:
    - Nombre de referencia del archivo: {imagen_file.name}
    - Porcentaje de clorofila sana en la imagen: {tele_optica['clorofila_sana_pct']}%
    - Porcentaje de clorosis / amarillamiento foliar: {tele_optica['clorosis_amarillamiento_pct']}%
    - Porcentaje de necrosis / manchas cafés o tejido seco: {tele_optica['necrosis_tejido_seco_pct']}%
    - Focos de lesión localizados por escáner de píxeles: {json.dumps(zonas_reales_cv, ensure_ascii=False)}

    Con base en estas mediciones reales de clorofila, clorosis y necrosis de la hoja, genera un diagnóstico fitosanitario único, preciso y detallado en el JSON solicitado.
    """
    payload_ia = {
        "model": modelo_texto_activo,
        "messages": [
            {
                "role": "system",
                "content": "Eres el fitopatólogo experto de GeoSync. Responde SIEMPRE en español y exclusivamente en formato JSON válido."
            },
            {"role": "user", "content": prompt_citometria}
        ],
        "temperature": 0.35
    }
    r = requests.post(url, headers=headers, json=payload_ia, timeout=30)
    r.raise_for_status()
    resultado = limpiar_json_ia(r.json()["choices"][0]["message"]["content"])

    if zonas_reales_cv:
        zonas_ia = resultado.get("zonas_dano", [])
        for i, z_cv in enumerate(zonas_reales_cv):
            if i < len(zonas_ia):
                zonas_ia[i]["box"] = z_cv["box"]
            else:
                zonas_ia.append(z_cv)
        resultado["zonas_dano"] = zonas_ia

    resultado["generado_por_ia"] = True
    resultado["fuente_motor"] = f"🤖 Diagnosticado por IA Sintética en Vivo (Groq: {modelo_texto_activo} + Escáner de Píxeles)"
    return resultado


def historial_usuario_view(request):
    """En modo invitado NO devuelve ningún historial."""
    if es_usuario_invitado(request):
        return JsonResponse({
            "es_invitado": True,
            "consultas_terreno": [],
            "planes_siembra": [],
            "escaneos_planta": []
        })

    consultas = ConsultaTerreno.objects.filter(usuario=request.user).order_by("-fecha_consulta")[:15]
    planes_siembra = PlanSiembra.objects.filter(usuario=request.user).order_by("-fecha_inicio")[:15]
    escaneos = EscaneoPlanta.objects.filter(usuario=request.user).order_by("-fecha_escaneo")[:15]

    lista_escaneos = []
    for esc in escaneos:
        plan_rec = getattr(esc, "plan_recuperacion", None)
        lista_escaneos.append({
            "id": esc.id,
            "planta_detectada": esc.planta_detectada,
            "reconocida_en_sistema": esc.reconocida_en_sistema,
            "diagnostico_ia": esc.diagnostico_ia,
            "generado_por_ia": esc.generado_por_ia,
            "fuente_motor": esc.fuente_motor,
            "fecha_escaneo": esc.fecha_escaneo.strftime("%Y-%m-%d %H:%M"),
            "plan_recuperacion": plan_rec.pasos_recuperacion_json if plan_rec else []
        })

    return JsonResponse({
        "es_invitado": False,
        "consultas_terreno": [
            {
                "id": c.id,
                "latitud": round(c.latitud, 4),
                "longitud": round(c.longitud, 4),
                "generado_por_ia": c.generado_por_ia,
                "fuente_motor": c.fuente_motor,
                "fecha_consulta": c.fecha_consulta.strftime("%Y-%m-%d %H:%M")
            } for c in consultas
        ],
        "planes_siembra": [
            {
                "id": p.id,
                "cultivo_elegido": p.cultivo_elegido,
                "etapas_json": p.etapas_json,
                "fecha_inicio": p.fecha_inicio.strftime("%Y-%m-%d")
            } for p in planes_siembra
        ],
        "escaneos_planta": lista_escaneos
    })


@csrf_exempt
def analizar_terreno_nasa_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido"}, status=405)

    body = json.loads(request.body)
    lat = float(body.get("lat"))
    lon = float(body.get("lon"))
    area_ha = body.get("area_hectareas", 1.0)
    enfoque = body.get("enfoque", "AUTO")
    invitado = es_usuario_invitado(request)

    datos_oficiales = obtener_datos_oficiales_terreno(lat, lon)
    datos_oficiales["area_hectareas"] = area_ha
    entorno = datos_oficiales.get("entorno_hidrico_ambiental", {})
    altitud_msnm = datos_oficiales.get("topografia_srtm_msnm", 800)

    if entorno.get("es_area_protegida"):
        nombre_res = entorno.get("nombre_area_protegida") or "Reserva Natural / Parque Nacional Protegido"
        motivo_bloqueo = (
            f"🚫 TERRENO NO APTO PARA AGRICULTURA NI GANADERÍA: El polígono seleccionado se encuentra en "
            f"'{nombre_res}'. Por normativa de conservación ambiental, está prohibida la tala, labranza o "
            f"introducción de ganado. Solo se permite preservación y restauración ecológica."
        )
        consulta_id = None
        if not invitado:
            consulta = ConsultaTerreno.objects.create(
                usuario=request.user,
                latitud=lat,
                longitud=lon,
                datos_nasa_json=datos_oficiales,
                cultivos_sugeridos=[],
                generado_por_ia=True,
                fuente_motor="🛡️ Bloqueo Ambiental Automático (Área Protegida / Reserva)"
            )
            consulta_id = consulta.id

        return JsonResponse({
            "consulta_id": consulta_id,
            "es_invitado": invitado,
            "es_apto": False,
            "motivo_no_apto": motivo_bloqueo,
            "nombre_area_protegida": nombre_res,
            "generado_por_ia": True,
            "fuente_motor": "🛡️ Bloqueo Ambiental Automático (Área Protegida / Reserva)",
            "datos_nasa": datos_oficiales,
            "recomendaciones": []
        })

    instruccion_enfoque = {
        "AUTO": (
            "MODO DECISIÓN IA: Evalúa objetivamente si este terreno es mejor para CULTIVOS AGRÍCOLAS o para GANADERÍA (PASTOS). "
            "En 'vocacion_terreno' declara un ganador claro con porcentajes y sugiere 2 opciones de la vocación ganadora y 1 de la otra."
        ),
        "CULTIVOS": (
            "MODO SOLO CULTIVOS AGRÍCOLAS: Sugiere en 'cultivos_recomendados' ÚNICAMENTE los 3 mejores cultivos agrícolas (NO incluyas pastos)."
        ),
        "GANADERIA": (
            "MODO SOLO GANADERÍA Y PASTOS: Sugiere en 'cultivos_recomendados' ÚNICAMENTE las 3 mejores opciones de pastos y forrajes."
        ),
        "MIXTO": (
            "MODO SISTEMA MIXTO AGROPECUARIO: Sugiere 2 cultivos agrícolas y 1 sistema de pastos mejorados."
        )
    }.get(enfoque, "")

    prompt_terreno = f"""
    Analiza estos datos satelitales y geográficos reales para un lote de {area_ha} hectáreas:
    {json.dumps(datos_oficiales, ensure_ascii=False)}

    DIRECTRIZ DEL USUARIO:
    {instruccion_enfoque}

    REGLAS TÉCNICAS OBLIGATORIAS:
    1. DISTANCIA AL MAR ({datos_oficiales['analisis_costero']['distancia_mar_km']} km a {datos_oficiales['analisis_costero']['costa_referencia']}): Explica cómo influye.
    2. RÍOS ({entorno['rio_mas_cercano']}): Evalúa acceso a agua y exige respetar 30 metros de ronda hídrica.
    3. COHERENCIA TÉRMICA SEGÚN ALTITUD ({altitud_msnm} msnm):
       - Si altitud >= 1800 msnm (clima frío), para ganadería recomienda ÚNICAMENTE Kikuyo, Raigrás, Azul Orchoro, Avena Forrajera o Trébol; NUNCA recomiendes Brachiaria en tierra fría.
       - Si altitud < 1800 msnm, recomienda Brachiaria, Guinea Mombasa, Estrella o Botón de Oro/Leucaena.
    4. Incluye siempre el rango de pH ideal y la recomendación de consultar con un ingeniero agrónomo o zootecnista.

    Responde ÚNICAMENTE en JSON válido con esta estructura exacta:
    {{
      "vocacion_terreno": "Veredicto claro con porcentajes",
      "justificacion_vocacion": "Explicación técnica citando río, distancia al mar, altitud SRTM y suelo SMAP",
      "cultivos_recomendados": [
        {{
          "nombre": "Nombre del Cultivo o Pasto Forrajero",
          "tipo_recomendacion": "GANADERIA_PASTOS" | "CULTIVO_AGRICOLA",
          "aptitud_porcentaje": 95,
          "justificacion_nasa": "Argumento citando distancia al mar, río cercano, temperatura POWER, humedad SMAP y altitud SRTM",
          "argumento_comparativo": "Por qué elegir esta opción frente a las demás alternativas",
          "ph_ideal": "5.5 a 6.5",
          "aviso_experto": "Recomendación de consultar con expertos, respetar ronda hídrica de 30m y revisar pH en laboratorio",
          "plan_siembra_semanal": [
            {{"etapa": "Semana 1-2: Preparación y Siembra", "tareas": "Labores para {area_ha} ha"}},
            {{"etapa": "Semana 3-8: Desarrollo / Rotación", "tareas": "Manejo hídrico y fertilización"}},
            {{"etapa": "Cosecha / Pastoreo", "tareas": "Recolección o manejo de carga animal"}}
          ]
        }}
      ]
    }}
    """
    respuesta_ia = llamar_ia_json(prompt_terreno, datos_nasa=datos_oficiales, area_ha=area_ha, enfoque=enfoque)
    recomendaciones = respuesta_ia.get("cultivos_recomendados", [])
    fue_ia = respuesta_ia.get("generado_por_ia", False)
    fuente_motor = respuesta_ia.get("fuente_motor", "⚙️ Sugerido por Programación")

    # Solo guarda en base de datos si NO es invitado
    consulta_id = None
    if not invitado:
        consulta = ConsultaTerreno.objects.create(
            usuario=request.user,
            latitud=lat,
            longitud=lon,
            datos_nasa_json=datos_oficiales,
            cultivos_sugeridos=recomendaciones,
            generado_por_ia=fue_ia,
            fuente_motor=fuente_motor
        )
        consulta_id = consulta.id

    return JsonResponse({
        "consulta_id": consulta_id,
        "es_invitado": invitado,
        "es_apto": True,
        "vocacion_terreno": respuesta_ia.get("vocacion_terreno", "Agropecuaria"),
        "justificacion_vocacion": respuesta_ia.get("justificacion_vocacion", ""),
        "generado_por_ia": fue_ia,
        "fuente_motor": fuente_motor,
        "datos_nasa": datos_oficiales,
        "recomendaciones": recomendaciones
    })


@csrf_exempt
def activar_plan_siembra_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Método no permitido"}, status=405)
    if es_usuario_invitado(request):
        return JsonResponse(
            {"error": "El Modo Invitado no guarda historial. Inicia sesión para guardar planes de siembra."},
            status=403
        )

    body = json.loads(request.body)
    consulta = get_object_or_404(ConsultaTerreno, id=body.get("consulta_id"), usuario=request.user)
    plan = PlanSiembra.objects.create(
        consulta=consulta,
        usuario=request.user,
        cultivo_elegido=body.get("cultivo_elegido"),
        etapas_json=body.get("etapas_json", [])
    )
    return JsonResponse({"mensaje": "Plan guardado exitosamente en tu bitácora.", "plan_id": plan.id})


@csrf_exempt
def escanear_hoja_experto_view(request):
    if request.method != "POST" or "imagen" not in request.FILES:
        return JsonResponse({"error": "Debes subir o tomar la fotografía de una hoja o planta."}, status=400)

    imagen = request.FILES["imagen"]
    invitado = es_usuario_invitado(request)

    prompt_vision = """
    Eres un botánico y fitopatólogo experto de la misión GeoSync.
    Genera una FICHA FITOSANITARIA DE 5 PUNTOS basada en el análisis óptico de la hoja subida.

    Responde ÚNICAMENTE en JSON válido con esta estructura exacta:
    {
      "es_hoja_valida": true,
      "pertenece_al_sistema": true,
      "planta_detectada": "Nombre Común y Científico estimado según tejido y muestra",
      "titulo_afeccion": "NOMBRE DE LA PLAGA, HONGO, NECROSIS O DEFICIENCIA DETECTADA",
      "severidad": "Sana" | "Baja" | "Media" | "Alta",
      "mensaje": "Resumen de identificación",
      "diagnostico": "Diagnóstico clínico detallando los porcentajes de clorofila, clorosis y necrosis medidos",
      "ficha_experta": {
        "que_es": "1. ¿Qué es?: Explicación clara del agente causal o deficiencia fisiológica medida",
        "como_se_reconoce": "2. ¿Cómo se reconoce?: Descripción visual de las lesiones marcadas en los recuadros",
        "cuando_aparece": "3. ¿Cuándo aparece?: Condición de humedad, temperatura o suelo que lo dispara",
        "danos": "4. ¿Daños?: Impacto sobre la fotosíntesis y rendimiento",
        "como_manejarlo": [
          "Acción 1 de control cultural o poda",
          "Acción 2 de corrección nutricional",
          "Acción 3 de manejo de riego y drenaje",
          "Acción 4 de tratamiento biológico o preventivo"
        ],
        "consejo_clave": "Frase clave para proteger la sanidad de la planta"
      },
      "zonas_dano": [
        {"etiqueta": "Nombre específico de la lesión", "confianza": 0.95, "box": [0.25, 0.30, 0.60, 0.68]}
      ],
      "plan_recuperacion": [
        {"dia": 1, "tarea": "Acción inmediata", "tratamiento": "Detalle del tratamiento"},
        {"dia": 3, "tarea": "Seguimiento", "tratamiento": "Ajuste agronómico"},
        {"dia": 7, "tarea": "Evaluación", "tratamiento": "Verificación de evolución"}
      ]
    }
    """
    resultado_ia = llamar_ia_vision_json(imagen, prompt_vision)
    es_planta_real = bool(resultado_ia.get("es_hoja_valida", False))
    planta = str(resultado_ia.get("planta_detectada", "NO ES UNA PLANTA")).strip()
    fue_ia = resultado_ia.get("generado_por_ia", False)
    fuente_motor = resultado_ia.get("fuente_motor", "⚙️ Escáner Óptico")
    diagnostico_texto = resultado_ia.get("diagnostico") if es_planta_real else resultado_ia.get("mensaje")
    zonas_dano = resultado_ia.get("zonas_dano", []) if es_planta_real else []
    plan_pasos = resultado_ia.get("plan_recuperacion", []) if es_planta_real else []

    escaneo_id = None
    imagen_original_url = ""
    imagen_anotada_url = None

    if not invitado:
        # Usuario registrado: guarda en su historial de base de datos
        escaneo = EscaneoPlanta.objects.create(
            usuario=request.user,
            imagen_hoja=imagen,
            reconocida_en_sistema=es_planta_real,
            planta_detectada=planta[:120],
            diagnostico_ia=diagnostico_texto,
            generado_por_ia=fue_ia,
            fuente_motor=fuente_motor
        )
        escaneo_id = escaneo.id
        imagen_original_url = escaneo.imagen_hoja.url if escaneo.imagen_hoja else ""
        if es_planta_real and escaneo.imagen_hoja and zonas_dano:
            imagen_anotada_url = dibujar_recuadros_dano(escaneo.imagen_hoja.path, zonas_dano)

        if es_planta_real and plan_pasos:
            PlanRecuperacion.objects.create(
                escaneo=escaneo,
                severidad=resultado_ia.get("severidad", "Media"),
                pasos_recuperacion_json=plan_pasos
            )
    else:
        # Modo Invitado: procesa la imagen para dibujar las lupas pero NO registra historial en BD
        imagen.seek(0)
        ruta_relativa = default_storage.save(f"hojas_escaneadas/guest_{imagen.name}", ContentFile(imagen.read()))
        ruta_absoluta = default_storage.path(ruta_relativa)
        imagen_original_url = f"/media/{ruta_relativa}"
        if es_planta_real and zonas_dano:
            imagen_anotada_url = dibujar_recuadros_dano(ruta_absoluta, zonas_dano)

    return JsonResponse({
        "escaneo_id": escaneo_id,
        "es_invitado": invitado,
        "generado_por_ia": fue_ia,
        "fuente_motor": fuente_motor,
        "reconocida_en_sistema": es_planta_real,
        "planta_detectada": planta,
        "titulo_afeccion": resultado_ia.get("titulo_afeccion", "DIAGNÓSTICO FITOSANITARIO"),
        "ficha_experta": resultado_ia.get("ficha_experta", {}) if es_planta_real else {},
        "severidad": resultado_ia.get("severidad", "N/A"),
        "mensaje": resultado_ia.get("mensaje"),
        "diagnostico": diagnostico_texto,
        "imagen_original_url": imagen_original_url,
        "imagen_anotada_url": imagen_anotada_url,
        "zonas_dano": zonas_dano,
        "plan_recuperacion": plan_pasos
    })


@csrf_exempt
def comentarios_api_view(request):
    if request.method == "GET":
        comentarios = ComentarioProyecto.objects.select_related("usuario").order_by("-fecha_creacion")[:30]
        return JsonResponse({
            "comentarios": [
                {
                    "usuario": c.usuario.username,
                    "modulo": c.get_modulo_display(),
                    "calificacion": c.calificacion,
                    "mensaje": c.mensaje,
                    "fecha": c.fecha_creacion.strftime("%Y-%m-%d %H:%M")
                } for c in comentarios
            ]
        })

    if request.method == "POST":
        body = json.loads(request.body)
        mensaje = body.get("mensaje", "").strip()
        if not mensaje:
            return JsonResponse({"error": "Escribe un comentario válido."}, status=400)

        if es_usuario_invitado(request):
            usuario_com, _ = User.objects.get_or_create(username="visitante_invitado")
        else:
            usuario_com = request.user

        ComentarioProyecto.objects.create(
            usuario=usuario_com,
            modulo=body.get("modulo", "GENERAL"),
            calificacion=int(body.get("calificacion", 5)),
            mensaje=mensaje
        )
        return JsonResponse({"mensaje": "¡Gracias! Tu comentario ha sido publicado."})