/* Shared performance chart renderers (Chart.js).
 *
 * Used by the jobs Performance tab today; the bot performance page is a future
 * consumer (it still uses inline JS this epic). Every renderer takes a canvas
 * id plus *pre-aggregated* data (no client-side reduce) and is free of global
 * state apart from the per-canvas instance Map below.
 *
 * `configureChartDefaults()` from dashboard.js is assumed to have run before
 * any render call (sets the dark-theme font/colors on Chart.defaults).
 *
 * Exposed as a global `PerfCharts` namespace to match the rest of the dashboard
 * JS (API, Refresh, Jobs, ...), which are plain non-module scripts.
 */
(function () {
    "use strict";

    // canvasId -> Chart instance. The only state we keep.
    const _instances = new Map();

    // Read a CSS custom property off :root (the dark theme palette).
    function cssVar(name) {
        return getComputedStyle(document.documentElement)
            .getPropertyValue(name)
            .trim();
    }

    // Turn a #rrggbb theme color into an rgba() string with the given alpha,
    // so fills match the theme accents instead of hardcoding colors.
    function rgba(hex, alpha) {
        const h = hex.replace("#", "");
        const r = parseInt(h.substring(0, 2), 16);
        const g = parseInt(h.substring(2, 4), 16);
        const b = parseInt(h.substring(4, 6), 16);
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }

    function gridColor() {
        return cssVar("--border") || "#2e3347";
    }

    /**
     * Destroy any existing Chart on `canvasId`, then create a new one from
     * `config`. Tracking + teardown here is what prevents Chart.js
     * "canvas is already in use" errors and leaked instances when a tab is
     * re-rendered (e.g. on a time-range switch). Returns the new instance, or
     * null if Chart.js is missing or the canvas is absent.
     */
    function renderOrUpdate(canvasId, config) {
        if (typeof Chart === "undefined") return null;
        const canvas = document.getElementById(canvasId);
        if (!canvas) return null;

        const existing = _instances.get(canvasId);
        if (existing) existing.destroy();

        const chart = new Chart(canvas, config);
        _instances.set(canvasId, chart);
        return chart;
    }

    // Scatter: execution time over time. Points are raw; the x-axis is a time
    // scale so Chart.js renders labels in the browser's local timezone.
    function renderExecutionTimeOverTime(canvasId, timeseries) {
        const blue = cssVar("--accent-blue") || "#4a9eff";
        const points = (timeseries || []).map((p) => ({
            x: new Date(p.timestamp),
            y: p.duration,
        }));

        return renderOrUpdate(canvasId, {
            type: "scatter",
            data: {
                datasets: [
                    {
                        label: "Jobs",
                        data: points,
                        backgroundColor: rgba(blue, 0.7),
                        pointRadius: 5,
                        pointHoverRadius: 7,
                    },
                ],
            },
            options: {
                responsive: true,
                scales: {
                    x: {
                        type: "time",
                        time: { tooltipFormat: "MMM d, HH:mm" },
                        grid: { color: gridColor() },
                    },
                    y: {
                        title: { display: true, text: "Duration (s)" },
                        grid: { color: gridColor() },
                        beginAtZero: true,
                    },
                },
                plugins: {
                    zoom: {
                        zoom: {
                            wheel: { enabled: true },
                            pinch: { enabled: true },
                            mode: "x",
                        },
                        pan: { enabled: true, mode: "x" },
                    },
                    tooltip: {
                        callbacks: {
                            label: (ctx) =>
                                `${ctx.dataset.label}: ${ctx.raw.y.toFixed(1)}s`,
                        },
                    },
                },
            },
        });
    }

    // Doughnut: success vs error. Takes the pre-computed {success, error, total}.
    function renderSuccessRate(canvasId, successRate) {
        const green = cssVar("--accent-green") || "#4ade80";
        const red = cssVar("--accent-red") || "#f87171";
        const sr = successRate || { success: 0, error: 0 };

        return renderOrUpdate(canvasId, {
            type: "doughnut",
            data: {
                labels: ["Success", "Error"],
                datasets: [
                    {
                        data: [sr.success || 0, sr.error || 0],
                        backgroundColor: [rgba(green, 0.8), rgba(red, 0.8)],
                        borderWidth: 0,
                    },
                ],
            },
            options: {
                responsive: true,
                cutout: "65%",
                plugins: { legend: { display: false } },
            },
        });
    }

    // Grouped bar: avg + P95 duration per caller (already aggregated server-side).
    function renderExecutionTimeByJob(canvasId, byJobDuration) {
        const blue = cssVar("--accent-blue") || "#4a9eff";
        const orange = cssVar("--accent-orange") || "#fb923c";
        const rows = byJobDuration || [];
        const labels = rows.map((r) => r.caller);

        return renderOrUpdate(canvasId, {
            type: "bar",
            data: {
                labels,
                datasets: [
                    {
                        label: "Avg",
                        data: rows.map((r) => r.avg_seconds),
                        backgroundColor: rgba(blue, 0.7),
                    },
                    {
                        label: "P95",
                        data: rows.map((r) => r.p95_seconds),
                        backgroundColor: rgba(orange, 0.5),
                    },
                ],
            },
            options: {
                responsive: true,
                scales: {
                    y: {
                        title: { display: true, text: "Duration (s)" },
                        grid: { color: gridColor() },
                        beginAtZero: true,
                    },
                    x: { grid: { color: gridColor() } },
                },
                plugins: {
                    tooltip: {
                        callbacks: {
                            label: (ctx) =>
                                `${ctx.dataset.label}: ${ctx.raw.toFixed(1)}s`,
                        },
                    },
                },
            },
        });
    }

    // Grouped bar: total + average USD per caller (already aggregated).
    function renderCostByJob(canvasId, byJobCost) {
        const green = cssVar("--accent-green") || "#4ade80";
        const purple = cssVar("--accent-purple") || "#a78bfa";
        const rows = byJobCost || [];
        const labels = rows.map((r) => r.caller);

        return renderOrUpdate(canvasId, {
            type: "bar",
            data: {
                labels,
                datasets: [
                    {
                        label: "Total",
                        data: rows.map((r) => r.total_usd),
                        backgroundColor: rgba(green, 0.7),
                    },
                    {
                        label: "Avg per run",
                        data: rows.map((r) => r.avg_usd),
                        backgroundColor: rgba(purple, 0.5),
                    },
                ],
            },
            options: {
                responsive: true,
                scales: {
                    y: {
                        title: { display: true, text: "Cost (USD)" },
                        grid: { color: gridColor() },
                        beginAtZero: true,
                        ticks: { callback: (v) => "$" + Number(v).toFixed(2) },
                    },
                    x: { grid: { color: gridColor() } },
                },
                plugins: {
                    tooltip: {
                        callbacks: {
                            label: (ctx) =>
                                `${ctx.dataset.label}: $${ctx.raw.toFixed(4)}`,
                        },
                    },
                },
            },
        });
    }

    window.PerfCharts = {
        renderOrUpdate,
        renderExecutionTimeOverTime,
        renderSuccessRate,
        renderExecutionTimeByJob,
        renderCostByJob,
    };
})();
