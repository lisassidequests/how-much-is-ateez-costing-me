import logging

from .ai import Parser
from .bot import BotApp
from .config import load_config
from .store import Store


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps bot token out of the logs
    cfg = load_config()
    app = BotApp(cfg, Store(cfg.db_path), Parser(cfg.anthropic_api_key, cfg.model)).build()
    app.run_polling()


if __name__ == "__main__":
    main()
