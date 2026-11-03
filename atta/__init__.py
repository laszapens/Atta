# -*- coding: utf-8 -*-
"""Atta — LLM-native parametric modeling workbench.

Atta is named after the leafcutter ant: small deterministic workers cutting
precise pieces, a colony that can build structures no single ant could.

Design commitments
------------------
* **Facts before pixels.** Geometry is measured, not guessed from renders.
* **One spec, one receipt.** Every model is rebuildable from its spec; every
  verdict carries a receipt.
* **Ten verbs, not eighty-eight ops.** Agents get a small semantic surface.
* **Blender is a backend, not the model.** Geometry is computed in numpy;
  Blender evaluates finish, renders, and exports.
* **Assumptions are not measurements.** Inferred defaults go into an explicit
  ledger and never change a verify verdict.
"""

from .version import VERSION, CODENAME

__version__ = VERSION
__all__ = ["__version__", "VERSION", "CODENAME"]

# Load the extended shape families (``atta/families/*``) once, at package
# import, so that *every* entry point sees the same op and recipe registry —
# including a bare ``from atta import features`` used to evaluate a feature
# graph.  The loader is tolerant: a broken third-party family is recorded and
# skipped rather than taking the engine down (set ATTA_FAMILIES_STRICT=1 to
# make import failures fatal; the test suite runs with it).
from . import families as _families  # noqa: E402

_families.load()
