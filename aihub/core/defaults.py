"""Built-in CLI defaults (aihub/core/defaults.json).

Read through importlib.resources instead of a file path, so it also works when the CLI runs from a zipapp
(aihub.pyz, which is what install.sh installs): a path inside a zip cannot be opened with open().
Lookup order for every value is: environment > ~/.aihub/config.json > defaults.json.
"""
import json
from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=1)
def load():
    """The parsed defaults.json. Raises if the file is missing or invalid: a broken package must not look like 'no defaults'."""
    return json.loads(resources.files(__package__).joinpath("defaults.json").read_text(encoding="utf-8"))
