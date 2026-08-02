"""Application lifecycle management for sync and async contexts.

Design
------
ApplicationLifespan owns:
    - startup/shutdown sequencing
    - container registration
    - lifecycle state (what has started, in what order)

It does NOT own:
    - how to connect to a given database
    - what tables to create
    - what SQL to run

Each subsystem (data warehouse, service database, ...) is a small
`LifecycleStep`. A step knows how to start and stop *itself* and returns
the objects that should be registered in the container. ApplicationLifespan
just iterates the list of steps in order on startup, and in reverse order
on shutdown. Adding a new subsystem means adding one LifecycleStep to the
list below — there's no second place to remember to wire it up, which is
what caused the container-registration and shutdown bugs in the previous
version (a subsystem was connected but its shutdown/registration logic
lived somewhere else and got missed).
"""

import asyncio
import contextlib
import functools
import logging
import os
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

from core.app.container import DependencyContainer
from core.caching.persistence_queue import start_persistence_queue, stop_persistence_queue
from core.concurrency.executors import shutdown_all_executors
from core.db.session import DatabaseSession
from core.storage.service_db import ServiceDatabase
from core.service_registry import ServiceManager
from core.observability.write_queue import ObservabilityWriteQueue
from core.observability.context import write_observability_record

# Optional: this codebase's core.performance instrumentation (tracing,
# metrics, pool/SQL adapters, resource collectors — see
# docs/performance/README.md). Guarded the same way the OpenTelemetry
# wiring below is: ApplicationLifespan must keep working with
# core.performance absent.
try:
    from core.performance.collectors import CollectorScheduler, build_enabled_collectors
    from core.performance.config import PerformanceConfig
    from core.performance.registry import get_default_registry
except Exception:
    CollectorScheduler = build_enabled_collectors = None  # type: ignore[assignment,misc]
    PerformanceConfig = get_default_registry = None  # type: ignore[assignment,misc]

# Optional: bridges the performance registry's counters/gauges/
# histograms onto OpenTelemetry metrics. This class existed in the
# codebase but nothing ever called it on any schedule — see
# PerformanceStep's docstring below for how/why that's now wired up.
try:
    from core.performance.exporters.otel_exporter import OTelExporter
except Exception:
    OTelExporter = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


# ============================================================
# Lifecycle step contract
# ============================================================


class LifecycleStep(ABC):
    """A single subsystem's slice of application startup/shutdown.

    A step is responsible only for its own subsystem: connecting,
    initializing, and tearing down. It reports back what should be
    registered in the container; it never touches the container directly.
    That keeps all registration decisions in ApplicationLifespan, in one
    place, instead of scattered across every subsystem.
    """

    #: Short name used in logs when a step fails.
    name: str = "unnamed_step"

    @abstractmethod
    def startup_sync(self) -> Dict[str, Any]:
        """Run sync startup work.

        Returns:
            Mapping of {registration_key: instance} to register in the
            container. Return an empty dict if this step is disabled
            (e.g. no config was provided for it).
        """
        raise NotImplementedError

    @abstractmethod
    def shutdown_sync(self) -> None:
        """Run sync shutdown/cleanup work. Must be safe to call even if
        startup_sync returned an empty dict (i.e. this step never started)."""
        raise NotImplementedError

    async def startup_async(self) -> Dict[str, Any]:
        """Run async startup work. Default: delegate to the sync version.

        Override this for subsystems with real async connection code
        (e.g. an async DB driver). Subsystems that are inherently
        blocking (e.g. sqlite3) should override this to offload the
        sync call with `asyncio.to_thread`, not pretend to be async.
        """
        return self.startup_sync()

    async def shutdown_async(self) -> None:
        """Run async shutdown work. Default: delegate to the sync version."""
        self.shutdown_sync()


# ============================================================
# Concrete steps
# ============================================================


class DataWarehouseStep(LifecycleStep):
    """Owns the DuckDB data warehouse connection."""

    name = "data_warehouse"

    def __init__(self, db_config):
        self.db_config = db_config
        self.db_session: Optional[DatabaseSession] = None

    def startup_sync(self) -> Dict[str, Any]:
        if not self.db_config:
            return {}
        self.db_session = DatabaseSession(self.db_config)
        self.db_session.initialize_sync()
        return {"db_session": self.db_session}

    async def startup_async(self) -> Dict[str, Any]:
        if not self.db_config:
            return {}
        self.db_session = DatabaseSession(self.db_config)
        await self.db_session.initialize()
        logger.info("Database session initialized (async mode)")
        return {"db_session": self.db_session}

    def shutdown_sync(self) -> None:
        if self.db_session and self.db_session._sync_pool:
            logger.info("Closing sync database connection")

    async def shutdown_async(self) -> None:
        if self.db_session and self.db_session._async_pool:
            logger.info("Closing async database connection")


