#!/usr/bin/env python3
"""
Archive quotidienne des images satellite infrarouges Meteosat (EUMETSAT / EUMETView).

Pour chaque jour UTC terminé qui n'a pas encore sa vidéo :
  1. lit la liste des images disponibles sur le serveur EUMETView,
  2. télécharge toutes les images de ce jour sur la zone Europe + Afrique,
  3. incruste la date et l'heure UTC,
  4. assemble une vidéo MP4 et la publie comme « release » GitHub nommée AAAA-MM-JJ.

Le serveur ne garde que quelques jours d'historique : le script rattrape
automatiquement les jours manqués tant qu'ils sont encore disponibles.
"""
import datetime as dt
import io
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------------- Réglages ----------------
WMS_URL = os.environ.get("WMS_URL") or "https://view.eumetsat.int/geoserver/wms"
LAYER = (os.environ.get("LAYER") or "").strip()   # vide = choix automatique
BBOX = (-25.0, -38.0, 55.0, 72.0)                 # lon min, lat min, lon max, lat max
WIDTH, HEIGHT = 800, 1100                         # taille de l'image en pixels
FPS = 8                                           # images par seconde dans la vidéo
CATCHUP_DAYS = 5                                  # nombre de jours passés à vérifier
MIN_FRAMES = 48                                   # en dessous : jour incomplet, ignoré
MAX_MB = 45                                       # au-delà : recompression plus forte
LIST_LAYERS = os.environ.get("LIST_LAYERS") == "1"

IR_HINTS = ("ir108", "ir105", "ir_108", "ir_105")
PREFERRED_PREFIXES = ("mtg_fd:", "msg_fes:")      # Meteosat 0°, disque complet
EXCLUDE = ("rss", "iodc")                         # balayage rapide / océan Indien
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WMS_NS = "{http://www.opengis.net/wms}"


# ---------------- Lecture du catalogue ----------------
def get_capabilities():
    r = requests.get(WMS_URL, params={"SERVICE": "WMS", "VERSION": "1.3.0",
                                      "REQUEST": "GetCapabilities"}, timeout=180)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    layers = {}
    for lyr in root.iter(WMS_NS + "Layer"):
        name = lyr.find(WMS_NS + "Name")
        if name is None or not name.text:
            continue
        time_dim = None
        for d in lyr.findall(WMS_NS + "Dimension"):
            if d.get("name", "").lower() == "time":
                time_dim = (d.text or "").strip()
        layers[name.text.strip()] = time_dim
    return layers


def choose_layer(layers):
    if LAYER:
        if LAYER not in layers:
            sys.exit(f"La couche '{LAYER}' n'existe pas sur le serveur.")
        return LAYER
    cands = [n for n, t in layers.items()
             if t and any(h in n.lower() for h in IR_HINTS)
             and not any(x in n.lower() for x in EXCLUDE)]

    def rank(n):
        for i, p in enumerate(PREFERRED_PREFIXES):
            if n.lower().startswith(p):
                return (i, len(n))
        return (len(PREFERRED_PREFIXES), len(n))

    cands.sort(key=rank)
    print("Couches infrarouges candidates :", cands)
    if not cands:
        sys.exit("Aucune couche infrarouge trouvée. Relance avec l'option "
                 "« Lister les couches » et envoie le résultat.")
    return cands[0]


def choose_overlays(layers):
    """Côtes et frontières, si le serveur en propose."""
    out = []
    for hint in ("coastline", "boundar"):
        m = [n for n, t in layers.items() if hint in n.lower() and not t]
        m.sort(key=lambda n: (not n.lower().startswith("backgrounds:"), len(n)))
        if m:
            out.append(m[0])
    return out


# ---------------- Gestion des dates ----------------
def parse_dt(s):
    d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def parse_period(p):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", p.strip())
    if not m:
        raise ValueError(f"Période inconnue : {p}")
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return dt.timedelta(days=d, hours=h, minutes=mi, seconds=s)


def parse_times(text):
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            a, b, p = part.split("/")
            t, end, step = parse_dt(a), parse_dt(b), parse_period(p)
            while t <= end:
                out.append(t)
                t += step
        else:
            out.append(parse_dt(part))
    return sorted(set(out))


