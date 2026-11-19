import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / "site"
if not BASE.is_dir() or not any(BASE.iterdir()):
    BASE.mkdir(exist_ok=True)
    with zipfile.ZipFile(HERE / "site.zip") as zf:
        zf.extractall(BASE)

os.chdir(BASE)
sys.path.insert(0, str(BASE))
os.environ.setdefault("FLATIRONS_PUBLIC", "1")
os.environ.setdefault("BIND_HOST", "0.0.0.0")

import station_service as mod

mod.ROOT = Path(BASE)
if __name__ == "__main__":
    port = os.environ.get("PORT", "8000")
    sys.argv = ["station_service", "--port", str(port)]
    mod.main()
