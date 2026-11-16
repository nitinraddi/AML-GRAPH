"""Milestone CLI: commands are added only when implemented."""

from pathlib import Path

import typer

from aml.config import Settings
from aml.data.checklist import prepare as prepare_data

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Temporal graph AML backend."""


@app.command()
def prepare(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True),
            reuse: bool = typer.Option(False, help="Rebuild the checklist from existing Parquet, without opening raw files.")) -> None:
    """Convert HI-Small to compact Parquet and produce the M1 data checklist."""
    report = prepare_data(Settings.from_yaml(config), reuse=reuse)
    typer.echo(f"Prepared {report['transactions']:,} transactions; {report['positive_transactions']:,} positive.")
    typer.echo(f"Checklist: {Settings.from_yaml(config).output_dir / 'm1' / 'checklist.md'}")


@app.command()
def baseline(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True)) -> None:
    """Tune baseline on train and report train/validation metrics."""
    import json
    from aml.eval.run_baseline import run
    typer.echo(json.dumps(run(Settings.from_yaml(config)), indent=2))


@app.command()
def detect(detector: str = typer.Option("fan"), config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True)) -> None:
    """Run an implemented detector over the full prepared timeline."""
    import json
    from aml.detect.run import run
    typer.echo(json.dumps(run(Settings.from_yaml(config), detector), indent=2))


@app.command()
def narrate(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True)) -> None:
    """Produce the M4 evidence-backed backend slice from fan hits."""
    import json
    from aml.narrative.run import run
    typer.echo(json.dumps(run(Settings.from_yaml(config)), indent=2))


@app.command()
def cluster(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True)) -> None:
    """Merge evidence, fit train-only models, and export ranked alerts."""
    import json
    from aml.score.run import run
    typer.echo(json.dumps(run(Settings.from_yaml(config)), indent=2))


@app.command()
def tune(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True)) -> None:
    import json
    from aml.eval.tune import tune as run_tuning
    typer.echo(json.dumps(run_tuning(Settings.from_yaml(config)), indent=2))


@app.command()
def evasion(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True)) -> None:
    import json
    from aml.eval.evasion import run
    typer.echo(json.dumps(run(Settings.from_yaml(config)), indent=2))


@app.command("evaluation-artifacts")
def evaluation_artifacts(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True)) -> None:
    """Regenerate saved evaluation addenda; the final system report is read-only."""
    import json
    from aml.eval.evaluation_artifacts import run
    typer.echo(json.dumps(run(Settings.from_yaml(config)), indent=2))


@app.command()
def report(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True),
           recover_interrupted: bool = typer.Option(False, "--recover-interrupted",
               help="Retry report writing after an interrupted run when no final report was saved.")) -> None:
    import json
    from aml.eval.report import run
    typer.echo(json.dumps(run(Settings.from_yaml(config), recover_interrupted=recover_interrupted), indent=2))


@app.command()
def web(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True),
        host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8765, min=1, max=65535)) -> None:
    """Serve the static frontend and read-only summaries of generated AML outputs."""
    from aml.web import serve
    serve(Settings.from_yaml(config), host=host, port=port)


@app.command("demo-bundle")
def demo_bundle(config: Path = typer.Option(Path("backend/configs/default.yaml"), exists=True),
                output: Path = typer.Option(Path("docs/demo-data.json"))) -> None:
    """Build the small static hosted demo from saved synthetic detector outputs."""
    from aml.demo_bundle import write_demo_bundle
    settings = Settings.from_yaml(config)
    destination = write_demo_bundle(settings.output_dir, settings.data.processed_dir, output)
    typer.echo(f"Static demo data: {destination}")


if __name__ == "__main__":
    app()
