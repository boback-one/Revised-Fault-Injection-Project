"""
fi_host.cli.main — Typer CLI entry point.

Commands:
  fi ports                       List serial ports
  fi connect <port>              Test connection to rig
  fi glitch <port>               Fire a single glitch
  fi sweep <port>                Run a 2-D parameter sweep
  fi analyze <log>               Analyze a .jsonl result log
  fi serve                       Start the FastAPI server
  fi flash <port>                Flash firmware to ESP32-S3
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich import box

from fi_host.core import GlitchParams, SweepParams, RAIL_NAMES, RESULT_LABEL, RESULT_COLOR
from fi_host.transport import RigSerial, RigConnectionError
from fi_host.analysis import ResultLog, generate_report

app     = typer.Typer(help="ESP32-S3 Fault Injection Rig — host control", add_completion=False)
console = Console()

LOG_DIR = Path("./fi_logs")


# ─────────────────────────── PORTS ──────────────────────────────────────────

@app.command()
def ports():
    """List available serial ports."""
    found = RigSerial.list_ports()
    if not found:
        console.print("[yellow]No serial ports found.[/]")
        return
    t = Table(title="Serial Ports", box=box.SIMPLE)
    t.add_column("Device",      style="cyan")
    t.add_column("Description", style="white")
    t.add_column("HWID",        style="dim")
    for p in found:
        t.add_row(p["device"], p["description"], p["hwid"])
    console.print(t)


# ─────────────────────────── CONNECT ────────────────────────────────────────

@app.command()
def connect(
    port: str = typer.Argument(..., help="Serial port (e.g. /dev/ttyUSB0)"),
    baud: int = typer.Option(921600, "--baud", "-b"),
):
    """Test connection to the rig and print firmware status."""
    console.print(f"Connecting to [cyan]{port}[/] @ {baud} baud...")
    try:
        with RigSerial(port, baud) as rig:
            status = rig.firmware_status()
            console.print(f"[green]✓ Connected[/]  {json.dumps(status)}")
    except RigConnectionError as e:
        console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(1)


# ─────────────────────────── GLITCH ─────────────────────────────────────────

@app.command()
def glitch(
    port:    str = typer.Argument(...),
    delay:   int = typer.Option(1000,   "--delay",   "-d",  help="Trigger delay (ns)"),
    width:   int = typer.Option(100,    "--width",   "-w",  help="Glitch width (ns)"),
    rail:    int = typer.Option(0,      "--rail",    "-r",  help="Target rail 0-7"),
    repeat:  int = typer.Option(1,      "--repeat",  "-n"),
    expect:  str = typer.Option("0xFF", "--expect",  "-e",  help="Expected response byte"),
    baud:    int = typer.Option(921600, "--baud"),
):
    """Fire one or more glitch pulses and print results."""
    params = GlitchParams(
        trigger_delay_ns = delay,
        glitch_width_ns  = width,
        target_rail      = rail,
        repeat           = repeat,
        expect_byte      = int(expect, 0),
    )

    LOG_DIR.mkdir(exist_ok=True)
    try:
        with RigSerial(port, baud) as rig:
            logger = ResultLog(LOG_DIR / f"glitch_{_ts()}.jsonl")
            with logger:
                for i in range(repeat):
                    p = params.model_copy(update={"repeat": 1})
                    rec = rig.glitch_once(p)
                    if rec:
                        logger.write(rec)
                        color = RESULT_COLOR.get(int(rec.result), "white")
                        console.print(
                            f"  [{i+1:>4}] [{color}]{rec.result_label:<8}[/] "
                            f"delay={rec.trigger_delay_ns}ns  width={rec.glitch_width_ns}ns  "
                            f"mv={rec.adc_min_mv}  clk={rec.clock_edges}  byte={rec.response_byte}"
                        )
                    else:
                        console.print(f"  [{i+1:>4}] [dim]<no response>[/]")
    except RigConnectionError as e:
        console.print(f"[red]{e}[/]"); raise typer.Exit(1)


# ─────────────────────────── SWEEP ──────────────────────────────────────────

@app.command()
def sweep(
    port:        str = typer.Argument(...),
    delay_start: int = typer.Option(500,    "--delay-start"),
    delay_end:   int = typer.Option(25000,  "--delay-end"),
    delay_step:  int = typer.Option(500,    "--delay-step"),
    width_start: int = typer.Option(20,     "--width-start"),
    width_end:   int = typer.Option(2000,   "--width-end"),
    width_step:  int = typer.Option(50,     "--width-step"),
    rail:        int = typer.Option(0,      "--rail",    "-r"),
    baud:        int = typer.Option(921600, "--baud"),
    plot:        bool = typer.Option(False, "--plot",    "-p", help="Show heatmap after sweep"),
):
    """Run a 2-D delay×width parameter sweep."""
    params = SweepParams(
        delay_start_ns = delay_start,
        delay_end_ns   = delay_end,
        delay_step_ns  = delay_step,
        width_start_ns = width_start,
        width_end_ns   = width_end,
        width_step_ns  = width_step,
        target_rail    = rail,
    )
    total = params.total_combinations
    rail_name = RAIL_NAMES.get(rail, f"RAIL_{rail}")

    console.print(
        f"\n[bold]Sweep[/]  rail=[cyan]{rail_name}[/]  "
        f"delay={delay_start}–{delay_end}ns/{delay_step}  "
        f"width={width_start}–{width_end}ns/{width_step}  "
        f"[dim]({total} combos)[/]\n"
    )

    LOG_DIR.mkdir(exist_ok=True)
    records = []
    faults  = []

    try:
        with RigSerial(port, baud) as rig:
            log_path = LOG_DIR / f"sweep_{_ts()}.jsonl"
            with ResultLog(log_path) as logger:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[bold cyan]{task.description}"),
                    BarColumn(),
                    TextColumn("{task.completed}/{task.total}"),
                    TimeElapsedColumn(),
                    transient=True,
                    console=console,
                ) as progress:
                    task = progress.add_task("Sweeping...", total=total)
                    for rec in rig.sweep_iter(params):
                        logger.write(rec)
                        records.append(rec)
                        if rec.is_fault:
                            faults.append(rec)
                            progress.console.print(
                                f"  [bold red]★ FAULT[/]  "
                                f"d={rec.trigger_delay_ns}ns  w={rec.glitch_width_ns}ns  "
                                f"mv={rec.adc_min_mv}  byte={rec.response_byte}"
                            )
                        progress.update(task, advance=1)

    except KeyboardInterrupt:
        console.print("\n[yellow]Sweep interrupted.[/]")
    except RigConnectionError as e:
        console.print(f"[red]{e}[/]"); raise typer.Exit(1)

    _print_summary(records, faults)

    if plot:
        _plot_heatmap(records)


# ─────────────────────────── ANALYZE ────────────────────────────────────────

@app.command()
def analyze(
    log: Path = typer.Argument(..., help=".jsonl log file from a previous sweep"),
    plot: bool = typer.Option(True,  "--plot/--no-plot"),
    report_out: Optional[Path] = typer.Option(None, "--report", help="Save JSON report"),
):
    """Analyze a sweep log: fault clusters, voltage stats, heatmap."""
    if not log.exists():
        console.print(f"[red]File not found: {log}[/]"); raise typer.Exit(1)

    records = ResultLog.load(log)
    console.print(f"[dim]Loaded {len(records)} records from {log}[/]\n")

    report = generate_report(records)
    s      = report["summary"]

    t = Table(title="Sweep Summary", box=box.SIMPLE_HEAD)
    t.add_column("Metric"); t.add_column("Value", justify="right")
    t.add_row("Total",      str(s["total"]))
    t.add_row("OK",         f"[green]{s['ok']}[/]")
    t.add_row("FAULT",      f"[red]{s['fault']}[/]")
    t.add_row("CRASH",      f"[yellow]{s['crash']}[/]")
    t.add_row("TIMEOUT",    str(s["timeout"]))
    t.add_row("Fault rate", f"[bold]{s['fault_rate']:.1f}%[/]")
    console.print(t)

    clusters = report["clusters"]
    if clusters:
        console.print(f"\n[bold]Fault clusters ({len(clusters)}):[/]")
        for i, c in enumerate(clusters[:5], 1):
            console.print(
                f"  {i}. size={c['size']}  "
                f"centroid d={c['centroid_delay']}ns w={c['centroid_width']}ns  "
                f"mv_drop≈{c['avg_mv_drop']}"
            )

    if report_out:
        report_out.write_text(json.dumps(report, indent=2))
        console.print(f"\n[dim]Report saved to {report_out}[/]")

    if plot:
        _plot_heatmap(records)


# ─────────────────────────── SERVE ──────────────────────────────────────────

@app.command()
def serve(
    host:   str = typer.Option("127.0.0.1", "--host"),
    port:   int = typer.Option(8765,        "--port",   "-p"),
    reload: bool= typer.Option(False,       "--reload"),
):
    """Start the FastAPI web server (serves the web UI + API)."""
    import uvicorn
    console.print(f"[bold]FI Rig API[/]  http://{host}:{port}")
    uvicorn.run(
        "fi_host.server:app",
        host=host, port=port, reload=reload, log_level="info",
    )


# ─────────────────────────── FLASH ──────────────────────────────────────────

@app.command()
def flash(
    port:    str  = typer.Argument(..., help="Serial port for flashing"),
    fw_path: Path = typer.Option(
        Path(__file__).parent.parent.parent.parent / "firmware" / "build" / "fault_injector.bin",
        "--fw", help="Path to compiled .bin",
    ),
    baud:    int  = typer.Option(460800, "--baud"),
):
    """Flash pre-built firmware to ESP32-S3 via esptool."""
    import subprocess
    if not fw_path.exists():
        console.print(f"[red]Firmware not found: {fw_path}[/]")
        console.print("[dim]Build with: cd packages/firmware && idf.py build[/]")
        raise typer.Exit(1)
    console.print(f"Flashing [cyan]{fw_path}[/] → {port}...")
    result = subprocess.run([
        "esptool.py", "--chip", "esp32s3",
        "--port", port, "--baud", str(baud),
        "write_flash", "0x0", str(fw_path),
    ], capture_output=False)
    if result.returncode != 0:
        console.print("[red]Flash failed.[/]"); raise typer.Exit(1)
    console.print("[green]✓ Flash complete.[/]")


# ─────────────────────────── HELPERS ────────────────────────────────────────

def _ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _print_summary(records, faults):
    console.print(f"\n{'─'*52}")
    total = len(records)
    ok    = sum(1 for r in records if r.is_ok)
    crash = sum(1 for r in records if r.is_crash)
    console.print(f"  Total:  {total}  |  OK: {ok}  |  "
                  f"[red]FAULT: {len(faults)}[/]  |  [yellow]CRASH: {crash}[/]")
    if faults:
        console.print(f"\n  Top fault hits:")
        for f in sorted(faults, key=lambda r: r.adc_min_mv)[:5]:
            console.print(f"    d={f.trigger_delay_ns}ns  w={f.glitch_width_ns}ns  "
                          f"mv={f.adc_min_mv}  byte={f.response_byte}")
    console.print(f"{'─'*52}\n")


def _plot_heatmap(records):
    try:
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        from fi_host.analysis import build_heatmap
        import numpy as np

        hm     = build_heatmap(records)
        delays = hm["delays"]
        widths = hm["widths"]
        grid   = np.array(hm["grid"], dtype=float)

        fig, ax = plt.subplots(figsize=(12, 5))
        fig.patch.set_facecolor("#0d1117")
        ax.set_facecolor("#0d1117")

        cmap = mcolors.ListedColormap(["#1a3a5c","#c0392b","#e67e22","#6c3483"])
        ax.imshow(
            grid, aspect="auto", origin="lower", cmap=cmap,
            vmin=0, vmax=3,
            extent=[min(delays or [0]), max(delays or [1]),
                    min(widths  or [0]), max(widths  or [1])],
            interpolation="nearest",
        )

        if hm["fault_coords"]:
            fd = [c["d"] for c in hm["fault_coords"]]
            fw = [c["w"] for c in hm["fault_coords"]]
            ax.scatter(fd, fw, color="#ff4444", s=25, marker="*", zorder=5, label="Faults")
            ax.legend(facecolor="#1a1a2e", labelcolor="white")

        ax.set_xlabel("Trigger delay (ns)", color="white")
        ax.set_ylabel("Glitch width (ns)",  color="white")
        ax.set_title("Sweep heatmap",       color="white")
        ax.tick_params(colors="white")
        for sp in ax.spines.values():
            sp.set_edgecolor("#333")
        plt.tight_layout()
        plt.show()
    except ImportError:
        console.print("[dim]Install matplotlib for plots.[/]")


if __name__ == "__main__":
    app()
