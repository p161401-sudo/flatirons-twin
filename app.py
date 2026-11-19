import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 站点文件直接解压到仓库根目录，使 service 的 ROOT 天然等于数据目录
if not (HERE / "data.js").exists():
    with zipfile.ZipFile(HERE / "site.zip") as zf:
        zf.extractall(HERE)

os.chdir(HERE)
sys.path.insert(0, str(HERE))
os.environ.setdefault("FLATIRONS_PUBLIC", "1")

import station_service as mod

mod.ROOT = HERE

_RealServer = mod.ThreadingHTTPServer


class _Bound(_RealServer):
    def __init__(self, addr, handler, *a, **kw):
        host, port = addr
        super().__init__(("0.0.0.0", port), handler, *a, **kw)


mod.ThreadingHTTPServer = _Bound
mod.Handler.do_HEAD = mod.Handler.do_GET
mod.Handler.safe_host = lambda self: True

if __name__ == "__main__":
    port = os.environ.get("PORT", "8000")
    sys.argv = ["station_service", "--port", str(port)]
    mod.main()
