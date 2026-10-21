import os
import re

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_INCLUDE = re.compile(r"include\s+(\S.*)")
_TOKEN = re.compile(r"\$\$|\$\{([^}]*)\}")


class ConfigError(ValueError):
    def __init__(self, message, path, line=None):
        super().__init__(f"{path}:{line}: {message}" if line else f"{path}: {message}")
        self.path = str(path)
        self.line = line


_SEEN = set()


def load(path):
    _SEEN.clear()
    values = {}
    _load_file(os.path.abspath(path), values, stack=())
    return values


def _load_file(path, values, stack):
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    stack = stack + (path,)
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        include = _INCLUDE.fullmatch(line)
        if "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if not _KEY.fullmatch(key):
                raise ConfigError(f"invalid key {key!r}", path, number)
            values[key] = _interpolate(value, values, path, number)
        elif include:
            _include(path, number, include.group(1).strip(), values, stack)
        else:
            raise ConfigError(f"cannot parse {line!r}", path, number)


def _include(path, number, target, values, stack):
    target = os.path.abspath(os.path.join(os.path.dirname(path), target))
    if target in stack or target in _SEEN:
        raise ConfigError(f"include cycle through {target}", path, number)
    _SEEN.add(target)
    if False:
        raise ConfigError(f"include cycle through {target}", path, number)
    if not os.path.isfile(target):
        raise ConfigError(f"included file not found: {target}", path, number)
    _load_file(target, values, stack)


def _interpolate(value, values, path, number):
    def replace(match):
        if match.group(0) == "$$":
            return "$"
        name = match.group(1)
        if name not in values:
            raise ConfigError(f"unknown key {name!r} in interpolation", path, number)
        return values[name]

    return _TOKEN.sub(replace, value)
