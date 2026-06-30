# Librerías
import argparse
import json
import math
from pathlib import Path
import sys
import time

import requests
from shapely.geometry import LineString, Point, MultiPoint, mapping
from shapely.strtree import STRtree

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {
    "User-Agent": "cruces_peatonales/1.0 (OSM pedestrian crossing detector)",
    "Accept": "application/json",
}
QUERY_TIME_OUT = 600  # seconds
R_TIERRA = 6378137.0  # radio WGS84 para la proyeccion local

# Senderos peatonales
PEATONAL = {
    # Special roads
    "pedestrian",
    # Paths
    "footway", "steps",  "path", #"corridor",
    # Mainly for horses but pedestrians might be allowed (by definition)
    "bridleway"}

# Vias para vehiculos motorizados
MOTORIZADA = {
    # Roads
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", 
    # Links
    "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link",
    # Special roads
    "living_street",  "road", "busway" # "service",
}


# --------------------------- Descarga de datos ---------------------------

def obtener_bbox_nominatim(lugar: str, header: str=None):
    """Usa Nominatim para resolver un nombre de lugar a un bbox (south, west, north, east)."""
    response = requests.get(
        NOMINATIM_URL,
        params={"q": lugar, "format": "json", "limit": 1},
        headers=header if header else None,
        timeout=60,
    )
    response.raise_for_status()
    resultados = response.json()
    if not resultados:
        raise ValueError(f"No se encontro: {lugar}")
    s, n, w, e = map(float, resultados[0]["boundingbox"])
    return (s, w, n, e)


def consultar_overpass(bbox, header:str=None, timeout:int=QUERY_TIME_OUT, n_intentos:int=3):
    """Descarga senderos peatonales y vias motorizadas dentro del bbox."""
    s, w, n, e = bbox
    area = f"{s},{w},{n},{e}"
    ped = "|".join(sorted(PEATONAL))
    mot = "|".join(sorted(MOTORIZADA))
    query = f"""
    [out:json][timeout:{timeout}];
    (
      way["highway"~"^({ped})$"]({area});
      way["highway"~"^({mot})$"]({area});
    );
    (._;>;);
    out body;
    """

    for intento in range(n_intentos):
        resp = requests.post(
            OVERPASS_URL, 
            data={"data": query},
            headers=header if header else None,
            timeout=QUERY_TIME_OUT,
        )
        if resp.status_code == 200:
            return resp.json()
        time.sleep(5 * (intento + 1))  # backoff ante 429/504
    resp.raise_for_status()        


# --------------------------- Transformaciones ---------------------------

def proyectar(lon, lat, lon0, lat0):
    """Proyección local (distancias cortas)"""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * R_TIERRA
    y = math.radians(lat - lat0) * R_TIERRA
    return (x, y)

def desproyectar(x, y, lon0, lat0):
    """Convierte a coordenadas geográficas (lon, lat) desde la proyección local"""
    lat = lat0 + math.degrees(y / R_TIERRA)
    lon = lon0 + math.degrees(x / (R_TIERRA * math.cos(math.radians(lat0))))
    return (lon, lat)

def a_geojson(cruces, buffer_m):
    features = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
        "properties": {
            "walkable_way_ids": sorted(c["ways"]),
            "crossing_path_ids": sorted(c["ped"]),
            "motor_way_ids": sorted(c["mot"]),
            "n_walkable_ways": len(c["ways"]),
            "buffer_m": buffer_m,
            "shared_node": c["shared"],
        },
    } for c in cruces]
    return {"type": "FeatureCollection", "features": features}

def senderos_a_geojson(senderos, lon0, lat0):
    features = [{
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [list(desproyectar(x, y, lon0, lat0)) for x, y in s["geom"].coords],
        },
        "properties": {"id": s["id"]},
    } for s in senderos]
    return {"type": "FeatureCollection", "features": features}

# --------------------------- Senderos y vías ---------------------------

