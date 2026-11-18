from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("myscraper")
except PackageNotFoundError:  # 源码目录直接运行（未安装）
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
