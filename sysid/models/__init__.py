# =============================================================================
# SYSID/MODELS - Model Registry
# =============================================================================

from sysid.models.base import ModelDef, ModelMatrices
from sysid.models.rc1 import RC1
from sysid.models.rc2 import RC2
from sysid.models.rc3 import RC3
from sysid.models.rc2_quasi import RC2_QUASI

REGISTRY: dict[str, ModelDef] = {
    "1R1C":   RC1,
    "2R2C":   RC2,
    "2R2C-q": RC2_QUASI,
    "3R3C":   RC3,
}

DEFAULT_MODEL = "2R2C"


def get_model_def(name: str) -> ModelDef:
    if name not in REGISTRY:
        available = ", ".join(REGISTRY.keys())
        raise ValueError(f"Unknown model '{name}'. Available: {available}")
    return REGISTRY[name]


def list_models() -> str:
    lines = []
    for name, mdef in REGISTRY.items():
        marker = " (default)" if name == DEFAULT_MODEL else ""
        lines.append(f"  {name}{marker}: {mdef.description}")
    return "\n".join(lines)