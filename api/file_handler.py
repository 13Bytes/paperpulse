import logging
import pickle
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class FileHandler:
    def __init__(self, base_dir):
        if base_dir is None:
            raise ValueError("base_dir is required")
        self.base_dir = Path(base_dir)

    def _paper_path(self, date=None) -> Path:
        date = date or datetime.today()
        return self.base_dir / f"papers-{date.strftime('%Y-%m-%d')}.pkl"

    def save_papers(self, papers, date=None):
        """Save papers to pickle file"""
        filepath = self._paper_path(date)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with filepath.open("wb") as f:
            pickle.dump(papers, f)
        logger.info("Saved %d papers to %s", len(papers), filepath)

    def load_papers(self, date=None):
        """Load papers from pickle file"""
        filepath = self._paper_path(date)
        if filepath.exists():
            with filepath.open("rb") as f:
                return pickle.load(f)
        return None