# ---------------- Images ----------------
def fetch_frame(layer_list, t):
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": ",".join(layer_list), "STYLES": "," * (len(layer_list) - 1),
        "SRS": "EPSG:4326", "BBOX": ",".join(str(v) for v in BBOX),
        "WIDTH": WIDTH, "HEIGHT": HEIGHT, "FORMAT": "image/png",
        "TIME": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for attempt in range(3):
        try:
            r = requests.get(WMS_URL, params=params, timeout=90)
            if r.ok and r.headers.get("Content-Type", "").startswith("image/"):
                return Image.open(io.BytesIO(r.content)).convert("RGB")
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    return None


try:
    FONT = ImageFont.truetype(FONT_PATH, 30)
except OSError:
    FONT = ImageFont.load_default()


def stamp(img, t):
    draw = ImageDraw.Draw(img)
    text = t.strftime("%Y-%m-%d  %H:%M UTC")
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=FONT)
    draw.rectangle((10, 10, 30 + x1 - x0, 30 + y1 - y0), fill=(0, 0, 0))
    draw.text((20 - x0, 20 - y0), text, font=FONT, fill=(255, 255, 255))
    return img


def encode(frames_dir, out, crf):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
                    "-i", f"{frames_dir}/f_%04d.png", "-c:v", "libx264",
                    "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", out], check=True)


# ---------------- Publication ----------------
def release_exists(tag):
    return subprocess.run(["gh", "release", "view", tag],
                          capture_output=True).returncode == 0


def process_day(day, layer_list, times):
    tag = day.isoformat()
    day_times = [t for t in times if t.date() == day]
    print(f"{tag} : {len(day_times)} images disponibles sur le serveur")
    if len(day_times) < MIN_FRAMES:
        print("  -> jour incomplet sur le serveur, ignoré")
        return
    workdir = f"work_{tag}"
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    n = 0
    for t in day_times:
        img = fetch_frame(layer_list, t)
        if img is None:
            print(f"  échec de l'image de {t:%H:%M}")
            continue
        stamp(img, t).save(f"{workdir}/f_{n:04d}.png")
        n += 1
    if n < MIN_FRAMES:
        raise RuntimeError(f"seulement {n} images récupérées pour {tag}")
    out = f"{tag}.mp4"
    encode(workdir, out, 23)
    if os.path.getsize(out) > MAX_MB * 1e6:
        encode(workdir, out, 30)
    size = os.path.getsize(out) / 1e6
    subprocess.run(["gh", "release", "create", tag, out,
                    "--title", f"Satellite IR {tag}",
                    "--notes", f"{n} images, couche {layer_list[0]}, {size:.1f} Mo"],
                   check=True)
    print(f"  -> vidéo publiée ({n} images, {size:.1f} Mo)")
    shutil.rmtree(workdir)
    os.remove(out)


def main():
    layers = get_capabilities()
    if LIST_LAYERS:
        for n, t in sorted(layers.items()):
            print(("[avec dates] " if t else "             ") + n)
        return

    layer = choose_layer(layers)
    times = parse_times(layers[layer])
    if not times:
        sys.exit(f"La couche {layer} ne donne aucune date.")
    print(f"Couche utilisée : {layer}")
    print(f"Historique disponible : du {times[0]:%Y-%m-%d %H:%M} au {times[-1]:%Y-%m-%d %H:%M} UTC")

    layer_list = [layer]
    overlays = choose_overlays(layers)
    if overlays:
        if fetch_frame([layer, *overlays], times[-1]) is not None:
            layer_list = [layer, *overlays]
            print("Côtes/frontières ajoutées :", overlays)
        else:
            print("Côtes/frontières indisponibles, images sans fond de carte")

    today = dt.datetime.now(dt.timezone.utc).date()
    errors = 0
    for i in range(CATCHUP_DAYS, 0, -1):
        day = today - dt.timedelta(days=i)
        if release_exists(day.isoformat()):
            continue
        try:
            process_day(day, layer_list, times)
        except Exception as e:
            errors += 1
            print(f"  ERREUR pour {day} : {e}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
