"""FastAPI-dependent adapters for `core.performance`.

This sub-package is the only place in `core.performance` allowed to
import FastAPI/Starlette, per `docs/PerformancePlan.md` Phase 4. Nothing
under `core.performance` outside this package (or `dashboard/`) may
import from here.
"""