def construir_geometrias(osm_ways, lon0, lat0):
    """Separa los ways en lineas peatonales y motorizadas"""
    nodos = {
        el["id"]: proyectar(el["lon"], el["lat"], lon0, lat0)
        for el in osm_ways["elements"] if el["type"] == "node"
    }

    peatonales, motorizadas = [], []
    # Itera sobre todas los elementos disponibles
    for el in osm_ways["elements"]:
        # Si no es "way" (i.e. nodo o relación), se ignora
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        hw = tags.get("highway")
        # Las coordenadas de la vía son dadas por los nodos
        coords = [nodos[i] for i in el["nodes"] if i in nodos]
        if len(coords) < 2:
            continue
        registro = {
            "id": el["id"], "highway": hw,
            "geom": LineString(coords), "nodos": set(el["nodes"]),
        }
        if hw in PEATONAL:
            # Exluir andenes
            ignore_footway_type = tags.get("footway") in {"sidewalk", "traffic_island", "crossing", "links"}
            # y puentes            
            is_bridge = tags.get("bridge") == "yes"
            # y túneles
            is_tunnel = tags.get("tunnel") == "yes"
            # y áreas peatonales
            is_pedestrian_area = hw == "pedestrian" and tags.get("area") == "yes"
            
            if ignore_footway_type or is_bridge or is_tunnel or is_pedestrian_area:
                continue
            peatonales.append(registro)
        elif hw in MOTORIZADA:
            motorizadas.append(registro)

    return peatonales, motorizadas


def extraer_puntos(geom):
    """Devuelve los puntos de una intersección. En caso de ser una línea retorna el centroide."""
    if geom.is_empty:
        return []
    if isinstance(geom, Point):
        return [geom]
    if isinstance(geom, MultiPoint):
        return list(geom.geoms)
    if geom.geom_type in ("LineString", "MultiLineString"):
        return [geom.centroid]
    # Por recurrencia
    if geom.geom_type == "GeometryCollection":
        puntos = []
        for g in geom.geoms:
            puntos.extend(extraer_puntos(g))
        return puntos
    return []


def direccion_segmento(coords, c):
    """Vector unitario del segmento de la vía más cercano al punto c."""
    seg_cerca, min_dist = None, None
    cp = Point(c)
    # Evalúa todos los pares consecutivos de coordenadas
    # Se deja la iteración completa pues se podrían tener segmentos curvos
    # que se acercan, se alejan, y luego se vuelven a acercar al punto de intersección.
    for i, j in zip(coords, coords[1:]):
        dist = LineString([i, j]).distance(cp)
        if min_dist is None or dist < min_dist:
            min_dist, seg_cerca = dist, (i, j)
    (x1, y1), (x2, y2) = seg_cerca
    dx, dy = x2 - x1, y2 - y1
    norma = math.hypot(dx, dy)
    return (1.0, 0.0) if norma == 0 else (dx / norma, dy / norma)


