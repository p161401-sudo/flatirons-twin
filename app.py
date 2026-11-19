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

import station_service as mod

mod.ROOT = Path(BASE)

# 1) 强制监听 0.0.0.0
_RealServer = mod.ThreadingHTTPServer


class _Bound(_RealServer):
    def __init__(self, addr, handler, *a, **kw):
        host, port = addr
        super().__init__(("0.0.0.0", port), handler, *a, **kw)


mod.ThreadingHTTPServer = _Bound

# 2) 健康检查用 HEAD，按 GET 处理
mod.Handler.do_HEAD = mod.Handler.do_GET

# 3) 部署平台域名不是 localhost，放行 Host 校验
mod.Handler.safe_host = lambda self: True

if __name__ == "__main__":
    port = os.environ.get("PORT", "8000")
    sys.argv = ["station_service", "--port", str(port)]
    mod.main()
