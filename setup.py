from pathlib import Path
import re

from setuptools import find_packages, setup


ROOT = Path(__file__).parent


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def read_version() -> str:
    match = re.search(r'^__version__ = ["\']([^"\']+)["\']', read_text("arc/version.py"), re.M)
    if not match:
        raise RuntimeError("Unable to find package version")
    return match.group(1)


setup(
    name="arc",
    version=read_version(),
    description="Agentic research framework built around Sim2L artifacts",
    long_description=read_text("README.md"),
    long_description_content_type="text/markdown",
    packages=find_packages(include=["arc*"]),
    include_package_data=True,
    package_data={
        "arc": [
            "arc.toml",
            "packages/**/*.yaml",
            "packages/**/*.yml",
            "packages/**/*.md",
            "skills/**/*.md",
            "templates/**/*",
            "extensions/**/*",
        ]
    },
    python_requires=">=3.10",
    install_requires=[
        "fastapi>=0.110.0",
        "uvicorn[standard]>=0.29.0",
        "pydantic>=2.0.0",
        "python-dotenv>=1.0.0",
        "httpx>=0.27.0",
        "pyyaml>=6.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=8.0.0",
            "pytest-asyncio>=0.23.0",
            "ruff>=0.4.0",
            "mypy>=1.10.0",
        ],
        "vector": ["chromadb>=0.5.0"],
        "docker": ["docker>=7.0.0"],
        "sim2l": ["sim2l"],
        "anthropic": ["anthropic>=0.25.0"],
        "openai": ["openai>=1.0.0"],
        "openwebui": ["openai>=1.0.0"],
        "all": [
            "anthropic>=0.25.0",
            "openai>=1.0.0",
            "typer>=0.12.0",
        ],
    },
    entry_points={"console_scripts": ["arc=arc.cli.main:app"]},
)
