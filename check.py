"""Avant Figueres Vilafant <-> Barcelona Sants: consulta Renfe, aplica ordenes de la web y avisa por ntfy."""
import asyncio, datetime as dt, json, os, re, sys, urllib.request
from pathlib import Path

DAYS = int(os.getenv("DAYS", "5"))
TOPIC = os.environ.get("NTFY_TOPIC", "")
SITE, DEBUG = Path("site"), Path("debug")
# shortcut: codigos y campos del formulario de memoria; si sale "0 trenes", revisar con el artifact debug
FIG = ("FIGUERES VILAFANT", "79315")
BCN = ("BARCELONA-SANTS", "71801")
ROUTES = {"FIG-BCN": (FIG, BCN), "BCN-FIG": (BCN, FIG)}
NOMBRE = {"FIG-BCN": "Figueres Vilafant → Barcelona Sants", "BCN-FIG": "Barcelona Sants → Figueres Vilafant"}
SEARCH_URL = "https://venta.renfe.com/vol/buscarTren.do?Idioma=es&Pais=ES"
TIME = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
FULL = re.compile(r"complet|agotad|no disponible|sin plazas", re.I)
CMD = re.compile(r"(FIG-BCN|BCN-FIG)\|\d{4}-\d\d-\d\d\|\d\d:\d\d")

SUBMIT_JS = """({url, fields}) => {
  const f = document.createElement('form'); f.method = 'POST'; f.action = url;
  for (const [k, v] of Object.entries(fields)) {
    const i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i);
  }
  document.body.appendChild(f); f.submit();
}"""

# Elemento mas pequeno que contiene "AVANT" y dos horas = una fila de tren (no depende de clases CSS)
ROWS_JS = """() => {
  const c = [...document.querySelectorAll('div,li,tr,article')].filter(e => {
    const t = e.innerText || '';
    return /avant/i.test(t) && (t.match(/\\b\\d{1,2}[:.]\\d{2}\\b/g) || []).length >= 2 && t.length < 2000;
  });
  return c.filter(e => !c.some(o => o !== e && e.contains(o))).map(e => e.innerText);
}"""


def form(o, d, fecha):
    f = fecha.strftime("%d/%m/%Y")
    return {"tipoBusqueda": "autocomplete", "currenLocation": "menuBusqueda", "vengoderenfecom": "SI",
            "desOrigen": o[0], "desDestino": d[0], "cdgoOrigen": o[1], "cdgoDestino": d[1],
            "idiomaBusqueda": "ES", "FechaIdaSel": f, "FechaVueltaSel": "", "_fechaIdaVisual": f,
            "adultos_": "1", "ninos_": "0", "ninosMenores": "0", "numJoven": "0", "numDorada": "0",
            "codPromocional": "", "plazaH": "false", "sinEnlace": "false", "asistencia": "false",
            "franjaHoraI": "", "franjaHoraV": "", "Idioma": "es", "Pais": "ES"}


def parse(texts):
    trenes = {}
    for t in texts:
        horas = [f"{int(h):02d}:{m}" for h, m in TIME.findall(t)]
        if len(horas) < 2:
            continue
        precio = re.search(r"(\d+[.,]\d{2})\s*€", t)
        trenes[horas[0]] = {"salida": horas[0], "llegada": horas[1],
                            "estado": "completo" if FULL.search(t) else "libre",
                            "precio": precio.group(1) if precio else None}
    return sorted(trenes.values(), key=lambda x: x["salida"])


def aplicar(vig, lineas):
    """Ordenes de la web via ntfy: cada mensaje 'RUTA|fecha|hora' activa o desactiva la vigilancia."""
    for line in lineas:
        m = json.loads(line)
        if m.get("event") != "message":
            continue
        vig["since"] = m["id"]
        c = m.get("message", "").strip()
        if CMD.fullmatch(c):
            vig["watches"].remove(c) if c in vig["watches"] else vig["watches"].append(c)


def ntfy(path, data=None, headers=None):
    req = urllib.request.Request(f"https://ntfy.sh/{path}", data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


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
        for ruta, (o, d) in ROUTES.items():
            status[ruta] = {}
            for f in sorted(fechas):
                trenes = await buscar(page, o, d, f, f"{ruta}_{f}")
                if trenes is not None:
                    status[ruta][f.isoformat()] = trenes
                print(ruta, f, "->", trenes)
        await browser.close()
    return status


def main():
    if not TOPIC:
        sys.exit("Falta el secret NTFY_TOPIC")
    vig = json.loads((SITE / "vigilar.json").read_text())
    aplicar(vig, ntfy(f"{TOPIC}-cmd/json?poll=1&since={vig.get('since', '12h')}").splitlines())

    hoy = dt.date.today()
    vig["watches"] = [w for w in vig["watches"] if w.split("|")[1] >= hoy.isoformat()]
    fechas = {hoy + dt.timedelta(i) for i in range(DAYS)} | {dt.date.fromisoformat(w.split("|")[1]) for w in vig["watches"]}
    status = asyncio.run(consultar(fechas))
    total = sum(len(t) for r in status.values() for t in r.values())

    if total:
        for w in list(vig["watches"]):
            ruta, fecha, salida = w.split("|")
            tren = next((t for t in status[ruta].get(fecha, []) if t["salida"] == salida), None)
            if tren and tren["estado"] == "libre":  # avisa una vez y deja de vigilar
                ntfy(TOPIC, f"{NOMBRE[ruta]}\n{fecha} a las {salida}: ya hay plazas".encode(),
                     {"Title": "Plaza libre en Avant", "Priority": "high", "Tags": "train",
                      "Click": "https://www.renfe.com/es/es"})
                vig["watches"].remove(w)
        (SITE / "estado.json").write_text(json.dumps(
            {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "routes": status}))
    (SITE / "vigilar.json").write_text(json.dumps(vig))
    if not total:
        sys.exit("0 trenes encontrados: descarga el artifact 'debug' del workflow")


if __name__ == "__main__":
    if "--test" in sys.argv:
        r = parse(["AVANT 08123\n7:12\n8:05\nCompleto", "AVANT 08125\n09.40 10.33\n4,05 €", "sin horas AVANT"])
        assert r == [{"salida": "07:12", "llegada": "08:05", "estado": "completo", "precio": None},
                     {"salida": "09:40", "llegada": "10:33", "estado": "libre", "precio": "4,05"}], r
        v = {"watches": [], "since": "12h"}
        aplicar(v, ['{"event":"open","id":"a"}', '{"event":"message","id":"b","message":"FIG-BCN|2026-11-09|07:12"}',
                    '{"event":"message","id":"c","message":"basura"}'])
        assert v == {"watches": ["FIG-BCN|2026-11-09|07:12"], "since": "c"}, v
        aplicar(v, ['{"event":"message","id":"d","message":"FIG-BCN|2026-11-09|07:12"}'])
        assert v["watches"] == [], v
        print("ok")
    else:
        main()
