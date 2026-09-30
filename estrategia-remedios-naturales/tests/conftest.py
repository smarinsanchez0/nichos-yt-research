"""Un unico directorio de datos temporal para TODA la suite (config.DATA_DIR se fija al importar `app`)."""
from __future__ import annotations
import os
import tempfile

os.environ.setdefault("ERN_DATA_DIR", tempfile.mkdtemp(prefix="ern-test-"))
# Ningun test puede tocar la red real: sin consulta de saldo (GET /api/v1/me) salvo las que la fakean explicitamente.
os.environ.setdefault("F5_TRACK_BALANCE", "0")


# ---- Salvaguarda: ningun test puede abrir una conexion a un host que no sea loopback (0 llamadas reales a proveedores).
import socket  # noqa: E402

_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex


def _is_local(addr) -> bool:
    if isinstance(addr, (str, bytes)):                     # socket unix
        return True
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")


def _guard(orig):
    def inner(self, addr, *a, **k):
        if not _is_local(addr):
            raise RuntimeError(f"RED REAL BLOQUEADA EN TESTS: intento de conexion a {addr!r}")
        return orig(self, addr, *a, **k)
    return inner


socket.socket.connect = _guard(_orig_connect)
socket.socket.connect_ex = _guard(_orig_connect_ex)
