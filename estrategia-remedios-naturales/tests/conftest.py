"""Un unico directorio de datos temporal para TODA la suite (config.DATA_DIR se fija al importar `app`)."""
from __future__ import annotations
import os
import tempfile

os.environ.setdefault("ERN_DATA_DIR", tempfile.mkdtemp(prefix="ern-test-"))
