"""Demo application for cjm-fasthtml-job-monitor library.

Demonstrates the job monitor UI with a real forced alignment plugin.
Shows the full lifecycle: trigger -> progress modal -> logs/resources tabs -> completion.

Run with: python demo_app.py
"""

from pathlib import Path


# -------------------------------------------------------------------------
# Test data — two sources for multi-source sequence testing
# -------------------------------------------------------------------------
TEST_DIR = Path(__file__).parent / "test_files"

TEST_SOURCES = [
    {
        "audio": TEST_DIR / "short_test_audio.mp3",
        "text": (TEST_DIR / "short_test_audio.txt").read_text().strip(),
        "label": "short_test_audio.mp3",
    },
    {
        "audio": TEST_DIR / "02 - 1. Laying Plans.mp3",
        "text": (TEST_DIR / "02 - 1. Laying Plans.txt").read_text().strip(),
        "label": "02 - 1. Laying Plans.mp3",
    },
]

# Keep single-source reference for display
TEST_AUDIO = TEST_SOURCES[0]["audio"]
TEST_TEXT = TEST_SOURCES[0]["text"]


def main():
    """Initialize job monitor demo and start the server."""
    import asyncio
    import tempfile

    from fasthtml.common import (
        fast_app, Div, H1, H2, P, Span, Button, Script, Pre, Code,
        APIRouter, FileResponse,
    )

    # DaisyUI components
    from cjm_fasthtml_daisyui.core.resources import get_daisyui_headers
    from cjm_fasthtml_daisyui.core.testing import create_theme_persistence_script
    from cjm_fasthtml_daisyui.components.actions.button import btn, btn_colors, btn_sizes, btn_styles
    from cjm_fasthtml_daisyui.components.data_display.badge import badge, badge_colors
    from cjm_fasthtml_daisyui.utilities.semantic_colors import bg_dui, text_dui, border_dui

    # Tailwind utilities
    from cjm_fasthtml_tailwind.utilities.spacing import p, m
    from cjm_fasthtml_tailwind.utilities.sizing import container, max_w, w, min_h
    from cjm_fasthtml_tailwind.utilities.typography import font_size, font_weight, font_family
    from cjm_fasthtml_tailwind.utilities.layout import position, overflow
    from cjm_fasthtml_tailwind.utilities.borders import border, rounded
    from cjm_fasthtml_tailwind.utilities.flexbox_and_grid import (
        flex_display, flex_direction, items, justify, gap,
    )
    from cjm_fasthtml_tailwind.core.base import combine_classes

    # App core
    from cjm_fasthtml_app_core.core.routing import register_routes
    from cjm_fasthtml_app_core.core.htmx import handle_htmx_request

    # Interactions library
    from cjm_fasthtml_interactions.core.state_store import get_session_id

    # State store
    from cjm_workflow_state.state_store import SQLiteWorkflowStateStore

    # Plugin system
    from cjm_plugin_system.core.manager import PluginManager
    from cjm_plugin_system.core.scheduling import QueueScheduler
    from cjm_plugin_system.core.queue import JobQueue

    # Lucide icons
    from cjm_fasthtml_lucide_icons.factory import lucide_icon

    # Job monitor library (this library)
    from cjm_fasthtml_job_monitor.html_ids import JobMonitorHtmlIds
    from cjm_fasthtml_job_monitor.models import JobMonitorUrls, JobMonitorConfig
    from cjm_fasthtml_job_monitor.services.monitor import JobMonitorService
    from cjm_fasthtml_job_monitor.routes.init import init_job_monitor_routes, check_inflight_job
    from cjm_fasthtml_job_monitor.components.trigger import render_job_trigger
    from cjm_fasthtml_job_monitor.components.overlay import render_job_overlay_placeholder
    from cjm_fasthtml_job_monitor.components.modal import get_sse_headers

    print("\n" + "=" * 70)
    print("Initializing cjm-fasthtml-job-monitor Demo")
    print("=" * 70)

    # -------------------------------------------------------------------------
    # App setup
    # -------------------------------------------------------------------------
    app, rt = fast_app(
        pico=False,
        hdrs=[*get_daisyui_headers(), create_theme_persistence_script(), *get_sse_headers()],
        title="Job Monitor Demo",
        htmlkw={'data-theme': 'light'},
        secret_key="demo-secret-key",
    )

    router = APIRouter(prefix="")
    workflow_id = "demo-workflow"

    # State store (fresh for each launch)
    tmp_dir = tempfile.mkdtemp(prefix="jm-demo-")
    state_store = SQLiteWorkflowStateStore(
        db_path=str(Path(tmp_dir) / "state.db")
    )

    # -------------------------------------------------------------------------
    # Plugin system setup
    # -------------------------------------------------------------------------
    print("\nLoading plugins...")
    plugin_manager = PluginManager(scheduler=QueueScheduler())
    plugin_manager.discover_manifests()

    # Target plugin: forced alignment
    fa_plugin_name = "cjm-transcription-plugin-qwen3-forced-aligner"
    fa_meta = plugin_manager.get_discovered_meta(fa_plugin_name)
    fa_available = False

    if fa_meta:
        try:
            success = plugin_manager.load_plugin(fa_meta, {"language": "English"})
            fa_available = success
            print(f"  {fa_plugin_name}: {'loaded' if success else 'failed'}")
        except Exception as e:
            print(f"  {fa_plugin_name}: error - {e}")
    else:
        print(f"  {fa_plugin_name}: not found")

    # System monitor (optional, for GPU stats)
    sysmon_name = "cjm-system-monitor-nvidia"
    sysmon_meta = plugin_manager.get_discovered_meta(sysmon_name)
    sysmon_available = False

    if sysmon_meta:
        try:
            success = plugin_manager.load_plugin(sysmon_meta)
            sysmon_available = success
            print(f"  {sysmon_name}: {'loaded' if success else 'failed'}")
        except Exception as e:
            print(f"  {sysmon_name}: error - {e}")
    else:
        print(f"  {sysmon_name}: not found (Resources tab will show CPU/RAM only)")

    if not fa_available:
        print("\n  WARNING: FA plugin not available. Demo will show trigger button")
        print("  but clicking it will fail. Install the plugin via plugins_test.yaml.")

    # -------------------------------------------------------------------------
    # Job queue
    # -------------------------------------------------------------------------
    queue = JobQueue(plugin_manager)

    # -------------------------------------------------------------------------
    # Job monitor service
    # -------------------------------------------------------------------------
    monitor_service = JobMonitorService(
        queue=queue,
        manager=plugin_manager,
        sysmon_plugin_name=sysmon_name if sysmon_available else None,
    )

    # -------------------------------------------------------------------------
    # Job args builder — returns list of (args, kwargs) for each source
    # -------------------------------------------------------------------------
    def build_fa_args(state_store, workflow_id, session_id):
        """Build arguments for the forced alignment plugin — one per source."""
        return [
            ((str(src["audio"]),), {"text": src["text"]})
            for src in TEST_SOURCES
        ]

    # -------------------------------------------------------------------------
    # Completion callback — receives list of results (one per source)
    # -------------------------------------------------------------------------
    result_store = {"last_results": None}

    async def on_fa_complete(results, request, sess):
        """Handle FA sequence completion — store results for display."""
        result_store["last_results"] = results

        # Summarize each source
        source_summaries = []
        for i, result in enumerate(results):
            word_count = len(result.get("items", [])) if isinstance(result, dict) else 0
            label = TEST_SOURCES[i]["label"] if i < len(TEST_SOURCES) else f"Source {i+1}"
            source_summaries.append(
                Div(
                    Span(f"{label}: ", cls=combine_classes(font_size.sm, font_weight.semibold)),
                    Span(f"{word_count} words aligned", cls=font_size.sm),
                    cls=m.b(1),
                )
            )

        summary = Div(
            Div(
                Span("Last Result", cls=combine_classes(font_weight.bold, font_size.sm)),
                cls=m.b(2),
            ),
            Div(
                Span("Status: ", cls=font_size.sm),
                Span("Completed", cls=combine_classes(badge, badge_colors.success, font_size.xs)),
                Span(f" ({len(results)} source{'s' if len(results) > 1 else ''})",
                     cls=font_size.sm),
                cls=m.b(1),
            ),
            *source_summaries,
            id="result-summary",
            hx_swap_oob="innerHTML:#result-summary",
            cls=combine_classes(p(4), rounded.lg, bg_dui.base_200, m.t(4)),
        )
        return [summary]

    async def on_fa_fail(job, request, sess):
        """Handle FA failure."""
        summary = Div(
            Div(
                Span("Last Result", cls=combine_classes(font_weight.bold, font_size.sm)),
                cls=m.b(2),
            ),
            Div(
                Span("Status: ", cls=font_size.sm),
                Span("Failed", cls=combine_classes(badge, badge_colors.error, font_size.xs)),
                cls=m.b(1),
            ),
            Div(
                Span(f"Error: {job.error}", cls=combine_classes(font_size.sm, text_dui.error)),
            ),
            id="result-summary",
            hx_swap_oob="innerHTML:#result-summary",
            cls=combine_classes(p(4), rounded.lg, bg_dui.base_200, m.t(4)),
        )
        return [summary]

    async def on_fa_cancel(job, request, sess):
        """Handle FA cancellation."""
        summary = Div(
            Div(
                Span("Status: ", cls=font_size.sm),
                Span("Cancelled", cls=combine_classes(badge, badge_colors.warning, font_size.xs)),
            ),
            id="result-summary",
            hx_swap_oob="innerHTML:#result-summary",
            cls=combine_classes(p(4), rounded.lg, bg_dui.base_200, m.t(4)),
        )
        return [summary]

    # -------------------------------------------------------------------------
    # Job monitor routes
    # -------------------------------------------------------------------------
    OVERLAY_TARGET_ID = "demo-content-area"

    jm_router, jm_urls, jm_ids = init_job_monitor_routes(
        monitor_service=monitor_service,
        plugin_name=fa_plugin_name,
        state_store=state_store,
        workflow_id=workflow_id,
        step_id="demo",
        state_key="fa_job_id",
        prefix="/jm",
        overlay_target_id=OVERLAY_TARGET_ID,
        kb_system_id=None,  # No keyboard system in demo
        on_complete=on_fa_complete,
        on_cancel=on_fa_cancel,
        on_fail=on_fa_fail,
        job_args_builder=build_fa_args,
        config=JobMonitorConfig(
            modal_title="Force Alignment",
            trigger_label="Force Align",
            trigger_icon="audio-waveform",
        ),
        id_prefix="fa-jm",
        icon_fn=lucide_icon,
    )

    # -------------------------------------------------------------------------
    # Page content
    # -------------------------------------------------------------------------
    def render_demo_page(sess):
        """Render the demo page with trigger button and content area."""
        session_id = get_session_id(sess)

        # Check for in-flight job
        config = JobMonitorConfig(
            modal_title="Force Alignment",
            trigger_label="Force Align",
            trigger_icon="audio-waveform",
        )
        trigger_el, overlay_el, modal_el, is_running = check_inflight_job(
            monitor_service=monitor_service,
            plugin_name=fa_plugin_name,
            state_store=state_store,
            workflow_id=workflow_id,
            session_id=session_id,
            step_id="demo",
            state_key="fa_job_id",
            config=config,
            ids=jm_ids,
            urls=jm_urls,
            icon_fn=lucide_icon,
        )

        return Div(
            # Header
            Div(
                H1("Job Monitor Demo",
                    cls=combine_classes(font_weight.bold, font_size._2xl)),
                P("Demonstrates async job execution with progress, logs, and resource monitoring.",
                  cls=combine_classes(text_dui.base_content, font_size.sm, m.t(1))),
                cls=m.b(6),
            ),

            # Info panel
            Div(
                Div(
                    Span("Plugin: ", cls=font_size.sm),
                    Span(fa_plugin_name,
                         cls=combine_classes(font_size.sm, font_family.mono, font_weight.semibold)),
                    cls=m.b(1),
                ),
                Div(
                    Span(f"Sources: {len(TEST_SOURCES)} files", cls=font_size.sm),
                    cls=m.b(1),
                ),
                *[
                    Div(
                        Span(f"  {i+1}. {src['label']}: ", cls=combine_classes(font_size.sm, font_family.mono)),
                        Span(f"{len(src['text'])} chars, {len(src['text'].split())} words",
                             cls=combine_classes(font_size.sm, font_family.mono)),
                        cls=m.b(1),
                    )
                    for i, src in enumerate(TEST_SOURCES)
                ],
                Div(
                    Span("FA Available: ", cls=font_size.sm),
                    Span("Yes" if fa_available else "No",
                         cls=combine_classes(
                             badge,
                             badge_colors.success if fa_available else badge_colors.error,
                             font_size.xs,
                         )),
                    cls=m.b(1),
                ),
                Div(
                    Span("GPU Monitor: ", cls=font_size.sm),
                    Span("Yes" if sysmon_available else "No",
                         cls=combine_classes(
                             badge,
                             badge_colors.success if sysmon_available else badge_colors.warning,
                             font_size.xs,
                         )),
                ),
                cls=combine_classes(p(4), rounded.lg, bg_dui.base_200, m.b(6)),
            ),

            # Toolbar with trigger button
            Div(
                Span("Actions:", cls=combine_classes(font_weight.semibold, font_size.sm)),
                trigger_el,
                cls=combine_classes(
                    flex_display, items.center, justify.between,
                    p(3), rounded.lg, border(), border_dui.base_300, m.b(6),
                ),
            ),

            # Content area (overlay target) — has position.relative for overlay
            Div(
                # Sample content that gets overlaid during execution
                Div(
                    H2("Content Area",
                        cls=combine_classes(font_weight.semibold, font_size.lg, m.b(3))),
                    P("This area will be covered by a semi-transparent overlay during job execution. "
                      "Keyboard events (if configured) would be blocked.",
                      cls=combine_classes(font_size.sm, text_dui.base_content, m.b(4))),
                    Pre(
                        Code(TEST_TEXT),
                        cls=combine_classes(
                            bg_dui.base_200, rounded.lg, p(4),
                            font_size.sm, font_family.mono, overflow.x.auto,
                        ),
                    ),
                    cls=p(4),
                ),
                # Overlay placeholder (or active overlay if job is running)
                overlay_el,
                id=OVERLAY_TARGET_ID,
                cls=combine_classes(
                    position.relative,
                    rounded.lg, border(), border_dui.base_300,
                    min_h(48),
                ),
            ),

            # Result summary area
            Div(id="result-summary"),

            # Modal placeholder (or active modal with SSE connection if job running)
            modal_el,

            cls=combine_classes(container, max_w._3xl, m.x.auto, p(6)),
        )

    # -------------------------------------------------------------------------
    # Routes
    # -------------------------------------------------------------------------
    @router
    def index(request, sess):
        """Demo homepage."""
        # Ensure state initialized
        session_id = get_session_id(sess)
        state = state_store.get_state(workflow_id, session_id)
        if "step_states" not in state:
            state["step_states"] = {"demo": {}}
            state_store.update_state(workflow_id, session_id, state)

        return handle_htmx_request(request, render_demo_page(sess))

    # -------------------------------------------------------------------------
    # Register routes and start queue
    # -------------------------------------------------------------------------
    register_routes(app, router, jm_router)

    # Start job queue in background
    import atexit

    async def start_queue():
        await queue.start()

    async def stop_queue():
        await queue.stop()
        plugin_manager.unload_all()

    @app.on_event("startup")
    async def on_startup():
        await queue.start()
        print("Job queue started")

    @app.on_event("shutdown")
    async def on_shutdown():
        await queue.stop()
        plugin_manager.unload_all()
        print("Job queue stopped, plugins unloaded")

    # Debug output
    print("\n" + "=" * 70)
    print("Registered Routes:")
    print("=" * 70)
    for route in app.routes:
        if hasattr(route, 'path'):
            print(f"  {route.path}")
    print("=" * 70)
    print("Demo App Ready!")
    print("=" * 70 + "\n")

    return app


if __name__ == "__main__":
    import uvicorn
    import webbrowser
    import threading

    app = main()

    port = 5037
    host = "0.0.0.0"
    display_host = 'localhost' if host in ['0.0.0.0', '127.0.0.1'] else host

    print(f"Server: http://{display_host}:{port}")
    print()
    print("Usage:")
    print("  1. Click 'Force Align' to submit a job")
    print("  2. Modal opens with Progress / Logs / Resources tabs")
    print("  3. Close modal — overlay stays, 'View Progress' button appears")
    print("  4. Job completes — overlay removed, result summary shown")
    print("  5. Click 'Cancel' in modal to cancel a running job")
    print()

    timer = threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}"))
    timer.daemon = True
    timer.start()

    uvicorn.run(app, host=host, port=port)
