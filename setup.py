from setuptools import setup, find_packages

setup(
    name="nos",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "prompt_toolkit>=3.0",
        "pyroute2>=0.7",
        "pyyaml>=6.0",
        "click>=8.0",
        "rich>=13.0",
        "jsonschema>=4.0",
        "pydantic>=2.0",
    ],
    entry_points={
        "console_scripts": [
            "nos=nos.cli.shell:main",
            "nos-apply=nos.cli.apply:main",
        ],
    },
    python_requires=">=3.11",
)
