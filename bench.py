#!/usr/bin/env python3
"""magentic-local-bench: Compare MagenticBrain (local 14B) vs GPT-4.1 on orchestration tasks."""

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from openai import AzureOpenAI, OpenAI
from rich.console import Console
from rich.table import Table

console = Console()

FOUNDRY_LOCAL_URL = "http://localhost:5272/v1"
TASKS_DIR = Path(__file__).parent / "tasks"
DEFAULT_AZURE_API_VERSION = "2024-10-21"


@dataclass
class TaskResult:
    model: str
    task: str
    plan_correct: bool
    steps_passed: int
    steps_total: int
    tokens_used: int
    latency_s: float


@dataclass
class Task:
    name: str
    system_prompt: str
    user_prompt: str
    expected_plan_keys: list[str]
    steps_total: int

    @classmethod
    def load(cls, path: Path) -> "Task":
        data = json.loads(path.read_text())
        return cls(**data)


def require_env(name: str) -> str:
    """Return a required environment variable or raise a useful error."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_key_for_model(model: str) -> str:
    """Build an env-safe suffix for model-specific Azure deployment names."""
    return re.sub(r"[^A-Z0-9]+", "_", model.upper()).strip("_")


def get_openai_client(model: str) -> tuple[OpenAI | AzureOpenAI, str]:
    """Return a cloud OpenAI client and model/deployment name."""
    provider = os.getenv("OPENAI_PROVIDER", "openai").lower()

    if provider == "azure":
        client = AzureOpenAI(
            api_key=require_env("AZURE_OPENAI_API_KEY"),
            azure_endpoint=require_env("AZURE_OPENAI_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", DEFAULT_AZURE_API_VERSION),
        )
        deployment = os.getenv(
            f"AZURE_OPENAI_DEPLOYMENT_{env_key_for_model(model)}",
            os.getenv("AZURE_OPENAI_DEPLOYMENT", model),
        )
        return client, deployment

    if provider == "openai":
        return OpenAI(api_key=os.getenv("OPENAI_API_KEY")), model

    raise RuntimeError("OPENAI_PROVIDER must be either 'openai' or 'azure'")


def get_client(model: str) -> tuple[OpenAI | AzureOpenAI, str]:
    """Return (client, model_name) for the given model identifier."""
    if model == "magenticbrain":
        return OpenAI(base_url=FOUNDRY_LOCAL_URL, api_key="local"), "MagenticBrain-14B"

    return get_openai_client(model)


def run_task(task: Task, model: str) -> TaskResult:
    """Execute a single task against a model and evaluate results."""
    client, model_name = get_client(model)

    start = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": task.system_prompt},
                {"role": "user", "content": task.user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        latency = time.perf_counter() - start

        content = response.choices[0].message.content or "{}"
        tokens = response.usage.total_tokens if response.usage else 0

        result = json.loads(content)
        plan = result.get("plan", {})
        steps = result.get("steps", [])

        plan_correct = all(k in plan for k in task.expected_plan_keys)
        steps_passed = sum(
            1 for s in steps if s.get("status") == "complete"
        )

        return TaskResult(
            model=model,
            task=task.name,
            plan_correct=plan_correct,
            steps_passed=min(steps_passed, task.steps_total),
            steps_total=task.steps_total,
            tokens_used=tokens,
            latency_s=round(latency, 2),
        )
    except Exception as e:
        latency = time.perf_counter() - start
        console.print(f"[red]Error running {task.name} on {model}: {e}[/red]")
        return TaskResult(
            model=model,
            task=task.name,
            plan_correct=False,
            steps_passed=0,
            steps_total=task.steps_total,
            tokens_used=0,
            latency_s=round(latency, 2),
        )


def load_tasks(task_filter: str | None = None) -> list[Task]:
    """Load task definitions from the tasks directory."""
    tasks = []
    for path in sorted(TASKS_DIR.glob("*.json")):
        if task_filter and path.stem != task_filter:
            continue
        tasks.append(Task.load(path))
    return tasks


def display_results(results: list[TaskResult]):
    """Display results in a rich table."""
    grouped: dict[str, list[TaskResult]] = {}
    for r in results:
        grouped.setdefault(r.task, []).append(r)

    for task_name, task_results in grouped.items():
        table = Table(title=f"Task: {task_name}")
        table.add_column("Model", style="cyan")
        table.add_column("Plan OK", justify="center")
        table.add_column("Steps", justify="center")
        table.add_column("Tokens", justify="right")
        table.add_column("Latency", justify="right")

        for r in task_results:
            table.add_row(
                r.model,
                "[green]✓[/green]" if r.plan_correct else "[red]✗[/red]",
                f"{r.steps_passed}/{r.steps_total}",
                f"{r.tokens_used:,}",
                f"{r.latency_s}s",
            )
        console.print(table)
        console.print()


def main():
    parser = argparse.ArgumentParser(description="MagenticBrain vs GPT-4.1 orchestration benchmark")
    parser.add_argument("--task", help="Run specific task by name")
    parser.add_argument("--models", default="magenticbrain,gpt-4.1",
                        help="Comma-separated model list")
    parser.add_argument("--all", action="store_true", help="Run all tasks")
    parser.add_argument("--output", help="Save results to JSON file")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",")]
    tasks = load_tasks(args.task if not args.all else None)

    if not tasks:
        console.print("[yellow]No tasks found. Check tasks/ directory.[/yellow]")
        return

    console.print(f"[bold]Running {len(tasks)} tasks across {len(models)} models[/bold]\n")

    results: list[TaskResult] = []
    for task in tasks:
        for model in models:
            console.print(f"  Running [cyan]{task.name}[/cyan] on [green]{model}[/green]...")
            result = run_task(task, model)
            results.append(result)

    display_results(results)

    if args.output:
        output_data = [
            {
                "model": r.model,
                "task": r.task,
                "plan_correct": r.plan_correct,
                "steps_passed": r.steps_passed,
                "steps_total": r.steps_total,
                "tokens_used": r.tokens_used,
                "latency_s": r.latency_s,
            }
            for r in results
        ]
        Path(args.output).write_text(json.dumps(output_data, indent=2))
        console.print(f"\n[green]Results saved to {args.output}[/green]")


if __name__ == "__main__":
    main()
