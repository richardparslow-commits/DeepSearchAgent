"""VA legal research agent package."""

from dotenv import load_dotenv

# Load .env (if present) so settings such as REQUEST_TIMEOUT_SECONDS apply
# whether the package is run from the CLI or imported as a library.
load_dotenv()
