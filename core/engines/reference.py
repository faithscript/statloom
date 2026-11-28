"""core/engines/reference.py: the reference engine (continuous and proportional metrics).

Navigation:
  ReferenceParams   confidence_level (default 0.95)
  ReferenceEngine   Welch's t-test (continuous) / two-proportion z-test (proportion)

For each treatment variant versus the control: effect = treatment - control, its
standard error, a confidence interval, and a two-sided p-value. Estimates are
computed directly so each formula can be checked by hand; SciPy supplies only the t
and normal distribution functions. Deterministic: no randomness, clock, or environment.

Proportions follow the usual two-proportion z-test convention: the p-value uses the
pooled standard error, the reported SE and interval use the unpooled one, so the
interval and p-value can differ very slightly near the significance boundary.
No multiple-comparison correction is applied across treatments (Q14).
"""
import math

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy import stats

from core.engines.base import Engine
from core.engines.contract import CONTRACT_VERSION, Comparison, EngineInput, EngineOutput
from core.engines.errors import DegenerateData
from core.engines.registry import register
from core.metrics.contract import MetricType


class ReferenceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confidence_level: float = Field(default=0.95, gt=0, lt=1)


def _welch(control: tuple[float, ...], treat: tuple[float, ...], level: float, names: tuple[str, str]):
    xc, xt = np.asarray(control, dtype=float), np.asarray(treat, dtype=float)
    nc, nt = len(xc), len(xt)
    mc, mt = float(xc.mean()), float(xt.mean())
    vc, vt = float(xc.var(ddof=1)), float(xt.var(ddof=1))
    a, b = vc / nc, vt / nt
    if a + b == 0:
        raise DegenerateData(f"variant '{names[1]}' vs control: no variance in either group")
    se = math.sqrt(a + b)
    df = (a + b) ** 2 / (a**2 / (nc - 1) + b**2 / (nt - 1))   # Welch-Satterthwaite
    effect = mt - mc
    p = float(2 * stats.t.sf(abs(effect / se), df))
    crit = float(stats.t.ppf(1 - (1 - level) / 2, df))
    return nc, nt, mc, mt, effect, se, effect - crit * se, effect + crit * se, p, "welch_t_test"


def _two_prop(control: tuple[float, ...], treat: tuple[float, ...], level: float, names: tuple[str, str]):
    nc, nt = len(control), len(treat)
    sc, st = sum(control), sum(treat)
    pc, pt = sc / nc, st / nt
    pooled = (sc + st) / (nc + nt)
    if pooled in (0.0, 1.0):
        raise DegenerateData(f"variant '{names[1]}' vs control: every observation is identical (all 0 or all 1)")
    effect = pt - pc
    se_unpooled = math.sqrt(pt * (1 - pt) / nt + pc * (1 - pc) / nc)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / nt + 1 / nc))
    p = float(2 * stats.norm.sf(abs(effect / se_pooled)))
    crit = float(stats.norm.ppf(1 - (1 - level) / 2))
    return (nc, nt, pc, pt, effect, se_unpooled, effect - crit * se_unpooled,
            effect + crit * se_unpooled, p, "two_proportion_z_test")


@register("reference")
class ReferenceEngine(Engine):
    name = "reference"
    version = "1.0.0"
    supports = frozenset({MetricType.CONTINUOUS, MetricType.PROPORTION})
    params_model = ReferenceParams

    def run(self, inp: EngineInput) -> EngineOutput:
        level = inp.params.confidence_level
        test = _welch if inp.metric_type is MetricType.CONTINUOUS else _two_prop
        control = inp.samples[inp.control_variant]
        out = []
        for variant, xs in inp.samples.items():   # variants are sorted: deterministic order
            if variant == inp.control_variant:
                continue
            nc, nt, mc, mt, eff, se, lo, hi, p, method = test(control, xs, level, (inp.control_variant, variant))
            out.append(Comparison(control=inp.control_variant, treatment=variant, n_control=nc,
                                  n_treatment=nt, mean_control=mc, mean_treatment=mt, effect=eff,
                                  effect_se=se, ci_lower=lo, ci_upper=hi, confidence_level=level,
                                  p_value=min(1.0, max(0.0, p)), method=method))
        return EngineOutput(contract_version=CONTRACT_VERSION, comparisons=tuple(out))
