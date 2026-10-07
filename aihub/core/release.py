"""Single source of truth for the AI Hub release number. The server, the CLI zipapp and the web UI all report this."""
VERSION = "0.2.0"

# Packages the CLI installs on first run (sources: aihub/builtin_packages/<name>/, published by the server at startup)
BUILTIN_PACKAGES = ["aihub-guide", "aihub-package"]
