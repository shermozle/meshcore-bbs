"""MeshCore BBS — a bulletin board reachable over MeshCore DMs."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("meshcore-bbs")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
