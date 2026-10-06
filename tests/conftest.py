from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET_SERVICE_APP = ROOT / "services" / "target-service"
if str(TARGET_SERVICE_APP) not in sys.path:
    sys.path.insert(0, str(TARGET_SERVICE_APP))

OPTIMIZER_ROOT = ROOT
if str(OPTIMIZER_ROOT) not in sys.path:
    sys.path.insert(0, str(OPTIMIZER_ROOT))
