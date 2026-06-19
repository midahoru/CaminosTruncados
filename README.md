# cruces_peatonales

Identifica posibles puntos en los que un sendero
peatonal (sin contar los andenes) es cruzado por una vía vehícular,
usando la información de OpenStreetMap (Overpass).




Salida: GeoJSON de puntos de cruce.

Para usarlo:
- Por bounding box (`south,west,north,east`):
python cruces_peatonales_detector.py --bbox "4.648,-74.070,4.652,-74.065" -o cruces_peatonales_salida.geojson
- Por nombre de lugar (Nominatim):
python cruces_peatonales_detector.py --place "Chapinero, Bogotá" -o cruces_peatonales_salida.geojson


---

## Project Structure

```
CaminosTruncados/│
├── src/
│   ├── cruces_peatonales_detector.py        # Generates isochrones. Returns a GeoJSON
│
    ├── data/                                # Stores the resulting data
│
├── requirements.txt
└── README.md                         # This file
```

## Cómo funciona

1. Busca los puntos en los que los senderos peatonales (`pedestrian`, `footway`, `steps`, `corridor`, `path`, `bridleway`) se
   cruzan con una vía para vehículos motorizados (`motorway`, `trunk`, `primary`, `secondary`, `tertiary`, `unclassified`, `residential`, `motorway_link`, `trunk_link`, `primary_link`, `secondary_link`, `tertiary_link`, `living_street`,  `road`, `busway`).
2. Evalúa cada vía motorizada que llega a cada intersección, buscando si hay senderos peatonales a ambos lados, en un buffer parametrizable al rededor de la intersección. En la mayoría de casos, esto representa un sendero peatonal discontinuo. En caso de encontrar un sendero en ambos lados, la intersección de alamcena, de lo contrario se ignora.   
3. Exporta un GeoJSON de puntos.



## Instalación

```bash
pip install shapely requests
```


## Argumentos


--bbox: Bounding box en formato "south,west,north,east" para delimitar el área de consulta. 
--place: Nombre de ciudad o zona para delimitar el área de consulta (resuelto con Nominatim).
--output: Archivo de salida en formato GeoJSON (default: cruces_peatonales_salida.geojson).
--buffer: Radio del buffer en metros para buscar sendero al otro lado de la vía (default: 20.0).

En caso de definir `bbox` y `place`, se prioriza el primero.

## Uso

Por bounding box (`south,west,north,east`):

```bash
python cruces_peatonales_detector.py --bbox "4.648,-74.070,4.652,-74.065" -o cruces_peatonales_salida.geojson
```

Por nombre de lugar (Nominatim):

```bash
python cruces_peatonales_detector.py --place "Chapinero, Bogotá" -o cruces_peatonales_salida.geojson
```

Cambiar el buffer (en metros):

```bash
python cruces_peatonales_detector.py --bbox "..." --buffer 30
```


## Salida

`FeatureCollection` de puntos con propiedades:

- `walkable_way_ids`: senderos caminables involucrados (ambos lados).
- `crossing_path_ids`: senderos que cruzan la vía en ese punto.
- `motor_way_ids`: vías motorizadas involucradas.
- `n_walkable_ways`: número de senderos distintos cerca del cruce.
- `buffer_m`: buffer usado.
- `shared_node`: `true` si el cruce comparte un nodo OSM (conexión mapeada),
  `false` si se cruzan solo geométricamente.

## Parámetros internos

En `detectar_cruces` (valores por defecto): `buffer_m=20`, `paso=2.0` (muestreo a lo
largo de los senderos), `tol=0.5` (distancia perpendicular mínima en metros para
asignar un lado) y `near=1.0` (ignora puntos pegados al cruce).

## Notas

- Overpass es un servicio público; bajo carga puede responder lento o con error. El
  script reintenta automáticamente. Para zonas grandes, divide el bbox.
- Las coordenadas se exportan en EPSG:4326 (lon, lat).