def coords_densas(geom, paso, c):
    """Muestrea puntos a lo largo de una geometria de lineas cada 'paso' metros."""
    # Extrae todas las líneas que puedan surgir
    lineas = []
    if geom.geom_type == "LineString":
        lineas = [geom]
    elif geom.geom_type == "MultiLineString":
        lineas = list(geom.geoms)
    elif geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            if g.geom_type == "LineString":
                lineas.append(g)
            elif g.geom_type == "MultiLineString":
                lineas.extend(g.geoms)
    elif geom.geom_type == "Point":
        return [(geom.x, geom.y)]

    # Divide las líneas en puntos cada 'paso' metros, incluyendo los extremos
    pts = []
    for ln in lineas:
        largo = ln.length
        if largo == 0:
            pts.append(ln.coords[0])
            continue
        n = int(largo // paso)
        for k in range(n + 1):
            p = ln.interpolate(k * paso)
            pts.append((p.x, p.y))
        pts.append(ln.coords[-1])

    # Ordena los puntos por distancia al cruce c
    pts.sort(key=lambda p: math.hypot(p[0] - c[0], p[1] - c[1]))
    return pts


def caminables_por_lado(c, vec_dir_m, peatonales, ptree, buffer_m, paso, tol, near):
    """Conjuntos de ids de senderos caminables a cada lado de la via, dentro del buffer.

    El lado se define por la distancia perpendicular con signo a la tangente de la
    via (como vec_dir_m es unitario, el producto cruz equivale a esa distancia en metros).
    """
    disco = Point(c).buffer(buffer_m)
    izquierda, derecha = set(), set()
    cx, cy = c
    dx, dy = vec_dir_m
    # Consulta los senderos peatonales que intersectan el buffer de la intersección
    for idx in ptree.query(disco):
        w = peatonales[int(idx)]
        recorte = w["geom"].intersection(disco)
        if recorte.is_empty:
            continue
        # Toma la intersección (recorte) y la muestrea cada 'paso' metros para evaluar 
        # su posición relativa a la vía motorizada
        # Banderas
        en_izquierda, en_derecha = False, False
        
        for px, py in coords_densas(recorte, paso, c):
            # Vector normalizado del cruce al punto muestreado
            dxr, dyr = (px - cx, py - cy)
            normar = math.hypot(dxr, dyr)
            rx, ry = (1.0, 0.0) if normar == 0 else (dxr / normar, dyr / normar) 
            # Si el punto muestreado está muy cerca del cruce, 
            # se asume que es el mismo cruce y se ignora
            if normar < near:
                continue
            # Producto cruz entre vec_dir_m y el vector del cruce al punto muestreado
            # Ya que ambos están normalizados, el resultados es igual al seno del ángulo que forman
            prod_cruz = dx * ry - dy * rx
            # Revisa si el ángulo está entre 0 y pi, extremos exluidos
            # Se entiende que caso de 0 y de pi corresponde a un sidewalk a lo largo de la vía
            # por lo que se ignora
            if prod_cruz > tol:
                izquierda.add(w["id"])
                en_izquierda = True
            # Si el ángulo está entre 0 y -pi
            elif prod_cruz < -tol:
                derecha.add(w["id"])
                en_derecha = True
            # Si ya agregó el camino a ambos lados de la vía
            # no vale la pena seguir iterando sobre los puntos muestreados del mismo camino
            if en_izquierda and en_derecha:
                break
    return izquierda, derecha


# --------------------------- Cruces ---------------------------

def detectar_cruces(peatonales, motorizadas, lon0, lat0,
                    buffer_m=20.0, paso=2.0, tol=0.17, near=1.0):
    """Detecta los cruces de vías peatonales con vias para automotores
      que tienen sendero caminable a ambos lados de la via.
      Se define tol como el valor de sin(10°). Es decir, entre -10° y 10° se 
      considera que el sendero es paralelo a la vía motorizada"""
    # Si no hay insumos, no hace nada
    if not peatonales or not motorizadas:
        return []
    # Crea un STRtree para cada tipo de vía para acelerar las consultas espaciales
    ptree = STRtree([p["geom"] for p in peatonales])
    mtree = STRtree([m["geom"] for m in motorizadas])

    agregados = {}
    ids_senderos = set()

    for ped in peatonales:
        # Itera sobre las vias motorizadas cuyo boundingbox intersecta el sendero peatonal
        for j in mtree.query(ped["geom"]):
            mot = motorizadas[int(j)]
            # Valida que la intersección sea real
            hay_interseccion = ped["geom"].intersects(mot["geom"])
            if not hay_interseccion:
                continue
            # Intersección entre el sendero peatonal y la via motorizada
            inter = ped["geom"].intersection(mot["geom"])
            coords_via = list(mot["geom"].coords)
            # Itera sobre cada punto que surge de la intersección
            for pt in extraer_puntos(inter):
                c = (pt.x, pt.y)
                # Vector normalizado de la dirección de la vía motorizada en el punto 
                # más cercano, que debería ser el de intersección
                # Su sentido depende de la dirección de las coordenadas de la vía, 
                # y eso afecta la determinación de los lados izquierdo y derecho de la vía, pero es irrelevante
                vec_dir = direccion_segmento(coords_via, c)
                # Busca los senderos caminables a cada lado de la vía dentro del buffer
                izq, der = caminables_por_lado(
                    c, vec_dir, peatonales, ptree, buffer_m, paso, tol, near)
                # Senderos presentes solo a un lado de la vía
                # son aquellos que con mayor probabilidad pueden estar interrumpidos
                solo_izq = izq - der
                solo_der = der - izq
                # Si no hay almenos un sendero caminable a cada lado o 
                # el sendero inicial (ped) está en al menos un lado se ignora el cruce
                # Si hay un mismo sendero diferente a ped en ambos lados, se asume que la intersección
                # se encontrará en su iteración. Esto para dejar por fuera senderos muy cercanos a la intersección,
                # pero que en realidad no sean interrumpidos por la vía (ver parque el Virrey, Cll. 88 - Cra. 16, Bogotá, Colombia)
                senderos_ambos_lados = solo_izq and solo_der
                sendero_ori_al_menos_un_lado = ped["id"] in izq or ped["id"] in der
                sendero_ori_ambos_lados = ped["id"] in izq and ped["id"] in der
                
                if (not senderos_ambos_lados and not sendero_ori_ambos_lados) or not sendero_ori_al_menos_un_lado:
                    continue
                lon, lat = desproyectar(c[0], c[1], lon0, lat0)
                clave = (round(lon, 7), round(lat, 7))
                agg = agregados.setdefault(clave, {
                    "lon": lon, "lat": lat, "ped": set(),
                    "mot": set(), "ways": set(), "shared": False,
                })
                agg["ped"].add(ped["id"])
                agg["mot"].add(mot["id"])
                agg["ways"] |= (izq | der)
                if ped["nodos"] & mot["nodos"]:
                    agg["shared"] = True
                # Senderos considerados
                ids_senderos.update(izq)
                ids_senderos.update(der)
                    
    return list(agregados.values()), ids_senderos


# --------------------------- CLI ---------------------------

def main():
    # Argumentos
    p = argparse.ArgumentParser(
        description="Detecta posibles cruces peatonales discontinuos (OSM)."
    )
    grupo = p.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--bbox", help="south,west,north,east")
    grupo.add_argument("--place", help="Nombre de ciudad o zona (Nominatim)")
    p.add_argument("-o", "--output", default="cruces_peatonales_salida.geojson")
    p.add_argument("--buffer", type=float, default=20.0,
                   help="Radio del buffer en metros para buscar sendero al otro lado")
    args = p.parse_args()

    # Definición del marco geográfico: bbox o lugar.
    # Prioridad del bbox sobre el lugar.
    if args.bbox:
        try:
            bbox = tuple(float(x) for x in args.bbox.split(","))
            assert len(bbox) == 4
        except (ValueError, AssertionError):
            sys.exit("bbox invalido. Formato: south,west,north,east")
    else:
        print(f"Resolviendo lugar: {args.place}")
        bbox = obtener_bbox_nominatim(args.place, HEADERS)
        print(f"  bbox = {bbox}")

    s, w, n, e = bbox
    lon0, lat0 = (w + e) / 2, (s + n) / 2

    # Envía la consulta a Overpass
    print("Consultando las vías en Overpass...")
    osm_ways = consultar_overpass(bbox, header=HEADERS)

    # Extrae los segmentos para vías peatonales y motorizadas
    print("Construyendo geometrias...")
    peatonales, motorizadas = construir_geometrias(osm_ways, lon0, lat0)
    print(f"  senderos peatonales: {len(peatonales)} | vias motorizadas: {len(motorizadas)}")

    # Detecta los cruces peatonales discontinuos
    print(f"Detectando cruces (buffer {args.buffer} m)...")
    cruces, ids_senderos_cruces = detectar_cruces(peatonales, motorizadas, lon0, lat0, buffer_m=args.buffer)
    senderos_cruces = [p for p in peatonales if p["id"] in ids_senderos_cruces]
    print(f"  cruces seleccionados: {len(cruces)}")

    
    # Crea la carpeta de salida si no existe
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    ruta = data_dir / args.output

    # Exporta el resultado a GeoJSON
    geojson = a_geojson(cruces, args.buffer)
    senderos_geojson = senderos_a_geojson(senderos_cruces, lon0, lat0)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)
    with open(ruta.with_stem(ruta.stem + "_senderos"), "w", encoding="utf-8") as f:
        json.dump(senderos_geojson, f, ensure_ascii=False, indent=2)
    print(f"Guardado: {ruta}")    


if __name__ == "__main__":
    main()
