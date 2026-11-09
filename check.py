"""Avant Figueres Vilafant <-> Barcelona Sants: consulta Renfe, aplica ordenes de la web y avisa con Web Push."""
import asyncio, datetime as dt, hashlib, json, os, re, sys, urllib.request
from pathlib import Path

DAYS = int(os.getenv("DAYS", "5"))
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC", "").strip()
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE", "").strip()
# Canal de ordenes de la web (publico; cada dispositivo firma con su id aleatorio, que nunca se publica)
CMD_TOPIC = "avant-" + hashlib.sha256(VAPID_PUBLIC.encode()).hexdigest()[:20]
SITE, DEBUG = Path("site"), Path("debug")
BCN = ("BARCELONA-SANTS", "71801")  # confirmado con la pagina de Renfe
ESTACIONES_JS = "https://www.renfe.com/content/dam/renfe/es/General/buscadores/javascript/estacionesEstaticas.js"
NOMBRE = {"FIG-BCN": "Figueres Vilafant → Barcelona Sants", "BCN-FIG": "Barcelona Sants → Figueres Vilafant"}
SEARCH_URL = "https://venta.renfe.com/vol/buscarTren.do?Idioma=es&Pais=ES"
HORAS = re.compile(r"salida a las (\d\d:\d\d) y llegada a las (\d\d:\d\d)")
TIPO = re.compile(r"Tipo de tren: ([^.]+)")
CMD = re.compile(r"(FIG-BCN|BCN-FIG)\|\d{4}-\d\d-\d\d\|\d\d:\d\d")

SUBMIT_JS = """({url, fields}) => {
  const f = document.createElement('form'); f.method = 'POST'; f.action = url;
  for (const [k, v] of Object.entries(fields)) {
    const i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i);
  }
  document.body.appendChild(f); f.submit();
}"""

# Filas de la lista de ida: horas y tipo vienen en aria-labels; el aviso (completo / no a la venta) en #boton-style
ROWS_JS = """() => [...document.querySelectorAll('#listaTrenesTBodyIda > div[id^="tren_i_"]')].map(r => ({
  lab: [...r.querySelectorAll('[aria-label]')].map(e => e.getAttribute('aria-label')).join(' '),
  aviso: r.querySelector('#boton-style')?.getAttribute('title') || '',
  precio: r.querySelector('.precio-final')?.getAttribute('title') || '',
}))"""


def form(o, d, fecha):
    f = fecha.strftime("%d/%m/%Y")
    return {"tipoBusqueda": "autocomplete", "currenLocation": "menuBusqueda", "vengoderenfecom": "SI",
            "desOrigen": o[0], "desDestino": d[0], "cdgoOrigen": o[1], "cdgoDestino": d[1],
            "idiomaBusqueda": "ES", "FechaIdaSel": f, "FechaVueltaSel": "", "_fechaIdaVisual": f,
            "adultos_": "1", "ninos_": "0", "ninosMenores": "0", "numJoven": "0", "numDorada": "0",
            "codPromocional": "", "plazaH": "false", "sinEnlace": "false", "asistencia": "false",
            "franjaHoraI": "", "franjaHoraV": "", "Idioma": "es", "Pais": "ES"}


def parse(rows):
    trenes = {}
    for r in rows:
        h, tipo = HORAS.search(r["lab"]), TIPO.search(r["lab"])
        if not h or not tipo or "AVANT" not in tipo.group(1).upper():
            continue
        aviso = r["aviso"].lower()
        if "no circula" in aviso:
            continue
        precio = re.search(r"\d+,\d{2}", r["precio"])
        trenes[h[1]] = {"salida": h[1], "llegada": h[2], "precio": precio[0] if precio else None,
                        "estado": "completo" if "completo" in aviso else "no_venta" if aviso else "libre"}
    return sorted(trenes.values(), key=lambda x: x["salida"])


