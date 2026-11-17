"""Regenerate ``jobindex/data/cities.tsv.gz``, the gazetteer behind the home-page globe
(``/places.json``, see ``jobindex/api/places.py``).

Source: GeoNames ``cities15000`` (every city with 15,000+ inhabitants), licensed
CC BY 4.0, https://www.geonames.org. Download and unzip it first, then run::

    curl -LO https://download.geonames.org/export/dump/cities15000.zip
    unzip cities15000.zip
    python scripts/make_cities.py cities15000.txt

Output, one city per line, tab-separated: country (ISO alpha-2), admin1 code (e.g. ``CA``
for California), latitude, longitude (2 decimals, ~1 km), population, display name, then
the keys the city is matched by (``places.place_key`` of the GeoNames name, its ASCII
form and, for large cities, Latin-script alternate names such as "Köln" for Cologne),
``|``-separated.
"""

import argparse
import csv
import gzip
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobindex.api.places import place_key

OUT = Path(__file__).resolve().parent.parent / "jobindex" / "data" / "cities.tsv.gz"
ALTERNATES_MIN_POPULATION = 300_000  # alternate names only for big cities (file size)


def latin(name: str) -> bool:
    if not 4 <= len(name) <= 40 or any(c.isdigit() for c in name):  # "BER", "HH", "A 832"
        return False
    return all(ord(c) < 0x250 or not c.isalpha() for c in name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("geonames", type=Path, help="unzipped cities15000.txt")
    args = parser.parse_args()
    rows = []
    with args.geonames.open(encoding="utf-8") as f:
        for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            name, ascii_name, alternates = r[1], r[2], r[3]
            lat, lng, cc, admin1, population = float(r[4]), float(r[5]), r[8], r[10], int(r[14])
            names = [name, ascii_name]
            if population >= ALTERNATES_MIN_POPULATION:
                names += [a for a in alternates.split(",") if a and latin(a)]
            keys: list[str] = []
            for n in names:
                key = place_key(n)
                if key and key not in keys:
                    keys.append(key)
            row = (cc, admin1, f"{lat:.2f}", f"{lng:.2f}", str(population), name, "|".join(keys))
            rows.append(row)
    rows.sort(key=lambda row: (row[0], -int(row[4])))
    OUT.parent.mkdir(exist_ok=True)
    with (
        gzip.GzipFile(OUT, "wb", compresslevel=9, mtime=0) as raw,
        io.TextIOWrapper(raw, encoding="utf-8") as f,
    ):
        f.write("# GeoNames cities15000 (CC BY 4.0, geonames.org); scripts/make_cities.py\n")
        f.writelines("\t".join(row) + "\n" for row in rows)
    print(f"{len(rows)} cities -> {OUT} ({OUT.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