class ServiceDatabaseStep(LifecycleStep):
    """Owns the SQLite service database (api keys, users, logging,
    tracing, caching, audit) and the ServiceManager built on top of it.

    ServiceDatabase itself only knows connect/create_tables/CRUD; this
    step is what decides *when* those happen and hands the resulting
    domain services to the container.
    """

    name = "service_database"

    def __init__(
        self,
        service_db_path: Optional[str],
        *,
        pool_min_size: int = 2,
        pool_max_size: int = 8,
    ):
        self.service_db_path = service_db_path
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.service_db: Optional[ServiceDatabase] = None
        self.service_manager: Optional[ServiceManager] = None
        self.observability_queue: Optional[ObservabilityWriteQueue] = None

    def startup_sync(self) -> Dict[str, Any]:
        if not self.service_db_path:
            return {}

        self.service_db = ServiceDatabase(
            self.service_db_path,
            min_size=self.pool_min_size,
            max_size=self.pool_max_size,
        )
        self.service_db.connect()
        self.service_db.create_tables()
        self.service_db.initialize_admin_user()
        self.service_db.cleanup_expired_cache()

        self.service_manager = ServiceManager(self.service_db)

        # Background flush queue for request logging/tracing/audit — see
        # core/observability/write_queue.py's docstring for why this
        # exists: batching writes into one transaction per request
        # still meant every request queued for SQLite's single writer.
        # This takes the write out of the request path entirely.
        self.observability_queue = ObservabilityWriteQueue(
            self.service_db,
            write_record=functools.partial(
                write_observability_record, self.service_manager
            ),
        )
        self.observability_queue.start()
        # emit_request_observability() looks for this attribute on
        # service_manager to decide whether to enqueue (fast path) or
        # write synchronously (fallback, e.g. in tests).
        self.service_manager.observability_queue = self.observability_queue

        return {
            "service_db": self.service_db,
            "service_manager": self.service_manager,
            "cache_service": self.service_manager.caching,
            "logging_service": self.service_manager.logging,
            "tracing_service": self.service_manager.tracing,
            "audit_service": self.service_manager.audit,
            "api_key_service": self.service_manager.api_keys,
            "user_service": self.service_manager.users,
        }

    async def startup_async(self) -> Dict[str, Any]:
        # sqlite3 is blocking; run the real startup off the event loop
        # thread rather than faking async support for it.
        return await asyncio.to_thread(self.startup_sync)

    def shutdown_sync(self) -> None:
        if self.observability_queue:
            # Stop the flush thread and write anything still queued —
            # otherwise the last batch of request logs before shutdown
            # would silently be lost.
            self.observability_queue.stop()
        if self.service_db:
            self.service_db.disconnect()

    async def shutdown_async(self) -> None:
        if self.observability_queue:
            await asyncio.to_thread(self.observability_queue.stop)
        if self.service_db:
            await asyncio.to_thread(self.service_db.disconnect)


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean env var, matching PerformanceConfig._read_bool's rules."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class PerformanceStep(LifecycleStep):
    """Owns the `core.performance` registry and its optional background
    resource-collector scheduler (CPU/memory/GC/threads/asyncio/process —
    see `docs/performance/collectors-exporters-dashboard.md`), plus an
    optional background task that bridges that registry onto OpenTelemetry.

    Registers the process-wide default registry so
    `install_performance_middleware`/`install_performance_dashboard`
    (installed on the app in `core.app.api.app.create_app`) and this
    step agree on which registry they're both touching, without either
    one having to construct or pass one around explicitly.

    Disabled by default, matching `PerformanceConfig`'s own
    fail-safe-disabled philosophy: with `PERF_ENABLED` unset, this step
    still registers the (empty, inert) registry but starts no
    background work. The collector scheduler and the OTel export loop
    only ever run in async mode — sync mode (e.g. `LifecycleMode.SYNC`,
    used by tests and scripts) has no running event loop to host them
    on, the same constraint `ServiceDatabaseStep` documents for sqlite3.

    OTel export: `core.performance.exporters.otel_exporter.OTelExporter`
    turns the registry's current counters/gauges/histograms into OTel
    metric instruments on every call to `.export()`, but nothing used to
    call it on any schedule — it sat unused in the codebase. This step
    now runs it every `PERF_OTEL_EXPORT_INTERVAL_SECONDS` (default 15s)
    for the lifetime of the app, so request/DB/pool/auth timings and
    resource-collector samples flow through the same OTLP pipeline
    `core.observability.otel.OpenTelemetryManager` already pushes traces
    (and its own db/cache metrics) through. `OTelExporter` reads the
    global meter via `opentelemetry.metrics.get_meter(...)`, so it picks
    up whatever `MeterProvider` `get_otel_manager()` configured in
    `ApplicationLifespan.__init__` below — that call happens first, so
    the provider is already in place by the time this step starts.
    Controlled by `PERF_EXPORT_OTEL` (defaults on whenever the
    performance module itself is enabled); set it to `0`/`false` to keep
    the registry populated (e.g. for the live dashboard) without also
    pushing it to OTel.
    """

    name = "performance"

    def __init__(self) -> None:
        self.scheduler: Optional[Any] = None
        self._otel_export_task: Optional["asyncio.Task[None]"] = None

    def startup_sync(self) -> Dict[str, Any]:
        if PerformanceConfig is None or get_default_registry is None:
            return {}
        registry = get_default_registry()
        return {"performance_registry": registry}

    async def startup_async(self) -> Dict[str, Any]:
        if PerformanceConfig is None or get_default_registry is None:
            return {}
        registry = get_default_registry()
        # PerformanceConfig.from_env() intentionally does not read the
        # collect_* flags (no env var wired up for them yet — see
        # docs/performance/configuration.md), so pull them in here
        # ourselves via dataclasses.replace rather than silently getting
        # an all-False config and starting no collectors.
        config = replace(
            PerformanceConfig.from_env(),
            collect_memory=_env_flag("PERF_COLLECT_MEMORY"),
            collect_gc=_env_flag("PERF_COLLECT_GC"),
            collect_threads=_env_flag("PERF_COLLECT_THREADS"),
            collect_cpu=_env_flag("PERF_COLLECT_CPU"),
            collect_asyncio=_env_flag("PERF_COLLECT_ASYNCIO"),
            collect_process=_env_flag("PERF_COLLECT_PROCESS"),
        )
        if config.enabled and build_enabled_collectors is not None:
            collectors = build_enabled_collectors(config)
            if collectors:
                self.scheduler = CollectorScheduler(collectors, registry)
                self.scheduler.start()
                logger.info(
                    "Performance resource collectors started: %s",
                    [c.name for c in collectors],
                )

        # Bridge the performance registry onto OpenTelemetry on a fixed
        # interval. Gated on the performance module itself being enabled
        # (no point running an export loop over an always-empty
        # registry) and, separately, on OTelExporter having imported
        # successfully.
        if (
            config.enabled
            and OTelExporter is not None
            and _env_flag("PERF_EXPORT_OTEL", default=True)
        ):
            interval_seconds = float(
                os.getenv("PERF_OTEL_EXPORT_INTERVAL_SECONDS", "15")
            )
            self._otel_export_task = asyncio.ensure_future(
                self._run_otel_export(registry, interval_seconds)
            )
            logger.info(
                "Performance-to-OTel export started (interval=%.1fs)",
                interval_seconds,
            )

        return {"performance_registry": registry}

    @staticmethod
    async def _run_otel_export(registry: Any, interval_seconds: float) -> None:
        """Periodically push the registry's current state onto OTel.

        Runs for the lifetime of the app; cancellation (on shutdown) is
        the normal, expected way this loop ends.
        """
        exporter = OTelExporter()
        while True:
            try:
                exporter.export(registry)
            except Exception:
                logger.warning(
                    "Performance-to-OTel export failed", exc_info=True
                )
            await asyncio.sleep(interval_seconds)

    def shutdown_sync(self) -> None:
        pass  # the scheduler/export loop are only ever started in async mode

    async def shutdown_async(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self._otel_export_task is not None:
            self._otel_export_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._otel_export_task
            self._otel_export_task = None


# ============================================================
# Cache persistence queue / executor shutdown (roadmap Phase 10)
# ============================================================


class PersistenceQueueStep(LifecycleStep):
    """Starts/stops the bounded cache-persistence queue
    (core.caching.persistence_queue) that QueryCacheCoordinator uses
    for out-of-band L2 writes and access-stat updates.

    Ordering matters here: this step is started *after*
    ServiceDatabaseStep (so it shows up later in `_steps`, meaning its
    shutdown runs *before* ServiceDatabaseStep's in the reversed
    shutdown walk -- see ApplicationLifespan's docstring). That's
    required by the roadmap's shutdown sequence: "stop accepting new
    background jobs, drain queue within timeout, [then] close service
    DB" -- persistence jobs write to the service DB, so it must still
    be open while the queue drains.
    """

    name = "persistence_queue"

    def startup_sync(self) -> Dict[str, Any]:
        start_persistence_queue()
        return {}

    async def startup_async(self) -> Dict[str, Any]:
        start_persistence_queue()
        return {}

    def shutdown_sync(self) -> None:
        # stop_persistence_queue() is async (it awaits queue.join());
        # there's no meaningful sync equivalent, so best-effort run it
        # to completion here rather than skip draining entirely.
        try:
            asyncio.run(stop_persistence_queue())
        except RuntimeError:
            # Already inside a running loop (shouldn't happen from the
            # sync shutdown path, but fail safe rather than crash
            # shutdown over it).
            logger.warning(
                "Could not synchronously drain cache persistence queue "
                "(already inside an event loop); some queued writes may "
                "be lost."
            )

    async def shutdown_async(self) -> None:
        await stop_persistence_queue()


class ExecutorsStep(LifecycleStep):
    """Owns shutdown of the shared db/service/background executors
    (core.concurrency.executors). Configuration/sizing happens eagerly
    at process startup (run_api.py's configure_executors() call, before
    the lifespan even begins) since it only depends on static pool
    config, not anything a LifecycleStep discovers at runtime -- this
    step exists purely to guarantee shutdown happens in the right slot:
    after the persistence queue has drained (so its jobs can still run)
    and after the service DB is closed, but before the main DB pool
    closes (roadmap Phase 10 shutdown ordering).
    """

    name = "executors"

    def startup_sync(self) -> Dict[str, Any]:
        return {}

    def shutdown_sync(self) -> None:
        shutdown_all_executors(wait=True)


# ============================================================
# ApplicationLifespan — sequencing, registration, state only
# ============================================================


class ApplicationLifespan:
    """Manages application startup and shutdown lifecycle for sync and
    async contexts.

    ApplicationLifespan is deliberately thin: it holds an ordered list of
    LifecycleSteps, runs them forward on startup and backward on shutdown,
    and registers whatever each step reports. It has no knowledge of what
    a "database" or "service" is beyond that contract.
    """

    def __init__(
        self,
        db_config=None,
        service_db_path: Optional[str] = None,
        mode: Literal["sync", "async"] = "sync",
        service_pool_min_size: Optional[int] = None,
        service_pool_max_size: Optional[int] = None,
    ):
        """Initialize application lifespan.

        Args:
            db_config: Optional data warehouse configuration
            service_db_path: Optional path to the SQLite service database
            mode: "sync" for sync operations, "async" for async operations
            service_pool_min_size: SQLite service-db pool floor. Defaults
                to `core.concurrency.cpu.recommended_sizing()` when not
                given, so the pool scales with the host's actual CPU
                count instead of a constant picked for one machine.
            service_pool_max_size: SQLite service-db pool ceiling; same
                default behavior as `service_pool_min_size`.
        """
        self.mode = mode
        self.container = DependencyContainer()

        if service_pool_min_size is None or service_pool_max_size is None:
            from core.concurrency.cpu import recommended_sizing

            sizing = recommended_sizing()
            service_pool_min_size = service_pool_min_size or sizing.service_pool_min
            service_pool_max_size = service_pool_max_size or sizing.service_pool_max

        # Explicitly initialize OpenTelemetry here rather than relying on
        # it happening implicitly the first time core.db.session gets
        # imported. get_otel_manager() is a memoized singleton, so this
        # is safe to call even if session.py (or anything else) also
        # triggers it — .initialize() only actually runs once, and this
        # makes the wiring an intentional, logged startup step instead of
        # a side effect buried in an unrelated import. It also has to run
        # before PerformanceStep (below), since that step's OTel export
        # loop reads the global meter provider this call sets up.
        try:
            from core.observability.otel import get_otel_manager

            get_otel_manager()
        except Exception:
            logger.warning(
                "OpenTelemetry initialization failed or unavailable; "
                "tracing/metrics export will be a no-op.",
                exc_info=True,
            )

        # The list of subsystems participating in lifecycle. To add a new
        # subsystem: write a LifecycleStep and append it here. Nothing
        # else needs to change.
        #
        # Order here is start order; shutdown runs in reverse. That
        # reversal is what gives the roadmap's required shutdown
        # sequence "drain persistence queue -> close service DB ->
        # close executors -> close main DB pool" just from listing
        # steps in the opposite sequence: DataWarehouse (owns the main
        # pool) starts first so it closes *last*; PersistenceQueueStep
        # starts last so it drains *first*, while ServiceDatabase and
        # the executors it depends on are still up.
        self._steps: List[LifecycleStep] = [
            PerformanceStep(),
            DataWarehouseStep(db_config),
            ExecutorsStep(),
            ServiceDatabaseStep(
                service_db_path,
                pool_min_size=service_pool_min_size,
                pool_max_size=service_pool_max_size,
            ),
            PersistenceQueueStep(),
        ]

        # Steps that successfully started, in start order. Shutdown walks
        # this in reverse, so a step that never started (or failed partway
        # through startup) is never asked to shut down out of order.
        self._started_steps: List[LifecycleStep] = []

    # ============= SYNC LIFECYCLE =============

    def startup_sync(self) -> None:
        """Execute sync startup tasks, in step order.

        Raises:
            Exception: If any step's initialization fails
        """
        try:
            for step in self._steps:
                registrations = step.startup_sync()
                self._register(registrations)
                self._started_steps.append(step)
            logger.info("Application startup completed")
        except Exception as e:
            logger.error(f"Application startup failed: {e}")
            raise

    def shutdown_sync(self) -> None:
        """Execute sync shutdown tasks, in reverse step order."""
        try:
            for step in reversed(self._started_steps):
                step.shutdown_sync()
            self._started_steps.clear()
            self.container.clear()
            logger.info("Application shutdown completed")
        except Exception as e:
            logger.error(f"Application shutdown failed: {e}")
            raise

    # ============= ASYNC LIFECYCLE =============

    async def startup_async(self) -> None:
        """Execute async startup tasks, in step order.

        Raises:
            Exception: If any step's initialization fails
        """
        try:
            for step in self._steps:
                registrations = await step.startup_async()
                self._register(registrations)
                self._started_steps.append(step)
            logger.info("Application startup completed")
        except Exception as e:
            logger.error(f"Application startup failed: {e}")
            raise

    async def shutdown_async(self) -> None:
        """Execute async shutdown tasks, in reverse step order."""
        try:
            for step in reversed(self._started_steps):
                await step.shutdown_async()
            self._started_steps.clear()
            self.container.clear()
            logger.info("Application shutdown completed")
        except Exception as e:
            logger.error(f"Application shutdown failed: {e}")
            raise

    @asynccontextmanager
    async def lifespan_context(self) -> AsyncGenerator[None, None]:
        """FastAPI lifespan context manager for proper lifecycle management.

        Usage with FastAPI:
            lifespan = ApplicationLifespan(db_config, mode="sync")

            @asynccontextmanager
            async def lifespan(app):
                async with lifespan.lifespan_context():
                    yield

            app = FastAPI(lifespan=lifespan)

        Yields:
            None after startup is complete
        """
        if self.mode == "sync":
            self.startup_sync()
        else:
            await self.startup_async()

        yield

        if self.mode == "sync":
            self.shutdown_sync()
        else:
            await self.shutdown_async()

    # ============= REGISTRATION =============

    def _register(self, registrations: Dict[str, Any]) -> None:
        """Register a step's outputs into the container.

        This is the single place that knows how the container's API
        works (e.g. that the db session has its own setter). Steps never
        touch the container directly.
        """
        for key, value in registrations.items():
            if key == "db_session":
                self.container.set_database_session(value)
            else:
                self.container.register(key, value)

    # ============= UTILITIES =============

    def get_container(self) -> DependencyContainer:
        """Get dependency container.

        Returns:
            DependencyContainer instance
        """
        return self.container

    def get_db_session(self) -> Optional[DatabaseSession]:
        """Get the data warehouse session, if the data warehouse step ran.

        Returns:
            DatabaseSession instance or None
        """
        for step in self._steps:
            if isinstance(step, DataWarehouseStep):
                return step.db_session
        return None

    def get_service_manager(self) -> Optional[ServiceManager]:
        """Get the service manager, if the service database step ran.

        Returns:
            ServiceManager instance or None
        """
        for step in self._steps:
            if isinstance(step, ServiceDatabaseStep):
                return step.service_manager
        return None