async def codigo_figueres(page):
    """Codigo de Figueres Vilafant sacado de la lista de estaciones de Renfe (o del secret FIG_CODE)."""
    if os.getenv("FIG_CODE"):
        return os.environ["FIG_CODE"]
    js = await page.evaluate(f"fetch('{ESTACIONES_JS}').then(r => r.text())")
    i = js.upper().find("VILAFANT")
    if i < 0:
        sys.exit("No encuentro Figueres Vilafant en la lista de estaciones de Renfe")
    print("Contexto Vilafant:", js[max(0, i - 300):i + 300])
    # el codigo esta en el mismo objeto {...} que el nombre; si no hay llaves, el numero mas cercano
    ini, fin = js.rfind("{", 0, i), js.find("}", i)
    base = ini if 0 <= ini and fin > 0 and fin - ini < 1000 else max(0, i - 300)
    obj = js[base:fin] if base == ini else js[base:i + 300]
    cods = [(abs(base + m.start() - i), m[0]) for m in re.finditer(r"\b\d{5}\b", obj)]
    if not cods:
        sys.exit("No encuentro el codigo de Figueres Vilafant")
    return min(cods)[1]


def huella(device_id):
    return hashlib.sha256(device_id.encode()).hexdigest()[:16]


def aplicar(vig, lineas):
    """Ordenes de la web via ntfy, en JSON:
    {"d": id_dispositivo, "a": "sub", "s": suscripcion_push}  registra el movil
    {"d": id_dispositivo, "a": "w", "id": "RUTA|fecha|hora"}  activa o desactiva un aviso
    """
    for line in lineas:
        m = json.loads(line)
        if m.get("event") != "message":
            continue
        vig["since"] = m["id"]
        try:
            c = json.loads(m.get("message", ""))
        except ValueError:
            continue
        d = c.get("d") if isinstance(c, dict) else None
        if not isinstance(d, str) or not 16 <= len(d) <= 64:
            continue
        dev = vig["devices"].setdefault(huella(d), {"sub": None, "watches": []})
        s = c.get("s")
        if c.get("a") == "sub" and isinstance(s, dict) and str(s.get("endpoint", "")).startswith("https://"):
            dev["sub"] = {"endpoint": s["endpoint"], "keys": s.get("keys", {})}
        elif c.get("a") == "w" and isinstance(c.get("id"), str) and CMD.fullmatch(c["id"]):
            w = dev["watches"]
            w.remove(c["id"]) if c["id"] in w else w.append(c["id"])


def leer_ordenes(vig):
    url = f"https://ntfy.sh/{CMD_TOPIC}/json?poll=1&since={vig.get('since', '12h')}"
    with urllib.request.urlopen(url, timeout=30) as r:
        aplicar(vig, r.read().decode().splitlines())


