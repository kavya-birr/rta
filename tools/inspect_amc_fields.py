"""One-off: inspect how CAMS and KFintech store AMC info in their feed files."""
import glob
import os

import pandas as pd
from dbfread import DBF

sep = os.sep
UPLOAD_DIR = "examples/django_reference/uploaded_files"


def basename(p):
    return p.split(sep)[-1]


print("=" * 80)
print("CAMS (DBF format)")
print("=" * 80)
for f in glob.glob(f"{UPLOAD_DIR}/*.dbf"):
    print(f"\nFile: {basename(f)}")
    rows = list(DBF(f, load=True, char_decode_errors="ignore"))
    if not rows:
        continue
    print(
        f"  Sample row: AMC_CODE={rows[0].get('AMC_CODE')!r}, "
        f"PRODCODE={rows[0].get('PRODCODE')!r}, "
        f"SCHEME={rows[0].get('SCHEME')!r}"
    )
    amcs = {}
    for r in rows:
        c = str(r.get("AMC_CODE", "")).strip()
        sc = str(r.get("SCHEME", "")).strip()
        if c and c not in amcs:
            amcs[c] = sc
    print(f"  Unique AMC_CODE values ({len(amcs)}):")
    for code, example in sorted(amcs.items()):
        print(f"    AMC_CODE={code:<4}  example scheme: {example[:55]}")

print()
print("=" * 80)
print("KFintech (WBTRN CSV format)")
print("=" * 80)
for f in glob.glob(f"{UPLOAD_DIR}/*WBTRN*.csv"):
    print(f"\nFile: {basename(f)}")
    df = pd.read_csv(f, dtype=str)
    row0 = df.iloc[0]
    print(
        f"  Sample row: Fund={row0.get('Fund')!r}, "
        f"Product Code={row0.get('Product Code')!r}, "
        f"Scheme Code={row0.get('Scheme Code')!r}, "
        f"Fund Description={str(row0.get('Fund Description'))[:60]!r}"
    )
    funds = df[["Fund", "Fund Description"]].drop_duplicates().sort_values("Fund")
    print(f"  Unique Fund codes ({len(funds)}):")
    for _, r in funds.iterrows():
        name = str(r["Fund Description"]).split("-")[0].strip()[:50]
        print(f"    Fund={str(r['Fund']):<5}  example: {name}")
