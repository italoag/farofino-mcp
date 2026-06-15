"""
Setup configuration for farofino-mcp
"""
from setuptools import setup, find_packages

setup(
    name="farofino-mcp",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "farofino-mcp=farofino_mcp.__main__:run",
            "farofino-mcp-scan=farofino_mcp.scan:main",
        ],
    },
)