def push(sub, titulo, texto):
    """Envia la notificacion. Devuelve False si la suscripcion ya no existe (app borrada, permiso quitado)."""
    from pywebpush import WebPushException, webpush
    try:
        webpush(sub, json.dumps({"title": titulo, "body": texto, "url": "https://www.renfe.com/es/es"}),
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": "mailto:avant-vigia@users.noreply.github.com"})
        return True
    except WebPushException as e:
        code = getattr(e.response, "status_code", None)
        print("Error push", code, e)
        return code not in (404, 410)


async def buscar(page, o, d, fecha, tag):
    try:
        async with page.expect_navigation(wait_until="networkidle", timeout=60000):
            await page.evaluate(SUBMIT_JS, {"url": SEARCH_URL, "fields": form(o, d, fecha)})
        await page.wait_for_timeout(3000)  # la disponibilidad a veces llega despues por XHR
        trenes = parse(await page.evaluate(ROWS_JS))
    except Exception as e:
        print(tag, "ERROR", e)
        trenes = None
    if not trenes:
        DEBUG.mkdir(exist_ok=True)
        (DEBUG / f"{tag}.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(DEBUG / f"{tag}.png"), full_page=True)
    return trenes


async def consultar(fechas):
    from playwright.async_api import async_playwright
    status = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(locale="es-ES", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"))
        await page.goto("https://www.renfe.com/es/es", wait_until="domcontentloaded")  # cookies antibot
        try:
            await page.click("#onetrust-accept-btn-handler", timeout=5000)
        except Exception:
            pass
        fig = ("FIGUERES VILAFANT", await codigo_figueres(page))
        print("Codigo Figueres Vilafant:", fig[1])
        for ruta, (o, d) in {"FIG-BCN": (fig, BCN), "BCN-FIG": (BCN, fig)}.items():
            status[ruta] = {}
            for f in sorted(fechas):
                trenes = await buscar(page, o, d, f, f"{ruta}_{f}")
                if trenes is not None:
                    status[ruta][f.isoformat()] = trenes
                print(ruta, f, "->", trenes)
        await browser.close()
    return status


def main():
    vig = json.loads((SITE / "vigilar.json").read_text())
    if "devices" not in vig:  # formato antiguo (ntfy): se empieza de cero
        vig = {"since": "12h", "devices": {}}
    if VAPID_PUBLIC and VAPID_PRIVATE:
        leer_ordenes(vig)
    else:
        print("Faltan los secrets VAPID_PUBLIC / VAPID_PRIVATE: abre la web para generarlos")

    hoy = dt.date.today()
    for dev in vig["devices"].values():
        dev["watches"] = [w for w in dev["watches"] if w.split("|")[1] >= hoy.isoformat()]
    fechas = {hoy + dt.timedelta(i) for i in range(DAYS)}
    fechas |= {dt.date.fromisoformat(w.split("|")[1]) for dev in vig["devices"].values() for w in dev["watches"]}
    status = asyncio.run(consultar(fechas))
    total = sum(len(t) for r in status.values() for t in r.values())

    if total:
        for h, dev in list(vig["devices"].items()):
            for w in list(dev["watches"]):
                ruta, fecha, salida = w.split("|")
                tren = next((t for t in status[ruta].get(fecha, []) if t["salida"] == salida), None)
                if not (tren and tren["estado"] == "libre" and dev["sub"] and VAPID_PRIVATE):
                    continue
                dia = dt.date.fromisoformat(fecha).strftime("%d/%m")
                if push(dev["sub"], "Plaza libre en el Avant de las " + salida,
                        f"{NOMBRE[ruta]}, {dia}. Toca para ir a Renfe."):
                    dev["watches"].remove(w)  # avisa una vez y deja de vigilar
                else:
                    del vig["devices"][h]  # movil dado de baja
                    break
        (SITE / "estado.json").write_text(json.dumps(
            {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "routes": status}))
    vig["devices"] = {h: d for h, d in vig["devices"].items() if d["sub"] or d["watches"]}
    (SITE / "vigilar.json").write_text(json.dumps(vig))
    if not total:
        sys.exit("0 trenes encontrados: descarga el artifact 'debug' del workflow")


if __name__ == "__main__":
    if "--test" in sys.argv:
        lab = "Tren con salida a las {} y llegada a las {}. Tipo de tren: {}."
        r = parse([{"lab": lab.format("07:12", "08:05", "AVANT"), "aviso": "Tren Completo", "precio": ""},
                   {"lab": lab.format("09:40", "10:33", "AVANT"), "aviso": "", "precio": "Precio desde 4,05"},
                   {"lab": lab.format("11:00", "11:50", "AVANT"), "aviso": "Este tren no se encuentra disponible para la venta", "precio": ""},
                   {"lab": lab.format("12:00", "12:50", "AVANT"), "aviso": "Tren no circula", "precio": ""},
                   {"lab": lab.format("06:16", "08:51", "REGIONAL"), "aviso": "", "precio": "Precio desde 14,40"}])
        assert [(t["salida"], t["estado"], t["precio"]) for t in r] == [
            ("07:12", "completo", None), ("09:40", "libre", "4,05"), ("11:00", "no_venta", None)], r
        v = {"since": "12h", "devices": {}}
        dev_id = "a" * 32
        sub = {"endpoint": "https://web.push.apple.com/x", "keys": {"p256dh": "k", "auth": "a"}}
        msg = lambda o, i: json.dumps({"event": "message", "id": i, "message": json.dumps(o)})
        aplicar(v, ['{"event":"open","id":"a"}',
                    msg({"d": dev_id, "a": "sub", "s": sub}, "b"),
                    msg({"d": dev_id, "a": "w", "id": "FIG-BCN|2026-11-09|07:12"}, "c"),
                    msg({"d": "corto", "a": "w", "id": "FIG-BCN|2026-11-09|07:12"}, "d"),
                    json.dumps({"event": "message", "id": "e", "message": "basura"})])
        assert v == {"since": "e", "devices": {huella(dev_id): {"sub": sub, "watches": ["FIG-BCN|2026-11-09|07:12"]}}}, v
        aplicar(v, [msg({"d": dev_id, "a": "w", "id": "FIG-BCN|2026-11-09|07:12"}, "f")])
        assert v["devices"][huella(dev_id)]["watches"] == [], v
        print("ok")
    else:
        main()
